"""NC-153 and NC-155: the composer holds more than one line, and an answer's
price survives the reload that used to destroy it.

TWO PROBLEMS, ONE SCREEN.

NC-153. Shift+Enter has to add a new line without sending the question.
The composer was an `<input>`, which cannot hold a newline at all — so this was
never a key-handler fix. The element had to become a textarea, and then it needs
the things an input got for free: a height that follows the content, a cap so a
long question cannot push the answer off the screen, and no drag handle fighting
the JS for control of the height.

NC-155. The API cost of each turn has to stay visible. Nothing had been
removed. `_Spend` computed it, the server sent it, `Provenance` rendered it —
and `chat_history` had nowhere to put it, persisting only `contacts_json` and
`truncated`. So `searched`, `trail` and `spend` were destroyed on every reload,
and `Provenance` opened with `if (!searched) return null`, which meant a
restored turn showed nothing at all. In practice the cost line existed for the
few seconds after an answer landed and never again.

WHAT THESE GUARD

  * Enter sends and Shift+Enter does not — asserted on the handler's own
    branches, since the DOM half is checked in a browser and cannot be here
  * the height cap exists and is read from the stylesheet rather than assumed
  * a receipt written today is readable tomorrow, through the real history
    handler, with the dollar figure intact
  * a turn written BEFORE the column existed still renders — it carries no
    receipt keys at all, which is honest rather than a row of zeros
  * a rule-based answer writes no receipt, because it touched no API and a
    receipt saying $0.00 is a claim about a request that never happened
  * corrupt JSON in the column degrades to no receipt instead of a 500

The browser half of NC-153 is checked in a browser, since a key handler's real
behaviour is what the browser does with it: Shift+Enter and Alt+Enter are left
un-prevented (the newline lands, nothing sends), an IME commit is left
un-prevented, plain Enter is prevented and sent, and the box grows with its
content up to a cap, scrolling after that.
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, code_of, db  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import api_ask                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(REPO, "static", *parts), encoding="utf-8") as fh:
        return fh.read()


class TestTheComposerHoldsMoreThanOneLine(unittest.TestCase):
    """NC-153. Source-level, deliberately: these assert the DECISIONS, and the
    behaviour they produce is checked in a browser (see the module docstring).
    A test that re-implements a key handler in Python proves nothing about the
    one the browser runs."""

    def setUp(self):
        self.js = _read("app", "ask.js")
        self.css = _read("css", "view-ask.css")
        at = self.js.index("<textarea class=\"input ask-input\"")
        self.composer = self.js[at:self.js.index("</textarea>", at)]

    def test_it_is_a_textarea_and_not_an_input(self):
        """The whole fix. An `<input>` silently discards a newline, so no
        keyboard handling on one could ever have satisfied the request."""
        self.assertIn("<textarea", self.js)
        self.assertNotIn('<input class="input" ref=${inputRef}', self.js,
                         "the single-line composer is still there")

    def test_shift_enter_is_left_alone_so_the_newline_lands(self):
        self.assertIn("e.shiftKey", self.composer)
        self.assertRegex(self.composer, r"if \(e\.shiftKey \|\| e\.altKey\) return")

    def test_plain_enter_still_sends_and_swallows_its_own_newline(self):
        """preventDefault matters as much as send() does: without it the
        question is sent AND a newline is left in the box."""
        self.assertIn("e.preventDefault()", self.composer)
        # `send(text` and not `send(text)`: NC-159 gave send() a second argument
        # saying the composer is the sender, which is what lets it clear the box
        # here and leave a draft alone when an option button sends instead.
        self.assertRegex(self.composer, r"send\(text\b")

    def test_an_ime_commit_does_not_send_a_half_written_question(self):
        """Typing Japanese or Chinese, Enter CONFIRMS the candidate word. Without
        this check that keystroke sends the question mid-word — and plenty of
        people write in other alphabets."""
        self.assertIn("isComposing", self.composer)
        self.assertIn("229", self.composer, "the older keyCode spelling, for Safari")

    def test_the_height_is_capped_and_the_cap_is_a_named_number(self):
        m = re.search(r"const ASK_ROWS_MAX = (\d+);", self.js)
        self.assertIsNotNone(m, "the row cap should be named, not buried in a literal")
        self.assertGreaterEqual(int(m.group(1)), 3)
        self.assertLessEqual(int(m.group(1)), 20,
                             "a composer that tall pushes the answer off screen")

    def test_the_height_resets_before_it_measures(self):
        """scrollHeight never reports less than the height already set, so
        without this the box grows and then never shrinks again."""
        grow = self.js[self.js.index("function autoGrow"):]
        grow = grow[:grow.index("\n}")]
        self.assertIn('el.style.height = "auto"', grow)
        self.assertLess(grow.index('"auto"'), grow.index("scrollHeight"),
                        "it must reset BEFORE measuring, or deleting a line "
                        "leaves the box stuck at its tallest")

    def test_sending_and_prefilling_both_resize(self):
        """Two paths change the value without a keystroke. Emptying the box on
        send leaves it tall; staging a long phrase from the command palette
        leaves it one line with the text half hidden."""
        self.assertEqual(self.js.count("autoGrow("), 4,
                         "autoGrow should be defined once and called on input, "
                         "on send and on prefill")

    def test_the_drag_handle_is_off(self):
        """The JS owns the height. A user-dragged one fights it on every
        keystroke, which reads as the box twitching."""
        block = self.css[self.css.index(".composer .ask-input"):]
        self.assertIn("resize: none", block[:block.index("}")])

    def test_the_send_button_stays_on_the_last_line(self):
        self.assertIn(".composer { align-items: flex-end; }", self.css)


class TestWhatAnAnswerCostSurvivesTheReload(Base):
    """NC-155, through the real history handler rather than the column."""

    RECEIPT = {
        "searched": {"contacts": 120, "conversations": 340, "messages": 4810},
        "trail": [{"tool": "find_person", "detail": "Ture", "matched": 3, "total": 3}],
        "spend": {"usd": 0.15, "rounds": 4, "model": "example-model",
                  "input_tokens": 12000, "output_tokens": 800,
                  "cache_read_tokens": 9000},
    }

    def setUp(self):
        super().setUp()
        self.cid = db.create_conversation(self.conn)

    def _restored(self, content):
        out = api_ask.handle_chat_history_get(self.conn, self.cid)
        return next(m for m in out["messages"] if m["content"] == content)

    def test_the_price_of_an_answer_is_still_there_tomorrow(self):
        db.add_chat_message(self.conn, "assistant", "Three people.",
                            conversation_id=self.cid,
                            receipt_json=json.dumps(self.RECEIPT))
        self.conn.commit()
        got = self._restored("Three people.")
        self.assertEqual(got["spend"]["usd"], 0.15)
        self.assertEqual(got["spend"]["rounds"], 4)
        self.assertEqual(got["searched"]["contacts"], 120)
        self.assertEqual(got["trail"][0]["tool"], "find_person")

    def test_it_arrives_under_the_same_keys_a_live_answer_uses(self):
        """The transcript renders restored turns and just-arrived ones through
        one component. Different key names would mean two code paths that drift,
        which is how the live-only version happened in the first place."""
        db.add_chat_message(self.conn, "assistant", "x", conversation_id=self.cid,
                            receipt_json=json.dumps(self.RECEIPT))
        self.conn.commit()
        got = self._restored("x")
        for key in ("searched", "trail", "spend"):
            self.assertIn(key, got, f"the live answer sends {key!r} at the top level")

    def test_a_turn_older_than_the_column_carries_no_receipt_at_all(self):
        """The counter-test. Absent is right; a row of zeros would be a claim
        about an API call whose numbers were never written down."""
        db.add_chat_message(self.conn, "assistant", "older answer",
                            conversation_id=self.cid)
        self.conn.commit()
        got = self._restored("older answer")
        for key in ("searched", "trail", "spend"):
            self.assertNotIn(key, got)

    def test_a_rule_based_answer_writes_no_receipt(self):
        """Invariant 8's engine touches no API. "$0.00" would be a price for a
        request that never happened, and the Ask view reads a missing spend as
        its proof that the keyword engine answered."""
        db.add_chat_message(self.conn, "assistant", "rules answer",
                            conversation_id=self.cid, receipt_json="")
        self.conn.commit()
        self.assertNotIn("spend", self._restored("rules answer"))

    def test_a_corrupt_receipt_degrades_instead_of_500ing(self):
        """The column is JSON in a text field. A half-written row must cost the
        receipt, never the transcript."""
        db.add_chat_message(self.conn, "assistant", "bad json",
                            conversation_id=self.cid, receipt_json="{not json")
        self.conn.commit()
        got = self._restored("bad json")
        self.assertEqual(got["content"], "bad json")
        self.assertNotIn("spend", got)

    def test_the_column_is_added_idempotently_where_invariant_9_says(self):
        src = code_of(os.path.join(REPO, "crm", "db.py"))
        at = src.index("def ensure_columns")
        self.assertIn("receipt_json", src[at:],
                      "schema changes go in ensure_columns — never a manual step")
        db.init_db(self.path)          # twice, because that is the whole point
        db.init_db(self.path)


class TestHandleChatActuallyBuildsTheReceipt(Base):
    """The write half, which the tests above do not touch — they all put a
    receipt in the row themselves, so none of them can notice the code in
    handle_chat that BUILDS it going missing. That is the reason this class
    exists: a round-trip test that supplies its own payload is testing the
    round trip, not the feature.

    `chat.chat` is patched rather than called, so no request reaches Anthropic
    and no key is needed — the same fixture shape test_api_ask.py already uses.
    """

    ANSWER = {
        "reply": "Three people.",
        "contacts": [],
        "trail": [{"tool": "find_person", "detail": "Ture", "matched": 3, "total": 3}],
        "spend": {"usd": 0.15, "rounds": 4, "model": "example-model",
                  "input_tokens": 12000, "output_tokens": 800,
                  "cache_read_tokens": 9000},
    }

    def _ask(self, answer):
        from unittest import mock
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-not-a-real-key"}), \
             mock.patch.object(app.api_ask.chat, "chat", lambda *a, **k: dict(answer)):
            return app.api_ask.handle_chat(self.conn, {"message": "who is in Paris"})

    def _stored(self, conversation_id):
        row = self.conn.execute(
            "SELECT receipt_json FROM chat_history WHERE role = 'assistant' "
            "AND conversation_id = ? ORDER BY id DESC LIMIT 1",
            (conversation_id,)).fetchone()
        return row["receipt_json"] if row else None

    def test_answering_a_question_writes_the_receipt_to_the_row(self):
        out = self._ask(self.ANSWER)
        stored = json.loads(self._stored(out["conversation"]["id"]))
        self.assertEqual(stored["spend"]["usd"], 0.15)
        self.assertEqual(stored["trail"][0]["tool"], "find_person")
        self.assertIsNotNone(stored["searched"],
                             "the searched counts are computed by handle_chat "
                             "itself, not by chat.chat")

    def test_what_was_written_is_what_comes_back(self):
        """End to end on the two halves together: ask, then reload."""
        out = self._ask(self.ANSWER)
        cid = out["conversation"]["id"]
        restored = next(m for m in api_ask.handle_chat_history_get(self.conn, cid)["messages"]
                        if m["role"] == "assistant")
        self.assertEqual(restored["spend"]["usd"], out["spend"]["usd"])
        self.assertEqual(restored["trail"], out["trail"])

    def test_an_answer_that_cost_nothing_writes_no_receipt(self):
        """The counter-test, and it is invariant 8's engine: no spend, no trail,
        and `searched` alone is not worth a receipt claiming an API call."""
        out = self._ask({"reply": "rules answer", "contacts": [], "trail": [],
                         "spend": None})
        stored = json.loads(self._stored(out["conversation"]["id"]) or "{}")
        self.assertIsNone(stored.get("spend"))
        self.assertFalse(stored.get("trail"))


class TestTheReceiptIsVisibleOnARestoredTurn(unittest.TestCase):
    """The server can persist it perfectly and the screen still show nothing —
    which is exactly what the old guard did."""

    def setUp(self):
        self.js = _read("app", "ask.js")
        at = self.js.index("function Provenance(")
        self.prov = self.js[at:self.js.index("\n}", at)]

    def test_it_no_longer_hides_everything_when_searched_is_absent(self):
        self.assertNotIn("if (!searched) return null;", self.prov,
                         "this is the line that threw away every restored "
                         "receipt — a history turn has no `searched`")
        self.assertIn("if (!searched && !spend && !n) return null;", self.prov)

    def test_a_turn_with_a_price_but_no_tool_calls_still_opens(self):
        self.assertIn("if (n === 0 && !spend)", self.prov,
                      "an answer with a price has tokens, rounds and a model "
                      "worth reading even when the trail is empty")

    def test_the_detail_row_shows_more_precision_than_the_summary(self):
        """The collapsed line rounds to cents so it reads at a glance, which
        turns a sub-cent follow-up into "$0.00" — a number that looks like nothing
        happened rather than like the cheap answer it was."""
        self.assertIn("spend.usd.toFixed(4)", self.prov)
        self.assertIn("spend.usd.toFixed(2)", self.prov)


if __name__ == "__main__":
    unittest.main()
