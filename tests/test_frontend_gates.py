"""Source-level gates for frontend invariants the Python suite cannot execute.

WHY THIS FILE EXISTS
--------------------
The frontend is vendored Preact with no build step, so `node --check` (the edit
hook) is the only automated gate — and it only proves the file parses. That is
how a change can make the People list completely unopenable by mouse while
every test stays green: the break is in DOM event routing, which no test here
executes.

These are deliberately narrow, and each one is tied to a specific failure mode.
A gate that merely encodes a preference does not belong here —
it will be worked around rather than fixed. If one of these ever blocks a change
that is genuinely correct, delete the gate and say why in the commit.
"""
import io
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402,F401 — Base is the CRM_DATA_DIR bootstrap
    Base, braced_block, js_code, js_statement, js_ternary_branch,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")


def _read_py(name):
    """A file from crm/, for gates that must check a server-side constant."""
    with open(os.path.join(REPO, "crm", name), encoding="utf-8") as fh:
        return fh.read()


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def _read_crm(name):
    """Some invariants here are about Python that BACKS the frontend's promises —
    a Settings toggle claiming a pass runs is a frontend lie told by a backend."""
    with open(os.path.join(REPO, "crm", name), encoding="utf-8") as fh:
        return fh.read()


def _function_body(src: str, opener: str) -> str:
    """The braced body that follows `opener`, by brace matching.

    Crude on purpose: a regex cannot find the end of a JS function, and pulling a
    parser into a zero-dependency repo to check one invariant is a worse trade
    than this. Raises rather than returning "" so a renamed function fails loudly
    instead of making the gate silently vacuous — which is the main way a test
    like this rots.
    """
    i = src.index(opener)
    start = src.index("{", i)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces after {opener!r}")


def _js_body(src: str, name: str) -> str:
    """The body of `function name(...) { ... }`, skipping a destructured
    parameter list, which `_function_body` would mistake for the body."""
    i = src.index(f"function {name}(")
    return _function_body(src[i:], ") {")


def _py_function(src: str, name: str) -> str:
    """The text of one top-level Python function, up to the next top-level
    definition. Raises (rather than returning "") when it is renamed, so a gate
    built on it cannot quietly check nothing."""
    i = src.index(f"\ndef {name}(") + 1
    m = re.search(r"^(def |class |[A-Z_]+ *[:=])", src[i + 4:], re.M)
    return src[i:i + 4 + m.start()] if m else src[i:]


class TestRowSelectionPointerCapture(unittest.TestCase):
    """The marquee must not take pointer capture on the press itself.

    THE BUG THIS ENCODES. `setPointerCapture` on the scroll container retargets
    pointerup/mouseup to that container, and `click` is then dispatched on the
    nearest common ancestor of the down and up targets. So a press that began on
    .nx-row fired its click on .nx-body, the row's own onClick never ran, and
    clicking a person did nothing at all — every row, every time. The events read:
    pointerdown target .nx-name, pointerup and click target .nx-body,
    defaultPrevented false throughout, so nothing is swallowing the event; it is
    simply delivered somewhere else.

    Capture belongs after DRAG_THRESHOLD is crossed, where the gesture is known to
    be a band. Nothing is lost by waiting: pointermove/pointerup/pointercancel are
    bound to `window`, which is what actually keeps a band tracking outside the
    table.
    """

    def setUp(self):
        self.src = js_code(_read("app", "select.js"))

    def test_on_down_does_not_take_pointer_capture(self):
        body = _function_body(self.src, "const onDown =")
        self.assertNotIn(
            "setPointerCapture", body,
            "onDown must not call setPointerCapture — it retargets the click away "
            "from the row and makes the People list unopenable by mouse. Capture "
            "belongs after the drag threshold is crossed, in onMove.")

    def test_capture_is_still_taken_once_the_band_commits(self):
        """The other half: don't 'fix' the above by dropping capture altogether.

        Without it a band stops tracking the moment the pointer leaves the table
        in some engines, so this asserts the call still exists somewhere.
        """
        self.assertIn("setPointerCapture", self.src,
                      "the marquee still needs capture once live — it just must "
                      "not be taken on pointerdown")

    def test_capture_is_released(self):
        self.assertIn("releasePointerCapture", self.src)

    def test_row_click_is_not_gated_on_a_flag_that_only_modifiers_clear(self):
        """`consumed` must be reset on the click it suppresses.

        A ref set on pointerdown and read on the following click is fine, but if
        the reset is ever moved out of the click handler, one modifier-click would
        swallow every plain click after it — the same symptom, a different cause.
        """
        people = js_code(_read("app", "people.js"))
        self.assertRegex(
            people, r"if\s*\(\s*consumed\.current\s*\)\s*\{\s*consumed\.current\s*=\s*false",
            "the consumed flag must be cleared in the same branch that reads it")


class TestStylesheetsAreRegistered(unittest.TestCase):
    """A view-*.css nobody links is invisible, and looks exactly like a CSS bug.

    Cheap to check and the same class of silent frontend failure as the above:
    the file is written, correct, and never served.
    """

    def setUp(self):
        self.index = _read("index.html")

    def test_every_linked_stylesheet_exists(self):
        for href in re.findall(r'<link[^>]+href="([^"]+\.css)"', self.index):
            self.assertTrue(
                os.path.exists(os.path.join(STATIC, href)),
                f"index.html links {href}, which does not exist")

    def test_every_view_stylesheet_is_linked(self):
        linked = set(re.findall(r'<link[^>]+href="css/([^"]+\.css)"', self.index))
        on_disk = {f for f in os.listdir(os.path.join(STATIC, "css"))
                   if f.startswith("view-") and f.endswith(".css")}
        missing = sorted(on_disk - linked)
        self.assertEqual(missing, [], f"view stylesheets never linked: {missing}")


class TestLinkedInPanelIsActuallyReachable(unittest.TestCase):
    """NC-064. Same failure mode as the FDA panel below, and the same gate.

    A finished feature that nothing on the page shows is invisible — the exact
    shape of NC-016 (api_intake.py: 2,527 lines and 127 passing tests behind six
    URLs nobody had registered). `node --check` says nothing about it and neither
    does the Python suite, so this does.

    NC-236. The separate LinkedIn panel (linkedin.js) went with the old Data
    page. LinkedIn is now three rows on the new page (connections export,
    messages, profile facts), built by the server in crm/api_ledger.py and drawn
    by data.js and ledger.js. The rules below follow the rows.

    The first two tests here are the ones with teeth. LinkedIn is the only
    surface in the app that describes an automation LinkedIn's terms do not
    allow, and the pacing that keeps that automation from earning a permanent
    ban lives in the scraper's own dispatcher, outside this app. A button in
    these rows that starts a scrape, or that ends a cooldown early, would defeat
    the daily cap, the randomised inter-session gap and the 24–48h cooldown at
    one click.
    """

    def setUp(self):
        self.data = js_code(_read("app", "data.js"))
        self.ledger = js_code(_read("app", "ledger.js"))
        self.server = _read_crm("api_ledger.py")
        self.rows = "\n".join(_py_function(self.server, n) for n in (
            "_row_linkedin_connections", "_row_linkedin_messages",
            "_row_linkedin_facts"))

    # NC-236: "the LinkedIn panel is rendered on the Data page" and "the panel
    # does not fetch the export finding for itself" went with the old Data page;
    # there is no panel component any more, and the rows come from one server
    # answer that already carries the finding.

    def test_it_offers_no_way_to_start_a_scrape_or_end_a_cooldown(self):
        """Ban risk, gated rather than remembered. The rows may DISPLAY a
        cooldown; a control that shortens one is the single thing the scraper
        handoff doc calls the most important operational rule to respect."""
        for where, text in (("data.js", self.data), ("ledger.js", self.ledger),
                            ("the LinkedIn rows in api_ledger.py", self.rows)):
            for banned in ("scrape_dispatch", "scrape_now", "runScrape", "startScrape",
                           "cooldown_clear", "clearCooldown", "endCooldown", "--force"):
                self.assertNotIn(banned, text,
                                 f"{where} contains {banned!r} — that is a ban "
                                 f"risk or billed spend behind a click")

    def test_a_paused_account_loses_its_import_button(self):
        """The one place the rows know about a cooldown: while LinkedIn has the
        account paused, the profile-facts row takes its Import button away
        instead of offering a click that would pull on a paused account."""
        facts = _py_function(self.server, "_row_linkedin_facts")
        self.assertIn('in_cooldown', facts)
        i = facts.index('in_cooldown')
        self.assertIn('a["kind"] != "import"', facts[i:i + 900],
                      "a row in cooldown still offers Import")

    # The only settings the LinkedIn rows may let someone write, and why each is
    # safe. Every one goes through /api/settings, the single writer for
    # secrets/.env (crm/settings.py), from a box that writes ONE named key.
    #
    #   LINKEDIN_TOKEN           NC-078. The token box on the messages row.
    #   IMAC, KEY                The machine that runs the scraper and where its
    #                            sign-in file is. Text only; KEY refuses a
    #                            pasted private key (ledger.js textProblem).
    #   SCRAPE_OVERRIDES_MANUAL  NC-234. A switch, off unless the owner turns it on.
    #   LINKEDIN_CLIENT_ID,      NC-371. The app's own LinkedIn credentials, which
    #   LINKEDIN_CLIENT_SECRET   only the token's expiry check reads
    #                            (linkedin_token.credentials). The secret is a masked
    #                            box; neither starts a scrape or spends anything.
    #
    # Anything not in here is a new kind of write from the rows that front a
    # ToS-gray automation, and needs a human to think about it.
    SETTINGS_KEYS_ALLOWED = {"LINKEDIN_TOKEN", "IMAC", "KEY", "SCRAPE_OVERRIDES_MANUAL",
                             "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"}

    def test_it_writes_only_to_the_allowlist(self):
        """Stricter than the blanket ban it replaced: it lists what
        is permitted and forbids everything else. NC-236 moved it from the
        panel's own POST calls to the boxes the server hands the rows, because
        that is now where a write is decided: each box names the one key it
        writes, and the page has a single place that sends it."""
        keys = set(re.findall(r'_(?:secret|text|switch_act)\(\s*"([A-Z_]+)"', self.rows))
        self.assertTrue(keys, "found no setting boxes on the LinkedIn rows to check")
        self.assertEqual(
            sorted(keys - self.SETTINGS_KEYS_ALLOWED), [],
            f"the LinkedIn rows offer to write settings beyond "
            f"{sorted(self.SETTINGS_KEYS_ALLOWED)} — these rows front a ToS-gray "
            f"automation, so a new write from here needs a human decision and an "
            f"entry in SETTINGS_KEYS_ALLOWED saying why it is safe")
        # And the page has exactly one route by which a box writes.
        self.assertEqual(
            re.findall(r'api\(\s*"(/api/settings)"\s*,\s*\{\s*method:\s*"POST"', self.ledger),
            ["/api/settings"],
            "ledger.js no longer writes a setting through the one shared path")

    def test_it_still_opens_urls_through_the_shared_allowlist(self):
        """Unchanged and still load-bearing: the row opens a URL the way every
        other 'Open …' button does, by sending an allowlisted KEY to the shared
        endpoint, never a URL of its own."""
        self.assertIn("openTarget(", self.data)
        self.assertIn('"kind": "open", "target": "linkedin"', self.rows)
        for where, text in (("data.js", self.data), ("ledger.js", self.ledger),
                            ("api_ledger.py", self.server)):
            self.assertNotIn("linkedin.com", text,
                             f"{where} holds a LinkedIn URL of its own; the URL "
                             "belongs in importers.OPEN_TARGETS")

    def test_the_row_can_accept_the_token_it_is_asking_for(self):
        """NC-078. The messages row's job in the no-token state is to ask for a
        token, and it used to end by sending the owner to another screen to hand one
        over. A surface that states a problem and cannot accept the fix is a
        surface you read and then leave."""
        # The RENDER, not the definition: a component nothing renders is not a
        # feature.
        self.assertIn("<${CredentialBox}", self.data,
                      "the page no longer draws a token field for a secret the "
                      "server asks for")
        self.assertIn('case "secret"', self.data)
        self.assertIn('type="password"', self.ledger,
                      "the token field is not a password field — a credential "
                      "must not be shoulder-surfable")
        self.assertIn('_secret("LINKEDIN_TOKEN"', _py_function(
            self.server, "_row_linkedin_messages"),
            "the messages row does not offer a box that writes LINKEDIN_TOKEN")

    def test_the_button_opens_the_generator_not_the_tools_index(self):
        """NC-079. The button's link must go to the page that issues a token,
        /token-generator (see LinkedIn's "Developer Portal Tools" documentation),
        and not to /developers/tools/oauth, which only LISTS the OAuth tools. Both
        return 200, so no status check would ever catch a wrong link — only
        reading the page does."""
        importers = _read_py("importers.py")
        i = importers.index('"linkedin": {"url"')
        entry = importers[i:i + 260]
        self.assertIn("/token-generator", entry,
                      "the LinkedIn button points at the OAuth tools index again, "
                      "which lists tools and issues nothing")

    def _help(self):
        fn = _py_function(self.server, "_row_linkedin_messages")
        i = fn.index('out["detail"]["help"] = (')
        return fn[i:fn.index("acts: List", i)]

    def test_the_steps_name_the_app_and_the_scope_in_order(self):
        """One sentence is not enough for a five-step trip through a developer
        console. Each of these fails LATE — the token saves fine and the symptom
        shows up afterwards — so the instruction is the only place it can be
        prevented."""
        prose = self._help()
        for needle, why in (
            ("Member Data Portability",
             "the steps do not say which app to pick, so the wrong one gives a "
             "60-day token instead of a year and the user is back six times as often"),
            ("r_dma_portability_self_serve",
             "the steps do not name the scope to tick, and a token without it "
             "saves fine and collects nothing"),
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, prose, why)
        # The row's help is written as a numbered list: the ORDER is the
        # information here.
        self.assertRegex(prose, r'(?s)"1\. .*?"2\. .*?"3\. ',
                         "the instructions are no longer numbered steps")

    def test_the_steps_warn_about_the_two_late_failures_they_used_to(self):
        """NC-236. The steps must name two traps: LinkedIn's approval screen (the
        step that cannot be automated) and that a short paste is a token cut off
        when copied — a ~500 character value that saves, then fails later as a
        rejected token."""
        prose = self._help()
        for needle, why in (
            ("Allow",
             "the steps do not mention LinkedIn's approval screen, which is the "
             "step that cannot be automated"),
            ("cut off",
             "the steps do not warn that a short paste is a token cut off — "
             "a ~500 character value that fails later as a rejected token"),
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, prose, why)

    def test_the_row_does_not_send_the_owner_to_settings_for_the_token(self):
        """The other half. Keeping the field AND the old 'paste it into Settings'
        sentence would be worse than either alone: two instructions for one
        action, one of which walks away from the field sitting underneath it."""
        self.assertNotIn(
            "paste it into Settings", self.rows,
            "a LinkedIn row tells the owner to paste the token into Settings while a "
            "field for it sits on the row — say it once, where the action is")
        self.assertNotIn('"kind": "settings"', _py_function(
            self.server, "_row_linkedin_messages"),
            "the messages row sends the owner to Settings instead of taking the token")

    def test_the_token_is_never_rendered_back(self):
        """Write-only, like Settings. The server's masked hint ('…last4') is the
        only form of the value allowed on the page; the box must never put a real
        token into an input's value or into the DOM."""
        from unittest import mock
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import api_ledger
        secret = "AQVthisIsAFakeTokenForTheTest0123456789"
        with mock.patch.dict(os.environ, {"LINKEDIN_TOKEN": secret}):
            act = api_ledger._secret("LINKEDIN_TOKEN", "LinkedIn token")
        self.assertTrue(act["set"])
        self.assertNotIn(secret, json.dumps(act),
                         "the server sends the whole token to the page")
        # Scoped to the SAVE path. `setValue("")` also lives in the Escape
        # handler, so a file-wide assertIn passes with the post-save clear
        # deleted — the gate would be reading the neighbourhood, not the behaviour.
        box = self.ledger[self.ledger.index("export function CredentialBox("):]
        save = box[box.index("const connect = async"):box.index("const remove = async")]
        self.assertIn('setValue("")', save,
                      "the token box does not clear itself after saving, so a "
                      "credential sits in component state for the rest of the visit")
        self.assertNotIn("value=${act.value", box,
                         "the box seeds an input from server state")

    def test_saving_a_token_moves_the_whole_page_not_just_this_row(self):
        """A saved token changes whether the LinkedIn row is blocked, which the
        needs list, the source row and the page's verdict all read. If only the
        box refreshed, the page would contradict itself again — which is NC-076."""
        box = self.ledger[self.ledger.index("export function CredentialBox("):]
        self.assertIn("onSaved?.()", box[:box.index("const remove = async")],
                      "the box cannot tell the Data page that a token landed")
        self.assertRegex(
            self.data, r"<\$\{CredentialBox\}[^>]*onSaved=\$\{onChanged\}",
            "the page does not listen for a saved token, so the row would still "
            "read 'Not collecting' after a successful save")
        self.assertIn("const changed = useCallback(() => load(false), [load])", self.data,
                      "a change on the page no longer asks the server again")

    def test_every_icon_it_names_exists(self):
        """Icon returns null for an unknown name — deliberately, so a typo cannot
        blank the page. The cost is that a wrong name is invisible: no error, no
        warning, no failing test. This is the failing test."""
        icons = set()
        for text in (self.data, self.ledger):
            icons |= set(re.findall(r'name="([A-Za-z_]+)"', text))
            icons |= set(re.findall(r'icon=\s*"([A-Za-z_]+)"', text))
        block = self.data[self.data.index("const ROW_ICON = {"):]
        block = block[:block.index("};")]
        icons |= set(re.findall(r':\s*"([A-Za-z_]+)"', block))
        self.assertTrue(icons, "found no icon names to check — the regex is stale")
        known = set(re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*):",
                               _read("app", "icons.js"), re.M))
        missing = sorted(icons - known)
        self.assertEqual(missing, [],
                         f"the Data page renders icons that do not exist: {missing}")


class TestFdaPanelIsActuallyReachable(unittest.TestCase):
    """The Full Disk Access flow has to be ON A PAGE.

    Everything needed to grant this permission — the helper, the installer, the
    exit code, the System Settings pane — has to be reachable without a Terminal.
    The app is meant to be used as the packaged .app, without a Terminal, so the
    steps must be on a page rather than printed to a console. A feature that is
    complete and unreachable is the same failure as crm/api_intake.py sitting
    behind six URLs nobody had wired up: correct code with no surface.

    A module nobody imports is exactly that failure again, and `node --check` says
    nothing about it. Hence these.
    """

    def setUp(self):
        self.fda = js_code(_read("app", "fda.js"))
        self.dataview = js_code(_read("app", "data.js"))
        self.onboarding = js_code(_read("app", "onboarding.js"))

    def test_the_panel_is_rendered_on_the_data_page(self):
        # NC-236: data.js (the page since the switch-over) draws the panel inside
        # the "Automatic copying" row, when the server sends a `helper` control.
        self.assertIn('from "./fda.js"', self.dataview,
                      "data.js does not import the panel")
        self.assertIn("<${FdaPanel}", self.dataview,
                      "data.js imports the panel but never renders it")
        self.assertIn('case "helper"', self.dataview,
                      "nothing on the page draws the panel for the row that offers it")

    def test_the_panel_is_rendered_in_the_onboarding_wizard(self):
        self.assertIn('from "./fda.js"', self.onboarding)
        self.assertIn("<${FdaPanel}", self.onboarding)

    def test_the_wizard_no_longer_tells_anyone_to_use_a_terminal(self):
        """The exact copy that made this invisible. It said "Run this in Terminal
        and follow what it prints", beside a path to a shell script."""
        self.assertNotIn("in Terminal", self.onboarding,
                         "the wizard is back to sending the user to a shell")

    def test_the_panel_asks_the_server_which_targets_to_open(self):
        """Both buttons post a KEY from importers.OPEN_TARGETS. A path or a URL
        written into the JS would route around the server-side allowlist that every
        other "Open …" button in this app goes through."""
        self.assertIn("status.targets.settings", self.fda)
        self.assertIn("status.targets.binary", self.fda)
        self.assertNotIn("x-apple.systempreferences", self.fda,
                         "the System Settings URL belongs in OPEN_TARGETS, not here")
        self.assertNotIn("Application Support", self.fda,
                         "the helper's path comes from /api/fda/status, not from JS")

    def test_the_panel_offers_a_way_to_verify_rather_than_asking_the_user_to_guess(self):
        """The missing third of the flow: the app could install the helper and open
        the pane, but never say whether the grant worked. "Check now" is that, and
        it is the reason this is a status panel and not an instruction sheet."""
        self.assertIn("/api/fda/recheck", self.fda)
        self.assertIn("/api/fda/install", self.fda)
        self.assertIn("/api/fda/status", self.fda)

    def test_the_dead_helper_payload_has_no_lingering_reader(self):
        """`imessage_helper` on /api/import/detect was the old, thinner answer to
        "what about the helper". It was removed when /api/fda/status replaced it.
        Two endpoints answering that question with different amounts of the truth
        is how the UI came to claim the permission was already granted."""
        for name, src in (("data.js", self.dataview),
                          ("onboarding.js", self.onboarding),
                          ("fda.js", self.fda)):
            with self.subTest(file=name):
                self.assertNotIn("imessage_helper", src)


class TestTheMarkCheckboxDoesNotCoverTheFace(unittest.TestCase):
    """The People table's mark checkbox lives in a reserved gutter, left of the avatar.

    WHAT THIS IS TIED TO. The checkbox must not be absolutely positioned ON TOP of
    the avatar. That is a common pattern in record tables, and the
    argument for it is that a permanent checkbox column costs ~27px on every row
    forever. But sitting on top of the face it hides part of the photo, and hovering
    greys a face out: the rows are photographs of people. So the checkbox has its own
    gutter.

    Two ways this silently regresses, both invisible to `node --check` and to every
    other test here, because both are pure layout:

      · the gutter is dropped from .nx-head / .nx-row padding, so the checkbox is
        back over the avatar;
      · the gutter is added to the rows but not the header, so every column label
        stops lining up with its values.

    So this asserts the two live on the same declaration. It does not assert
    anything about the 27px itself — that is a taste, and a gate on taste gets
    worked around.
    """

    def setUp(self):
        self.net = _read("css", "view-network.css")
        self.ui = _read("css", "ui.css")

    def test_the_gutter_token_exists(self):
        self.assertRegex(self.net, r"--nx-mark-col:\s*\d+px",
                         "view-network.css no longer defines the mark column width")

    def test_the_header_and_the_rows_reserve_it_together(self):
        """One selector for both, or the columns drift apart."""
        m = re.search(r"\.nx-head,\s*\.nx-row\s*\{(.*?)\}", self.net, re.S)
        self.assertIsNotNone(m, ".nx-head and .nx-row no longer share one rule")
        self.assertIn("--nx-mark-col", m.group(1),
                      "the shared .nx-head/.nx-row rule does not reserve the mark "
                      "gutter — the checkbox is back on top of the avatar, or the "
                      "header no longer lines up with its own columns")

    def test_the_checkbox_sits_in_that_gutter_and_not_over_the_avatar(self):
        """.nx-mark is absolutely positioned, so its `left` is the whole story: it
        has to be inside the reserved gutter, which means the row's own padding
        (var(--gap-sm)) and nothing added on top of it."""
        m = re.search(r"\.nx-mark\s*\{(.*?)\}", self.ui, re.S)
        self.assertIsNotNone(m, ".nx-mark is gone from ui.css")
        body = js_code(m.group(1))
        left = re.search(r"left:\s*([^;]+);", body)
        self.assertIsNotNone(left, ".nx-mark no longer sets `left`")
        self.assertEqual(left.group(1).strip(), "var(--gap-sm)",
                         "`left` moved off the gutter's own edge. The +1px nudge this "
                         "replaced existed to line the checkbox up with the avatar it "
                         "was covering; anything but the plain gap puts it back into "
                         "the person's face or floats it in the middle of nowhere")

    def test_it_is_still_a_sibling_of_the_row_button_not_a_child(self):
        """Not layout — correctness. A <button> inside a <button> is invalid HTML and
        Safari makes the inner one unclickable, so the checkbox would render
        perfectly and do nothing. Moving it into the person cell is the obvious
        "tidier" refactor and it is the one that breaks this."""
        people = js_code(_read("app", "people.js"))
        wrap = people.index("nx-rowwrap")
        row = people.index('class=${"nx-row"', wrap)
        mark = people.index('class=${"nx-mark"', wrap)
        self.assertLess(mark, row,
                        "the mark control must be emitted BEFORE the row button and "
                        "as its sibling — if it has moved inside .nx-row, Safari will "
                        "silently stop it working")


class TestTheFoundRowSaysImportWhenNothingHasBeenImported(unittest.TestCase):
    """NC-002. A "Found on this Mac" row's button must not read "Sync now" for a
    file nothing has ever read, such as a .vcf or an .ics that was never imported:
    a page of rows all claiming to sync would claim what they have never done.
    "Sync" is a promise about an ongoing connection being refreshed; a file
    nothing has ever read is an import.

    NC-236. The old page chose the verb on its own side, from whether the source
    had ever synced. The rebuilt page draws what the server sends, and the server
    only ever offers "Import" (or "Import this one instead") for a file it found.
    So the rule is now: nobody says "Sync now" about a file, and the button
    shows the server's verb rather than a literal of its own.

    A source-level gate because the row is Preact with no build step: the bug was
    a string literal that rendered perfectly and lied."""

    def setUp(self):
        self.data = js_code(_read("app", "data.js"))
        self.ledger = js_code(_read("app", "ledger.js"))
        self.server = _read_crm("api_ledger.py")

    def test_nothing_on_the_page_says_sync_now_about_a_file(self):
        for where, text in (("data.js", self.data), ("ledger.js", self.ledger),
                            ("api_ledger.py", self.server)):
            self.assertNotIn("Sync now", text,
                             f"{where} says \"Sync now\" again — every never-imported "
                             "file would claim to be syncing")

    def test_the_button_actually_renders_the_servers_verb(self):
        """The gate above only proves the bad word is absent. What matters is what
        the button renders: the verb the server chose, not a literal."""
        body = _js_body(self.data, "ImportAct")
        i = body.index('disabled=${running} onClick=${() => start(false)}>')
        label = body[i:body.index("</button>", i)]
        self.assertIn("act.verb", label,
                      "the import button does not render the server's verb — its "
                      f"label expression is: {label.strip()[-120:]!r}")

    def test_the_state_text_does_not_claim_a_file_was_found_and_handled(self):
        """The need's sentence for a file nobody has imported says what happens
        next — press Import — instead of a bare 'found on this Mac'."""
        needs = _py_function(self.server, "_file_needs")
        self.assertIn("Press Import to bring it in", needs,
                      "a found file's sentence no longer says that it has not "
                      "been imported yet")


class TestThePageNeverClaimsAnAllClearItCannotKnow(unittest.TestCase):
    """NC-033. GET /api/import/detect could stop answering — it globs Desktop and
    Downloads and stats WhatsApp's group container, all TCC-protected, so it can
    block on a consent dialog nobody is looking at, with no response at all from
    curl or from the page's own fetch. The Data page then rendered anyway and
    printed "No source needs you now."

    Two separate defects made that possible, so there are two gates."""

    def setUp(self):
        self.api = js_code(_read("app", "api.js"))
        self.dataview = js_code(_read("app", "data.js"))

    def test_the_fetch_wrapper_cannot_hang_forever(self):
        self.assertIn("AbortController", self.api,
                      "api() has no AbortController — a request that never answers "
                      "never settles, and every caller waits for it forever")
        self.assertIn("API_TIMEOUT_MS", self.api,
                      "the timeout must be a named constant, not an inline number")
        m = re.search(r"API_TIMEOUT_MS\s*=\s*(\d+)", self.api)
        self.assertIsNotNone(m, "API_TIMEOUT_MS is not assigned a literal")
        self.assertGreater(int(m.group(1)), 0,
                           "a zero/absent timeout disables the whole fix")
        # Scoped to api() itself: api.js also has a debounce helper that calls
        # clearTimeout, so a whole-file search would pass even with the abort
        # timer leaking.
        start = self.api.index("export async function api(")
        body = self.api[start:self.api.index("\n}", start)]
        self.assertIn("clearTimeout", body,
                      "api() leaks its abort timer — an uncleared timer keeps the tab "
                      "awake and can abort a later request that reused the controller")

    def test_a_timed_out_scan_names_the_permission_as_the_cause(self):
        """NC-034. detect answers in milliseconds for a process that has been granted
        the folders, and can wait indefinitely for one that has not — it globs
        Desktop and Downloads and stats WhatsApp's container, so an ungranted
        process waits on a consent dialog nobody can see. "Couldn't check" is
        honest but useless; the permission is the one thing the user can act on."""
        self.assertIn('"timeout"', self.dataview,
                      "the timeout case is not distinguished from a generic error")
        # NC-170: the TRUE branch, not 700 characters. The window also held the
        # generic "just now" message underneath, so a gate asserting the timeout
        # text names Downloads could have been satisfied by the wrong sentence.
        branch = js_ternary_branch(
            _js_body(self.dataview, "ScanFailed"), 'how === "timeout"')
        for phrase in ("Desktop", "Downloads", "System Settings"):
            self.assertIn(phrase, branch,
                          f"the timeout message does not mention {phrase}, so it does "
                          f"not tell the user what to actually do")

    def test_a_failed_scan_and_an_empty_scan_say_different_things(self):
        # NC-236: the page now asks GET /api/data/ledger?scan=1 instead of
        # /api/import/detect, and the scan's own failure is caught in load().
        self.assertIn("setScanFailed", self.dataview,
                      "there is no state distinguishing 'could not check' from "
                      "'checked, found nothing'")
        self.assertIn('setScanFailed(/did not answer/.test(e?.message || "") '
                      '? "timeout" : "error")', self.dataview,
                      "a scan that fails is no longer told apart from one that "
                      "timed out")
        # The all-clear sentence, and the "still looking" line that must also not
        # read as one. The scan-failed check has to come BEFORE either, and the
        # check for an unfinished scan before the sentence that says "Nothing.".
        clear = _js_body(self.dataview, "AllClear")
        failed = clear.index("if (scanFailed) return")
        looking = clear.index("if (!data.scanned)")
        nothing = clear.index("Nothing.")
        self.assertTrue(failed < looking < nothing,
                        "AllClear asserts 'Nothing.' before it knows whether the "
                        "scan answered, so the page still claims an all clear "
                        "when it could not look")
        # And the list above it never shows a scan that failed as a quiet list.
        self.assertIn("scanFailed ? html`<${ScanFailed} how=${scanFailed} />`",
                      self.dataview)


class TestTheAutomaticPassesDoNotDependOnAUiRowExisting(unittest.TestCase):
    """NC-038. The free facts pass and the writing-voice re-measure are things the
    app does FOR the user, on a timer, with no button. Their rows on the Data page
    are being retired.

    Both chains used to resolve their argv through `_build_command("facts")`, i.e.
    through `importers.REGISTRY`. Retiring the row would make that raise ApiError
    inside the caller's `except Exception: pass` — so the pass would silently stop
    running after every import, while the Settings toggle went on saying it ran.
    A capability must not die because its UI row did."""

    @staticmethod
    def _func_code(source, name):
        """A function's CODE, docstring and comments removed.

        Not fastidiousness — a gate reading the raw source fails against the very
        docstring that explains it, which says `_build_command("facts")` in prose
        while the code below it calls the named builder. Every assertion in this
        class is about an identifier, so every one of them would have that
        defect. Same reason `js_code` exists for the JS above.
        """
        import ast
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = list(node.body)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]          # drop the docstring
                return "\n".join(ast.unparse(stmt) for stmt in body)
        raise AssertionError(f"{name}() not found")

    def setUp(self):
        self.api_import = _read_crm("api_import.py")
        self.importers = _read_crm("importers.py")

    def test_the_chains_call_named_builders_not_the_registry(self):
        for fn, argv in (("_run_chained_facts_pass", "importers.facts_pass_cmd()"),
                         ("_run_chained_style_pass", "importers.style_pass_cmd()")):
            with self.subTest(fn=fn):
                code = self._func_code(self.api_import, fn)
                self.assertIn(argv, code, f"{fn} does not use {argv}")
                self.assertNotIn("_build_command", code,
                                 f"{fn} resolves its argv through the registry, so "
                                 f"retiring a Data-page row would silently kill it")

    def test_the_named_builders_exist_and_the_free_one_is_never_the_paid_one(self):
        code = self._func_code(self.importers, "facts_pass_cmd")
        self.assertIn("enrich_facts.py", code)
        self.assertNotIn("--llm", code,
                         "the automatic pass must never be the one that spends money")
        self.assertIn("style_profile.py", self._func_code(self.importers, "style_pass_cmd"))


class TestEverySettingWePointTheUserAtActuallyExists(unittest.TestCase):
    """NC-004. `NC_INTAKE_WEB`'s own refusal message read "Set NC_INTAKE_WEB to 1
    under Settings" — and it was not in MANAGED_KEYS, so the user went looking for
    a field that did not exist. Sending someone to a control that isn't there is
    worse than having no control.

    Deliberately narrow. Most env keys are NOT settings and should not be: data
    paths, test hooks, and internal plumbing like ENRICH_AFTER_IMPORT_SEC. This
    asserts only the thing that can never be right — naming a key in user-facing
    copy that points at Settings, while the key is absent from Settings."""

    def test_no_message_points_at_a_settings_field_that_does_not_exist(self):
        import glob
        import re
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import settings as settings_mod
        managed = set(settings_mod.MANAGED_KEYS)
        pyfiles = sorted(glob.glob(os.path.join(REPO, "crm", "*.py")))
        sources = {p: io.open(p, encoding="utf-8").read() for p in pyfiles}
        # Only keys the code ACTUALLY READS from the environment count. Without
        # this the gate would match `NC_STYLE_*` — a prefix wildcard inside a
        # docstring, not a key — and a gate with a false positive gets deleted
        # rather than obeyed.
        env_read = set()
        for src in sources.values():
            env_read |= set(re.findall(
                r'(?:os\.environ(?:\.get)?\(|_int_env\()\s*["\']([A-Z][A-Z0-9_]{3,})["\']', src))
        offenders = []
        pat = re.compile(r'["\']([^"\']*?\b([A-Z][A-Z0-9_]{3,})\b[^"\']*?[Ss]ettings[^"\']*)["\']')
        for path, src in sources.items():
            for text, key in pat.findall(src):
                if key in managed or key not in env_read:
                    continue
                offenders.append(f"{os.path.basename(path)}: {key} — {text.strip()[:90]!r}")
        self.assertEqual(offenders, [], "\n".join([
            "",
            "These tell the user to set a key under Settings, but the key is not in",
            "MANAGED_KEYS, so no such field is rendered. Either register the key or",
            "stop naming Settings in the message.",
            *(f"  {o}" for o in offenders),
        ]))

    def test_both_halves_of_the_paste_autoimport_switch_are_settable(self):
        """The other half of NC-004: api_import.py gates the iMessage and WhatsApp
        copies in one loop, over one pair of switches. One was a Settings field and
        the other could only be set by editing the environment.

        NC-074 corrected what this measures. The bug was never "these two must be
        on the screen" — it was the ASYMMETRY: one visible, its twin reachable only
        by editing a file, for two halves of a single loop. NC-074 removed both
        (the master switch stops them, and six per-source children asking one question was clutter), which resolves the
        asymmetry the honest way. So this asserts symmetry: both rendered or neither, never one.

        Both must still be read by api_import and still exist in the registry — the
        pair has to remain a pair, and removing a control must not become removing
        the capability.
        """
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import settings as settings_mod
        api_import = _read_crm("api_import.py")
        js = js_code(_read("app", "settings.js"))
        pair = ("NC_IMSG_AUTOIMPORT", "NC_WA_AUTOIMPORT")
        for key in pair:
            with self.subTest(key=key):
                self.assertIn(key, api_import, f"{key} is no longer read at all")
                self.assertIn(key, settings_mod.MANAGED_KEYS,
                              f"{key} is read from the environment but is not a setting")
        on_screen = {key for key in pair if key in js}
        self.assertIn(
            len(on_screen), (0, 2),
            f"one half of the paste-autoimport pair is on the Settings screen and "
            f"the other is not: rendered={sorted(on_screen)}. They gate two halves "
            f"of one loop, so either both are controls or neither is — the split "
            f"is NC-004.")


class TestAskNamesTheRightCause(unittest.TestCase):
    """NC-128. aiDownNotice(), RUN rather than read.

    Every other gate in this file matches source text, and that is how a bug
    like this survives: `credit balance` is present in ask.js, so any
    grep-shaped check would call the case covered. What matters is the *branch*
    it sits in — an out-of-credit error must not land in the alternation that
    says the key was rejected, because that would tell the user to go and look
    at a key that is perfectly good, and offer a Settings button that cannot fix
    it.

    Reading the file cannot tell those two apart. Executing the function can, so
    this class slices the function out and runs it under node. Change the regex
    and these fail; that is the whole point of paying for a subprocess here.
    """

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed")
        js = _read("app", "ask.js")
        at = js.index("function aiDownNotice(")
        # to the closing brace of the function, which sits in column 0
        end = js.index("\n}\n", at) + 3
        cls.fn = js[at:end]

    def notice(self, detail):
        script = (self.fn + "\nprocess.stdout.write(JSON.stringify("
                  + "aiDownNotice(" + json.dumps(detail) + ")));")
        out = subprocess.run([self.node, "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    # A realistic 400 body, as the API sends it when the account is out of credit.
    OUT_OF_CREDIT = ("BadRequestError: Error code: 400 - {'type': 'error', 'error': "
                     "{'type': 'invalid_request_error', 'message': 'Your credit "
                     "balance is too low to access the Anthropic API. Please go to "
                     "Plans & Billing to upgrade or purchase credits.'}}")
    BAD_KEY = ("AuthenticationError: Error code: 401 - {'error': {'type': "
               "'authentication_error', 'message': 'invalid x-api-key'}}")

    def test_an_empty_account_is_not_reported_as_a_bad_key(self):
        note = self.notice(self.OUT_OF_CREDIT)
        self.assertNotIn("rejected the key", note["text"],
                         "the key is fine — the account is out of money, and "
                         "sending the user to inspect a working key wastes the one "
                         "sentence Ask gets to explain itself")
        self.assertIn("out of credit", note["text"])

    def test_an_empty_account_offers_no_action_the_app_cannot_perform(self):
        note = self.notice(self.OUT_OF_CREDIT)
        self.assertIsNone(note.get("action"),
                          "billing is not in this app, so an 'Open Settings' "
                          "button here is a button that cannot repair the fault")

    def test_a_bad_key_still_reaches_the_key_branch(self):
        """The counter-test. Fixing one branch by breaking the other is not a fix."""
        note = self.notice(self.BAD_KEY)
        self.assertIn("rejected the key", note["text"])
        self.assertEqual(note.get("action"), "Open Settings")

    def test_a_bare_401_reaches_it_too(self):
        """Deleting `401` from the regex would leave the test above green, because
        that fixture also says "authentication" and "x-api-key" and any one of the
        three carries it. A 401 from a proxy or a gateway says none of those, and
        would fall through to "cannot reach the API" — the wrong diagnosis, with
        no button, for a fault that has both."""
        note = self.notice("HTTP 401 from upstream")
        self.assertIn("rejected the key", note["text"])
        self.assertEqual(note.get("action"), "Open Settings")

    def test_the_other_causes_are_untouched(self):
        self.assertIn("not installed",
                      self.notice("ModuleNotFoundError: No module named 'anthropic'")["text"])
        self.assertIn("busy", self.notice("Error code: 429 - rate_limit_error")["text"])
        self.assertIn("cannot reach",
                      self.notice("APIConnectionError: Connection error.")["text"])

    def test_every_branch_keeps_the_raw_cause(self):
        """The sentence is the diagnosis, the detail is the evidence."""
        for d in (self.OUT_OF_CREDIT, self.BAD_KEY, "Connection error."):
            self.assertEqual(self.notice(d)["detail"], d.strip())


class TestTwoPagesNeverSpellARowTheSameWay(unittest.TestCase):
    """NC-130. No class may be given LAYOUT by two different page stylesheets.

    static/index.html loads every view stylesheet into one document, so a bare
    `.src-row {}` in view-settings.css and a bare `.src-row {}` in view-data.css
    are the same selector at the same specificity, and the later <link> simply
    wins — on both pages. That is how the Data page came to draw each source's
    name on top of its own date: Settings had a three-column grid for its rows,
    the Data page's rows have one child, and the child landed in a 10px column
    with everything inside it overflowing on top of everything else.

    The component itself had moved (NC-105 took it from Settings to Data) and
    its stylesheet had not, so the two files were describing two different
    components under one name. Nothing caught it, because every check this file
    had asked whether a string was PRESENT — and the string was present, in the
    wrong file. Presence was never the question.

    Shared styling in ui.css is exempt and expected: one base rule that every
    page builds on is the opposite of two pages disagreeing. This gate only
    fires when two *view* stylesheets both lay out the same name.
    """

    LAYOUT = re.compile(r"\b(display|grid-template[a-z-]*|grid-area|grid-column|grid-row|"
                        r"flex|flex-[a-z]+|position|float|width|height|inset|top|left|right|"
                        r"bottom|align-items|align-self|justify-[a-z]+|gap|row-gap|column-gap|"
                        r"padding|margin|overflow|order)\s*:")
    # A selector already carrying its page's own scope cannot collide with
    # another page — that is the convention this gate is protecting.
    SCOPES = ("data-view", "settings-view", "ask-view", "people-view", "lists-view",
              "intake-view", "network-view", "setup-view", "boot")

    def _rules(self, path):
        css = re.sub(r"/\*.*?\*/", "", io.open(path, encoding="utf-8").read(), flags=re.S)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            sel, body = m.group(1).strip(), m.group(2).strip()
            if sel.startswith("@") or not body:
                continue
            for one in sel.split(","):
                yield one.strip(), body

    def test_no_two_view_stylesheets_lay_out_the_same_class(self):
        html = _read("index.html")
        order = re.findall(r'href="css/([a-z0-9-]+\.css)"', html)
        self.assertTrue(order, "no stylesheets found in index.html")

        owners = {}          # class -> {file: True} for layout-setting bare rules
        for f in order:
            if not f.startswith("view-"):
                continue     # ui.css / tokens.css are the shared base, by design
            path = os.path.join(STATIC, "css", f)
            if not os.path.exists(path):
                continue
            for sel, body in self._rules(path):
                if any(sc in sel for sc in self.SCOPES):
                    continue
                m = re.match(r"^\.([a-zA-Z0-9_-]+)", sel)
                if m and self.LAYOUT.search(body):
                    owners.setdefault(m.group(1), set()).add(f)

        clashes = {c: sorted(fs, key=order.index)
                   for c, fs in owners.items() if len(fs) > 1}
        self.assertEqual(
            clashes, {},
            "two page stylesheets lay out the same class name, and the one "
            "loaded last wins on BOTH pages: "
            + "; ".join(f".{c} in {' then '.join(fs)}" for c, fs in sorted(clashes.items()))
            + " — scope one of them to its page, or give the two components "
              "different names")

    def test_the_ledger_rows_are_styled_in_one_place_and_under_one_prefix(self):
        """NC-236. The pair this used to name (sources.js's `ledger-` rows against
        dataview.js's `src-` rows) is gone. What is left is the same idea for the
        new pair: the rebuilt Data and Settings pages draw every row with the
        `lg-` prefix from view-ledger.css, and neither page's own stylesheet may
        restyle those names (the later <link> would win on both pages), nor may
        the old `ledger-` family come back."""
        ledger_css = _read("css", "view-ledger.css")
        self.assertIn(".lg-dot", ledger_css,
                      "the rows' rules are not where the pages expect them")
        strip = lambda css: re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for name in ("view-data.css", "view-settings.css"):
            css = strip(_read("css", name))
            bare = sorted(set(re.findall(r"(?m)^\s*(\.lg-[a-z-]+)[\s,:{.]", css)))
            self.assertEqual(bare, [],
                             f"{name} styles {bare}, which view-ledger.css owns for "
                             "both pages")
        for name in sorted(os.listdir(os.path.join(STATIC, "css"))):
            if name.endswith(".css"):
                self.assertNotIn(".ledger-", strip(_read("css", name)),
                                 f"{name} has the pre-NC-236 `.ledger-` class family")


class TestEveryShippedAppFileParses(unittest.TestCase):
    """NC-131. `node --check FILE` answers "fine" for any file that uses
    import/export, which is every file in static/app/. So the files are fed to
    Node on standard input as modules, where a broken one is refused."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node missing")

    def test_every_shipped_app_file_actually_parses(self):
        """Run the real parser over the real files — the check that was never
        happening. Cheap, and it is the only thing that proves the twenty files
        are clean rather than merely unexamined."""
        broken = []
        for path in sorted(glob.glob(os.path.join(STATIC, "app", "*.js"))):
            with open(path, "rb") as fh:
                r = subprocess.run(["node", "--input-type=module", "--check"],
                                   stdin=fh, capture_output=True, timeout=60)
            if r.returncode:
                broken.append(os.path.basename(path))
        self.assertEqual(broken, [], f"these do not parse: {broken}")


class TestTheGreetingIsSeededWithNoName(unittest.TestCase):
    """NC-132. `useState("<a literal name>")` on a setting whose server default
    is "".

    Ask seeds selfName, then overwrites it only `if (s?.value)`. SELF_NAME's
    default in crm/settings.py is the empty string, so on any install where
    nobody has typed a name — which is every fresh install, by definition — a
    literal seed would never be replaced and the page would greet its new owner
    "Good morning, <that name>" forever.

    greeting() handles a blank name: it returns "Good morning" with no name at
    all. So the seed is the one thing to guard.
    """

    def test_the_seed_carries_no_name(self):
        js = js_code(_read("app", "ask.js"))
        m = re.search(r"useState\(([^)]*)\)[^\n]*//[^\n]*SELF_NAME", js) or \
            re.search(r"\[\s*selfName\s*,\s*setSelfName\s*\]\s*=\s*useState\(([^)]*)\)", js)
        self.assertIsNotNone(m, "could not find the selfName seed")
        self.assertIn(m.group(1).strip(), ('""', "''"),
                      "the greeting is seeded with a literal name, which is the "
                      "name a fresh install keeps forever because SELF_NAME's "
                      "server default is empty and the fetch only assigns when "
                      "the value is truthy")

    def test_a_blank_name_produces_a_greeting_with_no_name(self):
        """The other half: the seed is only safe because greeting("") is safe.
        Executed, not read — a seed fix plus a greeting that prints "Good
        morning, undefined" is not a fix."""
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        js = _read("app", "ask.js")
        at = js.index("function greeting(")
        fn = js[at:js.index("\n}\n", at) + 3]
        script = fn + '\nprocess.stdout.write(JSON.stringify([greeting(""), greeting("  "), greeting("Ada Lovelace")]));'
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        blank, spaces, named = json.loads(out.stdout)
        for got in (blank, spaces):
            self.assertRegex(got, r"^Good (morning|afternoon|evening)$",
                             f"a blank name produced {got!r}")
        self.assertRegex(named, r"^Good (morning|afternoon|evening), Ada$",
                         "a real name should still be used, and only the first part")


if __name__ == "__main__":
    unittest.main()


class TestTheWhatsAppPhotoImportSurvivesLosingItsRow(unittest.TestCase):
    """NC-030. When WhatsApp is imported, its photos are imported too. The photo
    cache had its own row directly under WhatsApp's, both claiming to sync
    themselves daily.

    Retiring that row is only safe because the import does not depend on it: the
    cache lives inside WhatsApp's group container, so detect_sources() finds it
    and the once-a-day boot pass that already reads that container runs it. Two
    ways to get this wrong, and both are gated here — dropping the registry entry
    (which would delete the capability, not the control), and filtering only the
    row list while the *finding* still builds a row of its own.

    NC-236. The folding now happens on the server (crm/api_ledger.py), which the
    rebuilt page draws as it is sent, so the gates read that instead of the
    old page's JavaScript."""

    def setUp(self):
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import api_ledger
        self.ledger = api_ledger
        self.server = _read_crm("api_ledger.py")
        self.importers = _read_crm("importers.py")

    def test_the_row_is_gone_from_both_the_list_and_the_findings(self):
        self.assertEqual(self.ledger.PART_OF.get("whatsapp_photos"), "whatsapp",
                         "whatsapp_photos is not declared as part of the WhatsApp row")
        keys = [r["key"] for r in self.ledger.ROWS]
        self.assertNotIn("whatsapp_photos", keys,
                         "the page still offers the folded row as a row of its own")
        whatsapp = next(r for r in self.ledger.ROWS if r["key"] == "whatsapp")
        self.assertIn("whatsapp_photos", whatsapp["sources"],
                      "the WhatsApp row no longer answers for the photo source")
        # The findings matter separately: the needs list is built straight from
        # `found`, so a Media/Profile folder that merely looked newer would put the
        # row back under "Needs you" with the row list in place.
        needs = _py_function(self.server, "_file_needs")
        self.assertIn('f.get("source") not in PART_OF', needs,
                      "the found files are not filtered, so the folded source can "
                      "still reappear as a need that asks for a click")

    def test_the_capability_and_its_automatic_trigger_are_untouched(self):
        self.assertIn('"whatsapp_photos": ImporterSpec(', self.importers,
                      "the registry entry was deleted, which removes the capability "
                      "rather than the control — run_autosync builds its argv from it")
        self.assertIn('"whatsapp_photos.py"', self.importers)
        # The trigger is path shape, not the registry: _autosync_pending selects
        # findings under WA_CONTAINER, and Media/Profile is one of them.
        self.assertIn('os.path.join(WA_CONTAINER, "Media", "Profile")', self.importers,
                      "detect_sources no longer finds the photo cache inside the "
                      "container, so nothing imports it automatically any more")

    def test_a_row_reports_only_the_photos_it_put_there_itself(self):
        """The photo rows read the count of contacts whose photo came from THAT
        source. Folding the global number onto WhatsApp's row would have had
        WhatsApp claim credit for every face LinkedIn and the vCard contributed."""
        counts = _py_function(self.server, "_photos_by_source")
        self.assertIn("GROUP BY photo_source", counts,
                      "no per-source photo attribution, so the count is the pile")
        self.assertIn('ctx["photos"].get("whatsapp", 0)',
                      _py_function(self.server, "_row_whatsapp"),
                      "the WhatsApp row does not read its own photo count")
        self.assertNotIn("photos_count", self.server,
                         "the global photo total is threaded into the rows")


class TestAnAutomaticSourceStopsItsOwnFilesAskingForAClick(unittest.TestCase):
    """NC-045. A calendar that syncs by itself should never ask for a click — and
    the page really was asking. Two .ics exports could sit under "Needs you" while
    `calendar_google` had already synced on its own.

    The existing dedup could not catch it: it is keyed by the finding's own
    source, so it only ever suppressed a manual copy of the SAME source, and the
    link-fed calendars are a different registry key entirely. detect_sources()
    reports files, so they are not in `found` at all and there was nothing to
    compare against.

    NC-236. The decision moved to the server (crm/api_ledger.py: COVERED_BY and
    _file_needs), which the rebuilt page draws as sent. Three ways to get this
    wrong, one gate each: no sibling map, a suppression that also hides the
    source, and a comparison of two different date shapes."""

    def setUp(self):
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import api_ledger
        self.ledger = api_ledger
        self.server = _read_crm("api_ledger.py")
        self.needs = _py_function(self.server, "_file_needs")
        self.importers = _read_crm("importers.py")

    def test_the_hand_export_knows_which_sources_cover_it(self):
        """Asserts the RULE, not the literal.

        Pinning the exact one-line map would break the day another calendar route
        is added — against correct code, which is the failure mode that teaches
        people to edit tests without reading them. What matters is that the
        hand-exported .ics knows something else covers it, and that every key
        names a real source.
        """
        as_map = {k: list(v) for k, v in self.ledger.COVERED_BY.items()}
        self.assertIn("calendar", as_map,
                      "nothing tells the page that the link-fed calendars are "
                      "the same events as the .ics it is asking the owner to click")
        self.assertTrue(as_map["calendar"],
                        "the hand-exported .ics is covered by nothing")
        # Every key AND every value has to be a real source, or the map silently
        # covers nothing — a typo here fails open and is invisible.
        for key in set(as_map) | {v for vals in as_map.values() for v in vals}:
            self.assertIn(f'"{key}": ImporterSpec(', self.importers,
                          f"COVERED_BY names {key}, which is not in the registry")
        # No source may cover itself: that would suppress a row on its own
        # freshness and take it off the page for good.
        for key, vals in as_map.items():
            self.assertNotIn(key, vals, f"{key} covers itself")

    def test_the_suppression_is_applied_where_the_click_is_asked_for(self):
        self.assertIn('covered_since(f["source"])', self.needs,
                      "the sibling check is not in the loop that builds the "
                      "'Needs you' list, so it suppresses nothing")
        covered = self.needs[self.needs.index("def covered_since("):]
        covered = covered[:covered.index("cands = []")]
        self.assertIn('"last_run"', covered,
                      "the sibling's freshness is not read from its last import")

    def test_a_covered_source_falls_through_instead_of_vanishing(self):
        """Suppressing the only findings a source had must not take the source off
        the page: that is how a user concludes a capability does not exist
        ([[NC-015]]). On the rebuilt page every source is always a row, and the
        suppression only stops the ASK under Needs you."""
        rows = {r["key"] for r in self.ledger.ROWS}
        covered = set(self.ledger.COVERED_BY) | {
            v for vals in self.ledger.COVERED_BY.values() for v in vals}
        for key in sorted(covered):
            with self.subTest(source=key):
                self.assertTrue(
                    any(key in r["sources"] or r["key"] == key
                        for r in self.ledger.ROWS),
                    f"{key} is named in COVERED_BY but has no row to fall back to "
                    f"(rows: {sorted(rows)})")
        # The suppression is `continue` inside the candidates loop, never a filter
        # on the found list that would also drop the row's own finding.
        i = self.needs.index("sib = covered_since(")
        self.assertIn("continue", self.needs[i:i + 200])

    def test_the_two_date_shapes_are_cut_to_the_same_length(self):
        """`mtime` is "2025-03-06" and `last_run` is
        "2025-03-21T09:00:00+00:00". Comparing them as raw strings works by
        accident of prefix and stops working the day either format moves."""
        i = self.needs.index("sib = covered_since(")
        block = self.needs[i:i + 240]
        self.assertEqual(block.count("[:10]"), 2,
                         "one side of the comparison is not cut to a date")


class TestEverySourceRowHasAnIcon(unittest.TestCase):
    """A source added to the registry with no SRC_ICON entry renders a blank
    gap where every sibling row has a glyph. `Icon` returns null for an unknown
    name — deliberately, so a typo cannot crash the page — which means the
    failure is invisible to every other test and to the person who added the
    source.

    A row can ship iconless this way (NC-042's `calendar_local` is a case):
    between iMessage and WhatsApp rows that both have a glyph, nothing but a
    test would notice.

    Compared against the real registry rather than a pinned list, so a source
    added tomorrow is covered without touching this test.
    """

    def test_every_registry_source_has_an_icon(self):
        import re
        from crm import importers
        reg = [k for k in importers.REGISTRY] if hasattr(importers, "REGISTRY") else []
        self.assertTrue(reg, "could not read the importer registry")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "static", "app", "icons.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        block = js[js.index("export const SRC_ICON = {"):]
        block = block[:block.index("};")]
        have = set(re.findall(r"(\w+):\s*\"", block))
        missing = sorted(k for k in reg if k not in have)
        self.assertEqual(
            missing, [],
            f"these sources render with no icon: {missing}. Add them to "
            "SRC_ICON in static/app/icons.js — Icon returns null for an "
            "unknown name, so the row just shows an empty gap.")

    def test_every_icon_name_used_by_the_map_actually_exists(self):
        """The other half: a SRC_ICON entry pointing at an icon that is not in
        ICONS is equally silent. `<${Icon} name="play" />` renders nothing at
        all — which is how a Preview button could ship with no glyph."""
        import re
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "static", "app", "icons.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        icons_block = js[js.index("const ICONS = {"):js.index("export const SRC_ICON")]
        defined = set(re.findall(r"^\s{2}([a-zA-Z_][a-zA-Z0-9_]*):", icons_block, re.M))
        src_block = js[js.index("export const SRC_ICON = {"):]
        src_block = src_block[:src_block.index("};")]
        used = set(re.findall(r":\s*\"(\w+)\"", src_block))
        unknown = sorted(u for u in used if u not in defined)
        self.assertEqual(
            unknown, [],
            f"SRC_ICON points at icons that do not exist: {unknown}")


class TestTheFdaPanelNamesEverySourceTheHelperReports(unittest.TestCase):
    """The helper copies four things — iMessage, WhatsApp, Contacts, Calendar
    (packaging/src/imessage_sync.swift). The permission panel had icons for two
    of them and a summary line that named two of them, both written when the
    helper only did two and neither updated when NC-042 added the other two.

    That is not cosmetic on this panel specifically: it is the one line that
    tells someone what Full Disk Access is actually buying them, so
    under-reporting it understates the whole feature.

    Both halves are read out of the Swift source rather than restated, so adding
    a fifth source to the helper fails this test until the panel keeps up.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _helper_labels(self):
        """The REAL source list — `let sources: [Source] = [ ... ]` — and not
        every `label:` in the file. There is a fifth, "SelfTest", built inside
        --selftest against a temp tree; it never reaches a real run's report, so
        requiring an icon for it would be requiring an icon for a test fixture.
        Slicing the declaration rather than filtering that one name by hand
        means a future fixture cannot break this either."""
        import re
        with open(os.path.join(self.ROOT, "packaging", "src",
                               "imessage_sync.swift"), encoding="utf-8") as fh:
            swift = fh.read()
        start = swift.index("let sources: [Source] = [")
        end = swift.index("\n]", start)
        labels = re.findall(r'label:\s*"([^"]+)"', swift[start:end])
        self.assertTrue(labels, "no Source labels found in the helper's list")
        self.assertNotIn("SelfTest", labels,
                         "the slice picked up the self-test fixture")
        return sorted(set(labels))

    def test_every_helper_source_has_an_icon_in_the_panel(self):
        import re
        with open(os.path.join(self.ROOT, "static", "app", "fda.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        block = js[js.index("const SRC_ICON = {"):]
        block = block[:block.index("};")]
        have = set(re.findall(r"(\w+):\s*\"", block))
        missing = [l for l in self._helper_labels() if l not in have]
        self.assertEqual(
            missing, [],
            f"the helper reports {missing} but the panel has no icon for them, "
            "so those rows render with a blank gap")

    def test_the_confirmation_line_is_not_a_hardcoded_pair_of_names(self):
        """The specific regression: a sentence listing source names by hand.
        It was 'Messages and WhatsApp refresh themselves' and stayed that way
        through two new sources."""
        with open(os.path.join(self.ROOT, "static", "app", "fda.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        # Comments explain the old copy on purpose, so read only real code.
        code = "\n".join(ln for ln in js.splitlines()
                         if not ln.lstrip().startswith("//"))
        self.assertNotIn(
            "Messages and WhatsApp refresh themselves", code,
            "the confirmation line hardcodes two source names again")
        self.assertNotIn(
            "neither Messages nor WhatsApp", code,
            "the absent-state line hardcodes two source names again")
        self.assertIn(
            "status.sources", code,
            "the confirmation line no longer derives its names from what the "
            "helper actually reported")

    def test_the_offer_names_every_source_the_helper_copies(self):
        """NC-194. The offer shown before the helper is installed said "Keep
        Messages and WhatsApp up to date" and "copies those two databases"
        through two new sources — the same regression as the confirmation line,
        on the screen where someone decides whether to give the permission."""
        from unittest import mock
        from tests.helpers import importers
        import api_fda
        with mock.patch.object(importers, "imsg_helper_installer", lambda: "/x/i.sh"), \
             mock.patch.object(importers, "imsg_helper_installed", lambda: False), \
             mock.patch.object(importers, "imsg_helper_report", lambda: {}):
            status = api_fda.handle_fda_status()
        self.assertEqual(status["state"], "not_installed")
        self.assertEqual(sorted(status["copies"]), self._helper_labels(),
                         "the offer's list is not the list the helper copies")

        with open(os.path.join(self.ROOT, "static", "app", "fda.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        start = js.index('if (status.state === "not_installed")')
        offer = js[start:js.index('if (status.state === "outdated")', start)]
        code = "\n".join(ln for ln in offer.splitlines()
                         if not ln.lstrip().startswith("//"))
        self.assertIn("status.copies", code,
                      "the offer no longer takes its names from the server")
        for stale in ("Keep Messages and WhatsApp", "two databases",
                      "your Messages folder"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, code)

    # NC-236: "the offer counts every helper source as a reason to show" went with the old Data
    # page; that page hid the offer from a Mac with no use for it (fdaRelevant), and the new
    # page deliberately always lists the Automatic copying row, so nothing is left to hide.

class TestWhereYourDataComesFromIsOnTheDataPage(unittest.TestCase):
    """NC-105. Two blocks sat in Settings that belong on the Data page. The
    reason generalises past this one move: **Settings is where you CHANGE
    something.** The install figures and the list of sources are
    not things you change, and the page that answers "what do I have and where
    did it come from" is Data.

    NC-236. The components those gates named (DataStats, SourceGrid) went with
    the old Data page, and so did "neither component knows which page it is on"
    and "the stat grid is defined in one stylesheet": there is no component or
    grid left to check. What still matters is the split itself, and that the old
    class names stay gone.

    These gates exist because a component that renders in the wrong place still
    renders, still looks right in a screenshot of itself, and no other test in
    this suite can tell.
    """

    def setUp(self):
        self.data = js_code(_read("app", "data.js"))
        self.settings = js_code(_read("app", "settings.js"))

    def test_the_data_page_draws_the_install_and_the_sources(self):
        for needle in ('"/api/data/ledger"', "<${SourceRow}", "<${Install}"):
            self.assertIn(needle, self.data,
                          f"the Data page no longer has {needle}")

    def test_settings_shows_neither(self):
        for needle in ("/api/data/ledger", "/api/sources", "<${SourceRow}", "<${Install}",
                       "DataStats", "SourceGrid"):
            self.assertNotIn(
                needle, self.settings,
                f"{needle} is in Settings. It is not a thing you change, and "
                "rendering it in both places gives one question two answers on "
                "two pages — which is what NC-087 is about")

    def test_the_old_settings_class_names_are_gone_everywhere(self):
        """Renamed with the move rather than left behind: a class called
        `settings-diag-*` that only ever renders on the Data page sends the next
        reader to the wrong stylesheet."""
        # CSS comments stripped for the same reason `js_code` exists for
        # JS: a comment that RECORDS this rename names the old class, and a gate
        # reading raw source trips on its own explanation.
        css = lambda name: re.sub(r"/\*.*?\*/", "", _read("css", name), flags=re.S)
        for where, text in (("settings.js", self.settings),
                            ("data.js", self.data),
                            ("ui.css", css("ui.css")),
                            ("view-settings.css", css("view-settings.css")),
                            ("view-data.css", css("view-data.css"))):
            self.assertNotIn("settings-diag", text,
                             f"{where} still uses the pre-NC-105 class name")


class TestSourceGridCannotContradictItself(unittest.TestCase):
    """NC-101. The per-source display, and the ways its first render lied.

    NC-236. The grid (sources.js) went with the old Data page, and with it the
    gates about its own internals: the `mode` each group declared, the function
    that chose a sentence per mode, the rule that only the graded mode may call a
    source behind, the footer built as one expression, and the caveat in state().
    The new page does not group sources by how they start, and it has no footer
    or state() function; each row's state is one sentence the server wrote.
    Two rules outlive the grid, and are kept here.
    """

    def setUp(self):
        self.data = js_code(_read("app", "data.js"))
        self.server = _read_crm("api_ledger.py")

    def test_every_tone_the_server_sends_has_a_rule_in_the_stylesheet(self):
        """A modifier class with no CSS renders invisible while every test
        stays green — the exact escape of .li-seg.is-due in NC-093. Derived from
        the server's own tones, so a new tone must bring its colour with it."""
        tones = set(re.findall(r'"tone":\s*"([a-z]+)"', self.server))
        self.assertGreaterEqual(len(tones), 5,
                                "found no row tones — this gate would pass vacuously")
        css = _read("css", "view-ledger.css")
        missing = sorted(t for t in tones if f".lg-dot.is-{t}" not in css)
        self.assertEqual(missing, [], f"tones with no colour: {missing}")

    def test_a_caveat_the_endpoint_publishes_is_actually_shown(self):
        """crm/api_sources.py knows engagement's stamp understates — it runs
        after every import without restamping. Publishing that and then not
        rendering it is worse than not knowing: the row reads "18 days ago"
        with the correction sitting unused in the payload."""
        self.assertIn('"last_run_caveat": src.get("last_run_caveat", "")', self.server,
                      "the ledger no longer passes the caveat on")
        self.assertRegex(
            self.data, r"row\.in_use \? d\.last_run_caveat : \"\"",
            "nothing on the page reads last_run_caveat, so the correction never "
            "reaches the screen")

    def test_it_reads_the_endpoint_rather_than_recomputing_the_answer(self):
        self.assertIn('api("/api/data/ledger")', self.data,
                      "the classification lives in crm/api_ledger.py; a second "
                      "copy here is how the two drift")
        self.assertNotIn("import_synced", self.data)


class TestHtmDoesNotEatTheSpaceBeforeAnInlineTag(unittest.TestCase):
    """htm drops the whitespace around a newline between text and an element, so

        choose the app with
          <b>Member Data Portability</b>

    renders as "choose the app withMember Data Portability". The fix is an
    explicit ${" "} before the tag.

    The data page carries a comment about this ("the space before <code> is
    explicit"), but the space is invisible in the source and only shows up in
    rendered text, so a new paragraph can hit it anyway. A comment in one file is
    not a gate over the others.

    No module has an instance today, so this costs nothing and fails only on a
    genuine new one.
    """

    def test_no_inline_tag_follows_bare_text_across_a_newline(self):
        offenders = []
        for path in sorted(glob.glob(os.path.join(REPO, "static", "app", "*.js"))):
            code = js_code(open(path, encoding="utf-8").read())
            for m in re.finditer(r"[A-Za-z,;)]\s*\n\s+<(?:b|code|i|em|strong)>", code):
                line = code[:m.start()].count("\n") + 1
                snippet = " ".join(code[max(0, m.start() - 46):m.end()].split())
                offenders.append(f"{os.path.basename(path)}:{line}  …{snippet}")
        self.assertEqual(offenders, [], "\n".join([
            "", "htm will render these two words glued together. Put an explicit",
            '${" "} before the tag:', *offenders]))

    def test_no_value_follows_bare_text_across_a_newline_unmarked(self):
        """NC-102: the same trap, and the far commoner half of it.

        The gate above only ever looked at <b>/<code>/<i>/<em>/<strong>. Sentences
        on the Intake page rendered glued, because what followed the newline was a
        ${...} value rather than a tag:

            Not saved:
              ${p.facts_declined.map(d => d.field).join(", ")}

        rendered "Not saved:email, phone".

        The rule is flat rather than clever ON PURPOSE. Fifteen sites match the
        shape and nine of them were already fine, because the value they
        interpolate begins with its own space — `${" · "}`, `` ${` The model…`} ``.
        A gate that tried to work out which was which would have to parse the
        expression, and would be wrong at exactly the moment someone edits the
        expression. So both halves must SAY which they are:

            ${" "}   a space is wanted here and htm would eat it
            ${ ""}   no space wanted — the value brings its own

        `${ ""}` was already the idiom in ask.js, dataview.js, linkedin.js and
        people.js for a deliberate newline; this only extends it to text runs.
        """
        marked = re.compile(r'\$\{(?:" "|\s*"")\}\s*$')
        # A backtick opens a template (no text run yet), `>` ends a tag, `}`
        # ends a previous value, `"` closes an attribute. None of those is text,
        # so none of them ever had a space to lose.
        boundary = re.compile(r'[^\s>}`"][ \t]*\n[ \t]*\$\{')
        offenders = []
        for path in sorted(glob.glob(os.path.join(REPO, "static", "app", "*.js"))):
            raw = open(path, encoding="utf-8").read()
            # Comments blanked, not removed, so the reported line number is the
            # line a person can go and open.
            code = re.sub(r"/\*.*?\*/",
                          lambda m: re.sub(r"[^\n]", " ", m.group(0)), raw, flags=re.S)
            code = re.sub(r"(?<![:/])//[^\n]*",
                          lambda m: " " * len(m.group(0)), code)
            lines = raw.split("\n")
            for m in boundary.finditer(code):
                n = code[:m.start()].count("\n")
                if marked.search(lines[n]):
                    continue
                offenders.append(f"{os.path.basename(path)}:{n + 1}  {lines[n].strip()[-64:]}")
        self.assertEqual(offenders, [], "\n".join([
            "", "htm eats the space on both sides of this newline. End the line",
            'with ${" "} if a space is wanted, or ${ ""} if the value below',
            "already starts with one:", *offenders]))

    def test_no_bare_text_follows_a_closing_tag_across_a_newline(self):
        """NC-182: the mirror, and the half that was never gated.

        Both tests above look at what comes AFTER the newline — a tag, or a
        value. htm eats the space on BOTH sides of it, so the other arrangement
        loses a space just as surely:

            and under <b>Audience</b>
              choose External

        renders "under Audiencechoose External". So does "the <b>Email
        (.mbox)</b>\n source imports", which renders "(.mbox)source imports".
        Neither shows up in the source, and no other test looks at rendered text.

        The fix is the same idiom pointing the other way: `${" "}` after the
        closing tag, or keep the first following word on the tag's own line.
        Both halves of a sentence have to be written with this in mind, which is
        the argument for gating both halves rather than trusting the habit.
        """
        marked = re.compile(r'\$\{(?:" "|\s*"")\}\s*$')
        offenders = []
        for path in sorted(glob.glob(os.path.join(REPO, "static", "app", "*.js"))):
            raw = open(path, encoding="utf-8").read()
            code = js_code(raw)
            lines = raw.split("\n")
            for m in re.finditer(
                    r"</(?:b|code|i|em|strong|a|span)>[ \t]*\n[ \t]*[A-Za-z]", code):
                n = code[:m.start()].count("\n")
                if marked.search(lines[n]):
                    continue
                snippet = " ".join(code[max(0, m.start() - 40):m.end()].split())
                offenders.append(f"{os.path.basename(path)}:{n + 1}  …{snippet}")
        self.assertEqual(offenders, [], "\n".join([
            "", "htm eats the space after this closing tag too. End the line",
            'with ${" "}, or keep the next word on the tag\'s own line:',
            *offenders]))

    def test_no_html_entity_is_written_into_a_template(self):
        """NC-182.

        htm does not decode HTML entities in template text. `APIs &amp;
        Services` renders as the literal five characters `&amp;`, and the app is
        written entirely in these templates.

        The whole answer is to type the character. `&` is `&`, and a `<` that
        genuinely needs escaping does not belong in prose — it belongs in a
        `${...}` value, where it is text and not markup.
        """
        offenders = []
        for path in sorted(glob.glob(os.path.join(REPO, "static", "app", "*.js"))):
            raw = open(path, encoding="utf-8").read()
            code = js_code(raw)
            lines = raw.split("\n")
            for m in re.finditer(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,9}|#[0-9]{1,5}|#x[0-9a-fA-F]{1,5});",
                                 code):
                n = code[:m.start()].count("\n")
                offenders.append(f"{os.path.basename(path)}:{n + 1}  "
                                 f"{m.group(0)}  in: {lines[n].strip()[:60]}")
        self.assertEqual(offenders, [], "\n".join([
            "", "htm renders these as their literal characters, ampersand and",
            "all. Type the character itself instead:", *offenders]))


class TestTheProvenanceRowShowsWhatTheServerSent(unittest.TestCase):
    """NC-081. The trail under an Ask answer is the user's only view of what was
    actually searched, and it silently dropped two fields the server sends.

    `total` has been in every trail entry since the per-call caps landed — the
    comment on chat.py's _tool_total literally says "the trail carries both and
    the UI can say which it is showing" — and this row rendered `matched` alone.
    So a search that matched hundreds of people and returned 80 read "80 matched": a
    wrong number, not a rounded one. `label` is for message_stats, which reports
    what exists rather than matching anything, and would otherwise read
    "0 matched" beside an answer built from a large message archive.

    Gated at source because no test executes this component, and a mutation that
    reverts the row to `${t.matched} matched` would pass the entire suite.
    """

    def _row(self):
        js = js_code(_read("app", "ask.js"))
        at = js.index('class="prov-row"')
        return js[at:at + 400]

    def test_the_row_prefers_the_servers_own_label(self):
        self.assertIn("t.label", self._row(),
                      "message_stats sends a `label` (what the archive holds) "
                      "because it matches nothing; without it the row reads "
                      "'0 matched' for a successful call")

    def test_the_row_shows_the_total_when_the_cap_hid_some(self):
        row = self._row()
        self.assertIn("t.total", row,
                      "chat.py sends `total` on every trail entry and this row "
                      "throws it away, so a capped search reports the size of "
                      "the page as though it were the size of the result")
        self.assertRegex(
            row, r"t\.total\s*>\s*t\.matched",
            "the total is rendered without comparing it to matched, so an "
            "uncapped search says 'N of N matched'")

    def test_the_server_still_sends_both_fields(self):
        """The other half of the contract. A gate on the UI alone would keep
        passing if chat.py stopped populating these."""
        py = _read_py("chat.py")
        self.assertRegex(py, r'entry\["label"\]\s*=',
                         "nothing sets the trail label any more, so the UI's "
                         "fallback is the only path and this gate is theatre")
        self.assertRegex(py, r'"total":\s*_tool_total\(',
                         "trail entries no longer carry a total")


class TestTheProvenanceHeaderNamesTheMessageStore(unittest.TestCase):
    """NC-081. The one line in the app that says what Ask can reach.

    It read "Searched N contacts · M conversations", and those conversations are
    the chunks built for the embedding model. The message tools search the
    message rows underneath them — several times as many — and nothing said
    so. A user who cannot see that their texts are searchable does not ask about
    them.

    Zero is hidden rather than shown: "· 0 messages" on an install with nothing
    imported reads as a broken feature, not an empty one.

    The gate is anchored on the declaration alone, `const counts = `, and not on
    the text after it (NC-155). An exact match on `const counts = html` plus a
    backtick would break the moment the line becomes `const counts = searched ?
    html` plus a backtick — the counts are conditional, because a turn restored
    from history has a price and no `searched`. Rewording the condition around
    the declaration must not trip a gate about what it protects.
    """

    @staticmethod
    def _counts_block():
        js = js_code(_read("app", "ask.js"))
        at = js.index("const counts = ")
        return js[at:js.index("`;", at)]

    def test_the_header_shows_the_message_count(self):
        block = self._counts_block()
        self.assertIn("searched.messages", block,
                      "the provenance header names only contacts and chunks, so "
                      "the message archive Ask now searches is invisible")

    def test_zero_messages_is_hidden_not_printed(self):
        block = self._counts_block()
        self.assertRegex(
            block, r"\(searched\.messages\s*\?\?\s*0\)\s*>\s*0",
            "the count is rendered unconditionally, so an install with no "
            "archive reads '· 0 messages' as a failure")

    def test_the_server_actually_sends_it(self):
        py = _read_py("api_ask.py")
        at = py.index("def _chat_searched(")
        body = py[at:py.index("\ndef ", at + 10)]
        self.assertIn("COUNT(*) AS n FROM messages", body,
                      "_chat_searched does not count the message rows, so the "
                      "UI's number is always zero")
        self.assertRegex(py, r'"messages":\s*messages',
                         "the count is taken and then not returned")


class TestThePendingLineSaysItIsAliveWithoutAnimating(unittest.TestCase):
    """NC-083. The animation disappears when the window is switched away: a question
    can show with nothing under it.

    The pending indicator's ONLY liveness signal was a CSS pulse, and the copy
    was written to lean on that: ask.js's copy rules removed the trailing
    ellipsis *because* the line animates. So anywhere the animation does not
    run, the screen reads "Searching your network", perfectly still, which is
    indistinguishable from a dead request.

    Two places it does not run. With reduced motion on it was switched off
    entirely — `view-ask.css` loads after `ui.css`, and its `animation: none`
    reset the whole shorthand, wiping the `animation-iteration-count: 1` that
    ui.css sets for this exact selector and whose own comment says why ("the
    shimmer collapsing to a static bar loses the 'still loading' signal"). A
    deliberate decision, silently defeated by load order. And in the packaged
    app WKWebView suspends the web process when its window is occluded.

    The fix is a counted second, which does not depend on anything rendering
    continuously — and which earns its place anyway now that NC-080 raised the
    ceiling to 180 seconds: a still line for three minutes tells you nothing
    about whether to keep waiting.
    """

    def _ask(self):
        return js_code(_read("app", "ask.js"))

    def _pending_block(self):
        js = self._ask()
        at = js.index("prov-searching")
        return js[max(0, at - 300):at + 500]

    def test_the_pending_line_carries_a_number(self):
        self.assertIn("prov-elapsed", self._pending_block(),
                      "the pending indicator has no liveness signal other than "
                      "the CSS pulse, so it looks identical to a dead request "
                      "wherever the animation does not run")

    def test_the_number_is_derived_from_a_timestamp_not_incremented(self):
        """This is the whole point. A counter that ++s on a timer resumes from
        where it fell asleep and reports the wrong figure after the window comes
        back; a value computed from the start time is correct on the first tick
        however long the process was suspended."""
        js = self._ask()
        self.assertRegex(
            js, r"Date\.now\(\)\s*-\s*startedAt",
            "the elapsed seconds are not computed from the start timestamp, so a "
            "throttled or suspended timer makes the number lie")
        at = js.index("const elapsed")
        self.assertNotRegex(
            js[at:at + 200], r"elapsed\s*\+\s*1|elapsed\+\+",
            "the elapsed value is being incremented rather than derived")

    def test_the_clock_starts_when_the_question_goes_out(self):
        js = self._ask()
        at = js.index("setPending(true)")
        self.assertIn("setStartedAt(", js[at:at + 300],
                      "the timestamp is never set on send, so the counter cannot "
                      "be right")

    def test_the_number_is_outside_the_pulsing_element(self):
        """The pulse animates opacity. A number fading in and out is harder to
        read than a still one, and this element is the thing that has to stay
        readable at every instant."""
        js = self._ask()
        at = js.index('class="prov-searching"')
        span = js[at:js.index("</span>", at)]
        self.assertNotIn("prov-elapsed", span,
                         "the seconds sit inside the pulsing span, so the one "
                         "reliable signal fades out twice a second")

    def test_reduced_motion_does_not_wipe_the_whole_animation_shorthand(self):
        css = _read("css", "view-ask.css")
        at = css.index("prefers-reduced-motion: reduce")
        block = css[at:css.index("}", css.index("}", at) + 1) + 1]
        self.assertNotRegex(
            block, r"\.prov-searching\s*\{[^}]*animation:\s*none",
            "`animation: none` resets the whole shorthand, so it also wipes the "
            "animation-iteration-count ui.css sets for this selector — which is "
            "how reduced-motion users ended up with no liveness signal at all")
        self.assertIn("animation-iteration-count", block,
                      "reduced motion should mean ONE gentle fade, not none — see "
                      "the reasoning in ui.css above its own rule for this class")

    def test_ui_css_still_owns_the_reduced_motion_intent(self):
        """The other half. If ui.css's rule goes away, view-ask.css's
        iteration-count is the only one left and this gate stops meaning
        anything."""
        ui = _read("css", "ui.css")
        # NC-170: the @media rule's own braces. 900 characters from the query
        # reached past the rule into whatever follows it in the file.
        self.assertIn(".prov-searching",
                      braced_block(ui, "prefers-reduced-motion: reduce"),
                      "the reduced-motion rule no longer covers .prov-searching, "
                      "so view-ask.css's iteration-count is the only thing left")

    def test_the_pending_row_does_not_redefine_a_shared_class(self):
        """Laying the row out with a second `.prov` rule would silently reflow
        every provenance block in the view, because .prov is already a COLUMN
        flex at the top of the same file. Two rules for one selector in one file
        is how that happens."""
        css = _read("css", "view-ask.css")
        bare = re.findall(r"^\.prov\s*\{", css, re.M)
        self.assertEqual(len(bare), 1,
                         f"`.prov` is defined {len(bare)} times in view-ask.css; "
                         f"the later one silently overrides the earlier")

    def test_the_pending_row_sets_its_own_direction(self):
        """And it must set flex-direction explicitly, or it inherits `column`
        from .prov and the seconds land on their own line — two lines to say one
        thing."""
        css = _read("css", "view-ask.css")
        at = css.index(".prov-pending")
        block = css[at:css.index("}", at)]
        self.assertIn("flex-direction: row", block)


class TestTheListsPaneDoesNotCountForItself(unittest.TestCase):
    """NC-089. The detail pane recomputed the pipeline counts in the browser from
    the member rows it was holding, while the rail BESIDE IT — same flex row, no
    scrolling between them — read the server's. The two rules differed: the
    server's `replied` is cumulative (attending implies replied), the browser's
    counted only the literal 'replied' status. One member at 'attending' put
    "1 replied · 1 going" next to "1 going", in the same four words, for the same
    list.

    The Python tests in test_api_lists.py assert the two ENDPOINTS agree. They
    cannot see a browser that ignores both and counts again — which is what the
    bug actually was. Hence this gate.
    """

    def setUp(self):
        self.js = _read("app", "lists.js")

    def test_the_pane_renders_the_counts_the_server_sent(self):
        self.assertIn("list.counts", self.js,
                      "the detail pane must read the server's counts")

    def test_the_pane_does_not_add_up_the_member_rows(self):
        self.assertNotRegex(
            self.js, r"counts\[m\.list_status\]\s*\+=",
            "the browser is counting members again; the rail beside it reads the "
            "server, so any second definition here shows two numbers at once")
        self.assertNotRegex(
            self.js, r"members\.forEach\([^)]*counts",
            "same recount, written another way")


class TestTheHealthyLineSaysHowMuchItRead(unittest.TestCase):
    """NC-093. The permission panel's green one-liner is the state the silent
    import failure can happen in — the helper reports every source `current`, the
    line goes green, and Apple Contacts may have matched a store holding almost
    nobody. A green line with no number cannot tell that apart from a working
    sync; a line that names the count can.

    So the count has to render in the `ok` branch specifically, not only in the
    panel states that appear when something is already wrong.
    """

    def setUp(self):
        self.js = _read("app", "fda.js")

    def test_the_ok_branch_renders_the_counts(self):
        at = self.js.index('if (status.state === "ok"')
        branch = self.js[at:self.js.index("</div>`;", at)]
        # Pin the EXPRESSION, not the string: `${false && html...` and swapping
        # the filter to `x.files` would both leave the searched-for text in place
        # while breaking the behaviour.
        self.assertIn('${read && html`<span class="fda-line-read">', branch,
                      "the healthy line does not show what each source read, "
                      "which is the only state the failure this fixes occurs in")
        self.assertIn('typeof x.matched === "number"', branch,
                      "the FILTER must be on `matched`: it is the count present "
                      "when a source is `current`, and `files` is absent in "
                      "exactly that case")

    def test_the_count_is_styled_as_quiet_supporting_text(self):
        css = _read("css", "view-data.css")
        at = css.index(".fda-line-read")
        block = css[at:css.index("}", at)]
        self.assertIn("var(--faint)", block)
        self.assertIn("tabular-nums", block, "digits in a row should line up")


class TestALockedCopySaysWhatToDoInsteadOfAskingForAnEmail(unittest.TestCase):
    """NC-099. A 401 must not pop a magic-link sign-in overlay — an email field
    posting to /api/auth/magic, a route that no longer exists. The one shape that
    can produce a 401 is README's home server, where there is no email to enter
    and nothing to send one.

    Against a server bound off loopback with APP_TOKEN set, an overlay reading
    "Enter your email to sign in" would sit over a People table reading "0
    people", with nothing on screen mentioning the token.

    The token plumbing is live, not dead: open the address with ?token=... and
    the page loads its people; without it every call 401s. So these gates protect
    two different things — that the dead half stays dead, and that the live half
    is not deleted with it the next time someone reads "cloud" in this file.
    """

    def setUp(self):
        # Comments stripped, or every gate here trips on the comment that
        # EXPLAINS it — the whole reason js_code exists (tests/helpers.py).
        self.api = js_code(_read("app", "api.js"))
        self.main = js_code(_read("app", "main.js"))

    # ── the dead half stays dead ────────────────────────────────────────────
    def test_nothing_posts_to_the_hosted_tiers_auth_route(self):
        for dead in ("/api/auth/magic", "/api/auth/verify", "/api/subscription"):
            self.assertNotIn(dead, self.api,
                             f"{dead} was only ever served by a server that no "
                             "longer exists; a call to it can only fail")

    def test_the_locked_message_is_not_a_credential_form(self):
        """The specific regression: any input the person is invited to type a
        secret into, on a page whose only possible recipient no longer exists."""
        locked = self.api[self.api.index("function _showLocked"):]
        locked = locked[:locked.index("\n}")]
        for shape in ("<input", "type=\"email\"", "type=\"password\""):
            self.assertNotIn(shape, locked,
                             "the locked overlay must tell the person where the "
                             "token goes, not collect one")

    def test_the_locked_message_names_the_token_and_the_file_it_lives_in(self):
        locked = self.api[self.api.index("function _showLocked"):]
        locked = locked[:locked.index("\n}")]
        self.assertIn("?token=", locked)
        self.assertIn("APP_TOKEN", locked)
        self.assertIn("secrets/.env", locked)

    def test_the_locked_overlay_never_builds_html_from_the_address_bar(self):
        """`location.host` is attacker-influenced in exactly the setup this
        overlay appears in. It is set with textContent, never interpolated into
        the innerHTML template."""
        locked = self.api[self.api.index("function _showLocked"):]
        locked = locked[:locked.index("\n}")]
        # The template literal only, from `innerHTML =` to its closing backtick.
        tpl = locked[locked.index("innerHTML"):]
        tpl = tpl[:tpl.index("`;") + 2]
        for live in ("location.host", "location.pathname", "${here}"):
            self.assertNotIn(live, tpl,
                             "the address bar must not reach innerHTML")
        self.assertIn('.textContent = location.host', locked)

    # ── the live half stays live ────────────────────────────────────────────
    def test_the_token_in_the_address_is_still_stored_and_sent(self):
        """The three steps that make the home-server shape work at all: read
        ?token= on load, keep it, and put it on every later request."""
        self.assertIn('params.get("token")', self.api)
        self.assertIn("setAccessToken(token)", self.api)
        self.assertIn('headers["Authorization"] = `Bearer ${token}`', self.api)

    def test_the_bearer_header_is_gated_on_being_off_loopback(self):
        """Not on a truthy token alone: a stale sessionStorage entry must not
        start decorating requests on the loopback install that needs none."""
        send = self.api[self.api.index("export async function api("):]
        send = send[:send.index("const { timeoutMs")]
        self.assertIn("needsAccessToken()", send)

    def test_the_predicate_tests_the_host_and_not_the_protocol(self):
        """The old isCloudMode() comment claimed "served over HTTPS from a
        non-localhost origin" while the code never looked at the protocol, so
        plain http on a LAN address counted too. The code was right and the
        comment was wrong — this pins the code, so a future reader cannot
        "correct" it to match the old prose and lock out every http LAN user."""
        fn = self.api[self.api.index("export function needsAccessToken"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("location.hostname", fn)
        self.assertNotIn("protocol", fn)
        self.assertNotIn("https", fn)

    # ── and the badge tells the truth ───────────────────────────────────────
    def test_the_badge_does_not_claim_a_cloud(self):
        """It said "Runs in the cloud. Data syncs from your Mac." on any
        non-loopback address, which on the home-server shape is two false
        statements: nothing is in a cloud and nothing syncs."""
        self.assertNotIn("Runs in the cloud", self.main)
        self.assertNotIn("local-dot cloud", self.main)
        dot = self.main[self.main.index("needsAccessToken()"):]
        dot = dot[:dot.index("<${Omnibox}")]
        self.assertIn("local-dot remote", dot)
        self.assertIn("your network", dot)

    def test_the_badge_class_has_a_style(self):
        """A renamed class with no matching rule renders an invisible dot — the
        failure mode a string-only gate would miss."""
        self.assertIn(".local-dot.remote", _read("css", "ui.css"))
        self.assertNotIn(".local-dot.cloud", _read("css", "ui.css"))


# NC-236: TestEveryCoverageSegmentHasAColour checked that each part of the LinkedIn coverage
# bar had a colour. The bar (linkedin.js) went with the old Data page; the new LinkedIn
# profile-facts row says "N of M people with profile facts" as a figure, so there is no bar.


class TestTheEpisodeListIsNeverAskedToWiden(unittest.TestCase):
    """NC-106. The person page's episode LIST must stay that person's own
    history: it is built from their 600 most recent messages, so folding in
    every other voice from their group chats would let one busy thread fill that
    budget and push their one-to-one history off their own page.

    Two halves, and each on its own is not enough. The server refuses
    (test_the_episode_list_is_not_widened), and the client must not ask —
    otherwise the day the server gains the option, the page silently changes.
    """

    def _people_js(self):
        with io.open(os.path.join(REPO, "static", "app", "people.js"),
                     encoding="utf-8") as fh:
            return js_code(fh.read())

    def test_the_episodes_request_does_not_carry_threads(self):
        js = self._people_js()
        i = js.index("/api/contact/${c.id}/episodes")
        # The params for that call are built just above it.
        window = js[max(0, i - 400):i]
        self.assertNotIn("threads", window,
                         "the episode list is asking for whole conversations")

    def test_the_history_request_does_carry_threads(self):
        """The other half: the expansion is where the widening belongs, so if
        this disappears the feature is gone and nothing else would say so."""
        js = self._people_js()
        i = js.index("/api/contact/${c.id}/history")
        window = js[max(0, i - 600):i]
        self.assertIn('params.set("threads", "1")', window,
                      "opening a group episode no longer asks for the "
                      "conversation, so it shows one voice again")

    def test_it_only_asks_for_group_episodes(self):
        """A one-to-one episode has no conversation to widen to, and asking
        would make the server resolve speaker names for nothing."""
        js = self._people_js()
        self.assertIn(
            "const wide = !!(ep.group && ep.thread_since && ep.thread_before)",
            js, "the widened request is no longer gated on this being a group "
                "episode with a conversation window to fetch")
        self.assertIn('if (wide) { params.set("threads", "1")', js,
                      "threads=1 is being sent outside the wide branch")

    def test_it_asks_for_the_conversations_window_not_the_episodes(self):
        """An episode's own bounds are the first and last thing THIS person
        said, so every other voice in the thread falls outside them. Asking for
        the episode's window returns one bubble on a group episode. The server
        computes the wider window because the gap that defines it is its own
        constant."""
        js = self._people_js()
        self.assertIn('before: (wide ? ep.thread_before : ep.ts_end)', js,
                      "the upper bound is back to the episode's own end")
        self.assertIn('params.set("since", ep.thread_since)', js,
                      "the request no longer carries the lower bound, so it "
                      "reads the 200 most recent messages before the episode "
                      "instead of the conversation around it")
        self.assertIn('const list = wide ? (d.messages || [])', js,
                      "a widened response is being trimmed to the episode's "
                      "own window again, which discards the other voices")

    def test_a_group_episode_does_not_start_from_the_inline_payload(self):
        """The server sends messages inline for episodes of six or fewer, and
        those are one person's lines only. Seeding state from them made `toggle`
        return early, so every SMALL group episode opened showing a single
        voice — and most group episodes are small, so the feature would look
        broken while working perfectly on the large ones."""
        js = self._people_js()
        self.assertIn("useState(ep.group ? null : (ep.messages || null))", js,
                      "a group episode is seeding itself from the inline "
                      "messages again, which are only this person's lines")

    def test_a_message_bubble_prefers_the_speaker_the_server_named(self):
        """Without this the whole conversation renders under the page contact's
        name — every line attributed to the wrong person, which is worse than
        showing none of them."""
        js = self._people_js()
        self.assertIn("m.who || who", js,
                      "the bubble ignores the speaker the server named")


class TestTheSetupPanelRowsWrapAtPhoneWidth(unittest.TestCase):
    """NC-113 and NC-186. Two rows in the setup panel on the Data page hold a run
    of labels, chips and buttons side by side. If they cannot wrap, at a 375px
    window each pushes past the edge: the step head (NC-186), and the step
    counter once every source is connected — the Refresh button cut off
    mid-word and the whole Data page scrolling sideways (NC-113).

    The layout cannot be run here, so this checks the one declaration that fixes
    each, on the rule that owns it."""

    ROWS = {".sp-count": "NC-113", ".sp-step-head": "NC-186"}

    def test_each_row_wraps_and_may_shrink(self):
        css = _read("css", "view-setup.css")
        for selector, entry in self.ROWS.items():
            with self.subTest(row=selector):
                m = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
                self.assertIsNotNone(m, f"{selector} is gone from view-setup.css")
                rule = m.group(1)
                self.assertRegex(rule, r"flex-wrap:\s*wrap",
                                 f"{selector} no longer wraps, so at phone width "
                                 f"it runs out of the window again ({entry})")
                self.assertRegex(rule, r"min-width:\s*0",
                                 f"{selector} cannot shrink below its content ({entry})")


class TestTheCommentBlankerKnowsAStringFromAComment(unittest.TestCase):
    """NC-109. Every JavaScript gate in the suite reads code through
    `helpers.js_code`, so its mistakes are every gate's mistakes. The five
    copies it replaced were regexes that deleted comments; two of them also cut
    a line at the `//` in "https://", so a gate could not see code after a URL.

    Each case: what goes in, what must come out, character for character."""

    CASES = (
        ('a = "https://example.com/y"; // gone', 'a = "https://example.com/y";        '),
        ("f(/* c */ b)", "f(        b)"),
        ('html`<p>${/* note */ ""}</p>` // x', 'html`<p>${           ""}</p>`     '),
        ('x.replace(/<[^>]*>/g, "") // y', 'x.replace(/<[^>]*>/g, "")     '),
        ("const r = a / b; // d", "const r = a / b;     "),
        ("s = `a ${b ? `c` : 'd'} e`; /* z */ q", "s = `a ${b ? `c` : 'd'} e`;         q"),
        ("x = /\\/\\//; y // c", "x = /\\/\\//; y     "),
        ("/* one\ntwo */ z", "      \n       z"),
    )

    def test_each_case(self):
        for src, want in self.CASES:
            with self.subTest(src=src):
                self.assertEqual(js_code(src), want)

    def test_it_never_moves_anything(self):
        """Blanked in place, so an index into the result is an index into the
        file — gates slice by position."""
        src = _read("app", "main.js")
        out = js_code(src)
        self.assertEqual(len(out), len(src))
        self.assertEqual(out.count("\n"), src.count("\n"))
