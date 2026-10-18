#!/usr/bin/env python3
"""The Ask surface: the rule-based query engine and the LLM chat path.

Flat sibling of app.py (see crm/api_core.py for why the imports look like this).

Both halves live here on purpose. Rule 8 in docs/EDITING.md says the app must answer
with zero API keys: handle_chat falls back to _ask_fallback, which calls
handle_ask, and all three are in this file — so the fallback can never be broken
by a change on the other side of a module boundary.
"""

import json
import logging
import os
import re
from http import HTTPStatus
from typing import Any, Dict, List

import config  # noqa: F401 — loads secrets/.env before anything reads os.environ
import archive as arc
import chat  # the LLM tool-use brain (anthropic imported lazily inside it)
import db
import figures  # NC-087: the one spelling of a contact-scope filter
from api_core import ApiError
from api_guards import _require_conversation
from enrich_facts import _match_role_type  # lightweight helper

# Where an Ask question that fell back to keyword search leaves its reason. The
# chat reply carries the same words for the person reading that one answer; this
# is the copy that is still there tomorrow, so a passing network hiccup can be
# told apart from something that keeps happening (NC-300).
log = logging.getLogger("networkcrm")


# ── Ask / event-invite engine ─────────────────────────────────────────────────

# Metro name aliases for query parsing (lowercase key → canonical)
_METRO_ALIASES: Dict[str, str] = {
    "san francisco": "San Francisco", "sf": "San Francisco",
    "bay area": "San Francisco", "silicon valley": "San Francisco",
    "palo alto": "San Francisco", "south bay": "San Francisco",
    "new york": "New York", "nyc": "New York", "ny": "New York",
    "manhattan": "New York", "brooklyn": "New York",
    "los angeles": "Los Angeles", "la": "Los Angeles",
    "london": "London", "stockholm": "Stockholm",
    "berlin": "Berlin", "paris": "Paris", "amsterdam": "Amsterdam",
    "singapore": "Singapore", "hong kong": "Hong Kong",
    "chicago": "Chicago", "austin": "Austin", "seattle": "Seattle",
    "boston": "Boston", "miami": "Miami", "denver": "Denver",
    "atlanta": "Atlanta", "washington dc": "Washington DC",
    "dc": "Washington DC", "dallas": "Dallas", "houston": "Houston",
    "phoenix": "Phoenix", "portland": "Portland",
}

# Theme words → which role_types are a good match
_THEME_ROLES: Dict[str, List[str]] = {
    "tech": ["tech", "founder", "product", "design", "research"],
    "ai": ["tech", "founder", "research", "product"],
    "startup": ["founder", "investor", "tech", "product", "sales"],
    "venture": ["investor", "founder"],
    "vc": ["investor", "founder"],
    "invest": ["investor", "founder", "finance"],
    "finance": ["finance", "investor", "founder"],
    "marketing": ["marketing", "sales", "founder"],
    "design": ["design", "product", "founder"],
    "product": ["product", "tech", "founder", "design"],
    "sales": ["sales", "marketing", "founder"],
    "health": ["medical", "research", "founder"],
    "legal": ["legal", "consultant"],
    "media": ["marketing", "creative", "consultant"],
    "creative": ["design", "marketing"],
}


def _parse_ask_query(query: str) -> Dict[str, Any]:
    """Extract location and themes from a free-text ask query."""
    q = query.lower()
    # find metro
    metro = ""
    for alias, canonical in sorted(_METRO_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in q:
            metro = canonical
            break
    # find themes
    themes: List[str] = []
    for theme_kw in _THEME_ROLES:
        if theme_kw in q:
            themes.append(theme_kw)
    return {"metro": metro, "themes": themes}


def _ask_reason(row: Any, metro: str, themes: List[str]) -> str:
    """Build a human-readable reason string for why this contact is suggested."""
    bits = []
    contact_metro = (row["home_metro"] or "").strip()
    contact_city = (row["home_city"] or "").strip()
    # Only claim "Based in X" when the requested metro actually matched — the
    # no-key fallback used to caption every result with its city even when
    # location wasn't why it matched, which read as a false reason.
    if metro and contact_metro and contact_metro.lower() == metro.lower():
        bits.append(f"Based in {contact_metro}")
    elif metro and contact_city and contact_city.lower() == metro.lower():
        bits.append(f"Based in {contact_city}")
    role = (row["role_type"] or "").strip()
    if role:
        bits.append(role.capitalize())
    pos = (row["position"] or "").strip()
    company = (row["company"] or "").strip()
    if pos and company:
        bits.append(f"{pos} at {company}")
    elif pos:
        bits.append(pos)
    elif company:
        bits.append(company)
    blurb = (row["blurb"] or "").strip()
    if blurb:
        bits.append(blurb)
    return " · ".join(bits) if bits else "In your network"


# How many people the rule-based (zero-key) answer returns when the shape asks for
# "all". It is the DEFAULT behind a call-time reader, because the People control
# makes it the number the user is actually choosing against — and a ceiling
# nobody can see or move is a hidden limit. The count it cut is reported as
# total_matched either way.
ASK_FALLBACK_CAP = 30


def ask_fallback_cap() -> int:
    """The rule-based engine's page size (NC_ASK_FALLBACK_CAP), read at call time.

    Same idiom as api_import.import_timeout_sec and chat_max_cards below: read
    now, clamped, a bad value falls back to the named default. Not yet in
    settings.MANAGED_KEYS, so today it is settable through secrets/.env only. It
    still takes effect with no restart.
    """
    try:
        cap = int(os.environ.get("NC_ASK_FALLBACK_CAP", ASK_FALLBACK_CAP))
    except (TypeError, ValueError):
        cap = ASK_FALLBACK_CAP
    return max(1, min(500, cap))


def parse_shape(body: Any) -> Dict[str, str]:
    """The answer shape a request asked for, clamped SERVER-side.

    The vocabulary, the clamps and the defaults all live in chat.normalize_shape,
    so there is exactly one list of allowed values and a client cannot raise a
    ceiling by editing the JSON it posts. A missing key, a wrong type and an
    invented value all fall back to the default and never raise a 400: refusing to
    answer a question because a dropdown sent a stale word would be a worse
    failure than answering in the default shape and saying which shape ran. What
    ran is echoed in the response as `shape` (see shape_status).
    """
    raw = body.get("shape") if isinstance(body, dict) else None
    return chat.normalize_shape(raw)


def rules_people_cap(shape: Any = None) -> int:
    """How many people the rule-based engine returns for this shape.

    This is the ONE place the People control genuinely removes people, and it is
    deliberate: the zero-key engine writes a single line of prose, so there is no
    answer text for a "short list" to shorten — the result size is the only thing
    the control can mean here. It stays honest because handle_ask returns
    total_matched next to it, so "matched 400, showing 10" is on screen.

    min() against ask_fallback_cap(), so the shape can only ever ask for FEWER
    than the engine's page size. "all" therefore means "this engine's page", not
    "the whole network" — the response says so through shape.people_cap.
    """
    ceiling = ask_fallback_cap()
    want = chat.people_count(shape)      # 0 for "all"
    return min(want, ceiling) if want else ceiling


# Which layer each control acts on, per engine — and "" for a control that does
# nothing here. Invariant 8 says the app works with zero API keys. It does NOT say
# every control may silently pretend to work: Detail and Search have no meaning
# without the LLM, so they are reported unavailable WITH a reason, and the UI
# disables them and says why.
_LAYER_LLM = {"people": "prose", "detail": "prose+max_tokens", "search": "tool_caps+rounds"}
_LAYER_RULES = {"people": "result_cap", "detail": "", "search": ""}
# STE (DESIGN_DIRECTION §3): one fact per sentence, active voice, name the actor,
# and put the instruction on the control rather than in the sentence.
_NO_KEY_REASON = ("Ask answered from keyword matching, not from the AI engine. "
                  "This control needs an API key.")
_DEGRADED_REASON = ("Ask could not reach the AI engine, so it answered from "
                    "keyword matching. This control had no effect.")


def shape_status(shape: Any, engine: str, caps: Dict[str, int] = None,
                 degraded: bool = False) -> Dict[str, Any]:
    """The EFFECTIVE shape, for the response. Never let the client assume its
    request was honoured.

    `requested` is what survived the server-side clamp. `effective` is what
    actually applied, and a control that did nothing at all reads None there —
    not its default, which would be indistinguishable from "full detail ran".
    `controls[x].layer` names the layer that changed, `people_cap` is the only
    number that removes anybody (0 on the LLM path, where nothing is removed), and
    `caps` is the four numbers the LLM request ran with, or None.
    """
    sh = chat.normalize_shape(shape)
    llm = engine == "llm"
    layers = _LAYER_LLM if llm else _LAYER_RULES
    reason = "" if llm else (_DEGRADED_REASON if degraded else _NO_KEY_REASON)
    effective: Dict[str, Any] = dict(sh)
    controls: Dict[str, Any] = {}
    for name in ("people", "detail", "search"):
        available = bool(layers[name])
        if not available:
            effective[name] = None      # this control did nothing on this answer
        controls[name] = {"available": available, "layer": layers[name],
                          "reason": "" if available else reason}
    return {"requested": sh, "effective": effective, "options": chat.SHAPE_OPTIONS,
            "controls": controls,
            "people_cap": 0 if llm else rules_people_cap(sh),
            "caps": dict(caps) if caps else None}


def handle_ask(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/ask — answer 'who should I invite to my event?'

    Body: {query: "...", location: "...", themes: [...], shape: {people, ...}}
    Returns: {contacts: [{id, name, reason, score, ...}], parsed: {metro, themes},
              total_matched, shape}
    """
    query = str(body.get("query") or "").strip()
    if not query:
        raise ApiError(HTTPStatus.BAD_REQUEST, "query is required")
    shape = parse_shape(body)

    parsed = _parse_ask_query(query)
    # allow explicit overrides
    if body.get("location"):
        loc_str = str(body["location"]).lower().strip()
        parsed["metro"] = _METRO_ALIASES.get(loc_str, body["location"])
    if body.get("themes"):
        parsed["themes"] = [str(t).lower() for t in body["themes"]]

    metro = parsed["metro"]
    themes = parsed["themes"]

    # resolve which role_types are relevant
    relevant_roles: set[str] = set()
    for th in themes:
        relevant_roles.update(_THEME_ROLES.get(th, []))

    rows = conn.execute(
        # NC-145. Was 'visible'. This is the OTHER half of Ask: invariant 8 says
        # the no-key path answers in the identical shape, and which people are in
        # scope is part of that shape. It is also not opt-in — handle_chat falls
        # back here on ANY exception from chat.chat(), so a network hiccup used
        # to move the answer to an engine with a wider population than the one
        # the user had just been getting. chat.ASK_POPULATION is the one name.
        f"SELECT * FROM contacts WHERE {figures.population_where(chat.ASK_POPULATION)}"
    ).fetchall()

    scored = []
    for row in rows:
        contact_metro = (row["home_metro"] or "").strip()
        contact_city = (row["home_city"] or "").strip()
        role_type = (row["role_type"] or "").strip()

        # location score: 1.0 when the person's metro OR their city is the place
        # asked about, else 0. NC-346: this used to look at the metro alone, so
        # someone saved with the city "London" and no metro was never found by
        # "Who is in London?". _ask_reason already said "Based in <city>" for a
        # city match; the score now agrees with the sentence.
        loc_score = 0.0
        if metro:
            wanted = metro.lower()
            if (contact_metro and contact_metro.lower() == wanted) or \
                    (contact_city and contact_city.lower() == wanted):
                loc_score = 1.0

        # theme/role score
        role_score = 0.0
        if relevant_roles:
            if role_type in relevant_roles:
                role_score = 1.0
            else:
                # partial: check if position/title matches
                title_text = " ".join(filter(None, [row["position"], row["company"]]))
                matched = _match_role_type(title_text)
                if matched in relevant_roles:
                    role_score = 0.7

        # relationship score (0-1)
        strength = db.relationship_strength(row) / 100.0

        # combined score — weight changes based on whether we have a location filter
        if metro:
            # strong location filter: prefer people who are there
            if loc_score == 0.0:
                continue  # skip if they're not in the location
            score = (loc_score * 0.5) + (strength * 0.35) + (role_score * 0.15)
        else:
            # no location: rank by relevance + strength
            score = (strength * 0.5) + (role_score * 0.5)

        # must have some signal (skip zero-score contacts with no history)
        if score == 0 and strength == 0:
            continue

        scored.append((score, row))

    scored.sort(key=lambda x: -x[0])
    # This cap USED to be the whole story: the zero-key path found N people,
    # returned the best 30, and said so nowhere. With no API key a question that
    # matched 400 people rendered 30 rows with no count on screen at all — the
    # same defect as the People table's "First 500 of N", living in the
    # fallback. total_matched below is what makes it honest; the caller renders it.
    #
    # The cap now honours the People control (10 / 25 / all), clamped server-side
    # by rules_people_cap. It is the one control that genuinely shortens a result
    # here, because this engine has no prose to shorten instead — and the pair
    # (total_matched, shape.people_cap) is what keeps that visible.
    total_matched = len(scored)
    people_cap = rules_people_cap(shape)
    top = scored[:people_cap]

    contacts = []
    for score, row in top:
        try:
            topics = json.loads(row["topics"] or "[]")
        except Exception:
            topics = []
        contacts.append({
            "id": row["id"],
            "name": row["name"],
            "company": row["company"] or "",
            "position": row["position"] or "",
            "home_city": row["home_city"] or "",
            "home_metro": row["home_metro"] or "",
            "role_type": row["role_type"] or "",
            "topics": topics,
            "blurb": row["blurb"] or "",
            "strength": db.relationship_strength(row),
            "strength_label": db.strength_label(db.relationship_strength(row)),
            "last": db.row_last_seen(row),
            "reason": _ask_reason(row, metro, themes),
            "score": round(score, 3),
        })

    # total_matched is NOT len(contacts) — it is how many the scan matched before
    # the People cap. Equal when nothing was cut, which is how the UI knows to
    # stay quiet.
    return {"contacts": contacts, "parsed": parsed, "query": query,
            "total_matched": total_matched,
            "shape": shape_status(shape, "rules")}


# ── Ask / chat ────────────────────────────────────────────────────────────────
# ONE constant used to serve TWO unrelated purposes, and splitting Ask into chats
# is what made that untenable.
#
# CHAT_HISTORY_CONTEXT = 20 was simultaneously (a) how many turns get replayed to
# the model as context and (b) how many turns the UI renders. Those numbers want
# to be nothing like each other. (a) is a cost and latency decision — every turn
# is re-sent and re-billed on every question. (b) is a scrollback decision, and
# the correct value is "the whole thread". While Ask was one endless conversation
# the conflation was merely wasteful; with chats it is a bug you can see, because
# a 30-turn thread you open would render as its last 20 turns with the earlier
# ten simply gone, and no number on screen to say so.
#
# So there are two constants and two call-time readers, and the two callers pass
# different ones to db.get_chat_history. handle_chat reports what it fed the model
# (`context`) and handle_chat_history_get reports what it trimmed (`trimmed`), so
# neither narrowing is silent.
CHAT_HISTORY_CONTEXT = 20        # turns replayed to the LLM per question — a cost knob
CHAT_TRANSCRIPT_MAX = 500        # turns the UI renders for one chat — a scrollback knob


def chat_history_context() -> int:
    """Turns of this chat replayed to the model (NC_CHAT_HISTORY_CONTEXT).

    Read at call time, clamped, bad value falls back to the named default — the
    api_import.import_timeout_sec idiom. This is the number that costs money: each
    turn in the window is re-sent with every question, so raising it buys memory
    and pays per answer for it. Kept small on purpose.

    ALWAYS ODD, and that is arithmetic rather than taste (NC-165). handle_chat
    writes the new question BEFORE reading this window, so a chat that has
    outgrown the window always holds an odd number of turns, alternating
    question, answer, question. Take an EVEN number off the end of an odd list
    and it begins on an answer — and the API requires the first message to be a
    question, so _llm_messages drops it. At the old default of 20 that happened
    on every chat from its 11th question onward: the window was 20 rows wide,
    19 of them reached the model, and nothing anywhere said so. An odd number
    makes the window start on a question by construction, which buys back the
    turn and turns _llm_messages' drop into the safety net it was written as.
    """
    try:
        n = int(os.environ.get("NC_CHAT_HISTORY_CONTEXT", CHAT_HISTORY_CONTEXT))
    except (TypeError, ValueError):
        n = CHAT_HISTORY_CONTEXT
    n = max(1, min(200, n))
    return n if n % 2 else n - 1


def chat_transcript_max() -> int:
    """Turns of one chat the UI renders (NC_CHAT_TRANSCRIPT_MAX).

    Deliberately an order of magnitude above chat_history_context(): this one costs
    nothing but a JSON payload, and a thread you can only see the tail of is not a
    thread. It is still a cap rather than "everything", because one payload holds
    every persisted contact card too — and it is reported, so a trimmed transcript
    says how many turns it left behind instead of just looking short.
    """
    try:
        n = int(os.environ.get("NC_CHAT_TRANSCRIPT_MAX", CHAT_TRANSCRIPT_MAX))
    except (TypeError, ValueError):
        n = CHAT_TRANSCRIPT_MAX
    return max(1, min(5000, n))


# How many contact cards one answer may carry. Generous because a roster answer
# ("50 people who work in logistics") is mostly cards, and each card is the only
# handle on that person — tick it, open it, add it to a list.
CHAT_MAX_CARDS_DEFAULT = 80


def chat_max_cards() -> int:
    """How many contact cards an Ask answer may render (NC_CHAT_MAX_CARDS).

    Read at call time, not at import: NC-003 took this out of the in-app
    config screen as pure tuning plumbing, so it is a plain env var now — but
    it still applies live to a value hand-edited into secrets/.env, with no
    restart needed. Captured once at import, that edit would silently do
    nothing until restart.
    """
    try:
        cards = int(os.environ.get("NC_CHAT_MAX_CARDS", CHAT_MAX_CARDS_DEFAULT))
    except (TypeError, ValueError):
        cards = CHAT_MAX_CARDS_DEFAULT
    return max(1, min(500, cards))


def _ask_fallback(conn: Any, message: str, shape: Any = None) -> Dict[str, Any]:
    """Rule-based reply used when ANTHROPIC_API_KEY isn't set, or the API call
    fails — same {reply, contacts} shape as the LLM path, so the frontend
    doesn't need to know which one answered.

    `shape` is forwarded so the People control works with zero API keys
    (invariant 8). Detail and Search reach nothing here, which is why the caller
    reports them unavailable rather than accepting them silently."""
    result = handle_ask(conn, {"query": message, "shape": shape})
    parsed = result.get("parsed", {})
    metro, themes = parsed.get("metro", ""), parsed.get("themes", [])
    contacts = result.get("contacts", [])
    if not contacts:
        reply = "I couldn't find anyone matching that in your network yet."
    else:
        bits = []
        if metro:
            bits.append(f"in {metro}")
        if themes:
            bits.append("around " + ", ".join(themes))
        where = " ".join(bits)
        reply = f"Here's who I found {where}:".replace("  ", " ") if where else "Here's who I found:"
    # total_matched has to be forwarded, not dropped. This function returns the
    # {reply, contacts} shape the frontend expects from either engine, and it used
    # to return EXACTLY that — which silently discarded handle_ask's count and
    # left the zero-key path unable to say "matched 400, showing 30". The extra
    # key is additive, so the shape contract still holds.
    return {"reply": reply, "contacts": contacts,
            "total_matched": result.get("total_matched", len(contacts))}


def _chat_searched(conn: Any) -> Dict[str, int]:
    """Provenance totals for the Ask view's "Searched N contacts · M
    conversations · K messages" line: active (non-excluded) contacts,
    conversation chunks, and individual messages in archive.db (0 when nothing's
    been imported). Cheap COUNTs — milliseconds even on a large archive, against a
    request that takes ~30 seconds.

    `messages` is here because NC-081 gave Ask tools that read the message rows
    directly, and this line is the only place the app says what Ask can reach.
    Naming only the conversation chunks while the tools search every message
    understates the reachable archive several-fold.

    NC-301: both archive counts go through the filter the tools search through,
    so a person you hid or deleted is left out here exactly as they are left out
    of every search. Two bare COUNT(*)s made the line claim Ask had read their
    messages, and counted media-only messages no text search can reach."""
    contacts = conn.execute(
        # The "Searched N contacts" line above every answer. It is the app's own
        # claim about what Ask just read, so it counts the population Ask reads
        # (NC-145) — a claim that overstates its reach by even one person is the
        # kind of quiet wrongness this whole figures.py family exists to end.
        f"SELECT COUNT(*) AS n FROM contacts "
        f"WHERE {figures.population_where(chat.ASK_POPULATION)}"
    ).fetchone()["n"] or 0
    conversations = messages = 0
    with arc.open_if_present() as arc_conn:
        if arc_conn is not None:
            hidden = sorted(chat._excluded_ids(conn))
            m_where, m_params = chat._msg_filters("", "", "", "", "", 0, True, hidden)
            c_where = ("contact_id NOT IN (%s)" % ",".join("?" * len(hidden))
                       if hidden else "1")
            for key, sql, params in (
                    ("conversations",
                     f"SELECT COUNT(*) AS n FROM chunks WHERE {c_where}", hidden),
                    ("messages",
                     f"SELECT COUNT(*) AS n FROM messages m WHERE {m_where}", m_params)):
                try:
                    n = arc_conn.execute(sql, params).fetchone()["n"] or 0
                except Exception:  # noqa: BLE001 — a locked archive must not fail the chat
                    n = 0
                if key == "conversations":
                    conversations = n
                else:
                    messages = n
    return {"contacts": contacts, "conversations": conversations,
            "messages": messages}


def _cards_for_reply(reply: str, contacts: List[Dict[str, Any]],
                     keep_all: bool = False) -> List[Dict[str, Any]]:
    """Keep only the people the answer actually talks about.

    The tools hand back everything the searches matched, so the cards used to be
    the raw union — an answer about London would show whoever semantic search
    happened to surface, including people it explicitly ruled out. The answer and
    the cards under it must agree. If nothing matches by name (the model
    paraphrased instead of naming), fall back to the unfiltered set rather than
    showing none.

    `keep_all` is the case where agreeing with the text is the WRONG thing, and
    there are now two of them. Both mean "the prose is known to name fewer people
    than the answer is about":

      1. A truncated answer. The prose broke off mid-list, so filtering to the
         names that survived throws away the people it was still about to reach. A
         long roster cut off partway arrived with only a few cards for exactly this.
      2. A capped People shape. The user asked for 10 names out of 47 found
         people; the other 37 were never dropped, they were just not written about.
         Filtering here would turn a prose control into a silent data cut — the one
         thing the shape controls must never do.

    In both cases keep everyone the searches found: the cards are then the more
    complete half of the answer, not a caption for it. handle_chat reports which
    reason applied, so the UI can explain why there are more cards than names.

    First name alone also counts. The model writes "Ada Lovelace" the first
    time and "Ada" after, and a surname-only or first-name-only mention is still
    a mention."""
    if keep_all:
        return contacts
    text = (reply or "").lower()

    def mentioned(c: Dict[str, Any]) -> bool:
        name = (c.get("name") or "").strip().lower()
        if not name:
            return False
        if name in text:
            return True
        # Fall back to the distinctive parts of the name, not every part: "de"
        # or "van" would match half the alphabet.
        parts = [p for p in re.split(r"[\s,]+", name) if len(p) > 2]
        return any(p in text for p in parts)

    named = [c for c in contacts if mentioned(c)]
    return named or contacts


# ── Chats (threads) ──────────────────────────────────────────────────────────
# Ask was one endless conversation. It is now sorted into chats: you exit one
# thread and start another, as in any chat app. The whole of the
# persistence lives in db.py (chat_conversations + chat_history.conversation_id);
# this section is the HTTP shape of it.
#
# Titles are DERIVED, never LLM-generated — db.chat_title(). Invariant 8 says the
# app works with zero API keys, so a label that needed a key would be the one part
# of this page that silently vanished without one; and billing an API call to name
# a thread the user already named by typing their question is wrong twice over.


def _conversation_dict(row: Any, turns: Any = None) -> Dict[str, Any]:
    """One chat, as JSON. Tolerates both row shapes db.py hands back.

    db.get_conversations() rows carry `turns` and `last_message_at` from the
    aggregate; db.get_conversation() rows are a plain SELECT * and carry neither.
    One serializer for both, because two would drift — and a key that exists on
    one surface and not the other is exactly the silent-200 failure api_serialize
    warns about.
    """
    keys = set(row.keys())
    n = turns if turns is not None else (row["turns"] if "turns" in keys else None)
    out: Dict[str, Any] = {
        "id": row["id"],
        # Never blank: chat_title() falls back to "New chat", so the list has no
        # nameless row to render (DESIGN_DIRECTION: empty cells get explicit text).
        "title": (row["title"] or "").strip() or db.CHAT_TITLE_FALLBACK,
        "created_at": row["created_at"] if "created_at" in keys else "",
        "updated_at": row["updated_at"] if "updated_at" in keys else "",
        "shape": _shape_from_json(row["shape_json"] if "shape_json" in keys else ""),
    }
    if n is not None:
        out["turns"] = n
    if "last_message_at" in keys:
        out["last_message_at"] = row["last_message_at"] or ""
    return out


def _shape_from_json(raw: Any) -> Dict[str, str]:
    """The answer shape a chat last ran with, or the defaults.

    Goes through chat.normalize_shape, so a hand-edited or stale shape_json is
    clamped to the allowed vocabulary exactly like a shape arriving over HTTP —
    the column is not a second, unguarded way into the ceilings.
    """
    if not raw:
        return chat.normalize_shape(None)
    try:
        return chat.normalize_shape(json.loads(raw))
    except (ValueError, TypeError):
        return chat.normalize_shape(None)


def _resolve_conversation(conn: Any, body: Dict[str, Any]) -> tuple:
    """Which chat a turn belongs to, and HOW that was decided. Returns (id, how).

    Three paths, and the caller reports which one ran as `conversation_resolved`:

      "requested"  the client named a chat. Verified through _require_conversation,
                   so a stale or deleted id 404s instead of quietly landing the
                   user's words in whatever thread happens to be newest.
      "newest"     no id — a pre-chats client, or a fresh tab. Continues the most
                   recently active thread, which is the same definition
                   /api/chat/history uses, so the old routes and the new ones never
                   disagree about which chat "the current one" is.
      "created"    no id and no chats at all. A first-ever question opens its
                   thread rather than making the user press New first.
    """
    raw = body.get("conversation_id") if isinstance(body, dict) else None
    if raw not in (None, ""):
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           "conversation_id must be an integer") from None
        _require_conversation(conn, cid)
        return cid, "requested"
    cid = db.newest_conversation_id(conn)
    if cid is not None:
        return cid, "newest"
    return db.create_conversation(conn), "created"


# "There are chats, but none of them is live" — every one was deleted. An integer
# rather than a new None-like value on purpose: both call sites already do
# int(scoped) and pass it to db.get_conversation and db.get_chat_history, and no row
# can carry conversation_id = -1, so this resolves to an empty chat through the
# existing code paths with no special case. None cannot serve here, because None
# already means "do not filter at all".
_EMPTY_CONVERSATION = -1


def _turn_contact_ids(r: Any) -> set:
    """The contact ids one stored turn wrote down, or an empty set.

    NC-151. Read defensively for the same reason `_stored_question` is: a row
    can predate the column, and a half-written value must cost the cards and
    nothing else. An unreadable value returns EMPTY, which means the turn looks
    like it names nobody — and for the replay that is the safe direction only
    because the prose is what carries names, so a turn with no readable cards is
    a turn we cannot show is unsafe, not one we have shown is safe. Said plainly:
    this filter works on what Ask wrote down, and what it wrote down is cards.
    """
    try:
        raw = r["contacts_json"] if "contacts_json" in r.keys() else ""
    except (TypeError, AttributeError, IndexError, KeyError):
        return set()
    if not raw:
        return set()
    try:
        cards = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(cards, list):
        return set()
    return {c["id"] for c in cards
            if isinstance(c, dict) and isinstance(c.get("id"), int)}


def _llm_messages(history: Any, hidden_ids: Any = frozenset()) -> List[Dict[str, str]]:
    """The context window, trimmed so it starts on a USER turn.

    The Anthropic API requires the first message to be a user turn. handle_chat
    writes the new user message BEFORE reading history, so the window it reads has
    an ODD length as soon as the chat is longer than the limit — and the last N of
    an odd-length [user, assistant, user, ...] run begins with an ASSISTANT turn.

    Concretely, with the default limit of 20 that is every question from the 11th
    onward: the request was malformed, chat.chat() raised, handle_chat caught it and
    answered from the keyword engine instead. So a long conversation quietly stopped
    using the LLM at all — the failure mode this file's own docstring calls out as
    the one invariant 8's fallback must never hide.

    Dropping the leading assistant turn costs one turn of context and is what makes
    the call valid. Anything else — reordering, synthesising a user turn — would put
    words in the user's mouth.

    NC-156: an assistant turn that ASKED something gets its options appended, for
    the model's eyes only. What is stored, and what the user sees, is the bare
    question — the options were buttons, and writing "(a, b or c)" into the
    transcript would say them twice. But the model reads this conversation back
    as plain prose, so without them the next user turn is a bare word —
    "Investors" — replying to a question whose choices are nowhere in the
    window. The line goes into the CONTEXT and never into the database, which is
    what keeps the screen and the model's view from having to agree about
    formatting.
    """
    rows = list(history)

    # NC-151. A person hidden today was still being re-sent to Anthropic on every
    # following question in a thread that once named them, because this replays
    # each turn's prose verbatim and the prose carries the names the tools
    # returned at the time. The rule is RE-CHECK ON READ: nothing hidden
    # is sent again, and nothing already written is destroyed.
    #
    # The PAIR goes, not the single turn. Dropping only the assistant turn would
    # leave its question answered by the turn after it and put two user turns
    # together, and the docstring above is about what a malformed window costs.
    # Losing the question with its answer also happens to be the honest cut: a
    # question like "what do I know about Anna" names her in the user's own
    # words, which no card-based check could see.
    #
    # The cost is stated rather than hidden: a follow-up that leans on a dropped
    # turn ("tell me more about the third one") has less to lean on. That is the
    # price of the rule, not a defect in it.
    drop = set()
    if hidden_ids:
        for i, r in enumerate(rows):
            if r["role"] != "assistant":
                continue
            if _turn_contact_ids(r) & set(hidden_ids):
                drop.add(i)
                if i and rows[i - 1]["role"] == "user":
                    drop.add(i - 1)

    msgs = []
    for i, r in enumerate(rows):
        if i in drop:
            continue
        content = r["content"]
        if r["role"] == "assistant":
            asked = _stored_question(r)
            if asked:
                offered = ", ".join(asked.get("options") or [])
                if offered:
                    content = f"{content}\n\n(Options offered: {offered})"
        msgs.append({"role": r["role"], "content": content})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _stored_question(r: Any) -> Any:
    """The question one persisted turn asked back, or None. NC-156.

    Probed against row.keys() rather than assumed, the same defensive read
    _history_message does for contacts_json: a database older than the column is a
    real case, and this runs on every question.

    Corrupt JSON returns None — the same answer as a row that never asked — so a
    half-written value costs the buttons and nothing else.
    """
    try:
        raw = r["ask_json"] if "ask_json" in r.keys() else ""
    except (TypeError, AttributeError, IndexError, KeyError):
        return None
    if not raw:
        return None
    try:
        asked = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return asked if isinstance(asked, dict) and asked.get("question") else None


def _asked_last_turn(history: Any) -> bool:
    """Did the previous assistant turn end with a question? NC-156.

    THE ONE RULE A PROMPT CANNOT ENFORCE. Everything else about when to ask
    lives in the tool description, because it is judgement. This is not
    judgement: two questions in a row is a loop, and a model that has just been
    handed a one-word answer is in exactly the state where asking again is most
    tempting and least useful.

    Read off the transcript rather than stored as a flag, because the transcript
    already knows. Takes ROWS rather than a connection so it stays a pure scan
    over whatever the caller can see; handle_chat now hands it the single newest
    assistant row of this chat (db.last_assistant_message), rather than the
    window it feeds the model — see NC-160 at the call site for why that
    distinction is the whole point.
    """
    for r in reversed(list(history)):
        if r["role"] == "assistant":
            return _stored_question(r) is not None
    return False


def _newest_or_unfiltered(conn: Any) -> Any:
    """The chat the legacy /api/chat/history routes mean, or None for "no filter".

    None is not a failure here, it is the pre-chats behaviour preserved on purpose.
    A shipped build older than this change calls /api/chat/history with no id, and
    it must not 404 — the packaged .app is what users run, so a stale bundle is a
    live client. If the
    database has chats, "the newest chat" is the honest answer. If it has none but
    still has turns (rows written before the migration, on a database that has not
    been through init_db yet), None makes db.get_chat_history read them unfiltered
    so the transcript still appears instead of coming back empty.

    THE TRAP, and why the second branch is guarded: "no live chats" and "no chats"
    are different states. Delete your only chat and the first is true while the
    second is false — the chat is soft-deleted, so its turns are all still in
    chat_history. Returning None there made the legacy route read UNFILTERED, so the
    transcript of the chat you just deleted stayed on screen, and the app looked like
    delete had done nothing. Unfiltered is only correct when no chat row has ever
    existed, which is the genuine pre-migration case. Otherwise an empty chat is the
    truthful answer, and _EMPTY_CONVERSATION says so without ambiguity — None cannot,
    because None already means "do not filter".
    """
    live = db.newest_conversation_id(conn)
    if live is not None:
        return live
    any_chat = conn.execute(
        "SELECT 1 FROM chat_conversations LIMIT 1").fetchone()
    return _EMPTY_CONVERSATION if any_chat else None


def handle_chats_get(conn: Any) -> Dict[str, Any]:
    """GET /api/chats — every live chat, most recently active first.

    `turns` on each row is a COUNT over chat_history (db.get_conversations), so the
    list cannot claim a length the transcript disagrees with.

    `adoption` is the upgrade receipt: on a database that predates chats, every
    existing turn was adopted into ONE thread by db._adopt_orphan_chat_history
    during init_db, and this is where the UI learns that happened and what the
    thread is called. A migration that silently reorganises the user's own words
    would be the same defect as a cap nobody can see.
    """
    rows = db.get_conversations(conn)
    chats = [_conversation_dict(r) for r in rows]
    adoption = None
    raw = db.state_get(conn, db.CHAT_ADOPTION_STATE_KEY, "")
    if raw:
        try:
            adoption = json.loads(raw)
        except (ValueError, TypeError):
            adoption = None
    return {
        "chats": chats,
        "total": len(chats),
        "newest_id": chats[0]["id"] if chats else 0,
        "adoption": adoption,
        # The two numbers that used to be one, published so the UI can explain a
        # trimmed transcript or a short memory without hardcoding either.
        "limits": {"context": chat_history_context(),
                   "transcript": chat_transcript_max(),
                   "title_max": db.CHAT_TITLE_MAX},
    }


def handle_chats_post(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/chats — start a new, empty chat.

    A title may be supplied and is normalised by db.chat_title like any other; the
    normal case sends none and the first question names the thread
    (db.title_conversation_if_unset). `shape` is stored so a chat opened with the
    controls already set keeps them.
    """
    title = str(body.get("title") or "").strip()
    shape = parse_shape(body)
    cid = db.create_conversation(conn, db.chat_title(title) if title else "",
                                 json.dumps(shape))
    conn.commit()
    row = db.get_conversation(conn, cid)
    return {"conversation": _conversation_dict(row, turns=0), "created": True}


def handle_chat_rename(conn: Any, conversation_id: int,
                       body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/chat/{id}/rename — name a chat by hand.

    Returns the title actually STORED, not the one sent: db.rename_conversation
    runs a hand-typed name through the same chat_title() as a derived one, so a
    300-character paste comes back truncated and the caller can render what the
    list will really show rather than assuming its input survived.
    """
    _require_conversation(conn, conversation_id)
    title = db.rename_conversation(conn, conversation_id, str(body.get("title") or ""))
    conn.commit()
    row = db.get_conversation(conn, conversation_id)
    return {"conversation": _conversation_dict(
        row, turns=db.count_chat_messages(conn, conversation_id)), "title": title}


def handle_chat_delete(conn: Any, conversation_id: int) -> Dict[str, Any]:
    """DELETE /api/chat/{id} — remove a chat from the list.

    Soft, like delete_list and delete_note: the thread gets deleted = 1 and its
    turns stay on disk, so a mis-click is recoverable and a future sync can carry
    the tombstone. `turns` says how many went with it and `purged: False` says they
    were not destroyed — a delete that reports "0" while removing fourteen turns is
    the silent kind this repo does not ship.
    """
    row = _require_conversation(conn, conversation_id)
    title = (row["title"] or "").strip() or db.CHAT_TITLE_FALLBACK
    turns = db.count_chat_messages(conn, conversation_id)
    db.delete_conversation(conn, conversation_id)
    conn.commit()
    return {"ok": True, "id": conversation_id, "title": title,
            "turns": turns, "purged": False}


def handle_chat(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/chat — the Ask view's conversational brain.

    With ANTHROPIC_API_KEY set, runs chat.chat()'s Anthropic tool-use loop,
    grounded in the CRM via search_people/semantic_search/get_contact. Without
    a key — or if the API call errors — falls back to the rule-based
    handle_ask engine so the app still answers with zero API keys.

    Body: {message: "...", shape: {...}, conversation_id: 3}. `conversation_id`
    says which chat this turn joins; omit it and _resolve_conversation continues
    the newest chat (or opens the first one), so a client that predates chats keeps
    working unchanged. The chat it landed in comes back as `conversation`, and how
    that was decided as `conversation_resolved`.

    The shape is no longer per-request-and-forgotten: chat_conversations.shape_json
    holds the shape each chat last ran with, so reopening a thread restores the
    controls that produced its answers instead of showing the client's current
    dropdowns. Only what actually ran is stored — shape_status's `effective`
    reports what applied, and touch_conversation gets the clamped request."""
    message = str(body.get("message") or "").strip()
    if not message:
        raise ApiError(HTTPStatus.BAD_REQUEST, "message is required")
    shape = parse_shape(body)
    conversation_id, resolved = _resolve_conversation(conn, body)

    db.add_chat_message(conn, "user", message, conversation_id=conversation_id)
    # Derived rather than generated: the first question names
    # the thread and later ones leave the name alone. Runs AFTER the insert above,
    # because the guard inside it counts user turns and expects to see this one.
    title = db.title_conversation_if_unset(conn, conversation_id, message)
    # NC-052. Commit BEFORE the network call, and this line is the whole fix.
    #
    # Both writes above are on this connection, and chat.chat() below runs the
    # entire Anthropic tool-use loop — up to NC_CHAT_TOOL_ROUNDS round trips —
    # before anything commits. Everything that loop touches is a read, so the
    # writer lock sat held for the full duration of the answer, and any other
    # connection wanting to write got sqlite3's "database is locked".
    #
    # A bigger busy_timeout cannot fix this — it was already 30 seconds — because
    # a 40-round answer outlasts any ceiling worth setting. Committing here is the
    # smallest correct change and it is strictly better in its own right: the
    # question survives a crash mid-answer, which is exactly when you would most
    # want it kept. Nothing below re-reads these rows expecting them uncommitted,
    # and the fallback path commits at the end regardless, so the durability of
    # the user's turn is unchanged on every path — only the lock is released.
    conn.commit()

    # The LLM context window, scoped to THIS chat. Two things changed here at once
    # and both matter: the read is filtered by conversation_id (without that,
    # starting a new thread would inherit the previous thread's turns as context —
    # the whole point of exiting a chat is that the next one starts clean), and the
    # limit is chat_history_context() rather than the constant that also sized the
    # rendered transcript.
    context_limit = chat_history_context()
    # Counted after the user turn is written, so the total includes the question
    # being asked — the same set the window is a window onto.
    context_total = db.count_chat_messages(conn, conversation_id)
    context_fed = 0

    # Which engine answered is decided here, not guessed downstream: the shape
    # echo has to report Detail and Search as unavailable when the LLM path was
    # skipped OR failed, and "the key is set" does not mean "the LLM answered".
    engine, degraded, caps = "rules", False, None
    if os.environ.get("ANTHROPIC_API_KEY"):
        history = db.get_chat_history(conn, limit=context_limit,
                                      conversation_id=conversation_id)
        # NC-151. The same set every other Ask surface tests ids against, read
        # fresh on every question so hiding someone takes effect on the next one.
        messages = _llm_messages(history, chat._out_of_scope_ids(conn))
        context_fed = len(messages)
        # NC-154. Registered HERE and not inside chat(), because the window that
        # matters opens before chat() is reached: the Stop button appears the
        # moment the question is sent, so a stop pressed in the first half-second
        # must find an entry to flag. begin_run returns "" for an id that is
        # absent, malformed, or already live in another tab — and "" only means
        # this answer cannot be stopped, never that it is refused.
        # NC-156. Whether this turn may come back with a question instead of an
        # answer. Decided here and not in chat.py, because the rule is about the
        # CONVERSATION — never twice running — and chat() is only ever handed a
        # window onto it.
        #
        # NC-160: its OWN one-row read, not the window above. Answering this off
        # `history` made a rule about what Ask is allowed to do depend on
        # NC_CHAT_HISTORY_CONTEXT, which exists only to trade memory for money —
        # so lowering a spending knob quietly switched off the guarantee that it
        # cannot ask you two questions in a row.
        #
        # NC-167: worked out BEFORE begin_run, so nothing at all sits between
        # claiming the handle and the try whose finally releases it. A raise in
        # that gap would leak the run's handle, and a leaked id can never be reused.
        may_ask = not _asked_last_turn(
            [r for r in [db.last_assistant_message(conn, conversation_id)]
             if r is not None])
        run_id = chat.begin_run(body.get("run_id"))
        try:
            result = chat.chat(conn, messages, shape=shape, run_id=run_id,
                               may_ask=may_ask)
            engine = "llm"
            # chat() ran the numbers; take them rather than recomputing, or the two
            # sides could report different caps for the same answer
            # (DESIGN_DIRECTION §4: two components each computing their own count).
            caps = result.pop("caps", None)
            result.pop("shape", None)     # replaced by the fuller echo below
        # Deliberately broad, and the cost of that is worth writing down: this
        # catches a PROGRAMMING error in chat() as readily as a network hiccup,
        # and ships it to the screen as "AI chat was unavailable". A test stub
        # with a stale signature raises TypeError here, and the suite sees
        # silent rule-based answers, not a crash. Degrading is still right;
        # knowing it hides this is why tests stub chat.chat rather than trusting
        # the fallback.
        except Exception as exc:  # noqa: BLE001 — network/API hiccups degrade, not crash
            # NC-300. The reason used to exist only in the reply below, so once
            # that conversation was closed nothing anywhere said what had failed.
            # What is written down: the kind of error, its own message (cut short)
            # and the kind of error underneath it, which is what separates "the
            # network dropped" from "the key was refused". Never the question and
            # never anything about a person.
            cause = exc.__cause__ or exc.__context__
            log.warning("Ask: the AI call failed and keyword search answered "
                        "instead: %s: %s%s", type(exc).__name__, str(exc)[:300],
                        f" (caused by {type(cause).__name__})" if cause else "")
            result = _ask_fallback(conn, message, shape)
            degraded = True
            # This exact string is a contract with static/app/ask.js: AI_DOWN_RE
            # parses the detail out of it, classifies the cause and renders the raw
            # text behind a "Show detail" toggle. Do not reword it here
            # without changing that file too.
            result["reply"] += f"\n\n(AI chat was unavailable: {exc})"
        finally:
            # The other half of begin_run, and the reason nothing has to sweep
            # the registry: whatever registered an entry removes it, on success,
            # on exception and on the stop path alike.
            chat.end_run(run_id)
    else:
        result = _ask_fallback(conn, message, shape)

    # provenance: the tool-call trail (empty on the rule-based fallback, which
    # makes no tool calls) plus how big the haystack was
    result.setdefault("trail", [])
    result["searched"] = _chat_searched(conn)
    # Which engine answered, stated once. The Ask view used to infer it from
    # `spend` being null; an explicit key is what the shape echo, the provenance
    # line and any future surface all read instead of each deriving it.
    result["engine"] = engine
    # The effective shape, always — a client must never assume its request was
    # honoured. On this path it also carries the reason Detail and Search did
    # nothing, which is what lets the UI disable them instead of offering a
    # control that quietly does nothing (invariant 8's visible degraded path).
    result["shape"] = shape_status(shape, engine, caps, degraded)
    # What the model was actually told, as numbers. This narrowing was invisible
    # while one constant sized both the context window and the transcript: a long
    # thread answered from its last 20 turns and nothing said so, which reads as
    # the assistant forgetting rather than as a cap doing its job. `trimmed` is 0
    # on the rules engine because nothing was fed there at all, which `fed_to_llm`
    # states rather than leaving to be inferred from a zero.
    result["context"] = {
        "limit": context_limit,
        "turns": context_fed,
        "total": context_total,
        "trimmed": max(0, context_total - context_fed) if engine == "llm" else 0,
        "fed_to_llm": engine == "llm",
        "scope": "conversation",
    }
    # What the answer cost. Nothing read the API's usage numbers before, so the
    # price of an Ask question was invisible — and a roster question can now run
    # twelve rounds re-sending every tool result. The rule-based fallback costs
    # nothing and reports nothing.
    result.setdefault("spend", None)
    # NC-156. The question this turn asked back, or None. Set on every path,
    # including the rule-based fallback, so the client reads one key rather than
    # testing whether it exists — an absent key and a null one would be two
    # spellings of "this turn is an answer".
    result.setdefault("question", None)

    # persist the contact cards with the turn so they survive a reload
    result["truncated"] = bool(result.get("truncated"))
    # Three numbers, because the cards go through two independent narrowings and
    # each one used to be silent. matched = what the searches found; named = what
    # survived agreeing with the prose; shown = what survived the payload cap. The
    # UI states the gap. Raising a cap without reporting it — which is what
    # happened when this cap went 12 → 80 — moves the lie rather than fixing it.
    found = result.get("contacts") or []
    result["cards_matched"] = len(found)
    # The People control caps the PROSE, so the name filter has to stand down: an
    # answer told to name 10 of 47 found people would otherwise ship 10 cards and
    # the other 37 would vanish between the search and the screen. That is the
    # shape control silently dropping people, which is the one thing it may never
    # do. Only on the LLM path — on the rules engine the cap already happened at
    # the result level, where total_matched reports it.
    prose_capped = engine == "llm" and chat.people_count(shape) > 0
    keep_all = result["truncated"] or prose_capped
    # Why the filter stood down, so the UI can say "the answer names 10 of 47" and
    # not leave cards_named == cards_matched looking like the answer named
    # everyone. Both reasons can apply to one answer, so this is a list.
    reasons = ([] if not result["truncated"] else ["answer_truncated"]) + \
              ([] if not prose_capped else ["people_shape"])
    result["cards_filter"] = "kept-all" if keep_all else "names"
    result["cards_filter_reasons"] = reasons
    named = _cards_for_reply(result.get("reply") or "", found, keep_all)
    result["cards_named"] = len(named)
    cards = named[:chat_max_cards()]
    result["contacts"] = cards
    result["cards_shown"] = len(cards)
    # conversation_id is NOT optional here: leaving it off files the question in
    # the chat but writes the ANSWER with conversation_id NULL, so every thread
    # renders as its user turns alone, with the replies invisible until the next
    # init_db sweeps them into a fresh "adopted" chat of their own. Both writes
    # take the same id.
    # NC-155. The three fields that say what this answer cost and what it called.
    # Stored rather than recomputed, because they cannot be recomputed: they are
    # what the API reported for THAT request, at the prices and model of that
    # day. Only written when there is something in them — a rule-based answer
    # touched no API and its receipt is honestly empty, and an empty string is
    # what _history_receipt reads as "nothing to show" rather than "zero".
    receipt = {"searched": result.get("searched"),
               "trail": result.get("trail") or [],
               "spend": result.get("spend"),
               # NC-154 rides in NC-155's column rather than adding one of its
               # own: it is the same kind of fact (what happened to this answer),
               # written, read and rendered by the same code. A reload must show
               # a stopped answer as stopped, or the transcript quietly rewrites
               # it into an answer that just happened to be short.
               "stopped": bool(result.get("stopped"))}
    # NC-156. Written as its own column, because a reload has to put the BUTTONS
    # back: the answer to a question was never written down, so a question whose
    # options vanish leaves the thread with nowhere to go. A stopped answer never
    # carries one — chat.py's stop path returns before any question can be
    # formed — so this is empty there by construction, not by a check here.
    asked = result.get("question")
    db.add_chat_message(conn, "assistant", result["reply"],
                        contacts_json=json.dumps(cards) if cards else "",
                        truncated=result["truncated"],
                        conversation_id=conversation_id,
                        receipt_json=(json.dumps(receipt)
                                      if any(receipt.values()) else ""),
                        ask_json=(json.dumps(asked)
                                  if isinstance(asked, dict) and asked else ""))
    # Bump the chat's activity stamp (it sorts the list) and persist the shape this
    # answer ran with. Both in one write, so a thread can never be left sorted by
    # an activity it did not have.
    db.touch_conversation(conn, conversation_id, json.dumps(shape))
    conn.commit()
    # Which chat this turn landed in, with the live title — so a first question can
    # name its thread and the client renders that name without a second round trip.
    conv = db.get_conversation(conn, conversation_id)
    # Re-counted rather than derived as context_total + 1: `turns` is the number the
    # chat list shows, and a count that agrees with the table by construction cannot
    # drift from it if the writes above ever change.
    result["conversation"] = (
        _conversation_dict(conv, turns=db.count_chat_messages(conn, conversation_id))
        if conv is not None else {"id": conversation_id, "title": title})
    result["conversation_resolved"] = resolved
    return result


def handle_chat_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/chat/stop — ask a running answer to stop. NC-154.

    A second, tiny request while the first is still open. That is only possible
    because app.py serves on a ThreadingHTTPServer, and it is the whole design:
    the browser does NOT abort its own fetch, so the original request stays open
    and comes back with the work already done and the real bill. Aborting the
    fetch instead would hide the spinner and leave the loop running and billing.

    Takes no database connection — it touches one in-memory flag and must not
    queue behind a writer while somebody is waiting for an answer to stop.

    "unknown" is not an error and answers 200: the run finished on its own
    between the press and this request, which is the common case for a question
    that was nearly done. There is nothing to apologise for and nothing to fix.
    """
    return {"ok": True, "status": chat.request_stop(payload.get("run_id")),
            "live": chat.live_runs()}


ROUTES = {"POST": {"/api/chat/stop": "handle_chat_stop"}}


def _history_message(r: Any, hidden_ids: Any = frozenset()) -> Dict[str, Any]:
    """One persisted turn, as the transcript renders it.

    A row read back may predate contacts_json / truncated, so both are probed
    against row.keys() rather than assumed — the same defensive read the previous
    version did, kept because a database older than those columns is a real
    case.
    """
    contacts = []
    raw = r["contacts_json"] if "contacts_json" in r.keys() else ""
    if raw:
        try:
            contacts = json.loads(raw)
        except (ValueError, TypeError):
            contacts = []
    # NC-151. The card was frozen at answer time and served back with no scope
    # check at all, so hiding someone left yesterday's answer still naming them
    # and still offering a working "Open <name>" button. Filtered on the way out
    # rather than scrubbed in the database: the prose stays as it was written,
    # and un-hiding someone brings their card back.
    withheld = 0
    if hidden_ids and isinstance(contacts, list):
        kept = [c for c in contacts
                if not (isinstance(c, dict) and c.get("id") in hidden_ids)]
        withheld = len(contacts) - len(kept)
        contacts = kept
    # NC-155. What that answer cost and what it called, if this row is new enough
    # to have it. Spread into the same three top-level keys the LIVE answer uses
    # (`searched`, `trail`, `spend`), so the transcript renders a restored turn
    # and a just-arrived one through one code path instead of two that drift.
    out = {"role": r["role"], "content": r["content"],
           "created_at": r["created_at"], "contacts": contacts,
           "truncated": bool(r["truncated"]) if "truncated" in r.keys() else False}
    # Only when there IS something withheld — the same rule as `stopped` and
    # `question` below, so an ordinary turn carries no key rather than a zero the
    # renderer has to test. The page needs the number because an answer that was
    # all cards and no prose would otherwise fall through to "No contact matches
    # that question.", which would be a new lie in place of the old one.
    if withheld:
        out["contacts_withheld"] = withheld
    raw = r["receipt_json"] if "receipt_json" in r.keys() else ""
    if raw:
        try:
            receipt = json.loads(raw)
        except (ValueError, TypeError):
            receipt = None
        # A half-written row costs the receipt, never the transcript — and it
        # costs it the SAME WAY a row older than the column does, by leaving the
        # three keys off entirely rather than setting them to null. Two spellings
        # of "there is nothing here" is two things the renderer has to know.
        if isinstance(receipt, dict):
            out["searched"] = receipt.get("searched")
            out["trail"] = receipt.get("trail") or []
            out["spend"] = receipt.get("spend")
            if receipt.get("stopped"):
                out["stopped"] = True
    # NC-156. The question this turn asked back, with its buttons, so reopening a
    # chat that ends on a question is still a place you can answer from. Only set
    # when there IS one — the same rule as `stopped` above — so an ordinary turn
    # carries no key rather than a null the renderer has to test.
    asked = _stored_question(r)
    if asked:
        out["question"] = asked
    return out


def handle_chat_history_get(conn: Any, conversation_id: Any = None) -> Dict[str, Any]:
    """GET /api/chat/{id}/history — one chat's transcript. Oldest turn first.

    Also serves the legacy GET /api/chat/history with no id, where it means the
    newest chat (see _newest_or_unfiltered).

    Sized by chat_transcript_max(), NOT by the LLM context window, so a long chat
    shows every turn instead of only its last few with the earlier ones unreachable
    and no number admitting it. `total` / `shown` / `trimmed` make any cut visible,
    and `limit` names the ceiling that caused it.
    """
    scoped = conversation_id if conversation_id is not None else _newest_or_unfiltered(conn)
    conversation = None
    if scoped is not None:
        # 404 only when the caller NAMED a chat. A resolved-to-newest id came from
        # this database a line ago, so it cannot be missing.
        row = (_require_conversation(conn, int(scoped)) if conversation_id is not None
               else db.get_conversation(conn, int(scoped)))
        if row is not None:
            conversation = _conversation_dict(row)
    limit = chat_transcript_max()
    total = db.count_chat_messages(conn, scoped)
    rows = db.get_chat_history(conn, limit=limit, conversation_id=scoped)
    # NC-151. Once per request, not once per turn: it is one query, and every
    # turn has to be judged against the same answer.
    hidden = chat._out_of_scope_ids(conn)
    out = [_history_message(r, hidden) for r in rows]
    return {"messages": out, "conversation": conversation,
            "conversation_id": int(scoped) if scoped is not None else 0,
            "total": total, "shown": len(out), "trimmed": max(0, total - len(out)),
            "limit": limit}


def handle_chat_history_clear(conn: Any, conversation_id: Any = None) -> Dict[str, Any]:
    """DELETE /api/chat/{id}/history — empty one chat, keeping the thread.

    Also serves the legacy DELETE /api/chat/history with no id, where it means the
    newest chat — NOT every chat. That is a deliberate narrowing of the old
    behaviour: the route used to wipe the single global transcript, which was
    everything there was; wiring it to "all chats" now would turn one stale
    button in a packaged build into a destructive clear of threads its UI cannot
    even show. It falls back to clearing unscoped rows only when there is no chat
    to name, which is the pre-migration database.

    Returns how many turns went, because "ok: true" for a delete of 40 messages
    tells the user nothing.
    """
    scoped = conversation_id if conversation_id is not None else _newest_or_unfiltered(conn)
    if conversation_id is not None:
        _require_conversation(conn, int(conversation_id))
    removed = db.clear_chat_history(conn, scoped)
    if scoped is not None:
        db.touch_conversation(conn, int(scoped))
    conn.commit()
    return {"ok": True, "cleared": removed,
            "conversation_id": int(scoped) if scoped is not None else 0,
            "scope": "conversation" if scoped is not None else "all"}
