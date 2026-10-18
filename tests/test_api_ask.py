"""crm/api_ask.py: the rule-based Ask engine, chat provenance/receipts, and conversation search across both keyword and semantic modes."""

import os
import sqlite3
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, arc, chat_mod, db, search_cli,
)


class TestAskCards(Base):
    """The contact cards under an Ask answer are the only handle the user has on
    the people in it — tick them, open them, add them to a list. A roster answer
    for dozens of people has to carry a card for each, not 3."""

    def _people(self, *names):
        return [{"id": i, "name": n} for i, n in enumerate(names, start=1)]

    def test_cards_keep_only_the_people_the_answer_names(self):
        people = self._people("Anna Svensson", "Erik Lindqvist", "Sofia Nyberg")
        cards = app._cards_for_reply("Ask Anna Svensson, and Sofia Nyberg too.", people)
        self.assertEqual([c["name"] for c in cards],
                         ["Anna Svensson", "Sofia Nyberg"])

    def test_a_truncated_answer_keeps_every_person_found(self):
        """The regression that produced 3 cards for a long roster: the reply
        was cut off at person 7, and filtering cards to the surviving text threw
        away everyone the answer had not reached yet."""
        people = self._people("Anna Svensson", "Erik Lindqvist", "Sofia Nyberg",
                              "Maja Holm", "Nina Wikström")
        reply = "1. Anna Svensson — Acme\n2. Erik Lindqvist — Co-founder"
        self.assertEqual(len(app._cards_for_reply(reply, people, False)), 2)
        self.assertEqual(len(app._cards_for_reply(reply, people, True)), 5)

    def test_a_first_name_mention_still_earns_a_card(self):
        people = self._people("Anna Svensson", "Erik Lindqvist")
        cards = app._cards_for_reply("Anna is your strongest science contact.", people)
        self.assertEqual([c["name"] for c in cards], ["Anna Svensson"])

    def test_short_name_particles_do_not_match_everyone(self):
        """'de' or 'van' as a match token would card every contact on any answer
        containing those letters, which is how a name filter becomes no filter."""
        people = self._people("Jan de Vries", "Ann Lee")
        cards = app._cards_for_reply("We settled the deal in Amsterdam.", people)
        self.assertEqual(len(cards), 2)   # nothing matched -> honest fallback, not 1 wrong card

    def test_nothing_named_falls_back_rather_than_showing_none(self):
        people = self._people("Jan de Vries", "Ann Lee")
        self.assertEqual(len(app._cards_for_reply("Nobody obvious fits that.", people)), 2)

    def test_truncated_flag_survives_a_reload(self):
        """A reloaded turn still visibly breaks off mid-sentence, so the flag that
        explains why has to be persisted with it."""
        db.add_chat_message(self.conn, "assistant", "1. Someone — cut off here",
                            contacts_json="", truncated=True)
        db.add_chat_message(self.conn, "assistant", "A complete answer.")
        self.conn.commit()
        msgs = app.handle_chat_history_get(self.conn)["messages"]
        self.assertTrue(msgs[-2]["truncated"])
        self.assertFalse(msgs[-1]["truncated"])


class TestAskCounts(Base):
    """Every narrowing between "what the searches found" and "what is on screen"
    must be reported. Both paths used to drop people silently: the zero-key engine
    returned the best 30 of N and named no total anywhere, and the LLM path capped
    the payload (at 12, then at 80) without saying so. That is the same defect as
    the People table's "First 500 of N"."""

    QUERY = "who do I know in Berlin"

    def _many(self, n):
        # home_metro, not bare names: the scorer skips anyone with no location
        # signal AND no relationship strength (api_ask.py "must have some
        # signal"), so a contact with only a name is invisible to it and every
        # count here would be 0. A metro match alone scores 0.5, independent of
        # history.
        for i in range(n):
            cid = db.create_contact(self.conn, f"Person Number {i:03d}")
            self.conn.execute("UPDATE contacts SET home_metro='Berlin' WHERE id=?", (cid,))
        self.conn.commit()

    def test_handle_ask_reports_total_before_the_cap(self):
        self._many(app.api_ask.ASK_FALLBACK_CAP + 12)
        out = app.handle_ask(self.conn, {"query": self.QUERY})
        # The cap still applies — the point is that the number it cut to is no
        # longer the only number the caller can see.
        self.assertLessEqual(len(out["contacts"]), app.api_ask.ASK_FALLBACK_CAP)
        self.assertGreater(out["total_matched"], len(out["contacts"]))

    def test_total_matched_equals_shown_when_nothing_was_cut(self):
        self._many(3)
        out = app.handle_ask(self.conn, {"query": self.QUERY})
        # Equal, so the UI can stay quiet rather than announcing a cut that did
        # not happen. Guards the "only speak when the numbers disagree" rule.
        self.assertEqual(out["total_matched"], len(out["contacts"]))

    def test_fallback_forwards_the_total(self):
        # _ask_fallback returned exactly {reply, contacts}, which discarded the
        # count and left the zero-key path unable to report it. Regression guard.
        self._many(app.api_ask.ASK_FALLBACK_CAP + 5)
        out = app.api_ask._ask_fallback(self.conn, self.QUERY)
        self.assertIn("total_matched", out)
        self.assertGreater(out["total_matched"], len(out["contacts"]))

    def test_handle_chat_reports_matched_named_and_shown(self):
        self._many(2)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        out = app.handle_chat(self.conn, {"message": self.QUERY})
        for key in ("cards_matched", "cards_named", "cards_shown"):
            self.assertIn(key, out)
        # Monotonic by construction: each stage can only narrow.
        self.assertGreaterEqual(out["cards_matched"], out["cards_named"])
        self.assertGreaterEqual(out["cards_named"], out["cards_shown"])
        self.assertEqual(out["cards_shown"], len(out["contacts"]))

    def test_card_cap_is_reported_not_just_applied(self):
        self._many(6)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["NC_CHAT_MAX_CARDS"] = "2"
        try:
            out = app.handle_chat(self.conn, {"message": self.QUERY})
        finally:
            os.environ.pop("NC_CHAT_MAX_CARDS", None)
        self.assertEqual(len(out["contacts"]), 2)
        # The cap cut people, so the response must carry the larger number the UI
        # needs in order to say so.
        self.assertGreater(out["cards_matched"], out["cards_shown"])


class TestChatProvenance(Base):
    """Ask provenance: searched totals + trail detail strings —
    all without any API call."""

    def test_chat_searched_counts_active_contacts_and_chunks(self):
        a = db.create_contact(self.conn, "Ada")
        e = db.create_contact(self.conn, "Eve")
        db.set_excluded(self.conn, e, 1)
        self.conn.commit()
        arc_orig = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="crm_prov_"), "archive.db")
        try:
            # no archive.db on disk → conversations 0; excluded Ada's Eve not counted
            self.assertEqual(app._chat_searched(self.conn),
                             {"contacts": 1, "conversations": 0, "messages": 0})
            arc.init_archive(arc.ARCHIVE_DB_PATH)
            ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
            arc.insert_message(ac, contact_id=a, channel="whatsapp", direction=0,
                               ts="2026-06-01 10:00:00", body="hello", source_key="p1")
            ac.commit()
            arc.build_chunks(ac, contact_id=a)
            ac.close()
            s = app._chat_searched(self.conn)
            self.assertEqual(s["contacts"], 1)
            self.assertEqual(s["conversations"], 1)
            # NC-081. The message rows are counted separately from the chunks
            # built out of them. Ask's message tools read the rows, and this line
            # is the only place the app says what Ask can reach — naming the
            # chunks while the tools search every message understates the
            # reachable archive many times over.
            self.assertEqual(s["messages"], 1,
                             "the individual messages are not counted, so the "
                             "provenance line describes only the chunk store")
        finally:
            arc.ARCHIVE_DB_PATH = arc_orig

    def test_chat_searched_leaves_out_what_ask_cannot_read(self):
        """NC-301. The "Searched … messages" line counted the whole archive while
        every search tool left hidden people out, so it read as Ask going through
        the messages of people you hid. It counts what the tools can reach: not a
        hidden person's, not a deleted person's, and not a message with no text."""
        a = db.create_contact(self.conn, "Ada")
        hid = db.create_contact(self.conn, "Hedda Hidden")
        gone = db.create_contact(self.conn, "Delia Deleted")
        db.set_excluded(self.conn, hid, 1)
        db.set_deleted(self.conn, gone, 1)
        self.conn.commit()
        arc_orig = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="crm_prov_"), "archive.db")
        try:
            arc.init_archive(arc.ARCHIVE_DB_PATH)
            ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
            for i, (cid, body) in enumerate([(a, "hello"), (a, ""), (hid, "secret"),
                                             (hid, "more"), (gone, "old news")]):
                arc.insert_message(ac, contact_id=cid, channel="whatsapp", direction=0,
                                   ts="2026-06-01 10:0%d:00" % i, body=body,
                                   source_key="p%d" % i)
            ac.commit()
            for cid in (a, hid, gone):
                arc.build_chunks(ac, contact_id=cid)
            ac.close()
            s = app._chat_searched(self.conn)
            self.assertEqual(s["messages"], 1,
                             "the count includes messages no Ask search can read: "
                             "a hidden or deleted person's, or one with no text")
            self.assertEqual(s["conversations"], 1,
                             "the count includes a hidden or deleted person's "
                             "conversations, which Ask never searches")
            # The same population the stats tool reports to the model, so the line
            # and the model cannot disagree about how big the archive is.
            stats = chat_mod._message_stats(self.conn)
            self.assertEqual(s["messages"], stats["total_messages"])
        finally:
            arc.ARCHIVE_DB_PATH = arc_orig

    def test_handle_chat_fallback_has_trail_and_searched(self):
        db.create_contact(self.conn, "Ada")
        self.conn.commit()
        had_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        arc_orig = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="crm_prov2_"), "archive.db")
        try:
            out = app.handle_chat(self.conn, {"message": "who do I know in London?"})
            self.assertEqual(out["trail"], [])      # rule-based path makes no tool calls
            self.assertEqual(out["searched"],
                         {"contacts": 1, "conversations": 0, "messages": 0})
        finally:
            arc.ARCHIVE_DB_PATH = arc_orig
            if had_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = had_key

    def test_tool_detail_and_matched(self):
        self.assertEqual(
            chat_mod._tool_detail("search_people", {"location": "London", "min_strength": 40}),
            "location: London, min_strength: 40")
        self.assertEqual(chat_mod._tool_detail("search_people", {}), "everyone")
        self.assertEqual(
            chat_mod._tool_detail("semantic_search", {"query": "ceramic artists"}),
            "ceramic artists")
        self.assertEqual(chat_mod._tool_detail("get_contact", {"id": 5}), "contact #5")
        self.assertEqual(
            chat_mod._tool_detail("search_conversations", {"query": "paris", "contact_id": 7}),
            "paris · mode: semantic · contact #7")
        self.assertEqual(chat_mod._tool_matched({"contacts": [1, 2, 3]}), 3)
        self.assertEqual(chat_mod._tool_matched({"conversations": [1]}), 1)
        self.assertEqual(chat_mod._tool_matched({}), 0)


class TestConversationSearch(Base):
    """app.handle_conversation_search: keyword (FTS5) + semantic, both modes
    excluding excluded contacts, both degrading gracefully without archive.db."""

    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="convsearch_test_")
        self.arc_path = os.path.join(self.arc_dir, "archive.db")
        self._orig_archive_path = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = self.arc_path
        self._orig_chunk_cache = search_cli.CHUNK_CACHE
        search_cli.CHUNK_CACHE = os.path.join(self.arc_dir, "chunk_vectors.npz")

    def tearDown(self):
        arc.ARCHIVE_DB_PATH = self._orig_archive_path
        search_cli.CHUNK_CACHE = self._orig_chunk_cache
        super().tearDown()

    def test_no_archive_db_returns_unavailable_with_message(self):
        # "nothing imported yet" must be distinguishable from "no matches"
        out = app.handle_conversation_search(self.conn, {"q": ["coffee"]})
        self.assertFalse(out["available"])
        self.assertEqual(out["conversations"], [])
        self.assertTrue(out.get("message"))

    def test_empty_query_returns_empty(self):
        out = app.handle_conversation_search(self.conn, {"q": [""]})
        self.assertEqual(out["conversations"], [])

    def test_keyword_search_finds_message_and_skips_excluded(self):
        keep = db.create_contact(self.conn, "Keep Kim")
        hide = db.create_contact(self.conn, "Hide Hank")
        db.set_excluded(self.conn, hide, 1)
        self.conn.commit()
        arc.init_archive(self.arc_path)
        arc_conn = arc.get_archive_db(self.arc_path)
        arc.insert_message(arc_conn, contact_id=keep, channel="whatsapp", direction=0,
                           ts="2026-05-01 10:00:00", body="let's meet for coffee", source_key="1")
        arc.insert_message(arc_conn, contact_id=hide, channel="whatsapp", direction=0,
                           ts="2026-05-01 10:00:00", body="let's meet for coffee", source_key="2")
        arc_conn.commit()
        arc_conn.close()
        out = app.handle_conversation_search(self.conn, {"q": ["coffee"], "mode": ["keyword"]})
        self.assertTrue(out["available"])
        names = {c["contact_name"] for c in out["conversations"]}
        self.assertIn("Keep Kim", names)
        self.assertNotIn("Hide Hank", names)

    def test_semantic_mode_degrades_without_libs_or_returns_list(self):
        cid = db.create_contact(self.conn, "Sem Sam")
        self.conn.commit()
        arc.init_archive(self.arc_path)
        arc_conn = arc.get_archive_db(self.arc_path)
        arc.insert_message(arc_conn, contact_id=cid, channel="whatsapp", direction=0,
                           ts="2026-05-01 10:00:00", body="hello there", source_key="1")
        arc_conn.commit()
        arc.build_chunks(arc_conn)
        arc_conn.close()
        out = app.handle_conversation_search(self.conn, {"q": ["greeting"], "mode": ["semantic"]})
        self.assertIn("available", out)
        self.assertIsInstance(out["conversations"], list)
        if not out["available"]:
            self.assertIn("message", out)


class TestWriteLockContention(Base):
    """NC-052: Ask's handle_chat inserts the user's turn on its crm.db
    connection and does not commit it until the whole Anthropic tool-use loop
    (tens of seconds) has finished, so that connection holds a real writer
    lock the entire time. A Settings or Goals save opened on a second
    connection then has to wait behind it, and used to fail with sqlite3's own
    "database is locked" — which reads as corruption, not contention.

    These tests hold a REAL uncommitted write open on a REAL second
    connection to the same file and make a REAL second connection write
    against it — not an inspection of what get_db() passed to sqlite3.connect.
    A test that only checked the busy_timeout argument would pass even if the
    pragma were silently ignored; these fail if the wait or the message ever
    stops actually happening.
    """

    def _hold_write_lock(self, path, ready, release, hold_ceiling_sec=10.0):
        """Runs on its own thread with its own connection (sqlite3 connections
        are not shared across threads): starts a write transaction, signals
        `ready` once the lock is actually held, waits for `release`, then
        commits and closes. `hold_ceiling_sec` is a safety net only — the real
        release is always the `release` event, so a stalled test fails fast
        instead of hanging the suite."""
        conn = db.get_db(path)
        try:
            conn.execute(
                "INSERT INTO state (key, value) VALUES ('lock_holder', 'held') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            ready.set()
            release.wait(timeout=hold_ceiling_sec)
            conn.commit()
        finally:
            conn.close()

    def test_busy_timeout_pragma_is_actually_set_on_a_live_connection(self):
        """The mechanism: PRAGMA busy_timeout, read back from a connection
        get_db() just opened, must equal the constant get_db() sets it from —
        not merely be present somewhere in the call that created it."""
        got = self.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(got, db.WRITE_LOCK_TIMEOUT_SEC * 1000)

    def test_a_second_writer_waits_out_a_held_lock_instead_of_failing_on_contact(self):
        """The outcome the writer lock is supposed to produce: a write that starts
        while another connection genuinely holds the writer lock does not fail
        the moment it touches the file — it waits, and once the holder commits,
        it goes through. Elapsed time is asserted (not just success) so a
        no-op busy_timeout — one WAL already made unnecessary because reads
        never blocked writers anyway — could not slip this test by returning
        instantly."""
        ready = threading.Event()
        release = threading.Event()
        holder = threading.Thread(target=self._hold_write_lock,
                                  args=(self.path, ready, release))
        holder.start()
        try:
            self.assertTrue(ready.wait(timeout=5), "holder thread never acquired the lock")
            # Release well inside db.WRITE_LOCK_TIMEOUT_SEC, so a successful
            # write here proves the wait, not a timeout that happened to be
            # long enough to look like one.
            threading.Timer(0.3, release.set).start()

            writer = db.get_db(self.path)
            started = time.monotonic()
            try:
                writer.execute(
                    "INSERT INTO state (key, value) VALUES ('lock_probe', 'ok') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
                writer.commit()
            finally:
                elapsed = time.monotonic() - started
                writer.close()
        finally:
            release.set()
            holder.join(timeout=5)

        self.assertGreater(elapsed, 0.2,
                           "write returned before the holder released its lock — "
                           "the collision this test sets up never happened")
        row = self.conn.execute(
            "SELECT value FROM state WHERE key = 'lock_probe'").fetchone()
        self.assertEqual(row["value"], "ok")

    def test_a_write_that_outlasts_the_timeout_never_says_database_is_locked(self):
        """Reproduces the actual NC-052 failure: a write still blocked when its
        busy_timeout expires. WRITE_LOCK_TIMEOUT_SEC is patched down to 200ms
        (get_db() reads it at call time, the same load-bearing pattern ~20
        tests already rely on for db.DB_PATH) so this proves the behaviour in
        well under a second instead of holding a real lock for
        WRITE_LOCK_TIMEOUT_SEC's real 20.

        Fails before the fix (the exception's text IS "database is locked",
        verbatim, from sqlite3 itself) and passes after it (same exception
        type — CLI callers that already catch sqlite3.OperationalError around
        a get_db() connection, e.g. style_profile.py, still catch this — with
        db.LOCKED_MESSAGE instead)."""
        ready = threading.Event()
        release = threading.Event()
        holder = threading.Thread(target=self._hold_write_lock,
                                  args=(self.path, ready, release))
        holder.start()
        try:
            self.assertTrue(ready.wait(timeout=5), "holder thread never acquired the lock")
            with mock.patch.object(db, "WRITE_LOCK_TIMEOUT_SEC", 0.2):
                writer = db.get_db(self.path)
                try:
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        writer.execute(
                            "INSERT INTO state (key, value) VALUES ('lock_probe2', 'x')"
                        )
                finally:
                    writer.close()
        finally:
            release.set()
            holder.join(timeout=5)

        message = str(caught.exception)
        # The substring trap: "database is locked" must not survive as a
        # literal fragment of the message shown to a person, in any case.
        self.assertNotIn("database is locked", message.lower())
        self.assertEqual(message, db.LOCKED_MESSAGE)

    def test_relock_leaves_unrelated_operational_errors_alone(self):
        """_relock only ever rewrites the lock message — every other
        sqlite3.OperationalError (a missing table, a malformed file) must come
        through completely unchanged, string and all, or a real schema bug
        would start reading as "the database is busy" and misdirect whoever
        is debugging it."""
        original = sqlite3.OperationalError("no such table: nonsense")
        translated = db._relock(original)
        self.assertIs(translated, original)
        self.assertEqual(str(translated), "no such table: nonsense")


class TestAskDoesNotHoldTheWriterLockAcrossTheNetworkCall(Base):
    """NC-052's actual fix, and the one the timeout could never have delivered.

    `busy_timeout` was ALREADY 30 seconds and "database is locked" still reached
    the browser. Raising it further would only have made the failure slower: Ask
    writes the user's turn on its connection and then runs the whole Anthropic
    tool-use loop — up to NC_CHAT_TOOL_ROUNDS round trips — before committing,
    and a 40-round answer outlasts any ceiling worth setting. The lock has to be
    released before the network call, not waited out.
    """

    def setUp(self):
        super().setUp()
        self._key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

    def tearDown(self):
        if self._key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._key
        super().tearDown()

    def test_the_user_turn_is_committed_before_the_model_is_called(self):
        """The gate. Read from a SECOND connection *during* chat.chat().

        A same-connection read cannot see this bug — an uncommitted write is
        visible to the connection that made it — which is exactly why a test on one
        connection can never catch it. The stand-in for the Anthropic call
        opens its own connection, which is what a concurrent HTTP request is.
        """
        seen = {}

        def fake_chat(conn_arg, messages, shape=None, **_kw):
            other = db.get_db(self.path)
            try:
                seen["turns"] = other.execute(
                    "SELECT COUNT(*) c FROM chat_history WHERE role = 'user'"
                ).fetchone()["c"]
                # And the decisive half: a concurrent WRITE must succeed while
                # the model is still "thinking". Before the fix this raised.
                other.execute(
                    "INSERT INTO state (key, value, updated_at) VALUES "
                    "(?, ?, datetime('now')) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value", ("nc052_probe", "written"))
                other.commit()
                seen["write_ok"] = True
            except sqlite3.OperationalError as exc:
                seen["write_ok"] = False
                seen["error"] = str(exc)
            finally:
                other.close()
            return {"reply": "ok", "contacts": []}

        with mock.patch.object(app.api_ask.chat, "chat", fake_chat):
            app.api_ask.handle_chat(self.conn, {"message": "who should I call"})

        self.assertEqual(seen.get("turns"), 1,
                         "the user's turn was not committed before the model "
                         "was called — the writer lock is still being held "
                         "across the whole tool-use loop")
        self.assertTrue(seen.get("write_ok"),
                        f"a concurrent write failed while Ask was mid-answer: "
                        f"{seen.get('error')!r}")

    def test_the_commit_is_before_the_call_in_the_source_not_merely_present(self):
        """Ordering, pinned by AST rather than by string search.

        handle_chat commits again at the end regardless, so a test that only
        asks "did it commit" passes with the fix deleted. Only the ORDER matters.

        String searching gets this wrong by matching the code's own prose: the
        fix's comment says "chat.chat() below runs the entire tool-use loop", and
        the function's DOCSTRING says "runs chat.chat()'s Anthropic tool-use loop"
        — which is not a # comment, so stripping those does not help either. Line
        numbers from real call nodes cannot be fooled by either.
        """
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "crm", "api_ask.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "handle_chat")

        def lines_of(pred):
            return [n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and pred(n)]

        def dotted(node):
            f = node.func
            if not isinstance(f, ast.Attribute):
                return ""
            base = f.value.id if isinstance(f.value, ast.Name) else ""
            return f"{base}.{f.attr}"

        def role_of(node):
            """The literal passed as add_chat_message's `role`, or ''."""
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                return node.args[1].value
            return ""

        # Selected by the ROLE literal, not by call count. handle_chat writes
        # twice — the user's turn before the model runs and the assistant's
        # after — and only the first one's position is what NC-052 is about.
        inserts = lines_of(lambda n: dotted(n) == "db.add_chat_message"
                           and role_of(n) == "user")
        calls = lines_of(lambda n: dotted(n) == "chat.chat")
        commits = lines_of(lambda n: dotted(n) == "conn.commit")
        self.assertEqual(len(inserts), 1, "expected exactly one user-turn insert")
        self.assertEqual(len(calls), 1, "expected exactly one chat.chat() call")
        self.assertTrue(
            any(inserts[0] < c < calls[0] for c in commits),
            f"no conn.commit() between the user-turn insert (line {inserts[0]}) "
            f"and the model call (line {calls[0]}); commits at {commits}. "
            f"That gap is the whole of NC-052.")

    def test_a_failed_answer_still_keeps_the_question(self):
        """The early commit is not just lock hygiene, it is strictly better.

        If the model call dies the question survives, which is precisely when
        you would most want it kept. Pinned so a later refactor cannot 'tidy'
        the commit away on the grounds that the end-of-handler one covers it.
        """
        def boom(conn_arg, messages, shape=None, **_kw):
            raise RuntimeError("network went away")

        with mock.patch.object(app.api_ask.chat, "chat", boom):
            out = app.api_ask.handle_chat(self.conn, {"message": "still asked"})
        self.assertIn("AI chat was unavailable", out["reply"])
        other = db.get_db(self.path)
        try:
            rows = [r["content"] for r in other.execute(
                "SELECT content FROM chat_history WHERE role = 'user'")]
        finally:
            other.close()
        self.assertIn("still asked", rows)
