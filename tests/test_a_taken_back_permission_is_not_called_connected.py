"""NC-183. Google taking a permission back leaves the token file where it was.

There is no mechanism by which a revocation at Google's end deletes a file on
this Mac, and none by which an expiry does either. So `gmail_token_<slug>.json`
existing means *somebody signed in once*, and means nothing at all about whether
that permission still works.

[[NC-117]]'s panel judged readiness from that file, and the result was the app
disagreeing with itself inside one screen: the panel printed **"Connected"**
directly above a source row printing **"Gmail needs you to sign in again"** —
[[NC-177]]'s sentence, written by the 12-hourly loop when the child came back
with exit 4. A person reading top to bottom is told the thing is fine and then
told to fix it, which is worse than either alone, because now neither can be
believed.

The expired state does offer a Sign in button — third in the row, unstyled, next
to the word Connected. That is the right control in a place nobody would look
for it, which is why it reads as absent. The fault is the claim, not a missing
button.

**The loop already knows.** It gets exit 4, writes a sentence under
`NEEDS_SIGN_IN_KEY` and — as of NC-183 — the addresses the child named under
`NEEDS_SIGN_IN_WHO_KEY`. This file gates that `status()` reads them, that the
panel's words follow, and that the two keys cannot drift apart.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db, importers  # noqa: E402,F401
import api_gmail  # noqa: E402
import api_import  # noqa: E402
import config  # noqa: E402
import gmail_accounts  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


class _Connected(Base):
    """One address, its client file, and a token: connected, as far as disk knows."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="nc183_")
        patch = mock.patch.object(config, "SECRETS_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        self._before = os.environ.get("GMAIL_ACCOUNTS")
        self.addCleanup(self._restore)
        os.environ["GMAIL_ACCOUNTS"] = "you@example.com"
        api_gmail.reset_for_test()
        self.addCleanup(api_gmail.reset_for_test)
        for path, blob in ((gmail_accounts.accounts_from_env()["you@example.com"],
                            {"installed": {"client_id": "x"}}),
                           (gmail_accounts.token_path("you@example.com"),
                            {"token": "t", "refresh_token": "r"})):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)

    def _restore(self):
        if self._before is None:
            os.environ.pop("GMAIL_ACCOUNTS", None)
        else:
            os.environ["GMAIL_ACCOUNTS"] = self._before

    def revoked(self, *named):
        """What the 12-hourly loop writes when the child comes back with exit 4."""
        api_import._set_needs_sign_in(
            self.conn, "gmail",
            api_import._sign_in_sentence("gmail", named), named)
        self.conn.commit()


class TestTheTokenFileIsNotTheAnswer(_Connected):

    def test_connected_until_something_says_otherwise(self):
        st = api_gmail.status(self.conn)
        self.assertEqual(st["ready"], ["you@example.com"])
        self.assertEqual(st["needs_sign_in"], [])
        self.assertFalse(st["accounts"][0]["sign_in_expired"])

    def test_a_revoked_permission_is_not_ready(self):
        """The file is still there — that is the whole point of NC-183."""
        self.revoked("you@example.com")
        self.assertTrue(os.path.exists(gmail_accounts.token_path("you@example.com")))
        st = api_gmail.status(self.conn)
        self.assertEqual(st["ready"], [])
        self.assertEqual(st["needs_sign_in"], ["you@example.com"])
        self.assertTrue(st["accounts"][0]["sign_in_expired"])

    def test_a_marker_that_names_nobody_covers_every_signed_in_account(self):
        """The child stops at the first account needing consent and does not
        always print which. Offering a sign-in to an account that turns out to
        be fine costs one extra trip through a Google tab; withholding it from
        the broken one costs a source that silently never collects again."""
        os.environ["GMAIL_ACCOUNTS"] = "a@example.com,b@example.com"
        for who in ("a@example.com", "b@example.com"):
            for path in (gmail_accounts.accounts_from_env()[who],
                         gmail_accounts.token_path(who)):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("{}")
        self.revoked()                      # a sentence, no names
        self.assertEqual(sorted(api_gmail.status(self.conn)["needs_sign_in"]),
                         ["a@example.com", "b@example.com"])

    def test_only_the_account_the_child_named_when_it_named_one(self):
        os.environ["GMAIL_ACCOUNTS"] = "a@example.com,b@example.com"
        for who in ("a@example.com", "b@example.com"):
            for path in (gmail_accounts.accounts_from_env()[who],
                         gmail_accounts.token_path(who)):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("{}")
        self.revoked("b@example.com")
        st = api_gmail.status(self.conn)
        self.assertEqual(st["needs_sign_in"], ["b@example.com"])
        self.assertEqual(st["ready"], ["a@example.com"])

    def test_signing_in_again_clears_it(self):
        """The cure, and the only one: a run that exits 0. Nothing has to know
        that is what happened — see _set_needs_sign_in."""
        self.revoked("you@example.com")
        api_import._set_needs_sign_in(self.conn, "gmail", "")
        self.conn.commit()
        self.assertEqual(api_gmail.status(self.conn)["ready"], ["you@example.com"])
        self.assertEqual(api_import.needs_sign_in_accounts(self.conn, "gmail"), [])

    def test_the_two_keys_are_cleared_together(self):
        """A sentence cleared without its addresses would leave the panel
        offering a Sign in button for an account nothing is waiting on."""
        self.revoked("you@example.com")
        self.assertEqual(api_import.needs_sign_in_accounts(self.conn, "gmail"),
                         ["you@example.com"])
        api_import._set_needs_sign_in(self.conn, "gmail", "")
        self.assertEqual(api_import.needs_sign_in_accounts(self.conn, "gmail"), [])

    def test_without_a_database_it_errs_towards_connected(self):
        """status() is also called with no conn. The safe direction is calling a
        working account connected, not sending somebody back through Google for
        no reason."""
        self.revoked("you@example.com")
        self.assertEqual(api_gmail.status()["ready"], ["you@example.com"])

    def test_the_endpoint_asks_for_the_database(self):
        """The router hands a connection only to a handler whose first parameter
        is named conn. Rename it and this silently goes back to trusting files."""
        import inspect
        params = list(inspect.signature(api_gmail.handle_gmail_status).parameters)
        self.assertEqual(params[:1], ["conn"])


class TestThePanelAndTheRowAgree(_Connected):
    """The symptom: two sentences on one screen saying opposite things."""

    def test_the_row_and_the_panel_now_say_the_same_thing(self):
        self.revoked("you@example.com")
        rows = {s["key"]: s for s in
                api_import.handle_import_sources(self.conn)["sources"]}
        self.assertIn("sign in again", rows["gmail"]["blocked"])
        self.assertNotIn("you@example.com", api_gmail.status(self.conn)["ready"],
                         "the panel calls it ready while the row above says the "
                         "permission is gone — the app disagreeing with itself "
                         "inside one screen, which is NC-183 exactly")


class TestTheWordsOnScreenFollow(unittest.TestCase):
    """What the panel renders, read out of the source it renders from."""

    def setUp(self):
        with open(os.path.join(APP, "gmail.js"), encoding="utf-8") as fh:
            self.js = fh.read()
        self.row = self.js[self.js.index("function AccountRow"):
                           self.js.index("export function GmailSetup")]

    def test_expired_outranks_the_token_file(self):
        """Order is the whole point. Below has_token, the file wins and it says
        Connected; above waiting, it talks over an open Google tab."""
        order = [m.group(1) for m in
                 re.finditer(r"(waiting|expired|!row\.has_token|"
                             r"!row\.has_credentials)\)? \{ says", self.row)]
        self.assertIn("expired", order, "the panel no longer reads sign_in_expired")
        self.assertLess(order.index("expired"), order.index("!row.has_token"))
        self.assertLess(order.index("waiting"), order.index("expired"))

    def test_it_does_not_say_connected_about_a_taken_back_permission(self):
        i = self.row.index("expired) { says")
        self.assertNotIn("Connected", self.row[i:i + 90])

    def test_the_sign_in_is_the_primary_button_then(self):
        """It was already there — third in the row, unstyled, beside the word
        Connected. The right control in a place nobody would look for it."""
        self.assertIn("(!row.has_token || expired) && html`", self.row)
        m = re.search(r"\(!row\.has_token \|\| expired\) && html`\s*<button ([^>]*)",
                      self.row)
        self.assertIsNotNone(m)
        self.assertIn("primary", m.group(1))

    def test_the_quiet_duplicate_is_not_shown_at_the_same_time(self):
        """Two Sign in buttons on one row is a question, not an instruction."""
        self.assertIn("row.has_token && !expired && !waiting && html`", self.row)


if __name__ == "__main__":
    unittest.main()
