"""NC-047 — the app must never raise a macOS consent dialog by itself.

The failure to prevent: permission questions that appear once per startup of the
program, when they need to be settled once. One such dialog says **"python3
would like to access data from other apps"** — that is the CRM app itself, not
the helper, asking for WhatsApp's group container.

The container is in the app-data TCC category, so merely probing it
(`os.path.exists` is enough) can raise that dialog. The code already knew: the
autosync loop rationed the probe to "at most once a day, and ONLY on the boot
pass ... it costs a consent dialog, so it is deliberately rare". Once per boot
pass IS once per startup. And a TCC grant made to a bare interpreter does not
durably stick, so answering it was never going to make it stop.

These tests pin the fix: when the helper has copied the container, the app does
not touch it — not on a page load, not on a timer, not at all.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db, importers  # noqa: E402

import api_import  # noqa: E402


def _touch(path, blob=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(blob)
    return path


class TestTheAppDoesNotProbeAContainerItHasACopyOf(Base):
    def setUp(self):
        super().setUp()
        self.container = tempfile.mkdtemp(prefix="wa-container-")
        self._orig_container = importers.WA_CONTAINER
        importers.WA_CONTAINER = self.container
        _touch(os.path.join(self.container, "ChatStorage.sqlite"))
        _touch(os.path.join(self.container, "Media", "Profile", "4670000-1.jpg"))

    def tearDown(self):
        importers.WA_CONTAINER = self._orig_container
        for p in (importers.WA_AUTO_DB,):
            if os.path.exists(p):
                os.remove(p)
        super().tearDown()

    def test_with_no_helper_copy_the_container_is_still_the_way_in(self):
        """The container route is not deleted. For someone who never installed
        the helper it is the only way WhatsApp works at all, and removing a
        capability to remove a control is the NC-015 defect."""
        found = importers.detect_sources()
        paths = [f["path"] for f in found]
        self.assertTrue(any(p.startswith(self.container) for p in paths),
                        "a Mac with no helper copy can no longer find WhatsApp")
        self.assertIn("whatsapp", [f["source"] for f in found])

    def test_once_the_copy_exists_the_container_is_not_in_the_findings(self):
        _touch(importers.WA_AUTO_DB)
        paths = [f["path"] for f in importers.detect_sources()]
        self.assertFalse(any(p.startswith(self.container) for p in paths),
                         "the Data page still offers the live container, so "
                         "loading the page can raise the app-data dialog")

    def test_the_container_is_never_even_STATTED_once_the_copy_exists(self):
        """The stronger claim, and the one that matches the bug: it is the PROBE
        that raises the dialog, not the read. A test that only checks the
        findings would still pass while os.path.exists sat blocked on a prompt."""
        _touch(importers.WA_AUTO_DB)
        seen = []
        real_exists, real_access = os.path.exists, os.access
        real_listdir, real_isdir = os.listdir, os.path.isdir

        def rec(fn, *a, **k):
            seen.append(str(a[0]) if a else "")
            return fn(*a, **k)

        os.path.exists = lambda p, *a, **k: rec(real_exists, p, *a, **k)
        os.access = lambda p, m, *a, **k: rec(real_access, p, m, *a, **k)
        os.listdir = lambda p=".", *a, **k: rec(real_listdir, p, *a, **k)
        os.path.isdir = lambda p, *a, **k: rec(real_isdir, p, *a, **k)
        try:
            importers.detect_sources()
            importers.detect_unprotected_sources()
        finally:
            os.path.exists, os.access = real_exists, real_access
            os.listdir, os.path.isdir = real_listdir, real_isdir
        touched = [p for p in seen if p.startswith(self.container)]
        self.assertEqual(touched, [],
                         f"the container was probed {len(touched)} time(s) "
                         f"despite a copy existing: {touched[:4]}")

    def test_the_autosync_loop_refuses_the_container_as_well(self):
        """Belt and braces on purpose. This is the function that decides to RUN an
        import, so a future finding source must not be able to smuggle one in past
        the detector-level guard.

        The findings are INJECTED rather than detected. The real detector already
        refuses to return a container path once a copy exists, so an assertion
        built on it would pass with the loop's own guard deleted. The only way to
        test the second guard is to hand it what the first one would have
        filtered out."""
        _touch(importers.WA_AUTO_DB)
        db.state_set(self.conn, "onboarded", "1")
        smuggled = [{"source": "whatsapp",
                     "path": os.path.join(self.container, "ChatStorage.sqlite"),
                     "label": "WhatsApp Desktop on this Mac", "mtime": "2026-01-01",
                     "count": 5, "blocked": False}]
        orig = importers.detect_sources
        importers.detect_sources = lambda: list(smuggled)
        try:
            pending = api_import._autosync_pending(self.conn, probe_protected=True)
        finally:
            importers.detect_sources = orig
        self.assertFalse(
            any(f["path"].startswith(self.container) for f in pending),
            "the boot pass still queues the live container, which would "
            "raise the consent dialog at every start-up")

    def test_without_a_copy_the_boot_pass_may_still_use_the_container(self):
        """The other half of the same guard: on a Mac with no helper the daily
        container pass is the only thing that imports WhatsApp at all, so the
        guard must be conditional and not a deletion."""
        db.state_set(self.conn, "onboarded", "1")
        pending = api_import._autosync_pending(self.conn, probe_protected=True)
        self.assertTrue(
            any(f["path"].startswith(self.container) for f in pending),
            "a Mac with no helper copy no longer imports WhatsApp automatically")


class TestThePhotoCacheMovedIntoOurOwnFolder(Base):
    """Media/Profile was the last thing in the app reachable only in place, and
    while that was true the container probe could not be removed. The helper now
    sweeps it (see the WhatsApp source in packaging/src/imessage_sync.swift)."""

    def setUp(self):
        super().setUp()
        # WA_PHOTOS_AUTO_DIR is a module constant shared by every test in the
        # process, not a per-test temp dir, so it has to be emptied here or one
        # test's photo leaks into the next one's "is it empty" assertion.
        shutil.rmtree(importers.WA_PHOTOS_AUTO_DIR, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(importers.WA_PHOTOS_AUTO_DIR, ignore_errors=True)
        for p in (importers.WA_AUTO_DB,):
            if os.path.exists(p):
                os.remove(p)
        super().tearDown()

    def test_the_copy_is_laid_out_so_the_importer_finds_its_database(self):
        """whatsapp_photos.find_chatstorage() looks in the folder, its parent, and
        two levels up. Copying to <WA_INBOX>/Media/Profile puts ChatStorage.sqlite
        exactly two levels up, so no argument has to be threaded through."""
        import whatsapp_photos
        _touch(importers.WA_AUTO_DB)
        os.makedirs(importers.WA_PHOTOS_AUTO_DIR, exist_ok=True)
        self.assertEqual(whatsapp_photos.find_chatstorage(importers.WA_PHOTOS_AUTO_DIR),
                         importers.WA_AUTO_DB)

    def test_an_empty_cache_is_not_offered_as_a_source(self):
        """The helper creates the destination before it knows whether the sweep
        matched anything, so "the folder exists" and "there are photos in it" are
        different questions. A row that could only ever report zero is worse than
        no row."""
        os.makedirs(importers.WA_PHOTOS_AUTO_DIR, exist_ok=True)
        self.assertFalse(importers._photo_cache_has_files(importers.WA_PHOTOS_AUTO_DIR))
        _touch(os.path.join(importers.WA_PHOTOS_AUTO_DIR, "notes.txt"))
        self.assertFalse(importers._photo_cache_has_files(importers.WA_PHOTOS_AUTO_DIR),
                         "a non-image file counts as a photo")
        _touch(os.path.join(importers.WA_PHOTOS_AUTO_DIR, "4670000-1.thumb"))
        self.assertTrue(importers._photo_cache_has_files(importers.WA_PHOTOS_AUTO_DIR),
                        ".thumb is what pick_best() falls back to and must count")

    def test_the_copy_is_a_finding_in_both_detectors(self):
        """One backs the page, the other backs the timer. A source present in only
        one either never appears or never runs — that was NC-039's shape."""
        _touch(os.path.join(importers.WA_PHOTOS_AUTO_DIR, "4670000-1.jpg"))
        for fn in (importers.detect_sources, importers.detect_unprotected_sources):
            rows = [f for f in fn() if f["source"] == "whatsapp_photos"]
            self.assertEqual([r["path"] for r in rows],
                             [importers.WA_PHOTOS_AUTO_DIR],
                             f"{fn.__name__} does not offer the copied cache")


class TestTheCacheHasAFingerprintOfItsOwn(Base):
    """NC-043's lesson, applied before it could bite again: a copy in the autosync
    loop with no state key re-imports on every single tick, forever, and nothing
    says so. The address book did exactly that."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(importers.WA_PHOTOS_AUTO_DIR, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(importers.WA_PHOTOS_AUTO_DIR, ignore_errors=True)
        super().tearDown()

    def test_the_directory_has_its_own_state_key(self):
        self.assertEqual(api_import._paste_fp_key(importers.WA_PHOTOS_AUTO_DIR),
                         api_import.WA_PHOTOS_PASTE_FP_KEY)
        keys = {api_import.IMSG_PASTE_FP_KEY, api_import.WA_PASTE_FP_KEY,
                api_import.AB_PASTE_FP_KEY, api_import.WA_PHOTOS_PASTE_FP_KEY}
        self.assertEqual(len(keys), 4, "two copies share one fingerprint key, so "
                                       "one of them will never import")

    def test_a_rewritten_file_changes_the_fingerprint(self):
        """The reason a directory needs a walk and not an os.stat: a directory's
        own mtime does not move when an existing file is rewritten in place, and
        WhatsApp overwrites <jid>-<id>.jpg when someone changes their picture."""
        d = importers.WA_PHOTOS_AUTO_DIR
        f = _touch(os.path.join(d, "4670000-1.jpg"), b"aaaa")
        os.utime(f, (1_000_000, 1_000_000))
        before = api_import._dir_fp(d)
        self.assertNotEqual(before, "")
        # SAME byte length, so neither the file count nor the total size moves and
        # the mtime is the only thing left to notice. A longer string would let
        # `newest = 0` pass.
        with open(f, "wb") as fh:
            fh.write(b"bbbb")
        os.utime(f, (2_000_000, 2_000_000))
        os.utime(d, (1, 1))          # pin the DIRECTORY's own mtime
        after = api_import._dir_fp(d)
        self.assertEqual(before.split(":")[:2], after.split(":")[:2],
                         "count and size were supposed to be identical here, so "
                         "this test is no longer exercising the mtime")
        self.assertNotEqual(after, before,
                            "rewriting a photo in place did not change the "
                            "fingerprint, so refreshed avatars never import")

    def test_a_missing_or_empty_directory_reads_as_nothing_to_do(self):
        self.assertEqual(api_import._dir_fp(os.path.join(self.dir, "nope")), "")
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        self.assertEqual(api_import._dir_fp(empty), "",
                         "an empty directory must not fingerprint as a change")


class TestEveryProtectedPathIsRedirectedInTheSuite(Base):
    """NC-044 as a general gate rather than one more remembered line.

    A path constant added without a redirect in tests/helpers.py can go unnoticed,
    because nothing fails while the real directory does not exist yet. The day the
    helper first writes it, the suite would start importing the developer's own
    address book. This fails the moment a new path constant is added without a
    redirect, whether or not the machine happens to be empty."""

    def test_no_importers_path_constant_points_at_the_real_home(self):
        home = os.path.expanduser("~")
        suffixes = ("_AUTO_DB", "_AUTO_DIR", "_INBOX", "_CONTAINER",
                    "_SOURCE_DIR", "_HELPER_BIN", "_HELPER_PLIST",
                    "_HELPER_REPORT", "_HELPER_LOG")
        offenders = []
        for name in dir(importers):
            if not name.isupper() or not name.endswith(suffixes):
                continue
            value = getattr(importers, name)
            if not isinstance(value, str) or not value:
                continue
            if os.path.abspath(value).startswith(os.path.join(home, "Library")):
                offenders.append(f"{name} = {value}")
        self.assertEqual(offenders, [],
                         "these point at the real Mac inside the test suite; add "
                         "each to tests/helpers.py in the change that adds it")

    def test_scan_home_is_not_the_real_home(self):
        """Desktop and Downloads are TCC categories of their own, and a glob of
        either blocks on a prompt rather than failing."""
        self.assertNotEqual(os.path.abspath(importers.SCAN_HOME),
                            os.path.abspath(os.path.expanduser("~")))
