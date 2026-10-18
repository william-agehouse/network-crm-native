"""NC-115. The three places that break "who is hidden or deleted" down by kind.

Each of these was a hand-written `excluded = 1` or `deleted = 1` until NC-115,
and none had a test with the one person who makes them differ: someone who is
BOTH hidden and deleted. `db.set_deleted` sets both columns, so that is everyone
you deleted through the app.

* the people ledger on the Settings page, whose parts must add up to the file;
* the People page's Excluded tab, which lists hidden people and leaves the
  deleted ones to their own tab;
* the style profile, which must skip someone deleted even when they are not
  hidden (another writer can set the two columns separately).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

import api_instance  # noqa: E402
import style_profile  # noqa: E402


class _FourKinds(Base):
    """One of each: usable, hidden only, deleted and hidden, deleted only."""

    def setUp(self):
        super().setUp()
        self.plain = db.create_contact(self.conn, "Plain Person")
        self.hidden = db.create_contact(self.conn, "Hidden Only")
        self.both = db.create_contact(self.conn, "Hidden And Deleted")
        self.gone = db.create_contact(self.conn, "Deleted Only")
        self.conn.execute("UPDATE contacts SET excluded = 1 WHERE id IN (?, ?)",
                          (self.hidden, self.both))
        self.conn.execute("UPDATE contacts SET deleted = 1 WHERE id IN (?, ?)",
                          (self.both, self.gone))
        self.conn.commit()


class TestThePeopleLedgerAddsUp(_FourKinds):

    def test_each_person_is_counted_once(self):
        led = api_instance._people_ledger(self.conn, 4)
        parts = led["parts"]
        self.assertEqual(parts, {"active": 1, "hidden": 1, "deleted": 2},
                         "the person who is hidden AND deleted belongs to "
                         "'deleted' only; counted under 'hidden' too, the parts "
                         "come to 5 for a file of 4")
        self.assertEqual(led["unexplained"], 0)


class TestTheExcludedTabLeavesTheDeletedToTheirOwnTab(_FourKinds):

    def ids(self, flt):
        return {r["id"] for r in db.list_contacts(self.conn, flt=flt, limit=50)}

    def test_excluded_is_hidden_and_not_deleted(self):
        self.assertEqual(self.ids("excluded"), {self.hidden})
        self.assertEqual(db.count_contacts(self.conn, flt="excluded"), 1)

    def test_deleted_is_everyone_deleted_hidden_or_not(self):
        self.assertEqual(self.ids("deleted"), {self.both, self.gone})


class TestTheStyleProfileSkipsTheDeletedToo(_FourKinds):

    def test_hidden_or_deleted_are_all_skipped(self):
        skip, ok = style_profile._excluded_contacts(self.conn)
        self.assertTrue(ok)
        self.assertEqual(skip, {self.hidden, self.both, self.gone},
                         "someone deleted but not hidden was left in, so their "
                         "messages would shape the style profile")


if __name__ == "__main__":
    unittest.main()
