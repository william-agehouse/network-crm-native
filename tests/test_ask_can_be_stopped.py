"""NC-154: Stop ends the spending, not just the spinner.

THE PROBLEM. While a search runs, the screen shows "Searching your network" and
a timer, so it has to offer something to press.

THE TRAP, and it is the reason this file is long for a button. `static/app/api.js`
already builds an abort handle for every call, so cancelling the browser's own
fetch is one line — and it would stop NOTHING. The answer is a single
synchronous POST; `chat()` would keep looping, keep calling Anthropic and keep
BILLING, with the spinner gone and nobody watching. That is worse than no button
at all: the money still goes out, and now it goes out silently.

So the browser keeps its request open and sends a second, tiny one that sets a
flag the loop reads. The original request comes back — at the next round
boundary — as an ordinary 200 carrying the people already found and the real
bill. This only works because app.py serves on a ThreadingHTTPServer, so the
stop request is handled while the answer is still being written.

WHAT A STOP SAVES. With a fake client that counts paid calls and never touches
the network, a run left alone makes 12 paid calls and one stopped after 0.6
seconds makes 3 — reporting the bill over 3 rounds, keeping both tool calls made
so far, and flagged `stopped`.

WHAT THESE GUARD

  * the SPENDING stops. Everything else here is detail; this is the feature
  * the work already paid for is kept — the trail, the people, the bill
  * `truncated` is True on a stopped answer, which is not cosmetic: it is what
    makes api_ask keep the cards instead of matching names against prose that
    was never written, whose `return named or contacts` fallback would then ship
    the entire unfiltered search result
  * the registry cannot be filled from a request body, cannot be shared between
    two tabs, and does not leak
  * a reload still says the answer was stopped
  * and the one that will save somebody later: the browser must NOT abort its
    own fetch. That is the tempting "simplification" and it silently reintroduces
    the whole bug
"""
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, code_of, db, js_code_of  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import api_ask                       # noqa: E402
import chat as chat_mod              # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Block:
    """`find_person`, not `message_stats`, and that matters: message_stats
    returns no people, so a fixture built on it makes every assertion about
    KEEPING the people found so far pass whether or not they are kept."""
    type = "tool_use"
    id = "t1"
    name = "find_person"
    input: dict = {"name": "Stoppable"}


class _Response:
    stop_reason = "tool_use"
    content = [_Block()]
    usage = types.SimpleNamespace(input_tokens=1000, output_tokens=50,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0)


class _CountingClient:
    """Stands in for the Anthropic client and COUNTS what would have been billed.

    The whole point of NC-154 is a number that must go down, so the fixture has
    to be able to report it. Nothing here touches the network, and no key is
    needed — a test that reached the real API would bill a real account to prove a
    button works.
    """

    def __init__(self, delay=0.05):
        self.calls = 0
        self._delay = delay
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **_kw):
        self.calls += 1
        time.sleep(self._delay)      # a round takes real time, like the real one
        return _Response()


class StopBase(Base):
    def setUp(self):
        super().setUp()
        from unittest import mock
        self.client = _CountingClient()
        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda **_kw: self.client
        self._mods = mock.patch.dict(sys.modules, {"anthropic": fake})
        self._mods.start()
        self._env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-a-real-key"})
        self._env.start()
        # Its own archive, patched on the module that owns the path. Without it
        # this file passes alone and fails in the full suite: `find_person` reads
        # archive.db for the message counts, and whichever file ran before us
        # left ARCHIVE_DB_PATH pointing at a torn-down temp directory. The house
        # pattern is tests/test_chat_messages.py's MsgBase; this is that.
        import shutil
        import tempfile
        from tests.helpers import arc
        self.arc_dir = tempfile.mkdtemp(prefix="crm_arc_")
        arc.init_archive(os.path.join(self.arc_dir, "archive.db"))
        self._arc = mock.patch.object(
            arc, "ARCHIVE_DB_PATH", os.path.join(self.arc_dir, "archive.db"))
        self._arc.start()
        self._rmtree = shutil.rmtree
        # Somebody for the fixture's find_person to find, so "it keeps the people
        # already found" is a real assertion rather than one that passes because
        # there were never any.
        db.create_contact(self.conn, "Stoppable Svensson")
        self.conn.commit()

    def tearDown(self):
        self._arc.stop()
        self._rmtree(self.arc_dir, True)
        self._env.stop()
        self._mods.stop()
        super().tearDown()

    def _run(self, run_id, out):
        """One answer, on its own connection — a real request thread has one."""
        conn = db.get_db(self.path)
        try:
            out["result"] = chat_mod.chat(
                conn, [{"role": "user", "content": "hi"}], run_id=run_id)
        finally:
            conn.close()


class TestTheSpendingStopsAndNotJustTheSpinner(StopBase):
    """The feature. If only one test in this file survives, it is this one."""

    def test_a_stopped_answer_makes_fewer_paid_calls(self):
        out = {}
        rid = chat_mod.begin_run("run-aaaaaaaa")
        try:
            self._run(rid, out)
        finally:
            chat_mod.end_run(rid)
        unstopped = self.client.calls
        self.assertGreater(unstopped, 3, "the fixture should run several rounds")

        self.client.calls = 0
        out = {}
        rid = chat_mod.begin_run("run-bbbbbbbb")
        worker = threading.Thread(target=self._run, args=(rid, out))
        worker.start()
        time.sleep(0.12)
        self.assertEqual(chat_mod.request_stop("run-bbbbbbbb"), "stopping")
        worker.join(timeout=30)
        chat_mod.end_run(rid)
        self.assertFalse(worker.is_alive(), "the loop ignored the stop")
        self.assertLess(self.client.calls, unstopped,
                        "the same number of paid calls was made either way — the "
                        "stop hid the spinner and kept billing, which is the "
                        "entire bug NC-154 is about")
        # And it stops at the FIRST boundary, not the next one. Without the
        # top-of-round check the between-tool-calls check still stops it — one
        # whole round later, which is one more paid re-send of the entire
        # conversation. The delay is 0.05s a round, the stop lands at 0.12s, so
        # two rounds are legitimately in flight; a third means the top-of-loop
        # check is gone.
        self.assertLessEqual(self.client.calls, 3,
                             "it ran on past the first round boundary — the "
                             "top-of-round check is where the money is, because "
                             "every round re-sends the whole conversation")

    def test_it_checks_before_paying_rather_than_after(self):
        """The structural half of the same point, because a timing assertion
        alone would be flaky on a loaded machine."""
        src = code_of(os.path.join(REPO, "crm", "chat.py"))
        at = src.index("for _ in range(caps[\"tool_rounds\"]):")
        body = src[at:src.index("convo.append({\"role\": \"assistant\"", at)]
        self.assertLess(body.index("stop_requested(run_id)"),
                        body.index("client.messages.create"),
                        "the flag is read after the round was paid for, which "
                        "prevents nothing")

    def test_a_stop_lands_between_two_tool_calls_in_one_round(self):
        """The second check site, and it needed a test of its own: with the
        top-of-round check in place, deleting this one still stops the answer —
        just later. Later is the whole cost. One round can carry several tool
        calls and `semantic_search` loads a sentence-transformer model, so
        "later" is seconds of CPU spent after Stop was pressed and the screen
        already said "Stopping".

        Deterministic rather than timed: the first tool call asks for the stop
        itself, so the second must never run. No sleeps, nothing to go flaky on
        a loaded machine.
        """
        from unittest import mock

        class _TwoBlocks(_Response):
            content = [_Block(), _Block()]

        rid = chat_mod.begin_run("run-ffffffff")
        real_run_tool = chat_mod.run_tool
        seen = {"n": 0}

        # **_kw, and it is not decoration: handle_chat wraps chat() in a broad
        # `except Exception`, so a stub whose signature has fallen behind the
        # real one raises TypeError there and the suite sees a silent rule-based
        # answer instead of a crash.
        def stopping_tool(conn, name, tool_input, caps=None, **_kw):
            seen["n"] += 1
            if seen["n"] == 1:
                chat_mod.request_stop(rid)     # pressed while this one is running
            return real_run_tool(conn, name, tool_input, caps, **_kw)

        out = {}
        try:
            with mock.patch.object(chat_mod, "run_tool", stopping_tool):
                self.client.messages.create = lambda **_kw: _TwoBlocks()
                self._run(rid, out)
        finally:
            chat_mod.end_run(rid)
        self.assertTrue(out["result"]["stopped"])
        self.assertEqual(seen["n"], 1,
                         "the second tool call in the round ran after the stop "
                         "was requested — on a real question that is a model "
                         "load and several seconds of CPU nobody asked for")

    def test_it_keeps_what_was_already_paid_for(self):
        out = {}
        rid = chat_mod.begin_run("run-cccccccc")
        worker = threading.Thread(target=self._run, args=(rid, out))
        worker.start()
        time.sleep(0.12)
        chat_mod.request_stop("run-cccccccc")
        worker.join(timeout=30)
        chat_mod.end_run(rid)
        r = out["result"]
        self.assertTrue(r["stopped"])
        self.assertTrue(r["trail"], "the tool calls that ran are the work paid for")
        self.assertTrue(r["contacts"],
                        "the people the searches already found were thrown away "
                        "— Stop would then waste what it had just paid for")
        self.assertIn("Stoppable", str(r["contacts"]))
        self.assertIn(str(len(r["contacts"])), r["reply"],
                      "the reply should say how many were found, or the cards "
                      "appear under a bare 'Stopped.' with nothing explaining them")
        self.assertGreater(r["spend"]["usd"], 0,
                           "a stopped answer that cost money must say so — hiding "
                           "it is the invisibility NC-155 was about")
        self.assertGreaterEqual(r["spend"]["rounds"], 1)

    def test_truncated_is_true_so_the_cards_are_not_filtered_away(self):
        """Not cosmetic. `keep_all` is `truncated or prose_capped`, and with it
        False `_cards_for_reply` matches names against prose that was never
        written — whose `return named or contacts` fallback then ships the whole
        unfiltered search result under a stopped answer."""
        out = {}
        rid = chat_mod.begin_run("run-dddddddd")
        worker = threading.Thread(target=self._run, args=(rid, out))
        worker.start()
        time.sleep(0.12)
        chat_mod.request_stop("run-dddddddd")
        worker.join(timeout=30)
        chat_mod.end_run(rid)
        self.assertTrue(out["result"]["truncated"])

    def test_an_answer_nobody_stopped_is_untouched(self):
        """The counter-test. A stop that fires on its own is a broken app."""
        out = {}
        rid = chat_mod.begin_run("run-eeeeeeee")
        try:
            self._run(rid, out)
        finally:
            chat_mod.end_run(rid)
        self.assertNotIn("stopped", out["result"])

    def test_an_answer_with_no_run_id_simply_cannot_be_stopped(self):
        """Invariant 8's temperament applied to a control: a missing or
        malformed handle costs the Stop button, never the answer."""
        out = {}
        self._run("", out)
        self.assertNotIn("stopped", out["result"])
        self.assertTrue(out["result"]["reply"])


class TestTheRegistryIsNotADoorway(unittest.TestCase):
    """The ids come from a request body."""

    def tearDown(self):
        for rid in ("run-11111111", "run-22222222"):
            chat_mod.end_run(rid)

    def test_a_malformed_id_is_refused_before_it_becomes_a_key(self):
        """An unvalidated key from a request body is an unbounded-memory path
        with a public door on it."""
        before = chat_mod.live_runs()
        for bad in ("", None, "short", "../etc/passwd", "a" * 65, "has space",
                    "semi;colon", {"not": "a string"}):
            self.assertEqual(chat_mod.begin_run(bad), "", f"{bad!r} was accepted")
        self.assertEqual(chat_mod.live_runs(), before,
                         "a refused id was stored anyway")

    def test_two_tabs_cannot_share_one_handle(self):
        self.assertEqual(chat_mod.begin_run("run-11111111"), "run-11111111")
        self.assertEqual(chat_mod.begin_run("run-11111111"), "",
                         "a second run under a live id would let one tab's Stop "
                         "cancel the other tab's answer")

    def test_stopping_something_that_already_finished_is_not_an_error(self):
        """The common case, not an edge one: the answer lands between the press
        and the request. There is nothing to apologise for."""
        self.assertEqual(chat_mod.request_stop("run-never-existed-xx"), "unknown")

    def test_ending_a_run_twice_is_safe(self):
        chat_mod.begin_run("run-22222222")
        chat_mod.end_run("run-22222222")
        chat_mod.end_run("run-22222222")
        chat_mod.end_run("")

    def test_the_registry_is_emptied_by_a_finally(self):
        """No timer, no expiry, no sweeper — so the ONLY thing keeping this from
        leaking is that begin and end are the two halves of one try/finally."""
        # The whole of handle_chat from begin_run onward, NOT a fixed number of
        # characters: lines added above the try (as NC-156 did) would push
        # `finally:` out of a fixed window and fail a test about code that had
        # not changed. A window sized in characters is a test of how much has
        # been written near the thing it checks. code_of() blanks comments, so
        # the window cannot match prose either.
        src = code_of(os.path.join(REPO, "crm", "api_ask.py"))
        at = src.index("chat.begin_run(")
        end = src.index("\ndef ", at)
        after = src[at:end]
        self.assertIn("finally:", after)
        self.assertIn("chat.end_run(run_id)", after)
        self.assertLess(after.index("finally:"), after.index("chat.end_run(run_id)"))


class TestTheStopRouteIsReachableAndCheap(unittest.TestCase):

    def test_it_is_declared_in_the_route_registry(self):
        self.assertIn(app.api_ask, app.ROUTE_MODULES)
        self.assertEqual(api_ask.ROUTES["POST"]["/api/chat/stop"], "handle_chat_stop")

    def test_it_takes_no_database_connection(self):
        """It flips one in-memory flag. Taking a connection would queue it behind
        a writer while somebody is waiting for an answer to stop — the slowest
        possible moment to need the database."""
        import inspect
        params = list(inspect.signature(api_ask.handle_chat_stop).parameters)
        self.assertNotEqual(params[:1], ["conn"],
                            "the route registry hands a conn to any handler whose "
                            "first parameter is named conn")

    def test_an_unknown_run_answers_200_rather_than_an_error(self):
        out = api_ask.handle_chat_stop({"run_id": "run-not-running-x"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "unknown")

    def test_it_does_not_shadow_the_chat_routes(self):
        app_py = code_of(os.path.join(REPO, "crm", "app.py"))
        import re as _re
        literal = set(_re.findall(r'path == "(/api/[^"]+)"', app_py))
        self.assertNotIn("/api/chat/stop", literal)


class TestTheBrowserMustNotAbortItsOwnRequest(unittest.TestCase):
    """The gate that will save somebody later.

    Aborting the fetch is one line, looks like a simplification, and silently
    restores the entire bug: the spinner goes, the loop keeps running and the
    bill keeps growing with nobody watching.
    """

    def setUp(self):
        # Comments blanked, and that is not a detail: a gate that reads raw
        # source trips on the comment beside stopTimedOut (NC-157) explaining why
        # aborting is wrong. A gate that punishes its own explanation teaches
        # people to delete the explanation.
        self.js = js_code_of(os.path.join(REPO, "static", "app", "ask.js"))
        # BOTH halves of the stop path: stopRun holds the request, stop() and
        # NC-157's stopTimedOut are the two things that call it. Sliced from
        # stopRun to the end of stopTimedOut so a new caller cannot appear
        # outside what this gate reads.
        at = self.js.index("const stopRun = useCallback")
        self.stop = self.js[at:self.js.index("const nameRef", at)]

    def test_stop_calls_the_server_and_does_not_abort(self):
        self.assertIn("/api/chat/stop", self.stop)
        for shape in ("abort", "AbortController", "controller.abort"):
            self.assertNotIn(shape, self.stop,
                             "aborting the browser's own request stops the "
                             "spinner and nothing else — crm/chat.py keeps "
                             "looping and keeps billing")

    def test_a_failed_stop_does_not_break_the_answer(self):
        """The run finished first. The answer is on its way regardless."""
        self.assertIn("catch", self.stop)

    def test_every_branch_of_the_body_carries_the_run_id(self):
        """There are two — a question in an existing chat and the first question
        in a new one. Count them: looking for the string anywhere would pass
        with one branch stripped."""
        self.assertEqual(self.js.count("run_id: runId"), 2,
                         "one of the two request bodies omits the handle, so "
                         "Stop does nothing for that kind of question")

    def test_the_button_swaps_rather_than_appearing_beside_ask(self):
        """A second control would change the footer's height the instant an
        answer started. It keeps .btn.primary because view-ask.css sizes the
        composer's button through that selector."""
        self.assertIn('class="btn primary ask-stop"', self.js)
        self.assertIn("${pending", self.js)

    def test_the_handle_is_cleared_when_the_request_settles(self):
        """Or the NEXT question's Stop press would name a run that has gone."""
        self.assertIn('runRef.current = ""', self.js)


class TestAStoppedAnswerStillSaysSoTomorrow(Base):
    """It rides in NC-155's receipt column rather than adding one of its own."""

    def setUp(self):
        super().setUp()
        self.cid = db.create_conversation(self.conn)

    def test_the_flag_survives_a_reload(self):
        import json
        db.add_chat_message(
            self.conn, "assistant", "Stopped.", conversation_id=self.cid,
            receipt_json=json.dumps({"searched": None, "trail": [],
                                     "spend": {"usd": 0.01}, "stopped": True}))
        self.conn.commit()
        out = api_ask.handle_chat_history_get(self.conn, self.cid)
        got = next(m for m in out["messages"] if m["content"] == "Stopped.")
        self.assertTrue(got["stopped"],
                        "a reload rewrote a stopped answer into one that just "
                        "happened to be short")

    def test_handle_chat_is_what_writes_the_flag(self):
        """The write path, which the two tests around it do not touch — they put
        the receipt in the row themselves, so they cannot notice the line in
        handle_chat that STORES `stopped` going missing. Same as NC-155's: a
        round-trip test that supplies its own payload is testing the round trip,
        not the feature."""
        from unittest import mock
        stopped_answer = {"reply": "Stopped.", "contacts": [], "trail": [],
                          "spend": {"usd": 0.01, "rounds": 1, "model": "m",
                                    "input_tokens": 1, "output_tokens": 1,
                                    "cache_read_tokens": 0},
                          "truncated": True, "stopped": True}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-a-real-key"}), \
             mock.patch.object(app.api_ask.chat, "chat",
                               lambda *a, **k: dict(stopped_answer)):
            out = app.api_ask.handle_chat(self.conn, {"message": "who is in Paris"})
        restored = next(
            m for m in api_ask.handle_chat_history_get(
                self.conn, out["conversation"]["id"])["messages"]
            if m["role"] == "assistant")
        self.assertTrue(restored["stopped"])

    def test_an_ordinary_answer_is_not_marked_stopped(self):
        import json
        db.add_chat_message(
            self.conn, "assistant", "A real answer.", conversation_id=self.cid,
            receipt_json=json.dumps({"searched": None, "trail": [],
                                     "spend": {"usd": 0.01}, "stopped": False}))
        self.conn.commit()
        out = api_ask.handle_chat_history_get(self.conn, self.cid)
        got = next(m for m in out["messages"] if m["content"] == "A real answer.")
        self.assertNotIn("stopped", got)


if __name__ == "__main__":
    unittest.main()
