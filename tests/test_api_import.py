"""crm/api_import.py: import-source detection, the import subprocess wiring and receipt parsing, and the whole auto-sync subsystem (file-backed and API-backed halves)."""

import inspect
import contextlib
import io
import datetime
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, code_of, db, importers, settings_mod, wa,
)
import api_import  # noqa: E402  — app re-exports only the router's subset;
                   # the calendar fetch and its switch are read here.
import imessage_import  # noqa: E402  (sys.path is set by tests.helpers)
import config  # noqa: E402
import gmail_accounts  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestImportDetect(Base):
    """Auto-detection + open-helpers + last-synced stamps for the Data page."""

    def test_detect_sources_shape(self):
        # This probes the machine, so it only asserts the SHAPE of the result.
        # tests/helpers.py points every scanned root at a temp dir — without that
        # this call BLOCKS on a macOS consent dialog rather than failing, and
        # hangs the whole suite. Do not "fix" it by scanning the real machine again.
        import importers
        found = importers.detect_sources()
        self.assertIsInstance(found, list)
        for f in found:
            self.assertIn(f["source"], importers.REGISTRY)
            self.assertTrue(f["path"])
            self.assertTrue(f["label"])

    def test_sources_include_synced_stamp_and_photo_count(self):
        db.state_set(self.conn, "import_synced_whatsapp", "2026-07-01T10:00:00+00:00")
        cid = db.create_contact(self.conn, "Pic Person")
        self.conn.execute("UPDATE contacts SET photo='1.jpg' WHERE id=?", (cid,))
        self.conn.commit()
        out = app.handle_import_sources(self.conn)
        by_key = {s["key"]: s for s in out["sources"]}
        self.assertEqual(by_key["whatsapp"]["synced_at"], "2026-07-01T10:00:00+00:00")
        self.assertEqual(by_key["linkedin"]["synced_at"], "")
        self.assertEqual(out["photos_count"], 1)
        self.assertTrue(by_key["imessage"]["open_helper"])   # Finder helper
        self.assertTrue(by_key["linkedin"]["open_helper"])   # token-page helper
        # iMessage is a copy/paste flow, so it offers two buttons: copy from the
        # protected folder, paste into the import folder.
        self.assertEqual([h["key"] for h in by_key["imessage"]["open_helpers"]],
                         ["imessage", "imessage_inbox"])
        self.assertEqual([h["key"] for h in by_key["linkedin"]["open_helpers"]],
                         ["linkedin"])

    def test_photo_counts_are_attributed_to_the_source_that_earned_them(self):
        """NC-030. WhatsApp's row now reports the photos its folded photo import
        put on people, so the endpoint has to say WHOSE photos they are. It used
        to return only the global total, which the photo rows displayed as their
        own — WhatsApp taking credit for every face LinkedIn contributed."""
        for name, src in (("Wa One", "whatsapp"), ("Wa Two", "whatsapp"),
                          ("Li One", "linkedin"), ("Hand", "manual")):
            cid = db.create_contact(self.conn, name)
            self.conn.execute("UPDATE contacts SET photo=?, photo_source=? WHERE id=?",
                              (f"{cid}.jpg", src, cid))
        # A contact with no photo must not count toward anyone.
        db.create_contact(self.conn, "Faceless")
        self.conn.commit()
        out = app.handle_import_sources(self.conn)
        self.assertEqual(out["photos_count"], 4)
        self.assertEqual(out["photos_by_source"],
                         {"whatsapp": 2, "linkedin": 1, "manual": 1})

    def test_open_helper_rejects_unknown_source(self):
        with self.assertRaises(app.ApiError):
            app.handle_import_open({"source": "totally-made-up"})

    def test_open_target_reveals_the_three_files_with_a_folder_fallback(self):
        import importers
        target = importers.resolve_open_target("imessage")
        # last argv is always the plain folder open — the fallback if macOS
        # refuses the reveal
        self.assertEqual(target["argv"][-1], ["open", importers.IMSG_SOURCE_DIR])
        for argv in target["argv"][:-1]:
            self.assertEqual(argv[:2], ["open", "-R"])
            self.assertTrue(argv[2:])
            for path in argv[2:]:
                self.assertEqual(os.path.dirname(path), importers.IMSG_SOURCE_DIR)
                self.assertIn(os.path.basename(path), importers.IMSG_COPY_NAMES)
        self.assertIsNone(importers.resolve_open_target("/etc/passwd"))
        self.assertIsNone(importers.resolve_open_target("imessage_nope"))

    def test_import_folder_target_creates_it_private(self):
        import importers
        inbox = os.path.join(self.dir, "imessage")
        orig = importers.OPEN_TARGETS["imessage_inbox"]
        importers.OPEN_TARGETS["imessage_inbox"] = {**orig, "path": inbox}
        try:
            target = importers.resolve_open_target("imessage_inbox")
        finally:
            importers.OPEN_TARGETS["imessage_inbox"] = orig
        self.assertEqual(target["argv"], [["open", inbox]])
        self.assertTrue(os.path.isdir(inbox))
        # it will hold a copy of every message ever sent — owner only
        self.assertEqual(oct(os.stat(inbox).st_mode)[-3:], "700")

    def test_import_folder_label_credits_the_helper_only_when_it_actually_ran(self):
        """The label is a claim about freshness, so it has to come from the helper's
        last run — not from the LaunchAgent existing.

        A test that stubs imsg_helper_installed() and asserts the label follows it
        locks in a bug: Full Disk Access is a separate manual grant that nothing
        prompts for, so "installed" and "working" are different facts, and the gap
        between them is the DEFAULT state of a fresh install. With the helper
        installed but blocked, the Data page would say "kept current by the iMessage
        helper" a few rows under its own panel saying "macOS is blocking iMessage".

        Four cases, because the two middle ones are the ones that bite.
        """
        import importers
        chatdb = os.path.join(self.dir, "chat.db")
        sqlite3.connect(chatdb).close()
        orig_db, orig_report = importers.IMSG_AUTO_DB, importers.imsg_helper_report
        importers.IMSG_AUTO_DB = chatdb

        def label_now():
            found = [f for f in importers.detect_sources() if f["path"] == chatdb]
            self.assertEqual(len(found), 1)
            return found[0]["label"]

        def reports(outcome):
            importers.imsg_helper_report = lambda: {
                "sources": [{"label": "iMessage", "outcome": outcome},
                            {"label": "WhatsApp", "outcome": "copied", "bytes": 1}]}
        try:
            # 1. No report at all — never installed, or too old to report.
            importers.imsg_helper_report = lambda: {}
            self.assertEqual(label_now(), importers.AUTO_COPY_MANUAL)  # you pasted it

            # 2. Installed and BLOCKED. The state every new install is in until
            #    it is given Full Disk Access, and the one that used to lie.
            reports("blocked")
            self.assertEqual(label_now(), importers.AUTO_COPY_MANUAL)

            # 3. Installed, and macOS is hiding the source entirely.
            reports("absent")
            self.assertEqual(label_now(), importers.AUTO_COPY_MANUAL)

            # 4. Actually working — the only case that earns the claim.
            for good in ("copied", "current"):
                with self.subTest(outcome=good):
                    reports(good)
                    self.assertEqual(label_now(), importers.AUTO_COPY_HELPER)
        finally:
            importers.IMSG_AUTO_DB = orig_db
            importers.imsg_helper_report = orig_report

    def test_the_two_sources_are_judged_separately(self):
        """A blocked iMessage beside a copied WhatsApp is the NORMAL shape on a Mac
        with WhatsApp Desktop, not an edge case — WhatsApp needs no Full Disk Access.
        Collapsing the two into one "is the helper working" is a known mistake (see
        the exit-code contract in imessage_sync.swift)."""
        import importers
        orig = importers.imsg_helper_report
        importers.imsg_helper_report = lambda: {
            "sources": [{"label": "iMessage", "outcome": "blocked"},
                        {"label": "WhatsApp", "outcome": "copied", "bytes": 70}]}
        try:
            self.assertEqual(importers.auto_copy_label(importers.HELPER_IMESSAGE),
                             importers.AUTO_COPY_MANUAL)
            self.assertEqual(importers.auto_copy_label(importers.HELPER_WHATSAPP),
                             importers.AUTO_COPY_HELPER)
        finally:
            importers.imsg_helper_report = orig

    def test_an_unknown_outcome_does_not_earn_the_claim(self):
        """A report written by a newer helper than this app understands must fail
        closed. "I do not recognise this" is not "everything is fine"."""
        import importers
        orig = importers.imsg_helper_report
        importers.imsg_helper_report = lambda: {
            "sources": [{"label": "iMessage", "outcome": "quarantined-by-something-new"}]}
        try:
            self.assertEqual(importers.auto_copy_label(importers.HELPER_IMESSAGE),
                             importers.AUTO_COPY_MANUAL)
        finally:
            importers.imsg_helper_report = orig

    def test_pasted_copy_is_auto_but_still_reports_its_age(self):
        """Regression: stale_days used to be computed only for non-auto findings,
        so the pasted chat.db claimed to be 0 days old forever."""
        import importers
        chatdb = os.path.join(self.dir, "chat.db")
        open(chatdb, "wb").close()
        old = datetime.datetime.now().timestamp() - 9 * 86400
        os.utime(chatdb, (old, old))
        wa_db = os.path.join(importers.WA_CONTAINER, "ChatStorage.sqlite")
        orig_detect, orig_db = importers.detect_sources, importers.IMSG_AUTO_DB
        importers.IMSG_AUTO_DB = chatdb
        importers.detect_sources = lambda: [
            {"source": "imessage", "path": chatdb, "label": "x", "mtime": ""},
            {"source": "whatsapp", "path": wa_db, "label": "y", "mtime": ""},
        ]
        # `auto` now means "run_autosync really will take this", so setup has to
        # be finished for it to be true at all — see
        # test_auto_is_false_until_setup_is_finished directly below.
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        try:
            by_src = {f["source"]: f
                      for f in app.handle_import_detect(self.conn)["found"]}
        finally:
            importers.detect_sources, importers.IMSG_AUTO_DB = orig_detect, orig_db
        self.assertTrue(by_src["imessage"]["auto"])        # imports itself…
        self.assertFalse(by_src["imessage"]["live"])       # …but it IS a snapshot
        self.assertEqual(by_src["imessage"]["stale_days"], 9)
        self.assertTrue(by_src["whatsapp"]["live"])        # read in place
        self.assertEqual(by_src["whatsapp"]["stale_days"], 0)

    def test_auto_is_false_until_setup_is_finished(self):
        """The Data page turns `auto` into a sentence — an auto row says the
        source "syncs itself and is current" and offers no button. Before setup
        that is false, because _autosync_pending returns nothing, so the flag has
        to be false too and the finding has to land under "Needs you"."""
        import importers
        chatdb = os.path.join(self.dir, "chat.db")
        open(chatdb, "wb").close()
        wa_db = os.path.join(importers.WA_CONTAINER, "ChatStorage.sqlite")
        orig_detect, orig_db = importers.detect_sources, importers.IMSG_AUTO_DB
        importers.IMSG_AUTO_DB = chatdb
        importers.detect_sources = lambda: [
            {"source": "imessage", "path": chatdb, "label": "x", "mtime": ""},
            {"source": "whatsapp", "path": wa_db, "label": "y", "mtime": ""},
        ]
        try:
            before = {f["source"]: f
                      for f in app.handle_import_detect(self.conn)["found"]}
            db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
            self.conn.commit()
            after = {f["source"]: f
                     for f in app.handle_import_detect(self.conn)["found"]}
        finally:
            importers.detect_sources, importers.IMSG_AUTO_DB = orig_detect, orig_db
        for src in ("imessage", "whatsapp"):
            self.assertFalse(before[src]["auto"],
                             f"{src} claimed to sync itself before setup")
            self.assertTrue(after[src]["auto"],
                            f"{src} should sync itself once setup is done")
        # `live` is a statement about HOW the file is read, not about who runs the
        # import, so the gate must not touch it.
        self.assertTrue(before["whatsapp"]["live"])


class TestAStaleCopySaysWhyItIsStale(Base):
    """NC-045. The Data page told the user to "paste a fresh copy into the import
    folder" for the Contacts copy — a file the helper rewrites every two minutes
    and was merely being refused permission to refresh. Pasting would not have
    fixed it; the one grant named in the panel three rows above would.

    `auto_copy_label` already asks the helper's report who wrote the file. This is
    the other half of the same question: WHY has it stopped moving. Kept separate
    from the label because the label is shown always and this only when the copy
    has gone behind."""

    def _report(self, outcome):
        import importers
        importers.imsg_helper_report = lambda: {
            "sources": [{"label": importers.HELPER_CONTACTS, "outcome": outcome}]}

    def setUp(self):
        super().setUp()
        import importers
        self._orig = importers.imsg_helper_report

    def tearDown(self):
        import importers
        importers.imsg_helper_report = self._orig
        super().tearDown()

    def test_only_a_refusal_counts_as_blocked(self):
        import importers
        self._report("blocked")
        self.assertTrue(importers.helper_blocked(importers.HELPER_CONTACTS))
        # "absent" is the source not being on this Mac, which no permission fixes.
        for benign in ("copied", "current", "absent"):
            with self.subTest(outcome=benign):
                self._report(benign)
                self.assertFalse(importers.helper_blocked(importers.HELPER_CONTACTS))

    def test_an_unknown_outcome_is_not_read_as_a_permission_problem(self):
        """Fails closed the same way auto_copy_label does: a report from a newer
        helper must not make the page name a fix that is not the fix."""
        import importers
        self._report("quarantined-by-something-new")
        self.assertFalse(importers.helper_blocked(importers.HELPER_CONTACTS))
        importers.imsg_helper_report = lambda: {}
        self.assertFalse(importers.helper_blocked(importers.HELPER_CONTACTS))

    def test_a_source_the_report_never_mentions_is_not_blocked(self):
        import importers
        self._report("blocked")
        self.assertFalse(importers.helper_blocked(importers.HELPER_IMESSAGE))

    def test_the_flag_reaches_the_finding_the_page_renders(self):
        """Both detectors, because one backs the page and the other backs the
        timer — a flag present in only one is the NC-039 shape of bug."""
        import importers
        db_path = os.path.join(self.dir, "AddressBook-v22.abcddb")
        open(db_path, "wb").close()
        orig = importers.AB_AUTO_DB
        importers.AB_AUTO_DB = db_path
        try:
            for outcome, want in (("blocked", True), ("current", False)):
                self._report(outcome)
                for fn in (importers.detect_sources,
                           importers.detect_unprotected_sources):
                    rows = [f for f in fn() if f["path"] == db_path]
                    self.assertEqual(len(rows), 1, f"{fn.__name__} lost the finding")
                    self.assertEqual(rows[0]["blocked"], want,
                                     f"{fn.__name__} reported blocked="
                                     f"{rows[0]['blocked']} for outcome {outcome}")
        finally:
            importers.AB_AUTO_DB = orig

    def test_every_finding_carries_the_key_so_the_page_never_reads_undefined(self):
        """A .vcf in Downloads has no helper behind it, so the answer is False —
        but the key has to be there. `finding.blocked` on an object without it is
        `undefined`, which is falsy and would have worked by luck until someone
        wrote `!f.blocked`."""
        import importers
        for f in importers.detect_sources():
            self.assertIn("blocked", f, f"{f['source']} finding has no blocked key")


class TestSnapshotUriReadsTheLogWithoutWritingToDisk(unittest.TestCase):
    """imessage_import.snapshot_uri: the newest messages live in chat.db-wal, and
    the old `immutable=1` open ignored that file. This is the SSD-wear fix from
    NC-028 — the requirement is not just "sees the new rows" (a checkpoint did
    that too) but "sees them without writing to the snapshot"."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="snapshot_uri_")
        self.src = os.path.join(self.dir, "src.db")
        self.copy = os.path.join(self.dir, "chat.db")
        # A WAL-mode db whose newest rows are still only in the log — exactly the
        # state ~/Library/Messages is in while the Messages daemons hold it open.
        # The writer stays OPEN for the whole test: closing it would checkpoint
        # the source and fold the rows into the main file, which quietly makes
        # every assertion below pass for the wrong reason.
        self.conn = sqlite3.connect(self.src)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE handle(id TEXT)")
        self.conn.execute("INSERT INTO handle VALUES('+46700000000')")
        self.conn.commit()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.executemany("INSERT INTO handle VALUES(?)",
                              [(f"+4670000{i:04d}",) for i in range(20)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _copy(self, *suffixes):
        for suffix in suffixes:
            shutil.copy2(self.src + suffix, self.copy + suffix)

    def _rows(self, path=None):
        uri = imessage_import.snapshot_uri(path or self.copy)
        conn = sqlite3.connect(uri, uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM handle").fetchone()[0]
        finally:
            conn.close()

    def test_a_full_copy_sees_the_newest_messages(self):
        self._copy("", "-wal", "-shm")
        # The regression this guards: immutable=1 returns 1 of 21 here, silently.
        self.assertEqual(
            sqlite3.connect(f"file:{self.copy}?immutable=1", uri=True)
            .execute("SELECT COUNT(*) FROM handle").fetchone()[0], 1)
        self.assertEqual(self._rows(), 21)

    def test_reading_a_full_copy_writes_nothing_at_all(self):
        self._copy("", "-wal", "-shm")
        before = {s: os.stat(self.copy + s).st_mtime_ns for s in ("", "-wal", "-shm")}
        sizes = {s: os.path.getsize(self.copy + s) for s in ("", "-wal", "-shm")}
        self.assertEqual(self._rows(), 21)
        for suffix in ("", "-wal", "-shm"):
            self.assertEqual(os.path.getsize(self.copy + suffix), sizes[suffix],
                             f"reading the snapshot resized {suffix or 'the main file'}")
        # The main file is the expensive one: a large APFS clone, where any write
        # breaks copy-on-write block by block. It must not be touched.
        self.assertEqual(os.stat(self.copy).st_mtime_ns, before[""])

    def test_a_copy_without_the_shm_still_reads_and_leaves_the_main_file_alone(self):
        self._copy("", "-wal")                     # what a two-file copy looks like
        size = os.path.getsize(self.copy)
        self.assertEqual(self._rows(), 21)
        # SQLite rebuilds the 32 KB wal-index it needs. That write is fine — it is
        # a sidecar in our own import folder, and the helper normally supplies it.
        self.assertEqual(os.path.getsize(self.copy), size)

    def test_a_one_file_copy_still_opens_instead_of_failing(self):
        lone = os.path.join(self.dir, "lone.db")
        shutil.copy2(self.src, lone)               # chat.db, no sidecars
        # mode=ro cannot open a WAL-mode database with no -wal beside it, so the
        # chooser must fall back to immutable=1 here. Only the checkpointed row
        # is visible, because the other 20 are in a log this copy doesn't have —
        # missing data the user can fix, not a crash.
        self.assertIn("immutable=1", imessage_import.snapshot_uri(lone))
        self.assertEqual(self._rows(lone), 1)

    def test_another_apps_library_folder_never_gets_mode_ro(self):
        """mode=ro creates a 32 KB -shm beside the file. Inside ~/Library/Messages
        or WhatsApp's Group Container that would be writing into another running
        application's live data, so those reads stay immutable."""
        lib = os.path.join(self.dir, "Library")
        theirs = os.path.join(lib, "Messages")
        os.makedirs(theirs)
        victim = os.path.join(theirs, "chat.db")
        for suffix in ("", "-wal", "-shm"):
            shutil.copy2(self.src + suffix, victim + suffix)
        with mock.patch.object(imessage_import, "LIBRARY_ROOT", lib), \
             mock.patch.object(imessage_import.config, "APP_SUPPORT_DIR",
                               os.path.join(lib, "Application Support", "NetworkCRM")):
            uri = imessage_import.snapshot_uri(victim)
        self.assertIn("immutable=1", uri)
        self.assertNotIn("mode=ro", uri)

    def test_our_own_snapshot_folder_is_not_caught_by_that_guard(self):
        """Our snapshots live at ~/Library/Application Support/NetworkCRM — inside
        ~/Library. A blanket "deny everything under Library" guard compiles,
        passes every other test, and silently drops the newest messages on the one
        path that actually matters."""
        lib = os.path.join(self.dir, "Library")
        ours = os.path.join(lib, "Application Support", "NetworkCRM", "imessage")
        os.makedirs(ours)
        snap = os.path.join(ours, "chat.db")
        for suffix in ("", "-wal", "-shm"):
            shutil.copy2(self.src + suffix, snap + suffix)
        with mock.patch.object(imessage_import, "LIBRARY_ROOT", lib), \
             mock.patch.object(imessage_import.config, "APP_SUPPORT_DIR",
                               os.path.join(lib, "Application Support", "NetworkCRM")):
            uri = imessage_import.snapshot_uri(snap)
            self.assertIn("mode=ro", uri)
            conn = sqlite3.connect(uri, uri=True)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM handle").fetchone()[0], 21)
            conn.close()

    def test_the_shipped_snapshot_path_sits_under_our_own_support_dir(self):
        """Ties the guard to production. If IMSG_AUTO_DB ever moves out from under
        APP_SUPPORT_DIR, the guard starts denying the live snapshot and the fix
        silently reverts to dropping the newest messages.

        Asserted against the source, not the runtime values: tests/helpers.py
        repoints IMSG_AUTO_DB at a temp dir on purpose, so nothing here ever
        touches the real machine."""
        support = self._literal("config.py", r'APP_SUPPORT_DIR = os\.path\.expanduser\("([^"]+)"\)')
        snap = self._literal("importers.py",
                             r'IMSG_AUTO_DB = os\.path\.expanduser\(\s*"([^"]+)"')
        self.assertTrue(snap.startswith(support + "/"),
                        f"{snap!r} is not under {support!r} — snapshot_uri's guard "
                        f"would treat our own snapshot as another app's data")

    def _literal(self, filename, pattern):
        import re
        src = io.open(os.path.join(REPO, "crm", filename), encoding="utf-8").read()
        m = re.search(pattern, src)
        self.assertIsNotNone(m, f"{pattern} not found in crm/{filename}")
        return m.group(1)

    def test_never_points_mode_ro_at_a_live_protected_database(self):
        """mode=ro creates an -shm next to the file. Doing that inside
        ~/Library/Messages or WhatsApp's Group Container would write into another
        application's live data, so those reads must stay immutable."""
        import re
        src = io.open(os.path.join(REPO, "crm", "whatsapp_import.py"),
                      encoding="utf-8").read()
        src += io.open(os.path.join(REPO, "crm", "whatsapp_photos.py"),
                       encoding="utf-8").read()
        self.assertNotIn("snapshot_uri", src,
                         "WhatsApp is read in place, not from a copy — it must not "
                         "use snapshot_uri()")
        imsg = io.open(os.path.join(REPO, "crm", "imessage_import.py"),
                       encoding="utf-8").read()
        # The one legitimate in-place read left in imessage_import.py is the
        # macOS AddressBook; everything else reads our copy.
        code = "\n".join(ln for ln in imsg.splitlines()
                          if not ln.lstrip().startswith(("#", "*")))
        raw = re.findall(r'sqlite3\.connect\(\s*f?"file:\{(\w+)\}\?([^"]*)"', code)
        self.assertEqual(raw, [("path", "immutable=1&nolock=1")],
                         f"unexpected raw sqlite URI(s) in imessage_import: {raw}")


class TestParseReceipts(unittest.TestCase):
    """importers.parse_receipts — importer stdout → verb-count receipt chips."""

    def test_whatsapp_style_summary(self):
        out = importers.parse_receipts(
            "Reading WhatsApp database: /tmp/ChatStorage.sqlite\n"
            "  40 one-to-one chats (3 groups ignored)\n\n"
            "Matched to existing contacts: 40\n"
            "New contacts created:        0\n"
            "Contacts updated:            40\n\n"
            "Archiving message content…\n"
            "  1,300 new messages archived  (8,000 already stored)\n"
            "  25 conversation chunks ready for search\n"
        )
        self.assertEqual(out, [
            {"verb": "Matched", "count": 40},
            {"verb": "Added", "count": 0},
            {"verb": "Updated", "count": 40},
            {"verb": "Archived", "count": 1300},
            {"verb": "Chunked", "count": 25},
        ])

    def test_a_calendar_import_that_wrote_says_added(self):
        """A real calendar import prints its own receipt and not the preview's
        "would be added" line, so its card needs the "Added" chip from it."""
        out = importers.parse_receipts(
            "  12 calendar events read.\n"
            "  3 meetings added.\n"
        )
        self.assertEqual(out, [{"verb": "Read", "count": 12}, {"verb": "Added", "count": 3}])
        self.assertEqual(importers.parse_receipts("  1 meeting added.\n"),
                         [{"verb": "Added", "count": 1}])

    def test_merge_scraped_facts_summary(self):
        out = importers.parse_receipts(
            "Merged 18 of 42 scraped contacts.\n"
            "  already up to date: 20\n"
            "  skipped (manual edit kept): 4\n"
        )
        self.assertEqual(out, [{"verb": "Merged", "count": 18}])

    def test_linkedin_style_summary(self):
        out = importers.parse_receipts(
            "Fetching LinkedIn connections…\n"
            "  500 connection records from API\n"
            "New contacts added:    3\n"
            "Existing backfilled:   17\n"
            "\nFetching LinkedIn messages…\n"
            "  520 new messages archived  (2,000 already stored, 12 unmatched contacts)\n"
            "  44 conversation chunks ready for search\n"
        )
        self.assertEqual(out, [
            {"verb": "Added", "count": 3},
            {"verb": "Archived", "count": 520},
            {"verb": "Chunked", "count": 44},
        ])

    def test_whatsapp_real_output_archives_the_total(self):
        # whatsapp_import prints two per-scope "N new" lines plus one canonical
        # total "N new messages archived" — the receipt shows the total, once.
        out = importers.parse_receipts(
            "Matched to existing contacts: 40\n"
            "New contacts created:        0\n"
            "  1-to-1: 300 new  (2,100 already stored)\n"
            "  groups: 4 new  (300 already stored)\n"
            "  304 new messages archived  (2,400 already stored)\n"
            "  25 conversation chunks ready for search\n"
        )
        self.assertEqual(out, [
            {"verb": "Matched", "count": 40},
            {"verb": "Added", "count": 0},
            {"verb": "Archived", "count": 304},
            {"verb": "Chunked", "count": 25},
        ])

    def test_gmail_number_first_new_contacts(self):
        # gmail prints "3 new contacts created" (count first, no colon) and its
        # final "Done. N new messages archived across K account(s)." line.
        out = importers.parse_receipts(
            "  120 new  40 already stored  8 contacts  (scanned 5,000 messages)\n"
            "  3 new contacts created\n"
            "  9 conversation chunks updated\n"
            "\nDone. 120 new messages archived across 2 account(s).\n"
        )
        self.assertEqual(out, [
            {"verb": "Added", "count": 3},
            {"verb": "Chunked", "count": 9},
            {"verb": "Archived", "count": 120},
        ])

    def test_offline_facts_enriched_total(self):
        out = importers.parse_receipts(
            "Enriching 500 contacts (Pass A + B)…\n"
            "Pass A: 40 contacts enriched (area code / TLD / title)\n"
            "Pass B: 12 contacts enriched (message scan)\n"
            "Enriched 47 contacts (offline facts pass)\n"
        )
        self.assertEqual(out, [{"verb": "Enriched", "count": 47}])

    def test_photo_import_receipt(self):
        self.assertEqual(
            importers.parse_receipts("Photos saved/refreshed:         31\n"),
            [{"verb": "Photos", "count": 31}])
        self.assertEqual(
            importers.parse_receipts("Photos imported:             12\n"),
            [{"verb": "Photos", "count": 12}])

    def test_nothing_recognizable_is_empty(self):
        self.assertEqual(importers.parse_receipts(""), [])
        self.assertEqual(importers.parse_receipts(
            "Traceback (most recent call last):\n  boom\n"), [])

    def test_dedupe_keeps_first_occurrence(self):
        out = importers.parse_receipts(
            "  10 new messages archived\n  99 new messages archived\n")
        self.assertEqual(out, [{"verb": "Archived", "count": 10}])


class TestAutosync(Base):
    """Auto-sync gating: WhatsApp's due-today logic, the pasted iMessage copy's
    per-snapshot logic, and the switches for both."""

    def setUp(self):
        super().setUp()
        # Every test in here is about autosync MECHANICS, which only run once
        # setup is finished — _autosync_pending gates on
        # api_instance.onboarding_done before it looks at anything else. Base
        # builds an empty DB with no flag, i.e. a first-run install, so without
        # this the whole class would assert [] and prove nothing. Same reasoning
        # as Base turning the API-backed half off: neutralise the precondition
        # you are not testing. TestAutosyncConsentGate owns the gate itself.
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()

    @contextlib.contextmanager
    def _paste(self, mtime=None):
        """A chat.db sitting in the import folder, as if pasted in Finder.

        A context manager because it rebinds two importers globals. Returning the
        path and leaving the restore to each caller's own try/finally would leak
        into other test files' tests when they run in the same process.
        """
        orig_detect, orig_db = importers.detect_sources, importers.IMSG_AUTO_DB
        chatdb = os.path.join(self.dir, "chat.db")
        conn = sqlite3.connect(chatdb)
        conn.execute("CREATE TABLE handle(id TEXT)")
        conn.commit()
        conn.close()
        if mtime:
            os.utime(chatdb, (mtime, mtime))
        importers.IMSG_AUTO_DB = chatdb
        importers.detect_sources = lambda: [
            {"source": "imessage", "path": chatdb, "label": "your copy", "mtime": ""}]
        try:
            yield chatdb
        finally:
            importers.detect_sources, importers.IMSG_AUTO_DB = orig_detect, orig_db

    def test_pasted_copy_imports_once_per_snapshot(self):
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with self._paste() as chatdb:
            # today's WhatsApp run already happened — the paste must not have to
            # wait for tomorrow because of it
            db.state_set(self.conn, app.AUTOSYNC_STATE_KEY, today)
            self.assertEqual([f["source"] for f in app._autosync_pending(self.conn)],
                             ["imessage"])

            app._stamp_pasted_copy(self.conn, chatdb)   # what a finished import does
            self.assertEqual(app._autosync_pending(self.conn), [])

            # re-pasting the same file: Finder preserves the original's mtime, so
            # the fingerprint is unchanged and nothing re-runs
            stat = os.stat(chatdb)
            os.utime(chatdb, (stat.st_atime, stat.st_mtime))
            self.assertEqual(app._autosync_pending(self.conn), [])

            # a fresher copy is a new snapshot — due again
            newer = stat.st_mtime + 3600
            os.utime(chatdb, (newer, newer))
            self.assertEqual([f["source"] for f in app._autosync_pending(self.conn)],
                             ["imessage"])

            # and the switch turns just this half off
            os.environ["NC_IMSG_AUTOIMPORT"] = "0"
            try:
                self.assertEqual(app._autosync_pending(self.conn), [])
            finally:
                os.environ.pop("NC_IMSG_AUTOIMPORT", None)

    def test_run_autosync_imports_a_fresh_paste_then_leaves_it_alone(self):
        orig_run, orig_refresh, orig_path = app.subprocess.run, app.engagement.refresh, db.DB_PATH
        calls = []

        class _Result:
            returncode, stdout, stderr = 0, "", ""

        app.subprocess.run = lambda cmd, **kw: (calls.append(cmd), _Result())[1]
        app.engagement.refresh = lambda conn: None
        db.DB_PATH = self.path            # never the real crm.db
        try:
            with self._paste() as chatdb:
                self.assertEqual(app.run_autosync(), 1)
                # Three subprocesses: the import, then the two passes the app runs
                # FOR the user with no row to click — free facts, and the writing
                # voice re-measure. It used to be one: both chains existed only on
                # the clicked path, so a background import never enriched and the
                # voice profile never refreshed at all. See NC-038.
                self.assertEqual(len(calls), 3, calls)
                self.assertIn("--chatdb", calls[0])
                self.assertIn(chatdb, calls[0])
                chained = " ".join(str(a) for c in calls[1:] for a in c)
                self.assertIn("enrich_facts.py", chained)
                self.assertIn("style_profile.py", chained)
                self.assertNotIn("--llm", chained,
                                 "a chained pass must never be one that costs money")
                # same bytes on the next tick → no second import
                self.assertEqual(app.run_autosync(), 0)
                self.assertEqual(len(calls), 3)
        finally:
            app.subprocess.run, app.engagement.refresh, db.DB_PATH = orig_run, orig_refresh, orig_path

    def test_the_background_chains_are_rate_limited(self):
        """The loop can fire every couple of minutes; the free facts pass is a full
        offline sweep over every contact. Chaining it unconditionally would be the
        same mistake as NC-028 with a different resource."""
        orig_run, orig_refresh, orig_path = app.subprocess.run, app.engagement.refresh, db.DB_PATH
        calls = []

        class _Result:
            returncode, stdout, stderr = 0, "", ""

        app.subprocess.run = lambda cmd, **kw: (calls.append(cmd), _Result())[1]
        app.engagement.refresh = lambda conn: None
        db.DB_PATH = self.path
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            # Both stamped just now → neither pass is due, so the import runs alone.
            for key in (app.FACTS_CHAIN_STATE_KEY, app.STYLE_CHAIN_STATE_KEY):
                db.state_set(self.conn, key, now.isoformat(timespec="seconds"))
            self.conn.commit()
            self.assertFalse(app._facts_chain_is_due(self.conn))
            self.assertFalse(app._style_chain_is_due(self.conn))
            with self._paste():
                self.assertEqual(app.run_autosync(), 1)
                self.assertEqual(len(calls), 1,
                                 f"a chained pass was not skipped: {calls}")
            for key, minutes, due in (
                    (app.FACTS_CHAIN_STATE_KEY, app.FACTS_CHAIN_MIN_MINUTES,
                     app._facts_chain_is_due),
                    (app.STYLE_CHAIN_STATE_KEY, app.STYLE_CHAIN_MIN_MINUTES,
                     app._style_chain_is_due)):
                with self.subTest(key=key):
                    # Just past its own window → due again.
                    db.state_set(self.conn, key, (
                        now - datetime.timedelta(minutes=minutes + 1)
                    ).isoformat(timespec="seconds"))
                    self.conn.commit()
                    self.assertTrue(due(self.conn))
                    # Garbage and absence both mean DUE. Failing closed would let
                    # one bad state value stop the pass forever, silently.
                    for bad in ("not-a-date", ""):
                        db.state_set(self.conn, key, bad)
                        self.conn.commit()
                        self.assertTrue(due(self.conn), bad)
                    db.state_set(self.conn, key, now.isoformat(timespec="seconds"))
                    self.conn.commit()
            # The two windows are deliberately different: facts is hourly, the
            # voice re-measure reads the whole archive and is weekly.
            self.assertGreater(app.STYLE_CHAIN_MIN_MINUTES, app.FACTS_CHAIN_MIN_MINUTES)
        finally:
            app.subprocess.run, app.engagement.refresh, db.DB_PATH = orig_run, orig_refresh, orig_path

    def test_autosync_interval_is_a_setting(self):
        self.assertEqual(app.autosync_interval_seconds(),
                         app.AUTOSYNC_MINUTES_DEFAULT * 60)
        # The garbage case falls back to the default, so it is derived from the
        # constant rather than spelled out — hardcoding it as 900 is what made
        # this test the only thing that broke when the default went 15 -> 2.
        cases = (("30", 1800), ("0", 60), ("99999", 86400),
                 ("x", app.AUTOSYNC_MINUTES_DEFAULT * 60))
        for value, expected in cases:
            os.environ["NC_AUTOSYNC_MINUTES"] = value
            try:
                self.assertEqual(app.autosync_interval_seconds(), expected)
            finally:
                os.environ.pop("NC_AUTOSYNC_MINUTES", None)

    def test_pending_respects_daily_stamp_and_toggle(self):
        import importers
        orig = importers.detect_sources
        importers.detect_sources = lambda: [
            {"source": "whatsapp", "path": importers.WA_CONTAINER + "/ChatStorage.sqlite",
             "label": "x", "mtime": ""},
            {"source": "imessage", "path": "/Users/x/Desktop/chat.db",   # user copy — never auto
             "label": "y", "mtime": ""},
        ]
        try:
            pending = app._autosync_pending(self.conn)
            self.assertEqual([f["source"] for f in pending], ["whatsapp"])

            today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            db.state_set(self.conn, app.AUTOSYNC_STATE_KEY, today)
            self.assertEqual(app._autosync_pending(self.conn), [])

            db.state_set(self.conn, app.AUTOSYNC_STATE_KEY, "2020-01-01")
            os.environ["NC_AUTOSYNC"] = "0"
            try:
                self.assertEqual(app._autosync_pending(self.conn), [])
            finally:
                os.environ.pop("NC_AUTOSYNC", None)
        finally:
            importers.detect_sources = orig


class TestAutosyncConsentGate(Base):
    """Nothing may be imported before the first-run wizard has been seen.

    Without this gate, a shipped bundle with an empty data dir would auto-import
    WhatsApp from its live container within seconds of first launch, and that
    import would push the contact count past ONBOARDED_GRANDFATHER_CONTACTS, so
    /api/onboarding would answer {"done": true} and the wizard would never appear
    at all. Two bugs in one chain: an import nobody consented to, and the wizard
    it silently suppressed.

    Base gives us exactly the shape that broke: an empty DB with no flag.
    """

    def _pasted_copy(self):
        """detect_sources() reporting one finding, as a first launch would."""
        chatdb = os.path.join(self.dir, "chat.db")
        c = sqlite3.connect(chatdb)
        c.execute("CREATE TABLE handle(id TEXT)")
        c.commit()
        c.close()
        return chatdb

    @contextlib.contextmanager
    def _one_finding(self):
        orig_detect, orig_db = importers.detect_sources, importers.IMSG_AUTO_DB
        chatdb = self._pasted_copy()
        importers.IMSG_AUTO_DB = chatdb
        importers.detect_sources = lambda: [
            {"source": "imessage", "path": chatdb, "label": "your copy", "mtime": ""}]
        try:
            yield chatdb
        finally:
            importers.detect_sources, importers.IMSG_AUTO_DB = orig_detect, orig_db

    def test_nothing_is_pending_before_setup_is_finished(self):
        with self._one_finding():
            self.assertEqual(app._autosync_pending(self.conn), [],
                             "a first-run install must import nothing on its own")

    def test_run_autosync_imports_nothing_before_setup_is_finished(self):
        # run_autosync() opens its own connection from db.DB_PATH, so that has to
        # point at this fixture — patched on the owner, never rebound on app.
        with self._one_finding(), \
             mock.patch.object(app.db, "DB_PATH", self.path):
            self.assertEqual(app.run_autosync(), 0)

    def test_the_gate_is_the_only_thing_holding_it_back(self):
        """Same fixture, flag set — proves the [] above is the gate and not a
        broken fixture that would have found nothing anyway."""
        with self._one_finding():
            db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
            self.conn.commit()
            self.assertEqual([f["source"] for f in app._autosync_pending(self.conn)],
                             ["imessage"])

    def test_autosync_cannot_suppress_the_wizard(self):
        """The bug chain, end to end: sync runs on a first launch, and the
        wizard must still be waiting afterwards."""
        with self._one_finding(), \
             mock.patch.object(app.db, "DB_PATH", self.path):
            app.run_autosync()
        self.assertEqual(app.handle_onboarding_get(self.conn), {"done": False})

    def test_skipping_setup_still_turns_sync_on(self):
        """"Skip setup" POSTs done=true. The user has seen what the app reads, so
        that counts as consent — the gate must not strand them with no sync."""
        app.handle_onboarding_post(self.conn, {"done": True})
        with self._one_finding():
            self.assertEqual([f["source"] for f in app._autosync_pending(self.conn)],
                             ["imessage"])

    def test_onboarding_done_never_writes(self):
        """The read-only twin must stay read-only: if it persisted the
        grandfather flag, autosync would be deciding its own permission from a
        background thread, which is the loop the split exists to break."""
        for _ in range(3):
            self.assertFalse(app.onboarding_done(self.conn))
        self.assertEqual(db.state_get(self.conn, app.ONBOARDED_STATE_KEY, ""), "",
                         "onboarding_done() wrote the flag it was only asked to read")

    def test_a_real_pre_existing_network_is_still_grandfathered(self):
        """The grandfather rule itself is correct and must survive: someone
        upgrading with a real network gets no first-run wizard."""
        for i in range(app.ONBOARDED_GRANDFATHER_CONTACTS + 1):
            db.create_contact(self.conn, f"Person {i}", source="test")
        self.conn.commit()
        self.assertTrue(app.onboarding_done(self.conn))
        with self._one_finding():
            self.assertEqual([f["source"] for f in app._autosync_pending(self.conn)],
                             ["imessage"])


class TestApiAutosync(Base):
    """Gmail and LinkedIn sync on a clock rather than by appearing on disk. No
    network here — only the gating, the argv, and the since-date arithmetic."""

    def setUp(self):
        super().setUp()
        # See TestAutosync.setUp: the consent gate is a precondition here too,
        # not the thing under test.
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        for spec in app.API_SOURCES.values():   # Base turns these off
            os.environ[spec["switch"]] = "1"
        # Every source also needs its credential present, or the `needs` gate
        # skips it before scheduling is ever consulted. These tests are about
        # the clock, not about credentials — test_api_source_without_its_
        # credential_is_never_queued below is what covers the gate.
        for spec in app.API_SOURCES.values():
            if spec.get("needs"):
                os.environ[spec["needs"]] = "set-for-test"
        self._detect = importers.detect_sources
        importers.detect_sources = lambda: []   # isolate from the file half

    def tearDown(self):
        importers.detect_sources = self._detect
        for k in ("NC_GMAIL_EVERY_HOURS", "NC_LINKEDIN_EVERY_HOURS"):
            os.environ.pop(k, None)
        for spec in app.API_SOURCES.values():
            if spec.get("needs"):
                os.environ.pop(spec["needs"], None)
        super().tearDown()

    def test_api_source_without_its_credential_is_never_queued(self):
        """A brand-new install has configured nothing, but both switches default
        to on. Without this gate it would queue Gmail every 12 hours and LinkedIn
        every 24, each failing on a missing credential, for as long as the app is
        installed. Stamping the attempt stops it spinning; it does not make it
        any less pointless."""
        for spec in app.API_SOURCES.values():
            if spec.get("needs"):
                os.environ.pop(spec["needs"], None)
        self.assertEqual(app._autosync_pending(self.conn), [])

        # Whitespace is not configuration — a key holding only spaces still
        # cannot authenticate anything.
        os.environ["GMAIL_ACCOUNTS"] = "   "
        self.assertEqual(app._autosync_pending(self.conn), [])

        # And one configured source does not drag the other in with it.
        # NC-180: fully configured, which for Gmail is two things — the address
        # AND a client file from Google. The address alone is now its own
        # not-ready state; test_a_half_finished_gmail_is_not_attempted below is
        # what covers that, and this line has to mean "set up" to still be
        # testing what it says it tests.
        _sec = tempfile.mkdtemp(prefix="gmailcreds_")
        creds = os.path.join(_sec, "client.json")
        with open(creds, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.environ["GMAIL_ACCOUNTS"] = f"you@example.com:{creds}"
        # NC-117: and a saved sign-in. A configured account with a file and no
        # token is its own not-ready state now — the one every install passes
        # through on the way in — so a test that means "attempted" has to say
        # both halves. SECRETS_DIR is redirected because that is where the token
        # file lives, and reading the developer's own would make this pass or
        # fail for a reason unrelated to the code.
        _p = mock.patch.object(config, "SECRETS_DIR", _sec)
        _p.start()
        self.addCleanup(_p.stop)
        with open(gmail_accounts.token_path("you@example.com"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        self.assertEqual(
            [p["source"] for p in app._autosync_pending(self.conn)], ["gmail"])

    def test_a_half_finished_gmail_is_not_attempted(self):
        """NC-180. The address is a Settings field; the client file from Google
        is not on any screen. Half set up used to spawn a subprocess every 12
        hours that could only exit 1 on the missing file, forever, silently."""
        os.environ["GMAIL_ACCOUNTS"] = "you@example.com:/nonexistent/client.json"
        self.assertEqual(
            [p["source"] for p in app._autosync_pending(self.conn)], ["linkedin"],
            "Gmail was queued with nothing to sign in with")

    def _stamp(self, key, hours_ago):
        when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
        db.state_set(self.conn, app.API_ATTEMPT_KEY + key,
                     when.isoformat(timespec="seconds"))

    def test_parse_stamp_reads_both_writers_formats(self):
        """Regression: the attempt stamp is ISO with an offset, archive.db's ts is
        space-separated with none. A parser that only took the latter returned
        None for our own stamp, which reads as "never ran" — so every source was
        due on every pass of the loop, forever."""
        iso = app._parse_stamp("2030-01-02T03:04:05+00:00")
        spaced = app._parse_stamp("2030-01-02 03:04:05")
        self.assertEqual(iso, spaced)
        self.assertIsNotNone(iso.tzinfo, "must be aware, or comparing to now() raises")
        self.assertEqual(app._parse_stamp("2030-01-02T03:04:05"), iso)  # naive -> UTC
        for junk in ("", "   ", "not a date", None):
            self.assertIsNone(app._parse_stamp(junk))

    def test_due_when_never_run_then_not_until_the_period_elapses(self):
        self.assertEqual(sorted(f["source"] for f in app._autosync_pending(self.conn)),
                         ["gmail", "linkedin"])
        for key in app.API_SOURCES:
            self._stamp(key, 0)
        self.conn.commit()
        self.assertEqual(app._autosync_pending(self.conn), [])
        for key in app.API_SOURCES:
            self._stamp(key, 48)
        self.conn.commit()
        self.assertEqual(sorted(f["source"] for f in app._autosync_pending(self.conn)),
                         ["gmail", "linkedin"])

    def test_each_source_keeps_its_own_period(self):
        """13h in: Gmail's 12h has elapsed, LinkedIn's 24h has not."""
        for key in app.API_SOURCES:
            self._stamp(key, 13)
        self.conn.commit()
        self.assertEqual([f["source"] for f in app._autosync_pending(self.conn)], ["gmail"])

    def test_a_failing_source_waits_out_its_period_instead_of_spinning(self):
        """run_autosync stamps an API attempt *before* running it, so a dead
        token is retried once a period rather than on every loop pass."""
        calls = []

        class Fail:
            returncode = 1
            stdout = stderr = ""

        with mock.patch.object(app.subprocess, "run",
                               side_effect=lambda cmd, **kw: (calls.append(cmd), Fail())[1]), \
             mock.patch.object(app.db, "DB_PATH", self.path), \
             mock.patch.object(app.engagement, "refresh", lambda c: None):
            self.assertEqual(app.run_autosync(), 0)      # nothing succeeded
            self.assertEqual(len(calls), 2)              # both were attempted once
            self.assertEqual(app._autosync_pending(db.get_db(self.path)), [],
                             "a failed attempt must still consume the period")

    def test_switches_are_honoured(self):
        os.environ["NC_GMAIL_AUTO"] = "0"
        self.assertEqual([f["source"] for f in app._autosync_pending(self.conn)], ["linkedin"])
        os.environ["NC_LINKEDIN_AUTO"] = "0"
        self.assertEqual(app._autosync_pending(self.conn), [])
        os.environ["NC_AUTOSYNC"] = "1"   # the master switch is separate
        try:
            os.environ["NC_AUTOSYNC"] = "0"
            self.assertEqual(app._autosync_pending(self.conn), [])
        finally:
            os.environ.pop("NC_AUTOSYNC", None)

    def test_period_is_a_setting_and_is_clamped(self):
        for value, want in (("0", 1), ("999999", 720), ("bogus", 12), ("6", 6)):
            os.environ["NC_GMAIL_EVERY_HOURS"] = value
            self.assertEqual(app._api_hours("gmail"), want)
        for key in ("NC_GMAIL_AUTO", "NC_GMAIL_EVERY_HOURS",
                    "NC_LINKEDIN_AUTO", "NC_LINKEDIN_EVERY_HOURS"):
            self.assertIn(key, settings_mod.MANAGED_KEYS, f"{key} must be tunable in Settings")

    def test_gmail_gets_an_incremental_since_and_linkedin_needs_none(self):
        forms = {f["source"]: f["form"] for f in app._autosync_pending(self.conn)}
        self.assertIn("since", forms["gmail"])
        self.assertNotIn("since", forms["linkedin"])
        # the argv the loop will actually run
        gmail_cmd = importers.REGISTRY["gmail"].build_cmd(forms["gmail"])
        self.assertIn("--since", gmail_cmd)
        self.assertIn("--scan", gmail_cmd)
        self.assertEqual(gmail_cmd[gmail_cmd.index("--since") + 1], forms["gmail"]["since"])
        # empty path is what makes linkedin take its API route, not the CSV one
        self.assertEqual(forms["linkedin"]["path"], "")
        self.assertNotIn("--csv", importers.REGISTRY["linkedin"].build_cmd(forms["linkedin"]))

    def test_since_comes_from_the_archive_watermark_minus_an_overlap(self):
        """Scheduled runs must not re-sweep the importer's 3-year default, and must
        overlap slightly so a message landing mid-run isn't skipped forever."""
        with mock.patch.object(app.arc, "channel_watermark", lambda c, ch: "2026-05-10 08:00:00"), \
             mock.patch.object(app.arc, "get_archive_db", lambda p=None: self.conn):
            self.assertEqual(app._api_since("gmail"), "2026-05-08")   # 2-day overlap
        # no archive yet: fall back to a recent window, never years
        with mock.patch.object(app.arc, "channel_watermark", lambda c, ch: ""), \
             mock.patch.object(app.arc, "get_archive_db", lambda p=None: self.conn):
            since = datetime.datetime.strptime(app._api_since("gmail"), "%Y-%m-%d")
            days = (datetime.datetime.now() - since).days
            self.assertLessEqual(days, app.API_FIRST_RUN_DAYS + 1)
            self.assertGreaterEqual(days, app.API_FIRST_RUN_DAYS - 1)


class TestApiImportModuleContract(unittest.TestCase):
    """Guards on the two things the api_import extraction depends on.

    Both are invisible to every other test: they fail only in production, and
    only in ways that look like something else.
    """

    def test_app_dir_holds_the_importer_scripts(self):
        """importers.REGISTRY builds argv from BARE filenames and the subprocess
        runs with cwd=APP_DIR. If APP_DIR stops pointing at crm/, every import
        dies with an interpreter-level 'No such file' that reads like a broken
        importer rather than a broken path."""
        import api_import
        self.assertTrue(
            os.path.isfile(os.path.join(api_import.APP_DIR, "contacts_import.py")),
            f"APP_DIR={api_import.APP_DIR!r} does not contain the importer scripts",
        )

    def test_app_still_exposes_subprocess(self):
        """app.py has no subprocess call of its own left, but the autosync tests
        patch app.subprocess.run — which only works while app and api_import
        share one module object. A tidy-up that drops the import would make
        those patches silently no-op and let tests shell out for real."""
        import api_import
        self.assertTrue(hasattr(app, "subprocess"))
        self.assertIs(app.subprocess, api_import.subprocess)


class TestAutosyncConsentDialogs(Base):
    """The background loop must not touch a TCC-protected folder on a timer.

    The failure: a "python3 would like to access…" dialog every few minutes.
    detect_sources() stats WhatsApp's group container and globs Desktop/ and
    Downloads/ — all behind the app-data category of macOS's privacy permissions —
    and _autosync_loop called it on every pass, forever. It says "python3" rather
    than "Network CRM" because the app runs as a plain interpreter, so a routine
    prompt reads as something foreign asking for the user's Messages. Two
    instances at once would halve the interval again.

    Boot is the one honest moment to ask, so the full scan runs once and every
    later pass looks only where macOS does not guard.
    """

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()

    @contextlib.contextmanager
    def _counting_probes(self):
        orig_full = importers.detect_sources
        orig_unprot = importers.detect_unprotected_sources
        seen = {"protected": 0, "unprotected": 0}

        def full():
            seen["protected"] += 1
            return []

        def unprot(**_):
            seen["unprotected"] += 1
            return []

        importers.detect_sources = full
        importers.detect_unprotected_sources = unprot
        try:
            yield seen
        finally:
            importers.detect_sources = orig_full
            importers.detect_unprotected_sources = orig_unprot

    def test_protected_scan_runs_once_at_boot_then_never(self):
        with self._counting_probes() as seen:
            first = True
            for _ in range(8):          # 8 passes = ~2 hours at the default cadence
                app._autosync_pending(self.conn, probe_protected=first)
                first = False
        self.assertEqual(seen["protected"], 1,
                         "the protected scan must run only on the first pass")
        self.assertEqual(seen["unprotected"], 7,
                         "every later pass still has to look, just not where macOS guards")

    def test_unprotected_probe_avoids_the_guarded_paths(self):
        """The cheap probe must not reach for anything that can raise a dialog."""
        src = inspect.getsource(importers.detect_unprotected_sources)
        # Strip the docstring first — it *names* the guarded paths to explain why
        # they are absent, and matching prose instead of code would make this
        # test pass or fail on comment wording.
        body = src.split('"""')[-1]
        for guarded in ("WA_CONTAINER", "IMSG_SOURCE_DIR", "Desktop", "Downloads", "glob"):
            self.assertNotIn(guarded, body,
                             f"detect_unprotected_sources must not touch {guarded}")

    def test_default_still_probes_so_callers_are_unchanged(self):
        """Only the loop opts out. A bare call keeps the old behaviour."""
        with self._counting_probes() as seen:
            app._autosync_pending(self.conn)
            app._autosync_pending(self.conn)
        self.assertEqual(seen["protected"], 2)
        self.assertEqual(seen["unprotected"], 0)


class TestWhatsAppSyncsWithoutAConsentDialog(Base):
    """WhatsApp must keep syncing on a timer, from a copy nothing prompts for.

    THE REGRESSION THIS ENCODES. Stopping the repeating consent dialog
    narrows the background loop to detect_unprotected_sources(). If that returned
    only the iMessage copy while _autosync_pending's WhatsApp branch still
    filtered findings for paths under WA_CONTAINER — paths that function can
    never return — the branch would match nothing on every pass but the boot one,
    and WhatsApp would stop updating until the app was restarted. Both halves are
    individually reasonable, so only a test that joins them can catch it.

    The fix is a WhatsApp copy in our own container, written by the FDA helper, so
    the timer has something unprotected to look at.
    """

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()

    @contextlib.contextmanager
    def _copies(self, imsg_mtime=None, wa_mtime=None):
        """Both import-folder copies present, as the helper would leave them."""
        orig = (importers.detect_sources, importers.detect_unprotected_sources,
                importers.IMSG_AUTO_DB, importers.WA_AUTO_DB)
        chatdb = os.path.join(self.dir, "chat.db")
        wadb = os.path.join(self.dir, "ChatStorage.sqlite")
        for path, table, mtime in ((chatdb, "handle", imsg_mtime),
                                   (wadb, "ZWACHATSESSION", wa_mtime)):
            c = sqlite3.connect(path)
            c.execute(f"CREATE TABLE {table}(id TEXT)")
            c.commit()
            c.close()
            if mtime:
                os.utime(path, (mtime, mtime))
        importers.IMSG_AUTO_DB, importers.WA_AUTO_DB = chatdb, wadb
        findings = [
            {"source": "imessage", "path": chatdb, "label": "helper", "mtime": ""},
            {"source": "whatsapp", "path": wadb, "label": "helper", "mtime": ""},
        ]
        # The loop's own probe. Returning the same list from both is faithful:
        # detect_sources() is a superset, and neither extra entry matters here.
        importers.detect_sources = lambda: list(findings)
        importers.detect_unprotected_sources = lambda **_: list(findings)
        try:
            yield chatdb, wadb
        finally:
            (importers.detect_sources, importers.detect_unprotected_sources,
             importers.IMSG_AUTO_DB, importers.WA_AUTO_DB) = orig

    def test_whatsapp_is_pending_on_a_timer_pass(self):
        """The regression, directly: probe_protected=False is the timer, and
        WhatsApp has to be in its result."""
        with self._copies():
            sources = [f["source"] for f
                       in app._autosync_pending(self.conn, probe_protected=False)]
        self.assertIn("whatsapp", sources,
                      "WhatsApp must sync on ordinary passes, not only at boot")

    def test_whatsapp_does_not_reimport_an_unchanged_snapshot(self):
        """Idempotence: the helper rewrites the file only when it changes, but the
        loop must not re-import the same bytes even so."""
        with self._copies() as (_, wadb):
            first = [f["source"] for f
                     in app._autosync_pending(self.conn, probe_protected=False)]
            self.assertIn("whatsapp", first)
            app._stamp_pasted_copy(self.conn, wadb)
            again = [f["source"] for f
                     in app._autosync_pending(self.conn, probe_protected=False)]
        self.assertNotIn("whatsapp", again)

    def test_a_fresher_whatsapp_snapshot_imports_again(self):
        with self._copies(wa_mtime=1_700_000_000) as (_, wadb):
            app._stamp_pasted_copy(self.conn, wadb)
            self.assertNotIn("whatsapp", [f["source"] for f in
                                          app._autosync_pending(self.conn, probe_protected=False)])
            os.utime(wadb, (1_700_000_900, 1_700_000_900))   # helper wrote a newer copy
            self.assertIn("whatsapp", [f["source"] for f in
                                       app._autosync_pending(self.conn, probe_protected=False)])

    def test_the_two_copies_have_separate_fingerprints(self):
        """Sharing one state key would make a fresh WhatsApp snapshot mark
        iMessage as already done, so whichever the helper wrote second would never
        import. Both are in the same folder and change on the same schedule, which
        is exactly when a shared key looks like it works."""
        self.assertNotEqual(app.IMSG_PASTE_FP_KEY, app.WA_PASTE_FP_KEY)
        with self._copies() as (chatdb, wadb):
            app._stamp_pasted_copy(self.conn, wadb)          # stamp WhatsApp only
            sources = [f["source"] for f
                       in app._autosync_pending(self.conn, probe_protected=False)]
            self.assertIn("imessage", sources,
                          "stamping WhatsApp must not mark iMessage done")
            self.assertNotIn("whatsapp", sources)
            app._stamp_pasted_copy(self.conn, chatdb)
            self.assertEqual(
                [f["source"] for f
                 in app._autosync_pending(self.conn, probe_protected=False)], [])

    def test_the_loop_stamps_every_automatic_copy_not_only_imessage(self):
        """NC-103. The bug this file could not see, because every other test in
        this class calls `_stamp_pasted_copy` ITSELF.

        The helper always worked. The autosync loop's call to it was guarded by
        `if f["path"] == importers.IMSG_AUTO_DB`, so four of the five automatic
        sources were never stamped — and with no stamp, `_autosync_pending`
        compares a live fingerprint against "" and finds them due on every
        two-minute pass, forever. That is NC-043's hot loop again, and NC-043's
        own fix (a fingerprint key per copy) did not close it, because the key
        existing changes nothing if nobody writes it.

        The import itself is made to fail, because the stamp is deliberately
        written BEFORE the attempt ("never loop on the same bytes"), so a failing
        run is the sharpest version of this: nothing else writes, and the
        fingerprints must still be there.
        """
        # run_autosync opens its own connection from db.DB_PATH, so that has to
        # point at this fixture — patched on the owner, never rebound on app.
        with self._copies() as (chatdb, wadb), \
             mock.patch.object(app.db, "DB_PATH", self.path), \
             mock.patch.object(app.subprocess, "run",
                               return_value=SimpleNamespace(
                                   returncode=1, stdout="", stderr="no")):
            app.run_autosync(probe_protected=False)
        got = {k: db.state_get(self.conn, k, "") for k in
               (app.IMSG_PASTE_FP_KEY, app.WA_PASTE_FP_KEY)}
        self.assertTrue(got[app.IMSG_PASTE_FP_KEY], "iMessage was not stamped")
        self.assertTrue(
            got[app.WA_PASTE_FP_KEY],
            "WhatsApp ran and was not stamped, so the next pass in two minutes "
            "will import the same bytes again — and the pass after that, forever")

    def test_the_stamp_is_not_gated_on_one_source(self):
        """The source-level half, because the behavioural test above can only
        cover the two copies its fixture builds. Five paths are automatic, and a
        guard naming any one of them is the shape of the defect.

        `_stamp_pasted_copy` returns early on a path with no fingerprint key, so
        it needs no guard at the call site at all — passing it every path is the
        correct use and always was."""
        import ast
        src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "crm", "api_import.py")
        with open(src_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "run_autosync")
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            calls = {c.func.id for c in ast.walk(node)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            if "_stamp_pasted_copy" not in calls:
                continue
            guard = ast.dump(node.test)
            self.assertNotIn(
                "AUTO_DB", guard,
                "the stamp is guarded by one source's path again. Every "
                "automatic copy needs its fingerprint written or it re-imports "
                f"on every tick: {guard}")
            self.assertNotIn("AUTO_DIR", guard, "same, for the photo directory")

    def test_whatsapp_autoimport_has_an_off_switch(self):
        """Same shape as NC_IMSG_AUTOIMPORT — every background import the user
        did not ask for individually needs a way to say no."""
        with self._copies():
            os.environ["NC_WA_AUTOIMPORT"] = "0"
            try:
                sources = [f["source"] for f
                           in app._autosync_pending(self.conn, probe_protected=False)]
            finally:
                os.environ.pop("NC_WA_AUTOIMPORT", None)
        self.assertNotIn("whatsapp", sources)
        self.assertIn("imessage", sources, "the switch must be WhatsApp-only")

    def test_the_unprotected_probe_offers_whatsapp(self):
        """detect_unprotected_sources is the timer's only window on the world; if
        WhatsApp is absent from it no filter downstream can help."""
        orig = importers.WA_AUTO_DB
        wadb = os.path.join(self.dir, "wa-probe.sqlite")
        c = sqlite3.connect(wadb)
        c.execute("CREATE TABLE ZWACHATSESSION(id TEXT)")
        c.commit()
        c.close()
        importers.WA_AUTO_DB = wadb
        try:
            found = importers.detect_unprotected_sources()
        finally:
            importers.WA_AUTO_DB = orig
        self.assertIn("whatsapp", [f["source"] for f in found])

    def test_the_importer_prefers_the_unprotected_copy(self):
        """Our own folder must be searched BEFORE the protected container, or the
        glob that finds the original raises the consent dialog the app must not
        show."""
        globs = wa.WA_GLOBS
        self.assertIn("NetworkCRM", globs[0],
                      "the import folder has to come first in WA_GLOBS")
        for later in globs[1:]:
            self.assertNotIn("NetworkCRM", later)


class TestTheFilePickerTellsYouWhenItCannotOpen(unittest.TestCase):
    """A picker that fails must not look identical to one you dismissed.

    handle_import_pick_file used to return a bare {"path": ""} for every
    non-success, and all three frontend callers did `if (d.path)` and nothing
    else — so a picker that could not open produced no path, no error and no
    message. You click "Choose file", nothing happens, and there is nothing to
    read. With no message, a failed picker looks like a broken import from the
    outside, while contacts_import.py itself works end to end on an Apple export
    — folded base64 photos, item-grouped TEL/EMAIL and all.

    Cancelling stays silent on purpose: a toast that says "you cancelled" after
    you cancelled is noise.
    """

    def _pick(self, returncode, stdout="", stderr=""):
        class R:
            pass
        r = R()
        r.returncode, r.stdout, r.stderr = returncode, stdout, stderr
        orig = app.api_import.subprocess.run
        app.api_import.subprocess.run = lambda *a, **k: r
        try:
            return app.handle_import_pick_file({"kind": "vcf"})
        finally:
            app.api_import.subprocess.run = orig

    def test_a_chosen_path_comes_back_clean(self):
        out = self._pick(0, stdout="/Users/x/Contacts.vcf\n")
        self.assertEqual(out["path"], "/Users/x/Contacts.vcf")
        self.assertFalse(out["cancelled"])
        self.assertFalse(out["error"])

    def test_cancelling_is_silent(self):
        for stderr in ("execution error: User canceled. (-128)",
                       "execution error: User cancelled. (-128)"):
            out = self._pick(1, stderr=stderr)
            self.assertEqual(out["path"], "")
            self.assertTrue(out["cancelled"], stderr)
            self.assertFalse(out["error"], "a cancel must not produce a message")

    def test_a_real_failure_produces_a_message(self):
        out = self._pick(1, stderr="execution error: Can't get application. (-1728)")
        self.assertEqual(out["path"], "")
        self.assertFalse(out["cancelled"])
        self.assertIn("could not open", out["error"])
        self.assertIn("paste the path", out["error"],
                      "the message must name the way out, not just the failure")

    def test_a_missing_osascript_produces_a_message(self):
        orig = app.api_import.subprocess.run
        def boom(*a, **k):
            raise FileNotFoundError("osascript")
        app.api_import.subprocess.run = boom
        try:
            out = app.handle_import_pick_file({"kind": "vcf"})
        finally:
            app.api_import.subprocess.run = orig
        self.assertFalse(out["cancelled"])
        self.assertIn("picker cannot open", out["error"])

    def test_every_frontend_caller_surfaces_the_error(self):
        """Four call sites: the Data page, the Gmail set-up box and two in
        onboarding. A caller that reads only d.path reintroduces the dead-button
        bug for its own view."""
        import re as _re
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name, expected in (("data.js", 1), ("gmail.js", 1), ("onboarding.js", 2)):
            with open(os.path.join(here, "static", "app", name), encoding="utf-8") as fh:
                src = fh.read()
            calls = src.count('"/api/import/pick-file"')
            self.assertEqual(calls, expected, f"{name}: call count changed")
            handled = len(_re.findall(r"d\.error", src))
            self.assertGreaterEqual(
                handled, expected,
                f"{name} calls the picker {calls}x but handles d.error {handled}x — "
                "a caller that ignores it shows nothing when the picker fails")


class TestTheCalendarLinksFetchOnAClock(Base):
    """NC-031. The link-fed calendars now refresh in the background, and this is
    the one change in this codebase that REVERSES a documented privacy decision:
    the block comment above fetch_calendar_ics used to say a background thread
    could never phone out on a timer, and it said so on purpose.

    The fetch runs once a day. So what these tests defend is not "does it run" —
    that is one dict entry — but that the two gates that actually protect the
    user did NOT move with it. NC_ICS_FETCH defaults to "0" and is the master
    switch; there is no URL until one is pasted.
    A regression that quietly queues a calendar on a fresh install, or on an
    install that turned downloading off, is the thing to catch.
    """

    CAL = ("calendar_google", "calendar_icloud")

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()
        self._detect = importers.detect_sources
        importers.detect_sources = lambda: []      # isolate from the file half
        os.environ["NC_CALENDAR_AUTO"] = "1"
        os.environ["NC_ICS_GOOGLE_URL"] = "https://calendar.google.com/x/basic.ics"
        os.environ["NC_ICS_ICLOUD_URL"] = "webcal://p01.icloud.com/x.ics"

    def tearDown(self):
        importers.detect_sources = self._detect
        for k in ("NC_CALENDAR_AUTO", "NC_CALENDAR_EVERY_HOURS", "NC_ICS_FETCH",
                  "NC_ICS_GOOGLE_URL", "NC_ICS_ICLOUD_URL"):
            os.environ.pop(k, None)
        super().tearDown()

    def _pending(self):
        return sorted(f["source"] for f in app._autosync_pending(self.conn)
                      if f["source"] in self.CAL)

    def test_downloading_off_means_no_calendar_on_any_schedule(self):
        """The gate that matters most. NC_ICS_FETCH defaults to "0", so an
        install that has never opted into calendar downloading must make no
        request however the schedule is configured — and turning it back off
        must stop the traffic, not just stop new links being added."""
        os.environ.pop("NC_ICS_FETCH", None)
        self.assertEqual(self._pending(), [], "a calendar was queued with "
                                              "downloading never turned on")
        os.environ["NC_ICS_FETCH"] = "0"
        self.assertEqual(self._pending(), [], "a calendar was queued with "
                                              "downloading explicitly off")

    def test_no_link_means_no_request_even_with_downloading_on(self):
        os.environ["NC_ICS_FETCH"] = "1"
        os.environ.pop("NC_ICS_GOOGLE_URL", None)
        os.environ.pop("NC_ICS_ICLOUD_URL", None)
        self.assertEqual(self._pending(), [])
        # One link configured must not drag the other calendar in with it.
        os.environ["NC_ICS_GOOGLE_URL"] = "https://calendar.google.com/x/basic.ics"
        self.assertEqual(self._pending(), ["calendar_google"])

    def test_both_gates_open_means_it_is_due(self):
        os.environ["NC_ICS_FETCH"] = "1"
        self.assertEqual(self._pending(), ["calendar_google", "calendar_icloud"])

    def test_its_own_switch_turns_the_schedule_off_without_touching_the_rest(self):
        """Two independent things: may this app download calendars at all, and do
        it on a clock. Turning the clock off must leave the button working."""
        os.environ["NC_ICS_FETCH"] = "1"
        os.environ["NC_CALENDAR_AUTO"] = "0"
        self.assertEqual(self._pending(), [])
        self.assertTrue(api_import.calendar_fetch_enabled(),
                        "turning the schedule off also disabled the manual fetch")

    def test_the_period_is_a_setting_and_is_respected(self):
        os.environ["NC_ICS_FETCH"] = "1"
        for key in self.CAL:
            when = (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(hours=13))
            db.state_set(self.conn, app.API_ATTEMPT_KEY + key,
                         when.isoformat(timespec="seconds"))
        self.conn.commit()
        self.assertEqual(self._pending(), [], "13h in, a 24h period is not due")
        os.environ["NC_CALENDAR_EVERY_HOURS"] = "12"
        self.assertEqual(self._pending(), ["calendar_google", "calendar_icloud"])

    def test_a_refused_link_does_not_take_the_rest_of_the_pass_down(self):
        """build_cmd refuses a calendar with no local file rather than importing a
        stale snapshot, so the fetch has to happen inside the loop — which puts a
        network failure on the loop's own error path. A revoked link must cost
        that one source, not every source behind it in the list."""
        os.environ["NC_ICS_FETCH"] = "1"
        ran = []

        def boom(source):
            raise api_import.ApiError(403, "revoked")

        def fake_run(cmd, **kw):
            ran.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(api_import, "fetch_calendar_ics", boom), \
             mock.patch.object(app.subprocess, "run", fake_run), \
             mock.patch.object(app.engagement, "refresh", lambda conn: None), \
             mock.patch.object(db, "DB_PATH", self.path):
            count = app.run_autosync(probe_protected=False)
        self.assertEqual(ran, [], "the importer ran without a fetched file")
        self.assertEqual(count, 0)
        # Stamped anyway, so the next pass two minutes later does not retry it.
        for key in self.CAL:
            self.assertTrue(db.state_get(self.conn, app.API_ATTEMPT_KEY + key, ""),
                            f"{key} was not stamped, so it will spin every pass")

    def test_a_fetched_calendar_reaches_the_importer_with_a_real_path(self):
        os.environ["NC_ICS_FETCH"] = "1"
        cmds = []

        def fake_fetch(source):
            return {"source": source, "path": f"/tmp/{source}.ics", "bytes": 10,
                    "host": "example.com", "note": "fetched"}

        def fake_run(cmd, **kw):
            cmds.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(api_import, "fetch_calendar_ics", fake_fetch), \
             mock.patch.object(app.subprocess, "run", fake_run), \
             mock.patch.object(app.engagement, "refresh", lambda conn: None), \
             mock.patch.object(db, "DB_PATH", self.path):
            app.run_autosync(probe_protected=False)
        ics = [c for c in cmds if any("ics_import.py" in str(a) for a in c)]
        self.assertEqual(len(ics), 2, f"both calendars should have run: {cmds}")
        paths = sorted(a for c in ics for a in c if str(a).endswith(".ics"))
        self.assertEqual(paths, ["/tmp/calendar_google.ics", "/tmp/calendar_icloud.ics"])
        # --apply, not a preview: the background run is a real import, and a loop
        # that only ever previewed would report success and change nothing.
        for c in ics:
            self.assertIn("--apply", [str(a) for a in c])


class TestTheDocsNoLongerPromiseWhatTheCodeStoppedDoing(unittest.TestCase):
    """The "only this button fetches" sentence appeared in three places and was
    load-bearing in all three: a block comment explaining the design, and the two
    help texts the user actually reads on the row. Leaving any of them behind
    would be the NC-005 defect again — a card that says nothing is written, and
    then writes."""

    def setUp(self):
        for name in ("api_import.py", "importers.py"):
            with io.open(os.path.join(REPO, "crm", name), encoding="utf-8") as fh:
                setattr(self, name.split(".")[0], fh.read())

    def test_no_file_still_says_the_button_is_the_only_fetch(self):
        for src, name in ((self.api_import, "api_import.py"),
                          (self.importers, "importers.py")):
            with self.subTest(file=name):
                self.assertNotIn("only this button fetches", src)
                self.assertNotIn("never run in the background", src)

    def test_both_calendar_rows_tell_the_user_it_repeats(self):
        """NC-074 changed HOW the row names the off switch, not whether it does.

        This used to require the literal string "NC_CALENDAR_AUTO". That is a
        variable name, and after NC-074 took the per-source automation switches
        off the Settings screen it was a variable name the reader could not find
        anywhere — the row said "NC_CALENDAR_AUTO turns that off" about a control
        that did not exist on any page. So the requirement is now the on-screen
        LABEL of the switch that actually stops it, read out of settings.js
        rather than hardcoded here, so renaming that switch forces this copy to
        follow instead of quietly going stale.

        Strictly stronger than before: the old form would pass while pointing at
        something unreachable, which is the failure it was written to prevent.
        """
        # NC-236: the switch is now drawn from the server's Settings rows, so the
        # label is read from there.
        js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "crm", "api_abilities.py")
        with io.open(js, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r'_switch\("NC_AUTOSYNC",\s*"([^"]+)"', src)
        self.assertIsNotNone(
            m, "NC_AUTOSYNC no longer renders with a label, so no calendar row "
               "can point the user at the switch that stops the daily fetch")
        label = m.group(1)

        for key in ("calendar_google", "calendar_icloud"):
            with self.subTest(key=key):
                text = importers.REGISTRY[key].export_help
                self.assertIn("once a day on its own", text,
                              "the row does not say the fetch repeats")
                self.assertIn(
                    label, text,
                    f"the row does not name the switch that stops it. The switch "
                    f"is called {label!r} on the Settings screen; naming the "
                    f"environment variable instead points at something the reader "
                    f"cannot find.")


class TestOneListDecidesWhatIsAutomatic(Base):
    """NC-036. A button is a defect unless macOS genuinely forbids the
    alternative, and the way this app grew one anyway was by keeping the answer
    to "is this source automatic" in two places.

    handle_import_detect had its own tuple of auto paths, and it had fallen two
    entries behind the loop that actually does the importing: NC-047 added
    WA_AUTO_DB and WA_PHOTOS_AUTO_DIR to the loop and not to the tuple. So the
    helper-written WhatsApp copy was imported every pass AND shown under "Needs
    you" with a "Sync now" next to text saying "kept current by the sync helper".
    Before the fix the answer was auto=false on a file the helper had rewritten
    that same day.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="auto_paths_")
        self._env = {k: os.environ.get(k) for k in
                     ("NC_AUTOSYNC", "NC_IMSG_AUTOIMPORT", "NC_WA_AUTOIMPORT",
                      "NC_CONTACTS_AUTOIMPORT")}
        for k in self._env:
            os.environ.pop(k, None)
        db.state_set(self.conn, "onboarded", "1")
        self.conn.commit()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _detect(self, findings):
        with mock.patch.object(importers, "detect_sources",
                               lambda: list(findings)):
            return api_import.handle_import_detect(self.conn)["found"]

    # ── the join that was broken ────────────────────────────────────────────
    def test_every_path_the_loop_imports_is_reported_as_automatic(self):
        """The whole point. Not "the four we remembered" — all of them, derived."""
        paths = [p for p, _s, _k in api_import.auto_import_paths()]
        self.assertGreaterEqual(len(paths), 4)
        found = self._detect([{"source": "x", "path": p, "label": "l",
                               "mtime": "2025-01-10", "count": 1}
                              for p in paths])
        for row in found:
            self.assertTrue(row["auto"],
                            f"{row['path']} is imported automatically but the "
                            f"Data page would show a button on it")

    def test_the_whatsapp_copy_and_its_photo_cache_are_both_in_the_list(self):
        # Named explicitly because these are the two NC-047 added to one list and
        # not the other, and a derived list that happened to omit both again
        # would still satisfy the test above.
        paths = [p for p, _s, _k in api_import.auto_import_paths()]
        for p in (importers.WA_AUTO_DB, importers.WA_PHOTOS_AUTO_DIR,
                  importers.IMSG_AUTO_DB, importers.AB_AUTO_DB):
            self.assertIn(p, paths)

    def test_a_path_nothing_imports_still_needs_a_click(self):
        stray = os.path.join(self.tmp, "Downloads", "export.vcf")
        found = self._detect([{"source": "contacts", "path": stray,
                               "label": "an export", "mtime": "2025-01-02",
                               "count": 3}])
        self.assertFalse(found[0]["auto"])

    # ── the switches, because `auto` is a promise about behaviour ───────────
    def test_switching_a_source_off_makes_its_row_offer_the_click_back(self):
        os.environ["NC_WA_AUTOIMPORT"] = "0"
        found = self._detect([{"source": "whatsapp", "path": importers.WA_AUTO_DB,
                               "label": "kept current", "mtime": "2025-01-10",
                               "count": 9}])
        self.assertFalse(found[0]["auto"],
                         "a row cannot say it syncs itself while its switch is off")
        os.environ["NC_WA_AUTOIMPORT"] = "1"
        found = self._detect([{"source": "whatsapp", "path": importers.WA_AUTO_DB,
                               "label": "kept current", "mtime": "2025-01-10",
                               "count": 9}])
        self.assertTrue(found[0]["auto"])

    def test_the_global_off_switch_makes_nothing_automatic(self):
        os.environ["NC_AUTOSYNC"] = "0"
        found = self._detect([{"source": "whatsapp", "path": importers.WA_AUTO_DB,
                               "label": "kept current", "mtime": "2025-01-10",
                               "count": 9}])
        self.assertFalse(found[0]["auto"])

    # ── the NC-044 guard ───────────────────────────────────────────────────
    def test_the_list_is_read_at_call_time_not_frozen_at_import(self):
        """A module-level tuple here would read the machine's real databases.

        tests/helpers.py redirects importers.WA_AUTO_DB and friends into a temp
        dir AFTER app.py — and therefore api_import — has been imported. A list
        built at import time would hold the pre-redirect paths, the suite would
        read the developer's actual message archive, and every test would still
        pass while it happened. That is NC-044 exactly.
        """
        moved = os.path.join(self.tmp, "elsewhere", "ChatStorage.sqlite")
        with mock.patch.object(importers, "WA_AUTO_DB", moved):
            self.assertIn(moved, [p for p, _s, _k in api_import.auto_import_paths()])
        self.assertNotIn(moved, [p for p, _s, _k in api_import.auto_import_paths()])

    def test_no_second_hardcoded_list_of_auto_paths_survives(self):
        """The defect was a duplicated list, so pin that there is only one.

        Counts occurrences of the path constants in the source rather than
        grepping for a phrase: a second list would have to name them again.
        """
        src = code_of(os.path.join(REPO, "crm", "api_import.py"))
        body = src[src.index("def auto_import_paths"):]
        body = body[:body.index("\ndef ", 1)]
        # WA/AB/photos must appear ONLY inside auto_import_paths(). IMSG_AUTO_DB
        # legitimately appears ONCE more, counting how many iMessage copies a
        # pass imported — one path getting special treatment is not a second
        # list. It was 2 until NC-103 took the stamping guard off it: the stamp
        # belongs to every automatic copy, not to iMessage.
        for const, allowed_outside in (("WA_AUTO_DB", 0), ("AB_AUTO_DB", 0),
                                       ("WA_PHOTOS_AUTO_DIR", 0),
                                       ("IMSG_AUTO_DB", 1)):
            outside = src.count(f"importers.{const}") - body.count(f"importers.{const}")
            self.assertEqual(
                outside, allowed_outside,
                f"importers.{const} is named {outside} time(s) outside "
                f"auto_import_paths(), expected {allowed_outside} — a second "
                f"per-path list is exactly the defect this gate exists for")

    def test_every_automatic_copy_carries_its_own_fingerprint_key(self):
        """NC-043's bug, made structurally impossible.

        A copy with no fingerprint key re-imports on every two-minute tick
        forever and reports nothing — the address book did exactly that, re-importing
        every contact, because the autosync loop gained the path and
        _paste_fp_key was never taught about it. Now they are one row.
        """
        rows = api_import.auto_import_paths()
        keys = [k for _p, _s, k in rows]
        self.assertTrue(all(keys), "a path with an empty fingerprint key")
        self.assertEqual(len(set(keys)), len(keys),
                         "two copies sharing a key: a fresh snapshot of one "
                         "would mark the other already done")
        for path, _switch, key in rows:
            self.assertEqual(api_import._paste_fp_key(path), key)


class TestEveryManualControlSaysWhyItWaitsForYou(Base):
    """NC-036's other half. A manual control that stays clickable still owes
    the reader the reason it waits for them.

    "No file needed" was the old answer for the two enrich rows, and it names the
    absence of a requirement rather than the presence of one — true, and no help
    at all in deciding whether to press it. The reason has to sit in the `need`
    slot specifically, because that is the only line visible without expanding
    the row: an explanation one click away does not answer "why is this asking
    me" for someone who has not clicked.
    """

    JS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "static", "app", "data.js")

    # NC-236: the per-row reason copy (WHY_LINE, SETUP_LINE, needText, RETIRED) and
    # the Export row's "when you ask" line went with the old Data page. That page
    # wrote each row's "why it waits for you" on the page itself, from maps keyed
    # by source. The new page draws every row's words from the server
    # (crm/api_ledger.py), which gives each row a cost chip (a file, a code to
    # paste, money...) and a "takes" sentence, so there is no client-side map left
    # to check. What still matters is below: the non-answer must not come back,
    # and the merge warning must stay.

    def test_find_duplicates_says_the_merge_is_irreversible(self):
        with open(self.JS, encoding="utf-8") as fh:
            src = fh.read()
        head = src[src.index('label="Possible duplicates"'):]
        head = head[:head.index("/>")]
        self.assertIn("cannot be undone", head,
                      "the duplicates row no longer says the merge is "
                      f"final: {head.strip()!r}")

    def test_no_row_answers_with_the_old_non_answer(self):
        """"No file needed" names the absence of a requirement, which is no help
        in deciding whether to press a button (NC-036, NC-067). The new page must
        not have brought it back in the page code or the server's row copy."""
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        for rel in (("static", "app", "data.js"), ("static", "app", "ledger.js"),
                    ("crm", "api_ledger.py")):
            with open(os.path.join(here, *rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("No file needed", text, "/".join(rel))

    def test_retiring_a_row_is_only_honest_while_the_automation_still_runs(self):
        """Retiring a control is NC-036's strongest move and its most dangerous.

        The three rows NC-038 removed are gone from the page because the app
        does them on its own. Delete one of those chained calls and there is no
        button left to notice it with: closeness silently stops recomputing,
        the writing profile silently freezes, and every surface goes on
        rendering the stale numbers as though they were current. That is not a
        hypothetical — `facts` had exactly this shape before NC-038, running
        after clicked imports and not background ones, on its way to never
        running at all as syncing moved to the background.

        So the retirement is pinned to the calls that justify it, in the
        autosync loop specifically. handle_import_run alone is not enough.
        """
        import ast
        src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "crm", "api_import.py")
        with open(src_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        # Named exactly. A substring match picks _api_autosync_pending, which
        # is a different function that only decides what is due.
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "run_autosync")
        called = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                elif isinstance(f, ast.Attribute):
                    base = f.value.id if isinstance(f.value, ast.Name) else ""
                    called.add(f"{base}.{f.attr}")
        for claim, call in (("engagement", "engagement.refresh"),
                            ("facts", "_run_chained_facts_pass"),
                            ("style", "_run_chained_style_pass")):
            self.assertIn(
                call, called,
                f"{claim!r} has no row on the Data page because the app is "
                f"supposed to do it automatically, but {call}() is no longer "
                f"called from the autosync loop ({fn.name}). It now happens "
                f"neither automatically nor by hand.")


class TestYourSourcesShowsOneRowPerRealSource(Base):
    """NC-032: the Data page listed too many things at once.

    # NC-236: folding a covered calendar row under its cover (foldedUnder,
    # coveringOf, the src-folded container) went with the old Data page. The new
    # page lists every calendar as its own row and only suppresses the "needs you"
    # entry for a source another one already covers (COVERED_BY, in
    # crm/api_ledger.py). The one rule that survives is that the map names real
    # sources, because a typo there fails open.
    """

    def test_every_covered_key_is_a_real_registry_source(self):
        import api_ledger
        self.assertTrue(api_ledger.COVERED_BY, "COVERED_BY is empty or gone")
        for key in api_ledger.COVERED_BY:
            self.assertIn(key, importers.REGISTRY,
                          f"COVERED_BY names {key!r}, which is not a source")


class TestAnUnchangedCopyIsNotAStaleCopy(Base):
    """A copy the helper has just verified is current, whatever its file's age.

    Staleness used to be read off the FILE'S MTIME. The helper deliberately does
    not re-copy a file it already has — that is what makes a two-minute timer
    affordable — so an address book nobody has edited for a fortnight keeps a
    fortnight-old mtime while being byte-identical to the live original. Old
    mtime, perfectly current data. Read that way, the row would say "14 days
    old", offer "Sync now" and sit in "Needs you", asking for a click that cannot
    help (there is no paste-it-yourself route for Apple Contacts either).

    The fix is `helper_current`: the helper's own verdict on that source, which
    is the only thing that actually knows.
    """

    def setUp(self):
        super().setUp()
        self.importers = importers
        self.saved = {
            "ab": importers.AB_AUTO_DB,
            "report": importers.imsg_helper_report,
        }
        # A copy whose mtime is two weeks old.
        self.ab = os.path.join(self.dir, "AddressBook-v22.abcddb")
        sqlite3.connect(self.ab).close()
        old = time.time() - 14 * 86400
        os.utime(self.ab, (old, old))
        importers.AB_AUTO_DB = self.ab

    def tearDown(self):
        self.importers.AB_AUTO_DB = self.saved["ab"]
        self.importers.imsg_helper_report = self.saved["report"]
        super().tearDown()

    def _reports(self, outcome):
        self.importers.imsg_helper_report = lambda: {
            "sources": [{"label": "Contacts", "outcome": outcome}]}

    def _finding(self):
        found = [f for f in self.importers.detect_sources()
                 if f["path"] == self.ab]
        self.assertEqual(len(found), 1, "the Contacts copy was not detected")
        return found[0]

    def test_the_helper_vouching_makes_the_copy_current(self):
        for good in ("copied", "current"):
            with self.subTest(outcome=good):
                self._reports(good)
                self.assertTrue(
                    self._finding()["helper_current"],
                    f"the helper reported {good!r} and the finding still does "
                    "not count as current")

    def test_a_blocked_or_missing_helper_leaves_the_file_judged_on_its_age(self):
        """The other direction matters as much: if the helper is NOT keeping the
        copy up to date, a fortnight-old file really is a fortnight old and the
        row should say so. Without this the fix would just hide staleness."""
        for bad in ("blocked", "absent", "unknown"):
            with self.subTest(outcome=bad):
                self._reports(bad)
                self.assertFalse(
                    self._finding()["helper_current"],
                    f"a {bad!r} helper still vouched for the copy")
        self.importers.imsg_helper_report = lambda: {}
        self.assertFalse(self._finding()["helper_current"],
                         "a helper that never reported still vouched")

    def test_the_api_reports_no_staleness_for_a_vouched_copy(self):
        """End to end through the endpoint the page actually reads, because the
        mtime arithmetic lives there and not in the finding."""
        def rows():
            return [f for f in api_import.handle_import_detect(self.conn)["found"]
                    if f["path"] == self.ab]

        self._reports("current")
        got = rows()
        self.assertEqual(len(got), 1)
        self.assertEqual(
            got[0]["stale_days"], 0,
            "a copy the helper vouched for is still reported as days old, so "
            "the row goes loud and asks to be pasted over")

        # And the control: same 14-day-old file, helper not vouching.
        self._reports("blocked")
        self.assertGreaterEqual(
            rows()[0]["stale_days"], 14,
            "a copy nothing is refreshing reports as fresh — the fix has "
            "hidden real staleness instead of correcting the signal")
