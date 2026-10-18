"""NC-126: the search finds the words; the answer is in the next message.

THE BUG, in one example exchange. Asked for a booking reference that was sent
in a chat, a search for "bokningsreferens" (Swedish for booking reference) finds
the question and reports that it has no number in it. Every part of that is true
and the answer is still wrong — the number is the VERY NEXT MESSAGE, inside the
same minute:

    14:12  me     "Vad är bokningsreferensen förresten?"
    14:12  them   "2468"

A number like that arrives as a bare message with no words in it at all, so no
keyword can ever reach it. The only thing that identifies it is what it sits
next to. A search that returns the match alone has the answer one row away and
cannot see it, and it also misses that the reference CHANGED — the old one
appears in a message that happens to say "referens", the new one never does.

And the person lookup was worse than the search. There was no tool that
searched contact NAMES, so for "my longtime contact Ture" the model had to guess
with text searches: semantic searches over profile text for the phrase "longtime
contact" (nobody's profile says that), or a search of message TEXT for "Ture",
which finds every message MENTIONING a Ture and returns dozens of people.
find_person exists to identify one contact in one call.

WHAT THESE GUARD

  * a question and its answer come back together, including when they share a
    timestamp to the second — the tie-break on id is the whole case, not
    defensive padding
  * neighbours never cross a conversation boundary, so a group-chat reply is
    never offered as context for a private message
  * a match is never also printed as somebody else's neighbour, and two nearby
    matches share the messages between them instead of repeating them
  * the tool path defaults context ON. This is the exact trap include_groups
    fell into in NC-100: `int(x.get("context") or 0)` turns a missing key into
    "off", so the schema default and the function default disagree and the
    feature is dead through the only path that matters
  * find_person ranks by how much relationship there is, so "my contact Ture"
    resolves to the Ture with years of history rather than the first row
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, arc, chat_mod as chat, db  # noqa: E402


class CtxBase(Base):
    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="ncrm-ctxarc-")
        self.arc_path = os.path.join(self.arc_dir, "archive.db")
        arc.init_archive(self.arc_path)
        self.ac = arc.get_archive_db(self.arc_path)
        self._patch = mock.patch.object(arc, "ARCHIVE_DB_PATH", self.arc_path)
        self._patch.start()
        self.n = 0

    def tearDown(self):
        self._patch.stop()
        self.ac.close()
        shutil.rmtree(self.arc_dir, True)
        super().tearDown()

    def person(self, name, excluded=0):
        cid = db.create_contact(self.conn, name)
        if excluded:
            db.set_excluded(self.conn, cid, 1)
        return cid

    def msg(self, cid, ts, body, direction=1, channel="imessage", group="",
            group_key=""):
        self.n += 1
        arc.insert_message(self.ac, contact_id=cid, channel=channel,
                           direction=direction, ts=ts, body=body,
                           source_key=f"k{self.n}", group_name=group,
                           group_key=group_key)
        self.ac.commit()

    def texts(self, item):
        """Context entries are single strings ("14:05 them: …"), not objects —
        NC-127 dropped the three key names, which cost more tokens per
        neighbour than the message text they labelled."""
        return list(item.get("before", [])) + list(item.get("after", []))


class TestTheAnswerIsInTheNextMessage(CtxBase):
    """The booking-reference case: question, bare answer, and an older value."""

    def setUp(self):
        super().setUp()
        self.j = self.person("Ture Exempelsson")
        # The old reference, in a message that happens to say "referens".
        self.msg(self.j, "2029-11-03 12:00:00", "8642 är referensen", direction=0)
        # The new reference. The question and the answer share a timestamp to
        # the second — message archives have this shape, and it is what makes the
        # id tie-break load-bearing rather than decorative.
        self.msg(self.j, "2030-05-10 14:05:00", "Jag kollar", direction=0)
        self.msg(self.j, "2030-05-10 14:12:00", "Vad är bokningsreferensen förresten?",
                 direction=1)
        self.msg(self.j, "2030-05-10 14:12:00", "2468", direction=0)
        self.msg(self.j, "2030-05-10 16:40:00", "Tack!", direction=0)

    def test_the_bare_number_comes_back_with_the_question(self):
        r = chat._search_messages(self.conn, query="bokningsreferens", contact_id=self.j)
        self.assertEqual(r["total_matched"], 1)
        item = r["messages"][0]
        self.assertIn("2468", " ".join(self.texts(item)),
                      "the answer was one row away and did not come back — this "
                      "is the whole bug")

    def test_the_answer_is_after_the_question_not_before(self):
        r = chat._search_messages(self.conn, query="bokningsreferens", contact_id=self.j)
        item = r["messages"][0]
        self.assertIn("2468", " ".join(item["after"]))

    def test_neighbours_read_oldest_first(self):
        """Asserted against the known sequence, not a re-parse of the label.

        A neighbour on the match's own day is stamped with the time alone; one
        from another day carries its date. So the labels are deliberately not
        all the same shape and cannot be sorted as strings — comparing
        "2029-11-03" against "14:05" would call correct data wrong.
        """
        r = chat._search_messages(self.conn, query="bokningsreferens", contact_id=self.j)
        item = r["messages"][0]                       # newest match: 2030-05-10
        self.assertEqual(
            [e.split(" them:")[0].split(" me:")[0] for e in item["before"]],
            ["2029-11-03 12:00", "14:05"],
            "before must run oldest to newest, with the far-away one dated")
        self.assertTrue(item["after"][0].startswith("14:12"))

    def test_a_neighbour_from_another_day_carries_its_date(self):
        r = chat._search_messages(self.conn, query="bokningsreferens", contact_id=self.j)
        far = r["messages"][0]["before"][0]
        self.assertTrue(far.startswith("2029-11-03"),
                        "a neighbour months away that shows only a clock time "
                        "reads as though it happened in the same conversation")

    def test_a_neighbour_says_who_sent_it(self):
        r = chat._search_messages(self.conn, query="bokningsreferens", contact_id=self.j)
        answer = next(e for e in r["messages"][0]["after"] if "2468" in e)
        self.assertIn("them:", answer,
                      "a neighbour that does not say who sent it is unusable — "
                      "the whole point is that THEY sent the number")

    def test_the_search_alone_cannot_see_it(self):
        """Proves the fixture reproduces the bug rather than testing a tautology."""
        r = chat._search_messages(self.conn, query="bokningsreferens",
                                  contact_id=self.j, context=0)
        blob = " ".join(m["snippet"] for m in r["messages"])
        self.assertNotIn("2468", blob,
                         "with context off the number must be unreachable, or "
                         "this fixture is not the bug")

    def test_both_references_are_reachable_so_a_change_is_visible(self):
        r = chat._search_messages(self.conn, query="referens", contact_id=self.j)
        blob = " ".join(
            [m["snippet"] for m in r["messages"]]
            + [t for m in r["messages"] for t in self.texts(m)])
        self.assertIn("8642", blob)
        self.assertIn("2468", blob)


class TestNeighboursStayInTheirConversation(CtxBase):
    def setUp(self):
        super().setUp()
        self.a = self.person("Ada Lovelace")
        self.b = self.person("Grace Hopper")
        self.msg(self.a, "2026-03-01 09:00:00", "what's the reference", direction=1)
        # Same minute, different person: must never be offered as the answer.
        self.msg(self.b, "2026-03-01 09:00:01", "SOMEONE-ELSE-9999", direction=0)
        # Same minute, same person, but a group thread: also not the answer.
        self.msg(self.a, "2026-03-01 09:00:02", "GROUP-THREAD-8888",
                 direction=0, group="Project chat", group_key="imessage:9")
        self.msg(self.a, "2026-03-01 09:00:03", "4321", direction=0)

    def test_another_persons_message_is_not_context(self):
        r = chat._search_messages(self.conn, query="reference", contact_id=self.a)
        blob = " ".join(self.texts(r["messages"][0]))
        self.assertNotIn("SOMEONE-ELSE-9999", blob)

    def test_a_group_reply_is_not_context_for_a_private_message(self):
        r = chat._search_messages(self.conn, query="reference", contact_id=self.a)
        blob = " ".join(self.texts(r["messages"][0]))
        self.assertNotIn("GROUP-THREAD-8888", blob,
                         "a group message was offered as context for a private "
                         "one, which reads as though they said it to the owner alone")
        self.assertIn("4321", blob)

    def test_a_group_match_gets_its_own_thread_as_context(self):
        self.msg(self.a, "2026-04-01 09:00:00", "group question here",
                 direction=1, group="Project chat", group_key="imessage:9")
        self.msg(self.a, "2026-04-01 09:00:01", "GROUP-ANSWER-7777",
                 direction=0, group="Project chat", group_key="imessage:9")
        r = chat._search_messages(self.conn, query="group question")
        self.assertIn("GROUP-ANSWER-7777", " ".join(self.texts(r["messages"][0])))


class TestNothingIsPrintedTwice(CtxBase):
    def setUp(self):
        super().setUp()
        self.a = self.person("Ada Lovelace")
        for i in range(6):
            self.msg(self.a, f"2026-03-01 09:0{i}:00", f"ping {i}", direction=1)

    def test_a_match_is_never_also_a_neighbour(self):
        # "ping" matches all six; none may appear as another's context.
        r = chat._search_messages(self.conn, query="ping", contact_id=self.a)
        self.assertEqual(len(r["messages"]), 6)
        for item in r["messages"]:
            self.assertEqual(self.texts(item), [],
                             "a matched message was repeated as context")

    def test_two_nearby_matches_share_what_is_between_them(self):
        self.msg(self.a, "2026-05-01 09:00:00", "alpha", direction=1)
        self.msg(self.a, "2026-05-01 09:01:00", "MIDDLE-ONE", direction=0)
        self.msg(self.a, "2026-05-01 09:02:00", "alpha again", direction=1)
        r = chat._search_messages(self.conn, query="alpha", contact_id=self.a)
        seen = [t for m in r["messages"] for t in self.texts(m)]
        self.assertEqual(sum(1 for t in seen if "MIDDLE-ONE" in t), 1,
                         "the message between two matches came back twice")


class TestTheKnobs(CtxBase):
    def setUp(self):
        super().setUp()
        self.a = self.person("Ada Lovelace")
        for i in range(20):
            self.msg(self.a, f"2026-03-01 09:{i:02d}:00", f"line {i}",
                     direction=1)

    def test_context_zero_turns_it_off(self):
        r = chat._search_messages(self.conn, query="line 5", contact_id=self.a,
                                  context=0)
        self.assertEqual(self.texts(r["messages"][0]), [])
        self.assertNotIn("context_note", r)

    def test_span_is_clamped_to_the_maximum(self):
        r = chat._search_messages(self.conn, query="line 9", contact_id=self.a,
                                  context=999)
        self.assertEqual(r["filters"]["context"], chat._CTX_SPAN_MAX)
        self.assertLessEqual(len(self.texts(r["messages"][0])),
                             chat._CTX_SPAN_MAX * 2)

    def test_a_negative_span_is_off_not_a_crash(self):
        r = chat._search_messages(self.conn, query="line 9", contact_id=self.a,
                                  context=-3)
        self.assertEqual(r["filters"]["context"], 0)

    def _sweep(self, n_matches):
        """n matches, each followed by six non-matching messages.

        INTERLEAVED IN TIME: putting every match on one day and every filler on
        the next would make each match's nearest neighbours other matches,
        correctly deduped away, and 60 matches would produce one single
        neighbour. The fixture, not the code, would be wrong — and a budget test
        that never reaches the budget proves nothing.
        """
        who = self.person(f"Busy {n_matches}")
        t = 0

        def stamp(i):
            return f"2026-06-01 {(i // 60) % 24:02d}:{i % 60:02d}:00"

        for i in range(n_matches):
            self.msg(who, stamp(t), f"needle {i}", direction=1); t += 1
            for k in range(6):
                self.msg(who, stamp(t), f"filler {i}-{k}", direction=0); t += 1
        return who

    def test_a_big_sweep_tapers_the_span(self):
        """Neighbours are precious on a precise search and waste on a trawl.

        Four hits for "bokningsreferens" is a question; thirty is a sweep, and wrapping
        every one of those in six messages either side reprints the same
        conversation over and over.
        """
        who = self._sweep(30)
        r = chat._search_messages(self.conn, query="needle", contact_id=who,
                                  context=chat._CTX_SPAN_MAX, limit=40)
        self.assertEqual(r["filters"]["context"], 1,
                         "a 30-match sweep still asked for the full span")

    def test_the_neighbour_budget_is_enforced(self):
        """Asserted against what an UNBUDGETED run would return, not against
        the constant being tested.

        Comparing the total to chat._CTX_BUDGET would use the number under
        test — raise the constant and the assertion becomes trivially true, so
        deleting the budget entirely would pass. A gate that reads its own
        subject cannot fail.
        """
        who = self._sweep(60)
        r = chat._search_messages(self.conn, query="needle", contact_id=who,
                                  context=chat._CTX_SPAN_MAX, limit=70)
        n = len(r["messages"])
        total = sum(len(self.texts(m)) for m in r["messages"])
        # The taper has already cut the span to 1, so an unbudgeted run returns
        # exactly 2 per match. That product — not the budget constant — is what
        # this compares against, so deleting the budget makes it fail.
        self.assertGreater(n, 40, "fixture too small to bound anything")
        self.assertLess(total, n * 2,
                        f"{total} neighbours for {n} matches, which is every "
                        "one available — the budget is holding nothing back")
        self.assertIn("context_truncated", r,
                      "the budget truncated without saying so")

    def test_the_tool_path_defaults_context_on(self):
        """NC-100's trap, one field over.

        `int(tool_input.get("context") or 0)` makes a MISSING key mean "off", so
        the schema default and the function default disagree and context is dead
        through the only path the model uses. The model omits the key whenever it
        is happy with the default, which is almost always.
        """
        caps = {"message_limit": 20}
        r = chat.run_tool(self.conn, "search_messages",
                          {"query": "line 5", "contact_id": self.a}, caps)
        self.assertEqual(r["filters"]["context"], chat._CTX_SPAN_DEFAULT)
        self.assertTrue(self.texts(r["messages"][0]),
                        "context was off through the tool path")

    def test_the_tool_path_still_honours_an_explicit_zero(self):
        caps = {"message_limit": 20}
        r = chat.run_tool(self.conn, "search_messages",
                          {"query": "line 5", "contact_id": self.a,
                           "context": 0}, caps)
        self.assertEqual(r["filters"]["context"], 0)


class TestTheCostCuts(CtxBase):
    """NC-127. These were the three biggest costs."""

    def setUp(self):
        super().setUp()
        self.a = self.person("Ada Lovelace")
        for i in range(4):
            self.msg(self.a, f"2026-03-01 09:0{i}:00", f"note {i}", direction=1)

    def test_search_people_with_no_filters_is_refused(self):
        """Thousands of tokens on a large archive — the most expensive call available,
        and it answers nothing: an arbitrary 120 rows out of many. It was
        reached for as a fallback when a NAME could not be looked up, which
        find_person now does."""
        r = chat._search_people(self.conn)
        self.assertIn("error", r)
        self.assertEqual(r["contacts"], [])
        self.assertIn("find_person", r["error"],
                      "the refusal must say what to call instead, or the model "
                      "just tries something equally expensive")

    def test_a_real_filter_still_works(self):
        db.set_field(self.conn, self.a, "home_city", "London")
        r = chat._search_people(self.conn, location="London")
        self.assertNotIn("error", r)
        self.assertEqual([c["name"] for c in r["contacts"]], ["Ada Lovelace"])

    def test_a_scoped_search_says_who_once_instead_of_per_message(self):
        r = chat._search_messages(self.conn, query="note", contact_id=self.a)
        self.assertEqual(r["all_messages_are_with"]["contact_id"], self.a)
        self.assertEqual(r["all_messages_are_with"]["contact_name"], "Ada Lovelace")
        for m in r["messages"]:
            self.assertNotIn("contact_id", m,
                             "the person is repeated on every row again")

    def test_an_unscoped_search_still_names_every_row(self):
        b = self.person("Grace Hopper")
        self.msg(b, "2026-03-02 09:00:00", "note from grace", direction=0)
        r = chat._search_messages(self.conn, query="note")
        self.assertNotIn("all_messages_are_with", r)
        for m in r["messages"]:
            self.assertIn("contact_name", m,
                          "without a scope every row must still say who it is "
                          "with, or the model cannot attribute anything")

    def test_the_redundant_date_field_is_gone(self):
        r = chat._search_messages(self.conn, query="note", contact_id=self.a)
        m = r["messages"][0]
        self.assertNotIn("date", m, "`date` was `ts[:10]` — the same string twice")
        self.assertTrue(m["ts"].startswith("2026-03-01"))


class TestManySpellingsOneCall(CtxBase):
    """NC-127's real cost fix, and it is about ROUNDS, not result size.

    Trimming the result shapes cuts most of the tokens in a result and changes
    the bill by nothing, because the bill is not in the results. Every round
    re-sends the conversation and re-writes the cached prefix, and cache WRITES
    bill at 1.25x. A model told "a literal search finds only the word you gave
    it, so try the other spellings" obeys the only way it can: one call per
    spelling — bokningsreferens, referensen, referens, Bokningsreferensen,
    reference, 2468, 2469, 8642 — and
    several one-word searches cost one round each.

    Several spellings in one call is one round, with the same coverage.
    """

    def setUp(self):
        super().setUp()
        self.j = self.person("Ture Exempelsson")
        self.msg(self.j, "2029-11-03 12:00:00", "8642 är referensen", direction=0)
        self.msg(self.j, "2030-05-10 14:12:00", "Vad är bokningsreferensen förresten?",
                 direction=1)
        self.msg(self.j, "2030-05-10 14:12:01", "2468", direction=0)
        self.msg(self.j, "2026-04-12 10:00:00", "inget alls här", direction=0)

    def test_several_phrases_match_any_of_them(self):
        r = chat._search_messages(self.conn, queries=["bokningsreferens", "referensen", "2468"],
                                  contact_id=self.j)
        self.assertEqual(r["total_matched"], 3)
        blob = " ".join(m["snippet"] for m in r["messages"])
        for want in ("8642", "bokningsreferensen", "2468"):
            self.assertIn(want, blob)

    def test_it_is_the_same_answer_as_three_separate_calls(self):
        one = chat._search_messages(self.conn,
                                    queries=["bokningsreferens", "referensen", "2468"],
                                    contact_id=self.j, context=0)
        got = {m["ts"] for m in one["messages"]}
        sep = set()
        for q in ("bokningsreferens", "referensen", "2468"):
            sep |= {m["ts"] for m in chat._search_messages(
                self.conn, query=q, contact_id=self.j, context=0)["messages"]}
        self.assertEqual(got, sep, "one call must cover exactly what the "
                                   "separate calls covered, or it is not a "
                                   "cost fix, it is a behaviour change")

    def test_the_snippet_centres_on_whichever_phrase_hit(self):
        long_tail = "x" * 400 + " NEEDLE-HERE " + "y" * 400
        self.msg(self.j, "2026-09-01 09:00:00", long_tail, direction=0)
        r = chat._search_messages(self.conn,
                                  queries=["bokningsreferens", "NEEDLE-HERE"],
                                  contact_id=self.j)
        hit = next(m for m in r["messages"] if m["ts"].startswith("2026-09-01"))
        self.assertIn("NEEDLE-HERE", hit["snippet"],
                      "the window centred on the first phrase in the list "
                      "rather than the one this message actually contains")

    def test_phrases_are_deduped_case_insensitively(self):
        r = chat._search_messages(self.conn,
                                  queries=["referens", "REFERENS", " referens ", ""],
                                  contact_id=self.j, context=0)
        self.assertEqual(r["filters"]["query"], "referens")

    def test_query_and_queries_combine(self):
        r = chat._search_messages(self.conn, query="bokningsreferens", queries=["2468"],
                                  contact_id=self.j, context=0)
        self.assertEqual(r["total_matched"], 2)

    def test_like_wildcards_are_still_escaped_in_every_phrase(self):
        self.msg(self.j, "2026-09-02 09:00:00", "literally 100% sure",
                 direction=0)
        self.msg(self.j, "2026-09-03 09:00:00", "no percent sign here",
                 direction=0)
        r = chat._search_messages(self.conn, queries=["%", "100%"],
                                  contact_id=self.j, context=0)
        self.assertEqual(r["total_matched"], 1,
                         "a bare % matched the whole archive — a search that "
                         "cannot fail is worse than one that returns nothing")

    def test_the_tool_path_passes_the_list(self):
        r = chat.run_tool(self.conn, "search_messages",
                          {"queries": ["bokningsreferens", "2468"], "contact_id": self.j},
                          {"message_limit": 20})
        self.assertEqual(r["total_matched"], 2)

    def test_people_in_messages_takes_the_list_too(self):
        r = chat.run_tool(self.conn, "people_in_messages",
                          {"queries": ["bokningsreferens", "2468"]},
                          {"message_people_limit": 20})
        self.assertEqual(r["total_people"], 1)

    def test_the_trail_names_every_phrase_searched(self):
        line = chat._tool_detail("search_messages",
                                 {"queries": ["bokningsreferens", "referensen"],
                                  "contact_id": 254})
        self.assertIn("bokningsreferens", line)
        self.assertIn("referensen", line)


class TestFindPerson(CtxBase):
    def setUp(self):
        super().setUp()
        self.close = self.person("Ture Exempelsson")
        self.faint = self.person("Ture Holm")
        self.namesake = self.person("Ture")
        self.other = self.person("Ada Lovelace")
        for i in range(40):
            self.msg(self.close, f"2021-09-{(i % 28) + 1:02d} 09:00:00",
                     f"old chat {i}", direction=i % 2)
        for i in range(3):
            self.msg(self.faint, f"2026-06-{i + 1:02d} 09:00:00",
                     f"hello {i}", direction=0)
        # Group traffic must not read as a close one-to-one relationship.
        for i in range(50):
            self.msg(self.other, f"2026-07-{(i % 28) + 1:02d} 09:00:00",
                     f"group noise {i}", direction=0,
                     group="Project chat", group_key="imessage:5")

    def test_a_partial_first_name_finds_them(self):
        r = chat._find_person(self.conn, "Ture")
        names = [p["name"] for p in r["people"]]
        self.assertIn("Ture Exempelsson", names)
        self.assertIn("Ture Holm", names)
        self.assertNotIn("Ada Lovelace", names)

    def test_the_one_with_the_most_history_ranks_first(self):
        r = chat._find_person(self.conn, "Ture")
        self.assertEqual(r["people"][0]["name"], "Ture Exempelsson",
                         "'my longtime contact Ture' must resolve to the Ture "
                         "with years of history, not the first row")

    def test_an_exact_full_name_outranks_a_token_match(self):
        r = chat._find_person(self.conn, "Ture Holm")
        self.assertEqual(r["people"][0]["name"], "Ture Holm")

    def test_every_row_carries_what_longtime_means(self):
        r = chat._find_person(self.conn, "Ture Exempelsson")
        p = r["people"][0]
        for key in ("years_known", "known_since", "messages_one_to_one",
                    "messages_total", "last_message", "strength"):
            self.assertIn(key, p)
        self.assertEqual(p["messages_one_to_one"], 40)
        self.assertGreaterEqual(p["years_known"], 1.0)
        self.assertTrue(p["known_since"].startswith("2021-"))

    def test_group_traffic_is_not_one_to_one_history(self):
        r = chat._find_person(self.conn, "Ada")
        p = r["people"][0]
        self.assertEqual(p["messages_total"], 50)
        self.assertEqual(p["messages_one_to_one"], 0,
                         "50 group messages read as a close personal history")

    def test_someone_with_no_messages_still_comes_back(self):
        r = chat._find_person(self.conn, "Ture")
        row = next(p for p in r["people"] if p["name"] == "Ture")
        self.assertEqual(row["messages_total"], 0)
        self.assertEqual(row["known_since"], "")
        self.assertEqual(row["years_known"], 0.0)

    def test_a_stub_card_does_not_outrank_the_real_friend(self):
        """A contact saved as exactly "Ture" with no messages is a perfect
        name match and still not the person meant by "my contact Ture"."""
        r = chat._find_person(self.conn, "Ture")
        order = [p["name"] for p in r["people"]]
        self.assertLess(order.index("Ture Exempelsson"), order.index("Ture"))

    def test_but_an_empty_card_still_wins_its_own_exact_query(self):
        """The demotion is one band, so it cannot invert a real difference in
        name certainty — otherwise searching a stub by its full name would
        return somebody else."""
        self.person("Ture Vantablack")
        r = chat._find_person(self.conn, "Ture Vantablack")
        self.assertEqual(r["people"][0]["name"], "Ture Vantablack")

    def test_an_empty_name_is_an_error_not_everyone(self):
        r = chat._find_person(self.conn, "")
        self.assertIn("error", r)
        self.assertEqual(r["people"], [])

    def test_a_name_nobody_has_says_so(self):
        r = chat._find_person(self.conn, "Zeynep Kowalczyk")
        self.assertEqual(r["people"], [])
        self.assertEqual(r["total_matched"], 0)
        self.assertIn("note", r)

    def test_a_hidden_person_is_not_returned(self):
        self.person("Ture Hidden", excluded=1)
        r = chat._find_person(self.conn, "Ture")
        self.assertNotIn("Ture Hidden", [p["name"] for p in r["people"]])

    def test_it_is_reachable_through_the_tool_path(self):
        r = chat.run_tool(self.conn, "find_person", {"name": "Ture"},
                          {"search_people_limit": 10})
        self.assertEqual(r["people"][0]["name"], "Ture Exempelsson")

    def test_the_answer_can_render_cards_for_it(self):
        """Every person named in an answer needs a clickable card, and the card
        loop reads `contacts`/`people`. A tool returning people under a third
        key renders names the user cannot open."""
        r = chat._find_person(self.conn, "Ture")
        self.assertTrue(chat._tool_cards(r))
        self.assertEqual(chat._tool_matched(r), len(r["people"]))


if __name__ == "__main__":
    unittest.main()
