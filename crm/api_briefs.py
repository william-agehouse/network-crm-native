#!/usr/bin/env python3
"""The AI-written pieces: the cached "story so far" brief and a draft message.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

Both are OPTIONAL. run_claude raises ApiError(400) when ANTHROPIC_API_KEY is
unset, and the contact detail keeps working — _story_text composes an honest
template from real fields instead. Nothing here is on the zero-key path; that
invariant lives in crm/api_ask.py.

anthropic is imported lazily inside run_claude, like crm/chat.py, so the module
imports cleanly without the dependency installed.
"""

import json
import logging
import os
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Dict

import db
import owner
from api_core import ApiError
from api_guards import _require_contact
from api_messages import handle_get_episodes
from api_serialize import _contact_detail


BRIEF_MAX_EPISODES = 12


def _ai_context(conn: Any, contact_id: int, row: Any) -> str:
    """The shared 'everything we know about this person' block fed to Claude for
    both the story-so-far brief and reconnect message drafts."""
    episodes = handle_get_episodes(contact_id, {"limit": [str(BRIEF_MAX_EPISODES)]})["episodes"]
    convo = "\n".join(
        f"- {e['ts_end'][:10]} · {e['channel']} · {e['count']} msgs: {e['snippet']}"
        for e in episodes
    ) or "(no archived conversations)"
    notes = "\n".join(f"- {n['created_at'][:10]}: {n['body'][:200]}"
                      for n in (db.get_notes(conn, contact_id) or [])[:6]) or "(none)"
    detail = _contact_detail(conn, row)
    return (
        f"Name: {detail['name']}\nRole: {detail['position']} at {detail['company']}\n"
        f"Location: {detail['home_metro'] or detail['home_city']}\n"
        f"How we met (their words): {detail['how_we_met'] or '(not written)'}\n"
        f"Channels: {json.dumps(detail['counts'])}\nLast contact: {detail['last']}\n"
        f"Notes:\n{notes}\nRecent conversations:\n{convo}"
    )


def _run_claude(system: str, user_content: str, max_tokens: int, error_label: str) -> str:
    """Shared one-shot Claude call for the brief + message-draft features.
    Raises ApiError (400 no key, 502 API failure) so callers don't repeat this."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "ANTHROPIC_API_KEY is not set")
    try:
        import anthropic  # optional dependency, imported lazily like crm/chat.py
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as exc:  # noqa: BLE001 — API/network problems surface as a clean error
        # NC-363: the provider's own error text ("Error code: 401 - {...}") goes to
        # the log; the person gets a sentence that says what to do.
        logging.getLogger("networkcrm").warning("%s: the AI service failed: %s", error_label, exc)
        raise ApiError(HTTPStatus.BAD_GATEWAY,
                       "The AI service did not answer. Check your Anthropic key in "
                       "Settings, then try again.") from exc
    if not text:
        raise ApiError(HTTPStatus.BAD_GATEWAY,
                       "The AI service answered with nothing. Try again in a moment.")
    return text


def handle_make_brief(conn: Any, contact_id: int) -> Dict[str, Any]:
    """POST /api/contact/{id}/brief — write a fresh AI 'story so far' using the
    profile + recent conversation episodes. Needs ANTHROPIC_API_KEY; the caller
    should treat a 400 as 'feature not available'. The result is cached on the
    contact row (brief / brief_updated_at) until explicitly refreshed."""
    row = _require_contact(conn, contact_id)
    profile = _ai_context(conn, contact_id, row)
    system = ("You write 'the story so far' for one contact in a personal CRM. "
              "3-4 warm, factual sentences in second person ('You met…'). Only use "
              "facts present in the data. No headers, no bullets, no invented details.")
    # What the user said they are working on (NC-010). Appended, never merged:
    # the hand-written half above describes the JOB, this describes WHOSE network
    # this is and what they want from it. Empty string on an install where nothing
    # has been filled in, which reproduces the request this made before.
    owner_block = owner.context_block(conn)
    if owner_block:
        system = system + "\n\n" + owner_block
    text = _run_claude(system, profile, 300, "brief generation")
    # Receipt for the prose: what the brief was built from, so the UI can say
    # "From N messages · M channels · built <date>" and mark it stale once newer
    # messages arrive. Same episode window _ai_context reads.
    episodes = handle_get_episodes(contact_id, {"limit": [str(BRIEF_MAX_EPISODES)]})["episodes"]
    meta = {
        "messages": sum(e["count"] for e in episodes),
        "channels": len({e["channel"] for e in episodes}),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    conn.execute(
        "UPDATE contacts SET brief = ?, brief_updated_at = datetime('now'), "
        "brief_meta = ? WHERE id = ?",
        (text, json.dumps(meta), contact_id),
    )
    conn.commit()
    return {"ok": True, "story": text, "source": "ai",
            "brief_meta": {**meta, "newer_messages": 0}}


def _style_block(conn: Any, row: Any) -> tuple:
    """The measured writing-style instruction for this draft, and the bucket it came
    from. ("", "") when no profile has been built.

    This is what connects crm/style_profile.py to something a person sees. Before
    it, the Data card said "Drafts can then match your habits instead of a generic
    voice", the pass wrote a profile into the state table, and NOTHING read it — a
    card promising an outcome the code did not deliver, which is the same defect
    class as a setting that does nothing.

    Nothing is injected when no profile exists, so the prompt stays byte-identical
    to today's for anyone who has not run the pass. style_directive() returns an
    honest "no profile available" sentence in that case, and putting that in a system
    prompt would spend tokens telling the model something it cannot act on.

    Invariant 7 holds by construction, not by trust: the directive is assembled from
    numbers and from tokens drawn from closed allowlists in style_profile.py, so it
    carries no message text and no third-party name. A test in
    tests/test_style_profile.py tries to break that.
    """
    try:
        import style_profile
    except Exception:      # noqa: BLE001 — an optional read must never break a draft
        return "", ""
    try:
        prof = style_profile.load(conn)
        if not prof.get("present"):
            return "", ""
        buckets = prof.get("buckets") or {}
        # Register from the contact's own strength, on db.strength_label's boundary,
        # and medium "chat" because that is what this draft is. Falling back to
        # best_bucket rather than to nothing: a profile measured only on letters is
        # still a better description of how the user writes than a generic voice.
        register = "close" if db.relationship_strength(row) >= 40 else "new"
        want = "." + register + ".chat"
        cands = [k for k in buckets if k.endswith(want)]

        def _n(key):
            b = buckets.get(key) or {}
            for f in ("messages", "n", "count", "sample"):
                v = b.get(f) if isinstance(b, dict) else None
                if isinstance(v, int):
                    return v
            return 0

        key = max(cands, key=_n) if cands else (prof.get("best_bucket") or "")
        if not key:
            return "", ""
        return style_profile.style_directive(prof, key), key
    except Exception:      # noqa: BLE001 — same reason
        return "", ""


def handle_draft_message(conn: Any, contact_id: int) -> Dict[str, Any]:
    """POST /api/contact/{id}/draftmessage — a short reconnect message grounded
    in real notes/conversation history. Not cached — each call is fresh (the
    UI has an explicit 'Regenerate'). Needs ANTHROPIC_API_KEY (400 without one)."""
    row = _require_contact(conn, contact_id)
    profile = _ai_context(conn, contact_id, row)
    # The sender's name comes from owner.display_name(), never from a name written
    # into the prompt (NC-010): every user of this app would otherwise draft
    # messages signed by somebody else's name. It falls back to "the user", never
    # to a guess: a prompt that names the wrong human produces confident output
    # that is wrong about who is sending it.
    system = (
        f"You draft a short, warm reconnect message from {owner.display_name(conn)} "
        "to one contact in their personal CRM, to send over WhatsApp/iMessage/email. "
        "2-4 sentences, casual and specific — reference a real detail from their "
        "notes or last conversation when there is one, instead of generic 'hope "
        "you're well' filler. Output only the message text, nothing else — no "
        "subject line, no explanation.")
    owner_block = owner.context_block(conn)
    if owner_block:
        system = system + "\n\n" + owner_block
    # The hand-written half of this prompt describes the JOB. The measured half
    # describes the VOICE, and it replaces nothing above — the two are orthogonal,
    # which is why the style block is appended rather than merged in.
    style, style_bucket = _style_block(conn, row)
    if style:
        system = system + "\n\n" + style
    text = _run_claude(system, profile, 200, "message draft")
    # Receipt: drafts are AI-written too, so they carry the same "From N
    # messages · M channels · built <now>" grounding line. Always fresh
    # (regenerated each call), so there is no stale marker. Same episode window
    # _ai_context fed to Claude.
    episodes = handle_get_episodes(contact_id, {"limit": [str(BRIEF_MAX_EPISODES)]})["episodes"]
    meta = {
        "messages": sum(e["count"] for e in episodes),
        "channels": len({e["channel"] for e in episodes}),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        # Which voice wrote it. "" means the generic prompt ran, and the UI must be
        # able to tell those apart — a draft claiming to be in the user's style when no
        # profile was measured is exactly the kind of unearned claim this codebase
        # keeps removing.
        "style_bucket": style_bucket,
    }
    return {"message": text, "meta": meta}
