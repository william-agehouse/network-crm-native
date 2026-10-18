"""crm/app.py's startup recovery: what happens when the database cannot be opened.

NC-011. `main()` began with a bare `db.init_db()`. A malformed file — an
interrupted write, a half-finished restore, a full disk — raised there, before the
HTTP server existed, so the app never opened and there was no in-app way back. The
only recourse was a terminal, which is the one thing the packaged .app exists to
avoid.

Two things are being asserted, and the second matters as much as the first: that a
corrupt file no longer stops startup, and that the corrupt bytes are still on disk
afterwards. A recovery that silently discards someone's only copy of their notes
would be a far worse bug than the crash it replaces.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import app, db  # noqa: E402 — sets CRM_DATA_DIR before crm imports

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestACorruptDatabaseDoesNotKillTheApp(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="corrupt_db_")
        self.path = os.path.join(self.dir, "crm.db")
        self._saved = db.DB_PATH
        db.DB_PATH = self.path

    def tearDown(self):
        db.DB_PATH = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def _aside(self):
        return [f for f in os.listdir(self.dir) if ".unreadable-" in f]

    def test_it_starts_fresh_and_keeps_the_unreadable_file(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("this is definitely not a sqlite database")
        notice = app._init_db_or_recover()
        self.assertTrue(notice, "recovery happened silently")
        self.assertIn("moved to", notice)
        self.assertIn("Nothing was deleted", notice)
        # The corrupt bytes are STILL THERE. A corrupt SQLite file is often
        # recoverable and is the only copy of those notes; deleting it to make
        # startup work would be worse than the bug this fixes.
        aside = self._aside()
        self.assertEqual(len(aside), 1,
                         f"the bad file was not preserved: {os.listdir(self.dir)}")
        with open(os.path.join(self.dir, aside[0]), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "this is definitely not a sqlite database")
        conn = db.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)
        conn.close()

    def test_a_healthy_database_is_left_completely_alone(self):
        db.init_db()
        conn = db.get_db()
        db.create_contact(conn, "Anna Svensson")
        conn.commit()
        conn.close()
        self.assertEqual(app._init_db_or_recover(), "", "a healthy db reported recovery")
        self.assertEqual(self._aside(), [], "a healthy db was moved aside")
        conn = db.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)
        conn.close()

    def test_a_missing_file_is_not_treated_as_corruption(self):
        """No file at all is the first-run case, and init_db creates it."""
        self.assertEqual(app._init_db_or_recover(), "")
        self.assertEqual(self._aside(), [])

    def test_the_notice_reaches_the_app_and_can_be_dismissed(self):
        """Printing it to a terminal the .app user never opens is the same as not
        saying it. It rides on /api/status, which the shell already polls."""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not a database")
        notice = app._init_db_or_recover()
        conn = db.get_db()
        db.state_set(conn, "db_recovery_notice", notice)
        conn.commit()
        self.assertEqual(app.handle_status(conn)["db_recovery"], notice)
        app._dismiss_db_recovery_notice(conn)
        self.assertEqual(app.handle_status(conn)["db_recovery"], "")
        conn.close()

    def test_the_shell_actually_renders_it(self):
        """A field on /api/status that nothing reads is not a surface either."""
        with open(os.path.join(REPO, "static", "app", "main.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("db_recovery", js, "the shell never reads the field")
        self.assertIn("db-recovery", js, "the shell never renders a banner")
        self.assertIn("/api/db-recovery-notice/dismiss", js, "no way to dismiss it")


class TestMainActuallyUsesTheRecoveryPath(unittest.TestCase):
    """The tests above drive `_init_db_or_recover()` directly, which means they all
    still pass if `main()` goes back to calling `db.init_db()` itself, so the call
    site needs a source-level check as well.

    `main()` cannot be called from a test — it binds a port and blocks in
    serve_forever — so this reads its source. Docstrings and comments are stripped
    first: the function's own comments name `db.init_db()` while explaining why it
    is no longer called, and a gate that greps raw source trips on its own rationale.
    """

    @staticmethod
    def _code(name):
        import ast
        src = io.open(os.path.join(REPO, "crm", "app.py"), encoding="utf-8").read()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = list(node.body)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]
                return "\n".join(ast.unparse(st) for st in body)
        raise AssertionError(f"{name}() not found in crm/app.py")

    def test_startup_goes_through_the_recovery_wrapper(self):
        code = self._code("main")
        self.assertIn("_init_db_or_recover()", code,
                      "main() no longer uses the recovery wrapper — a corrupt "
                      "database is back to killing the app before it can open")
        self.assertNotIn("db.init_db()", code,
                         "main() calls db.init_db() directly again, which is exactly "
                         "the unguarded call NC-011 was about")

    def test_startup_stores_the_notice_so_the_app_can_show_it(self):
        code = self._code("main")
        self.assertIn("db_recovery_notice", code,
                      "main() drops the recovery notice, so /api/status has nothing "
                      "to report and the user is never told their data moved")
