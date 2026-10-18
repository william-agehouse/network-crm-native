"""NC-156: Ask asks a question back instead of guessing which one you meant.

THE PROBLEM. "Who in my network could I introduce them to?", asked about a new
contact — a question with at least three readings (colleagues, clients, friends)
whose answers share almost no people. Ask used to pick one silently. The fix is
to come back with the question and some buttons.

THE HARD HALF IS THE RULE FOR NOT ASKING. There are ten other tools and a network
of thousands of people, so nearly every ambiguity a question carries is one the
model can settle by searching: "which Ture?" is a find_person call, not a question
for a human. A model that asks every turn triples the cost of every answer — ask,
read, answer — and is tedious to use. Most of what this file guards is the
restraint, not the feature.

WHAT THESE GUARD

  * the turn ENDS on the question. No assistant turn carrying a tool_use block
    with no matching tool_result is ever appended to the conversation, because
    the API rejects that outright and it would poison every later round
  * no tool in that round runs, so a question costs one round and not one round
    plus a sentence-transformer load
  * a question turn ships NO contact cards. api_ask falls back to `named or
    contacts` when the prose names nobody, and a question names nobody by
    construction — so cards here would put the entire unfiltered search result
    under "colleagues or clients?" as though it were the answer
  * never two questions running, read off the transcript rather than stored
  * the tool is STILL OFFERED when asking is forbidden. Dropping it from TOOLS
    would re-price the whole conversation: TOOLS sits in the cached prefix
  * a question with fewer than two distinct options is refused, and the refusal
    reaches the MODEL as a tool error it can fix in the round already paid for
  * a reload puts the buttons back, because the answer was never written down
    and a question without its options is a dead end
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
ASK_CSS = os.path.join(REPO, "static", "css", "view-ask.css")


# ── Fake model responses ──────────────────────────────────────────────────────

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
        self.content = content
        self.stop_reason = stop_reason
        self.usage = types.SimpleNamespace(input_tokens=1000, output_tokens=50,
                                           cache_read_input_tokens=0,
                                           cache_creation_input_tokens=0)


class _ScriptedClient:
    """Plays a fixed list of responses and records every request it was sent.

    It records `tools` on every call because one of the rules here is about what
    is OFFERED rather than about what is returned: withholding ask_the_user when
    asking is forbidden would invalidate the cached prefix, and the only way to
    see that is to look at the request.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.requests = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        self.requests.append(kw)
        return self.script[min(self.calls - 1, len(self.script) - 1)]


ASKED = _Use("ask_the_user",
             {"question": "Who should I look for?",
              "options": ["Colleagues", "Clients", "Friends"]})


class AskBase(Base):
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
        # Someone for find_person to find. A fixture whose searches return nobody
        # makes "a question turn ships no cards" pass for the wrong reason.
        db.create_contact(self.conn, "Askable Andersson")
        self.conn.commit()

    def tearDown(self):
        self._arc.stop()
        self._rmtree(self.arc_dir, True)
        super().tearDown()

    def run_chat(self, script, **kw):
        client = _ScriptedClient(script)
        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda **_k: client
        with mock.patch.dict(sys.modules, {"anthropic": fake}), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-a-real-key"}):
            result = chat_mod.chat(self.conn, [{"role": "user", "content": "q"}],
                                   **kw)
        return result, client


# ── The cleaner ───────────────────────────────────────────────────────────────

class TestTheQuestionIsCleanedBeforeItIsBelieved(unittest.TestCase):
    """Every field here came from a model, so every field is a suggestion."""

    def test_a_good_question_survives_intact(self):
        got = chat_mod.normalize_question(
            {"question": "Colleagues or clients?",
             "options": ["Colleagues", "Clients"]})
        self.assertEqual(got, {"question": "Colleagues or clients?",
                               "options": ["Colleagues", "Clients"],
                               "multi": False})

    def test_no_question_is_refused(self):
        for bad in ({}, {"options": ["a", "b"]},
                    {"question": "   ", "options": ["a", "b"]},
                    {"question": None, "options": ["a", "b"]}):
            self.assertIsNone(chat_mod.normalize_question(bad), bad)

    def test_one_option_is_not_a_choice(self):
        self.assertIsNone(chat_mod.normalize_question(
            {"question": "Which?", "options": ["Only this"]}))

    def test_no_options_is_refused(self):
        for bad in ([], None, 7, {"a": 1}):
            self.assertIsNone(chat_mod.normalize_question(
                {"question": "Which?", "options": bad}), bad)

    def test_a_bare_string_is_one_option_not_nine_letters(self):
        """The mistake a model makes when it has one option in mind. Iterating
        it would render "Colleagues" as ten single-letter buttons."""
        self.assertIsNone(chat_mod.normalize_question(
            {"question": "Which?", "options": "Colleagues"}))

    def test_two_spellings_of_one_word_are_one_button(self):
        """"Colleagues" and "colleagues" meant one choice. Two buttons that look
        identical and send different text is worse than one."""
        self.assertIsNone(chat_mod.normalize_question(
            {"question": "Which?", "options": ["Colleagues", "colleagues  "]}))

    def test_order_is_kept(self):
        got = chat_mod.normalize_question(
            {"question": "Which?", "options": ["C", "A", "B"]})
        self.assertEqual(got["options"], ["C", "A", "B"])

    def test_five_options_are_cut_to_four(self):
        got = chat_mod.normalize_question(
            {"question": "Which?", "options": ["a", "b", "c", "d", "e"]})
        self.assertEqual(got["options"], ["a", "b", "c", "d"])
        self.assertEqual(len(got["options"]), chat_mod.ASK_MAX_OPTIONS)

    def test_empty_and_non_text_options_are_dropped(self):
        got = chat_mod.normalize_question(
            {"question": "Which?", "options": ["A", "", "   ", None,
                                               {"not": "text"}, "B"]})
        self.assertEqual(got["options"], ["A", "B"])

    def test_a_long_question_is_cut_not_refused(self):
        """Cut rather than rejected: a clipped question is still answerable, a
        refused one costs the user a whole round."""
        got = chat_mod.normalize_question(
            {"question": "x" * 900, "options": ["a", "b"]})
        self.assertEqual(len(got["question"]), chat_mod.ASK_QUESTION_MAX)

    def test_a_long_option_is_cut_to_a_button(self):
        got = chat_mod.normalize_question(
            {"question": "Which?", "options": ["y" * 400, "b"]})
        self.assertEqual(len(got["options"][0]), chat_mod.ASK_OPTION_MAX)

    def test_multi_is_true_only_when_the_model_sent_a_real_boolean(self):
        """NC-161. bool() on this field made the string "false" mean True, which
        is the opposite of what was written. The schema declares a boolean, so a
        string is a model error either way — and False is the safe way to be
        wrong: it costs a click, where a wrong True hides the answer behind a
        Send button nobody noticed."""
        for sent in ("yes", "false", "true", 1, 0, [], None, "0"):
            with self.subTest(multi=sent):
                got = chat_mod.normalize_question(
                    {"question": "Which?", "options": ["a", "b"], "multi": sent})
                self.assertIs(got["multi"], False)
        got = chat_mod.normalize_question(
            {"question": "Which?", "options": ["a", "b"], "multi": True})
        self.assertIs(got["multi"], True)
        got = chat_mod.normalize_question(
            {"question": "Which?", "options": ["a", "b"]})
        self.assertIs(got["multi"], False)

    def test_junk_instead_of_an_object_is_refused(self):
        for bad in (None, "question?", ["a"], 3):
            self.assertIsNone(chat_mod.normalize_question(bad), bad)


# ── The turn ends on the question ─────────────────────────────────────────────

class TestTheTurnEndsWithTheQuestion(AskBase):

    def test_the_answer_is_the_question(self):
        result, client = self.run_chat([_Resp([ASKED])])
        self.assertEqual(result["question"]["question"], "Who should I look for?")
        self.assertEqual(result["reply"], "Who should I look for?")
        self.assertEqual(result["question"]["options"],
                         ["Colleagues", "Clients", "Friends"])

    def test_it_does_not_ask_the_model_again(self):
        """The turn is over. A second round here would be a paid call whose
        answer nobody is waiting for."""
        _, client = self.run_chat([_Resp([ASKED])])
        self.assertEqual(client.calls, 1)

    def test_no_tool_in_that_round_runs(self):
        """A round can carry several calls, and semantic_search loads a
        sentence-transformer model — seconds of CPU whose results a question turn
        then throws away."""
        ran = []
        real = chat_mod.run_tool

        def spy(conn, name, tool_input, caps=None, **kw):
            ran.append(name)
            return real(conn, name, tool_input, caps, **kw)

        with mock.patch.object(chat_mod, "run_tool", spy):
            self.run_chat([_Resp([_Use("find_person", {"name": "Askable"}, "t0"),
                                  ASKED])])
        self.assertEqual(ran, [], f"tools ran on a question turn: {ran}")

    def test_a_question_turn_carries_no_contact_cards(self):
        """THE ONE THAT MATTERS. api_ask matches names in the prose against what
        the searches found and falls back to `named or contacts` — everyone —
        when the prose names nobody. A question names nobody by construction, so
        cards here would arrive as the whole unfiltered search result presented
        as the answer."""
        result, _ = self.run_chat([
            _Resp([_Use("find_person", {"name": "Askable"}, "t0")]),
            _Resp([ASKED]),
        ])
        self.assertEqual(result["contacts"], [])

    def test_leading_text_is_kept_above_the_question(self):
        result, _ = self.run_chat(
            [_Resp([_Text("Paris has 40 people in it."), ASKED])])
        self.assertTrue(result["reply"].startswith("Paris has 40 people in it."))
        self.assertIn("Who should I look for?", result["reply"])

    def test_the_conversation_never_grows_an_unanswered_tool_use(self):
        """An assistant turn holding a tool_use block with no matching
        tool_result is rejected outright by the API. The early return is ABOVE
        the append for this reason, and this is what says so."""
        src = code_of(os.path.join(REPO, "crm", "chat.py"))
        body = src[src.index("def chat(conn"):]
        self.assertLess(body.index('"question": asked'),
                        body.index('convo.append({"role": "assistant"'),
                        "the question is returned AFTER the assistant turn is "
                        "appended, which puts an unanswered tool_use into the "
                        "conversation")

    def test_a_question_is_not_a_truncated_answer(self):
        result, _ = self.run_chat([_Resp([ASKED])])
        self.assertFalse(result["truncated"])

    def test_the_bill_so_far_is_still_reported(self):
        result, _ = self.run_chat([
            _Resp([_Use("find_person", {"name": "Askable"}, "t0")]),
            _Resp([ASKED]),
        ])
        self.assertEqual(result["spend"]["rounds"], 2)
        self.assertGreater(result["spend"]["usd"], 0)

    def test_the_trail_says_options_offered_and_not_zero_matched(self):
        """message_stats' problem: "0 matched" in the provenance trail reads as
        a failed search, and nothing was searched here."""
        result, _ = self.run_chat([_Resp([ASKED])])
        entry = result["trail"][-1]
        self.assertEqual(entry["tool"], "ask_the_user")
        self.assertEqual(entry["detail"], "Who should I look for?")
        self.assertIn("3 options", entry["label"])

    def test_an_ordinary_answer_carries_no_question(self):
        result, _ = self.run_chat([_Resp([_Text("Here they are.")], "end_turn")])
        self.assertIsNone(result.get("question"))


# ── Not twice running ─────────────────────────────────────────────────────────

class TestItMayNotAskTwiceRunning(AskBase):

    def test_a_forbidden_question_does_not_end_the_turn(self):
        result, client = self.run_chat(
            [_Resp([ASKED]), _Resp([_Text("Colleagues, then.")], "end_turn")],
            may_ask=False)
        self.assertIsNone(result.get("question"))
        self.assertEqual(result["reply"], "Colleagues, then.")
        self.assertEqual(client.calls, 2, "the model was not given the chance to "
                                          "answer after being refused")

    def test_the_model_is_told_why_in_the_round_it_already_paid_for(self):
        out = chat_mod.run_tool(self.conn, "ask_the_user",
                                ASKED.input, None, may_ask=False)
        self.assertIn("already asked", out["error"])
        self.assertNotIn("unknown tool", out["error"])

    def test_the_tool_is_still_offered_when_asking_is_forbidden(self):
        """NOT a simplification to make. TOOLS sits in the cached prefix of every
        request, so removing an entry re-prices the whole conversation, on a
        feature whose entire point is that it is cheaper than guessing wrong."""
        _, client = self.run_chat(
            [_Resp([ASKED]), _Resp([_Text("ok")], "end_turn")], may_ask=False)
        for req in client.requests:
            names = [t["name"] for t in req["tools"]]
            self.assertIn("ask_the_user", names,
                          "ask_the_user was withheld, which invalidates the "
                          "prompt cache for the whole conversation")

    def test_the_tools_list_is_never_rebuilt_per_request(self):
        _, client = self.run_chat([_Resp([_Text("ok")], "end_turn")])
        self.assertIs(client.requests[0]["tools"], chat_mod.TOOLS)


class TestWhoDecidesWhetherItMayAsk(Base):
    """Read off the transcript, because the transcript already knows."""

    def setUp(self):
        super().setUp()
        # A real chat row: chat_history.conversation_id carries a REFERENCES
        # clause, so an invented id is a foreign-key error and not a fixture.
        self.cid = db.create_conversation(self.conn)

    def _turn(self, role, content, ask=None):
        db.add_chat_message(self.conn, role, content, conversation_id=self.cid,
                            ask_json=json.dumps(ask) if ask else "")

    def _history(self):
        return db.get_chat_history(self.conn, limit=50, conversation_id=self.cid)

    def test_true_when_the_last_answer_was_a_question(self):
        self._turn("user", "who?")
        self._turn("assistant", "Which kind?",
                   {"question": "Which kind?", "options": ["A", "B"]})
        self._turn("user", "A")
        self.assertTrue(api_ask._asked_last_turn(self._history()))

    def test_false_when_the_last_answer_was_an_answer(self):
        self._turn("user", "who?")
        self._turn("assistant", "Which kind?",
                   {"question": "Which kind?", "options": ["A", "B"]})
        self._turn("user", "A")
        self._turn("assistant", "Here they are.")
        self._turn("user", "and in Paris?")
        self.assertFalse(api_ask._asked_last_turn(self._history()),
                         "it read an older question instead of the newest answer")

    def test_false_on_a_first_question(self):
        self._turn("user", "who?")
        self.assertFalse(api_ask._asked_last_turn(self._history()))
        self.assertFalse(api_ask._asked_last_turn([]))

    def test_corrupt_json_costs_the_buttons_and_nothing_else(self):
        db.add_chat_message(self.conn, "assistant", "Which kind?",
                            conversation_id=self.cid, ask_json="{not json")
        self.assertFalse(api_ask._asked_last_turn(self._history()))

    def test_a_row_older_than_the_column_is_not_a_question(self):
        self.assertFalse(api_ask._asked_last_turn(
            [{"role": "assistant", "content": "Here they are."}]))

    def test_a_stored_blob_with_no_question_is_not_a_question(self):
        """Valid JSON is not the same as a question. A row holding {} or only
        options would otherwise block the next turn from asking AND put a row of
        buttons on screen under no question at all."""
        for blob in ("{}", '{"options": ["A", "B"]}', '{"question": ""}',
                     '"a string"', "[]", "null"):
            with self.subTest(blob=blob):
                db.add_chat_message(self.conn, "assistant", "Here they are.",
                                    conversation_id=self.cid, ask_json=blob)
                self.assertFalse(api_ask._asked_last_turn(self._history()), blob)
                self.assertIsNone(api_ask._stored_question(
                    self._history()[-1]), blob)

    def test_handle_chat_passes_the_verdict_down(self):
        seen = {}

        def fake_chat(conn, messages, **kw):
            seen.update(kw)
            return {"reply": "ok", "contacts": [], "trail": [], "truncated": False}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(api_ask.chat, "chat", fake_chat):
            api_ask.handle_chat(self.conn, {"message": "first"})
            self.assertIs(seen["may_ask"], True)
        cid = db.newest_conversation_id(self.conn)
        db.add_chat_message(self.conn, "assistant", "Which kind?",
                            conversation_id=cid,
                            ask_json=json.dumps({"question": "Which kind?",
                                                 "options": ["A", "B"]}))
        self.conn.commit()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(api_ask.chat, "chat", fake_chat):
            api_ask.handle_chat(self.conn, {"message": "second",
                                            "conversation_id": cid})
            self.assertIs(seen["may_ask"], False)


# ── An unusable question ──────────────────────────────────────────────────────

class TestAnUnusableQuestionIsRefusedNotShown(AskBase):

    def test_run_tool_knows_the_tool_exists(self):
        """A tool declared to the model and not routed comes back as "unknown
        tool", and the failure then looks like the model's fault."""
        out = chat_mod.run_tool(self.conn, "ask_the_user", {})
        self.assertNotIn("unknown tool", out["error"])
        self.assertIn("options", out["error"])

    def test_one_option_does_not_end_the_turn(self):
        bad = _Use("ask_the_user", {"question": "Which?", "options": ["Only"]})
        result, client = self.run_chat(
            [_Resp([bad]), _Resp([_Text("Colleagues, then.")], "end_turn")])
        self.assertIsNone(result.get("question"))
        self.assertEqual(result["reply"], "Colleagues, then.")
        self.assertEqual(client.calls, 2)


# ── It survives a reload ──────────────────────────────────────────────────────

class TestTheButtonsComeBackAfterAReload(Base):

    def _ask(self, question):
        def fake_chat(conn, messages, **_kw):
            return {"reply": question["question"], "question": question,
                    "contacts": [], "trail": [], "truncated": False}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(api_ask.chat, "chat", fake_chat):
            return api_ask.handle_chat(self.conn, {"message": "introductions?"})

    def test_the_question_is_written_with_the_turn(self):
        q = {"question": "Which kind?", "options": ["A", "B"], "multi": False}
        out = self._ask(q)
        self.assertEqual(out["question"], q)
        row = self.conn.execute(
            "SELECT ask_json FROM chat_history WHERE role='assistant' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(row["ask_json"]), q)

    def test_the_transcript_hands_the_options_back(self):
        q = {"question": "Which kind?", "options": ["A", "B"], "multi": True}
        self._ask(q)
        msgs = api_ask.handle_chat_history_get(self.conn)["messages"]
        self.assertEqual(msgs[-1]["question"], q)

    def test_an_ordinary_answer_writes_nothing(self):
        def fake_chat(conn, messages, **_kw):
            return {"reply": "Here they are.", "contacts": [], "trail": [],
                    "truncated": False}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(api_ask.chat, "chat", fake_chat):
            api_ask.handle_chat(self.conn, {"message": "who?"})
        row = self.conn.execute(
            "SELECT ask_json FROM chat_history WHERE role='assistant' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["ask_json"], "")
        msgs = api_ask.handle_chat_history_get(self.conn)["messages"]
        self.assertNotIn("question", msgs[-1],
                         "an ordinary turn carries a null the renderer has to "
                         "test, instead of carrying nothing")

    def test_a_stopped_answer_asks_nothing(self):
        """chat.py's stop path returns before a question can be formed, so this
        is empty by construction. It is asserted anyway because "stopped" and
        "asked" are the two ways a turn can end early, and a stopped answer
        showing buttons would offer to continue a thread that was cut off."""
        def fake_chat(conn, messages, **_kw):
            return {"reply": "Stopped.", "contacts": [], "trail": [],
                    "truncated": True, "stopped": True}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(api_ask.chat, "chat", fake_chat):
            api_ask.handle_chat(self.conn, {"message": "who?"})
        row = self.conn.execute(
            "SELECT ask_json FROM chat_history WHERE role='assistant' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["ask_json"], "")

    def test_a_corrupt_row_costs_the_buttons_not_the_transcript(self):
        db.add_chat_message(self.conn, "assistant", "Which kind?",
                            conversation_id=db.create_conversation(self.conn),
                            ask_json="{half written")
        self.conn.commit()
        msgs = api_ask.handle_chat_history_get(self.conn)["messages"]
        self.assertEqual(msgs[-1]["content"], "Which kind?")
        self.assertNotIn("question", msgs[-1])

    def test_nothing_records_whether_it_was_answered(self):
        """The turn after a question IS the answer, so the transcript already
        knows. A stored flag would be a second copy of the same fact, free to
        disagree with the first."""
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(chat_history)")}
        self.assertNotIn("answered", cols)
        self.assertNotIn("ask_answered", cols)


# ── What the model is shown ───────────────────────────────────────────────────

class TestTheModelSeesTheOptionsItOffered(Base):

    def setUp(self):
        super().setUp()
        self.cid = db.create_conversation(self.conn)

    def _rows(self, ask=None):
        cid = self.cid
        db.add_chat_message(self.conn, "user", "introductions?", conversation_id=cid)
        db.add_chat_message(self.conn, "assistant", "Which kind?",
                            conversation_id=cid,
                            ask_json=json.dumps(ask) if ask else "")
        db.add_chat_message(self.conn, "user", "Colleagues", conversation_id=cid)
        return db.get_chat_history(self.conn, limit=50, conversation_id=cid)

    def test_the_options_are_appended_for_the_model(self):
        """Without them the model reads a bare "Colleagues" answering a question
        whose choices are nowhere in the window."""
        msgs = api_ask._llm_messages(
            self._rows({"question": "Which kind?",
                        "options": ["Colleagues", "Clients"]}))
        self.assertIn("(Options offered: Colleagues, Clients)",
                      msgs[1]["content"])
        self.assertTrue(msgs[1]["content"].startswith("Which kind?"))

    def test_it_is_context_and_never_the_transcript(self):
        """What is stored, and what the user sees, is the bare question: the
        options were buttons, and writing them into the prose says them twice."""
        self._rows({"question": "Which kind?", "options": ["A", "B"]})
        row = self.conn.execute(
            "SELECT content FROM chat_history WHERE role='assistant'").fetchone()
        self.assertEqual(row["content"], "Which kind?")
        msgs = api_ask.handle_chat_history_get(self.conn, self.cid)["messages"]
        self.assertNotIn("Options offered", msgs[1]["content"])

    def test_an_ordinary_turn_is_untouched(self):
        msgs = api_ask._llm_messages(self._rows())
        self.assertEqual(msgs[1]["content"], "Which kind?")

    def test_a_row_older_than_the_column_still_builds_a_window(self):
        msgs = api_ask._llm_messages([{"role": "user", "content": "hi"},
                                      {"role": "assistant", "content": "there"}])
        self.assertEqual([m["content"] for m in msgs], ["hi", "there"])


# ── The screen ────────────────────────────────────────────────────────────────

class TestTheOptionsAreButtonsOnScreen(unittest.TestCase):

    def setUp(self):
        self.js = js_code_of(ASK_JS)
        with open(ASK_CSS, encoding="utf-8") as fh:
            self.css = fh.read()

    def test_the_component_exists_and_is_rendered(self):
        self.assertIn("function AskOptions(", self.js)
        self.assertIn("<${AskOptions}", self.js)

    def test_it_reuses_the_chip_primitive(self):
        """The app already has a look for "a small set of alternatives, one of
        which you pick" — the shape chips. A second one would be a fourth button
        shape for the same job."""
        block = self.js[self.js.index("function AskOptions("):]
        block = block[:block.index("\nfunction ")]
        self.assertIn('class="chip', block.replace("class=${\"chip", 'class="chip'))
        self.assertIn(".ask-opts", self.css)

    def test_one_click_answers_a_single_choice_question(self):
        block = self.js[self.js.index("function AskOptions("):]
        block = block[:block.index("\nfunction ")]
        self.assertIn("onPick(o)", block,
                      "answering costs a click and then a Send press, which is "
                      "the ceremony that stops people answering at all")

    def test_answered_is_read_off_the_transcript(self):
        """A source gate, because this app has no JavaScript test runner — the
        house pattern is tests/test_frontend_gates.py. It reads BOTH lines on
        purpose: checking only that the next turn is looked up would let a
        hardcoded `answered = true` sail straight past while the lookup sits
        above it doing nothing."""
        self.assertIn("(msgs || [])[i + 1]", self.js,
                      "the turn after the question is never looked up")
        self.assertIn("const answered = !!nextTurn && nextTurn.role === \"user\";",
                      self.js,
                      "answered is not derived from the turn that follows the "
                      "question, so it is a stored or invented flag — and "
                      "NC-166: that turn must be the USER's, or two answers in "
                      "a row render a question as answered with dead buttons")

    def test_the_live_answer_carries_the_question_through(self):
        """This object is built key by key, so a server field nobody names is
        silently dropped — which is how the card counts could go missing."""
        self.assertIn("question: d.question || null", self.js)

    def test_the_composer_is_not_disabled_by_a_question(self):
        """The buttons are a shortcut, not a gate: a reading the model did not
        offer must still be typeable."""
        self.assertNotIn("disabled=${!!gone || !!question", self.js)
        self.assertIn('disabled=${!!gone}', self.js)


if __name__ == "__main__":
    unittest.main()
