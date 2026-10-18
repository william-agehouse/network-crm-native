"""NC-212. Opening a database for a request must not rewrite its index file.

When a connection opens a SQLite database that no other connection has open,
SQLite rebuilds the database's 32 KB index file from scratch: about 36 KB
written to the SSD.
The server opens a connection per request, and the window asks for its status
every 15 seconds, so that was nearly all of what an idle app wrote, a few
hundred megabytes a day. The server now holds one idle connection to each database, and
these tests pin what that has to mean.
"""

import ctypes
import ctypes.util
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import app, arc, db, js_code_of  # noqa: E402

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "static", "app", "main.js")


def _bytes_written_by_this_process():
    """What macOS has counted this process writing to disk (proc_pid_rusage)."""
    class RUsage(ctypes.Structure):
        _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(f"f{i}", ctypes.c_uint64) for i in range(16)] + [
            ("diskio_bytesread", ctypes.c_uint64), ("diskio_byteswritten", ctypes.c_uint64)]
    r = RUsage()
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    if libc.proc_pid_rusage(os.getpid(), 2, ctypes.byref(r)) != 0:
        raise OSError("proc_pid_rusage failed")
    return r.diskio_byteswritten


class _Held(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.crm = os.path.join(self.dir, "crm.db")
        self.archive = os.path.join(self.dir, "archive.db")
        db.init_db(self.crm)
        for p in (mock.patch.object(db, "DB_PATH", self.crm),
                  mock.patch.object(arc, "ARCHIVE_DB_PATH", self.archive),
                  mock.patch.dict(app._HELD_CONNECTIONS, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._close_held)

    def _close_held(self):
        for conn in list(app._HELD_CONNECTIONS.values()):
            conn.close()

    def open_and_close(self):
        conn = db.get_db(self.crm)
        conn.execute("SELECT COUNT(*) FROM contacts").fetchone()
        conn.close()


class TestTheServerHoldsItsDatabasesOpen(_Held):

    def index_changed_by_an_open(self):
        before = os.stat(self.crm + "-shm").st_mtime_ns
        time.sleep(0.01)
        self.open_and_close()
        return os.stat(self.crm + "-shm").st_mtime_ns != before

    def test_with_a_connection_held_an_open_leaves_the_index_alone(self):
        with mock.patch.object(app, "_HOLD_DATABASES", True):
            app._hold_databases_open()
        self.assertFalse(self.index_changed_by_an_open())

    def test_without_one_every_open_rebuilds_it(self):
        """The cost the held connection removes, shown directly."""
        self.open_and_close()
        self.assertTrue(self.index_changed_by_an_open())

    @unittest.skipUnless(sys.platform == "darwin", "counts what macOS says this process wrote")
    def test_what_fifty_requests_write(self):
        def fifty():
            start = _bytes_written_by_this_process()
            for _ in range(50):
                self.open_and_close()
            return _bytes_written_by_this_process() - start
        without = fifty()
        with mock.patch.object(app, "_HOLD_DATABASES", True):
            app._hold_databases_open()
        held = fifty()
        self.assertGreater(without, 50 * 16 * 1024, "about 36 KB an open")
        self.assertEqual(held, 0)

    def test_a_missing_archive_is_not_created_by_holding_it(self):
        with mock.patch.object(app, "_HOLD_DATABASES", True):
            app._hold_databases_open()
        self.assertFalse(os.path.exists(self.archive))
        self.assertEqual(list(app._HELD_CONNECTIONS), [self.crm])

    def test_the_archive_is_held_once_it_exists(self):
        """A fresh install's first import creates archive.db while the server runs."""
        with mock.patch.object(app, "_HOLD_DATABASES", True):
            app._hold_databases_open()
            arc.get_archive_db(self.archive).close()
            app._hold_databases_open()
        self.assertEqual(sorted(app._HELD_CONNECTIONS), sorted([self.crm, self.archive]))

    def test_outside_the_server_nothing_is_held(self):
        """Tests handle requests against a new database each time; holding would pile up."""
        app._hold_databases_open()
        self.assertEqual(app._HELD_CONNECTIONS, {})

    def test_every_request_connection_goes_through_it(self):
        with mock.patch.object(app, "_HOLD_DATABASES", True):
            app.CrmHandler._with_conn(lambda conn: None)
        self.assertIn(self.crm, app._HELD_CONNECTIONS)


class TestTheServerSwitchesItOn(unittest.TestCase):

    def test_main_turns_holding_on_before_the_first_request(self):
        import inspect
        src = inspect.getsource(app.main)
        on = src.index("_HOLD_DATABASES = True")
        self.assertLess(on, src.index("_hold_databases_open()"))
        self.assertLess(src.index("_hold_databases_open()"), src.index("ThreadingHTTPServer("))


class TestTheTimerDoesNotCountTheCopies(unittest.TestCase):
    """The helper replaces its copies with fresh files every couple of minutes, so
    opening one just to count its rows rebuilt its index and read it cold."""

    def test_the_automatic_check_asks_for_no_counts(self):
        import inspect
        import api_import
        self.assertIn("detect_unprotected_sources(count=False)",
                      inspect.getsource(api_import._autosync_pending))

    def test_without_counts_no_copy_is_opened(self):
        import importers
        opened = []
        with mock.patch.object(importers, "_readable", lambda p: True), \
                mock.patch.object(importers, "_quick_count", lambda *a: opened.append(a) or 7), \
                mock.patch.object(importers, "_photo_cache_has_files", lambda p: False, create=True):
            found = importers.detect_unprotected_sources(count=False)
            self.assertEqual(opened, [])
            self.assertTrue(found, "the copies are still found, only not counted")
            self.assertEqual({f["count"] for f in found if f["source"] in ("imessage", "whatsapp", "contacts")}, {0})
            importers.detect_unprotected_sources()
            self.assertEqual(len(opened), 3, "the Data page still gets its counts")


class TestAHiddenWindowStopsAsking(unittest.TestCase):

    def test_the_status_check_skips_a_hidden_window_and_catches_up_when_seen(self):
        code = js_code_of(MAIN)
        ping = code[code.index("const ping = async"):code.index("ping();", code.index("const ping = async"))]
        self.assertIn("if (document.hidden) return;", ping)
        self.assertIn('addEventListener("visibilitychange", ping)', code)


if __name__ == "__main__":
    unittest.main()
