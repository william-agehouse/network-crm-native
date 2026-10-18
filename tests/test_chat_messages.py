"""NC-081: the message tools, and the harness that lets Ask actually run them.

WHAT WAS ACTUALLY WRONG: more than missing date filters.

Take the question "which new contacts did I mention Acme Robotics to in the last
month, and did I send anyone the link?". Ask could not answer it at any level of
the stack:

  1. The model was never told what day it is. "Last month" had no anchor at all,
     so no tool argument could have been right.
  2. The only message-text tool read `chunks` — conversations glued into blocks
     for the embedding model. A chunk has NO direction (it mixes both sides) and
     its timestamp is the block's end, not any message's. There was no date
     parameter to pass. The question was not hard, it was inexpressible.
  3. Nothing could group by person, so "how many people" meant the model
     counting names by hand off a capped page of rows.

The fix is three tools over `messages` (one row per message, real ts, real
direction) plus today's date in the system blocks. With them the same question
takes three tool calls and returns the right people and the right new ones,
with clickable cards.

WHAT THESE TESTS GUARD, which is not "does LIKE work".

Each of them guards a defect this code has had:

  * `total_matched` counted rows the result then filtered out in Python, so a
    cap note described a cap that never applied. (TestOneFilterForBothTools)
  * `until` was exclusive of its own day, so "up to the 3rd" dropped the 3rd.
  * `message_stats` returned a COUNT under the key `messages` while
    `search_messages` returned a LIST under the same key — and `_tool_matched`
    called len() on the int and killed the whole answer.
  * `_tool_matched` did not know the new key names, so the provenance line read
    "0 matched" beside an answer built from 42 matches.
  * The answer loop collected cards from `contacts` only, so an answer naming
    several people rendered zero clickable cards. (TestEveryPersonNamedGetsACard)
  * `first` is the earliest match INSIDE the window, which is inside the window
    for everybody. Read as "new", it is a signal that only proves the wrong
    thing — and it pushes the model into one extra search per person, doing by
    hand what first_ever answers in one. (TestWhoIsNew)
"""
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, arc, chat_mod as chat, db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MsgBase(Base):
    """A CRM plus its own archive, with ARCHIVE_DB_PATH patched on its owner.

    chat.py reads `arc.ARCHIVE_DB_PATH` at call time (never a captured copy), so
    patching the attribute on the archive module is enough and is the only place
    it is allowed to be patched.
    """

    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="ncrm-msgarc-")
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


class TestAConversationIsNotAPerson(MsgBase):
    """NC-111. A message the owner sent to a group belongs to the conversation, not to
    one of its members, so it is stored under arc.NO_PERSON. Text search wants
    those rows — they are half of every group thread — but the people tool
    counts people, and `total_people` is the length of its grouped result, so
    leaving them in reported one person too many while the list itself silently
    dropped the row."""

    def setUp(self):
        super().setUp()
        self.ada = self.person("Ada Lovelace")
        self.msg(self.ada, "2026-03-01 09:00:00", "lunch friday?",
                 direction=0, group="Project chat", group_key="imessage:1")
        self.msg(arc.NO_PERSON, "2026-03-01 10:00:00", "lunch sounds good",
                 direction=1, group="Project chat", group_key="imessage:1")

    def test_the_people_count_counts_only_people(self):
        # A query is required — grouping the whole archive by person answers
        # nothing, and the tool says so.
        r = chat._people_in_messages(self.conn, query="lunch",
                                     include_groups=True)
        self.assertEqual(r["total_people"], 1,
                         "the conversation was counted as a person")
        self.assertEqual(r["returned"], 1)
        self.assertEqual(len(r["people"]), 1)

    def test_the_message_is_still_findable_by_text(self):
        """The row must stay searchable — dropping it from search would put the owner's
        half of every group conversation back out of reach."""
        r = chat._search_messages(self.conn, query="lunch sounds good",
                                  include_groups=True)
        self.assertEqual(r["total_matched"], 1)

    def test_both_sides_of_the_thread_are_there(self):
        r = chat._search_messages(self.conn, query="lunch", include_groups=True)
        self.assertEqual(r["total_matched"], 2)


class TestTheDatesMeanWholeDays(MsgBase):
    """Both bounds inclusive of their own day, in both tools.

    `ts` is 'YYYY-MM-DD HH:MM:SS'. Comparing it against a bare date makes
    `until` exclusive of its own day — '2026-03-02 14:00:00' > '2026-03-02' — so
    "everything up to the 2nd" silently dropped the 2nd. Off-by-one-day in a
    date filter is invisible in the answer: it just comes back one day short and
    reads as complete.
    """

    def setUp(self):
        super().setUp()
        self.cid = self.person("Dated Dan")
        self.msg(self.cid, "2026-03-01 09:00:00", "first day")
        self.msg(self.cid, "2026-03-02 14:30:00", "middle afternoon")
        self.msg(self.cid, "2026-03-03 23:59:00", "last day, last minute")

    def test_until_includes_its_own_whole_day(self):
        r = chat._search_messages(self.conn, since="2026-03-01", until="2026-03-02")
        self.assertEqual(r["total_matched"], 2,
                         "a message sent during the day named by `until` was "
                         "dropped, so every 'up to X' answer is a day short")

    def test_since_includes_its_own_whole_day(self):
        r = chat._search_messages(self.conn, since="2026-03-03")
        self.assertEqual(r["total_matched"], 1,
                         "a message sent late on the day named by `since` was "
                         "dropped")

    def test_a_single_day_window_is_one_day(self):
        r = chat._search_messages(self.conn, since="2026-03-02", until="2026-03-02")
        self.assertEqual(r["total_matched"], 1)

    def test_people_in_messages_uses_the_same_bounds(self):
        r = chat._people_in_messages(self.conn, since="2026-03-01", until="2026-03-02")
        self.assertEqual(r["total_messages"], 2,
                         "the two tools disagree about what a date range means, "
                         "so the same question answered two ways gives two "
                         "different numbers")


class TestABadDateIsAnErrorNotSilence(MsgBase):
    """A date the tool cannot parse must come back as a stated error.

    The argument comes from a model, which will sometimes send "last month" or
    "August". Dropping an unparseable filter answers a question about one month
    with the entire archive and sounds completely confident doing it — the worst
    available outcome. An error costs one round and the model retries.
    """

    def setUp(self):
        super().setUp()
        cid = self.person("Old Olof")
        self.msg(cid, "2019-01-01 09:00:00", "ancient history")
        self.msg(cid, "2026-03-02 09:00:00", "recent")

    def test_a_phrase_instead_of_a_date_is_refused(self):
        for bad in ("last month", "August", "2026-13-40", "3/2/2026", "2026"):
            with self.subTest(value=bad):
                r = chat._search_messages(self.conn, query="a", since=bad)
                self.assertIn("error", r, f"{bad!r} was accepted as a date")
                self.assertEqual(r["messages"], [])
                self.assertIn("YYYY-MM-DD", r["error"],
                              "the error must name the format so the retry works")

    def test_the_error_names_today_so_the_retry_can_be_right(self):
        r = chat._search_messages(self.conn, query="a", since="last month")
        self.assertIn(chat._utc_today(), r["error"])

    def test_people_in_messages_refuses_it_too(self):
        r = chat._people_in_messages(self.conn, query="a", until="soon")
        self.assertIn("error", r)
        self.assertEqual(r["people"], [])

    def test_a_bad_direction_is_refused(self):
        for bad in ("outbound", "in", "me"):
            with self.subTest(value=bad):
                self.assertIn("error", chat._search_messages(
                    self.conn, query="a", direction=bad))

    def test_neither_tool_will_dump_the_whole_archive(self):
        """No query, no dates, no contact: refused rather than answered with the
        newest N messages in the archive, which answers nothing and reads as an
        answer."""
        self.assertIn("error", chat._search_messages(self.conn))
        self.assertIn("error", chat._people_in_messages(self.conn))


class TestDirectionIsTheThingNothingElseCouldDo(MsgBase):
    """sent means the user wrote it. This is the whole reason the tools exist:
    "did I send anyone the link" is unanswerable from conversation chunks."""

    def setUp(self):
        super().setUp()
        self.cid = self.person("Two-way Tove")
        self.msg(self.cid, "2026-03-01 09:00:00", "here is the link", direction=1)
        self.msg(self.cid, "2026-03-01 10:00:00", "thanks for the link", direction=0)

    def test_sent_is_only_what_the_user_wrote(self):
        r = chat._search_messages(self.conn, query="link", direction="sent")
        self.assertEqual([m["snippet"] for m in r["messages"]], ["here is the link"])

    def test_received_is_only_what_came_back(self):
        r = chat._search_messages(self.conn, query="link", direction="received")
        self.assertEqual([m["snippet"] for m in r["messages"]],
                         ["thanks for the link"])

    def test_leaving_it_out_means_both(self):
        r = chat._search_messages(self.conn, query="link")
        self.assertEqual(r["total_matched"], 2)

    def test_the_row_says_which_way_it_went(self):
        r = chat._search_messages(self.conn, query="link")
        self.assertEqual({m["direction"] for m in r["messages"]},
                         {"sent", "received"},
                         "direction is reported as a word, not as 0/1 the model "
                         "has to remember the meaning of")


class TestQueryIsLiteral(MsgBase):
    """A dotted term is the case this was built for, and the case FTS5 cannot
    take: `MATCH 'example.com'` is a SYNTAX ERROR, not zero results. The query
    string comes straight from a model, so a tool that its own input can make
    throw is a tool that fails on exactly this question. LIKE cannot throw."""

    def setUp(self):
        super().setUp()
        self.cid = self.person("Dotty Doris")
        self.msg(self.cid, "2026-03-01 09:00:00", "come to example.com in Paris")
        self.msg(self.cid, "2026-03-02 09:00:00", "for example? com is short for company")
        self.msg(self.cid, "2026-03-03 09:00:00", "mail me at ann@example.com please")

    def test_a_dotted_term_neither_throws_nor_tokenises(self):
        r = chat._search_messages(self.conn, query="example.com")
        self.assertEqual(r["total_matched"], 2,
                         "a literal search matched 'example? com' — the term was "
                         "split into words somewhere")

    def test_a_url_matches_verbatim(self):
        r = chat._search_messages(self.conn, query="ann@example.com")
        self.assertEqual(r["total_matched"], 1)

    def test_case_does_not_matter(self):
        self.assertEqual(
            chat._search_messages(self.conn, query="EXAMPLE.COM")["total_matched"], 2)

    def test_a_wildcard_is_not_an_operator(self):
        """LIKE's own metacharacters must not widen the search: '%' and '_'
        are matched as plain text, or '%' matches every message in the archive
        and is reported as a hit, and 'sam_berg' matches 'samXberg'. A search
        that cannot come back empty is worse than one that comes back wrong
        once."""
        for q in ("acme%example", "acme_example", "%", "_", "\\"):
            with self.subTest(query=q):
                r = chat._search_messages(self.conn, query=q)
                self.assertEqual(r["total_matched"], 0,
                                 f"{q!r} was treated as a pattern, not as text")

    def test_an_escape_character_in_the_text_is_still_findable(self):
        """The escaping must not break searching for the characters themselves."""
        cid = self.person("Underscore Ulla")
        self.msg(cid, "2026-03-04 09:00:00", "my handle is sam_berg ok")
        self.assertEqual(
            chat._search_messages(self.conn, query="sam_berg")["total_matched"], 1)


class TestTheSnippetContainsTheMatch(MsgBase):
    """A window centred on the hit, not the first N characters.

    An iMessage runs to thousands of characters. `body[:300]` on a match at
    character 1800 hands back text that does not contain the query — and the
    model reads that as a false positive and drops a real hit."""

    def setUp(self):
        super().setUp()
        self.cid = self.person("Verbose Viktor")
        self.long = ("preamble " * 300) + "the secret is acme.example " + ("tail " * 300)
        self.msg(self.cid, "2026-03-01 09:00:00", self.long)

    def test_a_match_deep_in_a_long_message_is_in_the_snippet(self):
        r = chat._search_messages(self.conn, query="acme.example")
        self.assertIn("acme.example", r["messages"][0]["snippet"],
                      "the snippet does not contain the thing that matched, so "
                      "the model cannot tell a real hit from a false one")

    def test_the_snippet_is_bounded(self):
        snip = chat._search_messages(self.conn, query="acme.example")["messages"][0]["snippet"]
        self.assertLess(len(snip), 400,
                        "an unbounded snippet times 200 rows is a request too "
                        "large to send")

    def test_an_email_subject_is_searched_and_shown(self):
        cid = self.person("Mailer Maja")
        arc.insert_message(self.ac, contact_id=cid, channel="email", direction=1,
                           ts="2026-03-04 09:00:00", body="see attached",
                           subject="acme.example lunch", source_key="mail1")
        self.ac.commit()
        r = chat._search_messages(self.conn, query="acme.example lunch")
        self.assertEqual(r["total_matched"], 1)
        self.assertIn("acme.example lunch", r["messages"][0]["snippet"])


class TestOneFilterForBothTools(MsgBase):
    """Excluded people are filtered in SQL, so every reported total is a total
    of what the caller can actually reach.

    _search_conversations skips excluded contacts in the result loop, which is
    fine there because it reports no totals. Here both tools COUNT over the same
    clause, so a Python-side skip would make every total include people who are
    never returned — then "30 people match, 20 shown" describes a cap that never
    applied. Two numbers with the same name counting different populations is
    the defect this file exists to prevent.
    """

    def setUp(self):
        super().setUp()
        self.keep = self.person("Kept Kim")
        self.gone = self.person("Excluded Elin", excluded=1)
        self.msg(self.keep, "2026-03-01 09:00:00", "acme.example")
        self.msg(self.gone, "2026-03-01 10:00:00", "acme.example")
        self.msg(self.gone, "2026-03-01 11:00:00", "acme.example again")

    def test_an_excluded_person_is_not_in_the_rows(self):
        r = chat._search_messages(self.conn, query="acme.example")
        self.assertEqual([m["contact_name"] for m in r["messages"]], ["Kept Kim"])

    def test_and_is_not_in_the_total_either(self):
        r = chat._search_messages(self.conn, query="acme.example")
        self.assertEqual(r["total_matched"], 1,
                         "the total counts messages the tool will never return, "
                         "so the 'narrow your search' note describes a cap that "
                         "does not exist")

    def test_the_grouped_tool_agrees(self):
        r = chat._people_in_messages(self.conn, query="acme.example")
        self.assertEqual(r["total_people"], 1)
        self.assertEqual(r["total_messages"], 1)

    def test_and_so_does_the_orientation_tool(self):
        """message_stats must count the same population the search tools can
        reach. Telling the model there are N messages and then being unable to
        return N of them is the same trap in its cheapest form."""
        st = chat._message_stats(self.conn)
        self.assertEqual(st["total_messages"], 1)
        self.assertEqual(st["people_with_messages"], 1)
        self.assertEqual(st["contacts_excluded_from_every_count"], 1)


class TestGroupChatsAreSearchedAndLabelled(MsgBase):
    """NC-100. Group chats used to be OFF by default, on the reasoning that a
    group message is not evidence of a one-to-one relationship with each
    participant.

    That reasoning is sound and it belonged to the other tool. It is an argument
    about COUNTING PEOPLE, not about finding text — and applied to text search it
    hides real messages. Asked "did I send anyone the link", a message sent to
    a group is a hit.

    So searching finds everything and each row names its group; the
    relationship-counting worry moved to people_in_messages, where it belongs,
    as a visible split rather than an exclusion.
    """

    def setUp(self):
        super().setUp()
        self.cid = self.person("Grouped Greta")
        self.msg(self.cid, "2026-03-01 09:00:00", "acme.example one to one")
        self.msg(self.cid, "2026-03-02 09:00:00", "acme.example in the group",
                 group="Project crew")

    def test_group_messages_are_found_by_default(self):
        r = chat._search_messages(self.conn, query="acme.example")
        self.assertEqual(r["total_matched"], 2,
                         "a message sent to a group is still a message the owner sent")

    def test_every_row_names_its_group_so_a_group_hit_reads_as_one(self):
        r = chat._search_messages(self.conn, query="acme.example")
        # NC-127 drops `group` from one-to-one rows rather than sending
        # `"group": ""` on every one of them. The invariant this guards is
        # unchanged: a group hit still says it is one.
        by_group = {m.get("group", "") for m in r["messages"]}
        self.assertEqual(by_group, {"", "Project crew"},
                         "including group messages is only safe because each "
                         "one says it is one")

    def test_nothing_is_reported_as_dropped_when_nothing_is(self):
        r = chat._search_messages(self.conn, query="acme.example")
        self.assertNotIn("group_messages_not_counted", r)

    def test_asking_for_one_to_one_only_still_works(self):
        r = chat._search_messages(self.conn, query="acme.example", include_groups=False)
        self.assertEqual(r["total_matched"], 1)
        self.assertEqual(r.get("group_messages_not_counted"), 1,
                         "when the caller DOES narrow, what was left out must "
                         "still be reported — that half was always right")

    def test_the_tool_path_defaults_the_same_way_as_the_function(self):
        """The bug this would hide: the dispatch read
        bool(tool_input.get("include_groups")), so a missing key was False and
        the function's own default never applied through the tool path. Schema
        default and code default have to be the same default."""
        src = inspect.getsource(chat)
        self.assertNotIn('bool(tool_input.get("include_groups"))', src,
                         "an absent include_groups key reads as False again, so "
                         "the model gets one-to-one only while the schema says "
                         "group chats are searched")
        self.assertEqual(src.count('tool_input.get("include_groups", True)'), 2,
                         "both message tools must default the same way")


class TestWhoIsNew(MsgBase):
    """first_ever, and why `first` could never have answered this.

    With a date range, `first` is the earliest match INSIDE the window — which
    is inside the window for everybody, so reading "new" off it marks every
    single person new. That is a signal that only proves the wrong thing, and
    a model is right to distrust it: given only `first`, it needs one grouped
    search and then a separate before-the-window search for each person, to
    establish what first_ever answers for all of them in one query.
    """

    def setUp(self):
        super().setUp()
        self.old = self.person("Longtime Lars")
        self.new = self.person("Fresh Freja")
        self.msg(self.old, "2024-05-01 09:00:00", "acme.example from the start")
        self.msg(self.old, "2026-03-02 09:00:00", "acme.example again")
        self.msg(self.new, "2026-03-02 10:00:00", "acme.example for the first time")

    def _by_name(self):
        r = chat._people_in_messages(self.conn, query="acme.example",
                                     since="2026-03-01", until="2026-03-31")
        return r, {p["name"]: p for p in r["people"]}

    def test_first_ever_reaches_outside_the_window(self):
        _, people = self._by_name()
        self.assertEqual(people["Longtime Lars"]["first_ever"], "2024-05-01")
        self.assertEqual(people["Fresh Freja"]["first_ever"], "2026-03-02")

    def test_first_is_still_the_window_and_they_are_not_the_same_field(self):
        _, people = self._by_name()
        lars = people["Longtime Lars"]
        self.assertEqual(lars["first"], "2026-03-02")
        self.assertNotEqual(lars["first"], lars["first_ever"],
                            "first and first_ever collapsed into one value, so "
                            "the field that answers 'new' has been lost")

    def test_new_in_window_is_true_for_exactly_the_new_person(self):
        _, people = self._by_name()
        self.assertFalse(people["Longtime Lars"]["new_in_window"])
        self.assertTrue(people["Fresh Freja"]["new_in_window"])

    def test_the_count_is_reported_so_it_need_not_be_derived(self):
        r, _ = self._by_name()
        self.assertEqual(r["new_people_in_window"], 1)
        self.assertEqual(r["total_people"], 2)

    def test_first_ever_respects_the_direction_it_was_asked_about(self):
        """"New to hearing it FROM ME" is not "new to the topic". An inbound
        message must not make an outbound-first look older than it is."""
        cid = self.person("Inbound Ida")
        self.msg(cid, "2020-01-01 09:00:00", "have you heard of acme.example?",
                 direction=0)
        self.msg(cid, "2026-03-05 09:00:00", "yes — acme.example is mine", direction=1)
        r = chat._people_in_messages(self.conn, query="acme.example", direction="sent",
                                     since="2026-03-01")
        ida = [p for p in r["people"] if p["name"] == "Inbound Ida"][0]
        self.assertEqual(ida["first_ever"], "2026-03-05")
        self.assertTrue(ida["new_in_window"])

    def test_with_no_date_range_the_extra_query_is_not_run(self):
        """No window, nothing to be new relative to. The fields are absent rather
        than present and meaningless."""
        r = chat._people_in_messages(self.conn, query="acme.example")
        self.assertNotIn("new_people_in_window", r)
        self.assertNotIn("new_in_window", r["people"][0])


class TestTheCapIsVisible(MsgBase):
    """A truncated list must report the real total, and the note must say so.
    Answering "3 people" from a page of 3 out of 40 is a wrong number."""

    def setUp(self):
        super().setUp()
        for i in range(6):
            cid = self.person(f"Person {i}")
            self.msg(cid, f"2026-03-0{i + 1} 09:00:00", "acme.example")

    def test_the_message_total_survives_the_cap(self):
        r = chat._search_messages(self.conn, query="acme.example", limit=2)
        self.assertEqual(r["returned"], 2)
        self.assertEqual(r["total_matched"], 6)
        self.assertIn("6", r["note"])

    def test_the_people_total_is_counted_in_sql_not_in_the_page(self):
        r = chat._people_in_messages(self.conn, query="acme.example", limit=2)
        self.assertEqual(len(r["people"]), 2)
        self.assertEqual(r["total_people"], 6,
                         "total_people was the length of the returned page, so "
                         "the cap silently became the answer")
        self.assertIn("total_people", r["note"])

    def test_an_empty_result_says_the_query_was_literal(self):
        """Zero hits on a literal search usually means the phrase was too long,
        not that there is nothing there. The note has to say which."""
        r = chat._search_messages(self.conn, query="the acme.example lunch in Paris")
        self.assertEqual(r["total_matched"], 0)
        self.assertIn("literal", r["note"])


class TestTheEmptyCaseIsNamed(MsgBase):
    """No archive at all is a different answer from "nothing matched", and the
    difference has to reach the model. NC-035/061/067/068/070/076 were all one
    signal standing in for a fact it did not prove; this is the same rule."""

    def test_stats_say_there_is_nothing_rather_than_zero(self):
        with mock.patch.object(arc, "ARCHIVE_DB_PATH",
                               os.path.join(self.arc_dir, "nope.db")):
            st = chat._message_stats(self.conn)
        self.assertFalse(st["archive_present"])
        self.assertIn("imported", st["note"])

    def test_the_search_tools_say_it_too(self):
        with mock.patch.object(arc, "ARCHIVE_DB_PATH",
                               os.path.join(self.arc_dir, "nope.db")):
            r = chat._search_messages(self.conn, query="acme.example")
            p = chat._people_in_messages(self.conn, query="acme.example")
        self.assertFalse(r["archive_present"])
        self.assertFalse(p["archive_present"])

    def test_stats_report_the_real_date_limits(self):
        cid = self.person("Bounded Bo")
        self.msg(cid, "2021-06-01 09:00:00", "early")
        self.msg(cid, "2026-03-01 09:00:00", "late")
        st = chat._message_stats(self.conn)
        self.assertEqual(st["first_message"], "2021-06-01")
        self.assertEqual(st["last_message"], "2026-03-01")
        self.assertEqual(st["today"], chat._utc_today())


class TestEveryPersonNamedGetsACard(MsgBase):
    """The answer loop's card list must include the message tools' people.

    The Ask view builds a clickable card per named person by matching the name
    back to the contact summaries the tools returned. The loop read `contacts`
    only, and the message tools return people under `people` — so a correct
    answer naming several people would render plain names and not one of them
    would be openable. The names would be right; the feature would be broken.
    """

    def setUp(self):
        super().setUp()
        self.cid = self.person("Cardable Carl")
        self.msg(self.cid, "2026-03-01 09:00:00", "acme.example")

    def test_the_grouped_tool_returns_card_shaped_people(self):
        r = chat._people_in_messages(self.conn, query="acme.example")
        self.assertEqual([c["id"] for c in chat._tool_cards(r)], [self.cid])

    def test_the_message_tool_returns_them_too(self):
        r = chat._search_messages(self.conn, query="acme.example")
        self.assertEqual([c["id"] for c in chat._tool_cards(r)], [self.cid])

    def test_a_card_carries_what_a_card_renders(self):
        r = chat._search_messages(self.conn, query="acme.example")
        card = chat._tool_cards(r)[0]
        for key in ("id", "name", "strength", "strength_label"):
            self.assertIn(key, card)

    def test_the_older_tools_still_hand_over_their_contacts(self):
        r = chat._search_people(self.conn)
        self.assertEqual(len(chat._tool_cards(r)), len(r["contacts"]))

    def test_cards_never_raise_on_an_unexpected_shape(self):
        """A card list is worth zero answers. It must not be able to take the
        whole reply down, which is what len() on an int did."""
        for junk in ({"people": 7}, {"contacts": "nope"}, {"people": [1, 2]}, {}):
            with self.subTest(result=junk):
                self.assertEqual(chat._tool_cards(junk), [])


class TestTheProvenanceLineTellsTheTruth(MsgBase):
    """The trail under an answer is the user's only view of what was searched.
    "0 matched" beside an answer built from 42 matches is a receipt that
    contradicts the thing it is a receipt for."""

    def setUp(self):
        super().setUp()
        self.a = self.person("Trail Tina")
        self.b = self.person("Trail Tom")
        self.msg(self.a, "2026-03-01 09:00:00", "acme.example")
        self.msg(self.a, "2026-03-02 09:00:00", "acme.example twice")
        self.msg(self.b, "2026-03-03 09:00:00", "acme.example")

    def test_a_message_search_counts_messages(self):
        r = chat._search_messages(self.conn, query="acme.example")
        self.assertEqual(chat._tool_matched(r), 3,
                         "the message search reported its people count, so the "
                         "trail undercounts every message search")

    def test_a_people_search_counts_people(self):
        r = chat._people_in_messages(self.conn, query="acme.example")
        self.assertEqual(chat._tool_matched(r), 2)

    def test_the_total_survives_the_cap_in_the_trail_too(self):
        r = chat._people_in_messages(self.conn, query="acme.example", limit=1)
        self.assertEqual(chat._tool_matched(r), 1)
        self.assertEqual(chat._tool_total(r, 1), 2)

    def test_the_counter_cannot_raise_on_a_count(self):
        """message_stats used to return its total under the key `messages`, the
        same key search_messages returns a LIST under — and len() on the int
        killed the entire answer. One key name, two meanings."""
        self.assertEqual(chat._tool_matched({"messages": 4181}), 0)
        self.assertEqual(chat._tool_matched({"people": 12}), 0)

    def test_stats_does_not_use_the_list_key_names_for_its_counts(self):
        st = chat._message_stats(self.conn)
        for key in ("contacts", "conversations", "people", "messages",
                    "mutual_names"):
            with self.subTest(key=key):
                self.assertNotIsInstance(
                    st.get(key), int,
                    f"message_stats returns a count under {key!r}, which "
                    f"_tool_matched reads as a list")

    def test_the_detail_line_names_every_filter(self):
        detail = chat._tool_detail("people_in_messages", {
            "query": "acme.example", "since": "2026-03-01", "until": "2026-03-31",
            "direction": "sent", "channel": "whatsapp", "include_groups": True})
        for bit in ("acme.example", "2026-03-01", "2026-03-31", "sent", "whatsapp",
                    "group"):
            self.assertIn(bit, detail,
                          f"the trail line hides {bit!r}, so a filter the model "
                          f"got wrong is invisible to the user")

    def test_no_tool_has_an_empty_detail_line(self):
        """A tool wired into run_tool with no _tool_detail branch shows up in the
        trail as a bare name with nothing about what it looked for."""
        samples = {
            "find_person": {"name": "Ture"},
            "read_message": {"id": 1},
            "search_people": {"location": "London"},
            "semantic_search": {"query": "furniture designers"},
            "get_contact": {"id": 3},
            "search_conversations": {"query": "lunch"},
            "mutual_connections": {"contact_id": 3},
            "message_stats": {},
            "search_messages": {"query": "acme.example"},
            "people_in_messages": {"query": "acme.example"},
            "ask_the_user": {"question": "Colleagues or clients?",
                             "options": ["Colleagues", "Clients"]},
        }
        self.assertEqual(sorted(samples), sorted(t["name"] for t in chat.TOOLS),
                         "a tool was added or renamed without updating this "
                         "test's sample inputs")
        for name, args in samples.items():
            with self.subTest(tool=name):
                self.assertTrue(chat._tool_detail(name, args).strip(),
                                f"{name} has no provenance detail line")


class TestTheToolsAreActuallyReachable(MsgBase):
    """A tool declared to the model but not routed in run_tool comes back as
    "unknown tool" — the model asks for it, gets an error, and the failure looks
    like the model's fault."""

    def test_every_declared_tool_is_routed(self):
        for tool in chat.TOOLS:
            with self.subTest(tool=tool["name"]):
                out = chat.run_tool(self.conn, tool["name"], {})
                self.assertNotIn("unknown tool", str(out.get("error", "")),
                                 f"{tool['name']} is offered to the model and "
                                 f"not handled by run_tool")

    def test_the_message_tools_are_declared_at_all(self):
        names = {t["name"] for t in chat.TOOLS}
        for want in ("message_stats", "search_messages", "people_in_messages"):
            self.assertIn(want, names)

    def test_run_tool_passes_the_arguments_through(self):
        cid = self.person("Routed Rut")
        self.msg(cid, "2026-03-02 09:00:00", "acme.example", direction=1)
        self.msg(cid, "2026-03-03 09:00:00", "acme.example reply", direction=0)
        out = chat.run_tool(self.conn, "search_messages",
                            {"query": "acme.example", "direction": "sent",
                             "since": "2026-03-01", "until": "2026-03-31"})
        self.assertEqual(out["total_matched"], 1)
        self.assertEqual(out["filters"]["direction"], "sent")

    def test_the_caps_reach_the_message_tools(self):
        """Search=quick narrows every other limit; a message tool that ignored
        its cap would make Quick cost more than Thorough on the one question
        shape that returns the most rows."""
        for i in range(5):
            cid = self.person(f"Capped {i}")
            self.msg(cid, f"2026-03-0{i + 1} 09:00:00", "acme.example")
        out = chat.run_tool(self.conn, "search_messages", {"query": "acme.example"},
                            {"message_limit": 2})
        self.assertEqual(out["returned"], 2)
        out = chat.run_tool(self.conn, "people_in_messages", {"query": "acme.example"},
                            {"message_people_limit": 3})
        self.assertEqual(len(out["people"]), 3)

    def test_quick_never_searches_more_than_thorough(self):
        quick = chat.shape_caps({"search": "quick"})
        full = chat.shape_caps({"search": "thorough"})
        for key in ("message_limit", "message_people_limit"):
            with self.subTest(cap=key):
                self.assertLessEqual(quick[key], full[key])


class TestTheModelIsToldWhatDayItIs(unittest.TestCase):
    """Without a date anchor no date argument could ever be right, and the model
    cannot read a clock. It goes in the LAST, UNMARKED system block: above the
    cache marker it would write a fresh ~950-token cache entry every midnight
    and re-pay the whole prefix on the first question after it."""

    def test_todays_date_is_in_the_system_blocks(self):
        blocks = chat._system_blocks("", "OWNER")
        text = "\n".join(b["text"] for b in blocks)
        self.assertIn(chat._utc_today(), text,
                      "the model is never told the date, so 'last month' cannot "
                      "be resolved to a date range at all")

    def test_it_is_below_the_cache_marker(self):
        blocks = chat._system_blocks("", "OWNER")
        marked = [i for i, b in enumerate(blocks) if b.get("cache_control")]
        dated = [i for i, b in enumerate(blocks) if chat._utc_today() in b["text"]]
        self.assertTrue(marked and dated)
        self.assertGreater(min(dated), max(marked),
                           "the date sits inside the cached prefix, so the cache "
                           "is invalidated once a day for every user")

    def test_the_shape_directive_still_rides_in_that_same_block(self):
        blocks = chat._system_blocks("ANSWER SHAPE — test directive", "OWNER")
        self.assertIn("ANSWER SHAPE — test directive", blocks[-1]["text"])
        self.assertIn(chat._utc_today(), blocks[-1]["text"])

    def test_the_marker_count_is_still_one(self):
        """NC-080: four markers per request is the API's hard limit, and the
        system half must keep spending exactly one of them."""
        for directive in ("", "ANSWER SHAPE — x"):
            for owner_block in ("", "OWNER"):
                with self.subTest(directive=bool(directive), owner=bool(owner_block)):
                    blocks = chat._system_blocks(directive, owner_block)
                    self.assertEqual(
                        sum(1 for b in blocks if b.get("cache_control")), 1)

    def test_the_date_is_a_real_date(self):
        import datetime as dt
        dt.datetime.strptime(chat._utc_today(), "%Y-%m-%d")


class TestThePromptRoutesDatedQuestionsToTheRightTool(unittest.TestCase):
    """The tools exist and are still unused if the model reaches for
    search_conversations, whose chunks have no direction and no per-message
    date. The routing is prompt text, so it is gated as prompt text."""

    def test_the_prompt_names_all_three_tools(self):
        for name in ("message_stats", "search_messages", "people_in_messages"):
            self.assertIn(name, chat.SYSTEM_PROMPT,
                          f"{name} is never mentioned to the model")

    def test_the_prompt_says_when_not_to_use_the_chunk_search(self):
        self.assertRegex(
            chat.SYSTEM_PROMPT,
            r"(?is)search_conversations.{0,900}(date|timeframe).{0,400}"
            r"(search_messages|people_in_messages|message tools)",
            "nothing in the prompt sends a dated question away from "
            "search_conversations, so the new tools may never be called")

    def test_the_prompt_warns_against_reading_new_off_first(self):
        self.assertRegex(
            chat.SYSTEM_PROMPT, r"(?is)new_in_window|first_ever",
            "the prompt does not point at the field that answers 'new', so the "
            "model falls back to one search per person")

    def test_the_prompt_says_dates_must_be_resolved_not_passed_as_words(self):
        self.assertRegex(
            chat.SYSTEM_PROMPT, r"(?is)never pass.{0,80}last month",
            "nothing tells the model to resolve a phrase into YYYY-MM-DD, so it "
            "spends a round on a rejected argument")

    def test_the_descriptions_carry_the_routing_too(self):
        """A prompt line and a tool description can disagree, and the
        description is what the model sees next to the arguments."""
        by_name = {t["name"]: t for t in chat.TOOLS}
        self.assertIn("YYYY-MM-DD", by_name["search_messages"]["description"])
        self.assertIn("total_people", by_name["people_in_messages"]["description"])
        self.assertRegex(by_name["people_in_messages"]["description"],
                         r"(?is)new_in_window")
        self.assertRegex(by_name["search_conversations"]["description"],
                         r"(?is)search_messages|people_in_messages")

    def test_every_message_tool_declares_its_arguments(self):
        by_name = {t["name"]: t for t in chat.TOOLS}
        for tool in ("search_messages", "people_in_messages"):
            props = by_name[tool]["input_schema"]["properties"]
            for arg in ("query", "since", "until", "direction", "channel",
                        "include_groups"):
                with self.subTest(tool=tool, arg=arg):
                    self.assertIn(arg, props,
                                  f"{tool} accepts {arg} and never tells the "
                                  f"model it exists")


if __name__ == "__main__":
    unittest.main()


class TestTheModelIsToldTheNumberItCanActuallyReach(MsgBase):
    """NC-098, then NC-100 — and what survived both is the point.

    NC-098: `message_stats` counted with include_groups=True while both search
    tools defaulted to False, so the model was told a bigger number than
    it could reach. It was fixed by narrowing the headline to match the tools.

    NC-100 then widened the tools instead, so that searching finds
    everything. The headline moved back up. Both entries changed WHICH number it
    is; neither touched the rule underneath, and that rule is what these test:

        THE HEADLINE AND WHAT A DEFAULT SEARCH RETURNS ARE THE SAME POPULATION.

    So nothing below names a fixed count. Change either default and these fail,
    in whichever direction the change was made.
    """

    def setUp(self):
        super().setUp()
        self.cid = self.person("Group Gary")
        self.msg(self.cid, "2026-03-01 09:00:00", "just us two")
        self.msg(self.cid, "2026-03-02 09:00:00", "also just us")
        self.msg(self.cid, "2026-03-03 09:00:00", "in the group", group="Book Club")

    def test_the_headline_equals_what_a_default_search_can_return(self):
        """The invariant. Nothing here names 2 or 3."""
        stats = chat._message_stats(self.conn)
        reachable = chat._search_messages(
            self.conn, since="2000-01-01")["total_matched"]
        self.assertEqual(stats["total_messages"], reachable,
                         "the model is told a number it cannot search to")

    def test_the_headline_now_includes_the_group_messages(self):
        stats = chat._message_stats(self.conn)
        self.assertEqual(stats["total_messages"], 3)
        self.assertEqual(stats["in_group_threads"], 1)

    def test_the_group_figure_is_a_subset_never_an_addend(self):
        """The specific way this reads wrong: 3 + 1 = 4 messages that do not
        exist. in_group_threads is part of total_messages, not beside it."""
        stats = chat._message_stats(self.conn)
        self.assertLessEqual(stats["in_group_threads"], stats["total_messages"])
        one_to_one = chat._search_messages(
            self.conn, since="2000-01-01", include_groups=False)["total_matched"]
        self.assertEqual(one_to_one,
                         stats["total_messages"] - stats["in_group_threads"])

    def test_people_in_messages_agrees_with_the_headline_too(self):
        """The other search tool shares _msg_filters, so it must land on the same
        population — the reason that function exists."""
        stats = chat._message_stats(self.conn)
        grouped = chat._people_in_messages(self.conn, since="2000-01-01")
        self.assertEqual(grouped["total_messages"], stats["total_messages"])
        self.assertEqual(grouped["in_group_threads"], stats["in_group_threads"])

    def test_the_channel_rows_carry_their_own_group_figure(self):
        """One lump over the whole archive hides "WhatsApp is mostly group
        threads", which is a real thing to know about an archive."""
        self.msg(self.cid, "2026-03-04 09:00:00", "wa group",
                 channel="whatsapp", group="Project")
        by_channel = {c["channel"]: c for c in chat._message_stats(self.conn)["channels"]}
        self.assertEqual(by_channel["imessage"]["in_group_threads"], 1)
        self.assertEqual(by_channel["imessage"]["messages"], 3)
        self.assertEqual(by_channel["whatsapp"]["in_group_threads"], 1)
        self.assertEqual(by_channel["whatsapp"]["messages"], 1,
                         "a channel that is ENTIRELY group threads still has "
                         "searchable messages now, so it must appear")

    def test_an_excluded_contact_is_still_out_of_every_figure(self):
        """The filtering this tool already got right must survive the change."""
        gone = self.person("Excluded Person", excluded=1)
        self.msg(gone, "2026-03-05 09:00:00", "one to one")
        self.msg(gone, "2026-03-06 09:00:00", "grouped", group="Book Club")
        stats = chat._message_stats(self.conn)
        self.assertEqual(stats["total_messages"], 3)
        self.assertEqual(stats["in_group_threads"], 1)
        self.assertEqual(stats["contacts_excluded_from_every_count"], 1)

    def test_the_date_span_covers_exactly_what_can_be_searched(self):
        """A first_message date the default search cannot reach sends the model
        to a day that returns nothing — the same defect wearing a date instead
        of a count. Now that group threads ARE searched, a group-only message
        that predates everything SHOULD move the span."""
        early = self.person("Early Only In Groups")
        self.msg(early, "2019-01-01 09:00:00", "ancient group chat", group="Old Crew")
        stats = chat._message_stats(self.conn)
        self.assertEqual(stats["first_message"], "2019-01-01")
        found = chat._search_messages(self.conn, since=stats["first_message"],
                                      until=stats["last_message"])["total_matched"]
        self.assertEqual(found, stats["total_messages"],
                         "searching the span the tool reports must return "
                         "everything the tool says exists")

    def test_the_tool_description_says_which_population_the_number_is(self):
        """The model reads the schema before it reads a result. If the
        description promises one population and the payload means another, the
        payload's own note is arguing with the schema."""
        spec = next(t for t in chat.TOOLS if t["name"] == "message_stats")
        self.assertIn("in_group_threads", spec["description"])
        self.assertIn("INCLUDING group threads", spec["description"])
        self.assertNotIn("group_messages_not_searched", spec["description"],
                         "the schema still describes the population from before "
                         "group chats became searchable")

    def test_the_note_says_subset_not_sum(self):
        note = chat._message_stats(self.conn)["note"]
        self.assertIn("in_group_threads", note)
        self.assertIn("include_groups", note)
        self.assertIn("subset", note.lower())


class TestABusyGroupThreadCannotPassAsCloseness(MsgBase):
    """NC-100's other half, and the reason including group chats everywhere is
    not simply "more is better".

    One busy iMessage group thread can hold thousands of messages across
    dozens of people. Counted as one-to-one history, that thread alone would hand
    every one of those people a four-figure message count without any of them
    having written to the owner directly.

    Excluding them hid a real answer. Including them silently would invent a
    false one. So people_in_messages reports both halves per person, and the
    tool description tells the model to rank on one_to_one.
    """

    def setUp(self):
        super().setUp()
        self.quiet = self.person("Quiet Quentin")
        self.loud = self.person("Loud In Groups Only")
        self.msg(self.quiet, "2026-03-01 09:00:00", "topic, said directly")
        for i, day in enumerate(("02", "03", "04", "05")):
            self.msg(self.loud, f"2026-03-{day} 09:00:00", "topic, in the group",
                     group="Big Thread")

    def _by_name(self, **kw):
        r = chat._people_in_messages(self.conn, query="topic", **kw)
        return {p["name"]: p for p in r["people"]}, r

    def test_each_person_splits_into_one_to_one_and_group(self):
        people, _ = self._by_name()
        self.assertEqual(people["Loud In Groups Only"]["matching_messages"], 4)
        self.assertEqual(people["Loud In Groups Only"]["in_group_threads"], 4)
        self.assertEqual(people["Loud In Groups Only"]["one_to_one"], 0)
        self.assertEqual(people["Quiet Quentin"]["one_to_one"], 1)
        self.assertEqual(people["Quiet Quentin"]["in_group_threads"], 0)

    def test_the_two_halves_always_sum_to_the_total(self):
        """An identity, so a future filter change cannot make the split describe
        a different population than the count it sits beside."""
        people, res = self._by_name()
        for p in people.values():
            self.assertEqual(p["one_to_one"] + p["in_group_threads"],
                             p["matching_messages"], f"{p['name']} does not add up")
        self.assertEqual(res["total_messages"],
                         sum(p["matching_messages"] for p in people.values()))

    def test_the_loudest_by_total_is_not_the_closest(self):
        """The failure this exists to prevent, stated as the thing a reader
        would get wrong: ordering is by raw count, so the group-only person
        comes FIRST."""
        _, res = self._by_name()
        self.assertEqual(res["people"][0]["name"], "Loud In Groups Only")
        self.assertEqual(res["people"][0]["one_to_one"], 0,
                         "the person at the top of the list has never written "
                         "to the owner directly — one_to_one is what says so")

    def test_narrowing_to_one_to_one_drops_them_entirely(self):
        people, _ = self._by_name(include_groups=False)
        self.assertNotIn("Loud In Groups Only", people)
        self.assertIn("Quiet Quentin", people)

    def test_the_tool_description_tells_the_model_to_read_one_to_one(self):
        spec = next(t for t in chat.TOOLS if t["name"] == "people_in_messages")
        self.assertIn("one_to_one", spec["description"])
        self.assertIn("in_group_threads", spec["description"])
