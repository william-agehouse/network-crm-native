"""NC-129 gate: several phrasings of one question cost one round, not three.

A question like "Who do I know that bakes sourdough, rye or spelt?" used to cost
three times what it should, and none of that was the answer. It was three
`semantic_search` calls over near-identical ground, each returning the full
60-row cap: 180 contact cards and three model rounds for a question whose answer
is a shortlist. Every round re-sends the conversation, and cache writes bill at
1.25x, so the bill was in the ROUNDS. NC-127 had already learned this one tool
over, for search_messages.

Two things are pinned here, and the second is the one that is easy to get wrong.

  * The merge. Several phrasings, one ranked list, each person once at their
    best-scoring phrasing, and `matched` saying which phrasing that was.

  * THE FLOOR. Cosine scores are not comparable between phrasings, so a plain
    global top-N does not rank the phrasings against each other — it erases the
    quietest one. Under one global cut, a phrasing the embedding model finds
    unfamiliar can keep 2 of its own top 10 while the other two phrasings keep
    10 of 10. A third of the question, silently gone. So half the budget is
    shared out as a floor, one slice per phrasing, and the rest goes to global
    best score.

How large the cap is stays a setting of its own; this file pins only that the
cap bounds the whole answer, not each phrasing.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, code_of, db  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import chat  # noqa: E402
import search_cli  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")


class _PickerModel:
    """encode() returns one row of the identity per term, so the "vectors" ARE
    the score matrix and every number in these tests is one a person chose and
    can read. A term the table does not name scores zero against everyone.
    """

    def __init__(self, np, columns):
        self.np = np
        self.columns = columns

    def encode(self, terms, **_kw):
        eye = self.np.eye(len(self.columns))
        rows = [eye[self.columns.index(t)] if t in self.columns
                else self.np.zeros(len(self.columns)) for t in terms]
        return self.np.array(rows)


class TestSeveralPhrasingsAreOneSearch(Base):
    """Five people, three phrasings, scores chosen by hand.

        term 0 ("loud")    term 1 ("middling")   term 2 ("quiet")
    A      0.90                0.10                 0.31
    B      0.80                0.20                 0.30
    C      0.10                0.70                 0.29
    D      0.05                0.60                 0.28
    E      0.01                0.02                 0.32   <- term 2's own best

    Term 2 scores low against everyone, which is what a phrasing the embedding
    model finds unfamiliar looks like. E is its top match and would be ranked
    7th overall — invisible under a global top-4.
    """

    SCORES = [[0.90, 0.10, 0.31],
              [0.80, 0.20, 0.30],
              [0.10, 0.70, 0.29],
              [0.05, 0.60, 0.28],
              [0.01, 0.02, 0.32]]
    TERMS = ["loud", "middling", "quiet"]

    def setUp(self):
        super().setUp()
        import numpy as np
        self.np = np
        self.ids = [db.create_contact(self.conn, n)
                    for n in ("A One", "B Two", "C Three", "D Four", "E Five")]
        self.conn.commit()
        self.patches = [
            mock.patch.object(search_cli, "_get_model",
                              lambda: _PickerModel(np, self.TERMS)),
            mock.patch.object(search_cli, "_load_or_build_vectors",
                              lambda conn, ids, model, np_: np.array(self.SCORES)),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def rank(self, terms, limit):
        return search_cli.semantic_rank_many(self.conn, terms, limit=limit)

    def test_each_person_appears_once(self):
        out = self.rank(self.TERMS, 5)
        cids = [cid for _s, cid, _t in out]
        self.assertEqual(len(cids), len(set(cids)),
                         "the merged list repeats people, so the model pays for "
                         "the same contact card several times over — which is "
                         "most of what the three separate calls were doing")

    def test_each_person_carries_their_best_score(self):
        out = {cid: s for s, cid, _t in self.rank(self.TERMS, 5)}
        self.assertAlmostEqual(out[self.ids[0]], 0.90, places=6)
        self.assertAlmostEqual(out[self.ids[2]], 0.70, places=6)
        self.assertAlmostEqual(out[self.ids[4]], 0.32, places=6,
                               msg="a person is being scored by a phrasing that "
                                   "is not their best match")

    def test_matched_names_the_phrasing_that_found_them(self):
        out = {cid: t for _s, cid, t in self.rank(self.TERMS, 5)}
        self.assertEqual(out[self.ids[0]], "loud")
        self.assertEqual(out[self.ids[2]], "middling")
        self.assertEqual(out[self.ids[4]], "quiet",
                         "`matched` is the only account a merged ranking gives "
                         "of why someone is in it")

    def test_the_quiet_phrasing_is_not_erased_by_the_others(self):
        """THE one that matters. A global top-4 would be A, B, C, D and E would
        be gone, taking the third of the question that asked for her with it."""
        out = [cid for _s, cid, _t in self.rank(self.TERMS, 4)]
        self.assertIn(self.ids[4], out,
                      "the lowest-scoring phrasing lost every slot to the other "
                      "two. Scores are not comparable between phrasings, so that "
                      "is not a ranking — it is a third of the question being "
                      "dropped without saying so")

    def test_every_phrasing_keeps_its_own_top_match(self):
        out = {cid for _s, cid, _t in self.rank(self.TERMS, 4)}
        for t in range(3):
            best = max(range(5), key=lambda r: self.SCORES[r][t])
            self.assertIn(self.ids[best], out,
                          f"phrasing {self.TERMS[t]!r} did not keep even its own "
                          "single best match")

    def test_the_cap_bounds_the_answer_not_each_phrasing(self):
        """The whole cost fix in one assertion. Three phrasings used to mean
        three times the cap in cards.

        limit=2 with three phrasings is the case that actually holds the final
        slice honest: the floor is never less than one per phrasing, so it wants
        to hand back three rows for a cap of two, and only the trim stops it.
        A cap the caller set is a cap.
        """
        self.assertEqual(len(self.rank(self.TERMS, 2)), 2)
        self.assertEqual(len(self.rank(self.TERMS, 3)), 3)
        self.assertEqual(len(self.rank(self.TERMS, 5)), 5)

    def test_the_list_is_ordered_by_score_whichever_door_they_came_in(self):
        scores = [s for s, _cid, _t in self.rank(self.TERMS, 5)]
        self.assertEqual(scores, sorted(scores, reverse=True),
                         "the floor has left the merged list out of order, so "
                         "the model reads a ranking that is not one")

    def test_one_phrasing_behaves_exactly_as_it_did_before(self):
        """semantic_rank is now a wrapper. If the wrapper changed the answer,
        every other caller of it changed too — the CLI, and Ask's own
        single-description searches."""
        for limit in (1, 3, 5):
            many = [cid for _s, cid, _t
                    in search_cli.semantic_rank_many(self.conn, ["loud"], limit=limit)]
            one = [cid for _s, cid in
                   search_cli.semantic_rank(self.conn, "loud", limit=limit)]
            self.assertEqual(one, many)
            # "loud" scores A > B > C > D > E, so a single phrasing is still
            # plainly its own ranking, head first, with no floor reordering it.
            self.assertEqual(one, self.ids[:limit])

    def test_blank_and_duplicate_phrasings_are_dropped(self):
        out = search_cli.semantic_rank_many(self.conn, ["loud", "  ", ""], limit=5)
        self.assertTrue(all(t == "loud" for _s, _c, t in out))
        self.assertEqual(search_cli.semantic_rank_many(self.conn, [], limit=5), [])


class TestTheAskToolTakesTheList(Base):
    def test_the_handler_accepts_queries_and_reports_them(self):
        with mock.patch.object(search_cli, "embeddings_available", lambda: True), \
             mock.patch.object(search_cli, "semantic_rank_many",
                               lambda conn, terms, limit: []) as _:
            out = chat._semantic_search(self.conn, "", limit=5,
                                        queries=["a", "b", "a"])
        self.assertEqual(out["queries"], ["a", "b"],
                         "the handler no longer echoes the phrasings it used, "
                         "so the receipt cannot show what was searched for")

    def test_the_dispatcher_passes_the_list_through(self):
        src = code_of(os.path.join(CRM, "chat.py"))
        i = src.index('if name == "semantic_search":\n        return _semantic_search')
        self.assertIn("queries=tool_input", src[i:i + 400],
                      "the tool schema offers `queries` and the dispatcher drops "
                      "it, so the model batches its phrasings and only the first "
                      "is searched — worse than not offering it")

    def test_the_trail_shows_every_phrasing(self):
        detail = chat._tool_detail("semantic_search",
                                   {"query": "one", "queries": ["two", "three"]})
        for term in ("one", "two", "three"):
            self.assertIn(term, detail,
                          "the provenance trail shows one phrasing as if it were "
                          "the whole search")

    def test_the_tool_schema_declares_queries(self):
        spec = next(t for t in chat.TOOLS if t["name"] == "semantic_search")
        props = spec["input_schema"]["properties"]
        self.assertIn("queries", props)
        self.assertEqual(props["queries"]["type"], "array")
        self.assertNotIn("required", spec["input_schema"],
                         "`query` is required again, so a call that sends only "
                         "`queries` is rejected by the API before it runs")
        self.assertIn("ONE CALL", spec["description"],
                      "the description no longer tells the model to batch its "
                      "phrasings, which is the entire saving")

    def test_the_system_prompt_says_it_too(self):
        self.assertIn("ONE semantic_search", chat.SYSTEM_PROMPT,
                      "the prompt no longer tells the model that a multi-facet "
                      "question is one search. The tool description alone did "
                      "not change the behaviour for search_messages either")


if __name__ == "__main__":
    unittest.main()
