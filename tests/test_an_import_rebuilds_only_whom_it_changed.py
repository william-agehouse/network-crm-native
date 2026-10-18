"""NC-222. An import rebuilds the search pieces of the people it gave a new message.

After any iMessage or WhatsApp import that stored even one new message, the
archive step rebuilt the conversation pieces (chunks) of EVERY person the import
had matched, deleting and re-inserting each one. On a synthetic archive of 1,500
people, one new message cost 49 MB of disk writes. A person with nothing new
has nothing to rebuild: their pieces are made from their messages alone.

The proof is the pieces' own row ids. A rebuilt person's pieces are deleted and
inserted again, so they get new ids; a person left alone keeps theirs. (Not for
the person with the highest id: SQLite hands that id out again after a delete.
So the person who got a message is checked by their pieces' text instead.)
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import MAY2026, _apple_ns, _coredata_secs, arc, im, wa  # noqa: E402

PEOPLE = {"+46701740611": 1, "+46701740622": 2, "+46701740633": 3}


def _chunk_ids(arc_path):
    conn = arc.get_archive_db(arc_path)
    try:
        out = {}
        for r in conn.execute("SELECT rowid AS rid, contact_id FROM chunks ORDER BY rowid"):
            out.setdefault(r["contact_id"], []).append(r["rid"])
        return out
    finally:
        conn.close()


def _texts(arc_path, contact_id):
    conn = arc.get_archive_db(arc_path)
    try:
        return " ".join(r["text"] for r in conn.execute(
            "SELECT text FROM chunks WHERE contact_id = ?", (contact_id,)))
    finally:
        conn.close()


class TestAnIMessageImportRebuildsOnlyWhomItChanged(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc222_im_")
        self.arc = os.path.join(self.dir, "archive.db")
        self.chat = os.path.join(self.dir, "chat.db")
        c = sqlite3.connect(self.chat)
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
        self.ns = _apple_ns(MAY2026)
        for i, handle in enumerate(PEOPLE, start=1):
            c.execute("INSERT INTO handle VALUES(?, ?)", (i, handle))
            c.execute("INSERT INTO chat VALUES(?)", (i,))
            c.execute("INSERT INTO chat_handle_join VALUES(?, ?)", (i, i))
            c.execute("INSERT INTO message VALUES(?, ?, 0, ?, NULL, 0, ?)",
                      (i, self.ns + i * 10**9, f"hello from {i}", i))
            c.execute("INSERT INTO chat_message_join VALUES(?, ?)", (i, i))
        c.commit()
        c.close()

    def _one_more_message_from(self, handle_rowid, pk=50):
        c = sqlite3.connect(self.chat)
        c.execute("INSERT INTO message VALUES(?, ?, 0, 'one more thing', NULL, 0, ?)",
                  (pk, self.ns + 600 * 10**9, handle_rowid))
        c.execute("INSERT INTO chat_message_join VALUES(?, ?)", (handle_rowid, pk))
        c.commit()
        c.close()

    def test_the_first_import_builds_everyone(self):
        out = im.archive_and_chunk(self.chat, PEOPLE, archive_path=self.arc)
        self.assertEqual(out["new"], 3)
        self.assertEqual(set(_chunk_ids(self.arc)), {1, 2, 3})

    def test_one_new_message_rebuilds_one_person(self):
        im.archive_and_chunk(self.chat, PEOPLE, archive_path=self.arc)
        before = _chunk_ids(self.arc)
        self._one_more_message_from(2)
        out = im.archive_and_chunk(self.chat, PEOPLE, archive_path=self.arc)
        after = _chunk_ids(self.arc)
        self.assertEqual(out["new"], 1)
        self.assertEqual(after[1], before[1], "person 1 had nothing new and was rebuilt")
        self.assertEqual(after[3], before[3], "person 3 had nothing new and was rebuilt")
        self.assertIn("one more thing", _texts(self.arc, 2), "person 2's new message is not in the search")

    def test_nothing_new_rebuilds_nobody(self):
        im.archive_and_chunk(self.chat, PEOPLE, archive_path=self.arc)
        before = _chunk_ids(self.arc)
        out = im.archive_and_chunk(self.chat, PEOPLE, archive_path=self.arc)
        self.assertEqual((out["new"], out["chunks"]), (0, 0))
        self.assertEqual(_chunk_ids(self.arc), before)


class TestAWhatsAppImportRebuildsOnlyWhomItChanged(unittest.TestCase):

    JIDS = {"46701@s.whatsapp.net": 1, "46702@s.whatsapp.net": 2, "46703@s.whatsapp.net": 3}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc222_wa_")
        self.arc = os.path.join(self.dir, "archive.db")
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
        self.secs = _coredata_secs(MAY2026)
        for i, jid in enumerate(self.JIDS, start=1):
            c.execute("INSERT INTO ZWACHATSESSION VALUES (?, 0, ?, ?)", (i, jid, f"P{i}"))
            c.execute("INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, 0)",
                      (i, i, self.secs + i, f"hello from {i}"))
        c.commit()
        c.close()

    def test_one_new_message_rebuilds_one_person(self):
        wa.archive_and_chunk(self.wa_path, self.JIDS, archive_path=self.arc)
        before = _chunk_ids(self.arc)
        self.assertEqual(set(before), {1, 2, 3})
        c = sqlite3.connect(self.wa_path)
        c.execute("INSERT INTO ZWAMESSAGE VALUES (50, 3, ?, 'one more thing', 0)",
                  (self.secs + 600,))
        c.commit()
        c.close()
        out = wa.archive_and_chunk(self.wa_path, self.JIDS, archive_path=self.arc)
        after = _chunk_ids(self.arc)
        self.assertEqual(out["new"], 1)
        self.assertEqual(after[1], before[1], "person 1 had nothing new and was rebuilt")
        self.assertEqual(after[2], before[2], "person 2 had nothing new and was rebuilt")
        self.assertIn("one more thing", _texts(self.arc, 3), "person 3's new message is not in the search")


class TestALinkedInImportRebuildsOnlyWhomItChanged(unittest.TestCase):
    """NC-266. The same change for LinkedIn's messages."""

    URLS = {f"https://www.linkedin.com/in/person-{i}": i for i in (1, 2, 3)}

    def _row(self, i, day, body):
        return {"CONVERSATION ID": f"c{i}", "DATE": f"2026-05-{day:02d} 09:00:00 UTC",
                "FROM": f"P{i}", "SENDER PROFILE URL": f"https://www.linkedin.com/in/person-{i}",
                "RECIPIENT PROFILE URLS": "", "CONTENT": body, "IS MESSAGE DRAFT": "No"}

    def test_one_new_message_rebuilds_one_person(self):
        import linkedin_import as li
        arc_path = os.path.join(tempfile.mkdtemp(prefix="nc266_"), "archive.db")
        rows = [self._row(i, 1, f"hello from {i}") for i in (1, 2, 3)]
        first = li.archive_and_chunk(rows, self.URLS, archive_path=arc_path)
        self.assertEqual(first["new"], 3)
        before = _chunk_ids(arc_path)
        self.assertEqual(set(before), {1, 2, 3})
        out = li.archive_and_chunk(rows + [self._row(1, 2, "one more thing")], self.URLS,
                                   archive_path=arc_path)
        after = _chunk_ids(arc_path)
        self.assertEqual(out["new"], 1)
        self.assertEqual(after[2], before[2], "person 2 had nothing new and was rebuilt")
        self.assertIn("one more thing", _texts(arc_path, 1), "person 1's new message is not in the search")

    def test_main_uses_it(self):
        import inspect
        import linkedin_import as li
        self.assertIn("archive_and_chunk(", inspect.getsource(li.main))


if __name__ == "__main__":
    unittest.main()
