"""Importers writing message bodies into archive.db — the archive_messages half of each source, including the iMessage attributedBody decoders."""

import mailbox
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    MAY2026, _apple_ns, _coredata_secs, arc, db, ei, im, meta, tg, wa,
)


class TestWhatsappArchive(unittest.TestCase):
    """WhatsApp → archive.db pipeline: text read + idempotent write."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wa_arc_test_")
        # CRM db
        self.crm_path = os.path.join(self.dir, "crm.db")
        db.init_db(self.crm_path)
        self.crm = db.get_db(self.crm_path)
        # Archive db
        self.arc_path = os.path.join(self.dir, "archive.db")
        # Synthetic WA db with ZTEXT + ZISFROMME
        self.wa_path = os.path.join(self.dir, "ChatStorage.sqlite")
        c = sqlite3.connect(self.wa_path)
        c.executescript("""
        CREATE TABLE ZWACHATSESSION(
            Z_PK INTEGER PRIMARY KEY, ZSESSIONTYPE INTEGER,
            ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
        CREATE TABLE ZWAMESSAGE(
            Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
            ZMESSAGEDATE REAL, ZTEXT TEXT, ZISFROMME INTEGER);
        """)
        secs = _coredata_secs(MAY2026)
        c.execute("INSERT INTO ZWACHATSESSION VALUES (1,0,'46701@s.whatsapp.net','Anna')")
        c.executemany("INSERT INTO ZWAMESSAGE VALUES (?,1,?,?,?)", [
            (1, secs - 100, "Hey, how are you?", 0),
            (2, secs - 50,  "Doing well, thanks!", 1),
            (3, secs,       "Let's catch up soon", 0),
        ])
        c.commit(); c.close()

    def tearDown(self):
        self.crm.close()

    def test_archive_messages_stores_text(self):
        cid = db.create_contact(self.crm, "Anna", source="whatsapp")
        self.crm.commit()
        jid_to_cid = {"46701@s.whatsapp.net": cid}
        result = wa.archive_messages(self.wa_path, jid_to_cid,
                                     archive_path=self.arc_path)
        self.assertEqual(result["new"], 3)
        self.assertEqual(result["skipped"], 0)
        arc_conn = arc.get_archive_db(self.arc_path)
        msgs = arc.get_messages(arc_conn, cid)
        arc_conn.close()
        self.assertEqual(len(msgs), 3)
        bodies = {m["body"] for m in msgs}
        self.assertIn("Hey, how are you?", bodies)

    def test_archive_messages_is_idempotent(self):
        cid = db.create_contact(self.crm, "Anna", source="whatsapp")
        self.crm.commit()
        jid_to_cid = {"46701@s.whatsapp.net": cid}
        wa.archive_messages(self.wa_path, jid_to_cid, archive_path=self.arc_path)
        result2 = wa.archive_messages(self.wa_path, jid_to_cid,
                                      archive_path=self.arc_path)
        self.assertEqual(result2["new"], 0)
        self.assertEqual(result2["skipped"], 3)

    def test_archive_messages_respects_direction(self):
        cid = db.create_contact(self.crm, "Anna", source="whatsapp")
        self.crm.commit()
        wa.archive_messages(self.wa_path, {"46701@s.whatsapp.net": cid},
                            archive_path=self.arc_path)
        arc_conn = arc.get_archive_db(self.arc_path)
        sent = [m for m in arc.get_messages(arc_conn, cid) if m["direction"] == 1]
        recv = [m for m in arc.get_messages(arc_conn, cid) if m["direction"] == 0]
        arc_conn.close()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(recv), 2)

    def test_archive_skips_empty_jid_to_cid(self):
        result = wa.archive_messages(self.wa_path, {},
                                     archive_path=self.arc_path)
        self.assertEqual(result["new"], 0)


class TestWhatsappGroupIdentity(unittest.TestCase):
    """NC-110 on the WhatsApp side. A group with no subject gets the label
    "WhatsApp group", which every unnamed group shares — in a large archive
    that one row would hold messages from many people across years."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wa_grp_test_")
        self.crm_path = os.path.join(self.dir, "crm.db")
        db.init_db(self.crm_path)
        self.crm = db.get_db(self.crm_path)
        self.cid = db.create_contact(self.crm, "Anna", source="whatsapp")
        self.crm.commit()
        self.arc_path = os.path.join(self.dir, "archive.db")
        self.wa_path = os.path.join(self.dir, "ChatStorage.sqlite")
        c = sqlite3.connect(self.wa_path)
        c.executescript("""
        CREATE TABLE ZWACHATSESSION(
            Z_PK INTEGER PRIMARY KEY, ZSESSIONTYPE INTEGER,
            ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
        CREATE TABLE ZWAGROUPMEMBER(
            Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
            ZMEMBERJID TEXT, ZISACTIVE INTEGER);
        CREATE TABLE ZWAMESSAGE(
            Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
            ZMESSAGEDATE REAL, ZTEXT TEXT, ZISFROMME INTEGER,
            ZGROUPMEMBER INTEGER);
        """)
        secs = _coredata_secs(MAY2026)
        # Two unnamed groups — ZPARTNERNAME NULL, so both fall back to the same
        # label — with the same person speaking in each.
        jid = "46701@s.whatsapp.net"
        for pk in (10, 20):
            c.execute("INSERT INTO ZWACHATSESSION VALUES (?,1,?,NULL)",
                      (pk, f"g{pk}@g.us"))
            c.execute("INSERT INTO ZWAGROUPMEMBER VALUES (?,?,?,1)",
                      (pk, pk, jid))
            c.execute("INSERT INTO ZWAGROUPMEMBER VALUES (?,?,?,1)",
                      (pk + 1, pk, f"other{pk}@s.whatsapp.net"))
            c.execute("INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,0,?)",
                      (pk, pk, secs, f"said in group {pk}", pk))
        c.commit(); c.close()
        self.jid_to_cid = {jid: self.cid}

    def tearDown(self):
        self.crm.close()

    def _import(self):
        # match_group_senders reaches for db.get_db() with no path.
        orig = db.DB_PATH
        db.DB_PATH = self.crm_path
        try:
            return wa.archive_group_messages(
                self.wa_path, self.jid_to_cid, max_members=8,
                archive_path=self.arc_path)
        finally:
            db.DB_PATH = orig

    def test_two_unnamed_groups_are_two_conversations(self):
        self._import()
        arc_conn = arc.get_archive_db(self.arc_path)
        rows = arc_conn.execute(
            "SELECT group_name, group_key FROM messages WHERE group_name != ''"
        ).fetchall()
        arc_conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["group_name"] for r in rows}, {"WhatsApp group"},
                         "both unnamed groups should share the fallback label")
        self.assertEqual({r["group_key"] for r in rows},
                         {"whatsapp:10", "whatsapp:20"})

    def test_a_named_group_keeps_its_label_and_gains_a_key(self):
        c = sqlite3.connect(self.wa_path)
        c.execute("UPDATE ZWACHATSESSION SET ZPARTNERNAME='Project chat' WHERE Z_PK=10")
        c.commit(); c.close()
        self._import()
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute(
            "SELECT group_name, group_key FROM messages "
            "WHERE group_key = 'whatsapp:10'").fetchone()
        arc_conn.close()
        self.assertEqual(row["group_name"], "Project chat")

    def test_an_import_saves_the_owners_group_messages_and_chunks_them(self):
        """NC-199. The function above was written but never called, so no
        WhatsApp message the owner sent to a group was saved.
        archive_and_chunk is what main() runs."""
        c = sqlite3.connect(self.wa_path)
        c.execute("UPDATE ZWACHATSESSION SET ZPARTNERNAME='Project chat' WHERE Z_PK=10")
        c.execute("INSERT INTO ZWAMESSAGE VALUES (?,10,?,?,1,NULL)",
                  (99, _coredata_secs(MAY2026) + 10, "my reply"))
        c.commit(); c.close()
        orig = db.DB_PATH
        db.DB_PATH = self.crm_path
        try:
            out = wa.archive_and_chunk(self.wa_path, self.jid_to_cid, max_members=8,
                                       archive_path=self.arc_path)
        finally:
            db.DB_PATH = orig
        self.assertEqual(out["own"]["new"], 1)
        self.assertEqual(out["new"], 3, "Anna's line in each group, plus the owner's reply")
        arc_conn = arc.get_archive_db(self.arc_path)
        chunk = arc_conn.execute("SELECT * FROM chunks WHERE contact_id = ?",
                                 (arc.NO_PERSON,)).fetchone()
        arc_conn.close()
        self.assertIsNotNone(chunk, "the owner's message was stored and never chunked, "
                                    "so search by meaning could not find it")
        self.assertIn("my reply", chunk["text"])
        self.assertEqual(chunk["group_key"], "whatsapp:10")

    def test_main_runs_it(self):
        import inspect
        self.assertIn("archive_and_chunk(", inspect.getsource(wa.main))

    def test_the_owners_own_group_messages_are_stored_against_the_conversation(self):
        """NC-111, WhatsApp side. Its cause was its own: the group importer
        filters ZGROUPMEMBER IS NOT NULL, and a message the owner sent points at
        nobody, so it never matched — and the insert hardcoded direction=0 for
        whatever was left."""
        c = sqlite3.connect(self.wa_path)
        c.execute("UPDATE ZWACHATSESSION SET ZPARTNERNAME='Project chat' WHERE Z_PK=10")
        c.execute("INSERT INTO ZWAMESSAGE VALUES (?,10,?,?,1,NULL)",
                  (99, _coredata_secs(MAY2026) + 10, "my reply"))
        c.commit(); c.close()
        self._import()
        out = wa.archive_own_group_messages(self.wa_path,
                                            archive_path=self.arc_path)
        self.assertEqual(out["new"], 1)
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute(
            "SELECT * FROM messages WHERE direction = 1").fetchone()
        arc_conn.close()
        self.assertEqual(row["body"], "my reply")
        self.assertEqual(row["contact_id"], arc.NO_PERSON)
        self.assertEqual(row["group_key"], "whatsapp:10")
        self.assertEqual(row["group_name"], "Project chat")
        self.assertEqual(row["source_key"], "wag_99")

    def test_it_only_completes_conversations_the_archive_already_has(self):
        c = sqlite3.connect(self.wa_path)
        # A group the archive knows nothing about: nobody the owner knows ever spoke.
        c.execute("INSERT INTO ZWACHATSESSION VALUES (30,1,'g30@g.us',NULL)")
        c.execute("INSERT INTO ZWAGROUPMEMBER VALUES (30,30,'nobody@s.whatsapp.net',1)")
        c.execute("INSERT INTO ZWAMESSAGE VALUES (?,30,?,?,1,NULL)",
                  (98, _coredata_secs(MAY2026) + 5, "mine among strangers"))
        c.execute("INSERT INTO ZWAMESSAGE VALUES (?,10,?,?,1,NULL)",
                  (99, _coredata_secs(MAY2026) + 10, "mine among friends"))
        c.commit(); c.close()
        self._import()
        wa.archive_own_group_messages(self.wa_path, archive_path=self.arc_path)
        arc_conn = arc.get_archive_db(self.arc_path)
        bodies = {r["body"] for r in arc_conn.execute(
            "SELECT body FROM messages WHERE direction = 1")}
        arc_conn.close()
        self.assertEqual(bodies, {"mine among friends"})

    def test_running_it_twice_stores_nothing_new(self):
        c = sqlite3.connect(self.wa_path)
        c.execute("INSERT INTO ZWAMESSAGE VALUES (?,10,?,?,1,NULL)",
                  (99, _coredata_secs(MAY2026) + 10, "my reply"))
        c.commit(); c.close()
        self._import()
        wa.archive_own_group_messages(self.wa_path, archive_path=self.arc_path)
        again = wa.archive_own_group_messages(self.wa_path,
                                              archive_path=self.arc_path)
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["skipped"], 1)

    def test_the_backfill_recovers_keys_from_the_wag_prefix(self):
        """Group source keys are stored as "wag_<Z_PK>", and that prefix is what
        makes an already-archived row traceable back to its chat session."""
        self._import()
        arc_conn = arc.get_archive_db(self.arc_path)
        arc_conn.execute("UPDATE messages SET group_key = ''")   # as if pre-NC-110
        arc_conn.commit(); arc_conn.close()
        out = wa.backfill_group_keys(self.wa_path, archive_path=self.arc_path)
        self.assertEqual(out["filled"], 2)
        self.assertEqual(out["unknown"], 0)
        arc_conn = arc.get_archive_db(self.arc_path)
        keys = {r["group_key"] for r in arc_conn.execute(
            "SELECT group_key FROM messages WHERE group_name != ''")}
        arc_conn.close()
        self.assertEqual(keys, {"whatsapp:10", "whatsapp:20"})

    def test_re_importing_does_not_rewrite_a_key_it_already_has(self):
        self._import()
        second = self._import()
        self.assertEqual(second["new"], 0)
        arc_conn = arc.get_archive_db(self.arc_path)
        keys = {r["group_key"] for r in arc_conn.execute(
            "SELECT group_key FROM messages WHERE group_name != ''")}
        arc_conn.close()
        self.assertEqual(keys, {"whatsapp:10", "whatsapp:20"})


class TestIMessageArchive(unittest.TestCase):
    """imessage_import.archive_messages: attributedBody decoder + idempotent write."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="im_arc_test_")
        self.arc_path = os.path.join(self.dir, "archive.db")
        self.chat_path = os.path.join(self.dir, "chat.db")
        c = sqlite3.connect(self.chat_path)
        c.executescript("""
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INTEGER,
                             is_from_me INTEGER, text TEXT,
                             attributedBody BLOB, associated_message_type INTEGER,
                             handle_id INTEGER);
        CREATE TABLE chat(ROWID INTEGER PRIMARY KEY);
        CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);
        CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
        """)
        ns = _apple_ns(MAY2026)
        c.execute("INSERT INTO handle VALUES(1, '+46701740611')")
        c.execute("INSERT INTO chat VALUES(1)")
        c.execute("INSERT INTO chat_handle_join VALUES(1,1)")
        # message.handle_id = sender (matches real chat.db); 0 for my own outbound.
        c.executemany("INSERT INTO message VALUES(?,?,?,?,?,0,?)", [
            (1, ns,           0, "Hello there", None, 1),
            (2, ns + 60*10**9, 1, None, b"",        0),  # empty attributedBody → skipped
            (3, ns + 120*10**9, 0, None, None,      1),  # null text + null blob → skipped
        ])
        c.executemany("INSERT INTO chat_message_join VALUES(1,?)", [(1,),(2,),(3,)])
        c.commit(); c.close()

    def test_stores_text_messages(self):
        handle_to_cid = {"+46701740611": 42}
        result = im.archive_messages(self.chat_path, handle_to_cid,
                                     archive_path=self.arc_path)
        self.assertEqual(result["new"], 1)   # only row 1 has actual text
        arc_conn = arc.get_archive_db(self.arc_path)
        msgs = arc.get_messages(arc_conn, contact_id=42)
        arc_conn.close()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["body"], "Hello there")
        self.assertEqual(msgs[0]["direction"], 0)

    def test_is_idempotent(self):
        handle_to_cid = {"+46701740611": 42}
        im.archive_messages(self.chat_path, handle_to_cid, archive_path=self.arc_path)
        result2 = im.archive_messages(self.chat_path, handle_to_cid, archive_path=self.arc_path)
        self.assertEqual(result2["new"], 0)
        self.assertEqual(result2["skipped"], 1)

    def _add_group_chat(self, chat_id=2, members=("+46701740611", "+46701740622",
                                                  "+46701740633"),
                        display_name="Project chat"):
        c = sqlite3.connect(self.chat_path)
        if "display_name" not in {r[1] for r in c.execute("PRAGMA table_info(chat)")}:
            c.execute("ALTER TABLE chat ADD COLUMN display_name TEXT")
        c.execute("INSERT INTO chat(ROWID, display_name) VALUES(?, ?)",
                  (chat_id, display_name))
        ns = _apple_ns(MAY2026)
        for i, handle in enumerate(members):
            hid = c.execute("SELECT ROWID FROM handle WHERE id=?", (handle,)).fetchone()
            if not hid:
                c.execute("INSERT INTO handle(id) VALUES(?)", (handle,))
                hid = c.execute("SELECT ROWID FROM handle WHERE id=?", (handle,)).fetchone()
            c.execute("INSERT INTO chat_handle_join VALUES(?,?)", (chat_id, hid[0]))
        # one message in the group from the first member
        hid = c.execute("SELECT ROWID FROM handle WHERE id=?", (members[0],)).fetchone()[0]
        pk = 100 + chat_id
        c.execute("INSERT INTO message VALUES(?, ?, 0, ?, NULL, 0, ?)",
                  (pk, ns + 3600 * 10**9, f"group news {chat_id}", hid))
        c.execute("INSERT INTO chat_message_join VALUES(?, ?)", (chat_id, pk))
        c.commit(); c.close()

    def test_group_messages_archived_with_label_within_cap(self):
        self._add_group_chat()  # 3 members, named "Project chat"
        result = im.archive_messages(self.chat_path, {"+46701740611": 42},
                                     archive_path=self.arc_path,
                                     max_group_members=8)
        self.assertEqual(result["new"], 2)  # the 1:1 hello + the group message
        arc_conn = arc.get_archive_db(self.arc_path)
        grp = arc_conn.execute(
            "SELECT * FROM messages WHERE group_name != ''").fetchall()
        arc_conn.close()
        self.assertEqual(len(grp), 1)
        self.assertEqual(grp[0]["body"], "group news 2")
        self.assertEqual(grp[0]["group_name"], "Project chat")

    def test_groups_over_cap_are_ignored(self):
        self._add_group_chat()  # 3 members
        result = im.archive_messages(self.chat_path, {"+46701740611": 42},
                                     archive_path=self.arc_path,
                                     max_group_members=2)  # cap below group size
        self.assertEqual(result["new"], 1)  # only the 1:1 hello
        arc_conn = arc.get_archive_db(self.arc_path)
        n = arc_conn.execute(
            "SELECT COUNT(*) FROM messages WHERE group_name != ''").fetchone()[0]
        arc_conn.close()
        self.assertEqual(n, 0)

    def test_two_unnamed_groups_of_the_same_size_are_two_conversations(self):
        """NC-110, the whole bug in one test.

        An iMessage chat with no subject line has no label, so the importer
        makes one out of the head count. Two unrelated three-person chats
        therefore arrive labelled identically, and in a large archive that would
        weld many people's conversations into a single row called "Group of 3".
        The label may collide; the conversation must not.
        """
        self._add_group_chat(chat_id=2, display_name=None,
                             members=("+46701740611", "+46701740622",
                                      "+46701740633"))
        self._add_group_chat(chat_id=3, display_name=None,
                             members=("+46701740611", "+46701740644",
                                      "+46701740655"))
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        arc_conn = arc.get_archive_db(self.arc_path)
        rows = arc_conn.execute(
            "SELECT group_name, group_key FROM messages WHERE group_name != ''"
        ).fetchall()
        arc_conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["group_name"] for r in rows}, {"Group of 4"},
                         "the two chats should share a generated label")
        self.assertEqual(len({r["group_key"] for r in rows}), 2,
                         "two separate chats were stored as one conversation")
        for r in rows:
            self.assertTrue(r["group_key"].startswith("imessage:"))

    def test_a_named_group_still_gets_a_key(self):
        """A subject line is not an identity either — two chats can share one."""
        self._add_group_chat()
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute(
            "SELECT group_name, group_key FROM messages "
            "WHERE group_name != ''").fetchone()
        arc_conn.close()
        self.assertEqual(row["group_name"], "Project chat")
        self.assertEqual(row["group_key"], "imessage:2")

    def test_a_one_to_one_message_has_no_group_key(self):
        """'' is what "not a group" means, and the group views filter on it."""
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path)
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute("SELECT group_key FROM messages").fetchone()
        arc_conn.close()
        self.assertEqual(row["group_key"], "")

    def test_a_key_never_holds_more_speakers_than_its_label_claims(self):
        """The invariant that exposed NC-110 in the first place, as a test.

        A label reading "Group of N" cannot describe a conversation with more
        than N participants. Before the fix every generated label broke this — one
        "Group of 3" row held many distinct speakers — and the breach is what
        proves a label is being used as an identity.
        """
        self._add_group_chat(chat_id=2, display_name=None,
                             members=("+46701740611", "+46701740622",
                                      "+46701740633"))
        self._add_group_chat(chat_id=3, display_name=None,
                             members=("+46701740611", "+46701740644",
                                      "+46701740655"))
        # Both members are known contacts, so both chats have a speaker the
        # importer can attribute — with one shared handle across the two chats.
        im.archive_messages(self.chat_path,
                            {"+46701740611": 42, "+46701740622": 43},
                            archive_path=self.arc_path, max_group_members=8)
        arc_conn = arc.get_archive_db(self.arc_path)
        rows = arc_conn.execute(
            "SELECT group_name, group_key, COUNT(DISTINCT contact_id) AS speakers "
            "FROM messages WHERE group_key != '' GROUP BY group_key, group_name"
        ).fetchall()
        arc_conn.close()
        self.assertTrue(rows)
        for r in rows:
            claim = int(r["group_name"].rsplit(" ", 1)[1])
            self.assertLessEqual(
                r["speakers"], claim,
                f"{r['group_key']} is labelled {r['group_name']} but holds "
                f"{r['speakers']} distinct speakers — the label is being used "
                f"as an identity")

    def test_the_backfill_gives_old_rows_their_key_without_a_re_import(self):
        """The migration path for the messages already in the archive.

        A re-import would work — insert_message backfills — but it reads macOS
        Contacts and creates contacts on the way, which is a much larger act
        than filling in one column. This needs only the chat.db copy and the
        archive, and matches on the source_key already stored.
        """
        self._add_group_chat(chat_id=2, display_name=None)
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        arc_conn = arc.get_archive_db(self.arc_path)
        arc_conn.execute("UPDATE messages SET group_key = ''")   # as if pre-NC-110
        arc_conn.commit(); arc_conn.close()

        out = im.backfill_group_keys(self.chat_path, archive_path=self.arc_path)
        self.assertEqual(out["filled"], 1)
        self.assertEqual(out["unknown"], 0)
        self.assertEqual(out["ambiguous"], 0)
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute(
            "SELECT group_key, contact_id FROM messages "
            "WHERE group_name != ''").fetchone()
        arc_conn.close()
        self.assertEqual(row["group_key"], "imessage:2")
        self.assertEqual(row["contact_id"], 42, "the backfill moved a message "
                                                "to a different person")

    def test_the_backfill_is_a_no_op_the_second_time(self):
        self._add_group_chat(chat_id=2, display_name=None)
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        first = im.backfill_group_keys(self.chat_path, archive_path=self.arc_path)
        second = im.backfill_group_keys(self.chat_path, archive_path=self.arc_path)
        self.assertEqual(first["filled"], 0, "the import should already have "
                                             "written the key")
        self.assertEqual(second["filled"], 0)

    def test_the_backfill_leaves_one_to_one_messages_alone(self):
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path)
        im.backfill_group_keys(self.chat_path, archive_path=self.arc_path)
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute("SELECT group_key FROM messages").fetchone()
        arc_conn.close()
        self.assertEqual(row["group_key"], "")

    def test_the_backfill_counts_add_up_to_what_was_outstanding(self):
        """filled + ambiguous + unknown == pending. A row that went nowhere
        cannot hide inside a total that looks healthy."""
        self._add_group_chat(chat_id=2, display_name=None)
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        arc_conn = arc.get_archive_db(self.arc_path)
        # A group row the source copy knows nothing about — a message deleted
        # from chat.db since it was archived.
        arc.insert_message(arc_conn, contact_id=42, channel="imessage",
                           direction=0, ts="2026-05-01 09:00:00",
                           body="orphan", source_key="999999",
                           group_name="Group of 4")
        arc_conn.execute("UPDATE messages SET group_key = ''")
        arc_conn.commit()
        pending = arc_conn.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE channel='imessage' AND group_name <> '' AND group_key = ''"
        ).fetchone()[0]
        arc_conn.close()
        out = im.backfill_group_keys(self.chat_path, archive_path=self.arc_path)
        self.assertEqual(out["filled"] + out["ambiguous"] + out["unknown"],
                         pending)
        self.assertEqual(out["unknown"], 1)

    def test_the_owners_own_group_messages_are_stored_against_the_conversation(self):
        """NC-111. Every group message the importer archived was inbound — every
        one of them — so every group conversation was a transcript with the owner's
        half deleted. An outbound group message has no single recipient, which is why
        the main query skips it; with a conversation to belong to it no longer
        needs one."""
        self._add_group_chat(chat_id=2, display_name="Project chat")
        c = sqlite3.connect(self.chat_path)
        ns = _apple_ns(MAY2026)
        c.execute("INSERT INTO message VALUES(200, ?, 1, 'my reply', NULL, 0, 0)",
                  (ns + 3700 * 10**9,))
        c.execute("INSERT INTO chat_message_join VALUES(2, 200)")
        c.commit(); c.close()

        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        out = im.archive_own_group_messages(self.chat_path,
                                            archive_path=self.arc_path)
        self.assertEqual(out["new"], 1)
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute(
            "SELECT * FROM messages WHERE direction = 1 AND group_name != ''"
        ).fetchone()
        arc_conn.close()
        self.assertEqual(row["body"], "my reply")
        self.assertEqual(row["contact_id"], arc.NO_PERSON)
        self.assertEqual(row["group_key"], "imessage:2")
        self.assertEqual(row["group_name"], "Project chat",
                         "the owner's half was stored under a different label from "
                         "the other half of the same conversation")

    def test_an_import_saves_the_owners_group_messages_and_chunks_them(self):
        """NC-199, iMessage side: the step main() runs now includes the owner's half."""
        self._add_group_chat(chat_id=2, display_name="Project chat")
        c = sqlite3.connect(self.chat_path)
        c.execute("INSERT INTO message VALUES(200, ?, 1, 'my reply', NULL, 0, 0)",
                  (_apple_ns(MAY2026) + 3700 * 10**9,))
        c.execute("INSERT INTO chat_message_join VALUES(2, 200)")
        c.commit(); c.close()
        out = im.archive_and_chunk(self.chat_path, {"+46701740611": 42},
                                   max_group_members=8, archive_path=self.arc_path)
        self.assertEqual(out["own"], 1)
        self.assertEqual(out["new"], 3, "the hello, the group news, and the owner's reply")
        arc_conn = arc.get_archive_db(self.arc_path)
        row = arc_conn.execute("SELECT contact_id, direction FROM messages "
                               "WHERE body = 'my reply'").fetchone()
        chunk = arc_conn.execute("SELECT * FROM chunks WHERE contact_id = ?",
                                 (arc.NO_PERSON,)).fetchone()
        arc_conn.close()
        self.assertEqual((row["contact_id"], row["direction"]), (arc.NO_PERSON, 1))
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk["group_name"], "Project chat")

    def test_a_second_import_adds_nothing_and_rebuilds_nothing(self):
        self._add_group_chat(chat_id=2, display_name="Project chat")
        im.archive_and_chunk(self.chat_path, {"+46701740611": 42},
                             max_group_members=8, archive_path=self.arc_path)
        again = im.archive_and_chunk(self.chat_path, {"+46701740611": 42},
                                     max_group_members=8, archive_path=self.arc_path)
        self.assertEqual((again["new"], again["chunks"]), (0, 0))

    def test_main_runs_it(self):
        import inspect
        self.assertIn("archive_and_chunk(", inspect.getsource(im.main))

    def test_it_only_completes_conversations_the_archive_already_has(self):
        """Unscoped this would invent threads with no known participant —
        conversations no page could ever show."""
        self._add_group_chat(chat_id=2, display_name="Project chat")
        self._add_group_chat(chat_id=3, display_name="Strangers",
                             members=("+46701740699", "+46701740688"))
        c = sqlite3.connect(self.chat_path)
        ns = _apple_ns(MAY2026)
        for pk, chat in ((200, 2), (201, 3)):
            c.execute("INSERT INTO message VALUES(?, ?, 1, ?, NULL, 0, 0)",
                      (pk, ns + 3700 * 10**9, f"mine in {chat}"))
            c.execute("INSERT INTO chat_message_join VALUES(?, ?)", (chat, pk))
        c.commit(); c.close()

        # Only chat 2 has a known contact, so only chat 2 is in the archive.
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        out = im.archive_own_group_messages(self.chat_path,
                                            archive_path=self.arc_path)
        self.assertEqual(out["new"], 1)
        arc_conn = arc.get_archive_db(self.arc_path)
        keys = {r["group_key"] for r in arc_conn.execute(
            "SELECT group_key FROM messages WHERE direction = 1 "
            "AND group_name != ''")}
        arc_conn.close()
        self.assertEqual(keys, {"imessage:2"})

    def test_running_it_twice_stores_nothing_new(self):
        self._add_group_chat(chat_id=2, display_name="Project chat")
        c = sqlite3.connect(self.chat_path)
        c.execute("INSERT INTO message VALUES(200, ?, 1, 'my reply', NULL, 0, 0)",
                  (_apple_ns(MAY2026) + 3700 * 10**9,))
        c.execute("INSERT INTO chat_message_join VALUES(2, 200)")
        c.commit(); c.close()
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        im.archive_own_group_messages(self.chat_path, archive_path=self.arc_path)
        again = im.archive_own_group_messages(self.chat_path,
                                              archive_path=self.arc_path)
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["skipped"], 1)

    def test_a_group_thread_then_has_both_sides(self):
        """The observable end state, which is the point of NC-199."""
        self._add_group_chat(chat_id=2, display_name="Project chat")
        c = sqlite3.connect(self.chat_path)
        c.execute("INSERT INTO message VALUES(200, ?, 1, 'my reply', NULL, 0, 0)",
                  (_apple_ns(MAY2026) + 3700 * 10**9,))
        c.execute("INSERT INTO chat_message_join VALUES(2, 200)")
        c.commit(); c.close()
        im.archive_messages(self.chat_path, {"+46701740611": 42},
                            archive_path=self.arc_path, max_group_members=8)
        im.archive_own_group_messages(self.chat_path, archive_path=self.arc_path)
        arc_conn = arc.get_archive_db(self.arc_path)
        sides = {r["direction"] for r in arc_conn.execute(
            "SELECT direction FROM messages WHERE group_key = 'imessage:2'")}
        arc_conn.close()
        self.assertEqual(sides, {0, 1}, "the conversation still has one side")

    def test_decode_attributed_body_returns_string(self):
        import plistlib
        # Build a minimal NSKeyedArchiver plist with NS.string
        pl = {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": [
                "$null",
                {"$class": plistlib.UID(2), "NS.string": "decoded text"},
                {"$classname": "NSAttributedString", "$classes": ["NSAttributedString"]},
            ],
        }
        blob = plistlib.dumps(pl, fmt=plistlib.FMT_BINARY)
        result = im.decode_attributed_body(blob)
        self.assertEqual(result, "decoded text")

    def test_decode_attributed_body_handles_garbage(self):
        self.assertEqual(im.decode_attributed_body(b"not a plist"), "")
        self.assertEqual(im.decode_attributed_body(b""), "")

    @staticmethod
    def _make_typedstream(text: str) -> bytes:
        """Synthetic macOS 13+ `streamtyped` attributedBody blob for `text`."""
        payload = text.encode("utf-8")
        n = len(payload)
        if n < 0x80:
            length = bytes([n])
        elif n <= 0xFFFF:
            length = b"\x81" + n.to_bytes(2, "little")
        else:
            length = b"\x82" + n.to_bytes(4, "little")
        return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12"
                b"NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84"
                b"\x84\x08NSString\x01\x95\x84\x01\x2b" + length + payload
                + b"\x86\x84\x02iI\x01")

    def test_decode_typedstream_short_unicode_and_long(self):
        # single-byte length
        self.assertEqual(
            im.decode_attributed_body(self._make_typedstream("Hello there")),
            "Hello there")
        # unicode (accents + emoji)
        self.assertEqual(
            im.decode_attributed_body(self._make_typedstream("Hej på fredag? 🎉")),
            "Hej på fredag? 🎉")
        # >127 bytes → 0x81 uint16-LE length path
        longtext = "x" * 200
        self.assertEqual(
            im.decode_attributed_body(self._make_typedstream(longtext)), longtext)

    def test_archive_decodes_typedstream_attributedbody(self):
        # A row with NULL text but a real typedstream attributedBody must be
        # archived with the decoded text (the macOS 14+ common case).
        c = sqlite3.connect(self.chat_path)
        blob = self._make_typedstream("sent from newer macOS")
        c.execute("INSERT INTO message VALUES(4,?,?,?,?,0,?)",
                  (_apple_ns(MAY2026) + 180 * 10**9, 0, None, blob, 1))
        c.execute("INSERT INTO chat_message_join VALUES(1,4)")
        c.commit(); c.close()

        result = im.archive_messages(self.chat_path, {"+46701740611": 42},
                                     archive_path=self.arc_path)
        self.assertEqual(result["new"], 2)  # "Hello there" + the decoded blob
        arc_conn = arc.get_archive_db(self.arc_path)
        bodies = {m["body"] for m in arc.get_messages(arc_conn, contact_id=42)}
        arc_conn.close()
        self.assertIn("sent from newer macOS", bodies)

    def test_empty_handle_to_cid_returns_zero(self):
        result = im.archive_messages(self.chat_path, {}, archive_path=self.arc_path)
        self.assertEqual(result["new"], 0)


class TestTelegramArchive(unittest.TestCase):
    """telegram_import.archive_messages: text extraction + idempotent write."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_arc_test_")
        self.arc_path = os.path.join(self.dir, "archive.db")
        self.crm_path = os.path.join(self.dir, "crm.db")
        db.init_db(self.crm_path)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.crm_path
        # Synthetic Telegram export with a plain string message and entity-list message
        self.data = {
            "personal_information": {"user_id": 999},
            "chats": {"list": [{
                "type": "personal_chat",
                "name": "Anna",
                "messages": [
                    {"type": "message", "id": 1, "date_unixtime": "1746096000",
                     "from_id": "user42", "text": "hello there"},
                    {"type": "message", "id": 2, "date_unixtime": "1746096060",
                     "from_id": "user999", "text": [
                         {"type": "plain", "text": "fine "},
                         {"type": "bold", "text": "thanks"}
                     ]},
                    {"type": "service", "id": 3, "date_unixtime": "1746096120",
                     "text": ""},   # service message → no text
                ]
            }]},
        }

    def tearDown(self):
        db.DB_PATH = self._orig_db_path

    def test_stores_plain_and_entity_text(self):
        name_to_cid = {"Anna": 7}
        result = tg.archive_messages(self.data, name_to_cid, archive_path=self.arc_path)
        self.assertEqual(result["new"], 2)   # 2 real messages; service message empty text skipped
        arc_conn = arc.get_archive_db(self.arc_path)
        msgs = arc.get_messages(arc_conn, contact_id=7)
        arc_conn.close()
        bodies = {m["body"] for m in msgs}
        self.assertIn("hello there", bodies)
        self.assertIn("fine thanks", bodies)

    def test_is_idempotent(self):
        name_to_cid = {"Anna": 7}
        tg.archive_messages(self.data, name_to_cid, archive_path=self.arc_path)
        r2 = tg.archive_messages(self.data, name_to_cid, archive_path=self.arc_path)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(r2["skipped"], 2)

    def test_empty_map_returns_zero(self):
        result = tg.archive_messages(self.data, {}, archive_path=self.arc_path)
        self.assertEqual(result["new"], 0)


class TestMetaArchive(unittest.TestCase):
    """meta_import.archive_messages: content extraction + mojibake handling."""

    SELF = "Me Self"
    SELF_NORM = db.normalize_name("Me Self")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="meta_arc_test_")
        self.arc_path = os.path.join(self.dir, "archive.db")
        ts = int(MAY2026.timestamp() * 1000)
        anna = "Anna Boo"
        self.threads_raw = [
            ([anna, self.SELF], anna, [
                {"sender_name": anna,      "timestamp_ms": ts,      "content": "hey!"},
                {"sender_name": self.SELF, "timestamp_ms": ts + 60000, "content": "hi back"},
                {"sender_name": anna,      "timestamp_ms": ts + 120000},  # media, no content
            ])
        ]

    def test_stores_content_and_skips_media(self):
        result = meta.archive_messages(self.threads_raw, {"Anna Boo": 5},
                                       "messenger", self.SELF_NORM,
                                       archive_path=self.arc_path)
        self.assertEqual(result["new"], 2)   # 3rd message has no content
        arc_conn = arc.get_archive_db(self.arc_path)
        msgs = arc.get_messages(arc_conn, contact_id=5)
        arc_conn.close()
        directions = {m["body"]: m["direction"] for m in msgs}
        self.assertEqual(directions["hey!"], 0)
        self.assertEqual(directions["hi back"], 1)

    def test_is_idempotent(self):
        meta.archive_messages(self.threads_raw, {"Anna Boo": 5}, "messenger",
                              self.SELF_NORM, archive_path=self.arc_path)
        r2 = meta.archive_messages(self.threads_raw, {"Anna Boo": 5}, "messenger",
                                   self.SELF_NORM, archive_path=self.arc_path)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(r2["skipped"], 2)

    def test_empty_map_returns_zero(self):
        result = meta.archive_messages(self.threads_raw, {}, "messenger",
                                       self.SELF_NORM, archive_path=self.arc_path)
        self.assertEqual(result["new"], 0)


class TestEmailArchive(unittest.TestCase):
    """email_import.archive_messages: body extraction + direction + idempotency."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="em_arc_test_")
        self.arc_path = os.path.join(self.dir, "archive.db")
        self.crm_path = os.path.join(self.dir, "crm.db")
        db.init_db(self.crm_path)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.crm_path
        self.mbox_path = os.path.join(self.dir, "test.mbox")
        self._orig_self_email = ei.SELF_EMAIL
        ei.SELF_EMAIL = "me@example.com"
        self._write_mbox()

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        ei.SELF_EMAIL = self._orig_self_email

    def _make_msg(self, from_addr, to_addr, body, subject="hi", msg_id=None):
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Date"] = "Mon, 04 May 2026 09:00:00 +0000"
        msg["Subject"] = subject
        msg["Message-ID"] = msg_id or f"<{subject.replace(' ','_')}@test>"
        return msg

    def _write_mbox(self):
        mb = mailbox.mbox(self.mbox_path, create=True)
        mb.add(self._make_msg("ada@example.com", "me@example.com", "Hey there!\nHow are you?",
                              msg_id="<recv1@test>"))
        mb.add(self._make_msg("me@example.com", "ada@example.com", "Great, thanks!",
                              msg_id="<sent1@test>"))
        mb.flush(); mb.close()

    def test_stores_inbound_and_outbound_bodies(self):
        result = ei.archive_messages(self.mbox_path, {"ada@example.com": 99},
                                     archive_path=self.arc_path)
        self.assertEqual(result["new"], 2)
        arc_conn = arc.get_archive_db(self.arc_path)
        msgs = arc.get_messages(arc_conn, contact_id=99)
        arc_conn.close()
        self.assertEqual(len(msgs), 2)
        directions = {m["body"].strip(): m["direction"] for m in msgs}
        self.assertIn("Hey there!\nHow are you?".strip(), directions)
        self.assertIn("Great, thanks!", directions)
        self.assertEqual(directions.get("Great, thanks!"), 1)  # outbound

    def test_is_idempotent(self):
        ei.archive_messages(self.mbox_path, {"ada@example.com": 99}, archive_path=self.arc_path)
        r2 = ei.archive_messages(self.mbox_path, {"ada@example.com": 99}, archive_path=self.arc_path)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(r2["skipped"], 2)

    def test_empty_addr_map_returns_zero(self):
        result = ei.archive_messages(self.mbox_path, {}, archive_path=self.arc_path)
        self.assertEqual(result["new"], 0)


class TestAShortLinkedinWindowKeepsSavedHistory(unittest.TestCase):
    """LinkedIn's API only returns a recent window, much shallower than the
    history an older source (the retired scraper's snapshot) already saved.

    This used to be guarded by refusing to ever LOWER `msg_count`
    (TestLinkedinImportCountGuard). That kept history, and it also kept the
    placeholders the scraper counted and never saved: cards claimed messages
    their timelines did not hold (NC-107). The rule now: the count is the
    messages the app can show, recounted from archive.db. The history the old
    guard protected is still safe: an import only ever adds saved messages (the
    one thing that removes them is permanently deleting the person, card and
    all), so a short window cannot take any away. These tests say so directly."""

    def setUp(self):
        import engagement
        import linkedin_import as li
        self.li, self.engagement = li, engagement
        self.dir = tempfile.mkdtemp(prefix="crm_li_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        self.arc_path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.arc_path)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path

    def _row(self, direction, ts, body):
        return {
            "CONVERSATION ID": "conv-1", "DATE": ts + " UTC",
            "FROM": "them" if direction == 0 else "me",
            "CONTENT": body, "IS MESSAGE DRAFT": "No",
            "SENDER PROFILE URL": "https://linkedin.com/in/them" if direction == 0 else "https://linkedin.com/in/me",
            "RECIPIENT PROFILE URLS": "https://linkedin.com/in/me" if direction == 0 else "https://linkedin.com/in/them",
        }

    def _contact(self, name, count=0, last=""):
        conn = db.get_db(self.dbpath)
        cid = db.create_contact(conn, name)
        if count or last:
            db.set_channel(conn, cid, "linkedin", count, last)
        conn.commit()
        conn.close()
        return cid

    def _saved(self, cid, n):
        """History an older source already saved: n messages from 2019."""
        a = arc.get_archive_db(self.arc_path)
        for i in range(n):
            arc.insert_message(a, contact_id=cid, channel="linkedin", direction=0,
                               ts=f"2019-01-{1 + i:02d} 10:00:00", body=f"old {i}",
                               source_key=f"old-{cid}-{i}")
        a.commit()
        a.close()

    def _import_then_recount(self, cid, rows):
        self.li.archive_messages(rows, {"https://linkedin.com/in/them": cid},
                                 archive_path=self.arc_path)
        conn = db.get_db(self.dbpath)
        try:
            self.engagement.refresh(conn, archive_path=self.arc_path)
            return db.get_contact(conn, cid)
        finally:
            conn.close()

    def test_a_short_window_does_not_shrink_what_is_saved(self):
        cid = self._contact("Deep History Dana")
        self._saved(cid, 40)
        row = self._import_then_recount(cid, [self._row(0, "2026-07-01 10:00:00", "hey")])
        self.assertEqual(row["msg_count"], 41, "the 40 saved messages are still counted")
        self.assertEqual(row["last_contact"], "2026-07-01 10:00:00")

    def test_a_count_with_nothing_saved_behind_it_does_not_survive(self):
        """NC-107 itself: a number the timeline cannot show."""
        cid = self._contact("Placeholder Pat", count=5000, last="2026-08-01 00:00:00")
        row = self._import_then_recount(cid, [self._row(0, "2026-07-01 10:00:00", "hey")])
        self.assertEqual(row["msg_count"], 1)
        self.assertEqual(row["last_contact"], "2026-07-01 10:00:00",
                         "a date with no saved message behind it goes too")

    def test_both_directions_are_counted(self):
        cid = self._contact("Fresh Contact Fred")
        # Only the OTHER person's URL is ever a key here -- "me" never is,
        # since the owner is never a contact in their own CRM; that's what lets
        # archive_messages() tell inbound from outbound in the first place.
        row = self._import_then_recount(cid, [
            self._row(0, "2026-07-01 10:00:00", "hey"),
            self._row(1, "2026-07-01 10:05:00", "yo"),
        ])
        self.assertEqual(row["msg_count"], 2)

    def test_the_import_itself_writes_no_count(self):
        """One writer: archive_messages stores messages, the recount counts them."""
        cid = self._contact("Untouched Una", count=7, last="2020-01-01 00:00:00")
        self.li.archive_messages([self._row(0, "2026-07-01 10:00:00", "hey")],
                                 {"https://linkedin.com/in/them": cid},
                                 archive_path=self.arc_path)
        conn = db.get_db(self.dbpath)
        row = db.get_contact(conn, cid)
        conn.close()
        self.assertEqual((row["msg_count"], row["last_contact"]), (7, "2020-01-01 00:00:00"))


