"""NC-082: crm.db's writer lock may not be held across anything slow.

WHAT THIS IS ABOUT

SQLite in WAL mode allows exactly one writer at a time. Python's sqlite3 opens a
write transaction IMPLICITLY on the first INSERT/UPDATE/DELETE and holds it until
commit. Readers are never blocked; every other WRITER waits, and after
db.WRITE_LOCK_TIMEOUT_SEC (20s) it fails with db.LOCKED_MESSAGE.

So a write, then something slow, then the commit, blocks every other writer for
the duration of the slow thing. NC-052 was this in api_ask.handle_chat. NC-082 is
the same shape in five more places, each holding the lock across a network call,
an importer subprocess, or a `sips` invocation — worst case 300 seconds against a
20-second ceiling.

It is not theoretical: an Ask question can vanish with nothing saved — a
conversation with zero rows — and because the client also gives up at 20 seconds
the user sees a timeout rather than the real reason.

HOW THESE TESTS WORK, AND WHY NOT BY READING THE SOURCE

The gate is on the CONSEQUENCE, not the mechanism: a stand-in for the slow call
opens a SECOND connection and tries to write, with a short ceiling. If that write
is refused, the lock was held. That is exactly what the real importer subprocess
and the real Ask question do, so a test that passes here means the thing that was
broken now works — as opposed to a source-level check for a commit(), which any
future refactor satisfies while reintroducing the bug one line up.

TestAutosyncHoldsNothingAcrossASubprocess is the one that would have caught the
worst case. Note that it needs TWO due sources: with one, the loop happened to be
safe because the facts-chain commit lands right after the stamp. That was luck,
not design, and a single-source test would have passed throughout.
"""
import contextlib
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Short, because a real ceiling of 20s would make every one of these tests take
# 20 seconds to fail. The blocking mechanism does not care what the ceiling is.
PROBE_MS = 400


class LockProbe:
    """Tries to write from a second connection, the way a real importer child or
    a real Ask question does. Records whether it got through."""

    def __init__(self, path):
        self.path = path
        self.attempts = []

    def attempt(self, label=""):
        other = sqlite3.connect(self.path, timeout=PROBE_MS / 1000)
        started = time.perf_counter()
        try:
            other.execute(f"PRAGMA busy_timeout = {PROBE_MS}")
            try:
                other.execute("INSERT INTO state(key, value, updated_at) "
                              "VALUES ('probe' || random(), '1', datetime('now'))")
                other.commit()
                self.attempts.append({"label": label, "blocked": False,
                                      "secs": time.perf_counter() - started})
            except sqlite3.OperationalError:
                self.attempts.append({"label": label, "blocked": True,
                                      "secs": time.perf_counter() - started})
        finally:
            # finally, not after the except: a connection left open with a failed
            # transaction holds the lock itself, and makes an unrelated later
            # write fail.
            other.close()

    @property
    def blocked(self):
        return [a for a in self.attempts if a["blocked"]]

    def report(self):
        return "; ".join(
            f"{a['label']}{'=BLOCKED' if a['blocked'] else '=free'} "
            f"({a['secs'] * 1000:.0f}ms)" for a in self.attempts) or "no attempts"


class TestTheHelperItself(Base):
    """db.release_write_lock is a named no-op-or-commit, so that a commit placed
    for lock reasons cannot be mistaken for one placed for durability and
    quietly moved."""

    def test_it_commits_a_pending_write(self):
        db.state_set(self.conn, "k", "v")
        self.assertTrue(self.conn.in_transaction)
        self.assertTrue(db.release_write_lock(self.conn))
        self.assertFalse(self.conn.in_transaction)

    def test_it_is_a_no_op_when_nothing_is_pending(self):
        """It must be safe to put in front of any slow call without first working
        out what came before, or it will not be used."""
        self.assertFalse(db.release_write_lock(self.conn))

    def test_a_read_does_not_count_as_pending(self):
        """WAL never blocks readers, so a SELECT holds nothing and must not
        provoke a commit."""
        self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()
        self.assertFalse(db.release_write_lock(self.conn))

    def test_it_actually_releases_the_lock_for_another_writer(self):
        probe = LockProbe(self.path)
        db.state_set(self.conn, "held", "yes")
        probe.attempt("while held")
        db.release_write_lock(self.conn)
        probe.attempt("after release")
        self.assertEqual([a["blocked"] for a in probe.attempts], [True, False],
                         f"the helper did not free the lock: {probe.report()}")

    def test_the_ceiling_and_the_message_are_still_what_the_comments_say(self):
        self.assertEqual(db.WRITE_LOCK_TIMEOUT_SEC, 20)
        self.assertIn("busy with another write", db.LOCKED_MESSAGE)


class TestAutosyncHoldsNothingAcrossASubprocess(Base):
    """The worst instance, and the one that could stop a real import.

    run_autosync stamps `import_synced_<source>` after each successful import and
    did not commit it. The loop then ran the NEXT source's importer as a
    subprocess with timeout=import_timeout_sec() — 300 seconds by default — while
    holding the lock that child needs to write its own rows. So two due WhatsApp
    folders in one pass meant the second import could not run at all.
    """

    def setUp(self):
        super().setUp()
        db.state_set(self.conn, app.ONBOARDED_STATE_KEY, "1")
        self.conn.commit()

    @contextlib.contextmanager
    def _two_due_sources(self):
        """Two due file sources, neither the iMessage paste nor an API source —
        those two both stamp-and-commit before their own subprocess, so they hide
        the bug. _autosync_pending is patched ON ITS OWNING MODULE: app.py does
        `from api_import import _autosync_pending`, so app's name is a copied
        reference and patching it changes nothing run_autosync can see.
        """
        found = []
        for name in ("waA", "waB"):
            d = os.path.join(self.dir, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "WhatsApp Chat with X.txt"), "w") as fh:
                fh.write("[2026-01-01, 10:00:00] X: hi\n")
            found.append({"source": "whatsapp", "path": d, "label": name, "mtime": ""})
        with mock.patch.object(app.api_import, "_autosync_pending",
                               lambda conn, probe_protected=True: found):
            yield found

    def _run(self):
        probe = LockProbe(self.path)

        class _Ok:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(cmd, **kwargs):
            what = next((os.path.basename(str(a)) for a in reversed(cmd)
                         if ".py" in str(a)), "?")
            probe.attempt(what)
            return _Ok()

        with self._two_due_sources(), \
             mock.patch.object(app.api_import.subprocess, "run", fake_run), \
             mock.patch.object(app.api_import.engagement, "refresh", lambda conn: None), \
             mock.patch.object(db, "DB_PATH", self.path):
            ran = app.api_import.run_autosync()
        return ran, probe

    def test_both_sources_run(self):
        """If the fixture stops producing two importer launches this whole class
        proves nothing, so assert the precondition rather than assume it."""
        ran, probe = self._run()
        self.assertEqual(ran, 2)
        importer_calls = [a for a in probe.attempts if "whatsapp_import" in a["label"]]
        self.assertEqual(len(importer_calls), 2, probe.report())

    def test_no_subprocess_runs_with_the_lock_held(self):
        _, probe = self._run()
        self.assertEqual(
            probe.blocked, [],
            "run_autosync held crm.db's writer lock while a subprocess ran. The "
            "child importer needs that lock to write its own rows and waits "
            f"{db.WRITE_LOCK_TIMEOUT_SEC}s before failing, against a subprocess "
            f"ceiling of {app.api_import.IMPORT_TIMEOUT_DEFAULT}s. "
            f"Attempts: {probe.report()}")

    def test_the_stamps_still_land(self):
        """Committing earlier must not lose the bookkeeping. Every source that
        ran has to end up stamped, or it re-imports on the next pass forever."""
        self._run()
        for source in ("whatsapp",):
            self.assertTrue(db.state_get(self.conn, f"import_synced_{source}", ""),
                            f"{source} ran but was never stamped as synced")
        self.assertTrue(db.state_get(self.conn, app.AUTOSYNC_STATE_KEY, ""))

    def test_the_subprocess_ceiling_is_still_far_over_the_lock_ceiling(self):
        """The whole reason this matters. If these two numbers ever cross, the
        hold could no longer outlast the wait and this class could be deleted."""
        self.assertGreater(app.api_import.IMPORT_TIMEOUT_DEFAULT,
                           db.WRITE_LOCK_TIMEOUT_SEC)


class TestNoImporterHoldsTheLockAcrossItsSlowCall(Base):
    """The other four instances, each driven through its own real function.

    One class rather than four because the failure is identical in all of them:
    a write, then a slow call, then a commit far away. What differs is only which
    slow thing sits in the middle.
    """

    def setUp(self):
        super().setUp()
        self.probe = LockProbe(self.path)
        self._path = mock.patch.object(db, "DB_PATH", self.path)
        self._path.start()

    def tearDown(self):
        self._path.stop()
        super().tearDown()

    def test_google_photos_releases_before_each_network_fetch(self):
        """photos.save_photo UPDATEs and never commits — nothing in photos.py
        does. So the first saved photo took the lock and held it across every
        remaining person's fetch_photo(), a urllib call with a 20s timeout each."""
        import google_photos_import as gp
        people = [{"n": i} for i in range(4)]
        cids = [db.create_contact(self.conn, f"Photo Person {i}") for i in range(4)]
        self.conn.commit()
        it = iter(cids)

        def fetch(url):
            self.probe.attempt("fetch_photo")
            return b"jpegbytes"

        with mock.patch.object(gp, "get_people_service", lambda *a, **k: object()), \
             mock.patch.object(gp, "iter_people", lambda svc: iter(people)), \
             mock.patch.object(gp, "real_photo_url", lambda p: "https://x/y.jpg"), \
             mock.patch.object(gp, "person_identity", lambda p: ("N", [], [])), \
             mock.patch.object(gp, "match_contact", lambda *a, **k: next(it, None)), \
             mock.patch.object(gp, "fetch_photo", fetch), \
             mock.patch.object(gp.photos, "save_photo",
                               lambda conn, cid, data, src: bool(conn.execute(
                                   "UPDATE contacts SET photo_file_id = ? WHERE id = ?",
                                   ("x", cid)))), \
             mock.patch.object(gp.db, "load_exclude_list", lambda p: set()):
            gp.import_account("a@b.c", "creds.json", dry_run=False)
        self.assertGreater(len(self.probe.attempts), 1,
                           "fewer than two fetches ran, so nothing was proved")
        self.assertEqual(self.probe.blocked, [],
                         "a photo write held the lock across the next person's "
                         f"network fetch: {self.probe.report()}")

    def test_contacts_import_releases_before_the_sips_subprocess(self):
        """photos.save_photo shells out to `sips` with a 30s ceiling, and the
        card's own writes (create_contact, fields, identifiers) sit uncommitted
        around it. apply() opens its own connection from db.DB_PATH, which setUp
        has already pointed at this test's database."""
        import contacts_import as ci

        def save_photo(conn, cid, data, src):
            self.probe.attempt("sips")
            conn.execute("UPDATE contacts SET photo_file_id = ? WHERE id = ?",
                         ("x", cid))
            return True

        cards = []
        for i in range(3):
            card = ci.VCard()
            card.name = f"Carded {i}"
            card.company, card.title = "C", "T"
            card.phones = [f"+4670000000{i}"]
            card.emails = [f"c{i}@x.se"]
            card.photo = b"jpegbytes"
            cards.append(card)
        with mock.patch.object(ci.photos, "save_photo", save_photo), \
             mock.patch.object(ci.db, "load_exclude_list", lambda p: set()):
            stats = ci.apply(cards, enrich_only=False, dry_run=False)
        self.assertEqual(stats["created"], 3, "the fixture created no contacts, "
                                              "so no photo path was reached")
        self.assertEqual(len(self.probe.attempts), 3, self.probe.report())
        self.assertEqual(self.probe.blocked, [],
                         "a vCard's writes held the lock across the `sips` "
                         f"subprocess: {self.probe.report()}")

    def test_email_import_releases_before_the_first_model_call(self):
        """The channel and identifier writes shared one commit with the whole
        summarisation pass, so they held the lock across every Anthropic call."""
        import email_import as ei
        cids = [db.create_contact(self.conn, f"Mailer {i}") for i in range(3)]
        self.conn.commit()
        pool = list(cids)

        def summarize(client, name, subjects):
            self.probe.attempt("anthropic")
            return f"summary of {name}"

        agg = {}
        for i in range(3):
            a = ei.AddressAgg()
            a.inbound, a.outbound = 2, 1
            a.name = f"Mailer {i}"
            a.first = a.last = f"2026-01-0{i + 1} 10:00:00"
            a.subjects = ["Subject one", "Subject two"]
            agg[f"m{i}@x.se"] = a
        with mock.patch.object(ei, "summarize_email", summarize), \
             mock.patch.object(ei, "match_contact",
                               lambda conn, idx, address, name: pool.pop(0) if pool else None), \
             mock.patch.object(ei.db, "load_exclude_list", lambda p: set()):
            stats, _ = ei.apply(agg, create_new=False, dry_run=False, client=object())
        self.assertEqual(stats["matched"], 3, "the fixture matched no contacts, so "
                                              "the summarisation pass never ran")
        self.assertEqual(len(self.probe.attempts), 3, self.probe.report())
        self.assertEqual(self.probe.blocked, [],
                         "the scan's writes held the lock across the "
                         f"summarisation calls: {self.probe.report()}")

    def test_enrich_facts_pass_c_releases_before_each_model_call(self):
        """Pass C's _apply_changes UPDATEs without committing, and the next
        contact's model call plus a 0.3s sleep followed."""
        import enrich_facts as ef
        cid = db.create_contact(self.conn, "Enriched Erik")
        self.conn.commit()

        def one(conn, arc_conn, contact_id, client):
            self.probe.attempt("anthropic")
            return {"home_city": "Lisbon"}

        # Drive the loop's body directly: main() parses argv, opens its own
        # connection from DB_PATH and reads a candidate query this fixture would
        # have to reproduce exactly. The behaviour under test is the release, so
        # the loop is what the test should own.
        with mock.patch.object(ef, "enrich_pass_c_one", one):
            for _ in range(3):
                db.release_write_lock(self.conn)
                changes = ef.enrich_pass_c_one(self.conn, None, cid, object())
                if changes:
                    ef._apply_changes(self.conn, cid, changes, "llm")
                    self.conn.commit()
        self.assertEqual(self.probe.blocked, [], self.probe.report())

    def test_the_real_pass_c_loop_has_the_release_and_the_commit(self):
        """The test above owns a copy of the loop's shape, so this one checks the
        real loop still has both halves — a copy that drifts proves nothing."""
        with open(os.path.join(REPO, "crm", "enrich_facts.py"), encoding="utf-8") as fh:
            src = fh.read()
        at = src.index("changes_c = enrich_pass_c_one(")
        window = src[max(0, at - 1400):at + 400]
        self.assertIn("db.release_write_lock(conn)", window,
                      "Pass C no longer releases the lock before its model call")
        after = src[at:at + 400]
        self.assertIn("conn.commit()", after,
                      "Pass C no longer commits each enrichment as it lands, so "
                      "one failure late in the pass discards everything paid for")


class TestGmailReleasesBeforeFetchingHistory(Base):
    """gmail_import created a contact and then fetched its messages one API call
    at a time, with the only commit after every eligible sender."""

    def test_the_new_contact_is_committed_before_its_messages_are_fetched(self):
        src_path = os.path.join(REPO, "crm", "gmail_import.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        at = src.index('cid = db.create_contact(crm_conn, name, source="email")')
        upto = src.index('for msg_id, date_hdr in zip(', at)
        between = src[at:upto]
        self.assertIn("crm_conn.commit()", between,
                      "gmail_import creates a contact and then makes one Gmail "
                      "API call per message with that write uncommitted, so the "
                      "subprocess holds crm.db's writer lock across all of them")

    def test_it_is_the_crm_connection_not_the_archive_one(self):
        """The message bodies go to arc_conn, a different database with its own
        lock. Committing that one instead would look right and fix nothing."""
        with open(os.path.join(REPO, "crm", "gmail_import.py"), encoding="utf-8") as fh:
            src = fh.read()
        at = src.index('cid = db.create_contact(crm_conn, name, source="email")')
        between = src[at:src.index('for msg_id, date_hdr in zip(', at)]
        self.assertNotIn("arc_conn.commit()", between)


if __name__ == "__main__":
    unittest.main()


class TestAFailedQuestionIsNotLost(unittest.TestCase):
    """The user-visible half of NC-082.

    A write that cannot get the lock fails with nothing saved — a
    conversation with zero rows is what a lost question looks like in
    the database. The Ask view clears the input the moment you hit send; unless
    it puts the text back on failure, the words are gone from the box too and the
    question has to be retyped out of the transcript above it.

    Gated at source because no test renders this component, and the whole fix is
    one line in a catch block that any refactor of the send path would drop.
    """

    def _catch_block(self):
        """From the catch's own first statement to the end of send().

        Anchored on `} catch (e) {` and closed on the next top-level `const `,
        NOT on a fixed number of characters after a literal that happens to sit
        there: a field added to the object that literal belongs to makes such an
        anchor vanish (NC-157). A window measured in characters is a test of how
        much has been written near the thing it checks.
        """
        with open(os.path.join(REPO, "static", "app", "ask.js"), encoding="utf-8") as fh:
            js = fh.read()
        at = js.index("} catch (e) {")
        return js[at:js.index("\n  const ", at)]

    def test_the_question_goes_back_in_the_box(self):
        self.assertRegex(
            self._catch_block(), r"setText\(",
            "a failed question leaves the input empty, so it has to be retyped "
            "from the transcript to be sent again")

    def test_it_does_not_overwrite_something_newly_typed(self):
        """Restoring unconditionally would clobber a question typed while the
        first one was in flight — a worse bug than retyping."""
        self.assertRegex(
            self._catch_block(), r"setText\(\s*t\s*=>",
            "the restore is not a functional update, so it cannot tell an empty "
            "box from one you are typing in")

    def test_the_send_path_still_clears_it_on_send(self):
        """The counterpart. If setText("") ever goes away the restore is
        meaningless, and the box would keep every question ever asked."""
        with open(os.path.join(REPO, "static", "app", "ask.js"), encoding="utf-8") as fh:
            js = fh.read()
        at = js.index("const send = useCallback")
        # Two rules about reading the gate's own subject.
        #
        # The gate reads the whole function, not a fixed window past `at`: a long
        # comment at the top of send() (NC-154) would push the line it protects
        # out of any fixed reach.
        #
        # But widening alone would make the gate TOOTHLESS, which is worse than
        # brittle: a comment further down this same function says
        # `setText("") above clears the input`, so the assertion would match the
        # explanation instead of the code. Comments are stripped first — the same
        # trap helpers.code_of exists for on the Python side.
        import re as _re
        body = js[at:js.index("\n  }, [", at)]
        body = _re.sub(r"/\*.*?\*/", " ", body, flags=_re.S)
        body = _re.sub(r"(?m)//.*$", "", body)
        self.assertIn('setText("")', body)
