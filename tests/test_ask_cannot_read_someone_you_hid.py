"""NC-145: Hide and Delete are instructions Ask now reads.

THE BUG. Hiding someone is the one control in this app that is purely about
privacy. Ask — the Anthropic-backed chat — honoured it on the tools that list
people and ignored it on the tools that take an id, which meant it did not
honour it at all: `find_person` handed out the id, `get_contact` took it with
no scope test. Two ordinary tool calls turned a hidden person into their name,
company, position, city, how-we-met and every note, and sent it to Anthropic.
`mutual_connections` closed the loop from the other end, resolving a bare NAME
to a hidden person's row through `db.search_contacts`.

Underneath that sat FOUR different answers to "who is in scope", in one feature:

    population_where('visible')     search_people, find_person
    "WHERE excluded = 1" by hand    search_conversations, _excluded_ids,
                                    and twice more in search_cli.py
    db.get_contact(conn, id)        no predicate at all
    db.search_contacts(conn, name)  no predicate at all

The first two are the same population wearing two spellings, so BOTH admit
someone deleted without being hidden. The last two admit everyone.

WHY tests/test_scope_filter_one_spelling.py never saw any of it: that gate
matches the zero side of the predicate (`excluded = 0`), and its docstring puts
the complement (`excluded = 1`) deliberately out of scope as NC-115. Every
hand-written filter left in Ask was the complement. The gate that exists to
stop a 43rd hand-written scope filter was structurally blind to four of them.

WHAT THESE GUARD

  * every tool refuses a hidden person AND a deleted person, by id and by name
  * an ordinary person still comes back through every one of those tools — a
    gate that refuses everybody is as useless as one that refuses nobody, and
    this half is the one that would break the product
  * the group-thread neighbours, which are fetched by a SECOND query keyed on
    the thread and so never passed through the message filter at all
  * both halves of Ask agree: the no-key rule-based fallback is selected on any
    exception, not just a missing key, so a network hiccup must not widen who
    the answer can be about
  * the complement is derived from the population, not typed out beside it

THE DELETED-BUT-NOT-HIDDEN ROW is the case every test here turns on, and it is
why these are not just "hidden" tests. `db.set_deleted` writes `excluded = 1`
alongside `deleted = 1`, so in an ordinary database every deleted person is also
hidden and the two populations agree by arithmetic accident — the accident
figures.py calls "load-bearing in the worst way". `POST /api/contact/{id}/exclude
{excluded: false}` clears `excluded` and never touches `deleted`, so one call to
a live route makes the row. These fixtures make it directly.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, arc, chat_mod as chat, code_of, db  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import figures                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AskBase(Base):
    """One CRM, one archive, and three people who differ only in their flags.

    `seen` is the whole design of this file: every assertion is "the ordinary
    person is here AND the other two are not", run through the real `run_tool`
    dispatch rather than the private helper, because the dispatch is what the
    model actually reaches.
    """

    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        from unittest import mock
        self.arc_dir = tempfile.mkdtemp(prefix="crm_arc_")
        self.arc_path = os.path.join(self.arc_dir, "archive.db")
        arc.init_archive(self.arc_path)
        self.ac = arc.get_archive_db(self.arc_path)
        self._patch = mock.patch.object(arc, "ARCHIVE_DB_PATH", self.arc_path)
        self._patch.start()
        self._shutil = shutil
        self.n = 0

        self.ok = self._person("Ada Ordinary", company="Openly Ltd")
        self.hid = self._person("Hedda Hidden", company="Hidden Ltd")
        db.set_excluded(self.conn, self.hid, 1)
        # The row an ordinary database does not have: deleted WITHOUT being hidden.
        self.gone = self._person("Delia Deleted", company="Deleted Ltd")
        self.conn.execute(
            "UPDATE contacts SET deleted = 1, excluded = 0 WHERE id = ?", (self.gone,))
        self.conn.commit()

    def tearDown(self):
        self._patch.stop()
        self.ac.close()
        self._shutil.rmtree(self.arc_dir, True)
        super().tearDown()

    def _person(self, name, company=""):
        cid = db.create_contact(self.conn, name)
        self.conn.execute(
            "UPDATE contacts SET company = ?, position = ?, home_city = ?, "
            "how_we_met = ? WHERE id = ?",
            (company, "Engineer", "London", f"met {name} at a thing", cid))
        self.conn.commit()
        return cid

    def _msg(self, cid, ts, body, group_key="", group=""):
        self.n += 1
        arc.insert_message(self.ac, contact_id=cid, channel="imessage",
                           direction=0, ts=ts, body=body,
                           source_key=f"k{self.n}", group_name=group,
                           group_key=group_key)
        self.ac.commit()

    def tool(self, _tool, **kw):
        """Through the real dispatch, the way the model reaches it.

        The parameter is `_tool` rather than `name` because `name` is a tool
        INPUT for two of the tools under test here, and the collision silently
        turns a scope test into a TypeError.
        """
        return chat.run_tool(self.conn, _tool, kw)

    def blob(self, payload):
        """Everything in a tool result, flattened to one searchable string."""
        import json
        return json.dumps(payload, default=str)


class TestTheTwoWaysInThatHadNoLockAtAll(AskBase):
    """`get_contact` and `mutual_connections` — the pair NC-145 is named for.

    A list-level filter is not a filter when the tool next door hands out the
    id. These are the by-id and by-name doors.
    """

    def test_get_contact_refuses_a_hidden_person(self):
        out = self.tool("get_contact", id=self.hid)
        self.assertIn("error", out)
        self.assertNotIn("Hedda", self.blob(out))
        self.assertNotIn("Hidden Ltd", self.blob(out))

    def test_get_contact_refuses_a_deleted_person(self):
        """The half that `visible` would still have let through."""
        out = self.tool("get_contact", id=self.gone)
        self.assertIn("error", out)
        self.assertNotIn("Delia", self.blob(out))

    def test_the_refusal_names_the_flag_that_actually_applies(self):
        """"Hidden" for somebody the user DELETED sends them looking for an
        Unhide button the page does not have — a deleted contact is offered only
        Restore and Purge (people.js:1501)."""
        self.assertIn("hidden", self.tool("get_contact", id=self.hid)["error"])
        self.assertIn("deleted", self.tool("get_contact", id=self.gone)["error"])
        self.assertIn("Restore", self.tool("get_contact", id=self.gone)["error"])

    def test_get_contact_still_works_for_everybody_else(self):
        out = self.tool("get_contact", id=self.ok)
        self.assertNotIn("error", out)
        self.assertIn("Ada Ordinary", self.blob(out))
        self.assertIn("Openly Ltd", self.blob(out))

    def test_the_refusal_carries_no_field_of_theirs(self):
        """It says the person is hidden — the only reader is whoever hid them —
        but nothing about them travels with the sentence."""
        out = self.tool("get_contact", id=self.hid)
        text = self.blob(out)
        for leaked in ("Hedda", "Hidden Ltd", "Engineer", "London", "met "):
            self.assertNotIn(leaked, text)

    def test_mutual_connections_refuses_by_id(self):
        for cid in (self.hid, self.gone):
            out = self.tool("mutual_connections", contact_id=cid)
            self.assertIn("error", out, f"contact {cid} came back")

    def test_mutual_connections_refuses_by_name(self):
        """The other half of the pair. `db.search_contacts` has no scope filter,
        so a bare NAME used to resolve to a hidden person's id and confirmed
        spelling — which re-opens every by-id tool."""
        for name in ("Hedda Hidden", "Hedda", "Delia Deleted"):
            out = self.tool("mutual_connections", name=name)
            self.assertIn("error", out, f"{name!r} resolved to a row")

    def test_mutual_connections_still_resolves_an_ordinary_name(self):
        out = self.tool("mutual_connections", name="Ada Ordinary")
        self.assertNotIn("error", out)
        self.assertEqual(out["contact_id"], self.ok)

    def test_a_hidden_name_does_not_shadow_a_real_match(self):
        """The regression the filter could easily introduce. `search_contacts`
        returns the single closest match; if the hidden row wins the ranking and
        is then dropped, the real person must still be found rather than the
        call returning nothing."""
        twin = self._person("Hedda Hidden Twin")
        out = self.tool("mutual_connections", name="Hedda Hidden")
        self.assertNotIn("error", out)
        self.assertEqual(out["contact_id"], twin)


class TestTheToolsThatListPeople(AskBase):

    def test_find_person_returns_neither(self):
        out = self.tool("find_person", name="a")
        text = self.blob(out)
        self.assertIn("Ada Ordinary", text)
        self.assertNotIn("Hedda", text)
        self.assertNotIn("Delia", text)

    def test_search_people_returns_neither(self):
        out = self.tool("search_people", location="London")
        text = self.blob(out)
        self.assertIn("Ada Ordinary", text)
        self.assertNotIn("Hedda", text)
        self.assertNotIn("Delia", text)

    def test_search_people_does_not_count_them_either(self):
        """`total_matched` is the number the model quotes back. Filtering the
        list while leaving the total is how a count becomes a disclosure."""
        out = self.tool("search_people", location="London")
        self.assertEqual(out["total_matched"], 1)


class TestTheMessageTools(AskBase):

    def setUp(self):
        super().setUp()
        for cid, who in ((self.ok, "ada"), (self.hid, "hedda"), (self.gone, "delia")):
            self._msg(cid, "2026-01-05 10:00:00", f"secret from {who} about herring")

    def _ids(self, cid):
        return [r["id"] for r in self.ac.execute(
            "SELECT id FROM messages WHERE contact_id = ?", (cid,))]

    def test_search_messages_returns_neither_their_text_nor_their_card(self):
        out = self.tool("search_messages", query="herring")
        text = self.blob(out)
        self.assertIn("ada", text)
        for gone in ("hedda", "delia", "Hedda", "Delia"):
            self.assertNotIn(gone, text)

    def test_read_message_refuses_both(self):
        for cid, who in ((self.hid, "hidden"), (self.gone, "deleted")):
            for mid in self._ids(cid):
                out = self.tool("read_message", id=mid)
                self.assertIn("error", out, f"read a {who} person's message")
                self.assertNotIn("herring", self.blob(out))

    def test_read_message_still_reads_an_ordinary_one(self):
        for mid in self._ids(self.ok):
            out = self.tool("read_message", id=mid)
            self.assertNotIn("error", out)
            self.assertIn("herring", out["text"])

    def test_people_in_messages_lists_neither(self):
        out = self.tool("people_in_messages", since="2026-01-01", until="2026-12-31")
        text = self.blob(out)
        self.assertIn("Ada Ordinary", text)
        self.assertNotIn("Hedda", text)
        self.assertNotIn("Delia", text)

    def test_message_stats_counts_neither(self):
        """No name escapes here, but their messages used to land inside the
        totals — and the receipt field is called
        contacts_excluded_from_every_count, which is a promise."""
        out = self.tool("message_stats")
        self.assertEqual(out["total_messages"], 1)
        self.assertEqual(out["contacts_excluded_from_every_count"], 2)

    def test_search_messages_does_not_echo_a_name_it_was_only_given_an_id_for(self):
        """The subtle one. With an
        explicit contact_id, `search_messages` adds an `all_messages_are_with`
        block built from the ARGUMENT rather than from the rows — so the filter
        emptied the message list and the NAME came back beside it anyway. The id
        is the model's own input; the name is what it was fishing for."""
        for cid, gone in ((self.hid, "Hedda"), (self.gone, "Delia")):
            out = self.tool("search_messages", query="herring", contact_id=cid)
            self.assertNotIn(gone, self.blob(out))

    def test_search_messages_still_names_an_ordinary_person(self):
        """Asserted on the FIELD, not on the payload. Looking for the name anywhere
        in the result would pass with the echo blanked for everybody — the name is
        still in the `people` block, so such a counter-test would measure a
        different field from the one it guards."""
        out = self.tool("search_messages", query="herring", contact_id=self.ok)
        self.assertEqual(out["all_messages_are_with"]["contact_name"],
                         "Ada Ordinary")

    def test_search_conversations_returns_neither(self):
        for cid in (self.hid, self.gone):
            out = self.tool("search_conversations", query="herring", contact_id=cid)
            self.assertEqual(out["conversations"], [])


class TestTheNeighboursNobodyFiltered(AskBase):
    """The one no population could have fixed, because it is a different query.

    `search_messages` returns the messages either side of every match (NC-126).
    Those come from a SECOND archive query keyed on the
    thread, so they never pass through the filter that scopes the matches. In a
    group thread `_same_thread` selects on `group_key` alone — every member's
    messages — so a hidden person's words came back as "them: …" beside a
    visible person's match.

    It stays invisible in everyday use for a reason that is luck: when the
    messages belonging to hidden contacts happen to be one-to-one, the other
    branch is taken and the group branch is never exercised.
    """

    def setUp(self):
        super().setUp()
        G = "group-1"
        self._msg(self.ok, "2026-02-01 10:00:00", "which colour for the poster", G, "Team")
        self._msg(self.hid, "2026-02-01 10:01:00", "periwinkle", G, "Team")
        self._msg(self.gone, "2026-02-01 10:02:00", "no, vermilion", G, "Team")
        self._msg(self.ok, "2026-02-01 10:03:00", "thanks both", G, "Team")

    def test_a_hidden_members_words_are_not_quoted_back(self):
        out = self.tool("search_messages", query="colour for the poster", context=3)
        text = self.blob(out)
        self.assertIn("colour for the poster", text, "the match itself should still be here")
        self.assertNotIn("periwinkle", text, "a hidden person's words came back as a neighbour")
        self.assertNotIn("vermilion", text, "a deleted person's words came back as a neighbour")

    def test_a_visible_neighbour_still_comes_back(self):
        """The counter-test, and the one that would break the feature NC-126
        exists for: dropping the wrong neighbours must not drop the right ones."""
        out = self.tool("search_messages", query="colour for the poster", context=3)
        self.assertIn("thanks both", self.blob(out))


class TestOneSpellingRatherThanFour(AskBase):
    """The structural half. Closing five holes is worth less than making the
    sixth impossible to open."""

    ASK_FILES = ("chat.py", "search_cli.py", "api_ask.py")

    def test_the_complement_is_derived_from_the_population(self):
        for name in sorted(figures.POPULATIONS):
            pred = figures.population_where(name)
            comp = figures.population_not_where(name)
            if not pred:
                self.assertEqual(comp, "", "'all' excludes nobody")
            else:
                self.assertEqual(comp, f"NOT ({pred})")

    def test_ids_outside_is_exactly_the_people_the_population_leaves_out(self):
        everyone = {r["id"] for r in self.conn.execute("SELECT id FROM contacts")}
        for name in sorted(figures.POPULATIONS):
            pred = figures.population_where(name)
            inside = {r["id"] for r in self.conn.execute(
                "SELECT id FROM contacts" + (f" WHERE {pred}" if pred else ""))}
            self.assertEqual(db.ids_outside(self.conn, name), everyone - inside,
                             f"the two directions disagree for {name!r}")

    def test_ask_asks_for_both_flags(self):
        """'active' rather than 'visible', and the reason is specific to Ask:
        every other surface renders locally, and Ask sends what it reads to
        Anthropic. Where two defensible populations differ, the path that leaves
        the machine takes the narrower one."""
        self.assertEqual(chat.ASK_POPULATION, "active")
        self.assertIn("deleted", figures.population_where(chat.ASK_POPULATION))
        self.assertIn("excluded", figures.population_where(chat.ASK_POPULATION))

    def test_no_file_in_the_ask_path_spells_the_filter_by_hand(self):
        """The gate the existing one could not be: test_scope_filter_one_spelling
        matches `excluded = 0` and says in its own docstring that the complement
        is out of its scope. Every filter NC-145 fixed was the complement."""
        import re
        bad = re.compile(r"(?:COALESCE\s*\(\s*)?(?:[a-z]\.)?\b(?:excluded|deleted)\b"
                         r"\s*(?:,\s*[01]\s*\))?\s*(?:=\s*1|<>\s*0|!=\s*0)", re.I)
        for fname in self.ASK_FILES:
            # code_of, not the raw file: the raw source would trip on the
            # DOCSTRING quoting the filter it bans, which is the trap that
            # helper was written for.
            for n, line in enumerate(code_of(os.path.join(REPO, "crm", fname))
                                     .splitlines(), 1):
                if "SELECT" not in line.upper() and "WHERE" not in line.upper():
                    continue
                self.assertIsNone(
                    bad.search(line),
                    f"crm/{fname}:{n} spells a contact-scope filter by hand: "
                    f"{line.strip()!r}. Ask the population by name instead — "
                    "db.ids_outside(conn, chat.ASK_POPULATION).")

    def test_the_rankers_agree_with_ask_about_who_is_rankable(self):
        """`search_cli` carries its own constant because two of its four callers
        are local views rather than Ask. It still has to AGREE — a ranked list
        is what `semantic_search` and `search_conversations` return, so a wider
        population there re-opens both tools no matter what chat.py says.

        This is a constant comparison and not a behavioural test on purpose:
        both rankers need sentence-transformers, which this suite does not have,
        so the behavioural version would pass by skipping. That gap is real — a
        mutation there is not caught by the behavioural tests — and a constant
        two modules must share is the part that can be checked here.
        """
        import search_cli
        self.assertEqual(search_cli.RANK_POPULATION, chat.ASK_POPULATION,
                         "the rankers feed Ask; a wider population here undoes "
                         "the filter in chat.py")
        src = code_of(os.path.join(REPO, "crm", "search_cli.py"))
        self.assertIn("db.ids_outside(", src,
                      "search_cli should ask db for the set, not build its own")

    def test_both_halves_of_ask_read_the_same_population(self):
        """The rule-based fallback is not opt-in: handle_chat drops to it on ANY
        exception from chat.chat(), so a flaky network chose the engine. If the
        two disagree about scope, a timeout widens who the answer can name."""
        with open(os.path.join(REPO, "crm", "api_ask.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("population_where('visible')", src,
                         "api_ask.py names a population of its own; it should "
                         "take chat.ASK_POPULATION so the two engines agree")
        self.assertIn("population_where(chat.ASK_POPULATION)", src)


if __name__ == "__main__":
    unittest.main()
