"""NC-172 gate: a permanent delete has to leave something behind saying so.

Purge takes the row away for good. A row is also the only thing an importer
looks at, so the moment it is gone the person is a stranger again — and the next
import creates them from scratch. Of the three things that can happen after
"cannot be undone", coming back is the worst one.

What stops that is a line in `exclude_list.txt`, the file the ten importers
read. Delete writes it (NC-146), and the UI only ever offers purge from the
Deleted view, so in the app the line is always already there. But the precondition
lived only in the UI, and the UI is not the API: `handle_purge_contact` called
`_require_contact` and nothing else. A purge of someone who was never deleted
went straight through and left nothing behind.

Two things hold it now, in this order. The purge is refused unless they are
already deleted — the state the UI guarantees anyway, and the one the docstring
has always claimed. Then the name goes on the skip list *before* the row is
destroyed, because delete's own write to that file can have failed (NC-150) and
this is the last moment anyone can notice.

The last test here is the one that matters: a real .vcf, through the real
importer, after a real purge.
"""
import contextlib
import os
import sqlite3
import sys
import unittest
from http import HTTPStatus
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, arc, db, js_code_of  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import contacts_import  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")

VCARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:{name}\r\n"
         "TEL;TYPE=CELL:+46701740605\r\nEND:VCARD\r\n")


class PurgeBase(Base):
    def setUp(self):
        super().setUp()
        self.file = os.path.join(self.dir, "exclude_list.txt")

    def purge(self, cid):
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.file):
            return app.handle_purge_contact(self.conn, cid, {})

    def delete(self, cid, deleted=True):
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.file):
            return app.handle_delete_contact(self.conn, cid, {"deleted": deleted})


class TestOnlySomeoneAlreadyDeletedCanBePurged(PurgeBase):

    def test_purging_a_contact_who_was_never_deleted_is_refused(self):
        cid = db.create_contact(self.conn, "Still Here")
        self.conn.commit()

        with self.assertRaises(app.ApiError) as caught:
            self.purge(cid)

        self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Delete this contact first", str(caught.exception),
                      "the refusal has to say what to do instead — this is "
                      "reachable from a script, and a bare 400 teaches nothing")

    def test_the_refusal_leaves_them_exactly_as_they_were(self):
        cid = db.create_contact(self.conn, "Still Here")
        self.conn.commit()
        with self.assertRaises(app.ApiError):
            self.purge(cid)

        row = db.get_contact(self.conn, cid)
        self.assertIsNotNone(row, "the row was destroyed by a call that failed")
        self.assertEqual(row["name"], "Still Here")
        self.assertFalse(row["deleted"])
        self.assertEqual(db.load_exclude_list(self.file), set(),
                         "a refused purge must not quietly hide them either")

    def test_deleting_first_then_purging_works(self):
        cid = db.create_contact(self.conn, "Goodbye")
        self.conn.commit()
        self.delete(cid)

        out = self.purge(cid)

        self.assertTrue(out["purged"])
        self.assertIsNone(db.get_contact(self.conn, cid))

    def test_restoring_makes_them_unpurgeable_again(self):
        """`deleted` is the permission, so taking it back takes the permission."""
        cid = db.create_contact(self.conn, "Second Thoughts")
        self.conn.commit()
        self.delete(cid)
        self.delete(cid, deleted=False)

        with self.assertRaises(app.ApiError):
            self.purge(cid)
        self.assertIsNotNone(db.get_contact(self.conn, cid))


class TestThePurgedNameStaysOnTheSkipList(PurgeBase):

    def test_the_line_is_there_after_a_purge(self):
        cid = db.create_contact(self.conn, "Erase Me")
        self.conn.commit()
        self.delete(cid)
        self.purge(cid)

        self.assertIn(db.normalize_name("Erase Me"), db.load_exclude_list(self.file))

    def test_the_line_is_written_even_when_the_delete_failed_to_write_it(self):
        """The case that makes writing on purge worth doing at all.

        Delete already writes this line, so on a good day purge finds it there.
        On a bad day — an unwritable data folder, which NC-150 exists because of
        — delete came back with a warning and no line, and purge was the last
        moment anyone could still put one down.
        """
        cid = db.create_contact(self.conn, "Missed By Delete")
        self.conn.commit()
        with mock.patch.object(db, "add_to_exclude_file", return_value=False):
            out = self.delete(cid)
        self.assertFalse(out["exclude_file_saved"], "setup: the write should fail")
        self.assertEqual(db.load_exclude_list(self.file), set())

        self.purge(cid)

        self.assertIn(db.normalize_name("Missed By Delete"),
                      db.load_exclude_list(self.file))

    def test_a_failed_write_is_reported_rather_than_ticked(self):
        cid = db.create_contact(self.conn, "Unwritable")
        self.conn.commit()
        self.delete(cid)
        with mock.patch.object(db, "add_to_exclude_file", return_value=False):
            out = self.purge(cid)

        self.assertFalse(out["exclude_file_saved"])
        self.assertIn("warning", out,
                      "the row is gone and nothing is holding the importers off; "
                      "reporting success here is the NC-150 mistake with the "
                      "worst possible consequence")
        self.assertTrue(out["purged"],
                        "and it still has to be reported as done, because it is")

    def test_the_next_import_skips_them(self):
        """The whole point, end to end: a real .vcf through the real importer."""
        cid = db.create_contact(self.conn, "Reimport Me")
        self.conn.commit()
        self.delete(cid)
        self.purge(cid)
        self.assertIsNone(db.get_contact(self.conn, cid), "setup: the row is gone")

        vcf = os.path.join(self.dir, "cards.vcf")
        with open(vcf, "w", encoding="utf-8") as fh:
            fh.write(VCARD.format(name="Reimport Me"))
        cards = contacts_import.parse_vcards(vcf)
        self.assertEqual(len(cards), 1, "setup: the card should parse")

        # apply() opens its own connection through db.get_db(), so DB_PATH is
        # what decides which database this import lands in. Patch the owner.
        with mock.patch.object(contacts_import, "EXCLUDE_FILE", self.file), \
                mock.patch.object(db, "DB_PATH", self.path):
            stats = contacts_import.apply(cards, enrich_only=False, dry_run=False)

        self.assertEqual(stats["skipped_excluded"], 1)
        self.assertEqual(stats["created"], 0,
                         "the person just permanently deleted was created again "
                         "by the next import")

    def test_the_same_import_creates_someone_who_was_not_purged(self):
        """The control. Otherwise the test above passes with a broken importer."""
        vcf = os.path.join(self.dir, "cards.vcf")
        with open(vcf, "w", encoding="utf-8") as fh:
            fh.write(VCARD.format(name="Perfectly Fine"))
        cards = contacts_import.parse_vcards(vcf)

        # apply() opens its own connection through db.get_db(), so DB_PATH is
        # what decides which database this import lands in. Patch the owner.
        with mock.patch.object(contacts_import, "EXCLUDE_FILE", self.file), \
                mock.patch.object(db, "DB_PATH", self.path):
            stats = contacts_import.apply(cards, enrich_only=False, dry_run=False)

        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["skipped_excluded"], 0)


class TestTheirMessagesGoFirstOrNothingGoes(PurgeBase):
    """NC-087. Purge removed the person, committed, and then tried the message
    archive inside a bare `except: pass`. A busy archive left their messages
    behind under an id nobody has, and the answer said 0 removed."""

    def setUp(self):
        super().setUp()
        self.arc_path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.arc_path)
        self.cid = db.create_contact(self.conn, "Gone Soon")
        self.conn.commit()
        self.delete(self.cid)
        a = arc.get_archive_db(self.arc_path)
        for i in range(3):
            arc.insert_message(a, contact_id=self.cid, channel="whatsapp", direction=0,
                               ts=f"2026-05-0{i + 1} 10:00:00", body="hello", source_key=f"m{i}")
        a.commit()
        a.close()

    def _messages(self):
        a = sqlite3.connect(self.arc_path)
        try:
            return a.execute("SELECT COUNT(*) FROM messages WHERE contact_id = ?",
                             (self.cid,)).fetchone()[0]
        finally:
            a.close()

    def _purge(self, connect=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(arc, "ARCHIVE_DB_PATH", self.arc_path))
            if connect:
                stack.enter_context(mock.patch.object(arc, "get_archive_db", connect))
            return self.purge(self.cid)

    def test_a_purge_takes_their_messages_with_them(self):
        out = self._purge()
        self.assertEqual(out["messages_removed"], 3)
        self.assertEqual(self._messages(), 0)
        self.assertIsNone(db.get_contact(self.conn, self.cid))

    def test_a_busy_archive_deletes_nothing_and_says_so(self):
        holder = sqlite3.connect(self.arc_path)
        holder.execute("BEGIN IMMEDIATE")      # another writer holds the archive
        self.addCleanup(holder.close)

        def impatient(path=None):
            c = sqlite3.connect(path or self.arc_path, timeout=0.1)
            c.row_factory = sqlite3.Row
            return c

        with self.assertRaises(app.ApiError) as caught, \
                self.assertLogs("networkcrm", "ERROR") as logged:
            self._purge(connect=impatient)
        holder.rollback()
        self.assertIn("database is locked", "\n".join(logged.output),
                      "the reason goes to the log, not to the screen")

        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("nothing was deleted", str(caught.exception))
        row = db.get_contact(self.conn, self.cid)
        self.assertIsNotNone(row, "the person went while their messages stayed")
        self.assertEqual(row["deleted"], 1, "they stay in Deleted, to try again")
        self.assertEqual(self._messages(), 3)
        # And the next try, with the archive free, goes through.
        self.assertEqual(self._purge()["messages_removed"], 3)


class TestTheAppSaysSoWhenTheLineCouldNotBeWritten(unittest.TestCase):
    def test_the_purge_toast_prefers_the_warning(self):
        src = js_code_of(os.path.join(APP, "people.js"))
        at = src.index("/api/contact/${c.id}/purge")
        after = src[at:at + 600]
        self.assertIn("d.warning ||", after,
                      "the purge toast reports a clean permanent delete even "
                      "when the skip list could not be written, which is the "
                      "one time the person needs to know")


if __name__ == "__main__":
    unittest.main()
