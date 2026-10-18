"""Enrichment merges: scraped facts land, manual edits survive (invariant 4's facts_source precedence)."""

import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    db,
)


class TestMergeScrapedFacts(unittest.TestCase):
    """merge_scraped_facts.merge(): scraped facts land, manual edits survive,
    id/name drift never writes to the wrong person."""

    FACTS = {"home_city": "London", "home_country": "United Kingdom",
             "home_metro": "London", "topics": '["robotics"]',
             "role_type": "founder", "blurb": "Runs Acme Robotics."}

    def setUp(self):
        import merge_scraped_facts as msf
        self.msf = msf
        self.dir = tempfile.mkdtemp(prefix="crm_msf_")
        self.src_path = os.path.join(self.dir, "scraper_crm.db")
        self.dst_path = os.path.join(self.dir, "main_crm.db")
        db.init_db(self.src_path)
        db.init_db(self.dst_path)

    def _seed(self, path, name, facts_source="", facts=None):
        conn = db.get_db(path)
        cid = db.create_contact(conn, name)
        sets = {"facts_source": facts_source, **(facts or {})}
        clause = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(f"UPDATE contacts SET {clause} WHERE id = ?",
                     (*sets.values(), cid))
        conn.commit()
        conn.close()
        return cid

    def test_scraped_facts_merge_and_manual_survives(self):
        # same insert order on both sides -> ids align, like the scraper's snapshot
        self._seed(self.src_path, "Ada Lovelace", "linkedin_scrape", self.FACTS)
        self._seed(self.src_path, "Bob Smith", "linkedin_scrape",
                   {**self.FACTS, "home_city": "Berlin"})
        self._seed(self.dst_path, "Ada Lovelace")
        self._seed(self.dst_path, "Bob Smith", "manual", {"home_city": "Leeds"})

        stats = self.msf.merge(self.src_path, self.dst_path, dry_run=False)
        self.assertEqual(stats["merged"], 1)
        self.assertEqual(stats["skipped_manual"], 1)

        conn = db.get_db(self.dst_path)
        ada, bob = (db.get_contact(conn, i) for i in (1, 2))
        conn.close()
        self.assertEqual(ada["home_city"], "London")
        self.assertEqual(ada["facts_source"], "linkedin_scrape")
        self.assertEqual(ada["topics"], '["robotics"]')
        self.assertEqual(bob["home_city"], "Leeds")       # manual edit kept
        self.assertEqual(bob["facts_source"], "manual")

    def test_partial_snapshot_never_blanks_master_columns(self):
        # The scraper's LLM often extracts only SOME fields. A snapshot row
        # with just a city must not erase a blurb/topics the master already
        # got from another pass (llm/offline) — merge is column-by-column.
        self._seed(self.src_path, "Ada Lovelace", "linkedin_scrape",
                   {"home_city": "London", "home_country": "",
                    "home_metro": "", "topics": "[]", "role_type": "",
                    "blurb": ""})
        self._seed(self.dst_path, "Ada Lovelace", "llm",
                   {"home_city": "", "topics": '["ai"]',
                    "role_type": "founder", "blurb": "Runs Acme Robotics."})

        stats = self.msf.merge(self.src_path, self.dst_path, dry_run=False)
        self.assertEqual(stats["merged"], 1)

        conn = db.get_db(self.dst_path)
        row = db.get_contact(conn, 1)
        conn.close()
        self.assertEqual(row["home_city"], "London")   # new data landed
        self.assertEqual(row["topics"], '["ai"]')         # kept — '[]' is no data
        self.assertEqual(row["role_type"], "founder")     # kept — '' is no data
        self.assertEqual(row["blurb"], "Runs Acme Robotics.")

    def test_identical_snapshot_counts_as_unchanged(self):
        self._seed(self.src_path, "Ada Lovelace", "linkedin_scrape", self.FACTS)
        self._seed(self.dst_path, "Ada Lovelace", "linkedin_scrape", self.FACTS)

        stats = self.msf.merge(self.src_path, self.dst_path, dry_run=False)
        self.assertEqual(stats["merged"], 0)
        self.assertEqual(stats["unchanged"], 1)

    def test_offline_pass_never_touches_scraped_facts(self):
        # enrich_facts pass A fills empty fields from phone/title heuristics —
        # far weaker than the scraper's read of the person's full profile, so
        # linkedin_scrape rows are off-limits (like manual and llm).
        import enrich_facts as ef
        conn = db.get_db(self.dst_path)
        cid = db.create_contact(conn, "Scraped Sam")
        conn.execute("UPDATE contacts SET facts_source='linkedin_scrape', "
                     "position='Founder & CEO' WHERE id=?", (cid,))
        conn.commit()
        self.assertEqual(ef.enrich_pass_a(conn, cid), {})
        conn.close()

    def test_id_name_mismatch_never_writes(self):
        self._seed(self.src_path, "Ada Lovelace", "linkedin_scrape", self.FACTS)
        self._seed(self.dst_path, "Totally Different Person")

        stats = self.msf.merge(self.src_path, self.dst_path, dry_run=False)
        self.assertEqual(stats["merged"], 0)
        self.assertEqual(stats["id_name_mismatch"], 1)

        conn = db.get_db(self.dst_path)
        row = db.get_contact(conn, 1)
        conn.close()
        self.assertEqual(row["home_city"], "")            # untouched

    def test_dry_run_writes_nothing(self):
        self._seed(self.src_path, "Ada Lovelace", "linkedin_scrape", self.FACTS)
        self._seed(self.dst_path, "Ada Lovelace")

        stats = self.msf.merge(self.src_path, self.dst_path, dry_run=True)
        self.assertEqual(stats["merged"], 1)              # counted, not written

        conn = db.get_db(self.dst_path)
        row = db.get_contact(conn, 1)
        conn.close()
        self.assertEqual(row["home_city"], "")


class _FakeRun:
    """Stands in for subprocess.run, recording argv and replaying outcomes in order.

    An outcome is (returncode, stdout, stderr), or an exception instance to raise.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        outcome = self.outcomes.pop(0) if self.outcomes else (0, "", "")
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out, err = outcome
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def argv(self, program):
        return [c for c in self.calls if c and c[0] == program]


class TestPullSnapshot(unittest.TestCase):
    """merge_scraped_facts.pull(): what gets copied off the scraper machine.

    The bug this class exists for: the scraper's crm.db is in WAL mode, so its
    newest commits live in a -wal sidecar, and this script only ever copied
    crm.db. Every profile scraped since SQLite last checkpointed was therefore
    absent from the file we merged — silently, because a WAL-mode db missing its
    sidecar reads as a valid, older database.
    """

    def setUp(self):
        import merge_scraped_facts as msf
        self.msf = msf

    def _pull(self, *outcomes, dest="/tmp/unused-by-a-faked-scp.db"):
        """Run pull() with subprocess faked. Returns (fake, printed_stdout)."""
        fake = _FakeRun(*outcomes)
        buf = io.StringIO()
        with mock.patch.object(self.msf.subprocess, "run", fake):
            with contextlib.redirect_stdout(buf):
                self.msf.pull(dest)
        return fake, buf.getvalue()

    def test_the_copy_takes_the_snapshot_not_the_live_db(self):
        fake, _ = self._pull((0, "", ""), (0, "", ""), (0, "", ""))
        ssh, scp = fake.argv("ssh"), fake.argv("scp")
        self.assertEqual(len(scp), 1)
        # the snapshot is requested BEFORE anything is copied
        self.assertEqual(fake.calls[0][0], "ssh")
        self.assertIn(".backup", ssh[0][-1])
        # and the file scp fetches is that snapshot, never network-crm/data/crm.db
        source = scp[0][-2]
        self.assertEqual(source, f"{self.msf.IMAC}:{self.msf.REMOTE_SNAPSHOT}")
        self.assertNotIn(self.msf.IMAC_DB, source)

    def test_the_snapshot_is_a_private_file_and_is_proven_to_exist(self):
        # A snapshot is a complete copy of the CRM database: it must not be
        # world-readable on the scraper Mac, and an empty one must not pass for a pull.
        fake, _ = self._pull((0, "", ""), (0, "", ""), (0, "", ""))
        remote_cmd = fake.argv("ssh")[0][-1]
        self.assertIn("umask 077", remote_cmd)
        self.assertIn("test -s", remote_cmd)
        self.assertIn(self.msf.REMOTE_SNAPSHOT, remote_cmd)

    def _assert_cleaned_up(self, fake):
        """A second ssh whose whole job is removing the snapshot.

        Matched exactly, not by prefix: the snapshot command *also* opens with
        `rm -f <snapshot>` (it clears a leftover before writing), so a
        startswith() check here passes even when the cleanup is gone entirely.
        """
        ssh = fake.argv("ssh")
        self.assertEqual(len(ssh), 2, "expected a snapshot ssh and a cleanup ssh")
        self.assertEqual(ssh[-1][-1], f"rm -f {self.msf.REMOTE_SNAPSHOT}")

    def test_the_snapshot_is_deleted_from_the_scraper_mac_afterwards(self):
        fake, _ = self._pull((0, "", ""), (0, "", ""), (0, "", ""))
        self._assert_cleaned_up(fake)

    def test_the_snapshot_is_deleted_even_when_the_transfer_fails(self):
        # A failed scp leaves the copy on the scraper Mac just as surely as a good one.
        fake = _FakeRun((0, "", ""), (1, "", "scp: Connection closed"), (0, "", ""))
        with mock.patch.object(self.msf.subprocess, "run", fake):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    self.msf.pull("/tmp/unused.db")
        self.assertIn("Could not reach the scraper machine", str(caught.exception))
        self._assert_cleaned_up(fake)

    def test_an_ssh_that_never_connects_fails_fast_instead_of_twice(self):
        # 255 is ssh's own "I never got there". scp is about to fail identically,
        # so the user should not sit through a second connection timeout to be
        # told the same thing — nor read a snapshot warning that isn't the cause.
        fake = _FakeRun((255, "", "ssh: connect to host scraper-host port 22: "
                                  "Operation timed out"))
        buf = io.StringIO()
        with mock.patch.object(self.msf.subprocess, "run", fake):
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as caught:
                    self.msf.pull("/tmp/unused.db")
        self.assertIn("Is the scraper Mac on and reachable over the network?", str(caught.exception))
        self.assertEqual(fake.argv("scp"), [])
        self.assertNotIn("snapshot", buf.getvalue())

    def test_a_scraper_mac_that_cannot_snapshot_still_syncs_and_says_so(self):
        # No sqlite3, /tmp unwritable: a slightly stale merge beats no merge, but
        # the degradation has to be visible or a stale sync reads as a clean one.
        fake, printed = self._pull((127, "", "sqlite3: command not found"), (0, "", ""))
        source = fake.argv("scp")[0][-2]
        self.assertEqual(source, f"{self.msf.IMAC}:{self.msf.IMAC_DB}")
        self.assertIn("could not make a snapshot", printed)
        self.assertIn("may lag", printed)
        # nothing was staged, so nothing is cleaned up
        self.assertEqual(len(fake.argv("ssh")), 1)

    def test_an_unrunnable_ssh_falls_back_to_copying_the_live_db(self):
        fake, printed = self._pull(OSError("no ssh binary"), (0, "", ""))
        self.assertEqual(fake.argv("scp")[0][-2], f"{self.msf.IMAC}:{self.msf.IMAC_DB}")
        self.assertIn("may lag", printed)

    def test_a_snapshot_that_times_out_falls_back_rather_than_aborting(self):
        fake, printed = self._pull(
            subprocess.TimeoutExpired(cmd="ssh", timeout=120), (0, "", ""))
        self.assertEqual(fake.argv("scp")[0][-2], f"{self.msf.IMAC}:{self.msf.IMAC_DB}")
        self.assertIn("may lag", printed)

    def test_the_snapshot_refuses_to_invent_a_source_that_is_not_there(self):
        # The sqlite3 CLI opens its db argument with O_CREAT, so a missing crm.db
        # would otherwise be CREATED empty and snapshotted as a valid 4096-byte
        # database — which passes `test -s`. Two guards stop that; both must stay.
        remote_cmd = self._pull((0, "", ""), (0, "", ""), (0, "", ""))[0].argv("ssh")[0][-1]
        self.assertIn(f"test -s {self.msf.IMAC_DB}", remote_cmd)
        self.assertIn("sqlite3 -readonly", remote_cmd)

    def test_a_missing_remote_db_is_fatal_and_says_which_path(self):
        # Not a degraded sync: falling back would copy a path we just proved is
        # absent. And not "could not reach the scraper machine" — it answered fine.
        fake = _FakeRun((self.msf.SOURCE_MISSING, "",
                         f"{self.msf.IMAC_DB} is missing or empty"))
        with mock.patch.object(self.msf.subprocess, "run", fake):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    self.msf.pull("/tmp/unused.db")
        message = str(caught.exception)
        self.assertIn(self.msf.IMAC_DB, message)
        self.assertNotIn("Could not reach the scraper machine", message)
        self.assertNotIn("try again", message.lower())
        self.assertEqual(fake.argv("scp"), [])


@unittest.skipUnless(shutil.which("sqlite3"), "needs the sqlite3 CLI")
class TestSnapshotMechanism(unittest.TestCase):
    """The reason pull() stages a snapshot, exercised against real SQLite.

    This is the regression net: if someone ever simplifies pull() back to copying
    crm.db directly, or swaps the snapshot for the cheaper-looking
    `PRAGMA wal_checkpoint`, one of these fails.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_snap_")
        self.live = os.path.join(self.dir, "live.db")
        conn = sqlite3.connect(self.live)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE contacts(id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        # a reader pinned to the pre-insert state, i.e. a scrape in progress
        self.reader = sqlite3.connect(self.live)
        self.reader.execute("BEGIN")
        self.reader.execute("SELECT COUNT(*) FROM contacts").fetchall()
        for i in range(40):
            conn.execute("INSERT INTO contacts(name) VALUES (?)", (f"person {i}",))
            conn.commit()
        self.conn = conn

    def tearDown(self):
        self.reader.close()
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _rows_after_copying(self, path):
        """What scp delivers: the named file alone, without its -wal sidecar."""
        landed = os.path.join(self.dir, "landed.db")
        shutil.copyfile(path, landed)
        conn = sqlite3.connect(landed)
        try:
            return conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return f"unreadable: {exc}"
        finally:
            conn.close()
            os.remove(landed)

    def test_copying_the_live_db_loses_the_uncheckpointed_writes(self):
        # The original bug, in one assertion: 40 committed rows, none of them in
        # the file the old pull() fetched.
        self.assertNotEqual(self._rows_after_copying(self.live), 40)

    def test_a_wal_checkpoint_would_not_have_been_enough(self):
        # The cheaper-looking fix is wrong: contended, TRUNCATE reports
        # busy through an exit code of ZERO while half-applying the WAL, so the
        # copy comes back corrupt and the caller is told nothing is wrong.
        r = subprocess.run(["sqlite3", self.live, "PRAGMA wal_checkpoint(TRUNCATE);"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "a busy checkpoint still exits 0")
        self.assertTrue(r.stdout.startswith("1|"), f"expected busy=1, got {r.stdout!r}")
        self.assertNotEqual(self._rows_after_copying(self.live), 40)

    def test_the_snapshot_carries_every_committed_row(self):
        snap = os.path.join(self.dir, "snap.db")
        r = subprocess.run(["sqlite3", self.live, f".backup '{snap}'"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._rows_after_copying(snap), 40)

    def test_a_failed_backup_is_reported_through_the_exit_code(self):
        # pull() branches on this, unlike the checkpoint it replaced.
        r = subprocess.run(["sqlite3", self.live, ".backup '/nope/nowhere/x.db'"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_a_plain_backup_would_fabricate_a_missing_source(self):
        # Why remote_snapshot() passes -readonly. Without it the CLI's O_CREAT
        # turns "the scraper has no db" into "here is a valid empty one", and the
        # 4096-byte result sails through the snapshot's own `test -s` check.
        missing = os.path.join(self.dir, "not-there.db")
        snap = os.path.join(self.dir, "fabricated.db")
        r = subprocess.run(["sqlite3", missing, f".backup '{snap}'"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "the CLI does not consider this an error")
        self.assertTrue(os.path.exists(missing), "it created the source it was given")
        self.assertGreater(os.path.getsize(snap), 0, "so `test -s` cannot catch it")

    def test_readonly_refuses_instead_and_still_reads_the_wal(self):
        missing = os.path.join(self.dir, "also-not-there.db")
        r = subprocess.run(["sqlite3", "-readonly", missing,
                            f".backup '{os.path.join(self.dir, 'x.db')}'"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(missing), "and creates nothing")
        # …while still capturing the uncheckpointed commits, which is the point of
        # the snapshot in the first place.
        snap = os.path.join(self.dir, "ro.db")
        r = subprocess.run(["sqlite3", "-readonly", self.live, f".backup '{snap}'"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._rows_after_copying(snap), 40)
