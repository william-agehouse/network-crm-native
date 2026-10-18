"""crm/db.py core: name normalisation, contacts CRUD/filters/paging, the per-channel count+last columns, and the ensure_columns migration path."""

import csv
import datetime
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, arc, db, ex, importers,
)


class TestNormalize(unittest.TestCase):
    def test_strips_decoration_and_keeps_accents(self):
        self.assertEqual(db.normalize_name("José Núñez ⟢"),
                         db.normalize_name("José Núñez"))
        self.assertNotEqual(db.normalize_name("Núñez"), db.normalize_name("Nunez"))
        self.assertEqual(db.normalize_name("  Ada   LOVELACE "), "ada lovelace")


class TestContacts(Base):
    def test_crud_notes_search_dedupe(self):
        a = db.create_contact(self.conn, "Ada Lovelace", source="linkedin")
        db.set_field(self.conn, a, "company", "Analytical Engines")
        db.add_note(self.conn, a, "Met at a maths salon.")
        self.conn.commit()
        self.assertEqual(len(db.get_notes(self.conn, a)), 1)
        # search by normalized substring, exact match ranks first
        db.create_contact(self.conn, "Adam Adamson")
        self.conn.commit()
        rows = db.search_contacts(self.conn, "ada lovelace")
        self.assertEqual(rows[0]["name"], "Ada Lovelace")

    def test_note_edit_delete(self):
        c = db.create_contact(self.conn, "Bob")
        n = db.add_note(self.conn, c, "first")
        db.update_note(self.conn, n, "edited")
        self.conn.commit()
        self.assertEqual(db.get_notes(self.conn, c)[0]["body"], "edited")
        db.delete_note(self.conn, n)
        self.conn.commit()
        self.assertEqual(len(db.get_notes(self.conn, c)), 0)

    def test_list_filters(self):
        a = db.create_contact(self.conn, "New Person")
        b = db.create_contact(self.conn, "Done Person")
        db.add_note(self.conn, b, "note")
        db.mark_reviewed(self.conn, b, "described")
        x = db.create_contact(self.conn, "Hidden Person")
        db.set_excluded(self.conn, x, 1)
        self.conn.commit()
        self.assertEqual({r["id"] for r in db.list_contacts(self.conn, flt="todo")}, {a})
        self.assertEqual({r["id"] for r in db.list_contacts(self.conn, flt="described")}, {b})
        self.assertEqual({r["id"] for r in db.list_contacts(self.conn, flt="excluded")}, {x})
        # default 'all' must ALSO hide excluded people: the ⌘K jump query
        # and People 'Everyone' both come through here with no filter
        default_ids = {r["id"] for r in db.list_contacts(self.conn)}
        self.assertNotIn(x, default_ids)
        self.assertEqual(default_ids, {a, b})

    def test_contact_count_matches_the_list_it_counts(self):
        """count_contacts must agree with list_contacts under every filter — they
        share a WHERE builder precisely so the count can't drift from the list
        and put a wrong total on screen."""
        a = db.create_contact(self.conn, "Todo Person")
        b = db.create_contact(self.conn, "Done Person")
        db.add_note(self.conn, b, "note")
        db.mark_reviewed(self.conn, b, "described")
        x = db.create_contact(self.conn, "Hidden Person")
        db.set_excluded(self.conn, x, 1)
        db.set_field(self.conn, a, "home_metro", "Paris")
        self.conn.commit()
        for kwargs in ({}, {"flt": "todo"}, {"flt": "described"}, {"flt": "excluded"},
                       {"flt": "deleted"}, {"q": "person"}, {"location": "Paris"},
                       {"location": "Nowhere"}):
            rows = db.list_contacts(self.conn, limit=9999, **kwargs)
            self.assertEqual(db.count_contacts(self.conn, **kwargs), len(rows), kwargs)

    def test_contact_list_paging_reports_the_full_total(self):
        """A page must never be mistakable for the whole set: the total stays the
        real number however small the page is. A large list arriving as 500 rows
        with no total is what makes it read as "500 people"."""
        for i in range(7):
            db.create_contact(self.conn, f"Person {i}")
        self.conn.commit()
        self.assertEqual(db.count_contacts(self.conn), 7)
        self.assertEqual(len(db.list_contacts(self.conn, limit=3)), 3)
        self.assertEqual(db.count_contacts(self.conn), 7)      # unchanged by paging
        self.assertEqual(len(db.list_contacts(self.conn, limit=9999)), 7)
        # the closeness sort ranks in Python over every matching row, so its page
        # is a slice of the ranked whole rather than a ranked slice
        self.assertEqual(len(db.list_contacts(self.conn, limit=3, sort="closeness")), 3)
        self.assertEqual(len(db.list_contacts(self.conn, limit=9999, sort="closeness")), 7)

    def test_batch_excludes_excluded(self):
        keep = db.create_contact(self.conn, "Keep Me")
        hide = db.create_contact(self.conn, "Hide Me")
        db.set_excluded(self.conn, hide, 1)
        self.conn.commit()
        ids = {c["id"] for c in db.pick_review_batch(self.conn, 10)}
        self.assertIn(keep, ids)
        self.assertNotIn(hide, ids)

    def test_closeness_sort_ranks_by_strength_not_recency(self):
        """The Closeness order must rank on relationship strength across ALL
        matching rows — it used to fall through to last_contact DESC."""
        close = db.create_contact(self.conn, "Close Cleo")
        db.set_channel(self.conn, close, "whatsapp", 900, "2020-01-01 10:00:00")
        db.add_note(self.conn, close, "years of history")
        recent = db.create_contact(self.conn, "Recent Rita")   # newest touch, barely known
        db.set_channel(self.conn, recent, "linkedin", 1, "2026-07-24 10:00:00")
        self.conn.commit()
        names = [r["name"] for r in db.list_contacts(self.conn, sort="closeness")]
        self.assertLess(names.index("Close Cleo"), names.index("Recent Rita"))
        # and the recent order still puts the fresh touch first
        rec = [r["name"] for r in db.list_contacts(self.conn, sort="recent")]
        self.assertLess(rec.index("Recent Rita"), rec.index("Close Cleo"))

    def test_dry_run_is_honored_or_refused_never_ignored(self):
        """A preview flag that got silently dropped would run the real,
        irreversible thing while the user thought they'd asked for a preview."""
        # the one source whose script honors --dry-run end to end
        cmd = app._build_command("linkedin_facts", {"dry_run": True})
        self.assertIn("--dry-run", cmd)
        self.assertNotIn("--dry-run", app._build_command("linkedin_facts", {}))
        # everything else must REFUSE rather than pretend
        for key, spec in importers.REGISTRY.items():
            if spec.supports_dry_run:
                continue
            with self.assertRaises(app.ApiError, msg=f"{key} silently ignored dry_run"):
                app._build_command(key, {"dry_run": True, "path": "/tmp/x", "years": 1})

    def test_chat_cards_match_the_answer(self):
        cards = [{"id": 1, "name": "Åke Bergström"}, {"id": 2, "name": "Nils Dahl"}]
        reply = "The clearest London person is **Åke Bergström** — based in London."
        kept = app._cards_for_reply(reply, cards)
        self.assertEqual([c["name"] for c in kept], ["Åke Bergström"])
        # paraphrased answers keep every card rather than showing none
        self.assertEqual(app._cards_for_reply("Nobody obvious.", cards), cards)

    def test_soft_delete_hides_everywhere_and_restores(self):
        keep = db.create_contact(self.conn, "Keep Me")
        gone = db.create_contact(self.conn, "Delete Me")
        db.add_note(self.conn, gone, "note")   # a signal, so it would otherwise surface
        db.set_deleted(self.conn, gone, 1)
        self.conn.commit()
        # hidden from the default list; shows only in the Deleted view, not Excluded
        self.assertNotIn(gone, {r["id"] for r in db.list_contacts(self.conn)})
        self.assertNotIn(gone, {r["id"] for r in db.list_contacts(self.conn, flt="excluded")})
        self.assertEqual({r["id"] for r in db.list_contacts(self.conn, flt="deleted")}, {gone})
        # inherits excluded filtering: review batch and reconnect both skip it
        self.assertNotIn(gone, {r["id"] for r in db.pick_review_batch(self.conn, 50)})
        self.assertNotIn(gone, {r["id"] for r in db.find_reconnect(self.conn)})
        # restore brings it back to the default list
        db.set_deleted(self.conn, gone, 0)
        self.conn.commit()
        self.assertIn(gone, {r["id"] for r in db.list_contacts(self.conn)})

    def test_merge_combines_notes_and_channel_stats(self):
        a = db.create_contact(self.conn, "Dup Name")
        b = db.create_contact(self.conn, "Dup Name")
        db.add_note(self.conn, a, "from a")
        db.add_note(self.conn, b, "from b")
        self.conn.execute("UPDATE contacts SET wa_count=5, imsg_count=3, wa_last='2026-03-01' WHERE id=?", (a,))
        self.conn.execute("UPDATE contacts SET wa_count=2, imsg_count=7, wa_last='2026-06-01' WHERE id=?", (b,))
        self.conn.commit()
        db.merge_contacts(self.conn, a, b)
        self.conn.commit()
        self.assertEqual(len(db.get_notes(self.conn, a)), 2)
        m = db.get_contact(self.conn, a)
        self.assertEqual(m["wa_count"], 7)
        self.assertEqual(m["imsg_count"], 10)
        self.assertEqual(m["wa_last"], "2026-06-01")
        self.assertEqual(len(db.find_exact(self.conn, "Dup Name")), 1)

    def test_linkedin_upsert_derives_role_type_like_csv(self):
        """The one-click LinkedIn path must fill role_type too, not only
        the CSV fallback — same helper, same free facts either way."""
        from enrich_facts import _match_role_type
        title, company = "Founder", "Acme Works"
        cid = db.upsert_contact_from_linkedin(
            self.conn, name="Fia Founder", position=title,
            company=company, connected_on="2025-01-01")
        self.conn.commit()
        got = db.get_contact(self.conn, cid)["role_type"]
        self.assertEqual(got, "founder")
        # parity: identical to what the CSV path's helper would derive
        self.assertEqual(got, _match_role_type(f"{title} {company}"))
        self.assertEqual(db.derive_role_type(""), "")   # empty in, empty out
        # an unrecognized title stays empty rather than guessing
        cid2 = db.upsert_contact_from_linkedin(
            self.conn, name="Vague Vera", position="Partner", company="Somewhere",
            connected_on="2025-01-01")
        self.conn.commit()
        self.assertEqual(db.get_contact(self.conn, cid2)["role_type"], "")

    def test_purge_contact_removes_everything(self):
        """Permanent delete clears the contact and all its crm.db rows."""
        cid = db.create_contact(self.conn, "Purge Me")
        db.add_note(self.conn, cid, "a note")
        db.add_identifier(self.conn, cid, "email", "p@example.com")
        lid = db.create_list(self.conn, "L")
        db.add_list_members(self.conn, lid, [cid])
        self.conn.commit()
        db.purge_contact(self.conn, cid)
        self.conn.commit()
        self.assertIsNone(db.get_contact(self.conn, cid))
        self.assertEqual(db.get_notes(self.conn, cid), [])
        self.assertEqual(db.get_identifiers(self.conn, cid), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM list_members WHERE contact_id=?", (cid,)).fetchone()[0], 0)

    def test_merge_preserves_enrichment_facts(self):
        """Merging must not lose the discarded record's enrichment — even
        when the KEPT record is the bare one (the old one-click default)."""
        bare = db.create_contact(self.conn, "Same Name")
        rich = db.create_contact(self.conn, "Same Name")
        db.set_field(self.conn, rich, "home_metro", "Paris")
        self.conn.execute(
            "UPDATE contacts SET role_type='investor', topics='[\"robotics\"]', "
            "blurb='LP at X' WHERE id=?", (rich,))
        self.conn.commit()
        db.merge_contacts(self.conn, bare, rich)   # keep the BARE record
        self.conn.commit()
        kept = db.get_contact(self.conn, bare)
        self.assertEqual(kept["home_metro"], "Paris")
        self.assertEqual(kept["role_type"], "investor")
        self.assertIn("robotics", kept["topics"])
        self.assertEqual(kept["blurb"], "LP at X")

    def test_merge_keeps_the_discarded_records_dates(self):
        """Regression: contact_dates references contacts ON DELETE CASCADE and
        PRAGMA foreign_keys is ON, so deleting the duplicate used to take its
        birthdays and anniversaries with it — silently, leaving no orphan rows to
        notice afterwards."""
        keep = db.create_contact(self.conn, "Grace Hopper")
        dup = db.create_contact(self.conn, "Amazing Grace")
        for cid, kind, on_date in ((dup, "birthday", "1906-12-09"),
                                   (keep, "anniversary", "2020-06-01"),
                                   (dup, "anniversary", "2020-06-01")):   # already on keep
            self.conn.execute(
                "INSERT INTO contact_dates (contact_id, kind, on_date, recurring) "
                "VALUES (?, ?, ?, 1)", (cid, kind, on_date))
        self.conn.commit()

        db.merge_contacts(self.conn, keep, dup)
        self.conn.commit()

        rows = sorted((r["kind"], r["on_date"]) for r in self.conn.execute(
            "SELECT kind, on_date FROM contact_dates WHERE contact_id = ?", (keep,)))
        self.assertEqual(rows, [("anniversary", "2020-06-01"), ("birthday", "1906-12-09")],
                         "the birthday must survive, and the shared date must not double up")

    def test_merge_moves_archived_messages_for_every_caller(self):
        """The archive re-point used to live in the HTTP handler, so merges from
        the CLI or any other caller orphaned history under a deleted id. It belongs in
        merge_contacts, which is what every caller goes through."""
        archive_path = os.path.join(self.dir, "archive.db")
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", archive_path):
            arc.init_archive(archive_path)
            keep = db.create_contact(self.conn, "Keep Me")
            dup = db.create_contact(self.conn, "Fold Me")
            ac = arc.get_archive_db(archive_path)
            arc.insert_message(ac, contact_id=dup, channel="imessage", direction=0,
                               ts="2026-01-02 10:00:00", subject="", body="from the dup",
                               source_key="merge-test-1")
            ac.commit()
            ac.close()

            db.merge_contacts(self.conn, keep, dup)   # not through handle_merge
            self.conn.commit()

            ac = arc.get_archive_db(archive_path)
            try:
                self.assertEqual(ac.execute(
                    "SELECT COUNT(*) FROM messages WHERE contact_id = ?", (keep,)).fetchone()[0], 1)
                self.assertEqual(ac.execute(
                    "SELECT COUNT(*) FROM messages WHERE contact_id = ?", (dup,)).fetchone()[0], 0,
                    "nothing may be left pointing at the deleted contact")
            finally:
                ac.close()

    def test_merge_survives_a_missing_archive(self):
        """A merge already applied to crm.db must not fail because the separate
        archive database is absent or unreadable."""
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", os.path.join(self.dir, "nope.db")):
            keep = db.create_contact(self.conn, "A")
            dup = db.create_contact(self.conn, "B")
            db.add_note(self.conn, dup, "note that must survive")
            self.conn.commit()
            db.merge_contacts(self.conn, keep, dup)   # must not raise
            self.conn.commit()
        self.assertEqual(len(db.get_notes(self.conn, keep)), 1)
        self.assertIsNone(db.get_contact(self.conn, dup))

    def test_manual_field_provenance_no_rowwide_lock(self):
        """Hand-editing a location records per-field provenance and does NOT
        stamp facts_source='manual' (which used to lock the whole contact)."""
        cid = db.create_contact(self.conn, "Prov")
        self.conn.execute("UPDATE contacts SET facts_source='linkedin_scrape' WHERE id=?", (cid,))
        self.conn.commit()
        db.set_field(self.conn, cid, "home_city", "Berlin")
        self.conn.commit()
        row = db.get_contact(self.conn, cid)
        self.assertIn("home_city", db.manual_field_set(row))
        self.assertNotEqual(row["facts_source"], "manual")

    def test_enrich_respects_manual_field_but_fills_others(self):
        """Enrichment skips the one field you hand-edited, updates the rest."""
        import enrich_facts
        cid = db.create_contact(self.conn, "Enrich")
        db.set_field(self.conn, cid, "home_metro", "Paris")   # hand-edited
        self.conn.commit()
        enrich_facts._apply_changes(
            self.conn, cid, {"home_metro": "Lyon", "role_type": "investor"}, "offline")
        self.conn.commit()
        row = db.get_contact(self.conn, cid)
        self.assertEqual(row["home_metro"], "Paris")      # protected
        self.assertEqual(row["role_type"], "investor")    # still filled

    def test_p03_honest_signals(self):
        # handle_status reports AI availability
        self.assertIn("ai", app.handle_status(self.conn))
        # the 'deleted' filter must work THROUGH the handler's validation, not
        # silently fall back to 'all'
        gone = db.create_contact(self.conn, "Ghost")
        db.set_deleted(self.conn, gone, 1)
        self.conn.commit()
        out = app.handle_list_contacts(self.conn, {"filter": ["deleted"]})
        self.assertEqual({c["id"] for c in out["contacts"]}, {gone})
        # _ask_reason no longer claims "Based in X" unless the metro matched
        self.conn.execute("UPDATE contacts SET home_city='Paris' WHERE id=?", (gone,))
        self.conn.commit()
        row = db.get_contact(self.conn, gone)
        self.assertNotIn("Based in", app._ask_reason(row, "London", []))
        self.assertIn("Based in Paris", app._ask_reason(row, "Paris", []))

    def test_li_mutuals_surfaced_in_detail(self):
        # li_extra is in the contact detail (it was once written and read nowhere)
        cid = db.create_contact(self.conn, "Networked Nina")
        self.conn.execute(
            "UPDATE contacts SET li_extra=? WHERE id=?",
            ('{"mutual_names":["Ada","Alan"],"shared_history":["You both studied at Example University"],'
             '"recent_activity":"posts about robotics"}', cid))
        self.conn.commit()
        d = app.handle_get_contact(self.conn, cid)
        self.assertEqual(d["mutuals"]["mutual_names"], ["Ada", "Alan"])
        self.assertIn("Example University", d["mutuals"]["shared_history"][0])
        self.assertEqual(d["mutuals"]["recent_activity"], "posts about robotics")

    def test_exclude_list_file(self):
        db.create_contact(self.conn, "Jane Doe")
        db.create_contact(self.conn, "Keep Person")
        self.conn.commit()
        p = os.path.join(self.dir, "ex.txt")
        with open(p, "w") as fh:
            fh.write("# comment\nJane Doe\n")
        n = db.apply_exclude_list(self.conn, db.load_exclude_list(p))
        self.conn.commit()
        self.assertEqual(n, 1)
        self.assertEqual(db.find_exact(self.conn, "Jane Doe")[0]["excluded"], 1)

    def test_add_to_exclude_file_is_idempotent(self):
        p = os.path.join(self.dir, "ex.txt")
        db.add_to_exclude_file(p, "Eva Example")
        db.add_to_exclude_file(p, "eva  example")   # same person, different spacing/case
        names = db.load_exclude_list(p)
        self.assertIn(db.normalize_name("Eva Example"), names)
        with open(p) as fh:
            self.assertEqual(sum(1 for ln in fh if ln.strip()), 1)  # only one line

    def test_remove_from_exclude_file_keeps_comments_and_others(self):
        p = os.path.join(self.dir, "ex.txt")
        with open(p, "w") as fh:
            fh.write("# hidden people\nEva Example\nKeep Person\n")
        db.remove_from_exclude_file(p, "eva example")
        names = db.load_exclude_list(p)
        self.assertNotIn(db.normalize_name("Eva Example"), names)
        self.assertIn(db.normalize_name("Keep Person"), names)
        with open(p) as fh:
            self.assertIn("# hidden people", fh.read())  # comment preserved

    def test_exclude_list_add_endpoint_hides_and_lists(self):
        # Patch the OWNER module. EXCLUDE_FILE lives in crm/api_contacts.py and
        # the handlers read their own module global, so rebinding app.EXCLUDE_FILE
        # (only a re-export) would not reach them — the handler would write to the
        # configured exclude list instead of this temp file. The
        # db.load_exclude_list assertion below is the guard that makes that loud.
        p = os.path.join(self.dir, "ex.txt")
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", p):
            cid = db.create_contact(self.conn, "Spam Bot")
            self.conn.commit()

            # blank name is rejected
            with self.assertRaises(app.ApiError):
                app.handle_exclude_list_add(self.conn, {"name": "  "})

            out = app.handle_exclude_list_add(self.conn, {"name": "Spam Bot"})
            # the contact is hidden right away, without a re-import
            self.assertEqual(db.find_exact(self.conn, "Spam Bot")[0]["excluded"], 1)
            # the returned list reflects the new entry (and that it's a known contact)
            names = {e["name"]: e for e in out["entries"]}
            self.assertIn("Spam Bot", names)
            self.assertTrue(names["Spam Bot"]["in_db"])

            # idempotent: adding again doesn't duplicate the line
            app.handle_exclude_list_add(self.conn, {"name": "spam  bot"})
            self.assertEqual(db.load_exclude_list(p), {db.normalize_name("Spam Bot")})

            # remove takes it back off the file
            app.handle_exclude_list_remove({"name": "Spam Bot"})
            self.assertEqual(app.handle_exclude_list_get(self.conn)["entries"], [])


class TestChannelHelpers(Base):
    """reset_channel / set_channel / channel_watermark underpin idempotent re-import."""

    def test_set_overwrites_and_reset_zeroes(self):
        c = db.create_contact(self.conn, "Ada Lovelace")
        db.set_channel(self.conn, c, "email", 5, "2026-05-01 00:00:00")
        db.set_channel(self.conn, c, "email", 8, "2026-06-01 00:00:00")  # overwrite, not +13
        self.conn.commit()
        row = db.get_contact(self.conn, c)
        self.assertEqual(row["em_count"], 8)
        self.assertEqual(row["em_last"], "2026-06-01 00:00:00")
        db.reset_channel(self.conn, "email")
        self.conn.commit()
        row = db.get_contact(self.conn, c)
        self.assertEqual(row["em_count"], 0)
        self.assertEqual(row["em_last"], "")

    def test_reset_only_touches_its_own_channel(self):
        c = db.create_contact(self.conn, "Grace Hopper")
        db.set_channel(self.conn, c, "email", 4, "2026-01-01 00:00:00")
        db.set_channel(self.conn, c, "imessage", 9, "2026-02-01 00:00:00")
        db.add_note(self.conn, c, "keep me")
        self.conn.commit()
        db.reset_channel(self.conn, "email")
        self.conn.commit()
        row = db.get_contact(self.conn, c)
        self.assertEqual(row["em_count"], 0)        # email cleared
        self.assertEqual(row["imsg_count"], 9)      # iMessage untouched
        self.assertEqual(len(db.get_notes(self.conn, c)), 1)  # notes never touched

    def test_watermark_is_latest_last_seen(self):
        a = db.create_contact(self.conn, "A")
        b = db.create_contact(self.conn, "B")
        db.set_channel(self.conn, a, "email", 1, "2025-03-01 00:00:00")
        db.set_channel(self.conn, b, "email", 1, "2025-09-15 00:00:00")
        self.conn.commit()
        self.assertEqual(db.channel_watermark(self.conn, "email"), "2025-09-15 00:00:00")
        self.assertEqual(db.channel_watermark(self.conn, "telegram"), "")  # none yet

    def test_unknown_channel_raises(self):
        with self.assertRaises(ValueError):
            db.reset_channel(self.conn, "carrier_pigeon")
        with self.assertRaises(ValueError):
            db.set_channel(self.conn, 1, "carrier_pigeon", 1, "")


class TestNewChannels(Base):
    """Wiring proof for the four new channels, using email as the representative.

    bump_channel is the one call importers use; the rest mirror how imsg/wa flow
    through last-seen, the profile card, reconnect signals, merges and the CSV.
    """

    EMAIL_LAST = "2026-05-01 00:00:00"

    def _email_contact(self, name="Em Channel"):
        cid = db.create_contact(self.conn, name)
        db.bump_channel(self.conn, cid, "email", 5, self.EMAIL_LAST)
        self.conn.commit()
        return cid

    def test_bump_channel_sets_count_and_last(self):
        cid = self._email_contact()
        row = db.get_contact(self.conn, cid)
        self.assertEqual(row["em_count"], 5)
        self.assertEqual(row["em_last"], self.EMAIL_LAST)

    def test_bump_channel_rejects_unknown(self):
        cid = db.create_contact(self.conn, "Bad Channel")
        with self.assertRaises(ValueError):
            db.bump_channel(self.conn, cid, "carrier_pigeon", 1, self.EMAIL_LAST)

    def test_reconnect_counts_email_only_signal(self):
        # status='new', no notes, only em_count>0 -> still a relationship signal.
        cid = db.create_contact(self.conn, "Email Only Ed")
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        db.bump_channel(self.conn, cid, "email", 3, old)
        self.conn.commit()
        ids = {r["id"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertIn(cid, ids)

    def test_merge_sums_email_and_keeps_latest(self):
        a = db.create_contact(self.conn, "Merge Maeve")
        b = db.create_contact(self.conn, "Merge Maeve")
        db.bump_channel(self.conn, a, "email", 4, "2026-03-01 00:00:00")
        db.bump_channel(self.conn, b, "email", 6, "2026-06-01 00:00:00")
        self.conn.commit()
        db.merge_contacts(self.conn, a, b)
        self.conn.commit()
        m = db.get_contact(self.conn, a)
        self.assertEqual(m["em_count"], 10)
        self.assertEqual(m["em_last"], "2026-06-01 00:00:00")

    def test_app_last_seen_reflects_email(self):
        cid = self._email_contact()
        row = db.get_contact(self.conn, cid)
        self.assertEqual(app._last_seen(row), self.EMAIL_LAST)

    def test_csv_includes_email_column(self):
        cid = self._email_contact()
        out = ex.export_csv(self.conn)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertIn("email", rows[0])
        col = rows[0].index("email")
        data = [r for r in rows[1:] if r[0] == "Em Channel"][0]
        self.assertEqual(data[col], "5")


class MigrationTests(unittest.TestCase):
    """ensure_columns() has to work on a database that already holds rows, not
    just on the fresh ones every other test builds. SQLite rejects
    `ADD COLUMN ... DEFAULT (datetime('now'))` on a non-empty table, so an
    updated_at column added that way used to break app startup on any existing
    database while the whole suite stayed green."""

    def _old_schema_db(self, path):
        """A database as it looked before updated_at/deleted existed — with rows."""
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
                updated_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL,
                body TEXT NOT NULL, kind TEXT DEFAULT 'note',
                created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE list_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT, list_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL, status TEXT DEFAULT '',
                added_at TEXT DEFAULT (datetime('now')));
        """)
        conn.execute("INSERT INTO contacts (name) VALUES ('Ada')")
        conn.execute("INSERT INTO notes (contact_id, body, created_at) "
                     "VALUES (1, 'met at a talk', '2020-01-02 03:04:05')")
        conn.execute("INSERT INTO lists (name, created_at) VALUES ('dinner', '2020-01-02 03:04:05')")
        conn.execute("INSERT INTO list_members (list_id, contact_id, added_at) "
                     "VALUES (1, 1, '2020-01-02 03:04:05')")
        conn.commit()
        conn.row_factory = sqlite3.Row
        return conn

    def test_ensure_columns_migrates_a_populated_database(self):
        with tempfile.TemporaryDirectory() as d:
            conn = self._old_schema_db(os.path.join(d, "old.db"))
            db.ensure_columns(conn)          # used to raise OperationalError
            for table in ("notes", "lists", "list_members"):
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                self.assertIn("updated_at", cols, table)
                self.assertEqual(
                    0, conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE updated_at IS NULL").fetchone()[0],
                    f"{table}.updated_at left NULL")
            # seeded from the row's own creation stamp, not stamped "now"
            self.assertEqual("2020-01-02 03:04:05",
                             conn.execute("SELECT updated_at FROM notes").fetchone()[0])
            db.ensure_columns(conn)          # idempotent
            conn.close()

    def test_new_rows_get_a_real_timestamp_after_migration(self):
        """The migrated column has no DEFAULT, so the insert sites must supply one."""
        with tempfile.TemporaryDirectory() as d:
            conn = self._old_schema_db(os.path.join(d, "old.db"))
            db.ensure_columns(conn)
            db.add_note(conn, 1, "a second note")
            lid = db.create_list(conn, "drinks")
            db.add_list_members(conn, lid, [1])
            for table in ("notes", "lists", "list_members"):
                blank = conn.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    "WHERE updated_at IS NULL OR updated_at = ''").fetchone()[0]
                self.assertEqual(0, blank, f"{table} row written without updated_at")
            conn.close()


class TestMigrationOnAnOldSchema(Base):
    """ensure_columns() must work on a database that PREDATES the columns it adds.

    Every other test builds a fresh database via db.init_db() — where CREATE TABLE
    makes every current column and the migration has nothing to do — so an index
    in SCHEMA naming contact_dates.source_uid would pass all of them. init_db runs
    executescript(SCHEMA) BEFORE ensure_columns(), so on an existing database the
    CREATE TABLE is a no-op, the column does not exist yet, and CREATE INDEX fails
    the whole script with "no such column: source_uid" — the app could not start.
    Fresh installs would be fine; every existing one would be stuck.

    This builds the OLD shape explicitly and migrates it, which is the only way to
    exercise that path.
    """

    def _old_db(self):
        """A CURRENT database with contact_dates put back to its pre-provenance
        shape. Hand-stubbing the whole schema does not work — SCHEMA carries other
        tables and indexes that reference columns a stub would not have — and this is
        a more faithful simulation anyway: the only thing an older install differs by
        IS those two columns.
        """
        import tempfile, os as _os
        path = _os.path.join(tempfile.mkdtemp(prefix="crm_old_"), "crm.db")
        db.init_db(path)
        conn = db.get_db(path)
        conn.executescript("""
            DROP TABLE contact_dates;
            CREATE TABLE contact_dates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'custom',
                label TEXT DEFAULT '',
                on_date TEXT NOT NULL,
                year_known INTEGER NOT NULL DEFAULT 1,
                recurring INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(contact_id, kind, on_date, label));
        """)
        cid = db.create_contact(conn, "Old Friend")
        conn.execute("INSERT INTO contact_dates (contact_id, kind, on_date, label) "
                     "VALUES (?, 'meeting', '2026-01-01', 'Lunch')", (cid,))
        conn.commit()
        return conn

    def test_init_db_survives_a_pre_provenance_database(self):
        conn = self._old_db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(contact_dates)")}
        self.assertNotIn("source_uid", cols)      # the old shape
        # The crash was here: executescript(SCHEMA) then ensure_columns().
        conn.executescript(db.SCHEMA)
        db.ensure_columns(conn)
        conn.commit()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(contact_dates)")}
        self.assertIn("source", cols)
        self.assertIn("source_uid", cols)

    def test_the_existing_row_is_backfilled_as_manual_not_as_an_import(self):
        """Backfilling the other way would relabel dates the owner typed as machine
        guesses — the exact confusion the column exists to prevent."""
        conn = self._old_db()
        conn.executescript(db.SCHEMA)
        db.ensure_columns(conn)
        row = conn.execute("SELECT source, source_uid FROM contact_dates").fetchone()
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["source_uid"], "")

    def test_the_uid_index_exists_on_both_a_migrated_and_a_fresh_database(self):
        """It cannot live in SCHEMA (the column may not exist yet) and it cannot live
        inside the ALTER guard (which never fires on a fresh database). Both paths."""
        migrated = self._old_db()
        migrated.executescript(db.SCHEMA)
        db.ensure_columns(migrated)
        names = {r["name"] for r in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_cdates_uid", names, "missing after a migration")
        fresh = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_cdates_uid", fresh, "missing on a fresh database")

    def test_ensure_columns_is_idempotent(self):
        """Invariant 9: it runs on every init_db, so twice must equal once."""
        conn = self._old_db()
        conn.executescript(db.SCHEMA)
        db.ensure_columns(conn)
        db.ensure_columns(conn)          # must not raise
        db.ensure_columns(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) n FROM contact_dates").fetchone()["n"], 1)
