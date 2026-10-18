"""Phone identifier normalisation: one canonical row per number per contact.

The bug this file pins down: a contact's detail pane showed the same Swedish
mobile twice, '+46701740611' and '701740611'. Matching had always normalised to
the last nine digits (invariant 1), but the *stored* rows did not — the vCard
importer wrote the matching key itself, while iMessage and WhatsApp wrote the
full international form, so repeated imports from different sources accumulated
one row per spelling of one number.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, ci, db,
)


class TestCanonicalPhone(unittest.TestCase):
    """The write-side rule, independent of the database."""

    def test_spellings_of_one_number_canonicalise_together(self):
        for raw in ("+46701740611", "+46 70 174 06 11", "0046701740611",
                    "0046 70-174 06 11", "+46-70-174-0611"):
            self.assertEqual(db.canonical_phone(raw), "+46701740611", raw)

    def test_national_forms_keep_their_own_shape(self):
        # No country code was given, so none is invented — guessing one from the
        # owner's locale would mangle every number belonging to someone abroad.
        self.assertEqual(db.canonical_phone("070-174 06 11"), "0701740611")
        self.assertEqual(db.canonical_phone("701740611"), "701740611")

    def test_short_00_prefix_is_not_an_access_code(self):
        # '00' only reads as the international access code when what follows is
        # long enough to be a real number; a short extension keeps its zeros.
        self.assertEqual(db.canonical_phone("0012"), "0012")
        self.assertEqual(db.canonical_phone("0012025550100"), "+12025550100")

    def test_key_is_invariant_under_canonicalisation(self):
        # What makes the collapse safe: canonicalising never moves the last nine
        # digits, so a row cannot change which group it belongs to.
        for raw in ("+46 70 174 06 11", "0046701740611", "070-174 06 11",
                    "701740611", "+1 202 555 0100", "12345"):
            self.assertEqual(db.phone_key(db.canonical_phone(raw)), db.phone_key(raw), raw)

    def test_key_matches_the_importers_matching_convention(self):
        # db.phone_key and the importers' normalize_phone must not drift: the
        # whole point is that two spellings which match the same contact also
        # collapse to the same stored row.
        for raw in ("+46701740611", "0046701740611", "0701740611", "12345"):
            self.assertEqual(db.phone_key(raw), ci.normalize_phone(raw), raw)

    def test_non_numeric_value_survives_untouched(self):
        self.assertEqual(db.canonical_phone("no digits here"), "no digits here")
        self.assertEqual(db.canonical_phone("  "), "")


class TestPhoneDedupeOnWrite(Base):
    """add_identifier() collapses on the way in, so duplicates never accumulate."""

    def _phones(self, cid):
        return [r["value"] for r in self.conn.execute(
            "SELECT value FROM identifiers WHERE contact_id = ? AND kind = 'phone' "
            "ORDER BY id", (cid,))]

    def test_three_spellings_one_number_one_row(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        db.add_identifier(self.conn, cid, "phone", "+46701740611", source="message_scan")
        db.add_identifier(self.conn, cid, "phone", "0046701740611", source="message_scan")
        db.add_identifier(self.conn, cid, "phone", "701740611", source="contacts")
        self.conn.commit()
        # The international form is the informative one, so it is what survives.
        self.assertEqual(self._phones(cid), ["+46701740611"])

    def test_different_numbers_sharing_a_short_suffix_stay_apart(self):
        # Two genuinely different mobiles whose last four digits agree. The key
        # is nine digits precisely so this does not collapse.
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.add_identifier(self.conn, cid, "phone", "+46701740605", source="message_scan")
        db.add_identifier(self.conn, cid, "phone", "+46701740667", source="message_scan")
        self.conn.commit()
        self.assertEqual(sorted(self._phones(cid)), ["+46701740605", "+46701740667"])

    def test_short_numbers_sharing_a_suffix_stay_apart(self):
        # Under nine digits a number keys on itself, so a suffix match between
        # two short numbers is not treated as identity.
        cid = db.create_contact(self.conn, "Grace Hopper")
        db.add_identifier(self.conn, cid, "phone", "12345", source="contacts")
        db.add_identifier(self.conn, cid, "phone", "9912345", source="contacts")
        self.conn.commit()
        self.assertEqual(sorted(self._phones(cid)), ["12345", "9912345"])

    def test_same_number_on_two_contacts_is_two_rows(self):
        # The collapse is per-contact. Two people sharing a landline keep a row
        # each; find_duplicate_groups is what flags them, not this.
        a = db.create_contact(self.conn, "Ada Lovelace")
        b = db.create_contact(self.conn, "Bob Smith")
        db.add_identifier(self.conn, a, "phone", "+46701740611", source="message_scan")
        db.add_identifier(self.conn, b, "phone", "0701740611", source="contacts")
        self.conn.commit()
        self.assertEqual(self._phones(a), ["+46701740611"])
        self.assertEqual(self._phones(b), ["0701740611"])

    def test_emails_are_untouched_by_the_phone_rule(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.add_identifier(self.conn, cid, "email", "ada@example.com", source="message_scan")
        db.add_identifier(self.conn, cid, "email", "ada.l@example.com", source="message_scan")
        self.conn.commit()
        idents = {(i["kind"], i["value"]) for i in db.get_identifiers(self.conn, cid)}
        self.assertEqual(idents, {("email", "ada@example.com"), ("email", "ada.l@example.com")})

    def test_delete_lands_whichever_spelling_is_sent(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        db.add_identifier(self.conn, cid, "phone", "+46701740611", source="manual")
        self.conn.commit()
        db.delete_identifier(self.conn, cid, "phone", "070-174 06 11")
        self.conn.commit()
        self.assertEqual(self._phones(cid), [])


class TestSourcePrecedence(Base):
    """Invariant 4 decides which spelling survives — never the arrival order."""

    def _phone(self, cid):
        row = self.conn.execute(
            "SELECT value, source FROM identifiers WHERE contact_id = ? AND kind = 'phone'",
            (cid,)).fetchall()
        self.assertEqual(len(row), 1, f"expected exactly one phone row, got {len(row)}")
        return row[0]["value"], row[0]["source"]

    def test_manual_survives_a_later_import(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        db.add_identifier(self.conn, cid, "phone", "0701740611", source="manual")
        db.add_identifier(self.conn, cid, "phone", "+46701740611", source="message_scan")
        self.conn.commit()
        # message_scan's spelling is richer, but it does not outrank a hand-typed
        # number: a lower-precedence source must never win.
        self.assertEqual(self._phone(cid), ("0701740611", "manual"))

    def test_manual_still_wins_when_it_arrives_second(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        db.add_identifier(self.conn, cid, "phone", "+46701740611", source="message_scan")
        db.add_identifier(self.conn, cid, "phone", "0701740611", source="manual")
        self.conn.commit()
        self.assertEqual(self._phone(cid), ("0701740611", "manual"))

    def test_within_a_tier_the_richer_spelling_wins(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        db.add_identifier(self.conn, cid, "phone", "701740611", source="contacts")
        db.add_identifier(self.conn, cid, "phone", "+46701740611", source="message_scan")
        self.conn.commit()
        self.assertEqual(self._phone(cid)[0], "+46701740611")

    def test_legacy_sourceless_row_loses_to_a_real_source(self):
        # Rows written before the source column existed sit at the floor, so a
        # real write can improve them — but they never outrank one.
        cid = db.create_contact(self.conn, "Anna Svensson")
        self.conn.execute(
            "INSERT INTO identifiers (contact_id, kind, value) VALUES (?, 'phone', '701740611')",
            (cid,))
        db.add_identifier(self.conn, cid, "phone", "0701740611", source="manual")
        self.conn.commit()
        self.assertEqual(self._phone(cid), ("0701740611", "manual"))

    def test_source_is_upgraded_never_downgraded(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.add_identifier(self.conn, cid, "email", "ada@example.com", source="message_scan")
        db.add_identifier(self.conn, cid, "email", "ada@example.com", source="manual")
        db.add_identifier(self.conn, cid, "email", "ada@example.com", source="offline")
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT source FROM identifiers WHERE contact_id = ? AND kind = 'email'",
            (cid,)).fetchall()
        self.assertEqual([r["source"] for r in rows], ["manual"])


class TestCleanupExistingData(Base):
    """The migration half: rows already in the database heal themselves."""

    def _rows(self, cid):
        return [(r["value"], r["source"]) for r in self.conn.execute(
            "SELECT value, source FROM identifiers WHERE contact_id = ? AND kind = 'phone' "
            "ORDER BY id", (cid,))]

    def _seed_legacy(self, cid, *pairs):
        """Write rows the way the old code did — straight in, no normalisation."""
        for value, source in pairs:
            self.conn.execute(
                "INSERT INTO identifiers (contact_id, kind, value, source) VALUES (?, 'phone', ?, ?)",
                (cid, value, source))
        self.conn.commit()

    def test_collapses_one_number_stored_in_two_spellings(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        self._seed_legacy(cid, ("+46701740611", "message_scan"), ("701740611", "contacts"))
        self.assertEqual(len(self._rows(cid)), 2)         # two spellings, one number
        removed = db.dedupe_phone_identifiers(self.conn)
        self.conn.commit()
        self.assertEqual(removed, 1)
        self.assertEqual(self._rows(cid), [("+46701740611", "message_scan")])

    def test_cleanup_respects_source_precedence(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        self._seed_legacy(cid, ("+46701740611", "message_scan"), ("0701740611", "manual"))
        db.dedupe_phone_identifiers(self.conn)
        self.conn.commit()
        self.assertEqual(self._rows(cid), [("0701740611", "manual")])

    def test_cleanup_is_idempotent(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        self._seed_legacy(cid, ("+46 70 174 06 11", "message_scan"),
                          ("0046701740611", "contacts"), ("701740611", "offline"))
        self.assertEqual(db.dedupe_phone_identifiers(self.conn), 2)
        self.conn.commit()
        first = self._rows(cid)
        # Second and third runs must be pure no-ops, not just non-crashing.
        self.assertEqual(db.dedupe_phone_identifiers(self.conn), 0)
        self.assertEqual(db.dedupe_phone_identifiers(self.conn), 0)
        self.conn.commit()
        self.assertEqual(self._rows(cid), first)
        self.assertEqual(first, [("+46701740611", "message_scan")])

    def test_cleanup_canonicalises_a_lone_row(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        self._seed_legacy(cid, ("+46 70 174 06 05", "contacts"))
        db.dedupe_phone_identifiers(self.conn)
        self.conn.commit()
        self.assertEqual(self._rows(cid), [("+46701740605", "contacts")])

    def test_cleanup_leaves_distinct_numbers_alone(self):
        cid = db.create_contact(self.conn, "Grace Hopper")
        self._seed_legacy(cid, ("+46701740605", "contacts"), ("+46701740667", "contacts"),
                          ("12345", "contacts"), ("9912345", "contacts"))
        self.assertEqual(db.dedupe_phone_identifiers(self.conn), 0)
        self.conn.commit()
        self.assertEqual(len(self._rows(cid)), 4)

    def test_runs_from_ensure_columns_on_every_init_db(self):
        # Invariant 9: no manual migration step. Opening the database is the
        # whole upgrade path, so a pre-existing install heals on next launch.
        cid = db.create_contact(self.conn, "Anna Svensson")
        self._seed_legacy(cid, ("+46701740611", "message_scan"), ("701740611", "contacts"))
        self.conn.close()
        db.init_db(self.path)
        self.conn = db.get_db(self.path)
        self.assertEqual(self._rows(cid), [("+46701740611", "message_scan")])

    def test_migration_adds_the_source_column_to_an_old_table(self):
        # A database created before the column existed must gain it in place,
        # keeping its rows.
        self.conn.execute("DROP TABLE identifiers")
        self.conn.execute(
            "CREATE TABLE identifiers (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE, "
            "kind TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(contact_id, kind, value))")
        cid = db.create_contact(self.conn, "Anna Svensson")
        for v in ("+46701740611", "701740611"):
            self.conn.execute(
                "INSERT INTO identifiers (contact_id, kind, value) VALUES (?, 'phone', ?)",
                (cid, v))
        self.conn.commit()
        self.conn.close()

        db.init_db(self.path)
        self.conn = db.get_db(self.path)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(identifiers)")}
        self.assertIn("source", cols)
        self.assertEqual(self._rows(cid), [("+46701740611", "")])


class TestRepeatedImports(Base):
    """Invariant 3: re-running an import must never double rows."""

    def _phones(self, conn, cid):
        return [r["value"] for r in conn.execute(
            "SELECT value FROM identifiers WHERE contact_id = ? AND kind = 'phone'", (cid,))]

    def test_vcard_import_twice_writes_one_row(self):
        import tempfile
        vcf = os.path.join(tempfile.mkdtemp(prefix="vcf_"), "Contacts.vcf")
        with open(vcf, "w", encoding="utf-8") as fh:
            fh.write("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna Svensson\r\n"
                     "TEL;type=CELL:+46 70 174 06 11\r\nEND:VCARD\r\n")

        orig_db_path, orig_exclude = db.DB_PATH, ci.EXCLUDE_FILE
        db.DB_PATH = self.path
        ci.EXCLUDE_FILE = os.path.join(self.dir, "none.txt")
        try:
            cards = ci.parse_vcards(vcf)
            ci.apply(cards, enrich_only=False, dry_run=False)
            conn = db.get_db(self.path)
            cid = db.find_exact(conn, "Anna Svensson")[0]["id"]
            self.assertEqual(self._phones(conn, cid), ["+46701740611"])
            conn.close()

            # Same export, second run — the identical guarantee archive.db gives
            # for messages has to hold for identifiers too.
            ci.apply(ci.parse_vcards(vcf), enrich_only=False, dry_run=False)
            conn = db.get_db(self.path)
            self.assertEqual(self._phones(conn, cid), ["+46701740611"])
            self.assertEqual(len(db.find_exact(conn, "Anna Svensson")), 1)
            conn.close()
        finally:
            db.DB_PATH, ci.EXCLUDE_FILE = orig_db_path, orig_exclude

    def test_two_sources_of_one_number_converge_on_one_row(self):
        # The usual way this goes wrong: a vCard and a message import
        # describing one number, each in its own spelling.
        cid = db.create_contact(self.conn, "Anna Svensson")
        for _ in range(2):                       # each importer run twice
            db.add_identifier(self.conn, cid, "phone", "701740611", source="contacts")
            db.add_identifier(self.conn, cid, "phone", "+46701740611", source="message_scan")
        self.conn.commit()
        self.assertEqual(self._phones(self.conn, cid), ["+46701740611"])

    def test_manual_add_through_the_api_is_idempotent(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        self.conn.commit()
        for value in ("+46701740611", "0046 70 174 06 11", "+46-70-174-0611"):
            out = app.handle_add_identifier(self.conn, cid, {"kind": "phone", "value": value})
        self.assertEqual([i for i in out["identifiers"] if i["kind"] == "phone"],
                         [{"kind": "phone", "value": "+46701740611"}])


class TestMergeCollapses(Base):
    """Folding a duplicate contact is another way one person gets two spellings."""

    def test_merge_does_not_carry_two_spellings_across(self):
        keep = db.create_contact(self.conn, "Anna Svensson")
        dup = db.create_contact(self.conn, "Anna Svensson ⟢")
        db.add_identifier(self.conn, keep, "phone", "701740611", source="contacts")
        db.add_identifier(self.conn, dup, "phone", "+46701740611", source="message_scan")
        self.conn.commit()
        db.merge_contacts(self.conn, keep, dup)
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT value FROM identifiers WHERE contact_id = ? AND kind = 'phone'",
            (keep,)).fetchall()
        self.assertEqual([r["value"] for r in rows], ["+46701740611"])

    def test_merge_keeps_the_better_source(self):
        keep = db.create_contact(self.conn, "Anna Svensson")
        dup = db.create_contact(self.conn, "Anna Svensson ⟢")
        db.add_identifier(self.conn, keep, "phone", "+46701740611", source="message_scan")
        db.add_identifier(self.conn, dup, "phone", "0701740611", source="manual")
        self.conn.commit()
        db.merge_contacts(self.conn, keep, dup)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT value, source FROM identifiers WHERE contact_id = ? AND kind = 'phone'",
            (keep,)).fetchone()
        self.assertEqual((row["value"], row["source"]), ("0701740611", "manual"))


if __name__ == "__main__":
    unittest.main()
