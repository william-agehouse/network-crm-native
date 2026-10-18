"""NC-146 gate: a delete the importers cannot see is not a delete.

There are two copies of "people to leave alone" in this app. The `excluded`
column is what the app reads — review, search, dashboard, Ask, the default
list. `exclude_list.txt` is what the importers read, and there are ten of them.
They are one list only while every write to either reaches the other.

Hiding mirrored to the file. Deleting — the stronger of the two statements —
set the column and stopped, though `db.set_deleted` sets `excluded` too. So the
column listed every hidden and every deleted person while the file listed only
the hidden ones, and the missing names were exactly the deleted ones: the
importers went on collecting for the people the app had been told hardest to
forget.

These tests hold the two halves together from both ends: the handler that
writes, the backfill that catches up the deletes made before it existed, the
order that makes the backfill arrive before an importer can run, and the
premise underneath all of it — that the file, not the column, is what the
importers actually consult.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, code_of, db, py_block  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")


class TestDeleteReachesTheImporters(Base):
    def setUp(self):
        super().setUp()
        self.file = os.path.join(self.dir, "exclude_list.txt")

    def test_deleting_a_contact_writes_the_line_the_importers_read(self):
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.file):
            cid = db.create_contact(self.conn, "Delete Me")
            self.conn.commit()
            app.handle_delete_contact(self.conn, cid, {"deleted": True})
        self.assertIn(db.normalize_name("Delete Me"), db.load_exclude_list(self.file),
                      "deleting a contact still leaves exclude_list.txt unchanged, "
                      "so the next import re-counts the messages of someone the "
                      "app has been told to delete")

    def test_restoring_takes_the_line_back_off(self):
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.file):
            cid = db.create_contact(self.conn, "Come Back")
            self.conn.commit()
            app.handle_delete_contact(self.conn, cid, {"deleted": True})
            app.handle_delete_contact(self.conn, cid, {"deleted": False})
        self.assertEqual(db.load_exclude_list(self.file), set(),
                         "restoring a deleted contact leaves them on the "
                         "importers' skip list, so they come back in the app and "
                         "stay frozen in the imports")

    def test_restoring_leaves_a_line_another_excluded_contact_still_needs(self):
        """Two people share a name far more often than this file has entries."""
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.file):
            hidden = db.create_contact(self.conn, "Anna Andersson")
            deleted = db.create_contact(self.conn, "Anna Andersson")
            self.conn.commit()
            app.handle_set_excluded(self.conn, hidden, {"excluded": True})
            app.handle_delete_contact(self.conn, deleted, {"deleted": True})
            app.handle_delete_contact(self.conn, deleted, {"deleted": False})
        self.assertIn(db.normalize_name("Anna Andersson"),
                      db.load_exclude_list(self.file),
                      "restoring one Anna Andersson took the line out from under "
                      "the other one, who is still hidden — and nothing in the "
                      "app would show it, because her column still says excluded")

    def test_the_backfill_catches_up_a_delete_made_before_the_mirror_existed(self):
        """Anyone already deleted. The handler fix alone does nothing for them, and
        they are the ones the importers would still be collecting for."""
        cid = db.create_contact(self.conn, "Old Delete")
        db.set_deleted(self.conn, cid, 1)
        self.conn.commit()
        self.assertEqual(db.load_exclude_list(self.file), set())
        added = db.mirror_excluded_to_file(self.conn, self.file)
        self.assertEqual(added, 1)
        self.assertIn(db.normalize_name("Old Delete"), db.load_exclude_list(self.file))

    def test_the_backfill_adds_nothing_on_a_second_run(self):
        cid = db.create_contact(self.conn, "Old Delete")
        db.set_deleted(self.conn, cid, 1)
        self.conn.commit()
        db.mirror_excluded_to_file(self.conn, self.file)
        self.assertEqual(db.mirror_excluded_to_file(self.conn, self.file), 0,
                         "the startup backfill appends a duplicate line on every "
                         "launch, so exclude_list.txt grows without limit")

    def test_the_backfill_does_not_delete_names_the_owner_typed(self):
        """One-directional on purpose: a name in the file with no contact behind
        it is someone who has not been imported yet, not litter."""
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write("# do not import\nNever Imported\n")
        db.mirror_excluded_to_file(self.conn, self.file)
        self.assertIn(db.normalize_name("Never Imported"),
                      db.load_exclude_list(self.file))


class TestTheBackfillArrivesBeforeTheImportersDo(unittest.TestCase):
    def test_the_backfill_runs_before_the_autosync_thread_starts(self):
        main = py_block(code_of(os.path.join(CRM, "app.py")), "def main() -> None:")
        self.assertIn("mirror_excluded_to_file", main,
                      "startup no longer reconciles the exclude file, so the "
                      "deletes made before NC-146 stay invisible to the importers")
        self.assertLess(
            main.index("mirror_excluded_to_file"), main.index("threading.Thread"),
            "the reconcile now runs after the autosync thread is started, so the "
            "first import of a launch can still collect for a deleted contact — "
            "which is the whole bug, one launch narrower")


class TestTheFileIsWhatTheImportersConsult(unittest.TestCase):
    """The premise. If an importer ever switches to reading the `excluded`
    column directly, the mirror stops being load-bearing for it and this
    file's reasoning needs revisiting rather than quietly half-applying."""

    def test_every_module_holding_an_exclude_file_actually_reads_it(self):
        # api_contacts.py is the one module that holds the path in order to
        # WRITE it — it is the mirror itself, and it reads the file back by
        # hand in handle_exclude_list_get rather than through the normalizing
        # loader, because that view has to show the raw lines.
        offenders = []
        for name in sorted(os.listdir(CRM)):
            if not name.endswith(".py") or name == "api_contacts.py":
                continue
            src = code_of(os.path.join(CRM, name))
            if "EXCLUDE_FILE = " in src and "load_exclude_list" not in src:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "these modules declare an exclude-list path and never "
                         "read it, so the file they point at decides nothing")

    def test_enough_importers_read_it_that_the_mirror_matters(self):
        readers = [n for n in sorted(os.listdir(CRM))
                   if n.endswith("_import.py")
                   and "load_exclude_list" in code_of(os.path.join(CRM, n))]
        self.assertGreaterEqual(
            len(readers), 8,
            "far fewer importers read exclude_list.txt than when NC-146 was "
            "written. Either they have moved to the `excluded` column — in "
            "which case the mirror is no longer the fix — or they have stopped "
            "honouring exclusions at all")


if __name__ == "__main__":
    unittest.main()
