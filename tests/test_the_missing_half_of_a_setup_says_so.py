"""NC-180. Gmail needs two things and only one of them is on a screen.

The address goes in `GMAIL_ACCOUNTS`, which is a field under Settings. The
client file from Google — the small JSON that is what lets this app ask Google
anything — has to be fetched from Google Cloud Console by hand and dropped into
the secrets folder. Nothing in the app has ever mentioned it. Filling in the
half that is visible and not the half that is not is the obvious way to end up
half set up, and it is the state this file is about.

What used to happen in it: the background loop queued Gmail every 12 hours, the
subprocess raised `SystemExit("Missing <path>…")`, exit 1, and `run_autosync`
discarded it like any other non-zero. Forever. Meanwhile the Data page reported
Gmail as `configured: true`, because `configured` only ever asked whether
`GMAIL_ACCOUNTS` was set — so the one screen that could have said something was
saying the opposite.

Two claims are gated here, and they are different claims:

* **It says so.** `gmail_accounts.collection_blocked()` is a sentence on both
  lists on the Data page, and it names the file and where to get it.
* **It stops trying.** `_autosync_pending` does not queue a source that could
  only fail. This is the rule the block comment above `API_SOURCES` already
  states — "an unconfigured source is not a broken source; it should simply not
  be attempted" — applied to the half of the configuration `needs` cannot see.

And one that is easy to lose: **the server must not import Google to ask.**
`crm/gmail_import.py` pulls in `google.auth`, `google_auth_oauthlib` and
`googleapiclient` at import time, and `_source_blockers()` runs on every visit
to the Data page. `crm/gmail_accounts.py` exists for that call and nothing else.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db, importers  # noqa: E402,F401
import api_import  # noqa: E402
import api_sources  # noqa: E402
import config  # noqa: E402
import gmail_accounts  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Env(unittest.TestCase):
    """Sets GMAIL_ACCOUNTS and puts it back, with a real secrets dir."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc180_")
        self.creds = os.path.join(self.dir, "client.json")
        self._before = os.environ.get("GMAIL_ACCOUNTS")
        self.addCleanup(self._restore)
        # NC-117 added a second readiness question — has this account ever been
        # signed in — and its answer is a token file inside the secrets dir. So
        # the secrets dir has to be this temp folder or the test reads whatever
        # the developer running it happens to have signed into, and passes or
        # fails for a reason that has nothing to do with the code.
        patch = mock.patch.object(config, "SECRETS_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)

    def _restore(self):
        if self._before is None:
            os.environ.pop("GMAIL_ACCOUNTS", None)
        else:
            os.environ["GMAIL_ACCOUNTS"] = self._before

    def have_the_file(self):
        with open(self.creds, "w", encoding="utf-8") as fh:
            json.dump({"installed": {"client_id": "x"}}, fh)

    def have_signed_in(self, *emails):
        """The other half of being ready, since NC-117: a saved permission.

        A file and no token is its own blocker now — it is the state every
        install passes through on the way in, and the state it stays in forever
        if nobody presses Sign in. Tests that mean "fully set up" have to say
        both halves, because the code can now tell them apart."""
        for email in emails:
            with open(gmail_accounts.token_path(email), "w", encoding="utf-8") as fh:
                fh.write("{}")

    def configured(self, *emails):
        os.environ["GMAIL_ACCOUNTS"] = ",".join(f"{e}:{self.creds}" for e in emails)


def sign_in(email):
    """Write the token file that means "this account has been signed in".

    NC-117 split readiness in two: a client file makes Gmail *possible*, a token
    makes it *able*. Tests written before that split said "has its file" and
    meant "is fully set up" — so they say both halves now, and this is the
    second half. Its content is never parsed by anything the server does; only
    its existence is the fact.
    """
    with open(gmail_accounts.token_path(email), "w", encoding="utf-8") as fh:
        fh.write("{}")


class TestWhatCountsAsReady(_Env):
    """gmail_accounts, on its own."""

    def test_nothing_configured_is_not_a_problem(self):
        """A fresh install has no Gmail account and that is the normal state.
        Saying something here would put a warning on every new install."""
        os.environ.pop("GMAIL_ACCOUNTS", None)
        self.assertEqual(gmail_accounts.missing_credentials(), {})
        self.assertEqual(gmail_accounts.collection_blocked(), "")

    def test_an_account_with_its_file_is_ready(self):
        self.have_the_file()
        self.configured("you@example.com")
        self.have_signed_in("you@example.com")
        self.assertEqual(gmail_accounts.missing_credentials(), {})
        self.assertEqual(gmail_accounts.collection_blocked(), "")

    def test_an_account_with_its_file_and_no_sign_in_is_not_ready(self):
        """NC-117. The state between the two halves, which nothing could see
        until it had a name: the file is there, nobody ever pressed Sign in, and
        a run nobody started may not open a Google tab to fix it (NC-177). Left
        unsaid it is silent forever, on a row that reports itself configured."""
        self.have_the_file()
        self.configured("you@example.com")
        said = gmail_accounts.collection_blocked()
        self.assertIn("not signed in", said)
        self.assertIn("you@example.com", said)
        self.assertEqual(gmail_accounts.missing_credentials(), {},
                         "the file is there — this is the OTHER blocker")

    def test_an_account_without_its_file_is_named(self):
        self.configured("you@example.com")
        self.assertEqual(gmail_accounts.missing_credentials(),
                         {"you@example.com": self.creds})

    def test_the_sentence_names_the_file_and_where_to_get_it(self):
        """The whole difficulty of this state is that the missing thing is
        invisible. A sentence that said "Gmail is not working" would leave the
        person exactly where they started."""
        self.configured("you@example.com")
        said = gmail_accounts.collection_blocked()
        self.assertIn("client.json", said)
        self.assertIn("Google Cloud Console", said)
        self.assertIn("secrets", said)

    def test_two_accounts_missing_files_are_counted(self):
        self.configured("a@example.com", "b@example.com")
        self.assertEqual(len(gmail_accounts.missing_credentials()), 2)
        self.assertIn("2 of these accounts", gmail_accounts.collection_blocked())

    def test_the_default_filename_follows_the_address(self):
        """A bare address derives its own filename, and the sentence has to name
        THAT — telling somebody to save a file under the wrong name is worse
        than telling them nothing."""
        os.environ["GMAIL_ACCOUNTS"] = "First.Last@Example.COM"
        missing = gmail_accounts.missing_credentials()
        self.assertEqual(list(missing), ["first.last@example.com"])
        self.assertIn("gmail_credentials_firstlastexamplecom.json",
                      gmail_accounts.collection_blocked())


class TestTheServerDoesNotImportGoogleToAsk(unittest.TestCase):
    """The reason gmail_accounts.py exists at all."""

    def test_the_light_module_pulls_in_nothing_from_google(self):
        """Executed in a fresh interpreter, because this process has already
        imported gmail_import for other tests — asking sys.modules here would
        answer a question about the test run, not about the server."""
        import subprocess
        script = (
            "import sys, os\n"
            f"sys.path.insert(0, {os.path.join(REPO, 'crm')!r})\n"
            "import gmail_accounts\n"
            "google = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('google', 'google_auth_oauthlib', 'googleapiclient'))\n"
            "print(len(google), google[:3])\n"
        )
        env = dict(os.environ)
        env["CRM_DATA_DIR"] = tempfile.mkdtemp(prefix="nc180_dd_")
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, timeout=120, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("0 "),
                        "importing gmail_accounts dragged Google in: " + out.stdout)

    def test_and_gmail_import_really_would_have(self):
        """The control. Without it the test above passes on a module that simply
        does not exist yet, and proves nothing about the cost it avoids."""
        import subprocess
        script = (
            "import sys, os\n"
            f"sys.path.insert(0, {os.path.join(REPO, 'crm')!r})\n"
            "import gmail_import\n"
            "google = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('google', 'google_auth_oauthlib', 'googleapiclient'))\n"
            "print(len(google))\n"
        )
        env = dict(os.environ)
        env["CRM_DATA_DIR"] = tempfile.mkdtemp(prefix="nc180_dd_")
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, timeout=120, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertGreater(int(out.stdout.strip()), 10,
                           "gmail_import no longer imports Google at module level "
                           "— if that is deliberate, the split above may be "
                           "unnecessary and this whole module can go")

    def test_there_is_one_parser_for_the_setting(self):
        """Two places reading GMAIL_ACCOUNTS is how they come to disagree. The
        rule is the same one _source_blockers() states: the module that makes
        the decision exports it, and nobody reimplements it."""
        import gmail_import
        self.assertIs(gmail_import.accounts_from_env,
                      gmail_accounts.accounts_from_env)


class TestTheDataPageSaysIt(Base):
    """Both lists on the Data page, because both render `blocked`."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="nc180_")
        self.creds = os.path.join(self.dir, "client.json")
        self._before = os.environ.get("GMAIL_ACCOUNTS")
        os.environ["GMAIL_ACCOUNTS"] = f"you@example.com:{self.creds}"
        self.addCleanup(
            lambda: os.environ.__setitem__("GMAIL_ACCOUNTS", self._before)
            if self._before is not None else os.environ.pop("GMAIL_ACCOUNTS", None))
        # See _Env.setUp: the sign-in half of readiness lives in the secrets dir,
        # so it has to be this temp folder and not the developer's own.
        _p = mock.patch.object(config, "SECRETS_DIR", self.dir)
        _p.start()
        self.addCleanup(_p.stop)

    def _rows(self):
        a = {s["key"]: s for s in
             api_import.handle_import_sources(self.conn)["sources"]}
        b = {r["key"]: r for r in api_sources.handle_sources(self.conn)["sources"]}
        return a["gmail"]["blocked"], b["gmail"]["blocked"]

    def test_the_missing_file_reaches_both_lists(self):
        row, ledger = self._rows()
        self.assertIn("client.json", row)
        self.assertEqual(row, ledger,
                         "the two lists on one screen disagree about Gmail")

    def test_and_goes_away_once_the_file_is_there(self):
        with open(self.creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        sign_in("you@example.com")
        self.assertEqual(self._rows(), ("", ""))

    def test_configured_alone_never_meant_ready(self):
        """`configured` is what the row used to decide on, and it is true here —
        which is the whole reason this state was invisible."""
        rows = {s["key"]: s for s in
                api_import.handle_import_sources(self.conn)["sources"]}
        self.assertTrue(rows["gmail"]["configured"])
        self.assertTrue(rows["gmail"]["blocked"])


class TestTheLoopStopsTrying(Base):
    """_autosync_pending, which is where the 12-hourly pointless subprocess was."""

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        for spec in app.API_SOURCES.values():
            os.environ[spec["switch"]] = "1"
        os.environ["LINKEDIN_TOKEN"] = "set-for-test"
        self.addCleanup(lambda: os.environ.pop("LINKEDIN_TOKEN", None))
        self.dir = tempfile.mkdtemp(prefix="nc180_")
        self.creds = os.path.join(self.dir, "client.json")
        self._before = os.environ.get("GMAIL_ACCOUNTS")
        os.environ["GMAIL_ACCOUNTS"] = f"you@example.com:{self.creds}"
        self.addCleanup(
            lambda: os.environ.__setitem__("GMAIL_ACCOUNTS", self._before)
            if self._before is not None else os.environ.pop("GMAIL_ACCOUNTS", None))
        # See _Env.setUp: the sign-in half of readiness lives in the secrets dir,
        # so it has to be this temp folder and not the developer's own.
        _p = mock.patch.object(config, "SECRETS_DIR", self.dir)
        _p.start()
        self.addCleanup(_p.stop)
        self._detect = importers.detect_sources
        importers.detect_sources = lambda: []
        self.addCleanup(lambda: setattr(importers, "detect_sources", self._detect))

    def _queued(self):
        return [f["source"] for f in app._autosync_pending(self.conn, False)]

    def test_gmail_is_not_queued_without_its_client_file(self):
        self.assertNotIn("gmail", self._queued())

    def test_and_is_queued_the_moment_the_file_appears(self):
        """The control, and the one that matters: this must not become a source
        that never runs."""
        with open(self.creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        sign_in("you@example.com")
        self.assertIn("gmail", self._queued())

    def test_but_not_before_anybody_has_signed_in(self):
        """NC-117. Queueing a never-signed-in account is worse than skipping it:
        the unattended run exits 4 and writes "the permission it was saved with
        was withdrawn or has expired" over a row whose permission never existed,
        sending the person to look for a revocation that never happened."""
        with open(self.creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertNotIn("gmail", self._queued())

    def test_the_other_api_source_is_untouched(self):
        """LinkedIn has one credential and no second half. A readiness check
        that started skipping it would be a new bug wearing this fix's clothes."""
        self.assertIn("linkedin", self._queued())

    def test_a_source_that_cannot_answer_is_attempted_anyway(self):
        """_not_ready is narrower than _source_blockers on purpose. A token that
        expired is not a missing piece of setup — the attempt is how it finds
        out it works again — so a probe blocker must not stop the attempt."""
        with open(self.creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        sign_in("you@example.com")
        with mock.patch.object(api_import, "_source_blockers",
                               lambda: {"gmail": "A probe says so.",
                                        "linkedin": "And about this one too."}):
            self.assertEqual(sorted(self._queued()), ["gmail", "linkedin"])

    def test_it_never_raises_its_way_into_skipping_everything(self):
        """Inventing a reason to skip a source is worse than missing one."""
        def boom():
            raise RuntimeError("no")
        with mock.patch.object(gmail_accounts, "missing_credentials", boom):
            with open(self.creds, "w", encoding="utf-8") as fh:
                fh.write("{}")
            self.assertIn("gmail", self._queued())


if __name__ == "__main__":
    unittest.main()
