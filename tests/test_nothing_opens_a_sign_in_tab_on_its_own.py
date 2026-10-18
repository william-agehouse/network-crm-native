"""NC-177. A browser window that appears on its own is not a small bug.

The Gmail importer runs on a 12-hour timer whenever GMAIL_ACCOUNTS is set, and
it used to run the same command the button runs. So whenever the saved Google
permission had been withdrawn — which is exactly what somebody does when they no
longer trust an app — crm/gmail_import.py re-entered the consent handshake and
called `flow.run_local_server(port=0, open_browser=True)`. A Google sign-in tab
would open on the screen, twice a day, from a thread nobody started, asking for
the access that had just been taken away. Nothing would have reported it: the
loop discards a child's output entirely, so there was no trace of it anywhere in
the app.

Two gates now stop it, and this file proves each one works ALONE, because that
is the only thing that makes two gates worth having:

* `--no-browser` in the arguments — the explicit one, visible in the argv,
  passed by crm/importers.py::_cmd_gmail when the form says background.
* `NC_UNATTENDED=1` in the environment — the backstop, set by
  crm/api_import.py::run_autosync on every child it spawns, so an importer
  added later that decides to prompt cannot open a tab from a timer either.

The name is the third thing this file protects. Reusing `NC_NO_BROWSER` for the
backstop would be wrong: it already exists and means "the native window is
showing the UI, so the server must not also open a browser tab" — and the
packaged app sets it ON THE ENGINE. Every child inherits the engine's
environment, so in the shipped .app, and only there, pressing Run import on a
Gmail account that needed signing in would refuse instead. Every other test
would stay green; TestTheShippedAppCanStillSignIn is what catches it.

(Until 2026-10-17, NC-330, the packaged launcher script exported it. The app's own
program now starts the engine and sets it in `EngineLaunch.environment`, in
native/Sources/Engine.swift; the guard below looks there.)

And a refusal that says nothing is a different bug, so the third half of this is
that the refusal reaches the Data page: the child exits 4, the loop writes a
sentence, and /api/import/sources serves it on the same `blocked` key NC-076
built for "this source cannot collect right now".
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, db, importers,
)
import api_import  # noqa: E402
import gmail_import  # noqa: E402
import config  # noqa: E402
import gmail_accounts  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeCreds:
    """Stands in for a google.oauth2 Credentials object."""

    def __init__(self, valid=True, expired=False, refresh_token="rt",
                 refresh_raises=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._raises = refresh_raises
        self.refreshed = False

    def refresh(self, _request):
        if self._raises is not None:
            raise self._raises
        self.refreshed = True
        self.valid = True

    def to_json(self):
        return '{"token": "fake"}'


class _FlowSpy:
    """Records whether anything tried to start the interactive consent flow.

    This is the whole point of the file. A test that only checks an exception was
    raised proves nothing about the browser — the browser is opened several
    frames deeper, by run_local_server, and an exception raised AFTER that call
    would look identical from outside.
    """

    def __init__(self):
        self.built_from = []
        self.served = []

    def from_client_secrets_file(self, path, scopes):
        self.built_from.append((path, list(scopes)))
        return self

    def run_local_server(self, **kwargs):
        self.served.append(kwargs)
        return _FakeCreds()

    @property
    def touched(self):
        return bool(self.built_from or self.served)


class _AuthCase(unittest.TestCase):
    """Puts a fake credentials file and a controllable token file in a temp dir,
    and swaps out everything in gmail_import that would talk to Google."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc177_")
        self.creds_file = os.path.join(self.dir, "gmail_credentials_test.json")
        with open(self.creds_file, "w", encoding="utf-8") as fh:
            json.dump({"installed": {"client_id": "x", "client_secret": "y"}}, fh)
        self.token_file = os.path.join(self.dir, "token.json")
        self.flow = _FlowSpy()
        self.stored = None          # what Credentials.from_authorized_user_file returns

        self._patches = [
            mock.patch.object(gmail_import, "InstalledAppFlow", self.flow),
            mock.patch.object(gmail_import, "build",
                              lambda *a, **kw: "a-gmail-service"),
            mock.patch.object(gmail_import, "_token_path",
                              lambda _email: self.token_file),
            mock.patch.object(gmail_import.Credentials, "from_authorized_user_file",
                              staticmethod(lambda *a, **kw: self.stored)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        for var in ("NC_UNATTENDED",):
            self._restore(var)

    def _restore(self, var):
        before = os.environ.get(var)
        self.addCleanup(
            lambda: os.environ.__setitem__(var, before) if before is not None
            else os.environ.pop(var, None))
        os.environ.pop(var, None)

    def _have_a_token(self, creds):
        """Put a token file on disk that from_authorized_user_file will return."""
        self.stored = creds
        with open(self.token_file, "w", encoding="utf-8") as fh:
            fh.write('{"token": "stored"}')


class TestTheGateItself(unittest.TestCase):
    """browser_allowed() is one function and both gates run through it."""

    def setUp(self):
        before = os.environ.get("NC_UNATTENDED")
        self.addCleanup(
            lambda: os.environ.__setitem__("NC_UNATTENDED", before) if before is not None
            else os.environ.pop("NC_UNATTENDED", None))
        os.environ.pop("NC_UNATTENDED", None)

    def test_a_plain_run_may_open_a_browser(self):
        """The control. Without it every assertion below is satisfied by a
        function that returns False forever, which would break the button."""
        self.assertTrue(gmail_import.browser_allowed([]))
        self.assertTrue(gmail_import.browser_allowed(["--scan", "you@example.com"]))

    def test_the_flag_alone_closes_it(self):
        self.assertFalse(gmail_import.browser_allowed(["--no-browser"]))
        self.assertFalse(gmail_import.browser_allowed(["--scan", "--no-browser"]))

    def test_the_environment_alone_closes_it(self):
        os.environ["NC_UNATTENDED"] = "1"
        self.assertFalse(gmail_import.browser_allowed([]),
                         "the backstop must hold with no flag in the argv")

    def test_the_environment_reads_words_as_well_as_one(self):
        for yes in ("1", "true", "TRUE", "yes", " 1 "):
            os.environ["NC_UNATTENDED"] = yes
            self.assertFalse(gmail_import.browser_allowed([]), yes)
        for no in ("0", "", "no", "false"):
            os.environ["NC_UNATTENDED"] = no
            self.assertTrue(gmail_import.browser_allowed([]), no)

    def test_it_reads_the_real_argv_when_given_nothing(self):
        """The three get_service() call sites in gmail_import.py pass nothing, so
        the default path — off sys.argv — is the one that actually protects the
        app. A gate that only worked when a test handed it a list would be a gate
        that never ran in production."""
        with mock.patch.object(gmail_import.sys, "argv",
                               ["gmail_import.py", "--scan", "--no-browser"]):
            self.assertFalse(gmail_import.browser_allowed())
        with mock.patch.object(gmail_import.sys, "argv", ["gmail_import.py", "--scan"]):
            self.assertTrue(gmail_import.browser_allowed())


class TestNoTokenAtAll(_AuthCase):
    """The first-run case, and the case after somebody deletes the token file."""

    def test_a_run_nobody_started_refuses_instead_of_opening_a_tab(self):
        with self.assertRaises(gmail_import.NeedsSignIn) as caught:
            gmail_import.get_service("you@example.com", self.creds_file,
                                     allow_browser=False)
        self.assertFalse(self.flow.touched,
                         "the consent flow was started — a tab would have opened")
        self.assertIn("you@example.com", str(caught.exception))
        self.assertIn("sign in", str(caught.exception).lower())

    def test_the_environment_alone_refuses_too(self):
        """No flag anywhere near this call. Only the backstop."""
        os.environ["NC_UNATTENDED"] = "1"
        with mock.patch.object(gmail_import.sys, "argv", ["gmail_import.py"]):
            with self.assertRaises(gmail_import.NeedsSignIn):
                gmail_import.get_service("you@example.com", self.creds_file)
        self.assertFalse(self.flow.touched)

    def test_the_flag_alone_refuses_too(self):
        """No environment variable anywhere near this call. Only the flag."""
        self.assertIsNone(os.environ.get("NC_UNATTENDED"))
        with mock.patch.object(gmail_import.sys, "argv",
                               ["gmail_import.py", "--no-browser"]):
            with self.assertRaises(gmail_import.NeedsSignIn):
                gmail_import.get_service("you@example.com", self.creds_file)
        self.assertFalse(self.flow.touched)

    def test_a_run_a_person_started_still_opens_the_tab(self):
        """The control, and the one that matters most: the fix must not quietly
        break the button. A person who clicks Run import is asking for the Google
        tab, and it still has to appear."""
        with mock.patch.object(gmail_import.sys, "argv", ["gmail_import.py", "--scan"]):
            svc = gmail_import.get_service("you@example.com", self.creds_file)
        self.assertEqual(svc, "a-gmail-service")
        self.assertEqual(self.flow.built_from, [(self.creds_file, gmail_import.SCOPES)])
        self.assertEqual(self.flow.served, [{"port": 0, "open_browser": True}])
        self.assertTrue(os.path.exists(self.token_file), "the new token was not saved")


class TestAWithdrawnPermission(_AuthCase):
    """The revoked-access case: somebody revoked this app at
    myaccount.google.com/permissions. The saved refresh token is dead and
    refresh() raises. Falling through to the consent flow would make withdrawing
    access make the app ask for it again, on a timer."""

    def _dead_token(self):
        self._have_a_token(_FakeCreds(
            valid=False, expired=True, refresh_token="dead",
            refresh_raises=gmail_import.RefreshError("invalid_grant")))

    def test_the_background_run_refuses_and_opens_nothing(self):
        self._dead_token()
        with self.assertRaises(gmail_import.NeedsSignIn) as caught:
            gmail_import.get_service("you@example.com", self.creds_file,
                                     allow_browser=False)
        self.assertFalse(self.flow.touched,
                         "revoking access made the app ask for it again")
        self.assertIn("withdrawn", str(caught.exception))

    def test_a_person_who_clicks_is_still_taken_to_google(self):
        """Same dead token, opposite answer. This is how somebody fixes it."""
        self._dead_token()
        with mock.patch.object(gmail_import.sys, "argv", ["gmail_import.py"]):
            svc = gmail_import.get_service("you@example.com", self.creds_file)
        self.assertEqual(svc, "a-gmail-service")
        self.assertEqual(self.flow.served, [{"port": 0, "open_browser": True}])


class TestAHealthyTokenIsUntouched(_AuthCase):
    """The ordinary case — 364 days out of 365. Nothing must change for it."""

    def test_a_valid_token_needs_no_flow_and_no_permission(self):
        self._have_a_token(_FakeCreds(valid=True))
        for allow in (True, False):
            svc = gmail_import.get_service("you@example.com", self.creds_file,
                                           allow_browser=allow)
            self.assertEqual(svc, "a-gmail-service")
            self.assertFalse(self.flow.touched)

    def test_an_expired_token_that_can_refresh_itself_still_does(self):
        """This is the whole reason the token file exists: an access token dies
        every hour and renews itself silently. A gate that stopped this would
        make the background sync useless rather than safe."""
        creds = _FakeCreds(valid=False, expired=True, refresh_token="alive")
        self._have_a_token(creds)
        svc = gmail_import.get_service("you@example.com", self.creds_file,
                                       allow_browser=False)
        self.assertEqual(svc, "a-gmail-service")
        self.assertTrue(creds.refreshed)
        self.assertFalse(self.flow.touched)


class TestTheShippedAppCanStillSignIn(_AuthCase):
    """The packaged .app runs its engine with NC_NO_BROWSER=1 in the environment
    (native/Sources/Engine.swift, EngineLaunch.environment — it means "the native
    window is showing the UI, don't also pop a browser tab"; before NC-330 the
    launcher script set it). Every importer subprocess inherits that.

    So a gate keyed on NC_NO_BROWSER would be ON for every run in the shipped
    app, including the one a person started by pressing a button — and the only
    way to fix a Gmail account would have stopped working, on the shipped build
    alone. The dev server sets it too, so nothing in the suite or on a preview
    port would have shown it."""

    def test_the_window_flag_is_not_the_unattended_flag(self):
        before = os.environ.get("NC_NO_BROWSER")
        os.environ["NC_NO_BROWSER"] = "1"
        self.addCleanup(
            lambda: os.environ.__setitem__("NC_NO_BROWSER", before) if before is not None
            else os.environ.pop("NC_NO_BROWSER", None))
        with mock.patch.object(gmail_import.sys, "argv", ["gmail_import.py", "--scan"]):
            self.assertTrue(gmail_import.browser_allowed(),
                            "the shipped app can no longer sign in to Google")
            svc = gmail_import.get_service("you@example.com", self.creds_file)
        self.assertEqual(svc, "a-gmail-service")
        self.assertEqual(self.flow.served, [{"port": 0, "open_browser": True}])

    def test_the_app_really_does_hand_it_to_the_engine(self):
        """If this ever stops being true the test above is guarding nothing —
        and someone may reasonably fold the two names back together.

        Moved 2026-10-17 (NC-330): this used to read the launcher script's
        `export NC_NO_BROWSER=1`. The app's program now starts the engine, so the
        same line is `env["NC_NO_BROWSER"] = "1"` in EngineLaunch.environment."""
        swift = os.path.join(REPO, "native", "Sources", "Engine.swift")
        with open(swift, encoding="utf-8") as fh:
            src = fh.read()
        environment = src[src.index("func environment(code: URL)"):]
        environment = environment[:environment.index("\n    }\n")]
        self.assertRegex(environment, r'env\["NC_NO_BROWSER"\]\s*=\s*"1"')


class TestTheScheduledCommand(unittest.TestCase):
    """crm/importers.py — what the timer actually runs."""

    def test_a_background_form_carries_the_flag(self):
        cmd = importers.REGISTRY["gmail"].build_cmd({"path": "", "background": True})
        self.assertIn("--no-browser", cmd)

    def test_a_clicked_form_does_not(self):
        """The button builds its argv from the HTTP body, which has no
        `background` key. If this ever picked the flag up, clicking Run import on
        a revoked account would refuse instead of signing in — and there would be
        no way back."""
        cmd = importers.REGISTRY["gmail"].build_cmd({"path": ""})
        self.assertNotIn("--no-browser", cmd)
        cmd = importers.REGISTRY["gmail"].build_cmd({"path": "", "background": False})
        self.assertNotIn("--no-browser", cmd)


class TestTheLoopMarksTheForm(Base):
    """crm/api_import.py::_autosync_pending — every API source is a run nobody
    started, so every one of them says so."""

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        self._env = {}
        for spec in app.API_SOURCES.values():
            os.environ[spec["switch"]] = "1"
            if spec.get("needs"):
                self._env[spec["needs"]] = os.environ.get(spec["needs"])
                os.environ[spec["needs"]] = "set-for-test"
        self._detect = importers.detect_sources
        importers.detect_sources = lambda: []

    def tearDown(self):
        importers.detect_sources = self._detect
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_every_api_source_is_marked_as_a_run_nobody_started(self):
        pending = app._autosync_pending(self.conn)
        self.assertTrue(pending)
        for f in pending:
            self.assertTrue(f["form"].get("background"),
                            f"{f['source']} was queued without saying nobody started it")

    def test_and_that_reaches_the_argv(self):
        """The two halves joined up — the form says background, and the command
        the loop would run carries the flag. Either half alone is a fact about a
        dictionary, not about a browser."""
        pending = app._autosync_pending(self.conn)
        gmail = [f for f in pending if f["source"] == "gmail"]
        self.assertEqual(len(gmail), 1)
        cmd = importers.REGISTRY["gmail"].build_cmd(gmail[0]["form"])
        self.assertIn("--no-browser", cmd)


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class TestTheLoopsChildEnvironment(Base):
    """The backstop, at the place that sets it."""

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        os.environ["NC_GMAIL_AUTO"] = "1"
        # NC-180: an account with no client file from Google is not attempted at
        # all any more, so these tests — which are about what happens when it IS
        # attempted — have to hand it one. A stub is enough; nothing here reads
        # its contents, only whether the file is there.
        _sec = tempfile.mkdtemp(prefix="nc177_creds_")
        creds = os.path.join(_sec, "client.json")
        with open(creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.environ["GMAIL_ACCOUNTS"] = f"you@example.com:{creds}"
        # NC-117: and a saved sign-in. A configured account with a file and no
        # token is its own not-ready state now — the one every install passes
        # through on the way in — so a test that means "attempted" has to say
        # both halves. SECRETS_DIR is redirected because that is where the token
        # file lives, and reading the developer's own would make this pass or
        # fail for a reason unrelated to the code.
        _p = mock.patch.object(config, "SECRETS_DIR", _sec)
        _p.start()
        self.addCleanup(_p.stop)
        with open(gmail_accounts.token_path("you@example.com"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        self.addCleanup(lambda: os.environ.pop("GMAIL_ACCOUNTS", None))
        self._detect = importers.detect_sources
        importers.detect_sources = lambda: []
        self.addCleanup(lambda: setattr(importers, "detect_sources", self._detect))
        self.calls = []

        orig_run, orig_refresh, orig_path = (app.subprocess.run,
                                             app.engagement.refresh, db.DB_PATH)

        def _fake_run(cmd, **kw):
            self.calls.append((list(cmd), dict(kw.get("env") or {})))
            return _Result(self.exit_code)

        app.subprocess.run = _fake_run
        app.engagement.refresh = lambda conn: None
        db.DB_PATH = self.path
        self.addCleanup(lambda: (setattr(app, "subprocess", app.subprocess),
                                 setattr(app.subprocess, "run", orig_run),
                                 setattr(app.engagement, "refresh", orig_refresh),
                                 setattr(db, "DB_PATH", orig_path)))
        self.exit_code = 0

    def _gmail_call(self):
        for cmd, env in self.calls:
            if any("gmail_import.py" in str(a) for a in cmd):
                return cmd, env
        return None, None

    def test_the_child_is_told_not_to_open_a_browser(self):
        app.run_autosync()
        cmd, env = self._gmail_call()
        self.assertIsNotNone(cmd, f"gmail never ran: {self.calls}")
        self.assertEqual(env.get("NC_UNATTENDED"), "1")
        self.assertIn("--no-browser", cmd)

    def test_the_server_itself_does_not_catch_it(self):
        """A copy is modified, never os.environ. If the loop set it on the real
        environment, the NEXT interactive run — the button, in the same process —
        would inherit it and refuse to sign in, which is the failure this fix
        must not cause."""
        before = os.environ.get("NC_UNATTENDED")
        app.run_autosync()
        self.assertEqual(os.environ.get("NC_UNATTENDED"), before)
        self.assertIsNone(before)


class TestTheRefusalReachesTheScreen(Base):
    """A background run that stops silently is the same bug wearing a hat."""

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        os.environ["NC_GMAIL_AUTO"] = "1"
        # NC-180: an account with no client file from Google is not attempted at
        # all any more, so these tests — which are about what happens when it IS
        # attempted — have to hand it one. A stub is enough; nothing here reads
        # its contents, only whether the file is there.
        _sec = tempfile.mkdtemp(prefix="nc177_creds_")
        creds = os.path.join(_sec, "client.json")
        with open(creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.environ["GMAIL_ACCOUNTS"] = f"you@example.com:{creds}"
        # NC-117: and a saved sign-in. A configured account with a file and no
        # token is its own not-ready state now — the one every install passes
        # through on the way in — so a test that means "attempted" has to say
        # both halves. SECRETS_DIR is redirected because that is where the token
        # file lives, and reading the developer's own would make this pass or
        # fail for a reason unrelated to the code.
        _p = mock.patch.object(config, "SECRETS_DIR", _sec)
        _p.start()
        self.addCleanup(_p.stop)
        with open(gmail_accounts.token_path("you@example.com"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        self.addCleanup(lambda: os.environ.pop("GMAIL_ACCOUNTS", None))
        self._detect = importers.detect_sources
        importers.detect_sources = lambda: []
        self.addCleanup(lambda: setattr(importers, "detect_sources", self._detect))

        orig_run, orig_refresh, orig_path = (app.subprocess.run,
                                             app.engagement.refresh, db.DB_PATH)
        self.exit_code = importers.EXIT_NEEDS_SIGN_IN
        app.subprocess.run = lambda cmd, **kw: _Result(self.exit_code)
        app.engagement.refresh = lambda conn: None
        db.DB_PATH = self.path
        self.addCleanup(lambda: (setattr(app.subprocess, "run", orig_run),
                                 setattr(app.engagement, "refresh", orig_refresh),
                                 setattr(db, "DB_PATH", orig_path)))

    def _blocked_line(self):
        payload = api_import.handle_import_sources(self.conn)
        rows = {s["key"]: s for s in payload["sources"]}
        return rows["gmail"]["blocked"]

    def _ledger_line(self):
        """The OTHER list on the same screen — /api/sources, the 17-row ledger
        at the bottom of the Data page, which is where Gmail actually shows up
        (the row list above it only carries the sources a person has connected).
        static/app/sources.js:139 ranks `blocked` above every other sentence,
        so this is the string somebody really reads."""
        import api_sources
        rows = {r["key"]: r for r in api_sources.handle_sources(self.conn)["sources"]}
        return rows["gmail"]["blocked"]

    def test_before_anything_happens_the_row_says_nothing(self):
        self.assertEqual(self._blocked_line(), "")
        self.assertEqual(self._ledger_line(), "")

    def test_both_lists_on_the_page_say_the_same_thing(self):
        """A fix that fed the row endpoint only would leave the ledger underneath
        it saying "Has not run here yet" — the Data page contradicting itself in
        two places visible at once, which is the defect NC-076 already fixed
        once."""
        app.run_autosync()
        self.assertTrue(self._ledger_line(), "the ledger never heard about it")
        self.assertEqual(self._ledger_line(), self._blocked_line())

    def test_exit_four_puts_a_sentence_on_the_data_page(self):
        app.run_autosync()
        line = self._blocked_line()
        self.assertTrue(line, "the background run refused and said nothing")
        self.assertTrue(self._ledger_line())
        self.assertIn("Gmail", line)
        self.assertIn("sign in again", line)
        # It must say that nothing opened at them, because the worst outcome
        # here is a Google tab appearing by itself.
        self.assertIn("when you are ready", line)

    def test_an_ordinary_failure_says_nothing(self):
        """Exit 1 is a broken import — it waits out its period and tries again,
        exactly as before. Only the exit that cannot cure itself is news."""
        self.exit_code = 1
        app.run_autosync()
        self.assertEqual(self._blocked_line(), "")
        self.assertEqual(self._ledger_line(), "")

    def test_a_successful_run_clears_it(self):
        app.run_autosync()
        self.assertTrue(self._blocked_line())
        # The clock stops it running again inside 12 hours; clear the stamp so
        # the next tick really does spawn a child.
        db.state_set(self.conn, app.API_ATTEMPT_KEY + "gmail", "")
        self.conn.commit()
        self.exit_code = 0
        app.run_autosync()
        self.assertEqual(self._blocked_line(), "",
                         "a run that worked left the warning standing")
        self.assertEqual(self._ledger_line(), "")

    def test_the_button_clears_it_too(self):
        """The real cure. The person clicks Run import, the Google tab opens
        because they asked for it, the run exits 0 — and the line goes."""
        app.run_autosync()
        self.assertTrue(self._blocked_line())
        with mock.patch.object(db, "DB_PATH", self.path), \
             mock.patch.object(app.subprocess, "run",
                               lambda cmd, **kw: _Result(0, "Done. 0 new messages")), \
             mock.patch.object(app.engagement, "refresh", lambda conn: None):
            out = api_import.handle_import_run({"source": "gmail"})
        self.assertTrue(out["ok"])
        self.assertEqual(self._blocked_line(), "")

    def test_a_probe_blocker_still_shows_when_nothing_is_waiting(self):
        """Folding the sign-ins in must not push the probes out. Without this,
        emptying the probe half of source_blockers() breaks nothing any test can
        see, and LinkedIn's dead token goes quiet on both lists."""
        with mock.patch.object(api_import, "_source_blockers",
                               lambda: {"gmail": "A probe says so."}):
            self.assertEqual(self._blocked_line(), "A probe says so.")
            self.assertEqual(self._ledger_line(), "A probe says so.")

    def test_it_outranks_a_probe_blocker(self):
        """Both can be true at once. "Sign in again, here is the button" is the
        useful one; a generic "cannot collect" is not."""
        app.run_autosync()
        with mock.patch.object(api_import, "_source_blockers",
                               lambda: {"gmail": "Something else is wrong."}):
            self.assertIn("sign in again", self._blocked_line())


class TestTheRealScriptEndToEnd(unittest.TestCase):
    """No stubs. The actual file, run the way the timer runs it.

    This is the one that pins the number: the child exits with whatever
    importers.EXIT_NEEDS_SIGN_IN says, so the scheduler watching for that number
    and the child producing it cannot drift apart.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc177_e2e_")
        # Deliberately the WRONG SHAPE for an OAuth client file: it exists, so
        # the existence check passes, but Google's own parser rejects it. That is
        # what makes the control below finish — a well-formed stub would send
        # run_local_server off to bind a port and block forever waiting for a
        # redirect that never comes, which is the bug itself (timeout_seconds is
        # None) and takes the test suite down with it.
        self.creds = os.path.join(self.dir, "creds.json")
        with open(self.creds, "w", encoding="utf-8") as fh:
            json.dump({"this-is-not": "an installed-app client"}, fh)

    def _run(self, *extra):
        env = dict(os.environ)
        env["CRM_DATA_DIR"] = os.path.join(self.dir, "data")
        env["GMAIL_ACCOUNTS"] = f"you@example.com:{self.creds}"
        env.pop("NC_UNATTENDED", None)
        # If anything did try to open a browser, this is what it would run — and
        # the marker file would exist afterwards. POSIX webbrowser honours it.
        marker = os.path.join(self.dir, "a-tab-was-opened")
        opener = os.path.join(self.dir, "opener.sh")
        with open(opener, "w", encoding="utf-8") as fh:
            fh.write(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(opener, 0o755)
        env["BROWSER"] = opener + " %s"
        proc = subprocess.run(
            [sys.executable, "gmail_import.py", *extra],
            cwd=os.path.join(REPO, "crm"), env=env, capture_output=True,
            text=True, timeout=60)
        return proc, os.path.exists(marker)

    def test_no_browser_exits_with_the_code_the_scheduler_watches_for(self):
        proc, opened = self._run("--no-browser")
        self.assertEqual(proc.returncode, importers.EXIT_NEEDS_SIGN_IN,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertFalse(opened, "a browser was launched")
        self.assertIn("sign in to Google again", proc.stdout)
        self.assertIn("Data page", proc.stdout)
        # It refused BEFORE reading the client file — no parser error anywhere.
        self.assertNotIn("client", proc.stderr.lower())

    def test_without_the_flag_the_same_run_goes_somewhere_else(self):
        """The control. Without it, a script that exited 4 unconditionally would
        satisfy the test above. The credentials file here is a stub pointing at
        example.invalid, so the interactive path cannot complete — but it must
        fail as an interactive path, not as a refusal."""
        proc, _ = self._run()
        self.assertNotEqual(proc.returncode, importers.EXIT_NEEDS_SIGN_IN,
                            f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("sign in to Google again", proc.stdout)
        # It got as far as handing the file to Google's own parser, which is
        # several frames past where the flagged run stopped.
        self.assertIn("client", proc.stderr.lower())


if __name__ == "__main__":
    unittest.main()
