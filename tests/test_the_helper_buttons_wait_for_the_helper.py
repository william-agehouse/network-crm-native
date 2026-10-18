"""NC-206. The helper's three buttons wait longer than the server lets them run.

The Data page gives every call 20 seconds (`API_TIMEOUT_MS`). The server gives
the helper's installer and its off switch `INSTALL_TIMEOUT` (300 s), and "Check
now" up to 20 s to start the helper plus `RECHECK_TIMEOUT` (45 s) to hear back.
The installer runs the helper once and waits up to a minute for its report, so an
install is never quick.

Without that, an install that takes about 40 seconds ("Set up automatic
updates") would see the page give up at 20. It would go back to the offer to set
the helper up, with the OFF switch's output from a minute earlier still under it
("Uninstalled. The automatic sync will not run again."), while the new helper was
already installed and reporting. Only a reload would show the truth.

The same mistake as NC-176, and the same cure: the server says how long each
action may take, and the page waits longer than that. Whatever happens, the panel
re-reads the status afterwards, and it clears one run's output before the next.
The arithmetic runs under node, because reading the file cannot tell `+ margin`
from `- margin`.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, js_code_of  # noqa: E402

import api_fda  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FDA_JS = os.path.join(REPO, "static", "app", "fda.js")
ACTIONS = {"install": "/api/fda/install", "uninstall": "/api/fda/uninstall",
           "recheck": "/api/fda/recheck"}


class TestTheServerSaysHowLongEachButtonMayTake(Base):

    def test_every_button_has_a_wait(self):
        waits = api_fda.handle_fda_status()["waits"]
        self.assertEqual(set(waits), set(ACTIONS))

    def test_they_are_the_real_limits(self):
        with mock.patch.object(api_fda, "INSTALL_TIMEOUT", 777), \
             mock.patch.object(api_fda, "RECHECK_TIMEOUT", 50):
            waits = api_fda.handle_fda_status()["waits"]
        self.assertEqual(waits["install"], 777)
        self.assertEqual(waits["uninstall"], 777)
        self.assertEqual(waits["recheck"], api_fda.KICKSTART_TIMEOUT + 50,
                         "the check can spend the start-up AND the wait")

    def test_the_uninstaller_really_is_given_the_install_limit(self):
        """`waits.uninstall` claims the off switch gets INSTALL_TIMEOUT. That is
        only true while the handler passes it, so read the handler."""
        src = open(api_fda.__file__, encoding="utf-8").read()
        body = src[src.index("def handle_fda_uninstall"):src.index("def handle_fda_recheck")]
        self.assertIn("timeout=INSTALL_TIMEOUT", body)
        recheck = src[src.index("def handle_fda_recheck"):]
        self.assertIn("timeout=KICKSTART_TIMEOUT", recheck)


class TestThePageWaitsLongerThanTheServer(unittest.TestCase):
    """Executed, because the bug is arithmetic."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed")
        js = js_code_of(FDA_JS)
        start = js.index("const WAIT_MARGIN_MS")
        end = js.index("\n}\n", js.index("export function waitFor")) + 3
        cls.src = js[start:end].replace("export function", "function")

    def wait(self, action, waits):
        script = (self.src + "\nprocess.stdout.write(String(waitFor("
                  + json.dumps(action) + ", " + json.dumps(waits) + ")));")
        out = subprocess.run([self.node, "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return int(out.stdout)

    def test_it_outlasts_what_the_server_says(self):
        for action in ACTIONS:
            for sec in (30, 65, 300, 900):
                with self.subTest(action=action, sec=sec):
                    self.assertGreater(self.wait(action, {action: sec}), sec * 1000)

    def test_with_no_answer_yet_it_still_outlasts_the_defaults(self):
        for action, sec in (("install", 300), ("uninstall", 300),
                            ("recheck", 20 + 45)):
            with self.subTest(action=action):
                self.assertGreater(self.wait(action, None), sec * 1000)

    def test_the_fallback_matches_the_server_defaults(self):
        """The page's fallback is a copy of the server's defaults, so pin the
        copy to the source it was taken from."""
        self.assertEqual(self.wait("install", None), self.wait("install", {"install": 300}))
        self.assertEqual(self.wait("recheck", None),
                         self.wait("recheck", {"recheck": api_fda.KICKSTART_TIMEOUT + 45}))

    def test_rubbish_never_shortens_the_wait(self):
        good = self.wait("install", None)
        for junk in ("", 0, -5, "soon", [1], {"sec": 300}, "300", True, None):
            with self.subTest(junk=junk):
                self.assertEqual(self.wait("install", {"install": junk}), good)

    def test_it_does_not_wait_forever(self):
        self.assertLess(self.wait("install", {"install": 999999}),
                        3600 * 1000 + 120 * 1000)


class TestTheButtonsUseIt(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = js_code_of(FDA_JS)

    def handler(self, name):
        i = self.js.index(f"  const {name} = async () => {{")
        return self.js[i:self.js.index("\n  };", i)]

    def test_each_call_passes_its_own_wait(self):
        for action, path in ACTIONS.items():
            with self.subTest(action=action):
                call = self.js[self.js.index(f'api("{path}"'):]
                call = call[:call.index(");")]
                self.assertRegex(call, rf'timeoutMs:\s*waitFor\("{action}",\s*status\?\.waits\)')

    def test_a_failed_call_rereads_the_status(self):
        """A timeout is not the only way the answer goes missing. Whatever the
        reason, what the panel shows next comes from the server, not from
        before the button was pressed."""
        for name in ("install", "uninstall", "check"):
            with self.subTest(handler=name):
                catch = self.handler(name).split("} catch (e) {", 1)[1].split("} finally", 1)[0]
                self.assertIn("load();", catch)

    def test_one_runs_output_is_never_shown_beside_the_next(self):
        for name in ("install", "uninstall"):
            with self.subTest(handler=name):
                body = self.handler(name)
                self.assertLess(body.index('setOutput("")'), body.index("await api("))

    def test_the_20_second_default_is_still_everyone_elses(self):
        api_js = js_code_of(os.path.join(REPO, "static", "app", "api.js"))
        self.assertIn("export const API_TIMEOUT_MS = 20000;", api_js)
        self.assertEqual(len(re.findall(r"timeoutMs:", self.js)), 3,
                         "only the three helper buttons are given a longer wait")


if __name__ == "__main__":
    unittest.main()
