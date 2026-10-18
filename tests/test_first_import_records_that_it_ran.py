"""NC-198. A fresh install's first import records that it ran.

handle_import_run's bookkeeping after a successful import was one try block:
the familiarity score first, then the "last synced" stamp, the sign-in clear,
the pasted-copy mark and the new-people count — and `except: pass` after it.
On a fresh install the score always threw: archive.db exists (something opens
it before any import writes a message) but holds no tables, so the query hit
"no such table: messages". Everything after it was skipped, silently.

The case, as a fixture: importing a three-card vCard file into an empty install
makes "3 people", so the Apple Contacts row must stop saying "Has not run here
yet" — the server has to be told the source ran.
"""

import os
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

import api_import  # noqa: E402
import engagement  # noqa: E402


class TestAnEmptyArchiveHasNoFamiliarityToCompute(Base):

    def test_an_archive_with_no_tables_is_empty_not_an_error(self):
        path = os.path.join(self.dir, "archive.db")
        sqlite3.connect(path).close()        # the 4 KB file a fresh install has
        self.assertTrue(os.path.exists(path))
        self.assertEqual(engagement.refresh(self.conn, archive_path=path), 0)


class TestTheStampDoesNotDependOnTheScore(Base):

    def run_contacts_import(self):
        vcf = os.path.join(self.dir, "people.vcf")
        with open(vcf, "w", encoding="utf-8") as fh:
            fh.write("BEGIN:VCARD\nVERSION:3.0\nFN:Test Person\nEND:VCARD\n")
        with mock.patch.object(api_import.db, "DB_PATH", self.path), \
             mock.patch.object(api_import.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0,
                                                            stdout="", stderr="")), \
             mock.patch.dict(os.environ, {"NC_ENRICH_AUTO": "0"}):
            return api_import.handle_import_run({"source": "contacts", "path": vcf})

    def stamp(self):
        return db.state_get(db.get_db(self.path), "import_synced_contacts", "")

    def test_the_first_import_on_a_fresh_install_is_stamped(self):
        out = self.run_contacts_import()
        self.assertTrue(out["ok"])
        self.assertTrue(self.stamp(), "the import succeeded and nothing recorded it")

    def test_a_failing_score_still_leaves_the_stamp_and_says_so(self):
        def boom(conn, archive_path=None):
            raise sqlite3.OperationalError("no such table: messages")
        with mock.patch.object(api_import.engagement, "refresh", boom), \
             self.assertLogs("networkcrm.import", level="ERROR") as logs:
            self.run_contacts_import()
        self.assertTrue(self.stamp(), "a scoring failure cost the import its stamp")
        self.assertIn("familiarity", "\n".join(logs.output),
                      "the failure was swallowed instead of logged")


if __name__ == "__main__":
    unittest.main()
