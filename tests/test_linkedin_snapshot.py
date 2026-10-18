"""NC-070: recovering the LinkedIn messages stranded in a frozen scraper snapshot.

The thing being defended here is not "does it copy rows". It is that the copy
attaches each message to the RIGHT person, in the RIGHT direction, and that
running it twice does not double anybody's history. Two of those are derived from
the data rather than configured, so each has a test that would fail if the
derivation broke.
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import arc, code_of, db  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import linkedin_snapshot_import as M  # noqa: E402

OWNER = "Jordan Lee Berg"
_MODULE_PATH = M.__file__


def snap_key(convo, date, sender, body):
    """The id the old scraper stored — and, by construction, the source_key the
    archive uses. If these two ever stop being the same function, dedup silently
    stops working and every re-run doubles the history, so a test asserts it.
    """
    return hashlib.sha1(f"{convo}|{date}|{sender}|{body}".encode()).hexdigest()


class SnapshotFixture(unittest.TestCase):
    """A miniature snapshot + CRM + archive, wired the way the real ones are."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="li_snap_")
        self.snap_path = os.path.join(self.dir, "linkedin_messages.db")
        self.arc_path = os.path.join(self.dir, "archive.db")
        self.crm_path = os.path.join(self.dir, "crm.db")

        db.init_db(self.crm_path)
        self.crm = db.get_db(self.crm_path)
        self.ana = db.create_contact(self.crm, "Ana Silva", source="manual")
        self.bo = db.create_contact(self.crm, "Bo Lind", source="manual")
        self.crm.commit()

        self.snap = sqlite3.connect(self.snap_path)
        self.snap.executescript("""
            CREATE TABLE messages (
              id TEXT PRIMARY KEY, conversation_id TEXT, conversation TEXT,
              sender TEXT, recipient TEXT, date_utc TEXT, subject TEXT,
              body TEXT, folder TEXT, raw TEXT);
            CREATE TABLE contacts (
              url TEXT PRIMARY KEY, name TEXT, position TEXT, company TEXT,
              connected_on TEXT, email TEXT);
        """)

    def tearDown(self):
        try:
            self.crm.close()
        except Exception:
            pass

    def add_msg(self, sender, recipient, body, convo="c1",
                date="2019-03-04 10:00:00 UTC"):
        key = snap_key(convo, date, sender, body)
        self.snap.execute(
            "INSERT OR REPLACE INTO messages "
            "(id, conversation_id, conversation, sender, recipient, date_utc, "
            " subject, body, folder, raw) VALUES (?,?,?,?,?,?,'',?,'INBOX','')",
            (key, convo, recipient, sender, recipient, date, body))
        self.snap.commit()
        return key

    def add_owner_bulk(self, n=30, to="Ana Silva"):
        """The owner has to be the most frequent sender for owner_name() to find
        them, which is true of any real message file by a wide margin.

        `to` matters more than it looks: the filler is 30 real messages with
        whoever it names, so a test about what happens to one person's messages
        has to point the filler at somebody else or it measures the filler.
        """
        for i in range(n):
            self.add_msg(OWNER, to, f"filler {i}", convo=f"bulk{i}",
                         date=f"2019-01-{(i % 28) + 1:02d} 09:00:00 UTC")

    def plan(self):
        return M.plan(self.snap_path, self.arc_path, crm_conn=self.crm)

    def run_it(self, dry_run=False, create_contacts=True):
        # plan() and create_missing() open their own CRM connections via
        # db.get_db(), so point the module-level default at the fixture
        # database for the duration.
        orig = db.DB_PATH
        db.DB_PATH = self.crm_path
        try:
            return M.run(self.snap_path, self.arc_path, dry_run=dry_run,
                         create_contacts=create_contacts)
        finally:
            db.DB_PATH = orig

    def archived(self, contact_id=None, channel="linkedin"):
        if not os.path.exists(self.arc_path):
            return []
        c = arc.get_archive_db(self.arc_path)
        try:
            if contact_id is None:
                return c.execute(
                    "SELECT * FROM messages WHERE channel=? ORDER BY ts",
                    (channel,)).fetchall()
            return c.execute(
                "SELECT * FROM messages WHERE channel=? AND contact_id=? "
                "ORDER BY ts", (channel, contact_id)).fetchall()
        finally:
            c.close()


class TestTheIdIsTheSourceKey(SnapshotFixture):
    """Dedup rests entirely on the snapshot's `id` being the archive's
    `source_key`. Nothing enforces that at runtime, so it is asserted here."""

    def test_the_two_hash_the_same_four_fields(self):
        import linkedin_import as li
        convo, date, sender, body = "c9", "2020-05-05 07:00:00 UTC", "Ana Silva", "hi"
        theirs = li._msg_source_key({
            "CONVERSATION ID": convo, "DATE": date,
            "FROM": sender, "CONTENT": body})
        self.assertEqual(theirs, snap_key(convo, date, sender, body))

    def test_a_recovered_row_lands_under_the_snapshot_id(self):
        self.add_owner_bulk()
        key = self.add_msg("Ana Silva", OWNER, "an old note", convo="deep",
                           date="2018-02-25 08:00:00 UTC")
        self.run_it()
        keys = {r["source_key"] for r in self.archived()}
        self.assertIn(key, keys)


class TestRunningItTwiceChangesNothing(SnapshotFixture):
    """The whole safety argument for pointing this at a real archive."""

    def test_second_run_adds_zero(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "one", convo="d1",
                     date="2018-03-01 08:00:00 UTC")
        self.add_msg(OWNER, "Bo Lind", "two", convo="d2",
                     date="2018-03-02 08:00:00 UTC")
        first = self.run_it()
        before = len(self.archived())
        second = self.run_it()
        self.assertGreater(first["new"], 0)
        self.assertEqual(second["new"], 0, "a re-run inserted rows")
        self.assertEqual(len(self.archived()), before)

    def test_a_message_already_archived_is_not_offered_again(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "already there", convo="d1",
                     date="2018-03-01 08:00:00 UTC")
        self.run_it()
        # Second plan must not even list it as attachable.
        bodies = {r["CONTENT"] for r in self.plan()["rows"]}
        self.assertNotIn("already there", bodies)


class TestDirectionIsNotGuessedBackwards(SnapshotFixture):
    """A flipped direction is the failure that would look fine in every count
    and be wrong in every conversation — the owner's words rendered as theirs."""

    def test_a_message_from_the_contact_is_inbound(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "she wrote this", convo="in1",
                     date="2018-04-01 08:00:00 UTC")
        self.run_it()
        row = [r for r in self.archived(self.ana) if r["body"] == "she wrote this"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["direction"], 0)

    def test_a_message_from_the_owner_is_outbound(self):
        self.add_owner_bulk()
        self.add_msg(OWNER, "Bo Lind", "the owner wrote this", convo="out1",
                     date="2018-04-02 08:00:00 UTC")
        self.run_it()
        row = [r for r in self.archived(self.bo) if r["body"] == "the owner wrote this"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["direction"], 1)

    def test_the_owner_is_kept_out_of_the_name_map(self):
        # If the owner were in it, archive_messages would read "sender is a known
        # contact" as inbound and label everything they ever sent as received.
        db.create_contact(self.crm, OWNER, source="manual")
        self.crm.commit()
        names = M.name_index(self.crm, OWNER)
        self.assertNotIn(OWNER.lower(), names)
        self.assertIn("ana silva", names)


class TestOwnerDetection(SnapshotFixture):
    """owner_name() is derived from the data, so it gets its own tests."""

    def test_the_most_frequent_sender_wins(self):
        self.add_owner_bulk(n=12)
        self.add_msg("Ana Silva", OWNER, "just one", convo="x")
        self.assertEqual(M.owner_name(self.snap), OWNER)

    def test_blank_senders_cannot_win(self):
        for i in range(40):
            self.snap.execute(
                "INSERT INTO messages (id, conversation_id, sender, recipient, "
                "date_utc, body) VALUES (?,?,'','Ana Silva','2019-01-01',?)",
                (f"blank{i}", f"b{i}", f"body {i}"))
        self.snap.commit()
        self.add_owner_bulk(n=5)
        self.assertEqual(M.owner_name(self.snap), OWNER)


class TestItCreatesThePeopleTheSnapshotCanDescribe(SnapshotFixture):
    """A person the snapshot has a `contacts` row for is created and given their
    messages; a name the snapshot cannot describe is still only counted, because
    there is nothing to make a card out of."""

    def stranger(self, name="Kim Stranger", url="u/x", **cols):
        self.snap.execute(
            "INSERT INTO contacts (url, name, position, company, connected_on) "
            "VALUES (?,?,?,?,?)",
            (url, name, cols.get("position", "Analyst"),
             cols.get("company", "Acme Works"), cols.get("connected_on", "27 Dec 2024")))
        self.snap.commit()

    def test_a_stranger_is_created_and_gets_their_messages(self):
        self.add_owner_bulk()
        self.stranger()
        self.add_msg("Kim Stranger", OWNER, "from a stranger", convo="s1",
                     date="2018-06-01 08:00:00 UTC")
        before = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        r = self.run_it()
        after = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        self.assertEqual(after, before + 1)
        self.assertEqual(r["created"], 1)
        self.assertIn("from a stranger", {x["body"] for x in self.archived()})

    def test_the_created_card_carries_what_the_snapshot_knew(self):
        self.add_owner_bulk()
        self.stranger()
        self.add_msg("Kim Stranger", OWNER, "hello", convo="s1",
                     date="2018-06-01 08:00:00 UTC")
        self.run_it()
        row = self.crm.execute(
            "SELECT name, position, company, linkedin_url, connected_on, source "
            "FROM contacts WHERE name_norm = ?",
            (M._key("Kim Stranger"),)).fetchone()
        self.assertEqual(row["position"], "Analyst")
        self.assertEqual(row["company"], "Acme Works")
        self.assertEqual(row["linkedin_url"], "u/x")
        self.assertEqual(row["source"], "linkedin")
        # Every connected_on already in crm.db is ISO. The snapshot writes the
        # date the way LinkedIn shows it, so a raw copy would be the only
        # non-ISO value in the column.
        self.assertEqual(row["connected_on"], "2024-12-27")

    def test_no_create_reports_them_instead(self):
        self.add_owner_bulk()
        self.stranger()
        self.add_msg("Kim Stranger", OWNER, "from a stranger", convo="s1",
                     date="2018-06-01 08:00:00 UTC")
        before = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        r = self.run_it(create_contacts=False)
        after = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        self.assertEqual(before, after, "--no-create still created a contact")
        self.assertEqual(r["created"], 0)
        self.assertEqual(r["skipped"]["person not in the CRM"], 1)
        self.assertEqual(r["creatable"][M._key("Kim Stranger")]["messages"], 1)
        self.assertNotIn("from a stranger", {x["body"] for x in self.archived()})

    def test_a_dry_run_creates_nobody(self):
        self.add_owner_bulk()
        self.stranger()
        self.add_msg("Kim Stranger", OWNER, "from a stranger", convo="s1",
                     date="2018-06-01 08:00:00 UTC")
        before = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        r = self.run_it(dry_run=True)
        after = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(r["created"], 0)

    def test_creating_twice_creates_once(self):
        self.add_owner_bulk()
        self.stranger()
        self.add_msg("Kim Stranger", OWNER, "from a stranger", convo="s1",
                     date="2018-06-01 08:00:00 UTC")
        self.run_it()
        n_after_first = self.crm.execute(
            "SELECT COUNT(*) FROM contacts").fetchone()[0]
        second = self.run_it()
        self.assertEqual(second["created"], 0)
        self.assertEqual(
            self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            n_after_first)

    def test_plan_alone_still_writes_nothing(self):
        """`plan` reports the creatable people; only `run` acts on them. If that
        ever inverts, --dry-run stops meaning what it says."""
        self.add_owner_bulk()
        self.stranger()
        self.add_msg("Kim Stranger", OWNER, "hi", convo="s1",
                     date="2018-06-01 08:00:00 UTC")
        before = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        r = self.plan()
        self.assertEqual(len(r["creatable"]), 1)
        self.assertEqual(
            self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            before)

    def test_a_nameless_snapshot_row_makes_no_card(self):
        """An empty-named contact cannot be found or deleted from the UI, so it
        is worse than the message staying unattached."""
        self.add_owner_bulk()
        self.snap.execute(
            "INSERT INTO contacts (url, name) VALUES ('u/blank', '   ')")
        self.snap.commit()
        before = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        self.assertEqual(M.create_missing({"": {"name": "  "}}), 0)
        self.assertEqual(
            self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            before)

    def test_an_unknown_name_is_counted_separately(self):
        self.add_owner_bulk()
        self.add_msg("Nobody AtAll", OWNER, "who?", convo="s2",
                     date="2018-06-02 08:00:00 UTC")
        r = self.run_it()
        self.assertEqual(r["skipped"]["name not recognised"], 1)
        self.assertEqual(r["skipped"]["person not in the CRM"], 0)
        self.assertEqual(r["created"], 0)


class TestTheThreadFallback(SnapshotFixture):
    """When the display name does not resolve, the archive already knows who a
    thread belongs to — but only when it attached the thread to ONE person."""

    def _seed_archive(self, contact_id, convo, date, sender, body):
        arc.init_archive(self.arc_path)
        c = arc.get_archive_db(self.arc_path)
        arc.insert_message(c, contact_id=contact_id, channel="linkedin",
                           direction=0, ts=date.replace(" UTC", ""), body=body,
                           source_key=snap_key(convo, date, sender, body))
        c.commit()
        c.close()

    def test_a_thread_the_archive_knows_resolves(self):
        self.add_owner_bulk()
        # One message of the thread already crossed, attached to Ana.
        self.add_msg("Ana Renamed Silva", OWNER, "first", convo="t1",
                     date="2018-07-01 08:00:00 UTC")
        self._seed_archive(self.ana, "t1", "2018-07-01 08:00:00 UTC",
                           "Ana Renamed Silva", "first")
        # A second, still stranded, whose sender name matches no contact.
        self.add_msg("Ana Renamed Silva", OWNER, "second", convo="t1",
                     date="2018-07-02 08:00:00 UTC")
        self.run_it()
        bodies = {r["body"] for r in self.archived(self.ana)}
        self.assertIn("second", bodies)

    def test_a_group_thread_is_left_alone(self):
        self.add_owner_bulk()
        for who, body, d in ((self.ana, "a", "2018-08-01 08:00:00 UTC"),
                             (self.bo, "b", "2018-08-02 08:00:00 UTC")):
            self.add_msg("Group Person", OWNER, body, convo="g1", date=d)
            self._seed_archive(who, "g1", d, "Group Person", body)
        # A third message in the same thread, sender unknown to the CRM. The
        # archive attached this thread to two different people, so there is no
        # single right answer and it must not pick one.
        self.add_msg("Group Person", OWNER, "third", convo="g1",
                     date="2018-08-03 08:00:00 UTC")
        self.assertEqual(M.thread_index(self.snap, self.arc_path).get("g1"), None)
        r = self.run_it()
        self.assertNotIn("third", {x["body"] for x in self.archived()})
        self.assertEqual(r["skipped"]["name not recognised"], 1)


class TestAmbiguousNames(SnapshotFixture):
    """Two contacts with the same display name is a coin flip on someone's
    private messages. It does not get flipped."""

    def test_a_duplicated_name_resolves_to_nobody(self):
        db.create_contact(self.crm, "Ana Silva", source="manual")
        self.crm.commit()
        names = M.name_index(self.crm, OWNER)
        self.assertNotIn("ana silva", names)
        self.assertIn("bo lind", names)


class TestWhatItLeavesBehind(SnapshotFixture):
    def test_a_bodyless_row_is_skipped_and_counted(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "", convo="e1",
                     date="2018-09-01 08:00:00 UTC")
        r = self.run_it()
        self.assertEqual(r["skipped"]["no body"], 1)

    def test_the_reasons_add_up_to_the_whole_gap(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "keep", convo="k",
                     date="2018-10-01 08:00:00 UTC")
        self.add_msg("Ana Silva", OWNER, "", convo="k2",
                     date="2018-10-02 08:00:00 UTC")
        self.add_msg("Nobody AtAll", OWNER, "drop", convo="k3",
                     date="2018-10-03 08:00:00 UTC")
        r = self.plan()
        gap = len(M.missing_rows(self.snap, self.arc_path))
        self.assertEqual(len(r["rows"]) + sum(r["skipped"].values()), gap)


class TestDryRunWritesNothing(SnapshotFixture):
    def test_no_archive_appears(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "preview me", convo="p1",
                     date="2018-11-01 08:00:00 UTC")
        r = self.run_it(dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["new"], 0)
        self.assertGreater(len(r["rows"]), 0, "dry run found nothing to report")
        self.assertEqual(self.archived(), [])

    def test_it_still_says_what_it_would_do(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "preview me", convo="p1",
                     date="2018-11-01 08:00:00 UTC")
        bodies = {x["CONTENT"] for x in self.run_it(dry_run=True)["rows"]}
        self.assertIn("preview me", bodies)


class TestNoSnapshotIsNotAnError(unittest.TestCase):
    """Every install that never ran the old scraper — including every future
    one — has no snapshot. That is the normal case, not a failure."""

    def test_a_missing_file_reports_unavailable(self):
        d = tempfile.mkdtemp(prefix="li_none_")
        r = M.run(os.path.join(d, "nope.db"), os.path.join(d, "archive.db"))
        self.assertFalse(r["available"])
        self.assertEqual(r["new"], 0)

    def test_open_returns_none_rather_than_raising(self):
        self.assertIsNone(M._open("/definitely/not/here.db"))


class TestRecoveredMessagesAreSearchable(SnapshotFixture):
    """A message you can read on a timeline but cannot find by searching is only
    half recovered — and chunks are derived data no importer builds as a side
    effect of the insert, so forgetting this step is silent."""

    def _chunks(self):
        if not os.path.exists(self.arc_path):
            return 0
        c = arc.get_archive_db(self.arc_path)
        try:
            return c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            c.close()

    def test_chunks_are_built_for_what_it_recovered(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER,
                     "a long enough message to chunk on its own merits, twice over",
                     convo="s1", date="2019-02-01 08:00:00 UTC")
        r = self.run_it()
        self.assertGreater(r["new"], 0)
        self.assertGreater(r["chunks"], 0, "recovered messages were never chunked")
        self.assertGreater(self._chunks(), 0)

    def test_a_dry_run_builds_none(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "preview only", convo="s2",
                     date="2019-02-02 08:00:00 UTC")
        self.run_it(dry_run=True)
        self.assertEqual(self._chunks(), 0)

    def test_a_settled_archive_does_no_work(self):
        # The run after a successful recovery has nothing missing, so it must
        # not re-chunk anybody.
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "settle me", convo="s3",
                     date="2019-02-03 08:00:00 UTC")
        self.run_it()
        again = self.run_it()
        self.assertEqual(again["new"], 0)
        self.assertEqual(again.get("chunks", 0), 0)


class TestTheCountIsWhatWasRecovered(SnapshotFixture):
    """The never-lower guard is what hid this bug: it kept the NUMBER while the
    bodies were unreachable. It is gone (NC-107). After a recovery the card says
    how many messages are there to show."""

    def test_the_card_counts_the_saved_messages_and_nothing_else(self):
        self.add_owner_bulk()
        self.add_msg("Ana Silva", OWNER, "one real message", convo="w1",
                     date="2018-12-01 08:00:00 UTC")
        db.set_channel(self.crm, self.ana, "linkedin", 84, "2018-12-01")
        self.crm.commit()
        self.run_it()
        a = arc.get_archive_db(self.arc_path)
        saved = a.execute("SELECT COUNT(*) FROM messages WHERE contact_id = ? "
                          "AND channel = 'linkedin'", (self.ana,)).fetchone()[0]
        a.close()
        row = db.get_contact(db.get_db(self.crm_path), self.ana)
        self.assertGreater(saved, 0)
        self.assertEqual(row["msg_count"], saved)
        self.assertLess(row["msg_count"], 84, "the 84 had no bodies behind it")

    def test_a_dry_run_changes_no_count(self):
        self.add_owner_bulk()
        db.set_channel(self.crm, self.ana, "linkedin", 84, "2018-12-01")
        self.crm.commit()
        self.run_it(dry_run=True)
        self.assertEqual(db.get_contact(db.get_db(self.crm_path), self.ana)["msg_count"], 84)


if __name__ == "__main__":
    unittest.main()


class TestNamesMatchTheWayTheCrmMatchesNames(SnapshotFixture):
    """NC-071. This file used to report messages it could not place. Some of
    them belonged to people who were in crm.db all along: the snapshot spells
    their names with a DOUBLE SPACE, and the lookup compared raw lower-cased
    strings while the rest of the app goes through `db.normalize_name`, which
    collapses whitespace runs. Two spaces against one was the whole of the bug,
    and it hid real conversations."""

    def test_a_double_space_still_finds_the_person(self):
        self.add_owner_bulk()
        self.add_msg("Ana  Silva", OWNER, "two spaces in the name", convo="d1",
                     date="2018-07-01 08:00:00 UTC")
        r = self.run_it()
        self.assertEqual(r["skipped"]["person not in the CRM"], 0)
        self.assertEqual(r["skipped"]["name not recognised"], 0)
        self.assertIn("two spaces in the name",
                      {x["body"] for x in self.archived(self.ana)})

    def test_a_decoration_glyph_still_finds_the_person(self):
        """LinkedIn appends ⟢ to display names that carry a hiring/open badge."""
        self.add_owner_bulk()
        self.add_msg("Ana Silva \u27e2", OWNER, "decorated name", convo="d2",
                     date="2018-07-02 08:00:00 UTC")
        r = self.run_it()
        self.assertIn("decorated name",
                      {x["body"] for x in self.archived(self.ana)})
        self.assertEqual(r["skipped"]["name not recognised"], 0)

    def test_the_owner_is_excluded_even_when_the_snapshot_decorates_the_name(self):
        """The real hazard, not just a missed match. `archive_messages` reads
        "sender is in the map" as "this came IN", so the owner has to be out of
        the map or every message the owner sent lands on their own card as inbound.
        The old code excluded the owner by their snapshot spelling, which can
        carry the ⟢; when it does, their crm.db spelling stayed in."""
        me = db.create_contact(self.crm, OWNER, source="manual")
        self.crm.commit()
        decorated = OWNER + " \u27e2"
        for i in range(30):
            self.add_msg(decorated, "Ana Silva", f"filler {i}", convo=f"o{i}",
                         date=f"2019-02-{(i % 28) + 1:02d} 09:00:00 UTC")
        self.assertEqual(M.owner_name(self.snap), decorated.strip())
        names = M.name_index(self.crm, M.owner_name(self.snap))
        self.assertNotIn(M._key(OWNER), names,
                         "the account holder is in their own name map")
        self.run_it()
        self.assertEqual(len(self.archived(me)), 0,
                         "messages were filed onto the account holder's card")

    def test_direction_survives_a_decorated_owner(self):
        """The bug this class exists for, in its second form: when `owner_key`
        became normalized and the direction comparison stayed raw, every
        recovered message was written INBOUND — including the ones the owner sent.
        Counts would not show it, so the test reads the rows afterwards."""
        decorated = OWNER + " \u27e2"
        for i in range(30):
            self.add_msg(decorated, "Bo Lind", f"filler {i}", convo=f"p{i}",
                         date=f"2019-04-{(i % 28) + 1:02d} 09:00:00 UTC")
        self.add_msg(decorated, "Ana Silva", "I said this", convo="mix",
                     date="2019-05-01 09:00:00 UTC")
        self.add_msg("Ana  Silva", decorated, "she said this", convo="mix",
                     date="2019-05-01 09:05:00 UTC")
        self.run_it()
        got = {x["body"]: x["direction"] for x in self.archived(self.ana)}
        self.assertEqual(got.get("I said this"), 1, "the owner's own message "
                         "was stored as inbound")
        self.assertEqual(got.get("she said this"), 0)

    def test_there_is_only_one_definition_of_a_name_key(self):
        """The gate. Both spellings of this comparison existed in one file and
        each cost a bug, so a bare case-fold reappearing here should fail the
        suite rather than wait to be noticed in the data. Comments and
        docstrings are blanked first — the paragraphs above talk about
        `.lower()` at length, and a grep that trips on its own explanation
        teaches people to delete the explanation."""
        # Blanked in place, NOT re-joined from tokens: joining puts a space
        # between every token, so `x.lower()` becomes `x . lower ( )` and a
        # search for ".lower()" finds nothing, and a bare .lower() put back
        # would walk straight past the gate.
        code = code_of(_MODULE_PATH)
        for banned in (".lower()", ".upper()", ".casefold()"):
            self.assertNotIn(banned, code,
                             f"{banned} is back — route it through _key()")
        self.assertEqual(
            code.count("normalize_name"), 1,
            "normalize_name should be reached in exactly one place (_key); "
            "a second call site is a second definition of 'the same person'")


class TestSomeoneYouHidIsNeverResurrected(SnapshotFixture):
    """A contact the owner hid or deleted is absent from the name map for a reason
    the owner chose. Creating them would bring them back, and attaching to them
    files messages onto a card the owner cannot open — a count with no timeline,
    which is the exact bug NC-070 was about."""

    def _hide(self, cid, column):
        self.crm.execute(f"UPDATE contacts SET {column} = 1 WHERE id = ?", (cid,))
        self.crm.commit()

    def _snapshot_knows_ana(self):
        self.snap.execute(
            "INSERT INTO contacts (url, name, position) "
            "VALUES ('u/ana', 'Ana Silva', 'Analyst')")
        self.snap.commit()

    def test_a_hidden_person_is_neither_created_nor_attached(self):
        self.add_owner_bulk(to="Bo Lind")
        self._snapshot_knows_ana()
        self._hide(self.ana, "excluded")
        self.add_msg("Ana Silva", OWNER, "to someone hidden", convo="h1",
                     date="2018-08-01 08:00:00 UTC")
        before = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        r = self.run_it()
        self.assertEqual(
            self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            before, "a hidden person was re-created as a second card")
        self.assertEqual(r["skipped"]["person you hid or deleted"], 1)
        self.assertEqual(r["skipped"]["person not in the CRM"], 0)
        self.assertEqual(len(self.archived(self.ana)), 0)

    def test_a_deleted_person_is_neither_created_nor_attached(self):
        self.add_owner_bulk(to="Bo Lind")
        self._snapshot_knows_ana()
        self._hide(self.ana, "deleted")
        self.add_msg("Ana Silva", OWNER, "to someone deleted", convo="h2",
                     date="2018-08-02 08:00:00 UTC")
        before = self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        r = self.run_it()
        self.assertEqual(
            self.crm.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            before)
        self.assertEqual(r["skipped"]["person you hid or deleted"], 1)
        self.assertEqual(len(self.archived(self.ana)), 0)

    def test_the_reason_is_reported_apart_from_the_others(self):
        """Three different reasons, three different answers for the owner. Merging
        them would turn "you hid this person" into "we don't know who this is"."""
        self.add_owner_bulk(to="Bo Lind")
        self._snapshot_knows_ana()
        self._hide(self.ana, "excluded")
        self.snap.execute(
            "INSERT INTO contacts (url, name) VALUES ('u/k', 'Kim Stranger')")
        self.snap.commit()
        self.add_msg("Ana Silva", OWNER, "hidden", convo="r1",
                     date="2018-09-01 08:00:00 UTC")
        self.add_msg("Kim Stranger", OWNER, "stranger", convo="r2",
                     date="2018-09-02 08:00:00 UTC")
        self.add_msg("Nobody AtAll", OWNER, "unknown", convo="r3",
                     date="2018-09-03 08:00:00 UTC")
        r = self.run_it(create_contacts=False)
        self.assertEqual(r["skipped"]["person you hid or deleted"], 1)
        self.assertEqual(r["skipped"]["person not in the CRM"], 1)
        self.assertEqual(r["skipped"]["name not recognised"], 1)
        # And the person the owner hid never turns up as someone to create.
        self.assertNotIn(M._key("Ana Silva"), r["creatable"])


class TestDisplayNamesAreCleanedBeforeBeingStored(unittest.TestCase):
    """A person this creates can have a left-to-right mark sitting where a
    surname would be — the scraper captures it from the LinkedIn display name.
    A card whose name holds characters nobody can see is one nobody can search
    for or tidy up."""

    def test_invisible_marks_are_dropped(self):
        self.assertEqual(M._display_name("Abc \u200e"), "Abc")
        self.assertEqual(M._display_name("Ann\u200bmarie"), "Annmarie")

    def test_double_spaces_collapse(self):
        self.assertEqual(M._display_name("Ana  Silva"), "Ana Silva")

    def test_it_is_not_a_lookup_key(self):
        """Unlike _key it keeps case and accents, because this is the name that
        gets shown on a card."""
        self.assertEqual(M._display_name("Núñez  Ada"), "Núñez Ada")
        self.assertNotEqual(M._display_name("Ada Lovelace"), M._key("Ada Lovelace"))
