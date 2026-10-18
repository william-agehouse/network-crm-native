"""NC-157..NC-168: twelve failure modes of the stop button and ask-back.

NC-154 (Stop ends the spending) and NC-156 (Ask asks you back) touch the loop and
the run registry, conversation continuity across reloads and the context window,
the front end, and end-to-end sequences. These are the twelve ways they can go
wrong, each guarded below.

THE ONE THAT MATTERS IS NC-157, because it is NC-154's own failure arriving through
a different door. The browser gives up watching after three minutes and says so
honestly ("it may still be working"). But it cleared the run handle as it gave
up, so the answer still running on the server had no control anywhere on screen —
the hidden spinner, still billing, nobody watching. Exactly the state the Stop
button exists to make impossible.

The rest are smaller:

  * Stop appeared in whichever chat you were looking at, and cancelled the one
    that was actually answering
  * clicking an option threw away a longer reply you were part-way through
  * a SPENDING knob (how many turns of memory to buy) could switch off a
    CORRECTNESS rule (never ask twice running)
  * "false" turned multi-select on, because every non-empty string is true
  * the wrong option was marked as your choice when one option's text contained
    another's
  * a model that kept asking spent all twelve rounds and then blamed the question
  * nothing was ever announced to a screen reader, including the one turn that
    needs an answer from you
  * an even memory window quietly dropped one turn of every long chat
  * a question followed by another answer rendered as answered, with dead buttons
  * one line sat between claiming a run handle and the try that releases it
  * and one mistyped tool argument threw away a whole part-paid answer
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, code_of, db, js_code_of  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import api_ask                       # noqa: E402
import chat as chat_mod              # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASK_JS = os.path.join(REPO, "static", "app", "ask.js")


class _Use:
    type = "tool_use"

    def __init__(self, name, inp, id_="t1"):
        self.name, self.input, self.id = name, inp, id_


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content, self.stop_reason = content, stop_reason
        self.usage = types.SimpleNamespace(input_tokens=1000, output_tokens=50,
                                           cache_read_input_tokens=0,
                                           cache_creation_input_tokens=0)


class _Client:
    def __init__(self, script):
        self.script, self.calls, self.requests = list(script), 0, []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        self.requests.append(kw)
        return self.script[min(self.calls - 1, len(self.script) - 1)]


ASK = _Use("ask_the_user", {"question": "Which kind?",
                            "options": ["Colleagues", "Clients"]})


class LoopBase(Base):
    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        from tests.helpers import arc
        self.arc_dir = tempfile.mkdtemp(prefix="crm_arc_")
        arc.init_archive(os.path.join(self.arc_dir, "archive.db"))
        self._arc = mock.patch.object(
            arc, "ARCHIVE_DB_PATH", os.path.join(self.arc_dir, "archive.db"))
        self._arc.start()
        self._rmtree = shutil.rmtree
        db.create_contact(self.conn, "Reviewable Rut")
        self.conn.commit()

    def tearDown(self):
        self._arc.stop()
        self._rmtree(self.arc_dir, True)
        super().tearDown()

    def run_chat(self, script, **kw):
        client = _Client(script)
        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda **_k: client
        with mock.patch.dict(sys.modules, {"anthropic": fake}), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-a-real-key"}):
            return chat_mod.chat(self.conn, [{"role": "user", "content": "q"}],
                                 **kw), client


class JsBase(unittest.TestCase):
    """Source gates, because this app has no JavaScript test runner. Comments
    are blanked first — a gate that trips on the comment explaining it teaches
    people to delete the explanation."""

    def setUp(self):
        self.js = js_code_of(ASK_JS)

    def block(self, start, end):
        at = self.js.index(start)
        return self.js[at:self.js.index(end, at)]

    def send_body(self):
        """send(), start to end. Closed on its own dependency list, which is
        CODE — an end anchor that is a comment vanishes the moment comments are
        stripped."""
        return self.block("const send = useCallback",
                          "}, [pending, gone, shape, convId, loadChats]);")


# ── NC-157 ────────────────────────────────────────────────────────────────────

class TestGivingUpWatchingDoesNotMeanGivingUpStopping(JsBase):
    """The browser stops waiting after ASK_TIMEOUT_MS. The ANSWER does not stop."""

    def test_a_timed_out_turn_keeps_its_run_handle(self):
        catch = self.block("} catch (e) {", "\n  const ")
        self.assertIn("runId:", catch,
                      "the turn that gave up waiting carries no run id, so "
                      "nothing on screen can stop the answer still running")
        self.assertIn("TIMED_OUT_RE.test", catch,
                      "every failure keeps a handle, including ones where the "
                      "request never reached the server and there is nothing "
                      "to stop")

    def test_the_notice_offers_to_stop_it(self):
        self.assertIn("stopTimedOut(m.runId)", self.js)
        self.assertIn("Stop the answer", self.js)

    def test_it_is_an_offer_and_never_automatic(self):
        """The copy promises the answer may still be coming. Killing a
        nearly-finished one to tidy up would make that promise false."""
        catch = self.block("} catch (e) {", "\n  const ")
        self.assertNotIn("stopTimedOut(", catch,
                         "the timeout stops the answer by itself, so an answer "
                         "that was seconds from landing is thrown away")

    def test_pressing_it_says_so_and_cannot_be_pressed_twice(self):
        self.assertIn("stoppedRuns.has(m.runId)", self.js)
        self.assertIn("You stopped the answer", self.js)

    def test_the_stop_route_still_takes_it(self):
        """The id came from a turn rendered minutes ago, so this is the same
        route the live button uses and it must answer the same way."""
        self.assertEqual(chat_mod.begin_run("run-timedout-01"), "run-timedout-01")
        out = api_ask.handle_chat_stop({"run_id": "run-timedout-01"})
        self.assertEqual(out["status"], "stopping")
        chat_mod.end_run("run-timedout-01")
        self.assertEqual(
            api_ask.handle_chat_stop({"run_id": "run-timedout-01"})["status"],
            "unknown", "a run that already finished is not an error")


# ── NC-158 ────────────────────────────────────────────────────────────────────

class TestStopBelongsToTheChatThatIsAnswering(JsBase):

    def test_the_swap_is_scoped_to_this_chat(self):
        self.assertIn("const busyHere = pending && pendingIn === convId;", self.js)
        self.assertIn("${busyHere", self.js)

    def test_the_other_chats_show_a_dead_ask_and_say_why(self):
        self.assertIn("const busyElsewhere = pending && !busyHere;", self.js)
        self.assertIn("Another chat is still answering", self.js)
        # And it is actually WIRED to the button. Declaring the flag and writing
        # the sentence while the disabled test ignores both leaves an Ask button
        # that looks alive, refuses to send, and explains nothing — which is the
        # state this test exists to prevent.
        self.assertIn("disabled=${!text.trim() || !!gone || busyElsewhere}",
                      self.js,
                      "busyElsewhere is computed and never used, so Ask in "
                      "another chat is enabled and silently does nothing")

    def test_the_option_buttons_are_NOT_scoped(self):
        """These must not be scoped the same way: send() refuses while ANY chat
        is answering, so a button scoped to this chat would look live and do
        nothing — which is worse than a button that is visibly off."""
        self.assertIn("busy=${pending}", self.js,
                      "the option buttons were scoped to the open chat, so in "
                      "another chat they look pressable and do nothing")


# ── NC-162 ────────────────────────────────────────────────────────────────────

class TestTheChosenOptionIsMarkedExactly(JsBase):
    """An answered question keeps its options with your choice marked. That mark
    is the only job the state has, so marking the wrong one is the whole bug."""

    def options_block(self):
        return self.block("function AskOptions(", "\nfunction ")

    def test_it_compares_whole_answers(self):
        self.assertIn(
            "const chose = (o) => whole === norm(o) || parts.has(norm(o));",
            self.options_block())

    def test_it_is_not_a_substring_test(self):
        """Offer "Colleagues" and "Colleagues from my old job", pick the long
        one, and a substring test marks both."""
        self.assertNotIn(".includes(", self.options_block(),
                         "the chosen option is found by substring, so one "
                         "option containing another marks both")

    def test_the_split_agrees_with_the_join(self):
        """Multi-select sends its picks joined; this reads them back apart. Two
        separators that drift apart would mark nothing at all, silently."""
        block = self.options_block()
        self.assertIn('.join(", ")', block)
        self.assertIn('whole.split(",")', block)


# ── NC-159 ────────────────────────────────────────────────────────────────────

class TestClickingAnOptionKeepsWhatYouWereTyping(JsBase):

    def test_send_knows_who_sent(self):
        self.assertIn("const send = useCallback(async (q, fromComposer = false)",
                      self.js)

    def test_only_the_composer_empties_the_composer(self):
        body = self.send_body()
        at = body.index('setText("")')
        before = body[max(0, at - 200):at]
        self.assertIn("if (fromComposer)", before,
                      "the composer is cleared for every send, so clicking an "
                      "option throws away a reply you were part-way through")

    def test_an_option_click_does_not_claim_to_be_the_composer(self):
        self.assertIn("onPick=${(o) => send(o)}", self.js)

    def test_the_composer_does_claim_it(self):
        self.assertEqual(self.js.count("send(text, true)"), 2,
                         "the composer has two send paths — the button and "
                         "Enter — and both must own the box")


# ── NC-160 ────────────────────────────────────────────────────────────────────

class TestASpendingKnobCannotSwitchOffACorrectnessRule(Base):

    def setUp(self):
        super().setUp()
        self.cid = db.create_conversation(self.conn)

    def test_the_newest_answer_is_found_whatever_the_window(self):
        db.add_chat_message(self.conn, "user", "who?", conversation_id=self.cid)
        db.add_chat_message(self.conn, "assistant", "Which kind?",
                            conversation_id=self.cid,
                            ask_json=json.dumps({"question": "Which kind?",
                                                 "options": ["A", "B"]}))
        db.add_chat_message(self.conn, "user", "A", conversation_id=self.cid)
        row = db.last_assistant_message(self.conn, self.cid)
        self.assertEqual(row["content"], "Which kind?")
        self.assertTrue(api_ask._asked_last_turn([row]))

    def test_it_skips_the_user_turns_after_it(self):
        db.add_chat_message(self.conn, "assistant", "older",
                            conversation_id=self.cid)
        db.add_chat_message(self.conn, "assistant", "newest",
                            conversation_id=self.cid)
        db.add_chat_message(self.conn, "user", "and then", conversation_id=self.cid)
        self.assertEqual(
            db.last_assistant_message(self.conn, self.cid)["content"], "newest")

    def test_it_does_not_reach_into_another_chat(self):
        other = db.create_conversation(self.conn)
        db.add_chat_message(self.conn, "assistant", "elsewhere",
                            conversation_id=other)
        self.assertIsNone(db.last_assistant_message(self.conn, self.cid))

    def test_a_one_turn_memory_still_blocks_a_second_question(self):
        """NC_CHAT_HISTORY_CONTEXT buys memory and costs money per answer. It
        used to decide this too, so turning the spending down turned the rule
        off — and Ask could ask you two questions in a row."""
        seen = {}

        def fake_chat(conn, messages, **kw):
            seen.update(kw)
            return {"reply": "ok", "contacts": [], "trail": [], "truncated": False}

        env = {"ANTHROPIC_API_KEY": "k", "NC_CHAT_HISTORY_CONTEXT": "1"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(api_ask.chat, "chat", fake_chat):
            api_ask.handle_chat(self.conn, {"message": "first"})
        cid = db.newest_conversation_id(self.conn)
        db.add_chat_message(self.conn, "assistant", "Which kind?",
                            conversation_id=cid,
                            ask_json=json.dumps({"question": "Which kind?",
                                                 "options": ["A", "B"]}))
        self.conn.commit()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(api_ask.chat, "chat", fake_chat):
            api_ask.handle_chat(self.conn, {"message": "second",
                                            "conversation_id": cid})
        self.assertIs(seen["may_ask"], False)

    def test_the_rule_is_not_read_off_the_model_window(self):
        src = code_of(os.path.join(REPO, "crm", "api_ask.py"))
        self.assertNotIn("_asked_last_turn(history)", src,
                         "the rule reads the same rows the model is fed, so a "
                         "spending setting decides whether it applies")


# ── NC-163 ────────────────────────────────────────────────────────────────────

class TestAModelThatOnlyEverAsksIsCutOff(LoopBase):

    def test_a_refused_question_repeated_costs_two_rounds_not_twelve(self):
        result, client = self.run_chat([_Resp([ASK])], may_ask=False)
        self.assertEqual(client.calls, chat_mod.ASK_REFUSALS_MAX)
        self.assertIn("kept trying to ask", result["reply"])
        self.assertTrue(result["truncated"],
                        "the cards are dropped, because api_ask only keeps them "
                        "on a truncated answer")

    def test_it_does_not_blame_the_question(self):
        """"This question needs too many steps, split it into two" is the wrong
        instruction for a question that was perfectly clear and never searched."""
        result, _ = self.run_chat([_Resp([ASK])], may_ask=False)
        self.assertNotIn("too many steps", result["reply"].lower())

    def test_a_malformed_question_repeated_is_cut_off_the_same_way(self):
        bad = _Use("ask_the_user", {"question": "Which?", "options": ["Only"]})
        result, client = self.run_chat([_Resp([bad])])
        self.assertEqual(client.calls, chat_mod.ASK_REFUSALS_MAX)
        self.assertIn("kept trying to ask", result["reply"])

    def test_real_work_in_a_round_resets_the_count(self):
        """One refused question between two real searches is a mistake, not a
        groove. Cutting the answer off there would be the bug, not the fix."""
        find = _Use("find_person", {"name": "Reviewable"}, "t0")
        result, client = self.run_chat([
            _Resp([ASK]),                 # refused
            _Resp([find]),                # real work — the count resets
            _Resp([ASK]),                 # refused again
            _Resp([_Text("Here they are.")], "end_turn"),
        ], may_ask=False)
        self.assertEqual(client.calls, 4)
        self.assertEqual(result["reply"], "Here they are.")

    def test_the_people_already_found_are_kept(self):
        find = _Use("find_person", {"name": "Reviewable"}, "t0")
        result, _ = self.run_chat([_Resp([find]), _Resp([ASK])], may_ask=False)
        self.assertTrue(any(c["name"] == "Reviewable Rut"
                            for c in result["contacts"]))


# ── NC-164 ────────────────────────────────────────────────────────────────────

class TestSomethingIsSaidOutLoudWhenATurnLands(JsBase):

    def test_there_is_a_live_region(self):
        self.assertIn('class="sr-only" role="status" aria-live="polite"', self.js)

    def test_a_question_says_it_is_a_question_and_counts_its_options(self):
        self.assertIn("Ask has a question for you, with ${", self.js)

    def test_it_is_not_the_answer_text(self):
        """A long roster read aloud in full is worse than silence."""
        self.assertIn('"Answer ready"', self.js)
        self.assertNotIn("setAnnounce(d.reply", self.js)

    def test_it_is_cleared_on_the_next_send(self):
        """Two identical announcements in a row are two changes only if
        something resets it between them. Without this the second answer of a
        session is never read out."""
        self.assertIn('setAnnounce("")', self.send_body())


# ── NC-165 ────────────────────────────────────────────────────────────────────

class TestTheMemoryWindowIsOdd(Base):

    def test_the_default_is_odd(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NC_CHAT_HISTORY_CONTEXT", None)
            self.assertEqual(api_ask.chat_history_context() % 2, 1)

    def test_every_setting_comes_out_odd(self):
        for sent, want in (("20", 19), ("21", 21), ("2", 1), ("1", 1),
                           ("200", 199), ("500", 199), ("nonsense", 19)):
            with self.subTest(sent=sent):
                with mock.patch.dict(os.environ,
                                     {"NC_CHAT_HISTORY_CONTEXT": sent}):
                    self.assertEqual(api_ask.chat_history_context(), want)

    def test_a_long_chat_no_longer_loses_a_turn(self):
        """The question is written before the window is read, so a chat past the
        window always holds an ODD number of turns. An even window off an odd
        list begins on an answer, and _llm_messages has to drop it."""
        cid = db.create_conversation(self.conn)
        for i in range(30):
            db.add_chat_message(self.conn, "user" if i % 2 == 0 else "assistant",
                                f"turn {i}", conversation_id=cid)
        db.add_chat_message(self.conn, "user", "the new one", conversation_id=cid)
        limit = api_ask.chat_history_context()
        rows = db.get_chat_history(self.conn, limit=limit, conversation_id=cid)
        self.assertEqual(len(api_ask._llm_messages(rows)), len(rows),
                         "the window still starts on an answer, so the model is "
                         "fed one turn fewer than the setting paid for")

    def test_the_drop_is_still_there_as_a_safety_net(self):
        """An orphaned user turn flips the parity. The pop must stay."""
        msgs = api_ask._llm_messages([{"role": "assistant", "content": "a"},
                                      {"role": "user", "content": "b"}])
        self.assertEqual([m["role"] for m in msgs], ["user"])


# ── NC-167 ────────────────────────────────────────────────────────────────────

class TestNothingSitsBetweenClaimingAHandleAndReleasingIt(unittest.TestCase):

    def test_begin_run_is_immediately_followed_by_try(self):
        """No timer, no sweeper: the only thing that removes a registry entry is
        the finally paired with this try. A raise in the gap leaks it, and a
        leaked id can never be reused by that tab."""
        src = code_of(os.path.join(REPO, "crm", "api_ask.py"))
        at = src.index("chat.begin_run(")
        gap = src[src.index("\n", at) + 1:src.index("try:", at)]
        self.assertEqual(gap.strip(), "",
                         f"a statement sits between begin_run and its try: {gap!r}")


# ── NC-168 ────────────────────────────────────────────────────────────────────

class TestOneMistypedArgumentCostsOneToolCall(LoopBase):

    def test_as_int_reads_what_a_model_actually_writes(self):
        for sent, want in ((None, 0), ("", 0), ("7", 7), (7, 7), (7.9, 7),
                           ("high", None), ("", 0), ([], None), ({}, None),
                           (True, None), (False, None), ("12abc", None)):
            with self.subTest(sent=sent):
                self.assertEqual(chat_mod._as_int(sent), want)

    def test_a_word_where_a_number_belongs_is_a_tool_error(self):
        for name, args, word in (
                ("search_people", {"min_strength": "high"}, "min_strength"),
                ("search_conversations", {"contact_id": "Ture"}, "contact_id"),
                ("search_messages", {"contact_id": "Ture"}, "contact_id"),
                ("search_messages", {"context": "a few"}, "context")):
            with self.subTest(tool=name, args=args):
                out = chat_mod.run_tool(self.conn, name, args)
                self.assertIn(word, out.get("error", ""))
                self.assertNotIn("unknown tool", out.get("error", ""))

    def test_the_answer_survives_it(self):
        """THE POINT. The raise used to leave run_tool, leave chat(), and land in
        handle_chat's broad except — which threw away every round already paid
        for and answered from keyword search with the Python error stapled on."""
        result, client = self.run_chat([
            _Resp([_Use("search_people", {"min_strength": "high"}, "t0")]),
            _Resp([_Text("Here they are.")], "end_turn"),
        ])
        self.assertEqual(result["reply"], "Here they are.")
        self.assertEqual(client.calls, 2)

    def test_the_model_is_told_inside_the_round_it_paid_for(self):
        _, client = self.run_chat([
            _Resp([_Use("search_people", {"min_strength": "high"}, "t0")]),
            _Resp([_Text("ok")], "end_turn"),
        ])
        sent = json.dumps(client.requests[-1]["messages"], default=str)
        self.assertIn("min_strength must be", sent)

    def test_a_missing_number_still_means_zero(self):
        """Absent and empty both meant zero before this guard existed, and the
        schema defaults depend on it — a guard that changed them would be a
        different bug wearing the fix's clothes."""
        out = chat_mod.run_tool(self.conn, "search_people",
                                {"location": "London"})
        self.assertNotIn("error", out)
        self.assertIn("contacts", out)


if __name__ == "__main__":
    unittest.main()
