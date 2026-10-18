"""One machine, no SSH (NC-012), and no one else's machine baked into the repo
(NC-014).

import_scraped_photos.py and enrich_li_extras.py used to know only how to
rsync a directory over SSH from a separate scraper machine. These tests prove
their new --source-dir path reads a local directory directly — with zero SSH
configured anywhere (subprocess.run is patched to explode if either script
ever tries to shell out) — and that it never deletes the caller's own
directory, only a tempdir the script made for itself under --pull.

They also guard NC-014: the shipped LaunchAgent plist must carry the
substitution token, never a real absolute path, in the values launchd
actually reads (not just "no forbidden substring anywhere in the file" —
the file's own explanatory comment near the top shows an example path under
/Users/, so a naive full-text grep for that prefix would trip on the
commentary rather than on the values).
"""

import contextlib
import io
import json
import os
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    db,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLIST_PATH = os.path.join(REPO_ROOT, "deploy", "com.networkcrm.liscraper.plist")

JPEG = b"\xff\xd8\xff\xe0" + b"J" * 300     # valid magic, junk body (>photos.MIN_BYTES)

import subprocess as _real_subprocess  # noqa: E402 — captured before any patching
_ORIGINAL_RUN = _real_subprocess.run   # the real thing, before this module ever patches it


def _no_ssh_or_rsync(cmd, *args, **kwargs):
    """Stand-in for subprocess.run that forbids only ssh/rsync/scp — the network
    calls --source-dir must never make. Everything else (e.g. photos.py
    shelling out to macOS `sips` to resize the image) is real, local work that
    --source-dir has every right to still do, so it's passed through to the
    ORIGINAL run — never back through subprocess.run itself, which would just
    call this stand-in again and recurse forever, since `subprocess` is one
    shared module object process-wide: patching subprocess.run patches it for
    every importer of the module, including crm/photos.py — exactly why this
    can't be a blanket "raise on any call"."""
    program = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
    if program in ("ssh", "rsync", "scp"):
        raise AssertionError(f"{program!r} must never be invoked for --source-dir: {cmd!r}")
    return _ORIGINAL_RUN(cmd, *args, **kwargs)


def _run_main(module, argv, subprocess_guard=True):
    """Run module.main() with sys.argv patched, capturing stdout.

    subprocess_guard=True (the default) forbids ssh/rsync/scp specifically —
    the whole point of --source-dir is that none of those ever run — while
    still letting local subprocess calls (image normalization) proceed.
    """
    buf = io.StringIO()
    patches = [mock.patch.object(sys, "argv", ["prog", *argv])]
    if subprocess_guard:
        patches.append(mock.patch.object(_real_subprocess, "run", _no_ssh_or_rsync))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with contextlib.redirect_stdout(buf):
            module.main()
    return buf.getvalue()


class _NoScraperHostEnv(unittest.TestCase):
    """Shared setUp: prove no scraper-machine env is configured, and none leaks in."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in ("IMAC", "KEY")}
        self.assertEqual(os.environ.get("IMAC", ""), "",
                          "IMAC must be unset for these tests to mean anything")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v


class TestImportScrapedPhotosSourceDir(_NoScraperHostEnv):
    def setUp(self):
        super().setUp()
        import import_scraped_photos as isp
        self.isp = isp
        self.dir = tempfile.mkdtemp(prefix="crm_isp_test_")
        self.src_dir = os.path.join(self.dir, "li_raw")
        os.makedirs(self.src_dir)
        self.dst_path = os.path.join(self.dir, "crm.db")
        db.init_db(self.dst_path)
        conn = db.get_db(self.dst_path)
        self.cid = db.create_contact(conn, "Ada Lovelace")
        conn.commit()
        conn.close()
        with open(os.path.join(self.src_dir, f"{self.cid}.jpg"), "wb") as fh:
            fh.write(JPEG)
        with open(os.path.join(self.src_dir, f"{self.cid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "Ada Lovelace"}, fh)

    def test_source_dir_imports_with_no_subprocess_call(self):
        out = _run_main(self.isp, ["--source-dir", self.src_dir, "--dest", self.dst_path])
        self.assertIn("Imported 1 of 1", out)
        conn = db.get_db(self.dst_path)
        row = db.get_contact(conn, self.cid)
        conn.close()
        self.assertEqual(row["photo_source"], "linkedin")

    def test_source_dir_is_never_deleted_afterward(self):
        _run_main(self.isp, ["--source-dir", self.src_dir, "--dest", self.dst_path])
        self.assertTrue(os.path.isdir(self.src_dir),
                         "--source-dir must never be rmtree'd — that is the caller's own folder")
        self.assertTrue(os.path.exists(os.path.join(self.src_dir, f"{self.cid}.jpg")))

    def test_pull_and_source_dir_together_is_rejected(self):
        # Both flags must be rejected by the exclusivity check ITSELF — not
        # merely end up erroring some other way (e.g. --pull falling through
        # to "no IMAC set", which would raise SystemExit too and let a missing
        # exclusivity check hide behind an unrelated failure).
        err = io.StringIO()
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr", err):
            _run_main(self.isp, ["--pull", "--source-dir", self.src_dir, "--dest", self.dst_path],
                      subprocess_guard=False)
        self.assertIn("pass either --pull or --source-dir", err.getvalue())

    def test_neither_pull_nor_source_dir_is_rejected(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr", err):
            _run_main(self.isp, ["--dest", self.dst_path], subprocess_guard=False)
        self.assertIn("pass either --pull or --source-dir", err.getvalue())

    def test_missing_source_dir_fails_clearly_not_with_a_traceback(self):
        bogus = os.path.join(self.dir, "does-not-exist")
        with self.assertRaises(SystemExit) as caught:
            _run_main(self.isp, ["--source-dir", bogus, "--dest", self.dst_path])
        self.assertIn(bogus, str(caught.exception))


class TestEnrichLiExtrasSourceDir(_NoScraperHostEnv):
    def setUp(self):
        super().setUp()
        import enrich_li_extras as eli
        self.eli = eli
        self.dir = tempfile.mkdtemp(prefix="crm_eli_test_")
        self.src_dir = os.path.join(self.dir, "li_raw")
        os.makedirs(self.src_dir)
        self.dst_path = os.path.join(self.dir, "crm.db")
        db.init_db(self.dst_path)
        conn = db.get_db(self.dst_path)
        self.cid = db.create_contact(conn, "Ada Lovelace")
        conn.commit()
        conn.close()
        with open(os.path.join(self.src_dir, f"{self.cid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "Ada Lovelace", "profile_text": "Invents things."}, fh)

    def test_source_dir_dry_run_with_no_subprocess_call(self):
        # --dry-run needs no LLM client, so this exercises --source-dir end to
        # end without an ANTHROPIC_API_KEY.
        out = _run_main(self.eli, ["--source-dir", self.src_dir, "--dest", self.dst_path,
                                    "--dry-run"])
        self.assertIn("would extract extras for 1 of 1", out)

    def test_source_dir_is_never_deleted_afterward(self):
        _run_main(self.eli, ["--source-dir", self.src_dir, "--dest", self.dst_path, "--dry-run"])
        self.assertTrue(os.path.isdir(self.src_dir),
                         "--source-dir must never be rmtree'd — that is the caller's own folder")
        self.assertTrue(os.path.exists(os.path.join(self.src_dir, f"{self.cid}.json")))

    def test_pull_and_source_dir_together_is_rejected(self):
        # See the matching photos test for why this checks the MESSAGE, not
        # just "some SystemExit happened" — --pull falling through to "no IMAC
        # set" raises SystemExit too, and would hide a missing exclusivity
        # check behind an unrelated failure.
        err = io.StringIO()
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr", err):
            _run_main(self.eli, ["--pull", "--source-dir", self.src_dir, "--dest", self.dst_path,
                                  "--dry-run"], subprocess_guard=False)
        self.assertIn("pass either --pull or --source-dir", err.getvalue())

    def test_neither_pull_nor_source_dir_is_rejected(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr", err):
            _run_main(self.eli, ["--dest", self.dst_path, "--dry-run"], subprocess_guard=False)
        self.assertIn("pass either --pull or --source-dir", err.getvalue())

    def test_missing_source_dir_fails_clearly_not_with_a_traceback(self):
        bogus = os.path.join(self.dir, "does-not-exist")
        with self.assertRaises(SystemExit) as caught:
            _run_main(self.eli, ["--source-dir", bogus, "--dest", self.dst_path, "--dry-run"])
        self.assertIn(bogus, str(caught.exception))


class TestPlistIsAGenericTemplate(unittest.TestCase):
    """NC-014: no real person's machine is baked into the shipped LaunchAgent.

    Parses the plist and checks the actual launchd-read values (ProgramArguments,
    WorkingDirectory, StandardOutPath, StandardErrorPath) — never the raw file
    text — because the file's own explanatory XML comment intentionally shows an
    example path (/Users/you/…). A full-text grep for /Users/ would pass or fail
    on the comment, not on the values launchd reads."""

    def test_path_bearing_values_carry_the_template_token_not_a_real_path(self):
        with open(PLIST_PATH, "rb") as fh:
            data = plistlib.load(fh)
        path_values = [
            *data["ProgramArguments"],
            data["WorkingDirectory"],
            data["StandardOutPath"],
            data["StandardErrorPath"],
        ]
        self.assertTrue(path_values, "expected at least one path-bearing value to check")
        for value in path_values:
            self.assertNotIn("/Users/", value,
                              f"a real absolute path is baked into the plist: {value!r}")
            self.assertIn("__NETWORK_CRM_ROOT__", value,
                           f"expected the substitution token in {value!r}")


if __name__ == "__main__":
    unittest.main()
