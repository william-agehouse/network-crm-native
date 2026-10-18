"""NC-080 gate: Ask's prompt-cache markers may not accumulate past Anthropic's limit.

WHAT BROKE, AND WHY IT WENT UNNOTICED

`chat.chat()` runs a tool-use loop. Each round appended the round's tool results
to `convo` and marked the last block with `cache_control` — correct on its own,
and the comment explaining it is right about the money it saves. But `convo` is
re-sent WHOLE on every round, so the markers accumulated:

    round 1   system + 1 results marker = 2
    round 2   system + 2                = 3
    round 3   system + 3                = 4
    round 4   system + 4                = 5   -> 400, no answer at all

    400 A maximum of 4 blocks with cache_control may be provided. Found 5.

Anthropic rejects the entire request, so every question broad enough to need four
searches failed outright. And then invariant 8 did exactly what it promises —
caught the exception, answered from the rule-based engine, and appended "(AI chat
was unavailable: …)" to the reply. The app never crashed. It just quietly stopped
being the feature: a question that needs the AI comes back with the rule-based
engine's canned "Here's who I found in …:" reply.

That is the lesson worth keeping: a good fallback HIDES a total failure of the
thing it is falling back from. The rule-based engine is not the bug and must stay
— but nothing was watching the gap between "the app answered" and "the app
answered the way it is supposed to".

WHY THIS FILE DRIVES THE REAL LOOP

The bug is not in any one line; it is in what a line does when repeated. A unit
test on the marking statement would have passed. So these run `chat.chat()`
against a fake Anthropic that demands tool calls for more rounds than the limit
allows, and inspect the kwargs of every single request it makes — which is the
only place the accumulation is visible.
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, chat_mod as chat  # noqa: E402


def count_markers(blocks):
    """Markers in a flat block list, counted the way the API counts them."""
    if not isinstance(blocks, list):
        return 0
    return sum(1 for b in blocks
               if isinstance(b, dict) and b.get("cache_control") is not None)


def count_request_markers(kwargs):
    """Every marker in ONE request — both halves, because they are built by
    different functions and the API's limit spans them."""
    total = count_markers(kwargs.get("system"))
    for msg in kwargs.get("messages") or []:
        total += count_markers(msg.get("content"))
    return total


def always_tool_calls(rounds, requests):
    """A fake `anthropic` that keeps asking for tool calls, recording every
    request. `rounds` is how many tool-use replies it gives before finishing —
    set it above four and the old code produced a request the API rejects.

    Each request's kwargs are DEEP-COPIED at call time. Without that the recorded
    `messages` would be the same live list the loop keeps mutating, so a later
    round's cleanup would retroactively make an earlier request look innocent —
    the test would pass on the broken code.
    """
    import copy
    state = {"n": 0}

    class _Messages:
        def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            state["n"] += 1
            if state["n"] <= rounds:
                return types.SimpleNamespace(
                    stop_reason="tool_use",
                    content=[types.SimpleNamespace(
                        type="tool_use", id=f"call_{state['n']}",
                        name="search_people", input={"query": "engineer"})],
                    usage=types.SimpleNamespace(
                        input_tokens=100, output_tokens=20,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0))
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text="Here they are.")],
                usage=types.SimpleNamespace(
                    input_tokens=100, output_tokens=20,
                    cache_read_input_tokens=0, cache_creation_input_tokens=0))

    class _Anthropic:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic = _Anthropic
    return module


class TestTheMarkersNeverExceedTheApiLimit(Base):
    """The gate. Drives the loop past the limit and checks every request."""

    def _run(self, rounds):
        # Base's own connection, on Base's own temp schema. `db.get_db()` with no
        # path resolves to the real data dir, which has no `state` table under the
        # test bootstrap — and pointing a test back at the real data dir is never
        # the fix.
        requests = []
        module = always_tool_calls(rounds, requests)
        with mock.patch.dict(sys.modules, {"anthropic": module}), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            chat.chat(self.conn, [{"role": "user", "content": "who to invite?"}])
        return requests

    def test_a_long_tool_loop_stays_within_the_limit(self):
        """Eight rounds — twice the limit. Before the fix, request four carried
        five markers and the real API would have rejected it."""
        requests = self._run(rounds=8)
        self.assertGreater(len(requests), chat.MAX_CACHE_BLOCKS,
                           "the fake did not drive enough rounds to reach the "
                           "limit, so this test proves nothing")
        worst = max(count_request_markers(k) for k in requests)
        counts = [count_request_markers(k) for k in requests]
        self.assertLessEqual(
            worst, chat.MAX_CACHE_BLOCKS,
            f"a request carried {worst} cache_control markers, over Anthropic's "
            f"limit of {chat.MAX_CACHE_BLOCKS}. The API rejects the WHOLE request "
            f"with a 400 and Ask silently answers from the rule-based engine "
            f"instead. Per-request counts: {counts}")

    def test_the_newest_marker_is_the_one_kept(self):
        """Caching is prefix-based, so the marker at the END of the conversation
        is the valuable one. If trimming ever kept the oldest instead, the limit
        would still be respected and the caching would stop working — a silent
        cost regression with no failing test, which is how this got here."""
        requests = self._run(rounds=6)
        last = requests[-1]
        msgs = last.get("messages") or []
        marked = [i for i, m in enumerate(msgs) if count_markers(m.get("content"))]
        self.assertTrue(
            marked, "the final request has no conversation marker at all, so "
                    "every round re-pays for the whole transcript at full price")
        self.assertGreaterEqual(
            marked[-1], len(msgs) - 3,
            f"the surviving marker sits at message {marked[-1]} of {len(msgs)} — "
            f"near the START of the conversation, so it caches almost nothing")

    def test_the_system_prompt_keeps_its_own_marker(self):
        """The conversation cleanup must not reach into the system blocks. That
        marker is the stable prefix and the whole reason build_system_blocks puts
        the owner block inside it."""
        requests = self._run(rounds=6)
        for i, kwargs in enumerate(requests):
            with self.subTest(request=i):
                self.assertEqual(
                    count_markers(kwargs.get("system")), 1,
                    "the system prompt lost (or gained) its cache marker; it "
                    "should carry exactly one, on the last stable block")


class TestTheCleanupHelpersDoWhatTheySay(unittest.TestCase):
    """Small, because the loop test above is the one that matters. These exist so
    a failure points at the helper rather than at the whole loop."""

    def test_clear_removes_every_marker_and_reports_how_many(self):
        msgs = [
            {"role": "user", "content": [{"type": "tool_result",
                                          "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": [{"type": "tool_result"},
                                         {"type": "tool_result",
                                          "cache_control": {"type": "ephemeral"}}]},
        ]
        self.assertEqual(chat._clear_cache_markers(msgs), 2)
        self.assertEqual(chat._count_cache_markers_in(msgs), 0)

    def test_clear_survives_a_plain_string_content(self):
        """History rows come back as plain strings, not block lists — the shape
        this walks is not guaranteed."""
        msgs = [{"role": "user", "content": "just text"}]
        self.assertEqual(chat._clear_cache_markers(msgs), 0)

    def test_trim_drops_the_oldest_first(self):
        mark = lambda: {"type": "tool_result", "cache_control": {"type": "ephemeral"}}
        msgs = [{"role": "user", "content": [mark()]} for _ in range(4)]
        self.assertEqual(chat._trim_oldest_cache_markers(msgs, 2), 2)
        surviving = [i for i, m in enumerate(msgs) if count_markers(m["content"])]
        self.assertEqual(surviving, [2, 3],
                         "trimming took the newest markers, which keeps the count "
                         "legal and throws away the caching")

    def test_the_limit_matches_what_the_api_actually_allows(self):
        """Four is Anthropic's limit on cache_control blocks per request:
             "A maximum of 4 blocks with cache_control may be provided. Found 5."
        Named as a constant so the guard and the tests cannot disagree about it."""
        self.assertEqual(chat.MAX_CACHE_BLOCKS, 4)


if __name__ == "__main__":
    unittest.main()
