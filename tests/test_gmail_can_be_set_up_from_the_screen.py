"""NC-117. Gmail's own instructions told you to click a button that did not exist.

The help text on the Gmail row said, verbatim: "2. Click **Connect** below — a
browser tab opens for Google sign-in." There was no Connect control in the front
end and there never had been. The same text named
`secrets/gmail_credentials_<slug>.json` — a folder with no button to open it,
under a name derived by a regular expression that appears nowhere on any screen —
and pointed at a "README §Setup" that does not exist. Three instructions, none of
them followable, on the one source that cannot be set up any other way.

What this file gates is one sentence: **a person who has never seen
this repo can get Gmail connected using only what is on screen, and the row says
what it is waiting for while it waits.** Four claims, and they fail differently:

1. **The controls the words promise exist.** This is the original bug stated
   generally, and it is gated generally: every control the on-screen text names
   in bold has to be a control the panel renders. Checking only for the word
   "Connect" would gate the one instance and let the next one through.
2. **The three steps can be finished through the app.** Address, then file, then
   sign-in, each through an endpoint the panel calls — driven here end to end
   against the real handlers, with nothing hand-placed in the secrets folder.
3. **It says what it is waiting for while it waits.** Both while a Google tab is
   open, and — the part that survives the row being folded shut — as a blocker on
   the source itself, so the page's "No source needs you now." cannot print over
   an install that has never signed in.
4. **The steps say to publish the app, not to add a test user.** This is the one
   that would fail silently and slowly. A Google project left on "Testing"
   publishing status hands out permissions Google deletes after seven days, so
   the obvious-looking advice produces an app that works for a week and then asks
   for a sign-in every Monday forever. Google's own OAuth documentation
   states the seven-day rule.
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


def read(name):
    with open(os.path.join(APP, name), encoding="utf-8") as fh:
        return fh.read()


def client_file(path, kind="installed"):
    """A stub of what Google hands you: the top-level key is the whole shape."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({kind: {"client_id": "x.apps.googleusercontent.com",
                          "client_secret": "y"}}, fh)


class _Sandbox(Base):
    """A secrets folder of its own, and no Gmail account configured yet."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="nc117_")
        self.downloads = tempfile.mkdtemp(prefix="nc117_dl_")
        patch = mock.patch.object(config, "SECRETS_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        self._before = os.environ.get("GMAIL_ACCOUNTS")
        self.addCleanup(self._restore)
        os.environ.pop("GMAIL_ACCOUNTS", None)
        api_gmail.reset_for_test()
        self.addCleanup(api_gmail.reset_for_test)

    def _restore(self):
        if self._before is None:
            os.environ.pop("GMAIL_ACCOUNTS", None)
        else:
            os.environ["GMAIL_ACCOUNTS"] = self._before

    def configure(self, *emails):
        os.environ["GMAIL_ACCOUNTS"] = ",".join(emails)


# ── 1. The words name controls that exist ────────────────────────────────────

class TestEveryControlTheWordsNameIsOnScreen(unittest.TestCase):
    """The original bug, gated generally rather than by its own spelling.

    "Click **Connect** below" was wrong because no Connect existed. Gating the
    string "Connect" would catch that sentence and nothing else. So this reads
    every control the panel's own prose names in bold and checks the panel
    renders a control by that name — which is the rule the old text broke, and
    the rule any replacement for it has to keep.
    """

    def setUp(self):
        self.js = read("gmail.js")

    def rendered_controls(self):
        """The visible label of every <button> the panel renders."""
        out = set()
        for m in re.finditer(r"<button\b[^>]*>(.*?)</button>", self.js, re.S):
            text = re.sub(r"<\$\{[^}]*\}[^>]*/>", " ", m.group(1))   # <${Icon} …/>
            text = re.sub(r"\$\{[^{}]*\}", " ", text)                # ${expr}
            for part in re.split(r"[:?]", text):
                part = " ".join(part.split()).strip(' "\'…')
                if part:
                    out.add(part.lower())
        return out

    def named_in_prose(self):
        """Every <b>…</b> inside the panel's prose that reads as a control here.

        Google's own screens are named in bold too — "Desktop app", "Branding",
        "Publish app" — and those are not this app's controls. They are told
        apart by the verb in front of them: this panel says "press the button
        below", Google's steps say "open", "choose", "press <b>Create</b>" about
        a page that is not ours. So the test asks only about the handful of
        words that would be OUR controls, which is exactly the set the broken
        sentence drew from.
        """
        ours = {"connect", "sign in", "choose file", "choose file…", "add",
                "choose the file", "open google cloud console", "retry",
                "try again", "remove"}
        found = set()
        for m in re.finditer(r"<b>(.*?)</b>", self.js, re.S):
            word = " ".join(m.group(1).split()).strip(" .,—").lower()
            if word in ours:
                found.add(word)
        return found

    def test_the_panel_renders_a_control_for_every_one_it_names(self):
        controls = self.rendered_controls()
        self.assertTrue(controls, "read no buttons out of gmail.js at all")
        for word in sorted(self.named_in_prose()):
            with self.subTest(named=word):
                self.assertTrue(
                    any(word.rstrip("…") in c for c in controls),
                    f'the panel tells somebody to use "{word}" and renders no '
                    f"control with that name. Rendered: {sorted(controls)}. "
                    f"This is exactly NC-117: the help text said Click "
                    f"**Connect** below, and no Connect existed anywhere")

    def test_the_help_text_no_longer_names_a_control_that_is_not_there(self):
        """The row's own help text, which is where the broken sentence lived."""
        help_text = importers.REGISTRY["gmail"].export_help
        self.assertNotIn("Click **Connect**", help_text)
        self.assertNotIn("README §Setup", help_text,
                         "there is no §Setup heading in README.md — this is the "
                         "third of the three unfollowable instructions")
        readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
        self.assertNotIn("(per-account OAuth — no file)", readme,
                         "the README said Gmail needs no file. It needs the one "
                         "file the panel walks you through getting")

    def test_the_panel_is_reachable_from_the_data_page(self):
        """A panel nobody renders is the same as no panel."""
        data = read("data.js")
        self.assertIn('from "./gmail.js"', data)
        self.assertRegex(data, r"case \"gmail\":\s*return html`[^`]*<\$\{GmailSetup\}")
        with open(os.path.join(REPO, "crm", "api_ledger.py"), encoding="utf-8") as fh:
            ledger = fh.read()
        at = ledger.index("def _row_gmail(")
        self.assertIn('[{"kind": "gmail"}]', ledger[at:at + 1500],
                      "the Gmail row no longer sends the act that draws the panel")


# ── 4. The seven-day trap ────────────────────────────────────────────────────

class TestTheStepsDoNotSetUpAWeeklyNag(unittest.TestCase):
    """The failure that arrives a week late.

    Google deletes a permission issued by a project whose publishing status is
    still "Testing" after seven days. The obvious version of these instructions
    — "add yourself as a test user" — therefore produces an app that collects
    fine for a week and then asks for a sign-in every Monday forever, with
    nothing connecting that to a radio button pressed once in a console.

    The right instruction is Publish app / In production. This gates that it is
    still being given, because it is invisible in any test that runs in under
    seven days, including every other test in this repo.
    """

    def setUp(self):
        self.js = read("gmail.js")
        self.steps = self.js[self.js.index("const STEPS = ["):self.js.index("const UNVERIFIED_NOTE")]

    def test_the_steps_say_to_publish(self):
        self.assertIn("Publish app", self.steps)
        self.assertIn("In production", self.js)

    def test_the_steps_say_what_leaving_it_on_testing_costs(self):
        low = self.steps.lower()
        self.assertIn("seven days", low)
        self.assertIn("testing", low)

    def test_the_steps_do_not_tell_anybody_to_stay_a_test_user(self):
        """Being on the test-user list is the Testing-status path. It works,
        until it silently stops working, which is the worst kind."""
        self.assertNotIn("test user", self.steps.lower())

    def test_the_answer_is_also_where_the_weekly_nag_sends_you(self):
        """The person who needs this most already has their file, so a steps
        block that only appears while the file is missing would never reach
        them. It renders whenever an address does."""
        self.assertIn("rows.length > 0 && html`", self.js)
        self.assertIn("Signing in again every week?", self.js)


# ── 2. The three steps can be finished through the app ───────────────────────

class TestSetupRunsEndToEndThroughTheEndpoints(_Sandbox):
    """Address, then file, then sign-in — nothing placed by hand.

    The test drives the same three endpoints the panel's buttons call, in the
    order the panel calls them, and reads /api/gmail/status between each one.
    The status is what the screen shows, so if the sequence works here the
    screen can be finished; before NC-117 the second step had no endpoint at all
    and the third had no control.
    """

    def test_nothing_configured_says_so_and_asks_for_nothing_else(self):
        st = api_gmail.status()
        self.assertEqual(st["accounts"], [])
        self.assertFalse(st["configured"])
        self.assertEqual(st["needs_file"], [])
        self.assertEqual(st["needs_sign_in"], [])

    def test_an_address_alone_asks_for_the_file_and_names_it(self):
        self.configure("you@example.com")
        st = api_gmail.status()
        self.assertEqual(st["needs_file"], ["you@example.com"])
        self.assertEqual(st["accounts"][0]["credentials_file"],
                         "gmail_credentials_youexamplecom.json")
        self.assertNotIn("/", st["accounts"][0]["credentials_file"],
                         "the row shows a name, not somebody's home directory")

    def test_the_picked_file_is_filed_under_the_name_the_importer_looks_for(self):
        """The step that had no endpoint. The person downloads
        client_secret_1234-abcd.apps.googleusercontent.com.json and this puts it
        where gmail_import will look, under the name the slug rule derives —
        a rule stated on no screen, which is why the app applies it."""
        self.configure("you@example.com")
        picked = os.path.join(self.downloads,
                              "client_secret_1234-abcd.apps.googleusercontent.com.json")
        client_file(picked)
        api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                            "path": picked})
        landed = gmail_accounts.accounts_from_env()["you@example.com"]
        self.assertTrue(os.path.exists(landed))
        self.assertEqual(os.path.basename(landed),
                         "gmail_credentials_youexamplecom.json")
        self.assertEqual(api_gmail.status()["needs_file"], [])
        self.assertEqual(api_gmail.status()["needs_sign_in"], ["you@example.com"])

    def test_the_filed_copy_is_owner_only(self):
        """It holds a client secret. The repo's rule for everything in there."""
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        os.chmod(picked, 0o644)
        api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                            "path": picked})
        landed = gmail_accounts.accounts_from_env()["you@example.com"]
        self.assertEqual(os.stat(landed).st_mode & 0o077, 0,
                         "group or other can read the client secret")

    def test_a_web_client_is_refused_now_instead_of_by_google_later(self):
        """The commonest wrong turn. Picking "Web application" in the console
        produces a file that saves fine and fails at sign-in with an error about
        a redirect address, several screens and one wasted journey later."""
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_web.json")
        client_file(picked, kind="web")
        with self.assertRaises(Exception) as caught:
            api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                                "path": picked})
        said = str(caught.exception)
        self.assertIn("Desktop", said)
        self.assertFalse(os.path.exists(
            gmail_accounts.accounts_from_env()["you@example.com"]),
            "a file Google will refuse was stored anyway")

    def test_a_file_for_an_address_that_is_not_set_up_is_refused(self):
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        with self.assertRaises(Exception):
            api_gmail.handle_gmail_credentials({"email": "someone@else.example",
                                                "path": picked})

    def test_sign_in_will_not_start_before_there_is_a_file(self):
        """Pressing it anyway is the state the old instructions left people in.
        The refusal says which thing is missing, not that something failed."""
        self.configure("you@example.com")
        with self.assertRaises(Exception) as caught:
            api_gmail.handle_gmail_connect({"email": "you@example.com"})
        self.assertIn("no sign-in file", str(caught.exception))

    def test_sign_in_starts_and_returns_at_once(self):
        """The button returns immediately and the waiting happens on the page —
        the semantic install's shape. A handler that blocked for the length of a
        human's trip through Google's consent screens would time out the request
        and take the page with it."""
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                            "path": picked})
        import threading
        import time

        # The stand-in for a person on Google's consent screens: it starts, and
        # then it does not come back. A stub that returned immediately could not
        # tell a background thread from a blocking call — which is exactly how a
        # blocking handler could escape this gate.
        started = threading.Event()
        release = threading.Event()
        seen = {}

        def slow_run(account):
            seen["account"] = account
            started.set()
            release.wait(10)                # never actually opens a browser

        try:
            with mock.patch.object(api_gmail, "_run_connect", slow_run):
                began = time.monotonic()
                out = api_gmail.handle_gmail_connect({"email": "you@example.com"})
                elapsed = time.monotonic() - began
                self.assertTrue(started.wait(5), "the sign-in never started")
                self.assertLess(
                    elapsed, 1.0,
                    "handle_gmail_connect waited for the sign-in to finish. A "
                    "person's trip through Google's screens takes minutes; the "
                    "request would time out and take the page with it")
                self.assertTrue(out["started"])
                self.assertEqual(seen.get("account"), "you@example.com")
                # And while it is still out there, the status says so — this is
                # the "says what it is waiting for WHILE it waits" half.
                self.assertEqual(api_gmail.status()["connect"]["state"], "running")
        finally:
            release.set()

    def test_one_address_does_not_have_to_be_named(self):
        """With a single account there is nothing to disambiguate, and asking
        would be a question with one possible answer."""
        self.configure("only@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        api_gmail.handle_gmail_credentials({"email": "only@example.com",
                                            "path": picked})
        with mock.patch.object(api_gmail, "_run_connect", lambda a: None):
            api_gmail.handle_gmail_connect({})
        self.assertEqual(api_gmail.status()["connect"]["account"],
                         "only@example.com")


# ── 3. It says what it is waiting for ────────────────────────────────────────

class TestTheRowSaysWhatItIsWaitingFor(_Sandbox):
    """Both halves of "while it waits": the open row, and the shut one."""

    def test_while_a_google_tab_is_open_the_status_says_so(self):
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                            "path": picked})
        with mock.patch.object(api_gmail, "_run_connect", lambda a: None):
            api_gmail.handle_gmail_connect({"email": "you@example.com"})
        connect = api_gmail.status()["connect"]
        self.assertEqual(connect["state"], "running")
        self.assertTrue(connect["running"])
        self.assertEqual(connect["account"], "you@example.com")

    def test_a_second_press_does_not_start_a_second_tab(self):
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                            "path": picked})
        with mock.patch.object(api_gmail, "_run_connect", lambda a: None):
            api_gmail.handle_gmail_connect({"email": "you@example.com"})
            again = api_gmail.handle_gmail_connect({"email": "you@example.com"})
        self.assertFalse(again.get("started"))
        self.assertIn("Already waiting", again.get("message", ""))

    def test_a_never_signed_in_account_is_a_blocker_on_the_row_itself(self):
        """The half that survives the row being folded shut.

        A panel inside a source row does not render while the row is folded, so
        a person who has added an address, chosen the file and never pressed Sign
        in would otherwise see "No source needs you now." on a page where Gmail
        is silently doing nothing. The blocker is served on the source, so it
        reaches the verdict whether anybody opens the row or not.
        """
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                            "path": picked})
        rows = {s["key"]: s for s in
                api_import.handle_import_sources(self.conn)["sources"]}
        said = rows["gmail"]["blocked"]
        self.assertTrue(said, "the row said nothing about a setup it cannot use")
        self.assertIn("not signed in", said)
        self.assertIn("you@example.com", said)
        self.assertTrue(rows["gmail"]["configured"],
                        "still configured — that is why this state was invisible")

    def test_and_it_goes_away_once_the_sign_in_is_saved(self):
        self.configure("you@example.com")
        picked = os.path.join(self.downloads, "client_secret_x.json")
        client_file(picked)
        api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                            "path": picked})
        with open(gmail_accounts.token_path("you@example.com"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        rows = {s["key"]: s for s in
                api_import.handle_import_sources(self.conn)["sources"]}
        self.assertEqual(rows["gmail"]["blocked"], "")
        self.assertEqual(api_gmail.status()["ready"], ["you@example.com"])

    def test_the_panel_reports_upward_so_the_page_verdict_can_see_it(self):
        """NC-076's contract for a panel that holds its own status, and the
        cleanup that makes it honest: this panel unmounts when the row folds,
        and without the cleanup "needs you" would stay latched on for the rest
        of the session with nothing on screen to explain it."""
        js = read("gmail.js")
        self.assertIn("onAttention(asks)", js)
        self.assertIn("return () => onAttention(false)", js)
        # NC-236: the old Data page passed onAttention down from its source row.
        # The rebuilt page takes no report from the panel at all: the Gmail row's
        # own state and the "Needs you" list are built on the server from the
        # same status, so the verdict cannot disagree with the panel. What has
        # to hold is that the server row says so for each thing the panel asks.
        with open(os.path.join(REPO, "crm", "api_ledger.py"), encoding="utf-8") as fh:
            ledger = fh.read()
        at = ledger.index("def _row_gmail(")
        row = ledger[at:ledger.index("\ndef ", at + 10)]
        for asked in ('gm.get("needs_file")', 'gm.get("needs_sign_in")'):
            self.assertIn(asked, row,
                          "the panel asks for this and the row's state does not, "
                          "so the page can say nothing needs you beside a panel "
                          "that does")
        self.assertIn('"tone": "blocked"', row)


# ── The timeout is a number somebody can feel ────────────────────────────────

class TestHowLongItWaitsIsASetting(unittest.TestCase):
    """A person on a slow trip through Google's screens is not a fault, and the
    wait they are allowed is a number they can change rather than one buried in
    the source."""

    def setUp(self):
        self._before = os.environ.get("NC_GMAIL_CONNECT_TIMEOUT_SEC")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._before is None:
            os.environ.pop("NC_GMAIL_CONNECT_TIMEOUT_SEC", None)
        else:
            os.environ["NC_GMAIL_CONNECT_TIMEOUT_SEC"] = self._before

    def test_the_default_is_long_enough_for_a_first_visit(self):
        os.environ.pop("NC_GMAIL_CONNECT_TIMEOUT_SEC", None)
        self.assertGreaterEqual(api_gmail.connect_timeout_sec(), 300)

    def test_it_can_be_changed_without_a_restart(self):
        os.environ["NC_GMAIL_CONNECT_TIMEOUT_SEC"] = "900"
        self.assertEqual(api_gmail.connect_timeout_sec(), 900)

    def test_nonsense_falls_back_instead_of_crashing_the_page(self):
        os.environ["NC_GMAIL_CONNECT_TIMEOUT_SEC"] = "not a number"
        self.assertEqual(api_gmail.connect_timeout_sec(),
                         api_gmail.CONNECT_TIMEOUT_DEFAULT)

    def test_the_screen_says_the_number_rather_than_hiding_it(self):
        self.assertIn("connect_timeout_sec", read("gmail.js"))
        self.assertIn("minutes", read("gmail.js"))

    def test_the_key_is_written_down_where_the_page_says_it_is(self):
        """NC-184. It has no box, which is a defensible choice for a number you
        set once. Being findable nowhere but `crm/api_gmail.py` is not.

        crm/settings.py's own NC-003 comment calls itself "the single documented
        source of what each one does and where it is read, for whoever wants to
        hand-edit secrets/.env", and since NC-121 the Settings page says so on
        screen. A live tuning key missing from it makes that sentence false.

        Narrow on purpose: this test covers the one key. Other keys read across
        crm/ are in the same position, several of them money (NC_PRICE_IN_PER_MTOK)
        or wait time (the NC_FDA_ timeouts); tests/test_every_setting_is_accounted_for.py
        covers those.
        """
        with open(os.path.join(REPO, "crm", "settings.py"), encoding="utf-8") as fh:
            registry = fh.read()
        self.assertIn("NC_GMAIL_CONNECT_TIMEOUT_SEC", registry)

    def test_the_documented_default_is_the_real_one(self):
        """A default that drifts from the code is worse than none: it is a
        number somebody will trust without checking."""
        with open(os.path.join(REPO, "crm", "settings.py"), encoding="utf-8") as fh:
            registry = fh.read()
        i = registry.index("NC_GMAIL_CONNECT_TIMEOUT_SEC")
        para = registry[i:i + 700]
        self.assertIn(str(api_gmail.CONNECT_TIMEOUT_DEFAULT), para,
                      "settings.py names a default this key does not have")


if __name__ == "__main__":
    unittest.main()
