"""What each source brought — crm/api_provenance.py (NC-037).

WHAT THESE PROTECT
The map. SOURCE_TOKENS is the only thing in this codebase that joins
`messages.channel='linkedin'` to `contacts.source='linkedin'` to
`identifiers.source='linkedin_csv'` to `facts_source='linkedin_csv'`, and the
four are written by four different functions in three different files. Nothing
in the schema connects them and nothing ever will, so a rename on either side of
that join produces a confident zero rather than an error — which is the exact
failure NC-037 exists to stop.

So the gates come in two layers, and the second one is the real one:

  1. Map hygiene (TestTheMapIsTheOnlyJoin). Every source key is a real registry
     key; every token has a named writer file; every token is a string that file
     actually contains, matched as a whole constant or as a quoted SQL literal
     rather than by loose word search — "FROM contacts" must not be allowed to
     vouch for `source="contacts"`.
  2. The real writers (TestRealWritersLandUnderTheRightSource). The importers'
     own functions do the writing, and the panel is asked where it landed. That
     is what catches a renamed token: nothing in the test names the token, so
     when db.create_contact's default or contacts_import.IDENT_SOURCE changes,
     the count drops to zero and the assertion fails.

And the honesty gates (TestWhatItCannotKnow), which matter as much as the
counts. A panel that reports 0 where it means "no idea" is worse than one that
reports nothing, so each of the four gaps NC-037 names gets a test: a photo
that lost the precedence race, a token three importers share, a column that only
records who created a row, and an archive.db that is not there.

Nothing here reads the machine's real data. tests/helpers.py redirects CRM_DATA_DIR and the
protected paths; every DB below is a temp file, and archive.db is built by hand.
"""
import ast
import os
import re
import sys
import tempfile
import unittest
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import app, arc, ci, db, importers  # noqa: E402
# AFTER tests.helpers, never before: crm modules capture data/ paths at import
# time, so importing one first makes the whole suite read and write the real
# data dir while still passing (tests/helpers.py; tests/test_bootstrap.py).
import api_provenance as prov  # noqa: E402
import figures  # noqa: E402
import photos as ph  # noqa: E402
from api_core import ApiError  # noqa: E402

CRM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm")
JPEG = b"\xff\xd8\xff\xe0" + b"J" * 300      # valid magic, junk body (> MIN_BYTES)
JPEG2 = b"\xff\xd8\xff\xe1" + b"K" * 300


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _callee(node):
    """The bare name of what a Call calls: `photos.save_photo(…)` -> save_photo."""
    fn = node.func
    return getattr(fn, "attr", None) or getattr(fn, "id", None) or ""


def _str(node):
    return (node.value if isinstance(node, ast.Constant)
            and isinstance(node.value, str) else None)


def _writes_token(path, token, anchor):
    """Does this file write `token`, in the exact shape `anchor` claims?

    ast is used rather than grep for the reason this repo keeps relearning: a
    source-grepping gate matches the comment or the docstring that EXPLAINS a
    token, and passes while the code that writes it has moved. ast carries no
    comments at all, and the four shapes below are all code positions.

    The anchor is not decoration. Checking only "does the string appear in this
    file" would pass after renaming `channel="imessage"` to `"imessages"` in
    crm/imessage_import.py, because `source="imessage"` is in the same file. The
    anchor is what makes that rename fail.
    """
    tree = _tree(path)
    if anchor == "sql":
        # A quoted literal inside a longer SQL string: VALUES (…, 'linkedin').
        # Deliberately not a bare substring — "SELECT * FROM contacts" must not
        # be allowed to vouch for the VALUE 'contacts'.
        needle = f"'{token}'"
        return any(needle in (_str(n) or "")
                   for n in ast.walk(tree) if isinstance(n, ast.Constant))
    if anchor.startswith("kw:"):
        want = anchor[3:]
        return any(kw.arg == want and _str(kw.value) == token
                   for n in ast.walk(tree) if isinstance(n, ast.Call)
                   for kw in n.keywords)
    if anchor.startswith("arg:"):
        want = anchor[4:]
        return any(_str(a) == token
                   for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and _callee(n) == want for a in n.args)
    if anchor.startswith("const:"):
        want = anchor[6:]
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign):
                continue
            if not any(getattr(t, "id", None) == want for t in n.targets):
                continue
            if _str(n.value) == token:
                return True
            # NC-095: the constant may now be an ALIAS of another crm module's
            # constant — `FACTS_SOURCE = db.LI_FACTS_SOURCE`, so one literal has
            # two readers on two machines. Follow it one hop and check the
            # literal there.
            #
            # Following rather than accepting the alias on sight is the whole
            # point: the guard exists so that renaming the token fails the suite.
            # If `db.LI_FACTS_SOURCE` became "linkedin_scraped", an alias this
            # function merely tolerated would keep passing.
            if (isinstance(n.value, ast.Attribute)
                    and isinstance(n.value.value, ast.Name)):
                target = os.path.join(os.path.dirname(path),
                                      n.value.value.id + ".py")
                if os.path.exists(target) and _writes_token(
                        target, token, "const:" + n.value.attr):
                    return True
        return False
    raise AssertionError(f"unknown anchor kind: {anchor}")


class ProvBase(unittest.TestCase):
    """A temp crm.db, a temp archive.db, a temp photo dir. Nothing shared."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_prov_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        self._orig_db = db.DB_PATH
        db.DB_PATH = self.dbpath
        self.conn = db.get_db(self.dbpath)

        self.arcpath = os.path.join(self.dir, "archive.db")
        self._orig_arc = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = self.arcpath

        self._orig_photos = ph.PHOTOS_DIR
        ph.PHOTOS_DIR = os.path.join(self.dir, "photos")
        self._orig_ci_exclude = ci.EXCLUDE_FILE
        ci.EXCLUDE_FILE = os.path.join(self.dir, "exclude_list.txt")

    def tearDown(self):
        self.conn.close()
        db.DB_PATH = self._orig_db
        arc.ARCHIVE_DB_PATH = self._orig_arc
        ph.PHOTOS_DIR = self._orig_photos
        ci.EXCLUDE_FILE = self._orig_ci_exclude

    # ── fixture helpers ──────────────────────────────────────────────────────
    def open_archive(self):
        arc.init_archive(self.arcpath)
        return arc.get_archive_db(self.arcpath)

    def archive(self, contact_id, channel, n=1, key="k"):
        ac = self.open_archive()
        try:
            for i in range(n):
                arc.insert_message(ac, contact_id=contact_id, channel=channel,
                                   direction=i % 2, ts=f"2026-01-{i + 1:02d} 10:00:00",
                                   body="x", source_key=f"{key}-{channel}-{contact_id}-{i}")
            ac.commit()
        finally:
            ac.close()

    def vcards(self, vcf_text):
        path = os.path.join(self.dir, "Contacts.vcf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(vcf_text)
        return ci.parse_vcards(path)

    def panel(self, source):
        out = prov.handle_provenance(self.conn, {"source": [source]})
        self.assertEqual(len(out["panels"]), 1)
        return out["panels"][0]

    def kind(self, source, kind):
        for k in self.panel(source)["kinds"]:
            if k["kind"] == kind:
                return k
        self.fail(f"{source} has no '{kind}' line: "
                  f"{[k['kind'] for k in self.panel(source)['kinds']]}")


class TestTheMapIsTheOnlyJoin(unittest.TestCase):
    """SOURCE_TOKENS / TOKEN_WRITERS hygiene. No database involved."""

    def test_every_source_key_is_a_real_registry_key(self):
        """A panel for a source the app does not have is a dead endpoint, and it
        is how the map drifts: a key stays behind after its row is retired."""
        for key in prov.SOURCE_TOKENS:
            self.assertIn(key, importers.REGISTRY,
                          f"SOURCE_TOKENS has '{key}', which importers.REGISTRY does not")

    def test_every_field_named_is_one_of_the_six_columns(self):
        for key, entry in prov.SOURCE_TOKENS.items():
            for field in entry:
                if field == "part_of":
                    continue
                self.assertIn(field, prov.FIELDS, f"{key} names field '{field}'")

    def test_every_part_of_points_at_a_source_with_a_panel(self):
        for key, entry in prov.SOURCE_TOKENS.items():
            parent = entry.get("part_of")
            if not parent:
                continue
            self.assertIn(parent, prov.SOURCE_TOKENS, f"{key}.part_of is unknown")
            self.assertNotIn("part_of", prov.SOURCE_TOKENS[parent],
                             f"{key}.part_of chains through another sub-source")

    def test_every_token_used_has_a_named_writer(self):
        for key in prov.SOURCE_TOKENS:
            for field in prov.FIELDS:
                for token in prov.tokens_for(key, field):
                    self.assertIn((field, token), prov.TOKEN_WRITERS,
                                  f"{key}.{field} uses '{token}' with no TOKEN_WRITERS entry")

    def test_no_writer_entry_is_dead(self):
        """A writer for a token no source claims is either a forgotten source or
        a token that moved. Either way the map is out of date."""
        used = {(f, t) for k in prov.SOURCE_TOKENS for f in prov.FIELDS
                for t in prov.tokens_for(k, f)}
        for pair in prov.TOKEN_WRITERS:
            self.assertIn(pair, used, f"TOKEN_WRITERS has {pair}, which no source uses")

    def test_every_writer_file_exists(self):
        for (field, token), (fname, _anchor) in prov.TOKEN_WRITERS.items():
            self.assertTrue(os.path.exists(os.path.join(CRM_DIR, fname)),
                            f"{field}/{token} names crm/{fname}, which is not there")

    def test_every_token_is_written_where_the_map_says_it_is(self):
        """The gate on a renamed token. Not "does this string appear in the
        file" — that is too weak to catch anything, and passes straight
        through a real rename. It asserts the token appears in the exact syntax
        position the map claims: a `channel=` keyword, a positional argument to
        save_photo, a named constant, or a quoted SQL literal."""
        for (field, token), (fname, anchor) in prov.TOKEN_WRITERS.items():
            self.assertTrue(
                _writes_token(os.path.join(CRM_DIR, fname), token, anchor),
                f"crm/{fname} has no `{anchor}` writing '{token}' "
                f"(the claimed writer for {field})")

    def test_a_shared_token_is_reported_as_shared_by_every_sharer(self):
        """message_scan is written by three importers, so all three have to agree
        they cannot claim it alone. Computed, not declared — that is why."""
        sharers = prov._sharers("identifiers", "message_scan")
        self.assertGreater(len(sharers), 1)
        self.assertEqual(set(sharers), {"imessage", "whatsapp", "email"})


class TestRealWritersLandUnderTheRightSource(ProvBase):
    """The importers' own functions write; the panel is asked where it landed.

    Nothing in these tests names a provenance token, on purpose. That is what
    makes them the gate: rename the token in the importer and the count these
    assert goes to zero."""

    def test_the_linkedin_writer_fills_linkedin_people_and_identifiers(self):
        db.upsert_contact_from_linkedin(
            self.conn, name="Ada Lovelace", position="Analyst", company="AE",
            connected_on="2020-01-01", linkedin_url="https://x/ada",
            email="ada@example.com")
        self.conn.commit()
        self.assertEqual(self.kind("linkedin", "people")["count"], 1)
        self.assertEqual(self.kind("linkedin", "identifiers")["count"], 1)
        # And it must not leak into a source that did not write it.
        self.assertEqual(self.kind("contacts", "people")["count"], 0)

    def test_the_address_book_writer_fills_apple_contacts_four_ways(self):
        """One vCard, four columns: the record, its phone and email, its
        birthday and its photo. All four are Apple Contacts' tokens and all four
        come from contacts_import, which addressbook_import also runs through."""
        import base64
        b64 = base64.b64encode(JPEG).decode()
        cards = self.vcards(
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Grace Hopper\r\n"
            "TEL;TYPE=CELL:+1 202 555 0100\r\nEMAIL:grace@navy.example\r\n"
            "BDAY:1906-12-09\r\n"
            f"PHOTO;ENCODING=b;TYPE=JPEG:{b64}\r\n"
            "END:VCARD\r\n")
        ci.apply(cards, enrich_only=False, dry_run=False)
        self.assertEqual(self.kind("contacts", "people")["count"], 1)
        self.assertEqual(self.kind("contacts", "identifiers")["count"], 2)
        self.assertEqual(self.kind("contacts", "dates")["count"], 1)
        self.assertEqual(self.kind("contacts", "photos")["count"], 1)
        self.assertIn("birthday", self.kind("contacts", "dates")["detail"])

    def test_the_calendar_writer_fills_all_three_calendar_rows(self):
        """ics_import writes one token for three UI rows, so the three report the
        same number and say they cannot be told apart."""
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.upsert_calendar_date(self.conn, cid, "2026-09-01",
                                source_uid="uid-1", label="Coffee")
        self.conn.commit()
        for key in ("calendar", "calendar_google", "calendar_icloud"):
            line = self.kind(key, "dates")
            self.assertEqual(line["count"], 1, key)
            self.assertEqual(line["certainty"], "shared", key)

    def test_message_channels_land_under_their_own_source(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        self.archive(cid, "imessage", n=5)
        self.archive(cid, "linkedin", n=3)
        self.assertEqual(self.kind("imessage", "messages")["count"], 5)
        self.assertEqual(self.kind("linkedin", "messages")["count"], 3)
        self.assertEqual(self.kind("whatsapp", "messages")["count"], 0)
        self.assertEqual(self.kind("imessage", "messages")["of_total"], 8)
        self.assertEqual(self.kind("imessage", "messages")["detail"], "1 person")

    def test_a_token_in_identifiers_but_not_in_messages(self):
        """The vCard case: Apple Contacts writes identifiers and never a message,
        so its panel must show a real identifier count and state in words that it
        brings no messages at all — not a zero on a messages line."""
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.add_identifier(self.conn, cid, "email", "ada@example.com", source="contacts")
        self.conn.commit()
        panel = self.panel("contacts")
        self.assertEqual(self.kind("contacts", "identifiers")["count"], 1)
        self.assertNotIn("messages", [k["kind"] for k in panel["kinds"]])
        self.assertIn("messages", panel["not_recorded"])
        self.assertIn("does not bring", panel["not_recorded_note"])
        self.assertIn("messages", panel["not_recorded_note"])

    def test_a_sub_source_answers_under_its_parent(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        ph.save_photo(self.conn, cid, JPEG, "whatsapp")
        self.conn.commit()
        panel = self.panel("whatsapp_photos")
        self.assertEqual(panel["source"], "whatsapp")
        self.assertEqual(panel["resolved_from"], "whatsapp_photos")
        self.assertEqual(self.kind("whatsapp", "photos")["count"], 1)


class TestEveryLineSaysWhatItCounts(ProvBase):
    """NC-087. A line's count and its "of" travelled as bare integers under one
    field name meaning six things: rows of messages, of phone numbers, of dates,
    and people. Each line now carries a figure naming its unit and its group."""

    def test_each_line_names_its_unit_and_group(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.add_identifier(self.conn, cid, "email", "ada@example.com", source="linkedin_csv")
        self.conn.execute(
            "INSERT INTO contact_dates (contact_id, kind, label, on_date, source, source_uid) "
            "VALUES (?, 'meeting', 'Lunch', '2026-06-01', 'calendar', 'u1'), "
            "(?, 'meeting', 'Tea', '2026-06-02', 'calendar', 'u2')", (cid, cid))
        hidden = db.create_contact(self.conn, "Hidden Hal")
        self.conn.execute(
            "INSERT INTO contact_dates (contact_id, kind, label, on_date, source, source_uid) "
            "VALUES (?, 'meeting', 'Coffee', '2026-06-03', 'calendar', 'u3')", (hidden,))
        db.set_excluded(self.conn, hidden, 1)
        self.conn.commit()
        self.archive(cid, "whatsapp", n=3)

        dates = self.kind("calendar", "dates")["figure"]
        self.assertEqual((dates["n"], dates["of"], dates["unit"], dates["population"]),
                         (2, 2, "dates", "active"),
                         "two meetings with one person: dates, not people, and "
                         "not the hidden person's")
        msgs = self.kind("whatsapp", "messages")["figure"]
        self.assertEqual((msgs["n"], msgs["unit"], msgs["population"], msgs["side"]),
                         (3, "messages", "all", figures.SIDE_ARCHIVE))
        self.assertEqual(self.kind("linkedin", "identifiers")["figure"]["unit"], "handles")
        out = prov.handle_provenance(self.conn, {})
        self.assertEqual(figures.check_payload(out), [])
        for p in out["panels"]:
            for k in p["kinds"]:
                with self.subTest(source=p["source"], kind=k["kind"]):
                    self.assertEqual((k["figure"]["n"], k["figure"]["of"]),
                                     (k["count"], k["of_total"]))


class TestWhatItCannotKnow(ProvBase):
    """The four honest gaps. Each one has a specific wrong answer it must not
    give, and the wrong answer is always a confident number."""

    def test_a_photo_that_lost_the_precedence_race_is_gone_and_said_to_be(self):
        """Apple Contacts supplied this face; LinkedIn then outranked it
        (invariant 5) and overwrote the one file that exists for this person. So
        Apple Contacts' photo count is 0 — correctly — and the panel has to say
        the loss is not recorded, because the bytes are gone rather than hidden.
        This is the case that made 'exclude' impossible (NC-037)."""
        cid = db.create_contact(self.conn, "Ada Lovelace")
        self.assertTrue(ph.save_photo(self.conn, cid, JPEG, "contacts"))
        self.assertTrue(ph.save_photo(self.conn, cid, JPEG2, "linkedin"))
        self.conn.commit()
        lost = self.kind("contacts", "photos")
        self.assertEqual(lost["count"], 0)
        self.assertEqual(lost["certainty"], "current_only")
        self.assertIn("not recorded anywhere", lost["note"])
        # The winner is reported, under the source that actually holds it.
        self.assertEqual(self.kind("linkedin_facts", "photos")["count"], 1)

    def test_a_shared_token_names_its_sharers_and_refuses_to_split(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        self.archive(cid, "email", n=4)
        for key in ("email", "gmail"):
            line = self.kind(key, "messages")
            self.assertEqual(line["count"], 4, key)
            self.assertEqual(line["certainty"], "shared", key)
            self.assertIn("cannot split", line["note"])
            self.assertIn("Gmail", line["note"])
            self.assertIn("Email (.mbox)", line["note"])

    def test_the_people_line_says_it_only_knows_who_created_the_record(self):
        """contacts.source is written once and never updated on enrichment, so
        the line must not claim to describe what the source knows."""
        db.create_contact(self.conn, "Ada Lovelace", source="linkedin")
        self.conn.commit()
        line = self.kind("linkedin", "people")
        self.assertEqual(line["certainty"], "created_only")
        self.assertIn("first arrived", line["note"])
        self.assertIn("never rewrites", line["note"])

    def test_a_missing_archive_reports_not_known_and_never_zero(self):
        """No archive.db at all. 'I could not look' and 'there is nothing' are
        different answers, and a 0 here would be the panel inventing the second
        one (the same mistake as NC-033 on this page)."""
        self.assertFalse(os.path.exists(self.arcpath))
        line = self.kind("imessage", "messages")
        self.assertIsNone(line["count"])
        self.assertIsNone(line["of_total"])
        self.assertEqual(line["certainty"], "unavailable")
        self.assertIn("no message archive", line["note"])
        self.assertFalse(self.panel("imessage")["archive_present"])

    def test_a_source_with_zero_contributions_reports_a_real_zero(self):
        """Different from the case above and it must read differently: the column
        was read, and the answer is genuinely none yet."""
        self.archive(db.create_contact(self.conn, "Ada Lovelace"), "imessage", n=1)
        line = self.kind("whatsapp", "messages")
        self.assertEqual(line["count"], 0)
        self.assertEqual(line["certainty"], "exact")
        self.assertEqual(line["note"], "")

    def test_a_source_that_records_nothing_says_so_in_one_sentence(self):
        """Recompute closeness and Measure how you write leave no provenance
        anywhere — NC-032's category error. Six zeroes would be a lie about the
        shape of the thing; one sentence is the truth."""
        for key in ("engagement", "style"):
            panel = self.panel(key)
            self.assertTrue(panel["records_nothing"], key)
            self.assertEqual(panel["kinds"], [], key)
            self.assertEqual(panel["not_recorded"], [], key)
            self.assertIn("adds no data of its own", panel["note"], key)

    def test_the_dates_line_no_longer_admits_a_merge_loses_its_source(self):
        """The dates line carries no merge warning, and the shared sentence is
        still produced for the calendar rows.

        'calendar' is also a shared token, and a caveat table that let the shared
        sentence REPLACE the merge one would lose the warning on the only three
        rows it applied to; the additive-caveat machinery exists for that.

        The merge warning is gone because the bug is (NC-050) — db.merge_contacts
        now carries `source` and `source_uid` across. What this pins is that the
        shared sentence is still produced correctly for the calendar rows, which
        is what the additive test really guards."""
        cal = self.kind("calendar", "dates")
        self.assertEqual(cal["certainty"], "shared")
        self.assertIn("cannot split", cal["note"])
        self.assertNotIn("merge", cal["note"],
                         "the panel warns about behaviour that no longer exists")
        book = self.kind("contacts", "dates")
        self.assertEqual(book["certainty"], "exact")
        self.assertEqual(book["note"], "",
                         "an exact count with nothing to qualify should say "
                         "nothing, not carry a leftover caveat")


class TestMerge(ProvBase):
    """Folding two records together. NC-037 claims message counts survive it."""

    def test_message_counts_survive_a_merge(self):
        keep = db.create_contact(self.conn, "Ada Lovelace")
        dup = db.create_contact(self.conn, "A. Lovelace")
        self.archive(keep, "imessage", n=3, key="a")
        self.archive(dup, "imessage", n=4, key="b")
        self.conn.commit()
        self.assertEqual(self.kind("imessage", "messages")["count"], 7)
        self.assertEqual(self.kind("imessage", "messages")["detail"], "2 people")
        db.merge_contacts(self.conn, keep, dup)
        self.conn.commit()
        line = self.kind("imessage", "messages")
        self.assertEqual(line["count"], 7, "the merge lost archived messages")
        self.assertEqual(line["detail"], "1 person",
                         "the merge left messages under a contact that is gone")

    def test_a_conversation_owned_message_is_not_counted_as_a_person(self):
        """NC-111. Every group chat now holds messages stored against the
        conversation rather than a person. COUNT(*) still wants them; the people
        figure beside it must not, or every channel with group chats reads one
        person too many — small enough to look like rounding, and wrong in the
        direction that flatters the number."""
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.archive(ada, "imessage", n=3, key="a")
        ac = self.open_archive()
        arc.insert_message(ac, contact_id=arc.NO_PERSON, channel="imessage",
                           direction=1, ts="2026-02-01 10:00:00",
                           body="my group reply", source_key="own-1",
                           group_name="Book club", group_key="imessage:1")
        ac.commit(); ac.close()
        self.conn.commit()
        line = self.kind("imessage", "messages")
        self.assertEqual(line["count"], 4, "the owner's own group message was dropped")
        self.assertEqual(line["detail"], "1 person",
                         "the conversation was counted as a second person")

    def test_identifier_provenance_survives_a_merge(self):
        keep = db.create_contact(self.conn, "Ada Lovelace")
        dup = db.create_contact(self.conn, "A. Lovelace")
        db.add_identifier(self.conn, dup, "email", "ada@example.com", source="contacts")
        self.conn.commit()
        self.assertEqual(self.kind("contacts", "identifiers")["count"], 1)
        db.merge_contacts(self.conn, keep, dup)
        self.conn.commit()
        self.assertEqual(self.kind("contacts", "identifiers")["count"], 1)

    def test_a_merge_carries_date_provenance_across(self):
        """NC-050: db.merge_contacts' INSERT selected
        kind/label/on_date/year_known/recurring and NOT `source`, so a folded
        record's dates landed on the column default. The default is 'manual', so
        the merge did not lose the provenance — it FORGED it, and invariant 4 then
        refused every future correction to those dates, permanently. This pins
        the fix."""
        keep = db.create_contact(self.conn, "Ada Lovelace")
        dup = db.create_contact(self.conn, "A. Lovelace")
        db.upsert_calendar_date(self.conn, dup, "2026-09-01",
                                source_uid="uid-1", label="Coffee")
        self.conn.commit()
        self.assertEqual(self.kind("calendar", "dates")["count"], 1)
        db.merge_contacts(self.conn, keep, dup)
        self.conn.commit()
        self.assertEqual(self.kind("calendar", "dates")["count"], 1,
                         "the merge dropped contact_dates.source again")
        row = self.conn.execute(
            "SELECT source, source_uid FROM contact_dates").fetchone()
        self.assertEqual(row[0], "calendar",
                         "the folded date claims to have been typed by hand")
        self.assertEqual(row[1], "uid-1",
                         "source_uid was dropped, so ics_import can no longer "
                         "recognise this event and a moved meeting duplicates")

    def test_the_dates_note_no_longer_apologises_for_the_merge(self):
        """The caveat was true for as long as the bug was. Leaving it would have
        the panel warn about behaviour that no longer exists, which is the same
        defect as NC-005 in the other direction."""
        note = self.kind("calendar", "dates")["note"]
        self.assertNotIn("folded", note)
        self.assertNotIn("can be low", note)
        self.assertFalse(hasattr(prov, "FIELD_CAVEATS"),
                         "an empty caveat table is a branch nothing exercises")


class TestTheRoute(unittest.TestCase):
    """The wiring. A feature module with no registered route is finished and
    unreachable, as crm/api_intake.py would be without an entry in app.py; the
    registry is the fix and this is the gate on it."""

    def test_the_module_is_registered(self):
        self.assertIn(app.api_provenance, app.ROUTE_MODULES)

    def test_routes_names_a_callable_handler(self):
        self.assertEqual(list(prov.ROUTES), ["GET"])
        name = prov.ROUTES["GET"]["/api/provenance"]
        self.assertTrue(callable(getattr(prov, name, None)))

    def test_the_handler_asks_for_a_conn_so_the_registry_gives_it_one(self):
        """The connection is handed over on the strength of the FIRST parameter's
        name. Any other name and every count silently becomes a 500."""
        import inspect
        params = list(inspect.signature(prov.handle_provenance).parameters)
        self.assertEqual(params[0], "conn")
        self.assertEqual(len(params), 2, "conn + the parsed query, nothing else")

    def test_it_dispatches_through_the_registry(self):
        """Not a unit call: this goes through the same _route_registered the HTTP
        layer uses, with a real crm.db behind it."""
        d = tempfile.mkdtemp(prefix="crm_prov_route_")
        path = os.path.join(d, "crm.db")
        db.init_db(path)
        orig, db.DB_PATH = db.DB_PATH, path

        class _Stub:
            def _with_conn(self, fn):
                c = db.get_db()
                try:
                    return fn(c)
                finally:
                    c.close()
        try:
            out = app.CrmHandler._route_registered(
                _Stub(), "GET", "/api/provenance", {"source": ["linkedin"]})
        finally:
            db.DB_PATH = orig
        self.assertIsNotNone(out, "/api/provenance is not reachable through the registry")
        self.assertEqual(out["panels"][0]["source"], "linkedin")

    def test_an_unknown_source_is_a_404_not_an_empty_panel(self):
        d = tempfile.mkdtemp(prefix="crm_prov_404_")
        path = os.path.join(d, "crm.db")
        db.init_db(path)
        conn = db.get_db(path)
        try:
            with self.assertRaises(ApiError) as caught:
                prov.handle_provenance(conn, {"source": ["not_a_source"]})
            self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)
        finally:
            conn.close()

    def test_no_source_asked_for_answers_for_every_panel_source(self):
        d = tempfile.mkdtemp(prefix="crm_prov_all_")
        path = os.path.join(d, "crm.db")
        db.init_db(path)
        conn = db.get_db(path)
        try:
            panels = prov.handle_provenance(conn, {})["panels"]
        finally:
            conn.close()
        keys = [p["source"] for p in panels]
        self.assertEqual(sorted(keys), sorted(prov._panel_keys()))
        # A sub-source has no panel of its own; it answers under its parent.
        self.assertNotIn("whatsapp_photos", keys)


class TestTheFrontEndUsesIt(unittest.TestCase):
    """A gate of the shape tests/test_frontend_gates.py uses: the endpoint and
    the control have to exist in the view, or this ships as an API nobody calls
    — which is exactly how api_intake once sat finished but unreachable."""

    def _dataview(self):
        # NC-236: the Data page is static/app/data.js now; the report component
        # moved into it from the old dataview.js.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "app", "data.js"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_view_calls_the_endpoint(self):
        self.assertIn("/api/provenance?source=", self._dataview())

    def test_both_source_row_shapes_can_open_the_report(self):
        """NC-236: the old page had three row components (NeedsRow, AutoRow,
        SourceRow), each with its own ReportToggle, and this counted three. The
        new page has one row component for sources, SourceRow, with two shapes:
        an ordinary source and the folded row of passes the app runs by itself.
        Each opens the report through Record, which draws Provenance. Counted on
        the JSX tag, not on a comment. NC-239 added a third: a source's line
        under Needs you offers the same report, as the old page's did."""
        src = self._dataview()
        self.assertEqual(len(re.findall(r"<\$\{Record\}", src)), 3,
                         "one Record per shape of source row, and one on a need")
        self.assertEqual(len(re.findall(r"<\$\{Provenance\}", src)), 1,
                         "Record is the one place that draws Provenance")


if __name__ == "__main__":
    unittest.main()
