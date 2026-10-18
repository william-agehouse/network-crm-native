"""NC-151 gate: hiding someone has to reach the answers already written down.

NC-145 closed every path Ask uses to READ the contacts table. It did not touch
what Ask had already written, and there were two of those:

  * every answer freezes its contact cards into `chat_history.contacts_json`,
    and `GET /api/chat/{id}/history` served them back with no scope check at
    all — so a person hidden today was still named in yesterday's answer, and
    the "Open <name>" button under it still worked;
  * every following question in that thread replays the earlier turns' prose to
    Anthropic, and the prose carries whatever the tools returned at the time. So
    a person hidden mid-conversation went on being sent, question after
    question, for as long as the thread lived.

**The rule is to re-check on read.** Nothing hidden is sent again or turned
into a working link. The prose stays exactly as it was written, and nothing in
the database is rewritten — so un-hiding someone brings their card straight
back. A chat may read a little oddly afterwards, a name in a sentence with no
card beneath it, and that is the trade-off accepted.

Two things here are easy to get wrong and are pinned hard.

  * **The pair goes, not the turn.** Dropping only the assistant turn puts two
    user turns together and leaves a question answered by the turn after it —
    and `_llm_messages`' own docstring is about what a malformed window costs:
    the request raises, the fallback catches it, and the chat quietly stops
    using the LLM at all. Dropping the user turn with it is also the honest cut,
    because "what do I know about Anna" names her in the user's own words, which
    no card-based check could ever see.
  * **An all-cards answer must not become "No contact matches that question."**
    A roster answer is often prose-free. Filtering its cards away and falling
    through to that sentence would replace one lie with a fresher one.
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db, js_code_of  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import api_ask  # noqa: E402
import chat  # noqa: E402
import figures  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


def card(cid, name):
    return {"id": cid, "name": name, "company": "Acme", "position": "CTO",
            "city": "London", "category": "friend"}


class SavedChat(Base):
    """One chat that named two people, one of whom is about to be hidden."""

    def setUp(self):
        super().setUp()
        self.cid = db.create_conversation(self.conn)
        self.anna = db.create_contact(self.conn, "Anna Andersson")
        self.bo = db.create_contact(self.conn, "Bo Bergman")
        db.add_chat_message(self.conn, "user", "who do I know in London?",
                            conversation_id=self.cid)
        db.add_chat_message(
            self.conn, "assistant",
            "Anna Andersson and Bo Bergman are both in London.",
            contacts_json=json.dumps([card(self.anna, "Anna Andersson"),
                                      card(self.bo, "Bo Bergman")]),
            conversation_id=self.cid)
        self.conn.commit()

    def answer(self):
        msgs = api_ask.handle_chat_history_get(self.conn, self.cid)["messages"]
        return next(m for m in msgs if m["role"] == "assistant")

    def hide(self, contact_id):
        db.set_excluded(self.conn, contact_id, 1)
        self.conn.commit()


class TestTheCardGoesAndTheProseStays(SavedChat):

    def test_before_hiding_both_cards_are_there(self):
        got = self.answer()
        self.assertEqual([c["id"] for c in got["contacts"]], [self.anna, self.bo])
        self.assertNotIn("contacts_withheld", got,
                         "an unaffected answer must carry no key at all, not a zero")

    def test_hiding_someone_takes_their_card_out_of_a_saved_answer(self):
        self.hide(self.anna)
        got = self.answer()
        self.assertEqual([c["id"] for c in got["contacts"]], [self.bo])
        self.assertNotIn("Anna", json.dumps(got["contacts"]),
                         "the hidden person is still in the payload the page "
                         "turns into a working 'Open Anna Andersson' button")

    def test_the_sentence_is_untouched(self):
        self.hide(self.anna)
        self.assertEqual(
            self.answer()["content"],
            "Anna Andersson and Bo Bergman are both in London.",
            "the prose was rewritten — the rule is to stop LINKING and "
            "SENDING them, not to edit history")

    def test_it_says_how_many_it_withheld(self):
        self.hide(self.anna)
        self.assertEqual(self.answer()["contacts_withheld"], 1)

    def test_nothing_in_the_database_was_rewritten(self):
        self.hide(self.anna)
        self.answer()
        raw = self.conn.execute(
            "SELECT contacts_json FROM chat_history WHERE role = 'assistant'"
        ).fetchone()["contacts_json"]
        self.assertIn("Anna Andersson", raw,
                      "the stored card was scrubbed; hiding must never rewrite "
                      "saved answers, because that cannot be undone")

    def test_un_hiding_brings_the_card_straight_back(self):
        self.hide(self.anna)
        self.assertEqual(len(self.answer()["contacts"]), 1)
        db.set_excluded(self.conn, self.anna, 0)
        self.conn.commit()
        got = self.answer()
        self.assertEqual([c["id"] for c in got["contacts"]], [self.anna, self.bo])
        self.assertNotIn("contacts_withheld", got)

    def test_a_deleted_person_is_filtered_too(self):
        """Ask's population is `active`, which is neither hidden nor deleted."""
        db.set_deleted(self.conn, self.bo, 1)
        self.conn.commit()
        self.assertNotIn(self.bo, [c["id"] for c in self.answer()["contacts"]],
                         "deleting is the stronger statement of the two and it "
                         "reaches less far than hiding does")

    def test_it_uses_the_one_population_and_not_a_second_spelling(self):
        self.hide(self.anna)
        hidden = chat._out_of_scope_ids(self.conn)
        self.assertIn(self.anna, hidden)
        self.assertNotIn(self.bo, hidden)
        self.assertEqual(chat.ASK_POPULATION, "active",
                         "if Ask's population moves, this filter has to move "
                         "with it — which it does only while it asks for the "
                         "population by name instead of writing its own SQL")
        self.assertIn("excluded", figures.population_where(chat.ASK_POPULATION))


class TestTheOtherCardShapeIsFilteredToo(Base):
    """Two different shapes land in this one column, and the wider one is the
    one that carries free text about the person.

    The LLM path freezes `chat._contact_summary` — ten keys, no prose. The
    rules path (`handle_ask`, reached whenever there is no API key OR `chat()`
    raises) freezes fourteen, including `blurb` and `topics`. A filter that
    happened to key off something only the LLM shape has would leave the wider
    one intact, which is the wrong way round.
    """

    def test_a_rules_path_card_is_filtered_by_id_like_any_other(self):
        cid = db.create_conversation(self.conn)
        who = db.create_contact(self.conn, "Cecilia Carlsson")
        db.add_chat_message(
            self.conn, "assistant", "Cecilia Carlsson came up.",
            contacts_json=json.dumps([{
                "id": who, "name": "Cecilia Carlsson", "company": "", "position": "",
                "home_city": "", "home_metro": "", "role_type": "",
                "topics": ["robotics", "logistics"],
                "blurb": "Met at a workshop; introduced me to two colleagues.",
                "strength": 3, "strength_label": "warm", "last": "", "reason": "",
                "score": 0.8}]),
            conversation_id=cid)
        db.set_excluded(self.conn, who, 1)
        self.conn.commit()

        got = next(m for m in api_ask.handle_chat_history_get(
            self.conn, cid)["messages"] if m["role"] == "assistant")

        self.assertEqual(got["contacts"], [])
        self.assertEqual(got["contacts_withheld"], 1)
        self.assertNotIn("a workshop", json.dumps(got["contacts"]),
                         "the rules-path card carries a written note about the "
                         "person, which is more than the LLM one ever does")


class TestNothingHiddenIsSentAgain(SavedChat):
    """The half that costs money and leaves the machine."""

    def rows(self):
        return db.get_chat_history(self.conn, limit=50, conversation_id=self.cid)

    def replay(self):
        return api_ask._llm_messages(self.rows(), chat._out_of_scope_ids(self.conn))

    def test_before_hiding_the_whole_thread_is_replayed(self):
        self.assertEqual([m["role"] for m in self.replay()], ["user", "assistant"])

    def test_the_turn_that_named_them_is_not_replayed(self):
        self.hide(self.anna)
        sent = json.dumps(self.replay())
        self.assertNotIn("Anna Andersson", sent,
                         "a person hidden mid-conversation is still sent to "
                         "Anthropic on every following question in that thread")

    def test_the_question_goes_with_its_answer(self):
        """The user's own words can name someone no card ever recorded."""
        self.hide(self.anna)
        self.assertEqual(self.replay(), [],
                         "the assistant turn went and its question stayed, which "
                         "leaves two user turns together and a question answered "
                         "by the turn after it")

    def test_a_thread_that_never_named_them_is_untouched(self):
        other = db.create_conversation(self.conn)
        db.add_chat_message(self.conn, "user", "how many people do I have?",
                            conversation_id=other)
        db.add_chat_message(self.conn, "assistant", "About 400.",
                            conversation_id=other)
        self.conn.commit()
        self.hide(self.anna)
        rows = db.get_chat_history(self.conn, limit=50, conversation_id=other)
        self.assertEqual(
            [m["content"] for m in
             api_ask._llm_messages(rows, chat._out_of_scope_ids(self.conn))],
            ["how many people do I have?", "About 400."],
            "hiding one person emptied a chat that never mentioned them")

    def test_what_is_left_still_starts_on_a_user_turn(self):
        """The Anthropic API refuses a window that does not, and the fallback
        would swallow the refusal — so a long chat could stop using the LLM at
        all, silently."""
        db.add_chat_message(self.conn, "user", "and who else?",
                            conversation_id=self.cid)
        db.add_chat_message(self.conn, "assistant", "Nobody else.",
                            conversation_id=self.cid)
        self.conn.commit()
        self.hide(self.anna)
        msgs = self.replay()
        self.assertTrue(msgs, "setup: something should survive")
        self.assertEqual(msgs[0]["role"], "user")
        for a, b in zip(msgs, msgs[1:]):
            self.assertNotEqual(a["role"], b["role"],
                                "the window no longer alternates: %r"
                                % ([m["role"] for m in msgs],))

    def test_an_unreadable_card_list_does_not_crash_the_replay(self):
        db.add_chat_message(self.conn, "user", "again?", conversation_id=self.cid)
        db.add_chat_message(self.conn, "assistant", "Sure.",
                            contacts_json="{not json", conversation_id=self.cid)
        self.conn.commit()
        self.hide(self.anna)
        self.assertEqual([m["content"] for m in self.replay()],
                         ["again?", "Sure."])


class _TextBlock:
    type = "text"
    text = "Understood."


class _Reply:
    stop_reason = "end_turn"
    content = [_TextBlock()]
    usage = types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0)


class _RecordingClient:
    """Stands in for the Anthropic client and keeps what it was asked to send.

    Nothing here touches the network and no key is needed — a test that reached
    the real API would bill a real account to prove a filter works.
    """

    def __init__(self):
        self.sent = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.sent.append(kw.get("messages") or [])
        return _Reply()


class TestTheLiveQuestionPathPassesTheScope(SavedChat):
    """The end of the chain. Everything above tests _llm_messages directly; this
    asks a real follow-up question and reads what would have left the machine."""

    def setUp(self):
        super().setUp()
        self.client = _RecordingClient()
        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda **_kw: self.client
        self._mods = mock.patch.dict(sys.modules, {"anthropic": fake})
        self._mods.start()
        self.addCleanup(self._mods.stop)
        self._env = mock.patch.dict(os.environ,
                                    {"ANTHROPIC_API_KEY": "not-a-real-key"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def ask(self, text):
        api_ask.handle_chat(self.conn, {"message": text,
                                        "conversation_id": self.cid})
        return json.dumps(self.client.sent)

    def test_a_follow_up_does_not_carry_a_hidden_person(self):
        self.hide(self.anna)
        sent = self.ask("and who else?")
        self.assertTrue(self.client.sent, "setup: the model should have been called")
        self.assertNotIn("Anna Andersson", sent,
                         "asking a second question in this thread sent the "
                         "hidden person to Anthropic again")

    def test_the_control_before_hiding(self):
        """Otherwise the test above passes against a path that sends nothing."""
        sent = self.ask("and who else?")
        self.assertIn("Anna Andersson", sent,
                      "the earlier turns are not being replayed at all, so the "
                      "test above proves nothing about the filter")


class TestThePageDoesNotSwapOneLieForAnother(unittest.TestCase):

    def setUp(self):
        self.js = js_code_of(os.path.join(APP, "ask.js"))

    def test_an_emptied_answer_does_not_claim_there_was_no_match(self):
        at = self.js.index('"No contact matches that question."')
        window = self.js[max(0, at - 400):at]
        self.assertIn("withheld", window,
                      "an answer that was all cards and no prose loses every "
                      "card when those people are hidden, and then tells you "
                      "nothing matched — which swaps the leak for a false "
                      "statement")

    def test_the_page_says_what_it_withheld(self):
        self.assertIn("contacts_withheld", self.js,
                      "the page never reads the count, so a saved answer quietly "
                      "shows fewer people than it found and says nothing")
        self.assertIn("hidden now", self.js)

    def test_it_is_said_in_the_quiet_voice_the_other_cuts_use(self):
        at = self.js.index("hidden now")
        window = self.js[max(0, at - 300):at]
        self.assertIn("ask-cut", window,
                      "this belongs with the other 'what this answer left out' "
                      "lines, not as an error — the answer is still worth reading")


if __name__ == "__main__":
    unittest.main()
