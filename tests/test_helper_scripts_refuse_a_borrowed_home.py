"""NC-197. The sync helper's scripts refuse to run with a borrowed HOME.

A fresh-install preview runs the built app with HOME, the data folder and the
secrets folder all pointed at /tmp, so it can show a genuinely empty install.
Its Data page still offers "Set up automatic updates", and that button runs
packaging/install_imessage_sync.sh with the borrowed HOME. Two things in that
script ignore HOME:

* launchd names the agent by LABEL (io.github.william-agehouse.networkcrm.imessagesync), not by
  folder — so `launchctl bootout gui/<uid>/<label>` stops the account's
  already-installed helper, and the bootstrap that follows replaces it;
* the helper writes to the account's actual home (homeDirectoryForCurrentUser),
  so a copy with no Full Disk Access would report "blocked" into the installed
  app.

The uninstaller has the first problem too. Both now compare HOME with the
account's home from the user database and stop before touching launchd.

These tests RUN the scripts, which is only safe because every command either
script could reach outside the borrowed HOME — launchctl, swiftc, xcrun,
codesign, open — is a stub first on PATH that records the call and fails. So even
with the guard deleted (a mutation), nothing real is stopped or replaced; the
stub log is what fails the test.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = {
    "install": os.path.join(REPO, "packaging", "install_imessage_sync.sh"),
    "uninstall": os.path.join(REPO, "packaging", "uninstall_imessage_sync.sh"),
}
STUBBED = ("launchctl", "swiftc", "xcrun", "codesign", "open", "plutil", "security")


class TestTheHelperScriptsRefuseABorrowedHome(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc197-")
        self.home = os.path.join(self.tmp, "home")
        self.stubs = os.path.join(self.tmp, "stubs")
        self.log = os.path.join(self.tmp, "calls.log")
        os.makedirs(self.home)
        os.makedirs(self.stubs)
        for name in STUBBED:
            path = os.path.join(self.stubs, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f'#!/bin/bash\necho "{name} $*" >> "{self.log}"\nexit 97\n')
            os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, which):
        env = {"HOME": self.home, "PATH": f"{self.stubs}:/usr/bin:/bin",
               "LANG": "C"}
        return subprocess.run(["/bin/bash", SCRIPTS[which]], env=env,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_the_stubs_are_what_the_script_would_reach(self):
        """The premise: every stubbed name is one the scripts call by bare name,
        so PATH order decides which one runs. An absolute /bin/launchctl would
        walk past the stub, and this whole file would stop being safe."""
        import re
        for which, path in SCRIPTS.items():
            with open(path, encoding="utf-8") as fh:
                code = [ln for ln in fh.read().splitlines()
                        if not ln.lstrip().startswith("#")]
            for name in STUBBED:
                with self.subTest(script=which, command=name):
                    hits = [ln for ln in code if re.search(rf"/{name}\b", ln)]
                    self.assertEqual(hits, [], f"{name} is called by absolute path")

    def test_each_script_refuses_and_touches_nothing(self):
        for which in SCRIPTS:
            with self.subTest(script=which):
                proc = self.run_script(which)
                self.assertNotEqual(proc.returncode, 0,
                                    f"{which} ran with a borrowed HOME")
                self.assertEqual(self.calls(), [],
                                 f"{which} reached a system command before refusing")
                self.assertIn("Nothing was changed", proc.stderr)
                self.assertIn(self.home, proc.stderr,
                              "the refusal should name the HOME it refused")
                self.assertEqual(os.listdir(self.home), [],
                                 f"{which} wrote into the borrowed HOME")


if __name__ == "__main__":
    unittest.main()
