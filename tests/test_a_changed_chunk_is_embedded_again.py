"""NC-108. A search chunk whose text changed gets a new vector.

`content_hash` names which messages a chunk holds. The chunk's text also says
who sent each one ("Me:" or "Them:"), and that is not in the hash. So correcting
the direction of a message — which the NC-071 recovery had to do for some of them —
changed the text a chunk is searched by while its hash, and therefore its cached
vector, stayed the same. Meaning search went on answering from the old text.

The cache now remembers a fingerprint of the text each vector came from. These
tests use a stand-in model that records what it was asked to encode, so they need
numpy but not the real one.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import arc, search_cli  # noqa: E402

try:
    import numpy as np
except ImportError:  # pragma: no cover — a development venv has it
    np = None


class _Model:
    def __init__(self):
        self.asked = []

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        self.asked.append(list(texts))
        return np.array([[float(len(t)), 1.0] for t in texts])


@unittest.skipIf(np is None, "numpy is not installed")
class TestAChangedChunkIsEmbeddedAgain(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc108_")
        self.arc_path = os.path.join(self.dir, "archive.db")
        self._orig = search_cli.CHUNK_CACHE
        search_cli.CHUNK_CACHE = os.path.join(self.dir, "chunk_vectors.npz")
        arc.init_archive(self.arc_path)
        self.conn = arc.get_archive_db(self.arc_path)
        for key, direction, body in (("1", 0, "are you coming on friday"),
                                     ("2", 0, "yes, see you there")):
            arc.insert_message(self.conn, contact_id=7, channel="imessage",
                               direction=direction, ts=f"2026-06-01 10:0{key}:00",
                               body=body, source_key=key)
        self.conn.commit()
        arc.build_chunks(self.conn, contact_id=7)

    def tearDown(self):
        self.conn.close()
        search_cli.CHUNK_CACHE = self._orig

    def rows(self):
        return self.conn.execute("SELECT * FROM chunks ORDER BY id").fetchall()

    def load(self, model):
        return search_cli._load_or_build_chunk_vectors(self.rows(), model, np)

    def flip_the_second_message(self):
        self.conn.execute("UPDATE messages SET direction = 1 WHERE source_key = '2'")
        self.conn.commit()
        arc.build_chunks(self.conn, contact_id=7)

    def test_the_premise_the_hash_does_not_see_the_flip(self):
        before = self.rows()[0]
        self.flip_the_second_message()
        after = self.rows()[0]
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertNotEqual(before["text"], after["text"])
        self.assertIn("Me: yes, see you there", after["text"])

    def test_an_unchanged_chunk_is_not_encoded_twice(self):
        first, second = _Model(), _Model()
        self.load(first)
        self.load(second)
        self.assertEqual(len(first.asked), 1)
        self.assertEqual(second.asked, [], "the cache was ignored")

    def test_a_flipped_direction_is_encoded_again_from_the_new_text(self):
        self.load(_Model())
        self.flip_the_second_message()
        model = _Model()
        vecs = self.load(model)
        self.assertEqual(model.asked, [[self.rows()[0]["text"]]],
                         "the chunk's text changed and the old vector was reused")
        self.assertEqual(vecs[0][0], float(len(self.rows()[0]["text"])))

    def test_a_cache_from_before_this_is_kept_not_thrown_away(self):
        """An older cache has no text fingerprints. Throwing it away would mean
        re-encoding every chunk of a large archive for no change."""
        vec = np.array([[3.0, 1.0]])
        np.savez(search_cli.CHUNK_CACHE, vecs=vec,
                 hashes=np.array([self.rows()[0]["content_hash"]], dtype=object))
        model = _Model()
        self.load(model)
        self.assertEqual(model.asked, [])
        saved = np.load(search_cli.CHUNK_CACHE, allow_pickle=True)
        self.assertIn("said", saved.files, "the fingerprint should start from now")


if __name__ == "__main__":
    unittest.main()
