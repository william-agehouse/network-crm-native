#!/usr/bin/env python3
"""
LLM chat brain for the Ask view — grounds free-text questions ("who are good
people in London?") in the real CRM via Anthropic tool-use, instead of the
rule-based keyword/metro matching in app.py's handle_ask.

Needs ANTHROPIC_API_KEY. app.py falls back to handle_ask when it's unset, so
the app works with zero API keys — this module is only imported/used when a
key is present (see app.py's handle_chat).
"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

import archive as arc
import db
import figures  # NC-087: the one spelling of a contact-scope filter
import owner
import search_cli

# Defaults to Opus 5.5, the current Opus: more capable than Sonnet, and cheaper to
# run than the Opus 5 it replaced as the default (NC-327). Change it with
# ANTHROPIC_MODEL in secrets/.env (e.g. claude-sonnet-5-5 for a lower bill,
# claude-haiku-5-5 for the cheapest and fastest). The prices below are the ones
# for this default; if you switch model, set NC_PRICE_IN_PER_MTOK and
# NC_PRICE_OUT_PER_MTOK to match, or the cost shown under an answer is wrong.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
# A roster question ("give me 50 people to invite") needs both more
# searching and a much longer answer than a "who do I know in London?" one. The
# old ceilings — 5 rounds, 2048 output tokens — cut a long list off in the
# middle of a sentence and returned it as though it were finished. All three are
# env-overridable because the right value depends on the model and the question,
# and a silent constant is what caused this.
#
# All four are read at CALL time, not at import. They were module-level
# `int(os.environ.get(...))` constants, and all four were in
# settings.MANAGED_KEYS (until NC-003; now settings.UNMANAGED_KEYS) — so the Settings view rendered them, wrote them into
# os.environ live, and changed nothing at all until the app was restarted. That is
# a setting that lies about having an effect, which is worse than no setting: the
# comment above already said "a silent constant is what caused this" while being
# one. api_ask.chat_max_cards() is the idiom copied here, clamps included.
MAX_TOOL_ROUNDS_DEFAULT = 12
MAX_TOKENS_DEFAULT = 8192
# Per-call result caps for the search tools. 30/20 meant a request for 50 people
# could not be satisfied from tool output at all, so the model padded the list
# from its own world knowledge — names the user has no card for and cannot act on.
SEARCH_PEOPLE_LIMIT_DEFAULT = 120
SEMANTIC_LIMIT_DEFAULT = 60
# NC-081, the message tools. Individual messages are much smaller than a contact
# card, so these can be larger: 200 message rows at a ~200-character snippet is
# roughly 12k tokens, which one round can carry. The people-per-question cap is
# lower because a grouped answer with 300 names in it is not an answer.
MESSAGE_LIMIT_DEFAULT = 200
MESSAGE_PEOPLE_LIMIT_DEFAULT = 80


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """One env integer, read now and clamped. A bad value falls back to the
    default rather than raising: a typo in Settings must not break Ask."""
    try:
        val = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def max_tool_rounds() -> int:
    return _env_int("NC_CHAT_TOOL_ROUNDS", MAX_TOOL_ROUNDS_DEFAULT, 1, 40)


def max_tokens() -> int:
    return _env_int("NC_CHAT_MAX_TOKENS", MAX_TOKENS_DEFAULT, 256, 64000)


def search_people_limit() -> int:
    return _env_int("NC_CHAT_SEARCH_LIMIT", SEARCH_PEOPLE_LIMIT_DEFAULT, 1, 1000)


def semantic_limit() -> int:
    return _env_int("NC_CHAT_SEMANTIC_LIMIT", SEMANTIC_LIMIT_DEFAULT, 1, 1000)


def message_limit() -> int:
    return _env_int("NC_CHAT_MESSAGE_LIMIT", MESSAGE_LIMIT_DEFAULT, 1, 2000)


def message_people_limit() -> int:
    return _env_int("NC_CHAT_MESSAGE_PEOPLE_LIMIT", MESSAGE_PEOPLE_LIMIT_DEFAULT, 1, 1000)


# ── Answer-shape controls ─────────────────────────────────────────────────────
# The owner gets control over how their network is queried: a long list or a
# short list. Three controls, and one rule that decides where each of
# them is allowed to act:
#
#   THE SHAPE CONTROLS THE PROSE AND THE RENDERING. IT NEVER CHANGES WHICH PEOPLE
#   COME BACK.
#
# A "short list" that quietly returned fewer people would repeat a bug this repo
# has already fixed elsewhere — the People table reading "First 500 of N" beside
# "+ Add these N matches", and Ask returning a capped page of the matches while
# naming no total. So each control is pinned to exactly one layer:
#
#   people   → PROMPT ONLY. How many people the answer TEXT names. Every person a
#              search found still reaches the screen as a card:
#              api_ask.handle_chat keeps the name filter open whenever this is set,
#              precisely so a 10-name answer about 47 found people does not ship 10
#              cards. No tool cap changes, no result is discarded.
#   detail   → PROMPT + this request's max_tokens. Brief spends a short output
#              budget on people instead of prose, so it makes the existing
#              `truncated` flag MORE likely — which is fine, because that flag is
#              already reported and already rendered.
#   search   → THE TOOL CAPS AND THE LOOP DEPTH, through the call-time readers
#              above. Quick genuinely looks at less of the network, so the prompt
#              has to say so, the round budget is named in the out-of-rounds
#              reply, and every tool result still reports total_matched.
#
# The vocabulary is three fixed menus rather than free numbers on purpose: these
# are labels on buttons that the user reads before choosing, not silent limits.
# The tunables behind them are the NC_CHAT_* keys, each read at call time below.
SHAPE_PEOPLE_COUNTS: Dict[str, int] = {"10": 10, "25": 25, "all": 0}  # 0 = no count
SHAPE_DETAILS = ("brief", "full")
SHAPE_SEARCHES = ("quick", "thorough")
# The default IS today's behaviour: no count instruction, the configured token
# ceiling, the configured caps. A request that carries no shape therefore builds
# the byte-identical API call this module made before the controls existed.
SHAPE_DEFAULT: Dict[str, str] = {"people": "all", "detail": "full", "search": "thorough"}
SHAPE_OPTIONS: Dict[str, List[str]] = {
    "people": list(SHAPE_PEOPLE_COUNTS),
    "detail": list(SHAPE_DETAILS),
    "search": list(SHAPE_SEARCHES),
}

# Brief's own output ceiling and Quick's own three ceilings. Same contract as the
# four readers above: read at call time, clamped, and a bad value falls back to a
# named default instead of raising. shape_caps() min()s every one of them against
# the corresponding full/thorough number, so "brief" can never be longer than
# "full" and "quick" can never search harder than "thorough", however the two are
# configured against each other.
BRIEF_MAX_TOKENS_DEFAULT = 1024
QUICK_TOOL_ROUNDS_DEFAULT = 3
QUICK_SEARCH_LIMIT_DEFAULT = 30
QUICK_SEMANTIC_LIMIT_DEFAULT = 20
QUICK_MESSAGE_LIMIT_DEFAULT = 60
QUICK_MESSAGE_PEOPLE_LIMIT_DEFAULT = 30


def brief_max_tokens() -> int:
    return _env_int("NC_CHAT_BRIEF_MAX_TOKENS", BRIEF_MAX_TOKENS_DEFAULT, 256, 64000)


def quick_tool_rounds() -> int:
    return _env_int("NC_CHAT_QUICK_TOOL_ROUNDS", QUICK_TOOL_ROUNDS_DEFAULT, 1, 40)


def quick_search_limit() -> int:
    return _env_int("NC_CHAT_QUICK_SEARCH_LIMIT", QUICK_SEARCH_LIMIT_DEFAULT, 1, 1000)


def quick_semantic_limit() -> int:
    return _env_int("NC_CHAT_QUICK_SEMANTIC_LIMIT", QUICK_SEMANTIC_LIMIT_DEFAULT, 1, 1000)


def quick_message_limit() -> int:
    return _env_int("NC_CHAT_QUICK_MESSAGE_LIMIT", QUICK_MESSAGE_LIMIT_DEFAULT, 1, 2000)


def quick_message_people_limit() -> int:
    return _env_int("NC_CHAT_QUICK_MESSAGE_PEOPLE_LIMIT",
                    QUICK_MESSAGE_PEOPLE_LIMIT_DEFAULT, 1, 1000)


def normalize_shape(raw: Any) -> Dict[str, str]:
    """Any client input → one of the nine valid shapes. Total: this never raises.

    An unknown value, a wrong type, a missing key and None all fall back to that
    field's default. This is the SERVER-side clamp — the menu of allowed values
    lives here, so a client cannot invent {"people": "500"} and be served it, and
    it cannot earn a 400 by sending a stale "medium" either. A shape is a
    preference; refusing to answer the question because a dropdown sent the wrong
    word would be the worse failure. What actually applied is echoed back in the
    response instead (api_ask.shape_status), so the client never has to assume its
    request was honoured.

    Idempotent, so every entry point — parse_shape, chat, shape_caps,
    shape_directive — can call it and none of them has to trust its caller.
    """
    out = dict(SHAPE_DEFAULT)
    if not isinstance(raw, dict):
        return out
    for field, allowed in (("people", SHAPE_PEOPLE_COUNTS),
                           ("detail", SHAPE_DETAILS),
                           ("search", SHAPE_SEARCHES)):
        val = raw.get(field)
        if val is None:
            continue
        key = str(val).strip().lower()   # accepts 10 and "10" alike
        if key in allowed:
            out[field] = key
    return out


def people_count(shape: Any = None) -> int:
    """How many people the answer TEXT may name; 0 for "all" (no instruction).

    Never a limit on who comes back — see the rule at the top of this section.
    api_ask.rules_people_cap is the single caller that turns this into a real
    result cap, and only on the zero-key engine, where there is no prose to shape.
    """
    return SHAPE_PEOPLE_COUNTS.get(normalize_shape(shape)["people"], 0)


def shape_caps(shape: Any = None) -> Dict[str, int]:
    """The four numbers this one request runs with.

    Every number is read NOW from the environment (the
    api_import.import_timeout_sec idiom), then narrowed by the shape. Called once
    per chat(), so one answer runs on one consistent set of numbers while the next
    question re-reads them — a Settings change applies with no restart, which is
    the whole point of the call-time rule.
    """
    sh = normalize_shape(shape)
    rounds, tokens = max_tool_rounds(), max_tokens()
    people, semantic = search_people_limit(), semantic_limit()
    msgs, msg_people = message_limit(), message_people_limit()
    if sh["detail"] == "brief":
        tokens = min(brief_max_tokens(), tokens)
    if sh["search"] == "quick":
        # min(), not assignment: quick is a floor-ward narrowing of whatever
        # thorough is configured to be, never an independent number that could
        # end up larger.
        rounds = min(quick_tool_rounds(), rounds)
        people = min(quick_search_limit(), people)
        semantic = min(quick_semantic_limit(), semantic)
        msgs = min(quick_message_limit(), msgs)
        msg_people = min(quick_message_people_limit(), msg_people)
    return {"tool_rounds": rounds, "max_tokens": tokens,
            "search_people_limit": people, "semantic_limit": semantic,
            "message_limit": msgs, "message_people_limit": msg_people}


# The three directives. Written as instructions, not as a description of a mode,
# and placed LAST in the system parameter so they win on recency over the general
# guidance in SYSTEM_PROMPT above.
_PEOPLE_DIRECTIVE = (
    "People: name at most {n} people in your answer. If more of the user's "
    "network matches, name the {n} best and state the total you found — \"I found "
    "47 people; these {n} are the closest\". Never pad the list to {n} with anyone "
    "a tool did not return. Do not apologise for the people you leave unnamed: the "
    "user's screen still lists the people your searches found, and it reports any "
    "it cannot fit. A short answer loses nobody."
)
_BRIEF_DIRECTIVE = (
    "Detail: brief. One line per person — the name, then the single strongest "
    "reason. No preamble, no closing summary, no headings. Your output budget is "
    "deliberately short for this answer, so spend it on people, not on prose."
)
_QUICK_DIRECTIVE = (
    "Search: quick. You have fewer search rounds and smaller result caps for this "
    "answer, so you see only part of the user's network. Aim one or two searches "
    "well instead of running many. Never call the result complete or exhaustive. "
    "End with one short line that says a thorough search can find more people."
)


def _utc_today() -> str:
    """Today in UTC as YYYY-MM-DD. Its own function so the tests can freeze it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def shape_directive(shape: Any = None) -> str:
    """The shape instructions as prompt text, or "" for the default shape.

    Empty is load-bearing: _system_blocks appends no second block at all when
    there is nothing to say, so the default request is unchanged from before the
    controls existed.
    """
    sh = normalize_shape(shape)
    parts: List[str] = []
    n = SHAPE_PEOPLE_COUNTS.get(sh["people"], 0)
    if n:
        parts.append(_PEOPLE_DIRECTIVE.format(n=n))
    if sh["detail"] == "brief":
        parts.append(_BRIEF_DIRECTIVE)
    if sh["search"] == "quick":
        parts.append(_QUICK_DIRECTIVE)
    if not parts:
        return ""
    return ("ANSWER SHAPE — the user set these controls for this question. They "
            "change the shape of your answer. They never change which people you "
            "are allowed to name.\n\n" + "\n\n".join(parts))


def _system_blocks(directive: str, owner_block: str = "") -> List[Dict[str, Any]]:
    """The `system` parameter as blocks, in an order that is load-bearing for cost.

    ORDERING, and it must not be reversed. An Anthropic cache breakpoint caches
    the prefix up to and INCLUDING the block it sits on, so the rule here is:
    **the marker goes on the last block that is stable across questions.**

      block 0   SYSTEM_PROMPT — long and identical on every question
      block 1   the owner block (NC-010), when there is one   <- marker lands here
      block 2   today's date, plus the shape directive        <- never cached

    The owner block is the user's own name and goals (crm/owner.py). It varies
    only when they edit it, which is roughly never, so it belongs INSIDE the
    cached prefix — an edit writes one new cache entry and every question after it
    hits. Leaving it outside would re-send it on every single question forever to
    save a cache write that happens once.

    The last block genuinely varies — the shape directive per question, the date
    per day — so it stays UNMARKED, outside the cached prefix, where varying it is
    nearly free. Putting the shape text inside the cached prefix instead would mean every
    switch between Brief and Full writes a new ~950-token cache entry and re-pays
    the whole prefix at 1.25x on the next answer. That is the money in this
    decision, and it is invisible unless you know where the marker is — hence this
    comment.
    """
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": SYSTEM_PROMPT}]
    if owner_block:
        blocks.append({"type": "text", "text": owner_block})
    # On the LAST stable block, whichever that turned out to be.
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    # NC-081. Today's date, and it has to be here rather than in SYSTEM_PROMPT.
    #
    # The model was never told what day it is, so every date-relative question
    # was unanswerable no matter what the tools could do: "who did I text in the
    # last month" has no meaning without an anchor, and the model cannot look at a
    # clock. It either guessed from its training cutoff or ignored the timeframe.
    #
    # It goes in the LAST, UNMARKED block for the reason the shape directive does:
    # anything above the marker is the cached prefix, and a value that changes
    # daily would write a fresh ~950-token cache entry every midnight and re-pay
    # the prefix at 1.25x on the first question after it. Below the marker it is
    # nearly free. Same trade-off, same place, one line of consequence.
    tail = [t for t in (f"Today's date is {_utc_today()} (UTC). Resolve any relative "
                        f"timeframe in the question — \"last month\", \"this week\", "
                        f"\"since June\" — against that, and pass explicit "
                        f"YYYY-MM-DD dates to the tools that accept them.",
                        directive) if t]
    blocks.append({"type": "text", "text": "\n\n".join(tail)})
    return blocks


# How many cache_control markers Anthropic accepts in ONE request. Exceeding it
# is not a warning or a dropped marker — the whole request is rejected with a 400
# and no answer comes back at all.
MAX_CACHE_BLOCKS = 4


def _count_cache_markers(blocks: Any) -> int:
    """Markers in a flat block list — the `system=` argument's shape."""
    if not isinstance(blocks, list):
        return 0
    return sum(1 for b in blocks
               if isinstance(b, dict) and b.get("cache_control") is not None)


def _count_cache_markers_in(messages: List[Dict[str, Any]]) -> int:
    """Markers across a message list — the `messages=` argument's shape."""
    return sum(_count_cache_markers(m.get("content")) for m in messages)


def _trim_oldest_cache_markers(messages: List[Dict[str, Any]], n: int) -> int:
    """Drop the `n` oldest markers. Oldest, because the newest caches the most."""
    dropped = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if dropped >= n:
                return dropped
            if isinstance(block, dict) and block.pop("cache_control", None) is not None:
                dropped += 1
    return dropped


def _clear_cache_markers(messages: List[Dict[str, Any]]) -> int:
    """Strip `cache_control` from every content block in `messages`.

    NC-080. The tool loop marked ONE block per round, on a conversation that is
    re-sent whole every round. So the markers accumulated: round 1 sent two
    (system + results), round 4 sent five, and Anthropic rejected the request —

        400 A maximum of 4 blocks with cache_control may be provided. Found 5.

    Ask therefore failed on every question broad enough to need four searches,
    fell back to the rule-based engine, and reported it inside the reply text.
    Invariant 8's fallback did its job perfectly, which is precisely why this
    survived: the feature never crashed, it just quietly stopped being the
    feature.

    Only the NEWEST marker is wanted. Caching is prefix-based, so one marker at
    the end of the conversation already caches everything before it; the older
    markers bought nothing and cost the request. The system prompt keeps its own
    marker (build_system_blocks), and it is not in `messages`, so a cleared
    conversation plus the system prefix is two — comfortably under the four.

    Returns how many it removed, so the caller can assert rather than hope.
    """
    removed = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.pop("cache_control", None) is not None:
                removed += 1
    return removed


SYSTEM_PROMPT = """You are the Ask assistant inside a private, local Network CRM. \
You help the user find people in their own network and explain why each match fits.

You have tools to search the user's contacts. Always use a tool before answering \
a question about who the user knows — never invent people.

WHEN THE QUESTION NAMES SOMEONE, START WITH find_person. It is the only tool \
that searches contact NAMES, and it takes a partial name. Do not reach for \
semantic_search to find a named person: semantic_search reads profile text, so \
"my longtime friend Ture" matches nothing at all — no one's profile says \
"longtime friend". And do not put a person's name into people_in_messages or \
search_messages as the query: that searches message TEXT and finds every \
message MENTIONING somebody of that name, which is a different and much larger \
set than the person's own messages.

A SNIPPET IS A WINDOW, NOT THE MESSAGE. Search results show about 220 \
characters around the match. When a result carries an `id` and `full_chars`, \
that means it was cut and there is more — call read_message with that id before \
concluding anything about what the message says or does not say. For email this \
is the normal case rather than the exception: a typical email is a couple of \
thousand characters and most are longer than the window, so answering a \
question about a mail from its snippet alone is answering from the first \
paragraph. A result with no `id` was returned whole; do not call read_message \
on it.

The relationship words in a question are not decoration, and they are not \
vague — they are the filter that says WHICH person is meant. "Longtime", \
"oldest", "close", "best friend", "barely know them" are all claims about \
numbers find_person already returns: years_known, known_since, \
messages_one_to_one (their personal messages, group chats excluded), \
messages_total and strength. Read them off the rows. If three people are called \
Ture and one has years of history and thousands of one-to-one messages, that is \
the friend — resolve it from the data and get on with the question, rather than \
asking which Ture or searching again.

Then pass that person's id as contact_id to the other tools, so you are \
searching their messages and not the whole archive.

search_people's location/role_type only match exact metro names and a fixed list \
of broad role types — it will miss anything nuanced (an industry, a country, a \
combination like "retail investor", a description that isn't a literal tag). \
For anything other than a plain "people in <exact metro>" or "people who are \
<exact role_type>" query, call semantic_search first — it matches on meaning, \
not just tags. If search_people comes back empty or thin, always retry with \
semantic_search using the user's own phrasing before telling them nobody matches \
— an empty structured search means the filters didn't hit, not that no one fits.

A QUESTION WITH SEVERAL FACETS IS STILL ONE semantic_search. "Who works with \
climate, batteries or energy?" is three phrasings of one question, not three \
questions: pass them together as queries: ["climate research and clean \
energy", "battery founder or engineer", "utilities, grid, energy storage"] \
and you get one merged ranking, each person once, at their best-matching \
phrasing. Three separate calls return the same people three times over and cost \
three rounds — and a round is the most expensive thing you can do here, because \
every round re-sends the whole conversation. Be generous with the list.

search_conversations digs through the user's ARCHIVED MESSAGE TEXT (LinkedIn \
DMs, WhatsApp, iMessage, email). Use it whenever the question is about what was \
SAID or DISCUSSED rather than who someone is: "who was I talking to about \
hiring?", "who mentioned Berlin?". mode='keyword' finds exact words; \
mode='semantic' (the default) matches meaning. For "what did X and I talk \
about?" find X's contact_id first, then call search_conversations with that \
contact_id — never conclude there's no history from the contact card alone; \
notes and archived conversations are separate stores.

THE MESSAGE TOOLS, and when to use them instead of search_conversations. \
search_conversations reads conversations glued into blocks, which is the right \
shape for "what did X and I talk about" and the wrong one for anything with a \
date or a direction in it: a block mixes both sides of a conversation and its \
timestamp is the block's, not any message's. So the moment a question contains a \
TIMEFRAME ("last month", "since June", "this week", "recently") or a DIRECTION \
("did I send", "who have I told", "what did they reply"), use the message tools \
instead. They read one row per message, with the real send date and who sent it.

The route is always the same three steps, and skipping the first is how these \
questions get answered wrong:

1. message_stats — no arguments. It tells you the archive exists, which channels \
are in it, and the first and last date it covers. Call it before searching \
message text for the first time in a conversation. If the question's timeframe \
falls outside those dates, say so instead of reporting an empty search as though \
it were an answer.
2. people_in_messages when the answer is a LIST OF PEOPLE — "who have I \
mentioned acme.io to", "who did I text last month", "who have I sent the link \
to". It groups in the database, so its total_people is the real count even when \
the list you get back is capped. Quote total_people.
3. search_messages when the answer is WHAT WAS SAID, or to read one person's side \
after step 2 (same filter, plus their contact_id).

READ THE MESSAGES AROUND EACH MATCH. search_messages returns `before` and \
`after` on every hit — the messages either side of it in the same conversation, \
oldest first. They are there because a literal search can only find the words \
you gave it, and the answer to a question is usually the NEXT message, which \
will not contain those words. Ask for a confirmation number and the matching \
message is the question "what's the confirmation number?"; the answer is a bare \
number sent a minute later, which no keyword could ever reach. A short number, \
address, time, name or yes/no immediately after a question IS the answer to \
that question — \
say so plainly and quote both sides with their dates. Never say a search found \
no answer until you have read the neighbours; reporting "the matching messages \
contain no number" while the number sits in the next row is the specific \
failure this exists to prevent. It also tells you when something CHANGED: the \
same question asked in different years with different numbers after it means \
the value changed, so give both with their dates and say which is current.

ONE SEARCH IS ONE SPELLING, SO PUT EVERY SPELLING IN ONE SEARCH. query is \
matched literally, so it finds only the exact word you passed and nothing near \
it: "confirmation" cannot reach a message that says "booking", and neither \
reaches a bare "48213". The answer is NOT to search again and again — `queries` \
takes a LIST and matches any of them, so send them together: \
queries: ["confirmation", "booking", "reference"] is one round and covers all \
three. \
Be generous with that list. An extra phrase costs almost nothing; an extra \
ROUND is the single most expensive thing you can do, because every round \
re-sends the whole conversation. One call with eight phrases is far cheaper \
than two calls with one each.

DO NOT FILTER BY DIRECTION WHEN YOU ARE LOOKING FOR AN ANSWER. This undoes \
the neighbours and it is easy to get wrong. "What number did HE give me" sounds \
like direction='received', but the message that contains the word "confirmation" \
is usually the one YOU sent — the question — and his answer is the bare number \
next to it, which matches no search term at all. Filter it out and the search \
finds his older chattier messages and misses the actual answer. Leave direction \
off whenever the thing you want is a REPLY; use it only when the question is \
literally about who sent what ("did I ever send them the link").

So: plan the spellings before you search, not after. For a value that might \
have changed, include the value itself once you know it — searching "48213" \
shows every time it was given, which is how you tell a current answer from an \
old one and spot the day it changed. If a first search genuinely missed \
something, widen the LIST rather than adding another round.

Both take since and until as YYYY-MM-DD, both inclusive. Work the dates out \
yourself from today's date, which is given to you below — never pass a phrase \
like "last month" as a date; the tool will reject it and you will have spent a \
round. Both take direction='sent' or 'received'. query is matched LITERALLY as a \
substring, so "acme.io", a URL or a phone number all work exactly as typed, \
and there are no operators — a shorter phrase matches more, "acme.io" beats \
"the acme.io dinner". Group-chat messages are left out unless you ask for \
them, and the number left out is reported to you; mention it when it changes the \
answer.

Two things these tools report that you must pass on rather than round off. A \
total that is larger than the list you were given: say the total. And, when you \
gave people_in_messages a date range, each person's first_ever and \
new_in_window — first_ever is their earliest matching message in the WHOLE \
archive, so new_in_window already tells you who was hearing about the topic for \
the first time. Use it. Do not run a separate before-the-window search per \
person to work that out, and never read it off `first`, which is only the \
earliest match inside the window and is inside the window for everyone.

mutual_connections surfaces the mutual connections and shared history LinkedIn \
showed on a person's profile — use it for intro and "who do we both know?" \
questions (resolve the contact_id first, then call it). This data is LLM-parsed \
from LinkedIn profile TEXT, not a real connection graph, so ALWAYS present it as \
an unverified hint ("LinkedIn suggests you both know Ada", "you may share a \
connection named …"), never as a confirmed relationship — and never claim that \
anyone will actually make the introduction.

EVERY person you name must be someone a tool returned, with the id it gave you. \
The user's screen puts a clickable card under your answer for each person you \
name, matched back to these tool results — so a name you supplied from your own \
knowledge of the world, rather than from their network, arrives as an unclickable \
dead end they cannot open, message or add to a list. If they ask for 50 people \
and their network yields 31, give the 31 and say it is 31. Never pad a list to \
the number asked for. "I found 31, here they are" is a good answer; a list of 50 \
where 19 are strangers is a broken one.

When they ask for a ROSTER — many people for an event, a shortlist, "give me 50" \
— work for it before answering: run several searches with different phrasings and \
angles (role, topic, city, what was discussed), because one search sees one slice. \
search_people reports total_matched, so you can tell when its cap hid people from \
you and narrow the filters to reach the rest. Group the result under short \
headings and keep one line per person; a long list is exactly what was asked for, \
so the "be concise" guidance below does not apply to these.

ASK BACK ONLY WHEN SEARCHING CANNOT SETTLE IT. ask_the_user ends your turn \
with a question and two to four buttons instead of an answer. It is for the \
case where the question has two or more readings that lead to DIFFERENT PEOPLE \
and nothing in the user's data says which was meant — "who could I introduce \
this bakery to?" means investors, or customers, or other bakeries, and those \
three lists barely overlap. It is NOT for anything a tool can resolve: which \
Ture, which city, how many people, what counts as close. Those you work out \
from the numbers the tools already gave you. Default to answering the most \
likely reading and saying in one line which reading you took — a caveat costs \
the user nothing and a question costs them two extra turns. If you asked last \
turn you may not ask again; answer with what you have.

When you answer:
- Be concise. A short paragraph or a few bullet lines, not a wall of text — \
unless a roster was asked for, per above.
- Name the specific people you found and give one clear reason per person \
(location, role, how you know them, how close the relationship is).
- When the evidence is a conversation hit, quote or paraphrase the snippet and \
say when/where it was said (channel + date).
- If they ask a follow-up ("only founders", "who do I know best there?"), use the \
conversation history for context and call a tool again with the narrowed criteria.
"""

TOOLS = [
    {
        "name": "read_message",
        "description": (
            "Read ONE message in full, by id. Every search here returns a "
            "window — about 220 characters around the match — so a long message "
            "arrives cut. When that happens the result carries an `id` and "
            "`full_chars`; pass that id here to read the whole thing.\n\n"
            "USE IT WHENEVER THE ANSWER MIGHT BE IN THE PART YOU CANNOT SEE. An "
            "email typically runs to a couple of thousand characters, most longer "
            "than the window, so for mail this is normally required rather than "
            "optional: the snippet shows you the message exists, not what it "
            "says. For iMessage it is rarely needed — most messages are a line "
            "or two and almost none exceed the window.\n\n"
            "Do not call it on results with no `id`: those were returned whole "
            "and there is nothing more to read. If the reply carries "
            "`continues_at_offset`, call again with that offset to read on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer",
                       "description": "The message id from a cut search result."},
                "offset": {"type": "integer",
                           "description": ("Start this many characters in. Use the "
                                           "continues_at_offset from a previous "
                                           "reply; omit for the beginning.")},
            },
            "required": ["id"],
        },
    },
    {
        "name": "find_person",
        "description": (
            "Look a person up BY NAME. Call this FIRST whenever the question "
            "names someone — 'my friend Ture', 'what did Anna say', 'Erik's "
            "address'. It is the only tool that searches contact NAMES.\n\n"
            "Do not use semantic_search for this: that reads profile text, so a "
            "name plus a relationship word ('my longtime friend Ture') matches "
            "nothing, because nobody's profile says 'longtime friend'. Do not "
            "use people_in_messages either: giving it a name searches message "
            "TEXT, which finds every message that MENTIONS someone of that name "
            "rather than the person themselves.\n\n"
            "Partial names work — 'Ture' finds Ture Exempelsson. Results are "
            "ranked by how certain the name match is, then by how much history "
            "there is, so the first row is normally the person meant.\n\n"
            "RELATIONSHIP WORDS ARE ANSWERED HERE, from data, not guessed. Every "
            "row carries years_known and known_since (how long they have been in "
            "the archive), messages_one_to_one (how much they actually talk, "
            "excluding group chats), messages_total (including group chats), "
            "last_message and strength. 'Longtime', 'oldest', 'close' and 'best' "
            "friend are all statements about those numbers — read them off the "
            "rows rather than searching for the phrase. When two people share a "
            "name, the one with years of dense one-to-one history is the one "
            "meant by 'my friend'.\n\n"
            "Once you have the id, pass it as contact_id to search_messages to "
            "read only that person's messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The person's name, or any part of it"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_people",
        "description": (
            "Search contacts by structured filters: location, role type, a topic "
            "keyword, and/or a minimum relationship-strength score. Use this for "
            "queries with a clear filter, e.g. 'founders in Berlin' or 'people "
            "I'm closest to in London'. Leave a field out to not filter on it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City or metro area, e.g. 'Berlin', 'London'"},
                "role_type": {
                    "type": "string",
                    "description": "One of: founder, investor, tech, product, design, research, "
                                   "marketing, sales, finance, medical, legal, consultant, creative",
                },
                "topic": {"type": "string", "description": "A topic keyword to match against tagged topics"},
                "min_strength": {"type": "integer", "description": "Minimum relationship-strength score, 0-100"},
            },
        },
    },
    {
        "name": "semantic_search",
        "description": (
            "Find people by a natural-language description of who they are or what "
            "they do, even if those exact words never appear in their profile "
            "(e.g. 'a retail investor based in Spain'). Uses a local embedding "
            "model over each contact's profile text — the best default for vague "
            "or descriptive questions.\n\n"
            "PUT EVERY PHRASING IN ONE CALL. `queries` takes a LIST of "
            "descriptions and ranks everyone against all of them at once, so "
            "instead of one search per facet — 'climate research', then "
            "'battery founder', then 'energy storage' — pass queries: "
            "[\"climate research and clean energy\", \"battery founder or "
            "engineer\", \"utilities, grid, energy storage\"] and get one "
            "merged ranking. Each person appears once, at their best-matching "
            "phrasing, with `matched` saying which one found them. A second call "
            "costs far more than a longer list, so be generous with phrasings "
            "and call this once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "One description of who you are looking for"},
                "queries": {"type": "array", "items": {"type": "string"},
                            "description": "SEVERAL descriptions, ranked together "
                                           "in ONE search and merged into one list. "
                                           "Use this instead of one call per facet."},
            },
        },
    },
    {
        "name": "get_contact",
        "description": "Get full detail for one contact by id: notes, how you met, and relationship strength.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
    {
        "name": "search_conversations",
        "description": (
            "Search the user's archived message text — what was actually said in "
            "LinkedIn DMs, WhatsApp, iMessage and email. Use for questions about "
            "conversations, promises, plans or topics discussed, not about who a "
            "person is. For any question with a DATE or a DIRECTION in it "
            "('last month', 'did I send'), use search_messages or "
            "people_in_messages instead — this tool searches conversation "
            "blocks, which have no direction and no per-message date. "
            "mode='semantic' (default) matches meaning; "
            "mode='keyword' finds literal words/names. To read the history with "
            "ONE person ('what did X and I talk about?'), pass their contact_id "
            "(from search_people/semantic_search) — with a contact_id and no "
            "query you get their most recent conversations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["semantic", "keyword"]},
                "contact_id": {"type": "integer",
                               "description": "Restrict to one contact's conversations"},
            },
        },
    },
    # ── NC-081: the message tools ─────────────────────────────────────────────
    # Descriptions are longer than the ones above on purpose. The model cannot
    # inspect the database; everything it knows about what is searchable, over
    # what dates, and in what shape has to be in these strings. A one-line
    # description is why "who did I text about Acme last month" used to come
    # back as a keyword search over contact profiles.
    {
        "name": "message_stats",
        "description": (
            "Call this FIRST for any question about texts, messages, DMs, "
            "emails, or what was said to whom — before searching. Takes no "
            "arguments. Tells you whether a message archive exists at all, how "
            "many messages it holds, which channels are in it (iMessage, "
            "WhatsApp, LinkedIn, email), how many are sent vs received, how many "
            "people they cover, the first and last date it covers, and today's "
            "date. Use the dates to decide whether the question's timeframe has "
            "any data behind it: a question about a month the archive does not "
            "reach has no answer, and saying so is the correct answer. "
            "total_messages is what search_messages and people_in_messages can "
            "reach with their default arguments, INCLUDING group threads. "
            "in_group_threads says how many of that total came from group chats "
            "— a subset of it, never something to add on."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_messages",
        "description": (
            "Search individual messages by literal phrase and/or date range, "
            "with direction. This is the message-level tool — use it, not "
            "search_conversations, whenever the question has a DATE or a "
            "DIRECTION in it.\n\n"
            "query is matched literally as a substring of the message body or "
            "email subject, case-insensitively, so 'acme.io', a URL, or a "
            "phone number all work verbatim. It is not a keyword engine: no "
            "operators, no stemming. A shorter phrase matches more.\n\n"
            "PUT EVERY SPELLING IN ONE CALL. `queries` takes a LIST of "
            "phrases and matches any of them, so instead of one search per "
            "wording — confirmation, then booking, then reference, then the "
            "number itself — pass queries: [\"confirmation\", \"booking\", "
            "\"reference\", \"48213\"] and get the same coverage in one round. "
            "Each extra "
            "round costs far more than each extra phrase, so a generous "
            "list is cheap and a second call is not.\n\n"
            "direction='sent' means messages the user wrote; 'received' means "
            "messages other people wrote to them; leave it out for both. This is "
            "the only tool that can tell those apart, so it is the one for 'did "
            "I send anyone the link', 'who have I told about X', 'what did they "
            "reply'.\n\n"
            "since and until are dates as YYYY-MM-DD and both are inclusive. "
            "Resolve the question's timeframe to real dates yourself from "
            "today's date — do not pass words like 'last month'. Passing only "
            "since/until with no query returns everything in that window, which "
            "is how you read what was said during a period.\n\n"
            "Also takes channel ('imessage', 'whatsapp', 'linkedin', 'email'), "
            "contact_id to restrict to one person, and include_groups (default "
            "TRUE — group-chat messages are searched, and every result carries "
            "its `group` name so you can see which ones they are; pass false to "
            "search only one-to-one messages). Returns the newest matches with a "
            "snippet centred on the match, plus total_matched — quote that "
            "total, not the length of the list.\n\n"
            "EVERY MATCH COMES WITH ITS NEIGHBOURS. Each result carries the "
            "messages just before and just after it in the same conversation, "
            "as `before` and `after`, oldest first (control with `context`, "
            "default 2). Read them. A literal search only finds the words you "
            "gave it, and the answer to a question is usually the NEXT message, "
            "which will not contain those words: ask \"what is the confirmation "
            "number\" and the matching message is the question, while the answer "
            "is a bare number a minute later. A short number, address, "
            "time or yes/no straight after a question IS the answer to it — say "
            "so and quote both sides. Never report that a search found no "
            "answer without having read the messages around the matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Literal phrase to find in the message text"},
                "queries": {"type": "array", "items": {"type": "string"},
                            "description": "SEVERAL literal phrases, matched as "
                                           "any-of, in ONE search. Use this "
                                           "instead of one call per spelling."},
                "since": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "until": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "direction": {"type": "string", "enum": ["sent", "received"],
                              "description": "Omit for both directions"},
                "channel": {"type": "string",
                            "enum": ["imessage", "whatsapp", "linkedin", "email"]},
                "contact_id": {"type": "integer",
                               "description": "Restrict to one contact's messages"},
                "include_groups": {"type": "boolean",
                                   "description": "Search group-chat messages too (default true). Pass false for one-to-one only."},
                "context": {"type": "integer",
                            "description": "Messages either side of each match to include (default 2, max 6). 0 turns it off."},
            },
        },
    },
    {
        "name": "people_in_messages",
        "description": (
            "The same filter as search_messages, but grouped into PEOPLE instead "
            "of messages. Use it whenever the answer is a list of people rather "
            "than a list of things said: 'who have I mentioned acme.io to', "
            "'who did I text in the last month', 'who have I sent the link to', "
            "'which new people did I talk to since June'.\n\n"
            "Returns one row per person — their contact card, how many of their "
            "messages matched, the first and last date one did, and which "
            "channels — ordered by match count. total_people is the true number "
            "of people matching, counted in the database, so it is correct even "
            "when the returned list is capped. Quote total_people.\n\n"
            "GROUP CHATS COUNT HERE TOO, and that changes what a big number "
            "means. Every person also comes back with in_group_threads and "
            "one_to_one, which split matching_messages. A busy group thread "
            "gives everyone in it a high count without any of them having "
            "written to the user directly — so when you rank people by how much "
            "they talk to the user, read one_to_one, and when a person's total is "
            "mostly in_group_threads, say so rather than calling them a close "
            "contact. Pass include_groups: false to leave group threads out "
            "entirely.\n\n"
            "WHO IS NEW. When you pass a date range, every person also comes "
            "back with first_ever — their earliest matching message across the "
            "WHOLE archive, ignoring the range — and new_in_window, which is "
            "true when first_ever falls inside the window. That is the answer to "
            "'new contacts', 'who did I tell for the first time', 'who is new to "
            "this'. Read those fields; do NOT run a second search per person to "
            "check, and do not use `first` for it — `first` is only the earliest "
            "match inside the window, so it is inside the window for everybody "
            "and proves nothing.\n\n"
            "Same query/since/until/direction/channel/include_groups arguments "
            "as search_messages; needs at least a query or a date range. Follow "
            "up with search_messages for the same filter plus a contact_id to "
            "read what was actually said."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Literal phrase to find in the message text"},
                "queries": {"type": "array", "items": {"type": "string"},
                            "description": "SEVERAL literal phrases, matched as "
                                           "any-of, in ONE search. Use this "
                                           "instead of one call per spelling."},
                "since": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "until": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "direction": {"type": "string", "enum": ["sent", "received"],
                              "description": "Omit for both directions"},
                "channel": {"type": "string",
                            "enum": ["imessage", "whatsapp", "linkedin", "email"]},
                "include_groups": {"type": "boolean",
                                   "description": "Count group-chat messages too (default true). Pass false for one-to-one only."},
            },
        },
    },
    {
        "name": "mutual_connections",
        "description": (
            "For a contact (by contact_id, or a name to resolve), return the "
            "mutual connections and shared history LinkedIn showed on their "
            "profile: mutual_names (first names of people you may both know), "
            "shared_history (shared groups/schools/employers) and "
            "recent_activity. Use this for intro questions — 'who could "
            "introduce me to X?', 'who do we both know?'. This is LLM-parsed "
            "from LinkedIn profile TEXT, not a verified connection graph, so "
            "every result is an unverified hint, never a confirmed "
            "relationship (verified is always false)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer",
                               "description": "The contact whose mutuals to look up"},
                "name": {"type": "string",
                         "description": "Name to resolve to a contact when no id is known"},
            },
        },
    },
    {
        "name": "ask_the_user",
        "description": (
            "END YOUR TURN WITH A QUESTION instead of an answer, when the "
            "question you were given has two or more genuinely different "
            "readings and picking one silently would waste the user's time. "
            "The question and its options are shown as buttons; whichever they "
            "pick arrives as their next message and you carry on from there.\n\n"
            "THE BAR IS HIGH, AND IT IS NOT 'AM I UNCERTAIN'. It is: would "
            "searching answer this? Every other tool here reads the user's "
            "own network, so nearly every ambiguity you meet is one you settle "
            "yourself. 'Which Ture?' is a search — find_person returns "
            "years_known and messages_one_to_one and the answer is in those "
            "numbers. 'Investors or customers?' is not a search: nothing in the "
            "network says which one they meant, and the two answers share "
            "almost no people.\n\n"
            "So ask ONLY when all of these hold:\n"
            "- the readings lead to genuinely different sets of people, not to "
            "the same answer ordered differently;\n"
            "- no tool can tell them apart, because the missing fact is the "
            "user's intent rather than anything in their data;\n"
            "- you can name two to four concrete options, in their words, that "
            "cover the realistic readings.\n\n"
            "Otherwise pick the most likely reading, answer it, and say in one "
            "line which reading you took. An answer with a caveat is worth far "
            "more than a question, because it costs one turn rather than three "
            "and it is usually right.\n\n"
            "NEVER ask twice in a row, never ask to confirm something you have "
            "already been told, and never ask for a number or a filter the "
            "shape controls already set. Put the question ONLY in this tool, "
            "not in your text as well, or the user reads it twice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "The question, in one plain sentence"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Two to four concrete answers, each a few words in the "
                        "user's own vocabulary. They become buttons, so they "
                        "must be complete answers to the question and not "
                        "categories of answer."),
                },
                "multi": {"type": "boolean",
                          "description": "True when several options can sensibly "
                                         "be picked together (default false)"},
            },
            "required": ["question", "options"],
        },
    },
]


# ── Asking the user a question back ───────────────────────────────────────────
#
# NC-156. "Who could I introduce this bakery to from my network?" has at
# least three readings — investors, customers, other bakeries — and they share
# almost no people. Ask used to pick one silently. It now comes back with the
# question and some buttons.
#
# WHY A TOOL AND NOT A MODE. Everything the model can do here it does by calling
# a tool, and the loop already knows how to end on one. A question is just the
# eleventh: the model calls it, the loop returns instead of running it, and the
# turn ends with a question where an answer would have been.
#
# THE HARD HALF IS NOT THE TOOL, IT IS THE RULE FOR NOT USING IT. This app has
# ten other tools and thousands of people in it, so nearly every ambiguity a question
# carries is one the model can settle by searching — "which Ture?" is a
# find_person call, not a question for a human. A model that asks every turn
# triples the cost of every answer (ask, read, answer) and is infuriating to
# use. The tool description above is therefore mostly a description of when NOT
# to call it, and api_ask enforces the one rule a prompt cannot: never twice in
# a row.
#
# WHY THE TOOL IS STILL OFFERED WHEN ASKING IS FORBIDDEN. The obvious
# implementation of "you may not ask again" is to drop the tool from the list.
# Don't: TOOLS sits in the cached prefix of every request, so changing it
# invalidates the cache for the whole conversation — a fresh cache write each
# time, on a feature whose entire point is that it is cheaper than guessing
# wrong. The tool stays; run_tool refuses it. The model then gets a one-line
# error mid-round and answers, which costs nothing extra because that round was
# already paid for.

# Two to four. One "option" is not a choice, and five buttons is a form.
ASK_MIN_OPTIONS = 2
ASK_MAX_OPTIONS = 4
# Lengths, because these are rendered as a line of buttons and a model that
# writes a paragraph into an option would break the composer's layout rather
# than being told off. Cut rather than rejected: a slightly clipped option is a
# usable button, a refused question costs the user a whole round.
ASK_QUESTION_MAX = 240
ASK_OPTION_MAX = 60
# NC-163. How many rounds in a row may consist of nothing but a question that
# did not end the turn — a refused one (it already asked last turn) or a
# malformed one (fewer than two distinct options). Every such round is a full
# paid call that re-sends the whole conversation, so left alone a model stuck in
# this groove spends the entire round budget and the answer that finally comes
# back blames the question for needing too many steps. Two, because one is a
# mistake and two is a groove.
ASK_REFUSALS_MAX = 2


def normalize_question(tool_input: Dict[str, Any]) -> Any:
    """The model's ask_the_user input, cleaned — or None if it is unusable.

    None is the important return. Everything here comes from a model, so every
    field is a suggestion: `options` can arrive as a string, as one item, as
    five, as four spellings of the same word, or as a list of empty strings. A
    question with fewer than two real options is not a choice — there is nothing
    to click — so it is refused, and the refusal reaches the model as a tool
    error it can fix in the same round rather than as a dead turn on screen.

    Deduplication is case-insensitive and order-preserving: a model that offers
    "Investors" and "investors" meant one button, and the pair would otherwise
    render as two identical-looking choices that do different things.
    """
    if not isinstance(tool_input, dict):
        return None
    question = str(tool_input.get("question") or "").strip()[:ASK_QUESTION_MAX]
    if not question:
        return None
    raw = tool_input.get("options")
    # A LIST, and a string is not one. This reads as a belt-and-braces type
    # check and is not: a string is iterable, so without it "Investors" — the
    # mistake a model makes when it has a single option in mind — walks through
    # the loop below one character at a time and comes back as four buttons
    # reading I, n, v, e.
    if not isinstance(raw, (list, tuple)):
        return None
    options, seen = [], set()
    for item in raw:
        if not isinstance(item, (str, int, float)):
            continue
        text = str(item).strip()[:ASK_OPTION_MAX]
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        options.append(text)
    if len(options) < ASK_MIN_OPTIONS:
        return None
    # NC-161. A REAL boolean, or False. bool() on a model-supplied field made the
    # string "false" mean True, which is the opposite of what was written — and
    # the schema declares this a boolean, so a string is a model error either
    # way. Guessing wrong towards single-select costs a click; guessing wrong the
    # other way hides the answer behind a Send button nobody noticed.
    return {"question": question,
            "options": options[:ASK_MAX_OPTIONS],
            "multi": tool_input.get("multi") is True}


# ── Who Ask is allowed to see ─────────────────────────────────────────────────
#
# NC-145. One spelling, in one place, for this whole file.
#
# chat.py used to hold THREE answers to "is this person in scope?":
# `population_where('visible')` in the two people-search tools, a hand-written
# `WHERE excluded = 1` in `_excluded_ids` for the message tools, and — in
# `_get_contact` and `_mutual_connections` — nothing at all.
#
# A list-level filter is not a filter when the tool next door hands out the id.
# `find_person` resolved a NAME to an id and `get_contact` took an id with no
# scope test, so two ordinary tool calls turned somebody the user had hidden
# into their name, company, position, city, how-we-met and every note — and
# sent it to the Anthropic API. Hiding someone is the one instruction in this
# app that is purely about privacy, and the AI half was not reading it.
#
# WHICH POPULATION, and why it is not the app's usual one. The rest of the app
# says 'visible' (hidden removed). Ask says 'active' — hidden AND deleted — and
# that is a deliberate departure, for one reason that applies to Ask and to
# nothing else in this codebase:
#
#     every other surface renders on the user's own computer. Ask sends what it
#     reads to Anthropic.
#
# Where two defensible populations differ, the path that leaves the machine
# takes the narrower one. That is not a fifth answer to figures.py's question —
# it is one of its four named populations, chosen on a stated ground, which is
# exactly what that file asks a caller to do.
#
# The two agree today: `db.set_deleted` writes `excluded = 1` alongside
# `deleted = 1`, so every deleted row is also hidden, and no row has one set
# without the other. But `POST /api/contact/{id}/exclude {excluded:false}`
# clears `excluded` and never touches `deleted`, so one call to a live route
# makes a row that every `excluded = 1` filter in this file used to admit.
# (The UI itself cannot do it — `people.js:1501` offers a deleted contact only
# Restore and Purge. The route does not know that.) figures.py calls this
# agreement "load-bearing in the worst way"; 'active' stops leaning on it, and
# costs nothing while it holds.
#
# Both directions come from the one predicate, so they cannot drift apart:
# `_in_scope_sql()` to filter a query, `_out_of_scope_ids()` to check an id.
# That also retires a hand-spelled complement, which is the NC-115 direction.
ASK_POPULATION = "active"


def _in_scope_sql(alias: str = "") -> str:
    """The WHERE predicate for the people Ask may see. `alias` for joins."""
    return figures.population_where(ASK_POPULATION, alias)


def _out_of_scope_ids(conn) -> set:
    """Contact ids Ask must never return — the exact complement of the above.

    Derived from the same predicate rather than typed out again, so widening or
    narrowing the population reaches the by-id checks in the same edit.
    """
    return db.ids_outside(conn, ASK_POPULATION)


def _refuse_out_of_scope(conn, contact_id: Any) -> Any:
    """The refusal for a by-id tool, or None to go ahead.

    Says the person is hidden rather than pretending the id is unknown. The
    only reader is the person who hid them, and "no such contact" would send
    the model looking for a row it can see the id of — but the refusal carries
    no name, company or note, so nothing about them reaches the API.
    """
    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        return None         # not an id at all; the caller says so in its own words
    if cid in _out_of_scope_ids(conn):
        # Say which of the two it was. "Hidden" for somebody the user DELETED
        # sends them looking for an Unhide button that is not on the page —
        # `people.js:1501` offers a deleted contact only Restore and Purge.
        row = conn.execute(
            "SELECT COALESCE(deleted, 0) AS d FROM contacts WHERE id = ?",
            (cid,)).fetchone()
        if row is not None and row["d"]:
            return {"error": "that contact is deleted, so Ask cannot read them. "
                             "Restore them from the Deleted filter on the People "
                             "page if you want them included."}
        return {"error": "that contact is hidden, so Ask cannot read them. "
                         "Un-hide them on their page if you want them included."}
    return None


def _contact_summary(row: Any, strength: int) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"] or "",
        "position": row["position"] or "",
        "home_city": row["home_city"] or "",
        "home_metro": row["home_metro"] or "",
        "role_type": row["role_type"] or "",
        "category": row["category"] or "",
        "strength": strength,
        "strength_label": db.strength_label(strength),
    }


def _search_people(
    conn, location: str = "", role_type: str = "", topic: str = "", min_strength: int = 0,
    limit: int = 0,
) -> Dict[str, Any]:
    """`limit` is this request's cap from shape_caps() — 0 means "use the
    configured NC_CHAT_SEARCH_LIMIT". Whatever it is, total_matched below reports
    what the cap hid, so Quick's smaller cap is visible to the model and (through
    the trail) to the user rather than silently shrinking the world."""
    loc = location.strip().lower()
    role = role_type.strip().lower()
    top = topic.strip().lower()
    if not (loc or role or top or min_strength):
        # NC-127. Several thousand tokens — the single most expensive thing
        # the model can do, and it answers nothing: "everyone", capped at 120
        # arbitrary rows out of thousands. It was reached for as a fallback when
        # a NAME could not be looked up, which find_person now does.
        # search_messages has refused its own no-argument case
        # since NC-081 for exactly this reason; this is the same refusal.
        return {"contacts": [], "total_matched": 0, "error":
                "give at least one filter — location, role_type, topic or "
                "min_strength. With none of them this returns an arbitrary "
                "slice of the whole network, which answers no question and is "
                "the most expensive call available. To find someone BY NAME use "
                "find_person; to find people by what they do use "
                "semantic_search."}
    rows = conn.execute(
        f"SELECT * FROM contacts WHERE {_in_scope_sql()}"
    ).fetchall()
    out = []
    for row in rows:
        if loc:
            metro = (row["home_metro"] or "").strip().lower()
            city = (row["home_city"] or "").strip().lower()
            if loc not in metro and loc not in city:
                continue
        if role and (row["role_type"] or "").strip().lower() != role:
            continue
        if top:
            try:
                topics = json.loads(row["topics"] or "[]")
            except Exception:
                topics = []
            if not any(top in t.lower() for t in topics):
                continue
        strength = db.relationship_strength(row)
        if strength < min_strength:
            continue
        out.append((strength, row))
    out.sort(key=lambda pair: -pair[0])
    cap = limit if limit > 0 else search_people_limit()
    return {"contacts": [_contact_summary(row, strength) for strength, row in out[:cap]],
            "total_matched": len(out)}


def _find_person(conn, name: str = "", limit: int = 0) -> Dict[str, Any]:
    """Resolve a NAME to contacts, ranked by how much relationship is behind each.

    NC-126. Until this existed there were eight tools and not one of them could
    look a person up by name. A question that names one person ("my longtime
    friend Ture") could only be answered by a detour: semantic searches over
    profile text for the phrase "longtime friend" (no results, because nobody's
    LinkedIn headline says that), then a message-TEXT search for the word
    "Ture", which matches every message MENTIONING a Ture and hands back dozens
    of people to pick from. Several tool calls and rounds to identify one
    contact.

    The words in that question are not vague — they are a query. "Longtime"
    means the history is long, "friend" means it is dense and personal, and both
    are columns rather than vibes. So this returns them: how many messages, how
    many of those one-to-one, when the first one was, and how many years ago
    that is. A name plus those four numbers resolves "which Ture" in one call,
    with no embedding model and no second round.
    """
    name = (name or "").strip()
    if not name:
        return {"people": [], "error":
                "give a name to look for — this tool finds a person BY NAME. "
                "For 'who do I know in Berlin' use search_people; for a "
                "description of what someone does use semantic_search."}
    limit = limit or search_people_limit()
    q = db.normalize_name(name)
    q_tokens = [t for t in q.split() if t]

    scored = []
    rows = conn.execute(
        f"SELECT * FROM contacts WHERE {_in_scope_sql()}"
    ).fetchall()
    for row in rows:
        n = db.normalize_name(row["name"] or "")
        if not n:
            continue
        tokens = n.split()
        # Ordered most-certain first. A bare first name that IS one of their
        # name parts ("ture" in "ture exempelsson") outranks a substring match,
        # so "Ture" cannot put "Tureson" above the actual Ture.
        if n == q:
            score = 100
        elif q in tokens:
            score = 85
        elif q_tokens and all(t in tokens for t in q_tokens):
            score = 75
        elif q and n.startswith(q):
            score = 65
        elif q and q in n:
            score = 50
        elif q_tokens and any(t2.startswith(t) and len(t) >= 3
                              for t in q_tokens for t2 in tokens):
            score = 35
        else:
            continue
        scored.append([score, row])
    if not scored:
        return {"people": [], "total_matched": 0, "query": name,
                "note": f"No contact's name matches {name!r}. Try a shorter part "
                        "of the name, or semantic_search if you are describing "
                        "what they do rather than naming them."}

    # Relationship depth for the candidates only — ONE grouped query, not one
    # per person. Capped before the query so the IN list stays sane.
    scored.sort(key=lambda x: -x[0])
    cands = scored[:60]
    stats: Dict[int, Dict[str, Any]] = {}
    if os.path.exists(arc.ARCHIVE_DB_PATH):
        arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        try:
            ids = [r["id"] for _sc, r in cands]
            ph = ",".join("?" * len(ids))
            for m in arc_conn.execute(
                    f"SELECT contact_id, COUNT(*) AS total, "
                    f"  SUM(CASE WHEN COALESCE(group_key,'') = '' THEN 1 ELSE 0 END) "
                    f"    AS one_to_one, "
                    f"  MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                    f"FROM messages WHERE contact_id IN ({ph}) "
                    f"GROUP BY contact_id", ids):
                stats[m["contact_id"]] = dict(m)
        finally:
            arc_conn.close()

    today = _utc_today()
    out = []
    for score, row in cands:
        st = stats.get(row["id"], {})
        first = (st.get("first_ts") or "")[:10]
        years = 0.0
        if first:
            try:
                d0 = datetime.strptime(first, "%Y-%m-%d")
                d1 = datetime.strptime(today, "%Y-%m-%d")
                years = round((d1 - d0).days / 365.25, 1)
            except Exception:
                years = 0.0
        item = _contact_summary(row, db.relationship_strength(row))
        item.update({
            "name_match": score,
            "messages_total": st.get("total", 0) or 0,
            "messages_one_to_one": st.get("one_to_one", 0) or 0,
            "known_since": first,
            "years_known": years,
            "last_message": (st.get("last_ts") or "")[:10],
        })
        out.append(item)
    # Name certainty first, then depth of history: among four people called
    # Ture, the one with years of messages is the one meant by
    # "my longtime friend Ture".
    #
    # The one adjustment, and it earns its complexity. A contact with NO
    # messages at all is docked 20 points, which is exactly one band. In a large
    # address book many contacts have no conversation behind them (a LinkedIn
    # export brings people, not messages), so a bare stub card saved as
    # "Ture" is an ordinary thing to own — and without the penalty that stub
    # beats the actual friend on spelling alone, because "ture" == "ture" is a
    # perfect name match and "Ture Exempelsson" is only a token match. Nobody
    # asking about "my longtime friend Ture" means the empty card.
    #
    # One band, not more, so the demotion cannot invert a real difference in
    # name certainty: searching "Ture Vantablack" still returns Ture Vantablack first even
    # with an empty card, because every other Ture matches that query far more
    # weakly than one band.
    out.sort(key=lambda p: (-(p["name_match"] - (0 if p["messages_total"] else 20)),
                            -p["messages_one_to_one"], -p["years_known"]))
    res: Dict[str, Any] = {
        "people": out[:limit],
        "total_matched": len(scored),
        "returned": len(out[:limit]),
        "query": name,
        "note": ("Ranked by how certain the name match is, then by how much "
                 "history there is. Words like 'longtime', 'oldest', 'close' or "
                 "'best' friend are answered by years_known and "
                 "messages_one_to_one on these rows — do NOT run a semantic "
                 "search for such a phrase; it describes a relationship, and "
                 "semantic_search reads profile text. messages_total includes "
                 "group chats; messages_one_to_one is the personal half, and it "
                 "is the one that means closeness."),
    }
    if len(scored) > len(res["people"]):
        res["note"] += (f" {len(scored)} names matched; the best "
                        f"{len(res['people'])} are here.")
    return res


def _semantic_search(conn, query: str = "", limit: int = 0,
                     queries: Any = None) -> Dict[str, Any]:
    """`limit` is this request's cap from shape_caps(); 0 uses the configured
    NC_CHAT_SEMANTIC_LIMIT. The ranker scores every contact and returns the top
    slice, so there is no "total matched" to report — the depth it looked to is
    the honest number instead, and it is returned as `limit`.

    NC-129. Several phrasings arrive together and come back as ONE ranked list,
    each person appearing once at their best score. Before this, "who works with
    climate, batteries or energy?" was three calls over near-identical ground
    returning 60 rows each — 180 cards for a question whose answer is a
    shortlist, and three model rounds to pay for. The cap now bounds the ANSWER
    rather than each phrasing of the question.
    """
    terms = _terms(query, queries)
    if not terms or not search_cli.embeddings_available():
        return {"contacts": [], "available": False}
    depth = limit if limit > 0 else semantic_limit()
    try:
        ranked = search_cli.semantic_rank_many(conn, terms, limit=depth)
    except Exception as exc:  # noqa: BLE001 — surface as a tool result, not a crash
        return {"contacts": [], "available": False, "error": str(exc)}
    out = []
    for score, cid, term in ranked:
        row = db.get_contact(conn, cid)
        if row is None:
            continue
        c = _contact_summary(row, db.relationship_strength(row))
        c["score"] = round(float(score), 3)
        if len(terms) > 1:
            # Which phrasing found them. Without it a merged list is a ranking
            # with no account of itself, and "why is this person here" is the
            # first thing a reader asks of a semantic result.
            c["matched"] = term
        out.append(c)
    return {"contacts": out, "limit": depth, "queries": terms}


def _get_contact(conn, contact_id: Any) -> Dict[str, Any]:
    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        return {"error": "id must be an integer"}
    # NC-145. `db.get_contact` is a bare SELECT by primary key, so every
    # list-level filter in the app is bypassed by knowing the number — and
    # `find_person` hands the number out. The pair is the bug, not either half.
    refusal = _refuse_out_of_scope(conn, cid)
    if refusal:
        return refusal
    row = db.get_contact(conn, cid)
    if row is None:
        return {"error": "no contact with that id"}
    notes = [n["body"] for n in db.get_notes(conn, cid)]
    strength = db.relationship_strength(row)
    return {
        "contacts": [_contact_summary(row, strength)],
        "how_we_met": row["how_we_met"] or "",
        "notes": notes,
    }


def _search_conversations(conn, query: str, mode: str = "semantic",
                          contact_id: int = 0) -> Dict[str, Any]:
    """Message-text search for the Ask loop — same engines as the app's
    /api/search/conversations (FTS5 keyword / embedded chunks), returned in a
    compact tool-result shape. Excluded contacts never surface.

    With contact_id set this becomes a per-person history read: that contact's
    conversation chunks, newest first, optionally narrowed by the query — the
    primitive behind 'what did X and I talk about?'."""
    query = (query or "").strip()
    if not query and not contact_id:
        return {"conversations": []}
    if not os.path.exists(arc.ARCHIVE_DB_PATH):
        return {"conversations": [], "note": "no message archive imported yet"}

    arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
    try:
        # NC-145: was a fourth hand-written copy of the rule, and hidden-only.
        excluded = _out_of_scope_ids(conn)
        out: List[Dict[str, Any]] = []

        if contact_id:
            if contact_id in excluded:
                return {"conversations": []}
            c = db.get_contact(conn, contact_id)
            if c is None:
                return {"conversations": [], "note": f"no contact with id {contact_id}"}
            sql = ("SELECT * FROM chunks WHERE contact_id = ? "
                   + ("AND text LIKE ? " if query else "")
                   + "ORDER BY ts_end DESC LIMIT 8")
            params = (contact_id, f"%{query}%") if query else (contact_id,)
            for row in arc_conn.execute(sql, params):
                out.append({"contact_id": contact_id, "contact_name": c["name"],
                            "channel": row["channel"], "ts": row["ts_end"],
                            "snippet": row["text"][:400]})
            return {"conversations": out}

        if mode == "semantic" and search_cli.embeddings_available():
            try:
                ranked = search_cli.chunk_semantic_rank(arc_conn, query, limit=12,
                                                        crm_conn=conn)
            except Exception as exc:  # noqa: BLE001 — degrade, don't crash the loop
                return {"conversations": [], "error": f"semantic search failed: {exc}"}
            for score, row in ranked:
                if row["contact_id"] in excluded:
                    continue
                c = db.get_contact(conn, row["contact_id"])
                if c is None:
                    continue
                out.append({"contact_id": row["contact_id"], "contact_name": c["name"],
                            "channel": row["channel"], "ts": row["ts_end"],
                            "snippet": row["text"][:300],
                            "score": round(float(score), 3)})
        else:
            for row in arc.fts_search(arc_conn, query, limit=40):
                if row["contact_id"] in excluded:
                    continue
                c = db.get_contact(conn, row["contact_id"])
                if c is None:
                    continue
                out.append({"contact_id": row["contact_id"], "contact_name": c["name"],
                            "channel": row["channel"], "ts": row["ts"],
                            "snippet": row["snippet"]})
                if len(out) >= 12:
                    break
        return {"conversations": out}
    finally:
        arc_conn.close()


# ── NC-081: the message tools ─────────────────────────────────────────────────
#
# WHY THESE EXIST, when _search_conversations already searches message text.
#
# _search_conversations reads `chunks` — conversations glued into blocks of text
# for the embedding model. That shape is right for "what did X and I talk about",
# and wrong for every question with a date or a direction in it, because a chunk
# has no direction at all (it mixes both sides) and its ts is the block's end, not
# any message's. There is no date parameter, so "who did I text in the last month"
# was not a hard question — it was an inexpressible one.
#
# These three read `messages` instead: one row per message, with `direction`
# (0 in / 1 out), an indexed `ts`, `channel`, `group_name`. They map onto the
# three things a question actually needs:
#
#   message_stats        where the text is, how much, over what dates
#   search_messages      the messages themselves, filtered by date/direction
#   people_in_messages   the same filter, grouped into a list of people
#
# WHY LIKE AND NOT FTS5. `messages_fts` exists and works, but FTS5 raises a
# syntax error on a bare dotted term — MATCH 'acme.com' is an error, not zero
# results — and the query string here comes straight from a model. A tool that can
# be made to throw by its own input is a tool that fails on the exact question
# this was built for. LIKE is literal, cannot be made to throw, and a full scan
# is fast enough for an interactive tool, and faster still once a date range
# lets the ts index do the work. FTS5 can also match more rows than LIKE,
# because it tokenises a dotted term such as "acme.com" into two words and
# finds "acme" near "com".
_MSG_SNIPPET = 220          # characters around the match, not from the start
_READ_MAX = 12000           # characters read_message returns in one call

# NC-126: how much of the conversation AROUND a match comes back with it.
#
# The failure this exists for. Asked for a number that was sent in a chat, a
# search finds messages containing the word "confirmation" and reports that none
# of them has a number in it. The search is right and the answer is still wrong:
# the number is the VERY NEXT MESSAGE, sent inside the same minute — "What's the
# confirmation number?", then "12345" a moment later. Such a number arrives as a
# bare message with no words in it at all, so no keyword can ever reach it; the
# only thing that identifies it is what it sits next to. The search has the
# answer one row away and cannot see it.
#
# Neighbours are cut shorter than matches (150 vs 220 characters) because they
# are context, not evidence, and there are several of them per hit. The budget is
# a hard cap across one result set: a search matching 50 messages must not
# quietly return 250.
_CTX_SNIPPET = 150          # characters per neighbouring message
_CTX_SPAN_DEFAULT = 2       # messages either side, when the caller says nothing
_CTX_SPAN_MAX = 6
_CTX_BUDGET = 80            # neighbours in total, across every match in one call
_MSG_DIRECTIONS = {"sent": 1, "received": 0}


def _excluded_ids(conn) -> set:
    """Contacts that never appear in any tool result.

    NC-145: this used to spell `WHERE excluded = 1` by hand, which made it the
    third of three different scope rules in this file. It is now the complement
    of the one population Ask uses — same predicate, opposite direction — so the
    message tools and the people tools cannot disagree about who is hidden.
    The name is kept because thirteen call sites read well with it.
    """
    return _out_of_scope_ids(conn)


def _valid_date(raw: Any) -> str:
    """A YYYY-MM-DD date, or "" if it is not one.

    Model input, so it is validated rather than trusted, and the caller turns ""
    into a stated error. Silently dropping an unparseable date would answer a
    question about last month with the whole archive and look confident doing it.
    """
    text = str(raw or "").strip()[:10]
    if not text:
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return text


def _snippet(body: str, subject: str, query: Any) -> str:
    """A window centred on the match, not the first N characters.

    An iMessage can be thousands of characters long. `body[:300]` on a match at
    character 1800 returns text that does not contain the query, and the model
    reads that as a false positive and drops a real hit. So find the match and cut
    around it.
    """
    text = (subject + " — " + body) if subject else body
    text = " ".join(text.split())
    if not text:
        return ""
    # Centre on whichever of the terms this message actually contains — with
    # several spellings in one call, the first one is often not the one that hit.
    at = 0
    low = text.lower()
    for _t in _terms(query):
        found = low.find(_t.lower())
        if found >= 0:
            at = found
            break
    start = max(0, at - _MSG_SNIPPET // 3)
    out = text[start:start + _MSG_SNIPPET]
    return ("…" if start else "") + out + ("…" if start + _MSG_SNIPPET < len(text) else "")


def _read_message(conn, message_id: Any, offset: Any = 0) -> Dict[str, Any]:
    """One whole message, by id (NC-143).

    Every other tool here returns a WINDOW: 220 characters around a match, 150
    of a neighbour, 300-400 of a chunk. That is right for iMessage, where the
    typical message is a line or two and almost none run past the window. It is
    wrong for email, where most do — Ask was answering questions about mail
    from a fifth of a page and had no way to ask for the rest.

    This is deliberately a SEPARATE call rather than a wider window on every
    result. NC-127 established that the bill is in the rounds, not the results,
    but that cuts both ways: making every search return whole emails would put
    thousands of unread characters into every round for the one occasion they
    matter. One extra round, on demand, is the cheaper shape.

    `offset` exists so a genuinely long message can be read in pieces instead of
    being silently cut the way NC-142's importer was. The reply always says how
    much there is and whether any remains.
    """
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return {"error": "read_message needs the numeric id of a message, "
                         "which search_messages puts on any result it had to cut."}
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0

    if not os.path.exists(arc.ARCHIVE_DB_PATH):
        return {"error": "no message archive on this Mac"}
    arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
    row = arc_conn.execute(
        "SELECT m.* FROM messages m WHERE m.id = ?", (mid,)).fetchone()
    if row is None:
        return {"error": f"no message with id {mid}"}
    if row["contact_id"] in _excluded_ids(conn):
        return {"error": "that message belongs to a hidden contact"}

    text = (row["body"] or "")
    total = len(text)
    piece = text[start:start + _READ_MAX]
    c = db.get_contact(conn, row["contact_id"])
    out = {
        "id": row["id"],
        "ts": row["ts"],
        "channel": row["channel"],
        "direction": "sent" if row["direction"] == 1 else "received",
        "contact_name": c["name"] if c else "(unknown contact)",
        "text": piece,
        "total_chars": total,
    }
    if row["subject"]:
        out["subject"] = row["subject"]
    if row["group_name"]:
        out["group"] = row["group_name"]
    # Said only when true, and said as the NEXT CALL rather than as a fact about
    # the string — "there is more" that does not say how to get it is the same
    # silent truncation this tool exists to end.
    if start + len(piece) < total:
        out["continues_at_offset"] = start + len(piece)
    return out


def _terms(query: Any, queries: Any = None) -> List[str]:
    """The list of literal phrases a search is looking for.

    NC-127. Accepting several was the single biggest cost fix in this file, and
    it is not about the size of a result — it is about ROUNDS. A literal search
    finds only the word it was given, so the honest instruction is "try the
    other spellings too", and a model obeys it the only way it can: separate
    calls, one per spelling, across many rounds. Each round re-writes
    the cached prompt prefix, and cache WRITES are billed at 1.25x — which can
    come to more than the output tokens cost. Trimming the result shapes made no
    difference, because the bill was never in the results.

    Eight spellings in one call is one round. Same coverage, same answer.
    """
    out = [query] if isinstance(query, str) else list(query or [])
    out += list(queries or [])
    seen, terms = set(), []
    for t in out:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    return terms


def _ctx_snippet(body: str, subject: str) -> str:
    """A neighbour's opening words. No query to centre on — it did not match."""
    text = (subject + " — " + body) if subject else (body or "")
    text = " ".join(text.split())
    return text[:_CTX_SNIPPET] + ("…" if len(text) > _CTX_SNIPPET else "")


def _same_thread(row: Any):
    """The SQL for 'the same conversation as this message', and its parameters.

    A group message's conversation is its thread (`group_key`, which NC-110 made
    an identity separate from the label). A one-to-one message's conversation is
    that person's one-to-one history — `group_key = ''` matters, or a reply in a
    group chat would be offered as context for a private message and read as
    though the person had said it to the user alone.
    """
    gk = row["group_key"] or ""
    if gk:
        return "m.group_key = ?", [gk]
    return "m.contact_id = ? AND COALESCE(m.group_key, '') = ''", [row["contact_id"]]


def _thread_context(arc_conn, row: Any, span: int, seen: set,
                    day: str = "", excluded: Any = ()) -> Dict[str, List]:
    """The `span` messages either side of a match, in its own conversation.

    Ordering ties on `ts` are broken by `id`. That is not defensive: the case
    this was built for has the question and its answer in the same minute, and on
    an archive whose timestamps are second-resolution a tie is exactly where the
    interesting pair sits.

    `seen` carries message ids already rendered anywhere in this result set, so
    two matches three apart share the messages between them instead of printing
    them twice, and a neighbour that is itself a match is not repeated as one.

    `excluded` is NC-145, and it is the reason this is not just the match query
    run twice. `_msg_filters` scopes the rows that MATCHED; these are fetched by
    a second query keyed on the thread, so they never went through it. In a
    group thread `_same_thread` selects on `group_key` alone — every member's
    messages — so up to 80 neighbours of 150 characters each, written by anyone
    in the thread including someone the user hid, went to Anthropic as "them: …".
    It need not fire on a given archive, for a reason that is luck rather than
    design: if every message belonging to a hidden contact happens to be
    one-to-one, the other branch is taken and the leak stays invisible.

    The filter goes in the SQL rather than in the loop below, so an excluded
    neighbour does not silently consume one of the `span` slots a real neighbour
    should have had.
    """
    where, params = _same_thread(row)
    ids = [int(i) for i in (excluded or ())]
    if ids:
        where += f" AND m.contact_id NOT IN ({','.join('?' * len(ids))})"
        params = params + ids
    out: Dict[str, List] = {"before": [], "after": []}
    for key, cmp_, order in (("before", "<", "DESC"), ("after", ">", "ASC")):
        rows = arc_conn.execute(
            f"SELECT m.* FROM messages m WHERE {where} "
            f"  AND (m.ts {cmp_} ? OR (m.ts = ? AND m.id {cmp_} ?)) "
            f"ORDER BY m.ts {order}, m.id {order} LIMIT ?",
            params + [row["ts"], row["ts"], row["id"], span]).fetchall()
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            # One string, not {"direction":…,"ts":…,"text":…}. Those three key
            # names cost about 14 tokens per neighbour before a single character
            # of the message, and a result set carries up to 80 of them — more
            # tokens spent on punctuation than on what was said.
            # The day is dropped when it matches the match's own day, which it
            # does for almost every neighbour.
            when = r["ts"][11:16] if r["ts"][:10] == day else r["ts"][:16]
            who = "me" if r["direction"] == 1 else "them"
            out[key].append(f'{when} {who}: {_ctx_snippet(r["body"], r["subject"])}')
        if key == "before":
            out[key].reverse()   # oldest first, so it reads as a conversation
    return out


def _msg_filters(query: Any, since: str, until: str, direction: str,
                 channel: str, contact_id: int, include_groups: bool,
                 excluded: Any = ()):
    """The WHERE clause shared by both search tools, as (sql, params).

    One function because the two tools MUST filter identically. When
    people_in_messages says N people and search_messages then lists messages from
    fewer of them for the same filter, the difference is a bug the user cannot see —
    and two hand-written WHERE clauses is how that happens.

    `excluded` goes in HERE rather than being skipped over in the result loop,
    which is where _search_conversations does it. That matters because both tools
    report a total from a COUNT over this same clause: filtering in Python would
    have every total include people the user has excluded, and then a
    "N people match, M shown" note describing a cap that never applied.
    """
    where = ["(m.body <> '' OR m.subject <> '')"]
    params: List[Any] = []
    # `query` may be one phrase or several. Several are OR-ed, so one call can
    # cover every spelling of the same thing instead of one round each.
    for _t in _terms(query):
        # ESCAPE, because '%' and '_' are LIKE's own wildcards and this pattern is
        # built from model input. A query of "ada_lovelace" would match
        # "adaXlovelace", and a stray '%' matches every message in the archive and
        # reports it as a hit — a search that cannot fail is worse than one that
        # returns nothing. The '\' is escaped first or it would escape itself.
        pat = "%" + (_t.replace("\\", "\\\\").replace("%", "\\%")
                     .replace("_", "\\_")) + "%"
        params += [pat, pat]
    if params:
        one = "(m.body LIKE ? ESCAPE '\\' OR m.subject LIKE ? ESCAPE '\\')"
        where.append("(" + " OR ".join([one] * (len(params) // 2)) + ")")
    if since:
        where.append("m.ts >= ?")
        params.append(since + " 00:00:00")
    if until:
        # Inclusive of the whole day. `ts <= '2000-01-01'` would exclude every
        # message sent on the 1st, because '2000-01-01 14:20:00' sorts after it.
        where.append("m.ts <= ?")
        params.append(until + " 23:59:59")
    if direction in _MSG_DIRECTIONS:
        where.append("m.direction = ?")
        params.append(_MSG_DIRECTIONS[direction])
    if channel:
        where.append("m.channel = ?")
        params.append(channel)
    if contact_id:
        where.append("m.contact_id = ?")
        params.append(contact_id)
    if not include_groups:
        where.append("m.group_name = ''")
    ids = [int(i) for i in (excluded or ())]
    if ids:
        where.append("m.contact_id NOT IN (%s)" % ",".join("?" * len(ids)))
        params += ids
    return " AND ".join(where), params


def _message_stats(conn) -> Dict[str, Any]:
    """Where the message text is and what it covers. The orientation tool.

    The model had no way to learn that an archive exists, how big it is, which
    channels are in it or what dates it spans, so a question about texts was
    answered by guessing whether texts were searchable at all. This is one cheap
    call that replaces the guess — and it names the empty case explicitly, because
    "I found nothing" and "there is nothing to find" are different answers.
    """
    if not os.path.exists(arc.ARCHIVE_DB_PATH):
        return {"archive_present": False, "total_messages": 0, "channels": [],
                "note": "No message archive has been imported yet, so there is no "
                        "message text to search. Say that rather than reporting "
                        "zero results for the question."}
    arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
    try:
        # Excluded contacts are filtered out HERE too, on the same terms the
        # search tools use. If this counted them and the search tools did not,
        # the model would be told there are N messages and then be unable to
        # reach N of them — two numbers with the same name meaning different
        # populations, which is the trap this whole file keeps falling into.
        #
        # NC-098: it was falling into it anyway, one argument along. This passed
        # include_groups=True while both search tools defaulted to False, so the
        # model was told one number and could reach a smaller one — a hole as big
        # as the group chatter. That is the NC-081 failure again: the model
        # spends calls looking for something the count promised and the tool
        # will not return, and nothing errors.
        #
        # NC-100 closed the hole from the other end instead — the search tools
        # now default to include_groups=True, so the honest headline is the whole
        # archive again. The rule that survives both changes is the invariant,
        # not either number: THIS COUNT AND WHAT THE SEARCH TOOLS RETURN BY
        # DEFAULT MUST BE THE SAME POPULATION. `include_groups` below is that
        # promise written down; change one and the tests fail.
        #
        # in_group_threads stays, now as a breakdown rather than a subtraction:
        # group chats ARE searchable, and it is still worth knowing how much of
        # the iMessage history is group chatter before quoting a per-person
        # number at anybody.
        excluded = _excluded_ids(conn)
        where, wparams = _msg_filters("", "", "", "", "", 0, True, excluded)
        g_where, g_params = where, wparams
        rows = arc_conn.execute(
            f"SELECT m.channel channel, COUNT(*) n, MIN(m.ts) lo, MAX(m.ts) hi, "
            f"COUNT(DISTINCT m.contact_id) people, "
            f"SUM(m.direction = 1) sent, SUM(m.direction = 0) received "
            f"FROM messages m WHERE {where} "
            f"GROUP BY m.channel ORDER BY n DESC", wparams).fetchall()
        # Per channel, so "WhatsApp is mostly group threads" is visible rather
        # than being one lump subtracted from the whole archive.
        in_groups = {r["channel"]: r["n"] for r in arc_conn.execute(
            f"SELECT m.channel channel, COUNT(*) n FROM messages m "
            f"WHERE {g_where} AND m.group_name <> '' GROUP BY m.channel",
            g_params)}
        channels = [{"channel": r["channel"], "messages": r["n"],
                     "people": r["people"], "sent": r["sent"] or 0,
                     "received": r["received"] or 0,
                     "in_group_threads": in_groups.get(r["channel"], 0),
                     "first": (r["lo"] or "")[:10], "last": (r["hi"] or "")[:10]}
                    for r in rows]
        total = sum(c["messages"] for c in channels)
        groups_total = sum(in_groups.values())
        span = arc_conn.execute(
            f"SELECT MIN(m.ts) lo, MAX(m.ts) hi, COUNT(DISTINCT m.contact_id) people "
            f"FROM messages m WHERE {where}", wparams).fetchone()
        return {
            # total_messages, not "messages". _search_messages returns a LIST
            # under `messages`, and one key name holding a list in one tool and a
            # count in another is how _tool_matched came to call len() on an int.
            # Same word, two populations — the exact trap this file keeps hitting.
            "archive_present": True,
            "total_messages": total,
            "in_group_threads": groups_total,
            "people_with_messages": span["people"] or 0,
            "first_message": (span["lo"] or "")[:10],
            "last_message": (span["hi"] or "")[:10],
            "today": _utc_today(),
            "channels": channels,
            "contacts_excluded_from_every_count": len(excluded),
            "note": "Message text is searchable with search_messages (individual "
                    "messages) and people_in_messages (grouped by person). Both "
                    "take since/until as YYYY-MM-DD and direction as "
                    "sent/received. Dates above are the real limits of the "
                    "archive — a question about a period outside them has no data "
                    "behind it, and that is worth saying. "
                    "total_messages is everything those two tools can reach with "
                    "their default arguments, group chats included. "
                    "in_group_threads is how many of that same total came from "
                    "group threads rather than one-to-one — a SUBSET, never "
                    "something to add on. It matters when the question is about a "
                    "RELATIONSHIP: a busy group thread gives everyone in it a "
                    "high message count without any of them having talked to the "
                    "user directly, so people_in_messages reports one_to_one "
                    "beside every person\'s total. Pass include_groups: false to "
                    "either tool to leave group threads out.",
        }
    finally:
        arc_conn.close()


def _search_messages(conn, query: str = "", since: str = "", until: str = "",
                     direction: str = "", channel: str = "", contact_id: int = 0,
                     include_groups: bool = True, limit: int = 0,
                     context: int = _CTX_SPAN_DEFAULT,
                     queries: Any = None) -> Dict[str, Any]:
    """Individual messages matching a literal phrase and/or a date range.

    This is the tool behind "did I send anyone the link", because it can filter on
    direction — outbound only — which nothing else in this file can do.

    NC-100: include_groups defaults TRUE. It was False, which left every group
    message unfindable. Search has to be able to reach every message.

    The old default had a reason, and the reason was borrowed from the wrong
    tool. "A group message is not evidence of a one-to-one relationship with each
    participant" is true, and it is an argument about COUNTING PEOPLE, not about
    finding text. Asked "did I send anyone the link", a message sent to a group
    is a hit; excluding it loses the answer outright. Every row already carries
    its `group` name, so a group hit reads as one.

    NC-126: every match also carries the messages either side of it, in its own
    conversation. A literal search can only find the words it was given, and the
    answer to a question is very often the message AFTER the one containing
    them — a bare number, an address, a yes. Without neighbours this tool
    reports, correctly and uselessly, that the matching messages hold no answer.
    """
    limit = limit or message_limit()
    query = _terms(query, queries)
    if not os.path.exists(arc.ARCHIVE_DB_PATH):
        return {"messages": [], "archive_present": False,
                "note": "no message archive imported yet"}
    bad = [name for name, raw in (("since", since), ("until", until))
           if raw and not _valid_date(raw)]
    if bad:
        return {"messages": [], "error":
                f"{', '.join(bad)} must be a date as YYYY-MM-DD (today is "
                f"{_utc_today()}). Resolve the timeframe in the question to real "
                f"dates and call again."}
    since, until = _valid_date(since), _valid_date(until)
    if direction and direction not in _MSG_DIRECTIONS:
        return {"messages": [], "error":
                "direction must be 'sent' or 'received', or left out for both"}
    if not (query or since or until or contact_id):
        return {"messages": [], "error":
                "give at least a query, a date range, or a contact_id — this would "
                "otherwise return the newest messages in the whole archive, which "
                "answers no question."}

    arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
    try:
        excluded = _excluded_ids(conn)
        where, params = _msg_filters(query, since, until, direction, channel,
                                     contact_id, include_groups, excluded)
        total = arc_conn.execute(
            f"SELECT COUNT(*) FROM messages m WHERE {where}", params).fetchone()[0]
        # Counted and reported, never silently dropped. Group chats are off by
        # default because a group message is not evidence of a one-to-one
        # relationship with each participant, but "20 more of these are in group
        # threads" is a real part of the answer and hiding it is how a count comes
        # out lower than the user knows it should be.
        dropped_groups = 0
        if not include_groups:
            g_where, g_params = _msg_filters(query, since, until, direction,
                                             channel, contact_id, True, excluded)
            dropped_groups = arc_conn.execute(
                f"SELECT COUNT(*) FROM messages m WHERE {g_where} "
                "AND m.group_name <> ''", g_params).fetchone()[0]

        rows = arc_conn.execute(
            f"SELECT m.* FROM messages m WHERE {where} "
            f"ORDER BY m.ts DESC LIMIT ?", params + [limit]).fetchall()
        span = max(0, min(int(context), _CTX_SPAN_MAX))
        # Neighbours are precious on a precise search and waste on a sweep. Four
        # hits for a specific phrase is a question; 72 hits for "everything he
        # sent over two days" is a trawl, and surrounding every one of those
        # with more messages just reprints the same conversation twice.
        if len(rows) > 25:
            span = min(span, 1)
        elif len(rows) > 8:
            span = min(span, 2)
        # Every match is marked seen BEFORE any context is gathered, so a match
        # is never also printed as somebody else's neighbour. Two hits three
        # apart then share the messages between them instead of repeating them.
        seen_ids = {r["id"] for r in rows}
        ctx_budget = _CTX_BUDGET
        ctx_truncated = False
        out: List[Dict[str, Any]] = []
        for r in rows:
            c = db.get_contact(conn, r["contact_id"])
            item = {
                "direction": "sent" if r["direction"] == 1 else "received",
                "ts": r["ts"],
                "snippet": _snippet(r["body"], r["subject"], query),
            }
            # NC-143. The id rides along ONLY when the snippet is a window onto
            # something longer — which is the one case where the model has a
            # reason to call read_message. Sending an id on every row would put
            # it on every row of a sweep that is already complete, and NC-127
            # found that field names on rows nobody reads were more than half of
            # a result. `full_chars` is what tells the model whether the rest is a
            # sentence or three pages.
            # Two different measurements on purpose, and they must not be
            # confused. WHETHER it was cut is decided on the collapsed text,
            # because that is what _snippet actually windows. HOW MUCH there is
            # reports the stored body, because that is what read_message will
            # hand back — a model told "full_chars: 1000" and then given 1001
            # characters has been given a reason to distrust both numbers.
            _shown = len(" ".join(((r["subject"] + " — " + r["body"])
                                   if r["subject"] else (r["body"] or "")).split()))
            if _shown > _MSG_SNIPPET:
                item["id"] = r["id"]
                item["full_chars"] = len(r["body"] or "")
            # Only what varies. `date` was `ts[:10]` — the same string again.
            # `group: ""` was on every one-to-one row. And when the search is
            # already scoped to one person, their id and name were repeated on
            # every message; they are stated once at the top level instead.
            # Repeated on every row, those were most of a large result.
            if not contact_id:
                item["contact_id"] = r["contact_id"]
                item["contact_name"] = c["name"] if c else "(unknown contact)"
            if not channel:
                item["channel"] = r["channel"]
            if r["group_name"]:
                item["group"] = r["group_name"]
            if span and ctx_budget > 0:
                ctx = _thread_context(arc_conn, r, span, seen_ids, r["ts"][:10],
                                      excluded)
                ctx_budget -= len(ctx["before"]) + len(ctx["after"])
                for key in ("before", "after"):
                    if ctx[key]:
                        item[key] = ctx[key]
            elif span:
                ctx_truncated = True
            out.append(item)
        # The distinct people behind those messages, as contact summaries.
        #
        # Two things need this. The Ask view builds a clickable card for every
        # person the answer names by matching the name back to the contact
        # summaries the tools returned — so without this, a perfectly correct
        # answer naming several people rendered plain names and zero cards, and
        # the user could not open any of them. And the model otherwise knows only a
        # name and an id, so it burned a get_contact round per person to find out
        # where they live and what they do.
        # Built from the RAW rows, not from `out`. NC-127 hoists contact_id off
        # the compacted message when the search is already scoped to one
        # person, and reading it back off the trimmed dict raised KeyError for
        # every scoped search — i.e. the most common call there is.
        seen: Dict[int, Dict[str, Any]] = {}
        for _r in rows:
            cid = _r["contact_id"]
            if cid in seen:
                continue
            c = db.get_contact(conn, cid)
            if c is not None:
                seen[cid] = _contact_summary(c, db.relationship_strength(c))
        res: Dict[str, Any] = {
            "messages": out,
            "people": list(seen.values()),
            "total_matched": total,
            "returned": len(out),
            "limit": limit,
            "filters": {"query": " | ".join(query), "since": since,
                        "until": until,
                        "direction": direction or "both",
                        "channel": channel or "all",
                        "contact_id": contact_id or 0,
                        "include_groups": include_groups,
                        "context": span},
        }
        if contact_id:
            # NC-145. This echo is built from the ARGUMENT, not from the rows, so
            # it survived a result set the filter had already emptied: pass a
            # hidden person's id and their messages were gone but their NAME
            # came back anyway. The id is the model's own input; the name is not.
            _c = (None if contact_id in excluded
                  else db.get_contact(conn, contact_id))
            res["all_messages_are_with"] = {
                "contact_id": contact_id,
                "contact_name": _c["name"] if _c else "(unknown contact)"}
        if channel:
            res["all_messages_on_channel"] = channel
        if span:
            # One line. The full explanation lives in the tool DESCRIPTION,
            # which is sent once and cached; repeating it in every result paid
            # for the same 60 tokens on every call of every round.
            res["context_note"] = (
                "`before`/`after` are the surrounding messages, oldest first — "
                "the answer to a question is usually the next one.")
        if ctx_truncated:
            res["context_truncated"] = (
                f"Only the first matches carry surrounding messages "
                f"({_CTX_BUDGET} neighbours max per call). Narrow the search to "
                "see context for the rest.")
        if dropped_groups and not include_groups:
            res["group_messages_not_counted"] = dropped_groups
        if total > len(out):
            res["note"] = (f"{total} messages match; the newest {len(out)} are "
                           f"here. Narrow the dates or the query to see the rest, "
                           f"and say the total rather than implying this is all.")
        elif total == 0:
            res["note"] = ("Nothing matched. The query is a literal phrase, so try "
                           "a shorter or differently-spelled one before concluding "
                           "there is nothing there. Check message_stats for the "
                           "channels and dates that actually exist.")
        return res
    finally:
        arc_conn.close()


def _people_in_messages(conn, query: str = "", since: str = "", until: str = "",
                        direction: str = "", channel: str = "",
                        include_groups: bool = True,
                        limit: int = 0, queries: Any = None) -> Dict[str, Any]:
    """The same filter as search_messages, grouped into people.

    "Who have I mentioned Acme to" is not a question about messages, it is a
    question about people — and answering it from a message list means the model
    counting names by hand across a capped page of rows, which it gets wrong. This
    does the grouping in SQL, so the count is the real count even when the message
    list would have been truncated.
    """
    limit = limit or message_people_limit()
    query = _terms(query, queries)
    if not os.path.exists(arc.ARCHIVE_DB_PATH):
        return {"people": [], "archive_present": False,
                "note": "no message archive imported yet"}
    bad = [name for name, raw in (("since", since), ("until", until))
           if raw and not _valid_date(raw)]
    if bad:
        return {"people": [], "error":
                f"{', '.join(bad)} must be a date as YYYY-MM-DD (today is "
                f"{_utc_today()})."}
    since, until = _valid_date(since), _valid_date(until)
    if direction and direction not in _MSG_DIRECTIONS:
        return {"people": [], "error":
                "direction must be 'sent' or 'received', or left out for both"}
    if not (query or since or until):
        return {"people": [], "error":
                "give at least a query or a date range — grouping the entire "
                "archive by person answers no question."}

    arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
    try:
        excluded = _excluded_ids(conn)
        where, params = _msg_filters(query, since, until, direction, channel,
                                     0, include_groups, excluded)
        # NC-100: `grouped` counts, per person, how many of their matching
        # messages came from a group thread. Computed in the same pass, so it
        # cannot describe a different filter than `n` does.
        #
        # This is what makes including group chats safe HERE. Searching text
        # wants every message; counting PEOPLE does not, because a 50-person
        # thread with thousands of messages in it would otherwise read as 50 strong
        # relationships. Excluding them hid a real answer; including them
        # silently would inflate one. Reporting both lets the model say which it
        # is looking at, and say so out loud.
        # NC-111 adds one more thing that is not a person: a message the owner
        # sent to a group belongs to the conversation, so it is stored under
        # arc.NO_PERSON. Text search still wants those rows — they are half of
        # every group thread — but this query counts people, and `total_people`
        # below is len(rows), so leaving them in would report one person too
        # many while the list itself silently dropped the row.
        rows = arc_conn.execute(
            f"SELECT m.contact_id, COUNT(*) n, MIN(m.ts) lo, MAX(m.ts) hi, "
            f"       SUM(m.group_name <> '') grouped, "
            f"       GROUP_CONCAT(DISTINCT m.channel) chans "
            f"FROM messages m WHERE {where} AND {arc.is_a_person('m.')} "
            f"GROUP BY m.contact_id ORDER BY n DESC", params).fetchall()

        # first_ever: the same query and direction with the DATE RANGE REMOVED.
        #
        # This is what makes "new" answerable in one call. `first` above is the
        # earliest match INSIDE the window, which cannot distinguish somebody
        # hearing about a topic for the first time from somebody who has been
        # talking about it for two years — and the model was right not to trust
        # it. Asked which contacts were new to Acme last month, a model would
        # otherwise run people_in_messages once and then a separate
        # before-the-window search for every person, to establish what this one
        # extra grouped query answers for all of them: new means
        # first_ever >= since.
        ever: Dict[int, str] = {}
        if since or until:
            e_where, e_params = _msg_filters(query, "", "", direction, channel,
                                             0, include_groups, excluded)
            ever = {r["contact_id"]: (r["lo"] or "")[:10] for r in arc_conn.execute(
                f"SELECT m.contact_id, MIN(m.ts) lo FROM messages m "
                f"WHERE {e_where} AND {arc.is_a_person('m.')} "
                f"GROUP BY m.contact_id", e_params)}
        total_people = len(rows)
        total_msgs = sum(r["n"] for r in rows)
        total_grouped = sum(int(r["grouped"] or 0) for r in rows)
        people: List[Dict[str, Any]] = []
        for r in rows[:limit]:
            c = db.get_contact(conn, r["contact_id"])
            if c is None:
                continue
            strength = db.relationship_strength(c)
            item = _contact_summary(c, strength)
            first = (r["lo"] or "")[:10]
            in_groups = int(r["grouped"] or 0)
            item.update({
                "matching_messages": r["n"],
                "in_group_threads": in_groups,
                "one_to_one": r["n"] - in_groups,
                "first": first,
                "last": (r["hi"] or "")[:10],
                "channels": sorted((r["chans"] or "").split(",")),
            })
            if since or until:
                first_ever = ever.get(r["contact_id"], first)
                item["first_ever"] = first_ever
                item["new_in_window"] = bool(since) and first_ever >= since
            people.append(item)
        res: Dict[str, Any] = {
            "people": people,
            "total_people": total_people,
            "total_messages": total_msgs,
            "in_group_threads": total_grouped,
            "returned": len(people),
            "limit": limit,
            "filters": {"query": " | ".join(query), "since": since, "until": until,
                        "direction": direction or "both",
                        "channel": channel or "all",
                        "include_groups": include_groups},
        }
        if since or until:
            res["new_people_in_window"] = sum(
                1 for p in people if p.get("new_in_window"))
            res["first_ever_meaning"] = (
                "first_ever is that person's earliest matching message across the "
                "WHOLE archive for this query and direction, ignoring the date "
                "range. new_in_window is first_ever >= since. Use those two rather "
                "than searching each person separately — `first` is only the "
                "earliest match inside the window and cannot tell you who is new.")
        if total_people > len(people):
            res["note"] = (f"{total_people} people match; the {len(people)} with "
                           f"the most matching messages are here. total_people is "
                           f"the number to quote, not the length of this list.")
        elif total_people == 0:
            res["note"] = ("Nobody matched. The query is a literal phrase — try a "
                           "shorter one, or check message_stats for the dates the "
                           "archive really covers.")
        return res
    finally:
        arc_conn.close()


def _mutual_connections(conn, contact_id: Any = 0, name: str = "") -> Dict[str, Any]:
    """Named mutual connections + shared history re-parsed from the archived
    LinkedIn profile (enrich_li_extras.py wrote contacts.li_extra). It's LLM-
    parsed profile TEXT, not a real graph edge — a 'mutual name' is just a first
    name LinkedIn happened to render on the page — so the result is always
    flagged verified=False for the model to surface as a hint, never a fact."""
    try:
        cid = int(contact_id or 0)
    except (TypeError, ValueError):
        cid = 0
    # NC-145. Both ways in were unscoped. The id path shared _get_contact's
    # bare SELECT; the name path fell back to `db.search_contacts`, which has no
    # scope filter either — so a NAME alone resolved to a hidden person's row,
    # which is the same two-call pair from the other end.
    refusal = _refuse_out_of_scope(conn, cid) if cid else None
    if refusal:
        return refusal
    row = db.get_contact(conn, cid) if cid else None
    if row is None and (name or "").strip():
        # Let the model pass a name when it never looked up an id — take the
        # single closest match, same primitive the app's name search uses.
        out = _out_of_scope_ids(conn)
        matches = [m for m in db.search_contacts(conn, name.strip(), limit=8)
                   if m["id"] not in out]
        row = matches[0] if matches else None
    if row is None:
        return {"error": "no matching contact — pass a valid contact_id or name"}

    # li_extra is optional and enrichment-written: it may be absent, empty,
    # invalid JSON, or not a dict. Degrade to a clean empty result, never raise.
    try:
        li = json.loads(row["li_extra"] or "{}") if "li_extra" in row.keys() else {}
    except Exception:
        li = {}
    if not isinstance(li, dict):
        li = {}
    return {
        "contact_id": row["id"],
        "contact_name": row["name"],
        "mutual_names": li.get("mutual_names") or [],
        "shared_history": li.get("shared_history") or [],
        "recent_activity": li.get("recent_activity") or "",
        "verified": False,  # always — LinkedIn-derived, suggestive, not confirmed
    }


def _tool_detail(name: str, tool_input: Dict[str, Any]) -> str:
    """One short human-readable line per tool call, for the Ask view's
    expandable provenance trail ("Searched … · matched ›")."""
    if name == "search_people":
        bits = [f"{k}: {tool_input[k]}"
                for k in ("location", "role_type", "topic", "min_strength")
                if tool_input.get(k)]
        return ", ".join(bits) or "everyone"
    if name == "find_person":
        return f'name: {tool_input.get("name") or ""}'
    if name == "read_message":
        off = tool_input.get("offset")
        return (f"message #{tool_input.get('id')}"
                + (f" from character {off}" if off else " in full"))
    if name == "semantic_search":
        # Every phrasing, because the trail is the user's only account of what
        # was actually looked for — one of three shown as if it were the whole
        # search is the kind of half-truth NC-076 is about.
        terms = ([tool_input["query"]] if tool_input.get("query") else []) \
            + list(tool_input.get("queries") or [])
        return " | ".join(str(t) for t in terms if t)
    if name == "get_contact":
        return f"contact #{tool_input.get('id')}"
    if name == "search_conversations":
        cid = tool_input.get("contact_id")
        bits = [str(tool_input.get("query") or ""),
                f"mode: {tool_input.get('mode') or 'semantic'}",
                f"contact #{cid}" if cid else ""]
        return " · ".join(b for b in bits if b)
    if name == "mutual_connections":
        cid = tool_input.get("contact_id")
        return f"contact #{cid}" if cid else str(tool_input.get("name") or "")
    if name == "message_stats":
        return "what the message archive holds"
    if name == "ask_the_user":
        # NC-156. The question itself, because that IS what happened this turn —
        # and the trail is the only place a reload can show it if the question
        # was refused mid-round rather than ending the turn.
        return str(tool_input.get("question") or "a question")
    if name in ("search_messages", "people_in_messages"):
        # Every filter, spelled out. This line is the user's only way to see WHY
        # a number came back — "42 matched" with no dates attached is not
        # provenance, and a date the model got wrong is invisible without it.
        cid = tool_input.get("contact_id")
        since = str(tool_input.get("since") or "")
        until = str(tool_input.get("until") or "")
        _terms_in = ([tool_input["query"]] if tool_input.get("query") else []) \
            + list(tool_input.get("queries") or [])
        bits = [" | ".join(f'"{t}"' for t in _terms_in) if _terms_in else "",
                (f"{since} to {until}" if since and until
                 else f"since {since}" if since
                 else f"up to {until}" if until else ""),
                str(tool_input.get("direction") or ""),
                str(tool_input.get("channel") or ""),
                f"contact #{cid}" if cid else "",
                "incl. group chats" if tool_input.get("include_groups") else "",
                (f'context {tool_input["context"]}'
                 if name == "search_messages" and tool_input.get("context") is not None
                 else "")]
        return " · ".join(b for b in bits if b) or "everything"
    return ""


def _tool_matched(result: Dict[str, Any]) -> int:
    """How many things a tool result surfaced.

    Every list a tool can return has to be named here. The message tools return
    `people` and `messages`, and a key missing from this line makes the trail
    report "0 matched" for a search that found results — a provenance receipt
    claiming the search found nothing, next to an answer built from what it
    found. Anything added to run_tool needs its list key added here.

    The isinstance check is not defensive padding. This was an `or` chain, and a
    tool that returned a COUNT under one of these names crashed the whole answer
    with `len() of int` — one tool's key name taking down every question. A trail
    line is worth zero answers, so it cannot be able to raise.
    """
    # Order is load-bearing. search_messages returns both `messages` and the
    # `people` behind them, and it is a message search — "N people matched" for a
    # search that found more messages than people is the wrong number. Nothing
    # returns `messages` and means people, so messages wins wherever both exist.
    for key in ("contacts", "conversations", "messages", "people", "mutual_names"):
        val = result.get(key)
        if isinstance(val, list) and val:
            return len(val)
    return 0


def _tool_cards(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The contact summaries one tool result contributes to the answer's cards.

    Both key names, because the message tools return people under `people` while
    everything older uses `contacts`. Reading `contacts` only would mean an
    answer built entirely from the message tools names many people and renders
    no cards at all — the names are right and not one of them is clickable. Any new tool that returns people has to appear here, and the
    isinstance guard is for the same reason it is in _tool_matched: a card list
    must never be able to raise inside the answer loop.
    """
    out: List[Dict[str, Any]] = []
    for key in ("contacts", "people"):
        val = result.get(key)
        if isinstance(val, list):
            out += [c for c in val if isinstance(c, dict) and "id" in c]
    return out


def _tool_total(result: Dict[str, Any], matched: int) -> int:
    """How many the search actually matched, before the per-call cap.

    `matched` counts the capped list, so on its own the trail would say "matched
    30" for a search that matched 400. _search_people knows the real number, so
    the provenance line carries it too. With Quick that gap widens (a 30-result
    cap against a 120-result one), so the trail carries both and the UI can say
    which it is showing. Falls back to `matched` for the tools that have no larger
    number to report."""
    for key in ("total_matched", "total_people"):
        total = result.get(key)
        if isinstance(total, int) and total > matched:
            return total
    return matched


def _as_int(value: Any) -> Any:
    """A tool argument the schema calls an integer, as one — or None. NC-168.

    Absent and empty both mean zero, which is what every call site here already
    assumed. Anything else that will not convert returns None, and the caller
    turns that into a tool error the model can read and fix.

    It exists because a bare int() raises, and a raise here does not stay here:
    it leaves run_tool, leaves chat(), and lands in handle_chat's deliberately
    broad except, which discards the whole answer — every round already billed —
    and falls back to keyword search with the Python message stapled on. One
    mistyped argument should cost one tool call, not the answer.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):        # True is not 1 in an argument the model wrote
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run_tool(conn, name: str, tool_input: Dict[str, Any],
             caps: Dict[str, int] = None,
             may_ask: bool = True) -> Dict[str, Any]:
    """Execute one tool call.

    `caps` is this request's shape_caps() — optional, so an existing two- or
    three-argument caller keeps working and gets the configured defaults. Only the
    two people-search tools take a cap; nothing here decides WHICH people match,
    only how many of the ranked list one call hands back, and both report enough
    for the caller to see what the cap hid.

    `may_ask` is NC-156: whether ask_the_user is allowed this turn. It is a
    separate argument and NOT a key in `caps` on purpose — caps is a set of
    numbers the caller echoes back to the client as what ran, and a permission
    flag is neither a number nor the client's business.
    """
    caps = caps or {}
    if name == "read_message":
        return _read_message(conn, tool_input.get("id"), tool_input.get("offset"))
    if name == "find_person":
        return _find_person(conn, str(tool_input.get("name") or ""),
                            limit=int(caps.get("search_people_limit") or 0))
    if name == "search_people":
        # NC-168: guarded, like _get_contact and _read_message already guard
        # theirs. The model is asked for a number and can write a word —
        # min_strength: "high" is the obvious one — and a bare int() then raises
        # out of run_tool, out of chat(), and into handle_chat's broad except,
        # which throws away EVERY round already paid for and answers from
        # keyword search with the raw Python error stapled to it.
        strength = _as_int(tool_input.get("min_strength"))
        if strength is None:
            return {"error": "min_strength must be a whole number from 0 to 100"}
        return _search_people(
            conn,
            location=str(tool_input.get("location") or ""),
            role_type=str(tool_input.get("role_type") or ""),
            topic=str(tool_input.get("topic") or ""),
            min_strength=strength,
            limit=int(caps.get("search_people_limit") or 0),
        )
    if name == "semantic_search":
        return _semantic_search(conn, str(tool_input.get("query") or ""),
                                limit=int(caps.get("semantic_limit") or 0),
                                queries=tool_input.get("queries") or [])
    if name == "get_contact":
        return _get_contact(conn, tool_input.get("id"))
    if name == "search_conversations":
        cid = _as_int(tool_input.get("contact_id"))     # NC-168
        if cid is None:
            return {"error": "contact_id must be a whole number"}
        return _search_conversations(conn, str(tool_input.get("query") or ""),
                                     str(tool_input.get("mode") or "semantic"),
                                     cid)
    if name == "message_stats":
        return _message_stats(conn)
    if name == "search_messages":
        cid = _as_int(tool_input.get("contact_id"))     # NC-168
        span = (_CTX_SPAN_DEFAULT if tool_input.get("context") is None
                else _as_int(tool_input.get("context")))
        if cid is None or span is None:
            return {"error": "contact_id and context must be whole numbers"}
        return _search_messages(
            conn,
            query=str(tool_input.get("query") or ""),
            queries=tool_input.get("queries") or [],
            since=str(tool_input.get("since") or ""),
            until=str(tool_input.get("until") or ""),
            direction=str(tool_input.get("direction") or "").strip().lower(),
            channel=str(tool_input.get("channel") or "").strip().lower(),
            contact_id=cid,
            # NC-100: absent means TRUE. bool(.get(...)) made a missing key
            # False, so the schema default and the function default disagreed and
            # the function default never applied through the tool path.
            include_groups=bool(tool_input.get("include_groups", True)),
            limit=int(caps.get("message_limit") or 0),
            # Absent means the default span, not zero — the same trap
            # include_groups fell into above: int(.get(...) or 0) would turn a
            # missing key into "no context", so the schema default and the
            # function default would disagree and the feature would be off
            # through the only path that matters. Worked out above so the guard
            # and the default live in one place (NC-168).
            context=span,
        )
    if name == "people_in_messages":
        return _people_in_messages(
            conn,
            query=str(tool_input.get("query") or ""),
            queries=tool_input.get("queries") or [],
            since=str(tool_input.get("since") or ""),
            until=str(tool_input.get("until") or ""),
            direction=str(tool_input.get("direction") or "").strip().lower(),
            channel=str(tool_input.get("channel") or "").strip().lower(),
            # NC-100: absent means TRUE. bool(.get(...)) made a missing key
            # False, so the schema default and the function default disagreed and
            # the function default never applied through the tool path.
            include_groups=bool(tool_input.get("include_groups", True)),
            limit=int(caps.get("message_people_limit") or 0),
        )
    if name == "mutual_connections":
        return _mutual_connections(conn, tool_input.get("contact_id"),
                                   str(tool_input.get("name") or ""))
    if name == "ask_the_user":
        # NC-156. Reached ONLY when the question does not end the turn, which is
        # two cases and both are errors the model can fix in the round it is
        # already paying for:
        #
        #   may_ask=False — it asked last turn and may not ask again. Refused
        #   HERE rather than by withholding the tool, because TOOLS is in the
        #   cached prefix and dropping an entry re-prices the whole conversation
        #   (see the section comment above normalize_question).
        #
        #   the input is unusable — no question, or fewer than two distinct
        #   options. A question with one button is not a choice.
        if not may_ask:
            return {"error": "You already asked a question last turn. Answer "
                             "with what you have, and say which reading you "
                             "took."}
        return {"error": f"ask_the_user needs a question and {ASK_MIN_OPTIONS} "
                         f"to {ASK_MAX_OPTIONS} distinct, non-empty options. "
                         "Either call it again with them, or answer the "
                         "question you were given."}
    return {"error": f"unknown tool: {name}"}


# Price per million tokens for the model in ANTHROPIC_MODEL. Overridable because
# these are Anthropic's list prices at the time of writing, and a stale constant
# quietly reporting the wrong number is worse than reporting none.
# ── Stopping an answer that is already running ────────────────────────────────
#
# NC-154. A running answer ("Searching your network 4s") needs something on
# screen to press to stop it.
#
# THE TRAP, and it is why this is not four lines in the browser. static/app/api.js
# already builds an abort handle for every call, so cancelling the fetch is one
# line — and it would stop NOTHING. The POST to /api/chat is one synchronous
# request; the loop below would keep running, keep calling Anthropic and keep
# BILLING, with the spinner gone and nobody watching. A stop that only hides the
# answer is worse than no stop at all, because the money still goes out and now
# it goes out silently. So the browser keeps its request open and sends a second,
# tiny one that sets a flag this loop reads.
#
# In memory on purpose, and the precedent is api_instance's _semantic_lock: a
# restart clears the table, which is correct, because no in-flight answer
# survives a restart either. Nothing has to sweep it — begin/end are the two
# halves of one try/finally, so whatever created an entry removes it on success,
# on exception and on the stop path alike. No timer, no expiry, no leak.
#
# Threading is what makes it possible at all: app.py serves on a
# ThreadingHTTPServer, so the stop request is handled while the answer is still
# being written.
_RUNS_LOCK = threading.Lock()
_RUNS: Dict[str, Dict[str, Any]] = {}

# A run id comes from a request body, so it is checked BEFORE it becomes a
# dictionary key — an unvalidated one is an unbounded-memory path with a public
# door on it.
_RUN_ID_OK = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def begin_run(run_id: Any) -> str:
    """Register a cancellable run. Returns the id, or "" if it cannot be used.

    "" is not an error: the answer then runs exactly as it always has and simply
    cannot be stopped. A malformed id must never cost somebody their answer —
    that is invariant 8's temperament applied to a control rather than a key.
    """
    rid = str(run_id or "")
    if not _RUN_ID_OK.match(rid):
        return ""
    with _RUNS_LOCK:
        if rid in _RUNS:
            return ""            # already live; two tabs cannot share one id
        _RUNS[rid] = {"stop": False}
    return rid


def end_run(run_id: str) -> None:
    """Forget a run. Safe to call twice, and called from a finally."""
    if not run_id:
        return
    with _RUNS_LOCK:
        _RUNS.pop(run_id, None)


def request_stop(run_id: Any) -> str:
    """Ask a running answer to stop. "stopping" or "unknown"."""
    rid = str(run_id or "")
    with _RUNS_LOCK:
        run = _RUNS.get(rid)
        if run is None:
            return "unknown"
        run["stop"] = True
    return "stopping"


def stop_requested(run_id: str) -> bool:
    if not run_id:
        return False
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
        return bool(run and run["stop"])


def live_runs() -> int:
    """How many answers are in flight. For the diagnostics page and the tests —
    a registry that leaks is invisible without a way to count it."""
    with _RUNS_LOCK:
        return len(_RUNS)


# NC-195. The list prices this app costs its own answers with, in US dollars per
# million tokens. They were module constants, read once at start, so a corrected
# price did nothing until a restart — and no screen said which price a cost had
# assumed, so a stale one was invisible. Now read at call time and reported with
# every spend. Same keys as api_intake._price, so there is one place to fix them.
#
# NC-327. These are Opus 5.5's list prices — the model MODEL defaults to — and
# they are one pair for whatever model is set, not a row per model. Opus 5 was 5
# in and 25 out; Opus 5.5 is 4 and 20 (Anthropic's pricing page).
PRICE_IN_DEFAULT = 4.0
PRICE_OUT_DEFAULT = 20.0
PRICE_MAX = 1000.0   # a typo'd price must not turn every cost into nonsense


def _price(key: str, default: float) -> float:
    try:
        value = float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= PRICE_MAX else default


def price_in() -> float:
    return _price("NC_PRICE_IN_PER_MTOK", PRICE_IN_DEFAULT)


def price_out() -> float:
    return _price("NC_PRICE_OUT_PER_MTOK", PRICE_OUT_DEFAULT)


class _Spend:
    """What one Ask question actually cost.

    Nothing read resp.usage before, so the price of an answer was invisible —
    which matters now that a single roster question can run twelve rounds and
    re-send every tool result each time. Cache reads bill at about a tenth of
    input, cache writes at about 1.25x, so they are counted separately rather
    than lumped into one 'input' number that would overstate the bill."""

    def __init__(self) -> None:
        self.rounds = 0
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0

    def add(self, resp: Any) -> None:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        self.rounds += 1
        self.input += getattr(u, "input_tokens", 0) or 0
        self.output += getattr(u, "output_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

    def report(self) -> Dict[str, Any]:
        billed_in = self.input + self.cache_write * 1.25 + self.cache_read * 0.1
        p_in, p_out = price_in(), price_out()
        usd = (billed_in * p_in + self.output * p_out) / 1_000_000
        return {
            "rounds": self.rounds,
            "input_tokens": self.input,
            "output_tokens": self.output,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
            "usd": round(usd, 4),
            # The prices behind `usd`, so the screen can say what it assumed.
            "price_in_per_mtok": p_in,
            "price_out_per_mtok": p_out,
            "model": MODEL,
        }


def _stopped(collected: Dict[int, Dict[str, Any]], trail: List[Dict[str, Any]],
             spend: Any, sh: Dict[str, str], caps: Dict[str, int]) -> Dict[str, Any]:
    """What a stopped answer hands back. NC-154.

    Modelled on the out-of-rounds return at the bottom of chat(), because it is
    the same situation: the searches really happened and the people they found
    are most of the value even with no prose. Throwing that away would make Stop
    a button that wastes what you already paid for.

    `truncated` is True for the reason that flag exists — it is what tells
    api_ask to keep every card rather than filtering them against prose that was
    never written. Without it `_cards_for_reply` matches names against an empty
    string, and its `return named or contacts` fallback then ships the entire
    unfiltered search result, which is the opposite of what a stopped answer
    should show.

    The spend is reported, not suppressed. An answer stopped after four rounds
    cost what four rounds cost, and hiding that recreates exactly the
    invisibility NC-155 was about.
    """
    found = list(collected.values())
    note = ("Stopped." if not found else
            f"Stopped. The {len(found)} "
            f"{'person' if len(found) == 1 else 'people'} found so far "
            "are below.")
    return {"reply": note, "contacts": found, "trail": trail,
            "truncated": True, "spend": spend.report(),
            "shape": sh, "caps": caps, "stopped": True}


def chat(conn, messages: List[Dict[str, Any]],
         shape: Any = None, run_id: str = "",
         may_ask: bool = True) -> Dict[str, Any]:
    """Runs the Anthropic tool-use loop and returns {reply, contacts, trail}.

    `messages` is the running conversation as plain {role, content} dicts
    (content as a string for user turns). `trail` is one {tool, detail,
    matched, total} entry per tool call made — the Ask view's provenance receipt.
    Raises if ANTHROPIC_API_KEY is unset or the anthropic package isn't
    installed — app.py is responsible for falling back to handle_ask in that
    case, not this function.

    `shape` is the answer-shape control set (see the section at the top of this
    module). It is optional and normalizes to SHAPE_DEFAULT, which builds the
    identical request this function made before the controls existed, so any
    caller that passes only (conn, messages) keeps working unchanged.
    The normalized shape and the numbers it produced are returned as
    `shape` and `caps` so the caller echoes what ran instead of recomputing it.

    `run_id` is NC-154: the handle a second request uses to stop this one. Empty
    means uncancellable, which is how every existing caller behaves and is a
    fine answer — the CLI and the tests pass nothing. Registration belongs to the
    CALLER (api_ask.handle_chat), not here, because the window that matters is
    between the browser sending the question and this function reaching its first
    round, and only the caller spans it.

    `may_ask` is NC-156: whether this turn is allowed to come back with a
    question instead of an answer. The caller decides, because the rule is about
    the CONVERSATION — you may not ask twice running — and this function is only
    ever shown a window onto it.
    """
    import anthropic  # imported lazily so the app boots fine without the package

    sh = normalize_shape(shape)
    # One read per request, at request time — see shape_caps(). Read here rather
    # than inside the loop so every round of one answer runs on the same numbers.
    caps = shape_caps(sh)
    system = _system_blocks(shape_directive(sh), owner.context_block(conn))

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    convo: List[Dict[str, Any]] = list(messages)
    collected: Dict[int, Dict[str, Any]] = {}
    trail: List[Dict[str, Any]] = []
    spend = _Spend()
    refused_asks = 0        # NC-163, see ASK_REFUSALS_MAX

    for _ in range(caps["tool_rounds"]):
        # NC-154. THE check that matters, and it is here rather than after the
        # call because this is where the money is. Every round re-sends the whole
        # conversation — the cache-marker work below exists for exactly that — so
        # by round eight the round that has not started yet is the single most
        # expensive thing left to prevent. Checking after the response has
        # arrived would prevent nothing; that round is already paid for.
        if stop_requested(run_id):
            return _stopped(collected, trail, spend, sh, caps)
        # NC-080, and the reason this is a guard and not just a fixed call site.
        # Exceeding Anthropic's four-marker limit rejects the WHOLE request, so
        # the failure mode is "Ask stops working" — and because invariant 8
        # catches it and answers from the rule-based engine, nothing looks broken.
        # That is how a failure like that goes unnoticed: a 400 on every
        # multi-round question, presenting as slightly stupid answers.
        #
        # Counted across both halves of the request, because they are built in
        # different functions and neither can see the other's markers. Trimming
        # the OLDEST is right: caching is prefix-based, so the newest marker is
        # the valuable one. Never raises — degrading a cache hit is not worth
        # losing an answer over, which is the mistake one level up.
        over = _count_cache_markers(system) + _count_cache_markers_in(convo) - MAX_CACHE_BLOCKS
        if over > 0:
            _trim_oldest_cache_markers(convo, over)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=caps["max_tokens"],
            system=system,
            tools=TOOLS,
            messages=convo,
        )
        spend.add(resp)
        if resp.stop_reason != "tool_use":
            reply = "".join(b.text for b in resp.content if b.type == "text").strip()
            # stop_reason "max_tokens" means the answer stopped mid-sentence. It
            # used to be returned as if it were finished, so a roster question
            # produced "…7. Ada Lovelace — Co-founder" and simply ended: the
            # promised list broke off part-way with nothing saying so. Say so.
            # Detail=brief lowers max_tokens, so it makes this MORE likely — which
            # is why the flag had to already be honest before that control existed.
            truncated = resp.stop_reason == "max_tokens"
            return {"reply": reply or "I didn't find anything for that.",
                    "contacts": list(collected.values()), "trail": trail,
                    "truncated": truncated, "spend": spend.report(),
                    "shape": sh, "caps": caps}

        # NC-156. The turn ends with a QUESTION rather than an answer.
        #
        # ABOVE the convo.append below, and that placement is the whole trick.
        # An assistant turn carrying a tool_use block with no matching
        # tool_result is rejected outright by the API, so a question appended
        # here would poison the conversation for every later round. We return
        # instead: `convo` is a local, nothing is ever sent again, and the
        # half-used round dies with the function.
        #
        # Checked before any tool in this round RUNS, not after. A round can
        # carry several calls and semantic_search loads a sentence-transformer
        # model — seconds of CPU whose results we would then throw away, because
        # a question turn returns no cards at all (see below).
        if may_ask:
            asked = next((normalize_question(b.input or {})
                          for b in resp.content
                          if b.type == "tool_use" and b.name == "ask_the_user"),
                         None)
            if asked:
                lead = "".join(b.text for b in resp.content
                               if b.type == "text").strip()
                trail.append({"tool": "ask_the_user",
                              "detail": asked["question"],
                              "matched": 0, "total": 0,
                              # message_stats' problem: "0 matched" reads as a
                              # failed search, and nothing was searched here.
                              "label": f"{len(asked['options'])} options offered"})
                return {
                    "reply": f"{lead}\n\n{asked['question']}" if lead
                             else asked["question"],
                    "question": asked,
                    # NO CARDS under a question, and this is not tidiness.
                    # api_ask matches names in the prose against what the
                    # searches found, and falls back to `named or contacts` —
                    # everyone — when the prose names nobody. A question names
                    # nobody by construction, so shipping the collected people
                    # here would put the entire unfiltered search result under
                    # "investors or customers?" as though it were the answer.
                    "contacts": [],
                    "trail": trail,
                    "truncated": False,
                    "spend": spend.report(),
                    "shape": sh, "caps": caps,
                }
        convo.append({"role": "assistant", "content": resp.content})
        tool_results = []
        # NC-163. Whether this round did nothing except try to ask, which is the
        # shape of the groove: the model is refused, re-reads the same
        # conversation, and asks again.
        asks_only = True
        for block in resp.content:
            if block.type != "tool_use":
                continue
            # NC-154. One round can carry several tool calls, and semantic_search
            # loads a sentence-transformer model — seconds, on the CPU, with the
            # spinner already saying "stopping". Leaving the loop here without
            # sending a tool_result for every tool_use looks like a protocol
            # violation and is not: we are RETURNING, not sending `convo` again,
            # so the half-answered round is never submitted to anything.
            if stop_requested(run_id):
                return _stopped(collected, trail, spend, sh, caps)
            if block.name != "ask_the_user":
                asks_only = False          # NC-163: real work happened this round
            result = run_tool(conn, block.name, block.input or {}, caps,
                              may_ask=may_ask)
            matched = _tool_matched(result)
            entry = {"tool": block.name,
                     "detail": _tool_detail(block.name, block.input or {}),
                     "matched": matched,
                     "total": _tool_total(result, matched)}
            # message_stats matches nothing — it reports what exists. "0 matched"
            # in the trail would read as a failed search, so it says what it
            # actually found instead. Optional, so every other tool is unchanged.
            if block.name == "message_stats" and result.get("archive_present"):
                entry["label"] = (
                    f"{result.get('total_messages', 0):,} messages · "
                    f"{len(result.get('channels') or [])} channels · "
                    f"{result.get('first_message', '')} to "
                    f"{result.get('last_message', '')}")
            trail.append(entry)
            for c in _tool_cards(result):
                collected[c["id"]] = c
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })
        # Cache the conversation so far, not just the system prompt. Every round
        # re-sends the WHOLE conversation, and a search now returns up to 120
        # contact summaries (several thousand tokens) — so by round 12 the same
        # tool results have been paid for at full price a dozen times over.
        # Marking the last
        # block means the next round reads that prefix at roughly a tenth of the
        # price. Caching only the 932-token system prompt, as this did, saved
        # almost nothing: the growing part was the whole cost.
        if tool_results:
            # NC-080. Clear before marking, or this line adds a marker per round
            # to a conversation that is re-sent whole — five by round four, and a
            # 400 that kills the entire answer. See _clear_cache_markers.
            _clear_cache_markers(convo)
            tool_results[-1]["cache_control"] = {"type": "ephemeral"}
        convo.append({"role": "user", "content": tool_results})
        # NC-163. Give up on a model that only ever asks, rather than letting it
        # spend the round budget discovering that it may not. The message has to
        # be its own, and not the out-of-rounds one below: "this question needs
        # too many steps, split it into two" is the wrong instruction for a
        # question that was perfectly clear and never got searched.
        refused_asks = refused_asks + 1 if (tool_results and asks_only) else 0
        if refused_asks >= ASK_REFUSALS_MAX:
            return {
                "reply": "I kept trying to ask you a question instead of "
                         "answering, so I stopped.\n\nThe people found so far "
                         "are below.",
                "contacts": list(collected.values()),
                "trail": trail,
                "truncated": True,
                "spend": spend.report(),
                "shape": sh, "caps": caps,
            }

    # Out of rounds. The searches still happened, so hand back what they found
    # rather than only an apology — on a roster question the collected people are
    # most of the value even without the prose.
    #
    # The round budget is now named, because Search=quick cuts it to 3 and hitting
    # a 3-round ceiling is a different problem from asking too broad a question:
    # "ask something narrower" would be the wrong instruction for it. The first
    # line is kept byte-identical on purpose — static/app/ask.js rewrites it into
    # plain-English microcopy with a `^`-anchored, single-line regex
    # (SERVER_COPY), so that line must not change. The number goes on its own
    # line, where the rewrite cannot swallow it.
    note = (f"I stopped after {caps['tool_rounds']} search rounds. "
            "The people found so far are below.")
    if sh["search"] == "quick":
        note += " Quick search uses fewer rounds. Select Thorough to search further."
    return {
        "reply": "That question needed too many steps — try asking something narrower."
                 f"\n\n{note}",
        "contacts": list(collected.values()),
        "trail": trail,
        "truncated": True,
        "spend": spend.report(),
        "shape": sh,
        "caps": caps,
    }
