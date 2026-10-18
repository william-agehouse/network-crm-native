"""NC-176 gate: the page has to outlast the import, not race it.

`api()` gave every call 20 seconds. The server gives one import subprocess
`NC_IMPORT_TIMEOUT_SEC`, default 300. Both numbers were deliberate and neither
knew about the other, so every import between them was reported on screen as a
failure while it carried on and succeeded. An import that takes longer than
20 seconds shows this on screen:

    /api/import/run did not answer within 20s

The receipts, the "N new people" count, the toast and the row refresh all go
with it, because they are on the other side of the `await`. In the first-run
wizard it is worse: a first import that takes half a minute is reported as a
failure on a fresh install, five minutes in, which is the one moment someone
decides whether the app works.

The rule is one sentence: **the client's ceiling must be strictly greater than
the server's.** Not equal — when the server gives up it answers with what it
managed to do, and that answer is the useful one, so there has to be room for it
to travel. The margin is plumbing. The ceiling is the setting, it lives on the
server, and the page asks for it rather than keeping a second copy.

The arithmetic is executed under node, because reading the file cannot tell
`ceiling * 1000 + margin` from `ceiling * 1000 - margin`, and both are one
expression with the right names in it.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, js_code_of  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import api_import  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


class TestTheServerTellsThePageHowLongItMayTake(Base):

    def payload(self):
        return app.handle_import_sources(self.conn)

    def test_the_sources_payload_carries_the_ceiling(self):
        out = self.payload()
        self.assertIn("import_timeout_sec", out,
                      "the page cannot outlast a number nobody sent it")
        self.assertEqual(out["import_timeout_sec"], api_import.import_timeout_sec())

    def test_it_is_the_real_setting_and_not_a_second_copy_of_it(self):
        with mock.patch.dict(os.environ, {"NC_IMPORT_TIMEOUT_SEC": "900"}):
            self.assertEqual(self.payload()["import_timeout_sec"], 900,
                             "moving NC_IMPORT_TIMEOUT_SEC has to move what the "
                             "page waits for, or the two drift apart again")

    def test_an_absurd_setting_is_still_clamped(self):
        with mock.patch.dict(os.environ, {"NC_IMPORT_TIMEOUT_SEC": "999999"}):
            self.assertEqual(self.payload()["import_timeout_sec"], 3600)


class TestThePageWaitsLongerThanTheServer(unittest.TestCase):
    """Executed, because the bug was arithmetic and arithmetic has to be run."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed")
        js = js_code_of(os.path.join(APP, "api.js"))
        start = js.index("const IMPORT_MARGIN_MS")
        end = js.index("\n}\n", js.index("export function importTimeoutMs")) + 3
        # strip the `export ` keywords so the slice runs as a plain script
        cls.src = js[start:end].replace("export function", "function")

    def timeout_for(self, ceiling):
        script = (self.src + "\n" +
                  ("noteImportCeiling(" + json.dumps(ceiling) + ");\n"
                   if ceiling is not None else "") +
                  "process.stdout.write(String(importTimeoutMs()));")
        out = subprocess.run([self.node, "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return int(out.stdout)

    def test_it_outlasts_the_default_ceiling(self):
        self.assertGreater(self.timeout_for(None), 300 * 1000,
                           "with no answer from the server yet, the page still "
                           "has to be willing to wait out a default-length import")

    def test_it_outlasts_whatever_the_server_says(self):
        for ceiling in (60, 300, 900, 3600):
            self.assertGreater(
                self.timeout_for(ceiling), ceiling * 1000,
                "the page gives up before the server does at a ceiling of %ds, "
                "so the server's own report of what it managed to do never "
                "arrives" % ceiling)

    def test_it_does_not_wait_forever_either(self):
        self.assertLess(self.timeout_for(3600), 3600 * 1000 + 120 * 1000,
                        "a margin this wide is a hang, not a margin")

    def test_a_ceiling_bigger_than_the_server_can_have_is_clamped(self):
        """The server clamps to an hour, so anything above it is a wrong answer.

        A value like this can only come from a server that is older, patched, or
        not ours. Believing it turns a hung request into a tab that waits all
        day with a disabled button and no way to tell it has stopped.
        """
        self.assertLessEqual(
            self.timeout_for(999999), 3600 * 1000 + 120 * 1000,
            "the page believed a ceiling the server cannot actually have")

    def test_rubbish_from_the_server_leaves_the_fallback_alone(self):
        good = self.timeout_for(None)
        # [1] is the interesting one: Number([1]) is 1, so a guard written
        # with Number() would have accepted it and set a one-second ceiling.
        for junk in ("", None, 0, -5, "soon", [1], {"sec": 300}, "300", True):
            script = (self.src + "\nnoteImportCeiling(" + json.dumps(junk) +
                      ");\nprocess.stdout.write(String(importTimeoutMs()));")
            out = subprocess.run([self.node, "-e", script], capture_output=True,
                                 text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(int(out.stdout), good,
                             "a partial or older payload must not shorten the "
                             "wait to something meaningless: %r" % (junk,))

    def test_the_20_second_default_is_still_the_default_for_everything_else(self):
        js = js_code_of(os.path.join(APP, "api.js"))
        self.assertIn("export const API_TIMEOUT_MS = 20000;", js,
                      "NC-033's 20s ceiling protects every OTHER call from a "
                      "request that never settles — widening it globally would "
                      "trade one bug for that one")


class TestBothPlacesThatStartAnImportUseIt(unittest.TestCase):
    """Two surfaces run imports, and the wizard is the one that matters most."""

    def call_sites(self):
        out = []
        for name in ("data.js", "onboarding.js"):
            js = js_code_of(os.path.join(APP, name))
            at = 0
            while True:
                at = js.find("/api/import/run", at)
                if at == -1:
                    break
                out.append((name, js[max(0, at - 200):at + 400]))
                at += 1
        return out

    def test_every_call_site_asks_for_the_longer_wait(self):
        sites = [(n, w) for n, w in self.call_sites() if "api(" in w]
        self.assertGreaterEqual(len(sites), 2, "the call sites moved")
        for name, window in sites:
            self.assertIn("importTimeoutMs()", window,
                          "%s starts an import on the default 20s ceiling" % name)

    def test_both_surfaces_learn_the_ceiling_before_they_could_use_it(self):
        # NC-236: the Data page reads the ceiling off its own ledger answers
        # (quick and scanned), the wizard off the sources list.
        for name, needles in (
                ("data.js", ("noteImportCeiling(quick.import_timeout_sec)",
                             "noteImportCeiling(full.import_timeout_sec)")),
                ("onboarding.js", ("noteImportCeiling(d.import_timeout_sec)",))):
            js = js_code_of(os.path.join(APP, name))
            for needle in needles:
                self.assertIn(needle, js,
                              "%s never reads the server's ceiling, so it silently "
                              "runs on the fallback forever" % name)

    def test_the_data_page_answer_carries_the_ceiling(self):
        import api_ledger
        with open(api_ledger.__file__, encoding="utf-8") as fh:
            self.assertIn('"import_timeout_sec"', fh.read(),
                          "the Data page's own answer no longer carries the ceiling")


if __name__ == "__main__":
    unittest.main()
