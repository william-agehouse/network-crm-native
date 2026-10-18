"""NC-147 gate: a credential file this app writes is readable only by its owner.

`open(path, "w")` leaves a file 0644 — readable by every account on the Mac. A
copy of that line writing a Google refresh token for `contacts.readonly` would
expose the whole Google address book, renewable indefinitely, and fixing one copy
only sets up the next.

So the rule lives in config.harden() (NC-133, NC-147): every credential write
goes through it, and these tests are about the SHAPE rather than the two files
that happened to be wrong:

  * TestNoTokenIsWrittenWithoutHardeningIt reads every module that writes a
    credential and fails on the next copy of the six lines, wherever it lands.
  * TestTheSweepCatchesFilesNoCodeWrote covers the case the per-write chmod
    cannot reach at all: a Google OAuth client file is downloaded from Google
    Cloud and dropped into secrets/ by hand, arriving with whatever mode it was
    downloaded with (0644, with a client id and secret in it), and no line of
    code ever touches it. A per-write fix only covers the places that do the
    writing.

secrets/ is 0700, and that is what actually stops another account reading these
today. 0600 on the files is the second wall, for the moment one leaves the
directory still carrying its mode.
"""
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, code_of, py_block  # noqa: E402,F401

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import config  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class TestHardenMakesOneFileOwnerOnly(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_secrets_")

    def _file(self, name, mode):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.chmod(p, mode)
        return p

    def test_a_world_readable_token_becomes_owner_only(self):
        p = self._file("token.json", 0o644)
        config.harden(p)
        self.assertEqual(mode_of(p), 0o600,
                         "config.harden left the file readable by every account "
                         "on the Mac")

    def test_a_missing_file_does_not_raise(self):
        config.harden(os.path.join(self.dir, "not-there.json"))  # must not raise


class TestTheSweepCatchesFilesNoCodeWrote(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_secrets_")

    def _file(self, name, mode):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.chmod(p, mode)
        return p

    def test_a_hand_dropped_credentials_file_is_tightened(self):
        p = self._file("credentials_gmail.json", 0o644)
        with mock.patch.object(config, "SECRETS_DIR", self.dir):
            changed = config.harden_secrets_dir()
        self.assertEqual(changed, 1)
        self.assertEqual(mode_of(p), 0o600,
                         "a credential file nothing in this project writes is "
                         "still left group- and world-readable")

    def test_a_second_sweep_changes_nothing(self):
        self._file("credentials_gmail.json", 0o644)
        with mock.patch.object(config, "SECRETS_DIR", self.dir):
            config.harden_secrets_dir()
            self.assertEqual(config.harden_secrets_dir(), 0,
                             "the sweep reports work it did not do, so the "
                             "startup line would appear on every launch")

    def test_it_only_ever_tightens(self):
        """0444 is the case that separates "tighten" from "set to 0600".

        A flat `chmod 0600` reads as the stricter thing and is not: on a file
        the owner made read-only it takes away the other-read bit and hands
        back a write bit they had removed on purpose. The sweep clears the
        group and other bits and leaves the owner's alone.
        """
        p = self._file("read_only.json", 0o444)
        with mock.patch.object(config, "SECRETS_DIR", self.dir):
            config.harden_secrets_dir()
        self.assertEqual(
            oct(mode_of(p)), oct(0o400),
            "the sweep gave back a write bit the owner had removed. It must "
            "clear group and other, not overwrite the whole mode")

    def test_an_owner_executable_credential_helper_stays_executable(self):
        p = self._file("helper.sh", 0o755)
        with mock.patch.object(config, "SECRETS_DIR", self.dir):
            config.harden_secrets_dir()
        self.assertEqual(oct(mode_of(p)), oct(0o700),
                         "the sweep stripped the owner's execute bit, so a "
                         "script kept beside the credentials stops running")

    def test_it_does_not_follow_a_symlink_out_of_the_directory(self):
        outside = os.path.join(tempfile.mkdtemp(prefix="crm_outside_"), "other.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("not a secret")
        os.chmod(outside, 0o644)
        os.symlink(outside, os.path.join(self.dir, "link.json"))
        with mock.patch.object(config, "SECRETS_DIR", self.dir):
            config.harden_secrets_dir()
        self.assertEqual(mode_of(outside), 0o644,
                         "a symlink in secrets/ let the sweep change the "
                         "permissions of a file somewhere else on the disk")

    def test_a_missing_secrets_directory_is_not_an_error(self):
        with mock.patch.object(config, "SECRETS_DIR",
                               os.path.join(self.dir, "nope")):
            self.assertEqual(config.harden_secrets_dir(), 0)


class TestNoTokenIsWrittenWithoutHardeningIt(unittest.TestCase):
    """The shape, so the third copy of the six lines fails instead of shipping."""

    def test_every_token_write_is_followed_by_a_harden(self):
        offenders = []
        for name in sorted(os.listdir(CRM)):
            if not name.endswith(".py"):
                continue
            src = code_of(os.path.join(CRM, name))
            for m in re.finditer(r"^.*\bopen\([^)]*\"w\".*$", src, re.M):
                line = m.group(0)
                if "token_path" not in line and "secret_path" not in line:
                    continue
                # The ENCLOSING function, not the `with` statement's own body —
                # the permission is set after the file is closed, which is a
                # sibling of the write and not a child of it.
                opener = src.rfind("\ndef ", 0, m.start())
                func = py_block(src[opener + 1:], "def ") if opener != -1 else src
                if "harden(" not in func and "chmod" not in func:
                    offenders.append(f"{name}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "a credential is written and left at whatever mode "
                         "open() chose. config.harden(path) on the next line")

    def test_the_rule_lives_in_one_place(self):
        """Every credential write goes through config.harden(). Retyping
        `os.chmod(..., 0o600)` into a third importer is how the fourth one gets
        forgotten."""
        for name in ("gmail_import.py", "google_photos_import.py"):
            src = code_of(os.path.join(CRM, name))
            self.assertIn("config.harden(", src,
                          f"{name} hardens its token by hand again instead of "
                          "calling the one helper that documents why")


class TestTheSweepRunsBeforeAnythingUsesTheFiles(unittest.TestCase):
    def test_startup_tightens_the_directory_before_the_autosync_thread(self):
        main = py_block(code_of(os.path.join(CRM, "app.py")), "def main() -> None:")
        self.assertIn("harden_secrets_dir", main,
                      "startup no longer tightens secrets/, so a credentials "
                      "file dropped in by hand stays world-readable until "
                      "someone notices")
        self.assertLess(
            main.index("harden_secrets_dir"), main.index("threading.Thread"),
            "the sweep runs after the autosync thread, which is what reads "
            "these tokens — it should be tightened before it is used")


if __name__ == "__main__":
    unittest.main()
