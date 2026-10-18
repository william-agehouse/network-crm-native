"""Semantic search: the embedding cache's incrementality and chunk ranking, both against a deterministic stub model."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    arc, db, search_cli,
)


class _StubModel:
    """Deterministic fake embedding model — lets tests exercise the cache's
    incrementality logic without loading the real sentence-transformers model."""

    def __init__(self):
        self.calls: list = []

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        import numpy as np
        texts = list(texts)
        self.calls.append(texts)
        return np.array([[float(len(t)), float(sum(map(ord, t)) % 97)] for t in texts])


@unittest.skipUnless(search_cli.embeddings_available(), "numpy/sentence-transformers not installed")
class TestChunkVectorCache(unittest.TestCase):
    """search_cli._load_or_build_chunk_vectors: only new/changed hashes re-encode."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chunkvec_test_")
        self._orig_cache = search_cli.CHUNK_CACHE
        search_cli.CHUNK_CACHE = os.path.join(self.dir, "chunk_vectors.npz")

    def tearDown(self):
        search_cli.CHUNK_CACHE = self._orig_cache

    @staticmethod
    def _row(content_hash, text):
        return {"content_hash": content_hash, "text": text}

    def test_only_new_hashes_are_encoded(self):
        import numpy as np
        model = _StubModel()
        rows = [self._row("h1", "hello world"), self._row("h2", "second chunk")]
        search_cli._load_or_build_chunk_vectors(rows, model, np)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(model.calls[0]), 2)

        rows2 = rows + [self._row("h3", "third chunk")]
        vecs2 = search_cli._load_or_build_chunk_vectors(rows2, model, np)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[1], ["third chunk"])  # only the new one
        self.assertEqual(vecs2.shape[0], 3)

    def test_unchanged_hash_reuses_cached_vector_no_reencode(self):
        import numpy as np
        model = _StubModel()
        rows = [self._row("h1", "hello world")]
        v1 = search_cli._load_or_build_chunk_vectors(rows, model, np)
        v2 = search_cli._load_or_build_chunk_vectors(rows, model, np)
        self.assertEqual(len(model.calls), 1)  # second call encoded nothing
        np.testing.assert_array_equal(v1, v2)


@unittest.skipUnless(search_cli.embeddings_available(), "numpy/sentence-transformers not installed")
class TestTheCacheIsWrittenOnlyWhenItChanges(unittest.TestCase):
    """NC-213. Every search used to write the whole vector file again (a large
    file on a large archive), even when nothing in it had changed."""

    def setUp(self):
        from unittest import mock
        self.dir = tempfile.mkdtemp(prefix="chunkvec_write_")
        p = mock.patch.object(search_cli, "CHUNK_CACHE", os.path.join(self.dir, "chunk_vectors.npz"))
        p.start()
        self.addCleanup(p.stop)

    def writes(self, rows):
        import numpy as np
        from unittest import mock
        real = np.savez
        with mock.patch.object(np, "savez", side_effect=real) as savez:
            search_cli._load_or_build_chunk_vectors(rows, _StubModel(), np)
        return savez.call_count

    @staticmethod
    def rows(*pairs):
        return [{"content_hash": h, "text": t} for h, t in pairs]

    def test_a_repeat_search_writes_nothing(self):
        rows = self.rows(("h1", "hello world"), ("h2", "second chunk"))
        self.assertEqual(self.writes(rows), 1, "the first search builds the file")
        self.assertEqual(self.writes(rows), 0)

    def test_a_new_chunk_is_written(self):
        self.writes(self.rows(("h1", "hello world")))
        self.assertEqual(self.writes(self.rows(("h1", "hello world"), ("h2", "new"))), 1)

    def test_a_chunk_that_is_gone_is_written_out(self):
        self.writes(self.rows(("h1", "hello world"), ("h2", "second chunk")))
        self.assertEqual(self.writes(self.rows(("h1", "hello world"))), 1,
                         "so the file does not keep vectors for chunks that no longer exist")

    def test_changed_text_under_the_same_messages_is_written(self):
        self.writes(self.rows(("h1", "Me: hello")))
        self.assertEqual(self.writes(self.rows(("h1", "Them: hello"))), 1)

    def test_an_old_cache_without_fingerprints_gets_them_once(self):
        import numpy as np
        np.savez(search_cli.CHUNK_CACHE, vecs=np.array([[1.0, 2.0]]),
                 hashes=np.array(["h1"], dtype=object))
        rows = self.rows(("h1", "hello world"))
        self.assertEqual(self.writes(rows), 1)
        self.assertEqual(self.writes(rows), 0)


@unittest.skipUnless(search_cli.embeddings_available(), "numpy/sentence-transformers not installed")
class TestChunkSemanticRank(unittest.TestCase):
    """search_cli.chunk_semantic_rank: ranking + excluded-contact filtering."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chunkrank_test_")
        self.arc_path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.arc_path)
        self.arc_conn = arc.get_archive_db(self.arc_path)
        self.crm_path = os.path.join(self.dir, "crm.db")
        db.init_db(self.crm_path)
        self.crm = db.get_db(self.crm_path)
        self._orig_cache = search_cli.CHUNK_CACHE
        search_cli.CHUNK_CACHE = os.path.join(self.dir, "chunk_vectors.npz")
        self._orig_get_model = search_cli._get_model
        search_cli._get_model = lambda: _StubModel()

    def tearDown(self):
        self.arc_conn.close()
        self.crm.close()
        search_cli.CHUNK_CACHE = self._orig_cache
        search_cli._get_model = self._orig_get_model

    def test_excludes_excluded_contacts(self):
        cid_a = db.create_contact(self.crm, "Alice")
        cid_b = db.create_contact(self.crm, "Bob")
        db.set_excluded(self.crm, cid_b, 1)
        self.crm.commit()
        arc.insert_message(self.arc_conn, contact_id=cid_a, channel="whatsapp",
                           direction=0, ts="2026-05-01 10:00:00", body="hello there",
                           source_key="1")
        arc.insert_message(self.arc_conn, contact_id=cid_b, channel="whatsapp",
                           direction=0, ts="2026-05-01 10:00:00", body="hello there",
                           source_key="2")
        self.arc_conn.commit()
        arc.build_chunks(self.arc_conn)
        ranked = search_cli.chunk_semantic_rank(self.arc_conn, "hello", crm_conn=self.crm)
        ranked_contacts = {row["contact_id"] for _, row in ranked}
        self.assertIn(cid_a, ranked_contacts)
        self.assertNotIn(cid_b, ranked_contacts)

    def test_empty_chunks_returns_empty(self):
        ranked = search_cli.chunk_semantic_rank(self.arc_conn, "anything", crm_conn=self.crm)
        self.assertEqual(ranked, [])
