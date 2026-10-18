"""NC-112. Searching finds what the owner said in a group chat, and shows it.

NC-111 stored the owner's own group messages under `arc.NO_PERSON`: a message to
a group went to everyone in it, so no one person's page is its honest home. Both
search modes then looked each hit's person up, found none, and dropped it. The
words were in the index, matched, and ranked, and the search box showed nothing,
for every message the owner had sent to a group.

A hit like that now comes back as the conversation's: `contact_id` stays
NO_PERSON, `conversation` is the chat's label, and `open_id` is someone in the
chat whose page shows the whole thread (NC-106) — never a person the owner hid.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, arc, db, search_cli  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHRASE = "lighthouse picnic"
GROUP_KEY = "imessage:chat-book-club"


class _GroupArchive(Base):

    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="nc112_")
        self.arc_path = os.path.join(self.arc_dir, "archive.db")
        self._orig = (arc.ARCHIVE_DB_PATH, search_cli.CHUNK_CACHE)
        arc.ARCHIVE_DB_PATH = self.arc_path
        search_cli.CHUNK_CACHE = os.path.join(self.arc_dir, "chunk_vectors.npz")
        self.ada = db.create_contact(self.conn, "Ada Lovelace")
        self.bob = db.create_contact(self.conn, "Bob Hopper")
        db.set_excluded(self.conn, self.bob, 1)      # hidden, and nearest in time
        self.conn.commit()
        arc.init_archive(self.arc_path)
        a = arc.get_archive_db(self.arc_path)
        common = dict(channel="imessage", group_name="Book club", group_key=GROUP_KEY)
        arc.insert_message(a, contact_id=self.ada, direction=0, ts="2026-06-01 09:00:00",
                           body="who is bringing food", source_key="1", **common)
        arc.insert_message(a, contact_id=arc.NO_PERSON, direction=1,
                           ts="2026-06-01 10:00:00",
                           body=f"the {PHRASE} is on sunday", source_key="2", **common)
        arc.insert_message(a, contact_id=self.bob, direction=0, ts="2026-06-01 10:01:00",
                           body="great", source_key="3", **common)
        a.commit()
        arc.build_chunks(a)
        a.close()

    def tearDown(self):
        arc.ARCHIVE_DB_PATH, search_cli.CHUNK_CACHE = self._orig
        super().tearDown()

    def search(self, mode):
        return app.handle_conversation_search(self.conn, {"q": [PHRASE], "mode": [mode]})

    def only_hit(self, out):
        self.assertTrue(out["available"])
        self.assertEqual(len(out["conversations"]), 1, out["conversations"])
        return out["conversations"][0]

    def assert_is_the_conversation(self, hit):
        self.assertEqual(hit["contact_id"], arc.NO_PERSON)
        self.assertEqual(hit["conversation"], "Book club")
        self.assertEqual(hit["open_id"], self.ada,
                         "it should open the page of someone in the chat the owner can "
                         "see — Bob spoke nearer, but Bob is hidden")
        self.assertIn("lighthouse", hit["snippet"])


class TestKeywordSearch(_GroupArchive):

    def test_the_owners_group_message_is_found_and_shown(self):
        self.assert_is_the_conversation(self.only_hit(self.search("keyword")))


class TestSemanticSearch(_GroupArchive):
    """The model is not loaded in tests, so the ranking is stubbed to return the
    real chunk the archive built. What is under test is what the handler does
    with a chunk that has no person: it used to `continue`."""

    def test_the_owners_group_chunk_is_shown(self):
        a = arc.get_archive_db(self.arc_path)
        chunk = a.execute("SELECT * FROM chunks WHERE contact_id = ?",
                          (arc.NO_PERSON,)).fetchone()
        a.close()
        with mock.patch.object(search_cli, "embeddings_available", lambda: True), \
             mock.patch.object(search_cli, "chunk_semantic_rank",
                               lambda *a, **k: [(0.9, chunk)]):
            hit = self.only_hit(self.search("semantic"))
        self.assert_is_the_conversation(hit)
        self.assertEqual(hit["score"], 0.9)


class TestAChunkCarriesItsConversation(_GroupArchive):

    def test_the_chunk_knows_which_chat_it_came_from(self):
        a = arc.get_archive_db(self.arc_path)
        row = a.execute("SELECT group_name, group_key FROM chunks WHERE contact_id = ?",
                        (arc.NO_PERSON,)).fetchone()
        a.close()
        self.assertEqual((row["group_name"], row["group_key"]), ("Book club", GROUP_KEY))

    def test_an_older_archive_is_filled_in_once_without_re_embedding(self):
        """An archive made before the columns existed: the migration adds them
        and rebuilds the no-person chunks, whose text and hash come out the same
        — so the embedding cache, keyed by hash, still has every one."""
        a = sqlite3.connect(self.arc_path)
        before = a.execute("SELECT content_hash, text FROM chunks WHERE contact_id = 0").fetchall()
        self.assertTrue(before, "the premise: there are no-person chunks to migrate")
        a.execute("ALTER TABLE chunks DROP COLUMN group_key")
        a.execute("ALTER TABLE chunks DROP COLUMN group_name")
        a.commit()
        a.close()
        arc.init_archive(self.arc_path)
        a = arc.get_archive_db(self.arc_path)
        after = a.execute("SELECT content_hash, text, group_name, group_key FROM chunks "
                          "WHERE contact_id = 0").fetchall()
        a.close()
        self.assertEqual([(r["content_hash"], r["text"]) for r in after],
                         [tuple(r) for r in before])
        self.assertEqual({(r["group_name"], r["group_key"]) for r in after},
                         {("Book club", GROUP_KEY)})


class TestTheSearchBoxDrawsAndOpensIt(unittest.TestCase):

    def test_the_row_is_labelled_with_the_conversation_and_opens_someone_in_it(self):
        with open(os.path.join(REPO, "static", "app", "main.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn('row.c.conversation ? "You, in " + row.c.conversation', js)
        self.assertIn("row.c.open_id || row.c.contact_id", js,
                      "a conversation's hit would open people/0, a page that "
                      "does not exist")


if __name__ == "__main__":
    unittest.main()
