"""NC-181. "Gmail needs you to sign in again" — to which one?

`GMAIL_ACCOUNTS` is a comma-separated list and crm/gmail_import.py loops over
every address in it. Everything the Data page knows about a source is keyed by
source — `configured`, `blocked`, `synced_at`, and [[NC-177]]'s
`import_needs_signin_gmail` — so with two addresses configured and one of them
revoked, the row said Gmail needed signing in and could not say which. The only
way to find out was to open Google twice.

Two addresses configured, one of them revoked, is the ordinary case.

The child is the only thing that knows — a revoked permission looks like a
healthy one from the outside, because revoking changes nothing on this machine. So
it prints the address on its own marked line, `needs-sign-in: you@example.com`,
and the scheduler reads that.

**A marker line, not the prose.** crm/gmail_import.py already prints
"you@example.com needs you to sign in to Google again — …", and a parser aimed
at that sentence breaks the next time somebody improves the wording. Same reason
importers.parse_receipts exists.

**It names one account, and says so.** The child stops at the first address that
needs consent, so a second broken one is only found after the first is fixed.
The sentence is written to be true about that: "for a@…" rather than anything
implying the rest were checked and are fine.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db, importers  # noqa: E402,F401
import api_import  # noqa: E402
import config  # noqa: E402
import gmail_accounts  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestReadingTheMarker(unittest.TestCase):
    """importers.parse_sign_in_accounts, on its own."""

    def test_ordinary_output_names_nobody(self):
        self.assertEqual(importers.parse_sign_in_accounts(
            "42 new messages\nDone.\n"), [])
        self.assertEqual(importers.parse_sign_in_accounts(""), [])
        self.assertEqual(importers.parse_sign_in_accounts(None), [])

    def test_the_marker_line_is_read(self):
        out = ("0 contacts available\nneeds-sign-in: you@example.com\n"
               "you@example.com needs you to sign in to Google again — …\n")
        self.assertEqual(importers.parse_sign_in_accounts(out),
                         ["you@example.com"])

    def test_the_prose_alone_is_not_enough(self):
        """The line that reads as a sentence must NOT be what is parsed — that
        is the whole point of having a marker."""
        self.assertEqual(importers.parse_sign_in_accounts(
            "you@example.com needs you to sign in to Google again — …\n"), [])

    def test_two_accounts_keep_their_order_and_do_not_repeat(self):
        out = ("needs-sign-in: b@example.com\nneeds-sign-in: a@example.com\n"
               "needs-sign-in: b@example.com\n")
        self.assertEqual(importers.parse_sign_in_accounts(out),
                         ["b@example.com", "a@example.com"])

    def test_the_marker_has_to_start_the_line(self):
        """A marker in the MIDDLE of a line is somebody quoting it — a log
        wrapper, a traceback, a test harness's own `| ` prefix — not the
        child announcing an account.

        The two failures are not equally bad. Missing a real marker costs the
        address and leaves the message intact; matching a quoted one puts a
        wrong address, or a fragment of a sentence, on the Data page as the
        account to go and fix. So this reads the start of the line only.
        """
        self.assertEqual(importers.parse_sign_in_accounts(
            "see also needs-sign-in: someone@example.com for details\n"), [])
        self.assertEqual(importers.parse_sign_in_accounts(
            "  | needs-sign-in: someone@example.com\n"), [])
        # …and leading whitespace alone is still the child's own line.
        self.assertEqual(importers.parse_sign_in_accounts(
            "   needs-sign-in: someone@example.com\n"), ["someone@example.com"])

    def test_a_marker_with_nothing_after_it_names_nobody(self):
        self.assertEqual(importers.parse_sign_in_accounts("needs-sign-in:\n"), [])

    def test_the_two_sides_use_one_spelling(self):
        """The child writes the marker and the scheduler reads it. If the two
        ever spelled it differently the page would go quiet, not loud, which is
        the failure that hides."""
        with open(os.path.join(REPO, "crm", "gmail_import.py"), encoding="utf-8") as fh:
            child = fh.read()
        self.assertIn("SIGN_IN_MARKER", child)
        self.assertNotIn('"needs-sign-in', child,
                         "the child hardcodes the marker instead of importing it")


class TestTheSentence(unittest.TestCase):
    """_sign_in_sentence — what a person actually reads."""

    def test_with_no_account_it_still_reads_as_english(self):
        """The LinkedIn-shaped case, and any future source with one credential:
        there is no address to name and the sentence must not have a hole in it."""
        said = api_import._sign_in_sentence("gmail")
        self.assertIn("Gmail needs you to sign in again.", said)
        self.assertNotIn("for ,", said)
        self.assertNotIn("  ", said)

    def test_one_account_is_named(self):
        said = api_import._sign_in_sentence("gmail", ["you@example.com"])
        self.assertIn("you@example.com", said)
        self.assertIn("sign in again for you@example.com", said)

    def test_an_empty_address_is_dropped_rather_than_printed(self):
        """A marker line with nothing after it parses to nothing, but the
        sentence is also handed lists from elsewhere. "sign in again for ." is
        not a sentence, and it is the kind of hole nobody notices until it is on
        somebody's screen."""
        said = api_import._sign_in_sentence("gmail", ["", None, "you@example.com"])
        self.assertIn("sign in again for you@example.com.", said)
        self.assertNotIn(", ,", said)
        said = api_import._sign_in_sentence("gmail", ["", None])
        self.assertIn("Gmail needs you to sign in again.", said)

    def test_two_accounts_are_both_named(self):
        said = api_import._sign_in_sentence("gmail", ["a@example.com", "b@example.com"])
        self.assertIn("a@example.com", said)
        self.assertIn("b@example.com", said)

    def test_the_rest_of_the_sentence_is_unchanged(self):
        """NC-177's promise — nothing opened a window at you — has to survive
        this. It is the half that stops the message reading as an attack."""
        for accounts in ((), ["you@example.com"]):
            said = api_import._sign_in_sentence("gmail", accounts)
            self.assertIn("when you are ready", said)
            self.assertIn("not before", said)


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class TestItReachesTheDataPage(Base):
    """End to end through run_autosync, which is the only thing that writes it."""

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        os.environ["NC_GMAIL_AUTO"] = "1"
        _sec = tempfile.mkdtemp(prefix="nc181_")
        creds = os.path.join(_sec, "client.json")
        with open(creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        # NC-117: and a saved sign-in. A configured account with a file and no
        # token is its own not-ready state now — the one every install passes
        # through on the way in — so a test that means "attempted" has to say
        # both halves. SECRETS_DIR is redirected because that is where the token
        # file lives, and reading the developer's own would make this pass or
        # fail for a reason unrelated to the code.
        _p = mock.patch.object(config, "SECRETS_DIR", _sec)
        _p.start()
        self.addCleanup(_p.stop)
        for _who in ("first@example.com", "second@example.com"):
            with open(gmail_accounts.token_path(_who), "w", encoding="utf-8") as fh:
                fh.write("{}")
        self._before = os.environ.get("GMAIL_ACCOUNTS")
        os.environ["GMAIL_ACCOUNTS"] = (f"first@example.com:{creds},"
                                        f"second@example.com:{creds}")
        self.addCleanup(
            lambda: os.environ.__setitem__("GMAIL_ACCOUNTS", self._before)
            if self._before is not None else os.environ.pop("GMAIL_ACCOUNTS", None))
        self._detect = importers.detect_sources
        importers.detect_sources = lambda: []
        self.addCleanup(lambda: setattr(importers, "detect_sources", self._detect))

        orig_run, orig_refresh, orig_path = (app.subprocess.run,
                                             app.engagement.refresh, db.DB_PATH)
        self.child_output = ""
        app.subprocess.run = lambda cmd, **kw: _Result(
            importers.EXIT_NEEDS_SIGN_IN, self.child_output)
        app.engagement.refresh = lambda conn: None
        db.DB_PATH = self.path
        self.addCleanup(lambda: (setattr(app.subprocess, "run", orig_run),
                                 setattr(app.engagement, "refresh", orig_refresh),
                                 setattr(db, "DB_PATH", orig_path)))

    def _line(self):
        rows = {s["key"]: s for s in
                api_import.handle_import_sources(self.conn)["sources"]}
        return rows["gmail"]["blocked"]

    def test_the_address_the_child_named_is_on_the_page(self):
        self.child_output = ("needs-sign-in: second@example.com\n"
                             "second@example.com needs you to sign in…\n")
        app.run_autosync()
        said = self._line()
        self.assertIn("second@example.com", said)
        self.assertNotIn("first@example.com", said,
                         "the account that is fine was named as broken")

    def test_a_child_that_names_nobody_still_says_something(self):
        """An older build, or a failure path that did not get as far as naming
        an account. Losing the whole message would be worse than losing the
        address."""
        self.child_output = "something went wrong\n"
        app.run_autosync()
        said = self._line()
        self.assertTrue(said)
        self.assertIn("Gmail needs you to sign in again.", said)

    def test_the_marker_is_read_from_stderr_too(self):
        """Nothing guarantees which stream a subprocess uses, and run_autosync
        captures both. Reading only stdout would be a silent half-fix."""
        orig = app.subprocess.run
        app.subprocess.run = lambda cmd, **kw: _Result(
            importers.EXIT_NEEDS_SIGN_IN, "", "needs-sign-in: second@example.com\n")
        try:
            app.run_autosync()
        finally:
            app.subprocess.run = orig
        self.assertIn("second@example.com", self._line())


class TestTheRealChildPrintsIt(unittest.TestCase):
    """No stubs. The marker has to survive being printed by the actual script."""

    def test_the_script_names_the_account_it_stopped_on(self):
        import json
        import subprocess
        d = tempfile.mkdtemp(prefix="nc181_e2e_")
        creds = os.path.join(d, "client.json")
        with open(creds, "w", encoding="utf-8") as fh:
            json.dump({"not": "a real client"}, fh)
        env = dict(os.environ)
        env["CRM_DATA_DIR"] = os.path.join(d, "data")
        env["GMAIL_ACCOUNTS"] = f"only@example.com:{creds}"
        proc = subprocess.run(
            [sys.executable, "gmail_import.py", "--scan", "--no-browser"],
            cwd=os.path.join(REPO, "crm"), env=env, capture_output=True,
            text=True, timeout=120)
        self.assertEqual(proc.returncode, importers.EXIT_NEEDS_SIGN_IN,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertEqual(
            importers.parse_sign_in_accounts(proc.stdout + proc.stderr),
            ["only@example.com"])


if __name__ == "__main__":
    unittest.main()
