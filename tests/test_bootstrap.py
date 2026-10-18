"""Tripwire: prove the suite is pointed at a throwaway data dir, not the real one.

If a test file ever imports a crm module before tests.helpers has run, CRM_DATA_DIR
is never set and the whole suite silently starts reading and writing the real
data/ folder — a large store of actual contacts, messages and embedding caches
(see the warning in tests/helpers.py). Every other test would still pass while
it happened, which is exactly why this one exists.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import config  # noqa: E402


class TestBootstrap(unittest.TestCase):
    def test_data_dir_is_a_temp_dir(self):
        self.assertTrue(
            config.DATA_DIR.startswith(tempfile.gettempdir()),
            f"tests are pointed at {config.DATA_DIR!r}, not a temp dir — a crm "
            f"module was imported before tests.helpers set CRM_DATA_DIR",
        )
        self.assertIn(
            "ncrm-tests-", os.path.basename(config.DATA_DIR),
            f"CRM_DATA_DIR is a temp dir but not one this suite made: {config.DATA_DIR!r}",
        )


class TestEachTestGetsItsOwnDatabase(unittest.TestCase):
    """NC-211. Test databases are clones of one template built per run. A clone
    shares the template's blocks until it is written, so the property that
    matters is that a write never travels: not back to the template, and not on
    to the next test's database."""

    def test_a_write_stays_in_its_own_test(self):
        from tests.helpers import Base, db
        import tests.helpers as helpers

        class One(Base):
            def runTest(self):
                pass

        first = One()
        first.setUp()
        try:
            db.create_contact(first.conn, "Only In The First")
            first.conn.commit()
        finally:
            first.tearDown()
        second = One()
        second.setUp()
        try:
            n = second.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        finally:
            second.tearDown()
        self.assertEqual(n, 0, "the second test saw the first test's person")
        conn = db.get_db(helpers._TEMPLATE_DB)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0,
                             "a test wrote into the template")
        finally:
            conn.close()

    def test_the_template_is_the_schema_init_db_builds(self):
        """So a test's database is what the app's own first run would make."""
        import sqlite3
        from tests.helpers import db, fresh_crm_db
        d = tempfile.mkdtemp()
        built, cloned = os.path.join(d, "built.db"), os.path.join(d, "cloned.db")
        db.init_db(built)
        fresh_crm_db(cloned)

        def schema(path):
            conn = sqlite3.connect(path)
            try:
                return sorted(r for r in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            finally:
                conn.close()
        self.assertEqual(schema(cloned), schema(built))
