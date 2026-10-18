"""NC-088 / NC-114 / NC-236. The Data page draws one answer, and can be reached.

The endpoint's rules are pinned in test_the_data_ledger_is_one_answer.py. These
pin the page's side of the bargain:

  * it asks /api/data/ledger and nothing else for what it shows — the current
    page's eight endpoints, each decided on separately, are how two true numbers
    came to disagree on one screen;
  * since it replaced the old page (NC-236), the tab, ⌘K and the
    menus open it, and the requests they carry (find duplicates, merge two
    people, export a backup) are answered on it;
  * (NC-330, 2026-10-17: the packaged app's window is native now and does not show this
    page. The page is what a source checkout serves to a browser, and its checks stay.
    What the app's own menu offers is looked for in native/Sources instead.)
  * the credential boxes refuse, before sending, the pastes that go wrong;
  * an open row's five blocks come in one order, everywhere.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import js_code_of  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")
PAGE = os.path.join(APP, "data.js")
PARTS = os.path.join(APP, "ledger.js")
CSS = os.path.join(REPO, "static", "css", "view-ledger.css")
MAIN = os.path.join(APP, "main.js")
NATIVE = os.path.join(REPO, "native", "Sources")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _native_sources():
    """Every Swift file of the app's window, joined. Before NC-330 the window was one
    file, packaging/src/NetworkCRMApp.swift; it is native/Sources/*.swift now."""
    return "\n".join(_read(os.path.join(NATIVE, name))
                     for name in sorted(os.listdir(NATIVE)) if name.endswith(".swift"))


class TestThePageAsksOneEndpoint(unittest.TestCase):

    def test_everything_it_shows_comes_from_the_ledger(self):
        code = js_code_of(PAGE)
        asked = set(re.findall(r'api\(\s*["`](/api/[^"`?$]+)', code))
        # The ledger, and the actions the page runs itself: the file picker, an
        # import, and the duplicates finder in This install (NC-228), whose
        # groups are read only when you open them; the ledger carries the count.
        # What a source added is read only when you press its button. The import
        # and that report came from the old page's file with the switch-over
        # (NC-236). None of these is a read of what the page shows at rest.
        self.assertEqual(asked, {"/api/data/ledger", "/api/import/pick-file", "/api/import/run",
                                 "/api/provenance", "/api/duplicates", "/api/merge"})

    def test_a_need_keeps_its_place_when_another_is_resolved(self):
        """Keyed by position, resolving one need would remount the ones below
        it and throw away a Preview someone is reading."""
        code = js_code_of(PAGE)
        key = re.search(r"<\$\{NeedsRow\} key=\$\{`([^`]*)`\}", code).group(1)
        self.assertNotIn("${i}", key)

    def test_it_asks_again_after_anything_you_do(self):
        """NC-125: no refresh button. Every control is handed the reload."""
        code = js_code_of(PAGE)
        for control in ("ImportAct", "CredentialBox", "TextSetting", "FdaPanel", "GmailSetup"):
            with self.subTest(control=control):
                self.assertRegex(code, control + r"\b[^`]*?on(Ran|Saved|Changed)=\$\{onChanged\}")
        self.assertNotIn(">Refresh<", code)


class TestItCanBeReached(unittest.TestCase):
    """NC-236. The page sat at #data-next beside the old one until the old one
    was retired."""

    def test_the_data_address_draws_it(self):
        code = js_code_of(MAIN)
        self.assertIn('"data": () => html`<${DataPage} key="data" params=${route.params}', code)
        self.assertIn('import { DataPage } from "./data.js";', code)

    def test_the_old_address_still_leads_there(self):
        """A place remembered as #data-next before the switch-over."""
        self.assertIn('{ "data-next": "data", "settings-next": "settings" }', js_code_of(MAIN))

    def test_nothing_offers_a_second_data_page(self):
        self.assertNotIn('hash: "data-next"', js_code_of(MAIN))
        self.assertNotIn('"data-next"', _native_sources(),
                         "the app's window still points at an address of the old second page")

    def test_it_is_a_tab(self):
        """In the browser page ⌘1–⌘4 are bound to the four tabs, Data among them. (The
        app's window has its own Go menu, built from the same list as its sidebar;
        tests/test_packaging.py checks that.)"""
        code = js_code_of(MAIN)
        tabs = code[code.index("const TABS"):code.index("];", code.index("const TABS"))]
        self.assertIn('["data", "Data"]', tabs)
        self.assertNotIn("data-next", tabs)


class TestTheRequestsALinkCarries(unittest.TestCase):
    """⌘K and the menus ask the page for three things once it is open. The old
    page answered them; the switch-over must not leave them landing on the top
    of the page with nothing done (NC-236)."""

    def test_each_request_is_still_sent(self):
        main = js_code_of(MAIN)
        for ask in ("merge", "dup", "export"):
            with self.subTest(ask=ask):
                self.assertIn(f'hash: "data?{ask}=1"', main)

    def test_the_apps_own_menu_offers_the_same_three(self):
        """Moved 2026-10-17 (NC-330). The window used to send the page the three
        links ("data?merge=1" and so on). It is native now, so its File menu runs the
        three actions itself, and the same three are what this checks are still there."""
        menu = _read(os.path.join(NATIVE, "App.swift"))
        for title, action in (("Find Duplicates…", "CommandActions.findDuplicates(app)"),
                              ("Merge Two People…", "app.mergeRequest = MergeRequest()"),
                              ("Export a Backup…", "CommandActions.exportBackup(app)")):
            with self.subTest(item=title):
                self.assertIn(f'Button("{title}") {{ {action} }}', menu)

    def test_the_page_answers_each_one(self):
        code = js_code_of(PAGE)
        self.assertIn('const ASKS = ["dup", "merge", "export"];', code)
        self.assertIn("const ask = useAskFromLink(params, !!data, !!data?.scanned);", code)
        dups = code[code.index("function PossibleDuplicates"):code.index("function Install")]
        self.assertIn('if (ask.what === "merge") setManual(true);', dups)
        self.assertIn("setOpen(true); load();", dups)
        self.assertIn('id="lg-dups"', dups)
        self.assertEqual(code.count('<div class="lg-export" id="lg-export">'), 2,
                         "the export is found on an empty install too")

    def test_each_press_acts_again(self):
        """The one-shot number main.js adds to each press is part of the link."""
        main = js_code_of(MAIN)
        one_shot = main[main.index("const ONE_SHOT_PARAMS"):main.index("];", main.index("const ONE_SHOT_PARAMS"))]
        for ask in ("merge", "dup", "export", "row"):
            self.assertIn(f'"{ask}"', one_shot)
        hook = js_code_of(PAGE)
        hook = hook[hook.index("function useAskFromLink"):hook.index("function PossibleDuplicates")]
        self.assertIn('const link = params?.toString?.() || "";', hook)
        self.assertIn("if (!ready || done.current === link) return;", hook)

    def test_the_linkedin_facts_command_opens_its_row(self):
        self.assertIn('hash: "data?row=linkedin_facts"', js_code_of(MAIN))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestThePasteGuards(unittest.TestCase):
    """Executed, because what matters is which strings they refuse."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node") or "node"
        js = js_code_of(PARTS)
        # The format rules both functions end on (NC-218).
        rules = js[js.index("const CALENDAR_ADDRESS"):js.index("export function secretProblem")]
        cls.src = rules.replace("export const", "const") + "\n".join(
            js[js.index(f"export function {name}"):js.index("\n}\n", js.index(f"export function {name}")) + 3]
            .replace("export function", "function")
            for name in ("secretProblem", "textProblem"))

    def run_js(self, expr):
        out = subprocess.run([self.node, "-e",
                              self.src + f"\nprocess.stdout.write(JSON.stringify({expr}));"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def secret(self, value, masked=""):
        return self.run_js(f"secretProblem({json.dumps(value)}, {json.dumps(masked)})")

    def test_a_clean_token_goes_through(self):
        self.assertEqual(self.secret("AQV-abc123_DEF"), "")
        self.assertEqual(self.secret("  AQV-abc123  "), "", "surrounding space is trimmed, not refused")

    def test_what_goes_wrong_when_pasting_is_refused(self):
        self.assertIn("Paste it first", self.secret("   "))
        self.assertIn("line break", self.secret("AQV-abc\n123"))
        self.assertIn("space", self.secret("AQV abc"))
        self.assertIn("private key", self.secret("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertIn("hidden copy", self.secret("…7788", masked="…7788"))

    def test_the_scraper_key_wants_a_path_not_a_key(self):
        """NC-119."""
        refuse = self.run_js('textProblem("KEY", "-----BEGIN OPENSSH PRIVATE KEY-----")')
        self.assertIn("where the key file is", refuse)
        self.assertEqual(self.run_js('textProblem("KEY", "~/.ssh/example_key")'), "")
        self.assertEqual(self.run_js('textProblem("IMAC", "you@scraper-host")'), "")


class TestTheSharedParts(unittest.TestCase):

    def test_an_open_row_has_one_order(self):
        code = js_code_of(PARTS)
        block = code[code.index("DETAIL_BLOCKS = ["):code.index("];", code.index("DETAIL_BLOCKS = ["))]
        self.assertEqual(re.findall(r'\["(\w+)"', block), ["now", "act", "how", "other", "record"])

    def test_a_stored_secret_is_never_shown_back(self):
        code = js_code_of(PARTS)
        box = code[code.index("export function CredentialBox"):code.index("export function textProblem")]
        self.assertNotIn("act.value", box, "the stored secret itself never reaches the page")
        self.assertIn("act.masked", box)
        self.assertIn('type="password"', box)

    def test_the_columns_fold_at_the_three_widths(self):
        css = _read(CSS)
        for width in (1399, 1099, 899):
            with self.subTest(width=width):
                self.assertIn(f"@media (max-width: {width}px)", css)

    def test_a_figure_refuses_a_bare_number(self):
        code = js_code_of(PARTS)
        fig = code[code.index("export function Figure"):code.index("async function saveSetting")]
        self.assertIn("!fig.population", fig)

    def test_a_closed_row_in_use_says_when_in_place_of_what_it_takes(self):
        """NC-233. The server decides which rows carry `when`; the row draws it
        in the takes column, with no effort chip, since the effort is spent."""
        code = js_code_of(PARTS)
        row = code[code.index("export function LedgerRow"):code.index("export function RowDetail")]
        self.assertIn("${when || takes}", row)
        self.assertIn("!when && html`<${CostChip}", row)
        self.assertEqual(js_code_of(PAGE).count('when=${row.when || ""}'), 2,
                         "both kinds of row pass it on")


if __name__ == "__main__":
    unittest.main()
