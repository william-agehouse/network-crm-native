"""NC-125. The Data page's figures and source list re-read after an import.

Both came from sources.js's `useJson(path)`, whose effect depended only on
`path` — a constant — and dataview.js mounted `<DataStats />` and
`<SourceGrid />` with no props. So they fetched once on mount and never again:
an import could land thousands of messages under "0 messages".

The page already had the signal. `loadAll()` runs after every import (it is the
`onRan` of every row) and bumps `runs`, which the setup, helper and LinkedIn
panels were already reading as their refresh key. These two now read it too.
"""

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


# NC-236: the old page's pieces (useJson and its refresh key, DataStats,
# SourceGrid, loadAll and its `runs` counter) went with it. The new page has one
# answer, GET /api/data/ledger, and one function that asks for it again. So the
# rule is pinned the way the new page keeps it: every control that changes
# something is handed that function, and a failed re-read keeps what is on screen.
class TestTheFiguresReReadWhenAnImportLands(unittest.TestCase):

    def setUp(self):
        self.page = read("static", "app", "data.js")

    def test_the_page_asks_again_after_anything_you_do(self):
        self.assertRegex(self.page, r"const changed = useCallback\(\(\) => load\(false\), \[load\]\);")
        page = self.page[self.page.index("export function DataPage"):]
        load = page[page.index("const load = useCallback("):]
        load = load[:load.index("}, []);")]
        self.assertIn('api("/api/data/ledger?scan=1")', load)

    def test_an_import_finishing_calls_it(self):
        """useImportRunner calls onRan when a run succeeds, and ImportAct is
        given the page's `changed` as that."""
        runner = self.page[self.page.index("export function useImportRunner"):]
        runner = runner[:runner.index("return { running")]
        self.assertIn("onRan()", runner)
        self.assertRegex(self.page, r"ImportAct\} key=\$\{key\} act=\$\{a\} label=\$\{label\} onRan=\$\{onChanged\}")

    def test_the_rows_and_the_install_figures_get_it(self):
        for needle in ("<${SourceRow} key=${r.key}", "<${Install} install="):
            with self.subTest(component=needle):
                at = self.page.index(needle)
                self.assertIn("onChanged=${changed}", self.page[at:at + 400])

    def test_a_refresh_never_blanks_numbers_already_on_screen(self):
        """A re-read that fails keeps the rows on screen and says the check
        failed; the page only shows its error when it has nothing to show."""
        self.assertIn("if (!shown) throw e;", self.page)
        self.assertIn("if (!data) {", self.page)
        render = self.page[self.page.index("if (!data) {"):]
        self.assertIn("error ?", render[:300])


if __name__ == "__main__":
    unittest.main()
