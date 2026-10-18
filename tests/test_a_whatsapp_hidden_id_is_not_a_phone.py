"""NC-243. WhatsApp's hidden id is not a phone number.

For a chat addressed as "<digits>@lid" with a saved name, the import took the
digits as the person's phone number and stored "+<digits>" on them. A person
could carry a number that does not exist, and some had a US city filled in from
its first three digits. That fake number was
also, by accident, what kept a re-import on the same person after a merge or a
rename, so the fix has to keep that and only drop the number.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db, wa  # noqa: E402

HIDDEN = "100000000000001"
ADA = {"jid": HIDDEN + "@lid", "name": "Ada Lovelace", "count": 3,
       "first": "2026-05-01", "last": "2026-05-02"}


class _Import(Base):

    def _apply(self, *sessions):
        with mock.patch.object(db, "DB_PATH", self.path):
            stats, jid_to_cid = wa.apply(list(sessions), {}, include_unknown=False,
                                         dry_run=False)
        return stats, jid_to_cid

    def _phones(self, cid):
        return [r["value"] for r in self.conn.execute(
            "SELECT value FROM identifiers WHERE contact_id = ? AND kind = 'phone'",
            (cid,))]

    def _hidden(self, cid):
        return [r["value"] for r in self.conn.execute(
            "SELECT value FROM identifiers WHERE contact_id = ? AND kind = 'whatsapp_id'",
            (cid,))]


class TestAHiddenIdIsNeverStoredAsAPhone(_Import):

    def test_a_named_hidden_chat_adds_no_phone(self):
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        _, jid_to_cid = self._apply(ADA)
        self.assertEqual(jid_to_cid, {ADA["jid"]: ada})
        self.assertEqual(self._phones(ada), [], "a hidden id became a phone number")
        self.assertEqual(self._hidden(ada), [ADA["jid"]])

    def test_the_number_in_a_formatted_name_is_still_kept(self):
        # A hidden chat whose name is a formatted number: that number is real.
        s = dict(ADA, name="+46 70 174 06 11")
        with mock.patch.object(wa, "resolve_name", return_value="Bo Byphone"):
            _, jid_to_cid = self._apply(s)
        cid = jid_to_cid[s["jid"]]
        self.assertEqual(self._phones(cid), ["+46701740611"])

    def test_a_classic_chat_still_stores_its_number(self):
        anna = db.create_contact(self.conn, "Anna")
        self.conn.commit()
        self._apply({"jid": "46701740611@s.whatsapp.net", "name": "Anna", "count": 1,
                     "first": "2026-05-01", "last": "2026-05-01"})
        self.assertEqual(self._phones(anna), ["+46701740611"])

    def test_no_page_shows_the_hidden_id(self):
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        self._apply(ADA)
        self.assertEqual([tuple(r) for r in db.get_identifiers(self.conn, ada)], [])


class TestAReImportStaysOnTheSamePerson(_Import):

    def test_after_a_rename_the_next_import_finds_the_same_person(self):
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        self._apply(ADA)
        # Renamed in the CRM, so the saved WhatsApp name no longer matches.
        db.set_field(self.conn, ada, "name", "Augusta Ada King")
        self.conn.commit()
        stats, jid_to_cid = self._apply(ADA)
        self.assertEqual(jid_to_cid[ADA["jid"]], ada)
        self.assertEqual(stats["created"], 0, "a rename made a duplicate person")

    def test_an_old_fake_phone_is_turned_into_the_hidden_id(self):
        # What an older import left behind, on a person since renamed.
        ada = db.create_contact(self.conn, "Augusta Ada King")
        db.add_identifier(self.conn, ada, "phone", "+" + HIDDEN, source="message_scan")
        self.conn.commit()
        stats, jid_to_cid = self._apply(ADA)
        self.assertEqual(jid_to_cid[ADA["jid"]], ada, "lost the person the old import found")
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["fake_phones_removed"], 1)
        self.assertEqual(self._phones(ada), [])
        self.assertEqual(self._hidden(ada), [ADA["jid"]])

    def test_a_number_typed_by_hand_is_never_removed(self):
        ada = db.create_contact(self.conn, "Ada Lovelace")
        db.add_identifier(self.conn, ada, "phone", "+" + HIDDEN, source="manual")
        self.conn.commit()
        stats, _ = self._apply(ADA)
        self.assertEqual(stats["fake_phones_removed"], 0)
        self.assertEqual(self._phones(ada), ["+" + HIDDEN])

    def test_a_stranger_whose_number_ends_the_same_is_not_matched(self):
        # find_by_handle compares the last nine digits. A hidden id must never
        # go through it, or a random id lands on someone's real number.
        bo = db.create_contact(self.conn, "Bo")
        db.add_identifier(self.conn, bo, "phone", "+46" + HIDDEN[-9:], source="contacts")
        self.conn.commit()
        _, jid_to_cid = self._apply(dict(ADA, name="Someone Else"))
        self.assertNotEqual(jid_to_cid.get(ADA["jid"]), bo)


class TestAGroupMemberResolvesThroughTheHiddenId(_Import):

    def test_a_hidden_member_matches_the_person_a_chat_tied_it_to(self):
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        self._apply(ADA)
        with mock.patch.object(db, "DB_PATH", self.path):
            out = wa.match_group_senders([ADA["jid"]], {})
        self.assertEqual(out, {ADA["jid"]: ada})

    def test_an_unknown_hidden_member_stays_unmatched(self):
        bo = db.create_contact(self.conn, "Bo")
        db.add_identifier(self.conn, bo, "phone", "+46" + HIDDEN[-9:], source="contacts")
        self.conn.commit()
        with mock.patch.object(db, "DB_PATH", self.path):
            out = wa.match_group_senders([HIDDEN + "@lid"], {})
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()


class TestACityReadFromAHiddenIdIsForgotten(Base):
    """NC-250. The offline pass read a US area code from the fake number, so
    some people had a city that came from nowhere. NC-243
    stopped new ones; this clears the ones already written, and only those."""

    NYC_ID = "121200000000001@lid"     # "+1212..." reads as New York

    def setUp(self):
        super().setUp()
        import enrich_facts
        self.ef = enrich_facts

    def _person(self, name, *, metro="New York", city="New York", country="US",
                source="offline", manual="", phone=None):
        cid = db.create_contact(self.conn, name)
        self.conn.execute(
            "UPDATE contacts SET home_metro = ?, home_city = ?, home_country = ?, "
            "facts_source = ?, manual_fields = ? WHERE id = ?",
            (metro, city, country, source, manual, cid))
        db.add_identifier(self.conn, cid, "whatsapp_id", self.NYC_ID, source="message_scan")
        if phone:
            db.add_identifier(self.conn, cid, "phone", phone, source="contacts")
        self.conn.commit()
        return cid

    def _place(self, cid):
        r = db.get_contact(self.conn, cid)
        return r["home_metro"], r["home_city"], r["home_country"]

    def test_a_city_that_came_from_the_hidden_id_is_cleared(self):
        cid = self._person("Ada")
        self.assertEqual(self.ef.forget_cities_from_hidden_ids(self.conn), 1)
        self.assertEqual(self._place(cid), ("", "", ""))

    def test_a_hidden_id_that_does_not_start_with_1_is_recognised_too(self):
        # "+212..." reads as New York only under the old reading, which read any
        # ten digits as a US number. Most hidden ids look like this one.
        cid = db.create_contact(self.conn, "Bea")
        self.conn.execute(
            "UPDATE contacts SET home_metro = 'New York', home_city = 'New York', "
            "home_country = 'US', facts_source = 'offline' WHERE id = ?", (cid,))
        db.add_identifier(self.conn, cid, "whatsapp_id", "212000000000001@lid",
                          source="message_scan")
        self.conn.commit()
        self.assertEqual(self.ef.forget_cities_from_hidden_ids(self.conn), 1)
        self.assertEqual(self._place(cid), ("", "", ""))

    def test_everything_else_is_left_alone(self):
        kept = {
            "typed in": self._person("Typed", manual='["home_city"]'),
            "from the AI pass": self._person("Llm", source="llm"),
            "a real New York number": self._person("Real", phone="+12125550100"),
            "a different city": self._person("Elsewhere", metro="London", city="London",
                                             country="United Kingdom"),
        }
        self.assertEqual(self.ef.forget_cities_from_hidden_ids(self.conn), 0)
        for why, cid in kept.items():
            with self.subTest(why=why):
                self.assertNotEqual(self._place(cid)[0], "")

    def test_a_country_from_elsewhere_stays(self):
        cid = self._person("Ada", country="Sweden")
        self.ef.forget_cities_from_hidden_ids(self.conn)
        self.assertEqual(self._place(cid), ("", "", "Sweden"))

    def test_it_runs_once_and_says_so(self):
        self._person("Ada")
        self.assertEqual(self.ef.forget_cities_from_hidden_ids(self.conn), 1)
        self.assertEqual(self.ef.forget_cities_from_hidden_ids(self.conn), 0)
        receipt = db.state_get(self.conn, self.ef.CITY_FROM_HIDDEN_ID_KEY, "")
        self.assertIn('"cleared": 1', receipt)

    def test_it_runs_when_the_app_starts(self):
        cid = self._person("Ada")
        self.conn.close()
        db.init_db(self.path)
        self.conn = db.get_db(self.path)
        self.assertEqual(self._place(cid), ("", "", ""))

    def test_the_offline_pass_never_reads_a_hidden_id(self):
        cid = db.create_contact(self.conn, "Only Hidden")
        db.add_identifier(self.conn, cid, "whatsapp_id", self.NYC_ID, source="message_scan")
        self.conn.commit()
        self.assertNotIn("home_metro", self.ef.enrich_pass_a(self.conn, cid))
