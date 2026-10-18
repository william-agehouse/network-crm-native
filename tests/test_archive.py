"""archive.db itself: schema, idempotent insert, watermarks, FTS, conversation chunking, and the engagement roll-up over it."""

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, arc, db,
)


class TestArchive(unittest.TestCase):
    """archive.py: schema, idempotent insert, watermark, get_messages, FTS."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="arc_test_")
        self.path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.path)
        self.conn = arc.get_archive_db(self.path)

    def tearDown(self):
        self.conn.close()

    def _insert(self, contact_id=1, channel="whatsapp", direction=0,
                ts="2026-05-01 10:00:00", body="hello", source_key="wa_1"):
        return arc.insert_message(self.conn, contact_id=contact_id,
                                  channel=channel, direction=direction,
                                  ts=ts, body=body, source_key=source_key)

    def test_init_creates_tables(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("messages", tables)
        self.assertIn("chunks", tables)

    def test_group_name_migration_on_pre_group_archive(self):
        # An archive created before group support gains the column on init.
        legacy = os.path.join(self.dir, "legacy.db")
        c = sqlite3.connect(legacy)
        c.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL,
                channel TEXT NOT NULL, direction INTEGER NOT NULL DEFAULT 0,
                ts TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '', source_key TEXT NOT NULL,
                UNIQUE(channel, source_key));
        """)
        c.close()
        arc.init_archive(legacy)
        c = arc.get_archive_db(legacy)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        c.close()
        self.assertIn("group_name", cols)
        # NC-110's column arrives by the same route. The column's index is
        # created after the ALTER, never in _DDL: on an archive whose `messages`
        # table already exists, CREATE TABLE IF NOT EXISTS is a no-op, so an
        # index in the DDL would name a column that is not there yet and
        # executescript would raise.
        self.assertIn("group_key", cols)

    def test_the_group_key_index_exists_after_a_migration(self):
        """Threads are read by key, so the index has to survive the upgrade path
        and not only the fresh-install path."""
        legacy = os.path.join(self.dir, "legacy2.db")
        c = sqlite3.connect(legacy)
        c.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL,
                channel TEXT NOT NULL, direction INTEGER NOT NULL DEFAULT 0,
                ts TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '', source_key TEXT NOT NULL,
                UNIQUE(channel, source_key));
        """)
        c.close()
        arc.init_archive(legacy)
        arc.init_archive(legacy)          # and it is idempotent
        c = arc.get_archive_db(legacy)
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        c.close()
        self.assertIn("idx_msg_group", idx)

    def test_a_group_key_is_stored_and_read_back(self):
        arc.insert_message(self.conn, contact_id=1, channel="imessage",
                           direction=0, ts="2026-05-01 10:00:00",
                           body="hi", source_key="k1",
                           group_name="Group of 3", group_key="imessage:7")
        row = self.conn.execute(
            "SELECT group_name, group_key FROM messages").fetchone()
        self.assertEqual(row["group_name"], "Group of 3")
        self.assertEqual(row["group_key"], "imessage:7")

    def test_a_row_stored_before_the_column_gets_its_key_on_re_import(self):
        """The entire NC-110 migration path for every existing message: no
        backfill script, the importers fill the keys in as they next run."""
        arc.insert_message(self.conn, contact_id=1, channel="imessage",
                           direction=0, ts="2026-05-01 10:00:00",
                           body="hi", source_key="k1", group_name="Group of 3")
        self.assertEqual(self.conn.execute(
            "SELECT group_key FROM messages").fetchone()["group_key"], "")
        again = arc.insert_message(
            self.conn, contact_id=1, channel="imessage", direction=0,
            ts="2026-05-01 10:00:00", body="hi", source_key="k1",
            group_name="Group of 3", group_key="imessage:7")
        self.assertFalse(again, "the row should not have been re-inserted")
        self.assertEqual(self.conn.execute(
            "SELECT group_key FROM messages").fetchone()["group_key"],
            "imessage:7")

    def test_an_existing_key_is_never_overwritten(self):
        """Only '' is backfilled. A second importer run with a different id —
        a rebuilt chat.db renumbering its chats, say — must not silently move a
        message into another conversation."""
        arc.insert_message(self.conn, contact_id=1, channel="imessage",
                           direction=0, ts="2026-05-01 10:00:00",
                           body="hi", source_key="k1",
                           group_name="Group of 3", group_key="imessage:7")
        arc.insert_message(self.conn, contact_id=1, channel="imessage",
                           direction=0, ts="2026-05-01 10:00:00",
                           body="hi", source_key="k1",
                           group_name="Group of 3", group_key="imessage:999")
        self.assertEqual(self.conn.execute(
            "SELECT group_key FROM messages").fetchone()["group_key"],
            "imessage:7")

    def test_insert_returns_true_for_new_message(self):
        self.assertTrue(self._insert())

    def test_insert_is_idempotent(self):
        self._insert(source_key="wa_1")
        self.assertFalse(self._insert(source_key="wa_1"))  # duplicate → False
        count = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_key_different_channel_is_not_duplicate(self):
        self._insert(channel="whatsapp", source_key="99")
        self.assertTrue(self._insert(channel="imessage", source_key="99"))
        count = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 2)

    def test_watermark_empty_when_no_messages(self):
        self.assertEqual(arc.channel_watermark(self.conn, "whatsapp"), "")

    def test_watermark_returns_latest_ts(self):
        self._insert(ts="2026-03-01 00:00:00", source_key="wa_1")
        self._insert(ts="2026-05-01 00:00:00", source_key="wa_2")
        self._insert(ts="2026-01-01 00:00:00", source_key="wa_3")
        self.assertEqual(arc.channel_watermark(self.conn, "whatsapp"),
                         "2026-05-01 00:00:00")

    def test_watermark_is_per_channel(self):
        self._insert(channel="whatsapp", ts="2026-05-01 00:00:00", source_key="w1")
        self._insert(channel="email",    ts="2025-01-01 00:00:00", source_key="e1")
        self.assertEqual(arc.channel_watermark(self.conn, "email"),
                         "2025-01-01 00:00:00")

    def test_get_messages_newest_first(self):
        self._insert(ts="2026-01-01 00:00:00", source_key="wa_1", body="old")
        self._insert(ts="2026-05-01 00:00:00", source_key="wa_2", body="new")
        msgs = arc.get_messages(self.conn, contact_id=1)
        self.assertEqual(msgs[0]["body"], "new")
        self.assertEqual(msgs[1]["body"], "old")

    def test_get_messages_filters_by_channel(self):
        self._insert(channel="whatsapp", source_key="w1", body="wa msg")
        self._insert(channel="email",    source_key="e1", body="email msg")
        wa_msgs = arc.get_messages(self.conn, contact_id=1, channel="whatsapp")
        self.assertEqual(len(wa_msgs), 1)
        self.assertEqual(wa_msgs[0]["body"], "wa msg")

    def test_get_messages_paginates_with_before(self):
        for i in range(5):
            self._insert(ts=f"2026-0{i+1}-01 00:00:00", source_key=f"wa_{i}",
                         body=f"msg {i}")
        # get messages before April
        msgs = arc.get_messages(self.conn, contact_id=1, before="2026-04-01 00:00:00")
        self.assertTrue(all(m["ts"] < "2026-04-01" for m in msgs))

    def test_fts_search_finds_keyword(self):
        self._insert(body="Let's meet for coffee next week", source_key="wa_1")
        self._insert(body="Great seeing you at the conference", source_key="wa_2")
        self.conn.commit()
        results = arc.fts_search(self.conn, "coffee")
        self.assertEqual(len(results), 1)
        self.assertIn("coffee", results[0]["body"])

    def test_fts_search_returns_empty_for_no_match(self):
        self._insert(body="Hello there", source_key="wa_1")
        self.conn.commit()
        results = arc.fts_search(self.conn, "quantum physics")
        self.assertEqual(results, [])

    def test_contact_archive_counts(self):
        self._insert(channel="whatsapp", source_key="w1")
        self._insert(channel="whatsapp", source_key="w2")
        self._insert(channel="email",    source_key="e1")
        counts = arc.contact_archive_counts(self.conn, contact_id=1)
        self.assertEqual(counts["whatsapp"], 2)
        self.assertEqual(counts["email"], 1)


class TestReadingAWholeConversation(unittest.TestCase):
    """NC-106. Group threads show on everyone's page.

    Each group message is attributed to whoever said it, which is right — but it
    means a page shows only that person's lines from a thread, so nobody's page
    shows the conversation. Reading by group_key is what fixes that, and no
    participant table is needed: took part in == said something in it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="thread_read_")
        self.path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.path)
        self.conn = arc.get_archive_db(self.path)
        self.n = 0
        # A three-way thread: Ada and Bo speak, and so does the owner.
        self.msg(1, "2026-05-01 10:00:00", "ada in the group", key="imessage:1")
        self.msg(2, "2026-05-01 10:01:00", "bo in the group", key="imessage:1")
        self.msg(arc.NO_PERSON, "2026-05-01 10:02:00", "my reply",
                 key="imessage:1", direction=1)
        # Ada also has a one-to-one message, and Bo is in a second thread.
        self.msg(1, "2026-05-01 11:00:00", "ada dm")
        self.msg(2, "2026-05-01 12:00:00", "bo elsewhere", key="imessage:2")

    def tearDown(self):
        self.conn.close()

    def msg(self, cid, ts, body, key="", direction=0):
        self.n += 1
        arc.insert_message(self.conn, contact_id=cid, channel="imessage",
                           direction=direction, ts=ts, body=body,
                           source_key=f"k{self.n}",
                           group_name="Project chat" if key else "",
                           group_key=key)
        self.conn.commit()

    def test_without_threads_a_page_sees_only_their_own_lines(self):
        """The default, and every existing caller depends on it — counts,
        engagement, style profiling all mean "this person's messages"."""
        bodies = {m["body"] for m in arc.get_messages(self.conn, 1)}
        self.assertEqual(bodies, {"ada in the group", "ada dm"})

    def test_with_threads_a_page_sees_the_conversation(self):
        bodies = {m["body"] for m in
                  arc.get_messages(self.conn, 1, include_threads=True)}
        self.assertEqual(bodies, {"ada in the group", "bo in the group",
                                  "my reply", "ada dm"})

    def test_it_does_not_reach_into_threads_they_were_not_in(self):
        bodies = {m["body"] for m in
                  arc.get_messages(self.conn, 1, include_threads=True)}
        self.assertNotIn("bo elsewhere", bodies,
                         "a conversation she was never in appeared on her page")

    def test_thread_keys_are_the_conversations_they_spoke_in(self):
        self.assertEqual(arc.thread_keys(self.conn, 1), ["imessage:1"])
        self.assertEqual(sorted(arc.thread_keys(self.conn, 2)),
                         ["imessage:1", "imessage:2"])

    def test_somebody_with_no_group_history_reads_exactly_as_before(self):
        solo = 9
        self.msg(solo, "2026-05-02 10:00:00", "just a dm")
        self.assertEqual(arc.thread_keys(self.conn, solo), [])
        self.assertEqual(
            [m["body"] for m in arc.get_messages(self.conn, solo,
                                                 include_threads=True)],
            ["just a dm"])

    def test_the_channel_and_before_filters_still_apply(self):
        """Widening must not quietly drop the other filters — a channel tab
        showing another channel's messages is worse than showing none."""
        self.msg(1, "2026-05-01 10:30:00", "wa group line", key="whatsapp:1")
        rows = arc.get_messages(self.conn, 1, channel="imessage",
                                include_threads=True)
        self.assertEqual({m["channel"] for m in rows}, {"imessage"})
        rows = arc.get_messages(self.conn, 1, before="2026-05-01 10:01:00",
                                include_threads=True)
        self.assertEqual({m["body"] for m in rows}, {"ada in the group"})


class TestChunking(unittest.TestCase):
    """archive.py: build_chunks boundary rules and content_hash stability."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chunk_test_")
        self.path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.path)
        self.conn = arc.get_archive_db(self.path)

    def tearDown(self):
        self.conn.close()

    def _insert(self, contact_id=1, channel="whatsapp", direction=0,
                ts="2026-05-01 10:00:00", body="hello", source_key="k1"):
        arc.insert_message(self.conn, contact_id=contact_id, channel=channel,
                           direction=direction, ts=ts, body=body, source_key=source_key)

    def test_single_run_of_messages_becomes_one_chunk(self):
        self._insert(ts="2026-05-01 10:00:00", body="hi", source_key="1")
        self._insert(ts="2026-05-01 10:05:00", body="hey", source_key="2", direction=1)
        self.conn.commit()
        n = arc.build_chunks(self.conn, contact_id=1)
        self.assertEqual(n, 1)
        rows = self.conn.execute("SELECT * FROM chunks").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("Them: hi", rows[0]["text"])
        self.assertIn("Me: hey", rows[0]["text"])

    def test_time_gap_splits_into_two_chunks(self):
        self._insert(ts="2026-05-01 10:00:00", body="morning", source_key="1")
        self._insert(ts="2026-05-01 23:00:00", body="evening", source_key="2")  # >4h gap
        self.conn.commit()
        n = arc.build_chunks(self.conn, contact_id=1)
        self.assertEqual(n, 2)

    def test_message_count_cap_splits_chunk(self):
        for i in range(25):
            self._insert(ts=f"2026-05-01 10:{i:02d}:00", body=f"msg {i}", source_key=str(i))
        self.conn.commit()
        n = arc.build_chunks(self.conn, contact_id=1)
        self.assertEqual(n, 2)  # 20 + 5

    def test_channel_change_splits_chunk(self):
        self._insert(channel="whatsapp", ts="2026-05-01 10:00:00", body="wa", source_key="1")
        self._insert(channel="email", ts="2026-05-01 10:01:00", body="em", source_key="2")
        self.conn.commit()
        n = arc.build_chunks(self.conn, contact_id=1)
        self.assertEqual(n, 2)

    def test_media_only_messages_produce_no_chunk(self):
        self._insert(body="", source_key="1")
        self.conn.commit()
        n = arc.build_chunks(self.conn, contact_id=1)
        self.assertEqual(n, 0)

    def test_group_messages_chunk_separately_and_carry_label(self):
        # 1:1 and group-chat messages minutes apart: the group is its own
        # chunk, and its lines carry the group label for search/AI clarity.
        self._insert(ts="2026-05-01 10:00:00", body="dm hello", source_key="1")
        arc.insert_message(self.conn, contact_id=1, channel="whatsapp",
                           direction=0, ts="2026-05-01 10:01:00",
                           body="group news", source_key="2",
                           group_name="Project chat")
        self.conn.commit()
        n = arc.build_chunks(self.conn, contact_id=1)
        self.assertEqual(n, 2)
        texts = [r["text"] for r in self.conn.execute("SELECT text FROM chunks")]
        self.assertTrue(any("Them: dm hello" in t for t in texts))
        self.assertTrue(any("Them (in Project chat): group news" in t for t in texts))

    def test_two_conversations_sharing_a_label_are_two_chunk_streams(self):
        """NC-110. Chunk boundaries used to break on the group LABEL, so every
        unnamed three-person chat — all of them called "Group of 3" — was one
        continuous conversation as far as search was concerned. In a large
        archive that welded many messages from many
        people into one stream."""
        for i, (key, body) in enumerate((
                ("imessage:1", "one chat"), ("imessage:2", "another chat"))):
            arc.insert_message(self.conn, contact_id=1, channel="imessage",
                               direction=0, ts=f"2026-05-01 10:0{i}:00",
                               body=body, source_key=f"g{i}",
                               group_name="Group of 3", group_key=key)
        self.conn.commit()
        n = arc.build_chunks(self.conn, contact_id=1)
        self.assertEqual(n, 2, "two separate chats were chunked as one "
                               "conversation because they share a label")
        texts = [r["text"] for r in self.conn.execute("SELECT text FROM chunks")]
        self.assertTrue(any("one chat" in t and "another chat" not in t
                            for t in texts))

    def test_messages_with_no_key_yet_still_chunk_by_label(self):
        """While a backfill is outstanding every group row reads group_key=''.
        Breaking on the key alone would then weld group talk to the one-to-one
        stream, which is worse than the bug — so the break is on the pair."""
        self._insert(ts="2026-05-01 10:00:00", body="dm hello", source_key="1")
        arc.insert_message(self.conn, contact_id=1, channel="whatsapp",
                           direction=0, ts="2026-05-01 10:01:00",
                           body="group news", source_key="2",
                           group_name="Project chat")   # no group_key
        self.conn.commit()
        self.assertEqual(arc.build_chunks(self.conn, contact_id=1), 2)

    def test_group_label_backfills_and_changes_chunk_hash(self):
        # A message archived before group support exists with group_name='';
        # re-importing it with a label must backfill the label AND change the
        # chunk's content_hash so the embedding cache re-encodes the new text.
        self._insert(ts="2026-05-01 10:00:00", body="old group msg", source_key="g1")
        self.conn.commit()
        arc.build_chunks(self.conn, contact_id=1)
        hash_before = self.conn.execute("SELECT content_hash FROM chunks").fetchone()[0]

        again = arc.insert_message(self.conn, contact_id=1, channel="whatsapp",
                                   direction=0, ts="2026-05-01 10:00:00",
                                   body="old group msg", source_key="g1",
                                   group_name="Planning group")
        self.assertFalse(again)  # not a new row…
        self.conn.commit()
        row = self.conn.execute("SELECT group_name FROM messages").fetchone()
        self.assertEqual(row["group_name"], "Planning group")  # …but label landed

        arc.build_chunks(self.conn, contact_id=1)
        hash_after = self.conn.execute("SELECT content_hash FROM chunks").fetchone()[0]
        self.assertNotEqual(hash_before, hash_after)

    def test_rebuild_is_idempotent_on_content_hash(self):
        self._insert(ts="2026-05-01 10:00:00", body="hi", source_key="1")
        self.conn.commit()
        arc.build_chunks(self.conn, contact_id=1)
        hash1 = self.conn.execute("SELECT content_hash FROM chunks").fetchone()[0]
        arc.build_chunks(self.conn, contact_id=1)  # rebuild, same data
        rows = self.conn.execute("SELECT content_hash FROM chunks").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content_hash"], hash1)

    def test_build_chunks_scoped_to_one_contact_leaves_others_untouched(self):
        self._insert(contact_id=1, ts="2026-05-01 10:00:00", body="hi", source_key="1")
        self._insert(contact_id=2, ts="2026-05-01 10:00:00", body="hello", source_key="2")
        self.conn.commit()
        arc.build_chunks(self.conn, contact_id=None)
        before = self.conn.execute(
            "SELECT id FROM chunks WHERE contact_id = 2").fetchone()["id"]
        arc.build_chunks(self.conn, contact_id=1)  # only rebuild contact 1
        after = self.conn.execute(
            "SELECT id FROM chunks WHERE contact_id = 2").fetchone()["id"]
        self.assertEqual(before, after)  # contact 2's chunk row untouched


class TestEngagement(Base):
    """crm/engagement.py — archive → per-contact interaction features."""

    def setUp(self):
        super().setUp()
        import engagement as eng
        self.eng = eng
        self.arc_dir = tempfile.mkdtemp(prefix="crm_eng_")
        self._orig_arc = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(self.arc_dir, "archive.db")
        arc.init_archive(arc.ARCHIVE_DB_PATH)

    def tearDown(self):
        arc.ARCHIVE_DB_PATH = self._orig_arc
        super().tearDown()

    def test_refresh_aggregates_and_missing_archive_is_quiet(self):
        cid = db.create_contact(self.conn, "Eng Emma")
        self.conn.commit()
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        recent = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        arc.insert_message(ac, contact_id=cid, channel="whatsapp", direction=1,
                           ts="2024-03-01 10:00:00", body="hi", source_key="a")
        arc.insert_message(ac, contact_id=cid, channel="whatsapp", direction=0,
                           ts="2024-03-01 12:00:00", body="hello", source_key="b")
        arc.insert_message(ac, contact_id=cid, channel="whatsapp", direction=0,
                           ts=recent, body="lunch?", source_key="c")
        ac.commit()
        ac.close()

        n = self.eng.refresh(self.conn)
        self.assertEqual(n, 1)
        row = db.get_contact(self.conn, cid)
        self.assertEqual(row["eng_active_days"], 2)   # two distinct days
        self.assertEqual(row["eng_sent"], 1)
        self.assertEqual(row["eng_recv"], 2)
        self.assertEqual(row["eng_recent90"], 1)
        self.assertEqual(row["eng_first"], "2024-03-01 10:00:00")
        self.assertEqual(row["eng_last"], recent)

        self.assertEqual(self.eng.refresh(self.conn, archive_path="/nope/missing.db"), 0)

    def test_a_refresh_with_nothing_new_writes_no_one(self):
        """NC-214. Every refresh used to rewrite every person with a history, and
        stamp them, so the whole table changed on every import for nothing."""
        cid = db.create_contact(self.conn, "Eng Still")
        other = db.create_contact(self.conn, "Eng Moved")
        self.conn.commit()
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        for who, key in ((cid, "s1"), (other, "m1")):
            arc.insert_message(ac, contact_id=who, channel="whatsapp", direction=0,
                               ts="2024-03-01 10:00:00", body="hi", source_key=key)
        ac.commit()
        self.assertEqual(self.eng.refresh(self.conn), 2)
        self.conn.execute("UPDATE contacts SET eng_updated_at = 'untouched'")
        self.conn.commit()
        self.assertEqual(self.eng.refresh(self.conn), 0)
        arc.insert_message(ac, contact_id=other, channel="whatsapp", direction=1,
                           ts="2024-03-02 10:00:00", body="again", source_key="m2")
        ac.commit()
        ac.close()
        self.assertEqual(self.eng.refresh(self.conn), 1, "only the person with a new message")
        stamps = {r["id"]: r["eng_updated_at"] for r in
                  self.conn.execute("SELECT id, eng_updated_at FROM contacts WHERE id IN (?, ?)", (cid, other))}
        self.assertEqual(stamps[cid], "untouched")
        self.assertNotEqual(stamps[other], "untouched")
        self.assertEqual(db.get_contact(self.conn, other)["eng_active_days"], 2)

    def test_a_conversation_owned_row_is_not_scored_as_a_person(self):
        """NC-111. A message the owner sent to a group belongs to the conversation, so
        it carries arc.NO_PERSON. Left in the roll-up it became a phantom entry
        keyed 0 whose write-back updated no row at all — a silent no-op, which
        is exactly the kind of failure nothing reports."""
        cid = db.create_contact(self.conn, "Eng Emma")
        self.conn.commit()
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        arc.insert_message(ac, contact_id=cid, channel="imessage", direction=0,
                           ts="2024-03-01 10:00:00", body="hi", source_key="a",
                           group_name="Project chat", group_key="imessage:1")
        arc.insert_message(ac, contact_id=arc.NO_PERSON, channel="imessage",
                           direction=1, ts="2024-03-01 11:00:00",
                           body="my reply", source_key="b",
                           group_name="Project chat", group_key="imessage:1")
        ac.commit()
        features = self.eng.compute(ac)
        ac.close()
        self.assertIn(cid, features)
        self.assertNotIn(arc.NO_PERSON, features,
                         "a conversation was scored as if it were a person")
        self.assertEqual(self.eng.refresh(self.conn), 1)
