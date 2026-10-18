"""NC-330, NC-316. crm/python_env.py: the private environment the optional parts run in.

Until 2026-10-17 these steps were bash in the app's launcher script, and the tests here
replace the ones that ran that script's functions. What they hold it to:

- a healthy environment for this package list is left alone, and `check` says so;
- a broken one (half-built, or pointing at a Python that has gone) is rebuilt;
- one built by a different Python is rebuilt, so a new bundled Python is adopted (NC-021);
- a failed install never fails the start, leaves a note the Settings page reads, and
  is tried again next time; a good install clears the note;
- with fingerprints in requirements-lock.txt, pip is told to check them and to take only
  ready-built packages (NC-316).

pip is never really run: a stand-in Python records what it was asked and says yes or no.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import tests.helpers  # noqa: F401 — import order contract; see tests/helpers.py

import python_env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_RUN = subprocess.run


class _Folder(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pyenv-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = os.path.join(self.root, "app")
        self.data = os.path.join(self.root, "data")
        self.venv = os.path.join(self.root, "venv")
        self.log = os.path.join(self.root, "setup.log")
        os.makedirs(self.app)
        os.makedirs(self.data)
        self.write("requirements.txt", "anthropic==1.0.0\n")
        self.write("requirements-lock.txt", "anthropic==1.0.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")

    def write(self, name, text):
        with open(os.path.join(self.app, name), "w") as fh:
            fh.write(text)

    def stand_in_venv(self, pip_ok=True, runs=True):
        """A venv folder whose python3 is a script: it answers `-c import sys`, and
        `-m pip install …` succeeds or fails and writes its arguments to a file."""
        bindir = os.path.join(self.venv, "bin")
        os.makedirs(bindir, exist_ok=True)
        py = os.path.join(bindir, "python3")
        record = os.path.join(self.root, "pip-args.txt")
        with open(py, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                #!/bin/sh
                [ "{1 if runs else 0}" = 1 ] || exit 1
                if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
                  shift 2
                  printf '%s\\n' "$@" > "{record}"
                  [ "{1 if pip_ok else 0}" = 1 ] && exit 0 || exit 1
                fi
                exit 0
                """))
        os.chmod(py, stat.S_IRWXU)
        return py, record

    def stamp(self, base_id):
        with open(os.path.join(self.venv, python_env.BASE_STAMP), "w") as fh:
            fh.write(base_id)


class TestAHealthyEnvironmentIsLeftAlone(_Folder):

    def test_check_says_ready_and_prepare_installs_nothing(self):
        _py, record = self.stand_in_venv()
        base = python_env.base_id()
        self.stamp(base)
        with open(os.path.join(self.venv, python_env.DEPS_STAMP), "w") as fh:
            fh.write(python_env.requirements_hash(self.app))
        self.assertTrue(python_env.is_ready(self.venv, self.app))
        got = python_env.prepare(self.venv, self.app, self.data, self.log)
        self.assertEqual(got, python_env.venv_python(self.venv))
        self.assertFalse(os.path.exists(record), "pip ran with nothing to install")

    def test_a_changed_package_list_installs_again(self):
        self.stand_in_venv()
        self.stamp(python_env.base_id())
        with open(os.path.join(self.venv, python_env.DEPS_STAMP), "w") as fh:
            fh.write(python_env.requirements_hash(self.app))
        self.write("requirements.txt", "anthropic==1.0.0\nrequests==2.34.2\n")
        self.assertFalse(python_env.is_ready(self.venv, self.app))


class TestABrokenEnvironmentIsRebuilt(_Folder):

    def test_a_python_that_no_longer_starts_is_replaced(self):
        self.stand_in_venv(runs=False)
        leftover = os.path.join(self.venv, "leftover")
        open(leftover, "w").close()
        made = []

        def fake_run(cmd, **kw):
            if cmd[1:3] == ["-m", "venv"]:
                made.append(cmd)
                self.stand_in_venv()
                return subprocess.CompletedProcess(cmd, 0)
            return _REAL_RUN(cmd, **kw)

        with mock.patch.object(python_env.subprocess, "run", side_effect=fake_run):
            python_env.prepare(self.venv, self.app, self.data, self.log)
        self.assertEqual(len(made), 1, "the broken environment was not rebuilt")
        self.assertFalse(os.path.exists(leftover), "the old environment was kept")

    def test_one_built_by_another_python_is_replaced(self):
        self.stand_in_venv()
        self.stamp("3.9.6 system")
        with open(os.path.join(self.venv, python_env.DEPS_STAMP), "w") as fh:
            fh.write(python_env.requirements_hash(self.app))
        self.assertFalse(python_env.is_ready(self.venv, self.app),
                         "an environment from another Python counted as ready")
        made = []

        def fake_run(cmd, **kw):
            if cmd[1:3] == ["-m", "venv"]:
                made.append(cmd)
                self.stand_in_venv()
                return subprocess.CompletedProcess(cmd, 0)
            return _REAL_RUN(cmd, **kw)

        with mock.patch.object(python_env.subprocess, "run", side_effect=fake_run):
            python_env.prepare(self.venv, self.app, self.data, self.log)
        self.assertEqual(len(made), 1)
        with open(os.path.join(self.venv, python_env.BASE_STAMP)) as fh:
            self.assertEqual(fh.read(), python_env.base_id())

    def test_the_bundled_python_counts_as_a_different_one(self):
        self.assertEqual(python_env.base_origin(
            "/Applications/Network CRM.app/Contents/Resources/python/bin/python3"), "bundled")
        self.assertEqual(python_env.base_origin("/opt/homebrew/bin/python3"), "system")


class TestAFailedInstallNeverStopsTheApp(_Folder):

    def test_it_leaves_a_note_and_tries_again_next_time(self):
        self.stand_in_venv(pip_ok=False)
        self.stamp(python_env.base_id())
        got = python_env.prepare(self.venv, self.app, self.data, self.log)
        self.assertEqual(got, python_env.venv_python(self.venv),
                         "the core must still start on the environment's Python")
        with open(os.path.join(self.data, python_env.MARKER_NAME)) as fh:
            note = json.load(fh)
        self.assertEqual(note["phase"], "pip_install")
        self.assertIn("tries again", note["message"])
        self.assertEqual(note["log_file"], self.log)
        self.assertFalse(os.path.exists(os.path.join(self.venv, python_env.DEPS_STAMP)),
                         "a failed install was recorded as done, so it would never retry")

    def test_a_good_install_clears_the_note(self):
        self.stand_in_venv(pip_ok=True)
        self.stamp(python_env.base_id())
        with open(os.path.join(self.data, python_env.MARKER_NAME), "w") as fh:
            fh.write("{}")
        python_env.prepare(self.venv, self.app, self.data, self.log)
        self.assertFalse(os.path.exists(os.path.join(self.data, python_env.MARKER_NAME)))
        with open(os.path.join(self.venv, python_env.DEPS_STAMP)) as fh:
            self.assertEqual(fh.read(), python_env.requirements_hash(self.app))

    def test_no_environment_at_all_falls_back_to_the_bare_python(self):
        def fake_run(cmd, **kw):
            if cmd[1:3] == ["-m", "venv"]:
                return subprocess.CompletedProcess(cmd, 1)
            return _REAL_RUN(cmd, **kw)

        with mock.patch.object(python_env.subprocess, "run", side_effect=fake_run):
            got = python_env.prepare(self.venv, self.app, self.data, self.log, base=sys.executable)
        self.assertEqual(got, sys.executable)
        with open(os.path.join(self.data, python_env.MARKER_NAME)) as fh:
            self.assertEqual(json.load(fh)["phase"], "venv_create")


class TestFingerprintsAreChecked(_Folder):

    def test_pip_is_told_to_check_them_and_build_nothing(self):
        _py, record = self.stand_in_venv()
        self.stamp(python_env.base_id())
        python_env.prepare(self.venv, self.app, self.data, self.log)
        with open(record) as fh:
            args = fh.read().split("\n")
        self.assertIn("--require-hashes", args)
        self.assertIn("--only-binary=:all:", args)
        self.assertIn(os.path.join(self.app, "requirements-lock.txt"), args)

    def test_without_fingerprints_it_still_keeps_to_the_versions(self):
        self.write("requirements-lock.txt", "anthropic==1.0.0\n")
        cmd = python_env.install_command("py", self.app)
        self.assertNotIn("--require-hashes", cmd)
        self.assertEqual(cmd[-2:], ["-c", os.path.join(self.app, "requirements-lock.txt")])

    def test_the_real_lock_file_has_a_fingerprint_for_every_package(self):
        with open(os.path.join(REPO, "requirements-lock.txt")) as fh:
            text = fh.read()
        lines = text.split("\n")
        entries = [e for e in lines if e and not e.startswith((" ", "#"))]
        self.assertGreater(len(entries), 10)
        # Every "name==version" line is followed by at least one fingerprint.
        for i, line in enumerate(lines):
            if line and not line.startswith((" ", "#")):
                self.assertTrue(line.endswith("\\"), f"{line} has no fingerprints")
                self.assertTrue(lines[i + 1].strip().startswith("--hash=sha256:"),
                                f"{line} has no fingerprints")


class TestTheCommandLine(_Folder):

    def test_check_exits_3_when_setting_up_is_needed(self):
        out = subprocess.run([sys.executable, os.path.join(REPO, "crm", "python_env.py"),
                              "check", "--venv", self.venv, "--app-home", self.app],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, python_env.NEEDS_SETUP)
        self.assertEqual(out.stdout, "")


if __name__ == "__main__":
    unittest.main()
