"""NC-270. `--help` explains, and a made-up option stops, before anything runs.

README told readers to add `--help` to any importer for its options. Most of
them read their options by hand and ignored words they did not know, so
`linkedin_import.py --help`, with a token set, fetched and saved a real import.
These run every command-line tool README lists, in a fresh data folder, and
check that neither `--help` nor a made-up option leaves a database behind.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.helpers  # noqa: E402,F401 — import order contract; see tests/helpers.py

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")


def _readme_tools():
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()
    block = readme[readme.index("### CLI (advanced)"):]
    block = block[:block.index("```", block.index("```") + 3)]
    return sorted(set(re.findall(r"crm/(\w+\.py)", block)))


class TestAToolStopsOnAnOptionItDoesNotKnow(unittest.TestCase):

    def _run(self, script, *args):
        data = tempfile.mkdtemp(prefix="nc270_")
        # HOME and CHAT_DB point into the empty folder too, so a tool that loses
        # its guard finds nothing of this Mac's to read (and fires no macOS
        # consent prompt looking for it) before the test catches it.
        env = dict(os.environ, CRM_DATA_DIR=data, HOME=data, NC_NO_BROWSER="1",
                   CHAT_DB=os.path.join(data, "chat.db"), LINKEDIN_TOKEN="not-a-real-token")
        proc = subprocess.run([sys.executable, script, *args], cwd=CRM, env=env,
                              capture_output=True, text=True, timeout=60)
        return proc, os.listdir(data)

    def test_the_readme_lists_the_tools_this_checks(self):
        self.assertGreaterEqual(len(_readme_tools()), 7, _readme_tools())

    def test_help_explains_and_runs_nothing(self):
        for script in _readme_tools():
            with self.subTest(script=script):
                proc, left = self._run(script, "--help")
                self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
                self.assertIn("usage", proc.stdout.lower())
                self.assertFalse([f for f in left if f.endswith(".db")],
                                 f"--help wrote {left}")

    def test_a_made_up_option_stops_it(self):
        for script in _readme_tools():
            with self.subTest(script=script):
                proc, left = self._run(script, "--no-such-option")
                self.assertEqual(proc.returncode, 2, proc.stdout[-400:])
                self.assertFalse([f for f in left if f.endswith(".db")],
                                 f"an unknown option still wrote {left}")


if __name__ == "__main__":
    unittest.main()
