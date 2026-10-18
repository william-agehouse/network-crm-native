"""
Search your network from the Mac — this is the "look things up later" tool that
runs on your own machine, not in the cloud.

Two modes:
  keyword   (default)  fast SQL search across names, roles, notes, summaries
  semantic  (--semantic) meaning-based search using sentence-transformers,
            e.g. "people in fintech I met at a conference". Embeddings are
            cached next to crm.db and rebuilt when the data changes.

Usage:
    python search_cli.py "acme"
    python search_cli.py --semantic "founders I should reconnect with"
    python search_cli.py --stats
"""

import hashlib
import importlib.util
import os
import sys

import cli_guard
import config  # noqa: F401 — loads .env
import db
import figures

CACHE = config.data_path("crm_vectors.npz")
CHUNK_CACHE = config.data_path("chunk_vectors.npz")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")

# The SentenceTransformer is expensive to construct, so cache the instance for the
# lifetime of the process (the desktop app reuses it across requests).
_MODEL = None


def embeddings_installed() -> bool:
    """Are the packages for search by meaning on this Mac? Answered without
    importing them — NC-221.

    embeddings_available() answers by importing sentence_transformers, which
    loads PyTorch, which costs seconds of start-up and hundreds of megabytes of
    memory. The Settings page would pay that the first time it opened just to
    print "On", and the server would keep the memory for as long as it ran. A
    status line only needs to know the packages are there. Anything
    that actually searches still goes through embeddings_available(), so a
    broken install falls back to keyword search exactly as before."""
    if "sentence_transformers" in sys.modules:
        return True
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("numpy", "sentence_transformers"))
    except (ImportError, ValueError):
        return False


def embeddings_available() -> bool:
    """True if the optional semantic-search dependencies are installed."""
    try:
        import numpy  # noqa: F401
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(EMBED_MODEL)
    return _MODEL


def contact_text(conn, cid: int) -> str:
    c = db.get_contact(conn, cid)
    bits = [c["name"], c["position"], c["company"], c["category"],
            c["how_we_met"], c["auto_summary"]]
    bits += [n["body"] for n in db.get_notes(conn, cid)]
    return " ".join(b for b in bits if b)


def keyword_search(query: str, limit: int = 15):
    conn = db.get_db()
    try:
        q = f"%{db.normalize_name(query)}%"
        like = f"%{query.lower()}%"
        rows = conn.execute(
            """SELECT DISTINCT c.* FROM contacts c
               LEFT JOIN notes n ON n.contact_id = c.id
               WHERE c.name_norm LIKE ?
                  OR lower(c.company)  LIKE ?
                  OR lower(c.position) LIKE ?
                  OR lower(c.how_we_met) LIKE ?
                  OR lower(c.auto_summary) LIKE ?
                  OR lower(n.body) LIKE ?
               ORDER BY c.last_contact DESC
               LIMIT ?""",
            (q, like, like, like, like, like, limit),
        ).fetchall()
        _print_rows(conn, rows)
    finally:
        conn.close()


# NC-145. Both rankers here feed Ask (crm/chat.py) as well as two local views,
# and each used to carry its own `WHERE excluded = 1`. 'active' matches what
# chat.py asks for, since a ranked result that reaches Anthropic must not carry
# somebody the user deleted; the two local callers want the same answer.
RANK_POPULATION = "active"


def semantic_rank_many(conn, queries, limit: int = 15):
    """Rank contacts against SEVERAL phrasings of the same question, in one pass.

    Returns (score, contact_id, phrasing) triples, highest first, each contact
    ONCE with whichever phrasing scored it best. Embeddings are built over ALL
    contacts and cached in crm_vectors.npz (so the cache stays consistent
    between this and the CLI); excluded people are filtered from the RESULTS
    rather than from the cache.

    NC-129. A question with three facets was the dearest kind Ask could be
    asked, because one phrasing per call meant one call, one model round and a
    page of contact cards per facet, for a question whose answer is a shortlist.
    Each round rewrites the cached prompt prefix and cache WRITES bill at 1.25x,
    so the bill was in the rounds, never in the results. This is the same fix NC-127 made for search_messages, one tool
    over: the whole list arrives at once.

    Three phrasings cost one vectors load, one encode of three short strings
    and one matrix multiply instead of three of each — but that is a bonus.
    The round is what was expensive.
    """
    import numpy as np

    terms = [t.strip() for t in (queries or []) if (t or "").strip()]
    if not terms:
        return []
    ids = [r["id"] for r in conn.execute("SELECT id FROM contacts ORDER BY id")]
    if not ids:
        return []
    model = _get_model()
    vecs = _load_or_build_vectors(conn, ids, model, np)
    qv = model.encode(terms, normalize_embeddings=True)      # (terms, dim)
    scores = np.asarray(vecs) @ np.asarray(qv).T             # (contacts, terms)
    best = scores.max(axis=1)
    which = scores.argmax(axis=1)
    # NC-145: was hidden-only and hand-written here. Two of this function's
    # callers are Ask, which sends what it ranks to Anthropic.
    excluded = db.ids_outside(conn, RANK_POPULATION)

    # WHY EACH PHRASING GETS A GUARANTEED SLICE, RATHER THAN ONE GLOBAL TOP-N.
    #
    # Cosine scores are not comparable BETWEEN phrasings. Take three phrasings of
    # one three-part question ("who works with bakeries, cafés or restaurants?"):
    # a plain global top-60 can keep the first phrasing's own top ten whole and
    # the second's too — and only a couple of the third's, a broad one ("food,
    # hospitality, catering"). That third phrasing simply scores lower against
    # every contact, so under one global cut it is not out-ranked, it is erased,
    # and the answer loses a whole facet of the question that was asked.
    #
    # So: half the budget is shared out as a floor, one slice per phrasing, and
    # the rest is filled by global best score. With one phrasing the floor is
    # just the head of the same list, so a single-query search is bit-for-bit
    # what it was before.
    floor = max(1, limit // (2 * len(terms))) if limit > 0 else 0

    taken, out = set(), []

    def _take(i):
        cid = ids[i]
        if cid in excluded or cid in taken:
            return False
        taken.add(cid)
        out.append((float(best[i]), cid, terms[int(which[i])]))
        return True

    if len(terms) > 1 and floor:
        for t in range(len(terms)):
            kept = 0
            for i in scores[:, t].argsort()[::-1]:
                if kept >= floor:
                    break
                if _take(int(i)):
                    kept += 1

    for i in best.argsort()[::-1]:
        if limit > 0 and len(out) >= limit:
            break
        _take(int(i))

    # One list, highest first, whichever door each person came in through.
    out.sort(key=lambda r: -r[0])
    return out[:limit] if limit > 0 else out


def semantic_rank(conn, query: str, limit: int = 15):
    """One phrasing. `semantic_rank_many` with a list of one, so there is a
    single ranker to reason about and a single place the exclusion rule lives."""
    return [(score, cid)
            for score, cid, _term in semantic_rank_many(conn, [query], limit=limit)]


def semantic_search(query: str, limit: int = 15):
    if not embeddings_available():
        raise SystemExit("Semantic search needs: pip install sentence-transformers numpy")
    conn = db.get_db()
    try:
        ranked = semantic_rank(conn, query, limit=limit)
        if not ranked:
            print("No contacts yet — run seed_from_csv.py first.")
            return
        for rank, (score, cid) in enumerate(ranked, 1):
            row = db.get_contact(conn, cid)
            print(f"{rank:2d}. [{score:.2f}] {row['name']} — "
                  f"{row['position'] or ''} {('· ' + row['company']) if row['company'] else ''}")
            if row["auto_summary"]:
                print(f"      {row['auto_summary'][:120]}")
    finally:
        conn.close()


def chunk_semantic_rank(arc_conn, query: str, limit: int = 15, crm_conn=None):
    """Rank archived conversation chunks by meaning-similarity to `query`.

    Returns (score, chunk_row) tuples, highest first. Unlike the per-contact
    card cache, the chunk embedding cache is keyed by content_hash (see
    _load_or_build_chunk_vectors), so re-running this after a fresh import
    only encodes chunks that are new or actually changed. If crm_conn is
    given, contacts marked excluded are filtered from the results."""
    import numpy as np

    rows = arc_conn.execute("SELECT * FROM chunks ORDER BY id").fetchall()
    if not rows:
        return []
    model = _get_model()
    vecs = _load_or_build_chunk_vectors(rows, model, np)
    qv = model.encode([query], normalize_embeddings=True)[0]
    scores = vecs @ qv
    order = scores.argsort()[::-1]
    excluded = set()
    if crm_conn is not None:
        excluded = db.ids_outside(crm_conn, RANK_POPULATION)   # NC-145, as above
    out = []
    for i in order:
        row = rows[i]
        if row["contact_id"] in excluded:
            continue
        out.append((float(scores[i]), row))
        if len(out) >= limit:
            break
    return out


def _text_print(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _load_or_build_chunk_vectors(rows, model, np):
    """Incremental embedding cache for conversation chunks, keyed by
    content_hash. Only chunks whose hash isn't already cached get re-encoded —
    rebuilding chunks for one contact after a re-import doesn't cost a full
    re-embed of the whole archive.

    NC-108. `content_hash` says WHICH messages a chunk holds, not what its text
    says, and the text can change under the same messages: correct who sent a
    message and its line flips between "Me:" and "Them:" with the hash
    unchanged. So the cache also keeps a fingerprint of the text each vector was
    built from (`said`), and a chunk whose text no longer matches is encoded
    again. Hashing the text into content_hash instead would have thrown away
    every cached vector at once, a full re-embed for no change.

    A cache written before this has no fingerprints. Its vectors are kept, and
    the fingerprint starts from the text as it is now. A chunk whose text changed
    under its hash and was never embedded has nothing stale to carry over.
    """
    hashes = [r["content_hash"] for r in rows]
    said = [_text_print(r["text"]) for r in rows]
    hash_to_vec, hash_to_said = {}, {}
    had_said = False
    if os.path.exists(CHUNK_CACHE):
        data = np.load(CHUNK_CACHE, allow_pickle=True)
        had_said = "said" in data.files
        old_said = data["said"] if had_said else [None] * len(data["hashes"])
        for h, v, t in zip(data["hashes"], data["vecs"], old_said):
            hash_to_vec[str(h)] = v
            hash_to_said[str(h)] = None if t is None else str(t)
    cached = set(hash_to_vec)

    missing_idx = [i for i, h in enumerate(hashes)
                   if h not in hash_to_vec
                   or hash_to_said.get(h) not in (None, said[i])]
    if missing_idx:
        print(f"Embedding {len(missing_idx)} new/changed conversation chunk(s)…")
        texts = [rows[i]["text"] for i in missing_idx]
        new_vecs = model.encode(texts, normalize_embeddings=True,
                                show_progress_bar=len(texts) > 20)
        for i, v in zip(missing_idx, new_vecs):
            hash_to_vec[hashes[i]] = v

    vecs = np.array([hash_to_vec[h] for h in hashes])
    # Only keep cache entries for hashes that are still live, so the file
    # doesn't grow unboundedly as old chunks get superseded by rebuilds.
    #
    # NC-213. And write it only when that changes something. Written on every
    # search, it would rewrite the whole file (tens of megabytes on a large
    # archive) each time, when nothing in it had changed.
    if missing_idx or not had_said or cached != set(hashes):
        np.savez(CHUNK_CACHE, vecs=vecs, hashes=np.array(hashes, dtype=object),
                 said=np.array(said, dtype=object))
    return vecs


def _load_or_build_vectors(conn, ids, model, np):
    sig = conn.execute("SELECT COALESCE(MAX(updated_at),'') || COUNT(*) FROM contacts").fetchone()[0]
    if os.path.exists(CACHE):
        data = np.load(CACHE, allow_pickle=True)
        if str(data.get("sig")) == sig and len(data["vecs"]) == len(ids):
            return data["vecs"]
    print("Building embeddings (first run / data changed)…")
    texts = [contact_text(conn, cid) for cid in ids]
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
    np.savez(CACHE, vecs=vecs, sig=sig)
    return vecs


def _print_rows(conn, rows):
    if not rows:
        print("No matches.")
        return
    for r in rows:
        role = " · ".join(x for x in (r["position"], r["company"]) if x)
        extra = f" — {role}" if role else ""
        meta = []
        if r["category"]:
            meta.append(r["category"])
        if r["msg_count"]:
            meta.append(f"{r['msg_count']} msgs")
        if r["last_contact"]:
            meta.append(f"last {r['last_contact'][:10]}")
        tail = f"  ({', '.join(meta)})" if meta else ""
        print(f"• {r['name']}{extra}{tail}")
        notes = db.get_notes(conn, r["id"])
        if notes:
            print(f"    ↳ {notes[0]['body'][:140]}")


def stats():
    """NC-087. "With message history" used to count LinkedIn's column alone, over
    every row including people you hid or deleted, under a label that claims every
    channel. Both lines now come from db.source_coverage(), the figures the Data
    page shows, over the same people the People page lists."""
    conn = db.get_db()
    try:
        cov = db.source_coverage(conn)
        notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()
    print(f"People: {cov['people']:,}  ({figures.population_says('active')})")
    print(f"People you have messages with, on any channel: {cov['history']:,}")
    print(f"Notes written: {notes:,}")


USAGE = """Usage: search_cli.py [--semantic] WORDS…   or   search_cli.py --stats

Searches your contacts and messages for WORDS. With no words, or --stats,
prints what the search index holds.
  --semantic  rank by meaning (needs the optional search packages)"""


def main():
    cli_guard.check(sys.argv[1:], USAGE, flags=("--semantic", "--stats"))
    args = sys.argv[1:]
    if not args or "--stats" in args:
        return stats()
    semantic = "--semantic" in args
    query = " ".join(a for a in args if not a.startswith("--"))
    if not query:
        return stats()
    (semantic_search if semantic else keyword_search)(query)


if __name__ == "__main__":
    main()
