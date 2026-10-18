#!/usr/bin/env python3
"""The shapes the API returns for a person — the one place a contact is serialized.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

Two payloads matter: _contact_summary (the list/card shape) and _contact_detail
(the person page, ~30 keys assembled from 8 helpers and 5 db calls). Both are
consumed by static/app/people.js and both are contract: a dropped key here is a
200 response with a silently missing field, which no unit test catches because
they assert individual keys rather than payload completeness. Add keys freely;
remove or rename one only together with its frontend reader.

_last_seen delegates to db.row_last_seen so there is exactly one implementation
of "most recent touchpoint across the seven channels".
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import archive as arc
import db
import photos
from api_dates import _contact_dates, sentence_case
from api_notes import _note_dict


def _last_seen(row: Any) -> str:
    """Most recent touchpoint across all seven channels, or ''.

    Kept as a name here because tests and several handlers call app._last_seen;
    the implementation lives in db.row_last_seen so there is only one copy.
    """
    return db.row_last_seen(row)


def _channel_counts(row: Any) -> Dict[str, int]:
    return {
        "linkedin": row["msg_count"] or 0,
        "imessage": row["imsg_count"] or 0,
        "whatsapp": row["wa_count"] or 0,
        "email": row["em_count"] or 0,
        "telegram": row["tg_count"] or 0,
        "messenger": row["msgr_count"] or 0,
        "instagram": row["ig_count"] or 0,
    }


def _contact_summary(row: Any) -> Dict[str, Any]:
    """The list/card shape for one person.

    This is now the only copy. cloud/pwa_api.py carried a diverged fork of it
    until NC-096 deleted the cloud tier; nothing outside crm/ builds this shape.
    """
    strength = db.relationship_strength(row)
    # topics is a JSON-array STRING in the DB — parse it like _contact_detail does,
    # so the list/people view can render topic tags (never the raw string).
    try:
        topics = json.loads(row["topics"] or "[]")
    except Exception:
        topics = []
    return {
        "id": row["id"],
        "photo": photos.photo_url(row),
        "name": row["name"],
        "company": row["company"] or "",
        "position": row["position"] or "",
        "home_city": row["home_city"] or "",
        "home_country": row["home_country"] or "",
        "home_metro": row["home_metro"] or "",
        "role_type": row["role_type"] or "",
        "blurb": row["blurb"] or "",
        "topics": topics,
        "category": row["category"] or "",
        "status": row["status"] or "",
        "excluded": bool(row["excluded"]),
        "counts": _channel_counts(row),
        "last": _last_seen(row),
        "strength": strength,
        "strength_label": db.strength_label(strength),
    }


def _archive_preview(contact_id: int) -> List[Dict[str, Any]]:
    """Most-recent 5 archived messages for the contact-detail preview.

    Opens archive.db independently (it may not exist yet — returns [] safely).
    """
    try:
        with arc.open_if_present() as arc_conn:
            if arc_conn is None:
                return []
            msgs = arc.get_messages(arc_conn, contact_id, limit=5)
        return [
            {
                "id": m["id"],
                "channel": m["channel"],
                "direction": m["direction"],
                "ts": m["ts"],
                "subject": m["subject"],
                "body": m["body"],
            }
            for m in msgs
        ]
    except Exception:
        return []


def _next_action(conn: Any, row: Any) -> Optional[Dict[str, Any]]:
    """Derived-only next action for the person page (no reminder schema).
    One kind for now: 'reconnect', present exactly when the contact would
    appear on the reconnect list and isn't snoozed (db.reconnect_due),
    carrying enough context to render the card without another fetch."""
    if not db.reconnect_due(conn, row):
        return None
    last = _last_seen(row)
    days_silent = 0
    if last:
        try:
            then = datetime.strptime(last[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_silent = max(0, (datetime.now(timezone.utc) - then).days)
        except (ValueError, TypeError):
            days_silent = 0
    preview = _archive_preview(row["id"])
    snippet, channel = "", ""
    if preview:
        m = preview[0]  # newest first
        snippet = ((m["subject"] + " — " if m["subject"] else "") + (m["body"] or ""))[:120]
        channel = m["channel"] or ""
    return {"kind": "reconnect", "days_silent": days_silent,
            "last_snippet": snippet, "last_channel": channel}


def _brief_meta(row: Any) -> Optional[Dict[str, Any]]:
    """The brief's receipt, read back from the brief_meta column
    handle_make_brief writes: messages/channels/built_at at generation time,
    plus newer_messages counted from the archive right now — the stale marker."""
    raw = (row["brief_meta"] or "") if "brief_meta" in row.keys() else ""
    if not raw.strip():
        return None
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return None
    built_at = str(meta.get("built_at") or "")
    newer = 0
    if built_at:
        with arc.open_if_present() as arc_conn:
            if arc_conn is not None:
                try:
                    newer = arc_conn.execute(
                        "SELECT COUNT(*) AS n FROM messages WHERE contact_id = ? AND ts > ?",
                        (row["id"], built_at),
                    ).fetchone()["n"] or 0
                except Exception:  # noqa: BLE001 — a locked archive must not fail the page
                    newer = 0
    return {"messages": int(meta.get("messages") or 0),
            "channels": int(meta.get("channels") or 0),
            "built_at": built_at,
            "newer_messages": newer}


def _dedupe_history(items: Any) -> list:
    """Drop a shared-history line that another line already says more specifically.

    The extractor often emits both a general and a dated version of one fact
    ("You both studied at Exempel University" AND "You both studied at Exempel
    University from Jan 2021 to Dec 2024"), which rendered as two near-identical
    bullets for people who have more than one. Keep the longer, more specific
    line; drop the one it starts with. Exact repeats collapse to their first appearance, and order is
    otherwise preserved because these read as a list, not a set.

    Comparison is on whitespace-collapsed casefolded text, but the ORIGINAL string
    is what survives — never a normalized rewrite of the user's data."""
    if not isinstance(items, list):
        return []
    norm = []
    for raw in items:
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        key = " ".join(s.split()).casefold()
        if key:
            norm.append((key, s))
    kept = []
    for i, (key, s) in enumerate(norm):
        # Superseded by a strictly longer line that begins with this one, or —
        # for an exact repeat — by an earlier copy of itself.
        if any(other.startswith(key) and (len(other) > len(key) or j < i)
               for j, (other, _) in enumerate(norm) if j != i):
            continue
        kept.append(s)
    return kept


def _li_mutuals(row: Any) -> Dict[str, Any]:
    """Networking context re-parsed from the archived LinkedIn profile
    (enrich_li_extras.py): named mutual connections, shared history, recent
    activity. LLM-parsed, so the UI labels it 'from LinkedIn (unverified)'.
    Previously written to li_extra and read nowhere."""
    try:
        li = json.loads(row["li_extra"] or "{}") if "li_extra" in row.keys() else {}
    except Exception:
        li = {}
    if not isinstance(li, dict):
        li = {}
    return {
        "mutual_names": li.get("mutual_names") or [],
        "shared_history": _dedupe_history(li.get("shared_history") or []),
        "recent_activity": li.get("recent_activity") or "",
    }


def _contact_detail(conn: Any, row: Any) -> Dict[str, Any]:
    notes = [_note_dict(n) for n in db.get_notes(conn, row["id"])]
    idents = [
        {"kind": i["kind"], "value": i["value"]}
        for i in db.get_identifiers(conn, row["id"])
    ]
    try:
        topics = json.loads(row["topics"] or "[]")
    except Exception:
        topics = []
    return {
        "id": row["id"],
        "photo": photos.photo_url(row),
        "name": row["name"],
        "company": row["company"] or "",
        "position": row["position"] or "",
        "category": row["category"] or "",
        "how_we_met": row["how_we_met"] or "",
        "connected_on": row["connected_on"] or "",
        "first_contact": row["first_contact"] or "",
        "linkedin_url": row["linkedin_url"] or "",
        "status": row["status"] or "",
        "excluded": bool(row["excluded"]),
        "deleted": bool(row["deleted"]) if "deleted" in row.keys() else False,
        "source": row["source"] or "",
        "counts": _channel_counts(row),
        "last": _last_seen(row),
        "strength": db.relationship_strength(row),
        "strength_label": db.strength_label(db.relationship_strength(row)),
        "notes": notes,
        "identifiers": idents,
        "history_preview": _archive_preview(row["id"]),
        # facts layer
        "home_city": row["home_city"] or "",
        "home_country": row["home_country"] or "",
        "home_metro": row["home_metro"] or "",
        "topics": topics,
        "role_type": row["role_type"] or "",
        "blurb": row["blurb"] or "",
        "facts_source": row["facts_source"] or "",
        "mutuals": _li_mutuals(row),
        "dates": _contact_dates(conn, row["id"]),
        "story": _story_text(conn, row),
        "story_source": ("ai" if (("brief" in row.keys()) and (row["brief"] or "").strip()) else "auto"),
        # derived next-action card + the brief's receipt
        "next_action": _next_action(conn, row),
        "brief_meta": _brief_meta(row),
        # the transparent inputs behind the closeness score, for the breakdown popover
        "strength_parts": {
            k: (round(v, 3) if isinstance(v, float) else v)
            for k, v in db.strength_breakdown(row).items()
        },
    }


def _story_text(conn: Any, row: Any) -> str:
    """The 'story so far' paragraph for a profile. Uses the saved brief (AI or
    otherwise) when present; otherwise composes an honest template from real
    fields, so every profile has a story even with zero API keys."""
    brief = (row["brief"] or "").strip() if "brief" in row.keys() else ""
    if brief:
        return brief
    bits: List[str] = []
    how = (row["how_we_met"] or "").strip()
    if how:
        bits.append(how if how.endswith((".", "!", "?")) else how + ".")
    counts = _channel_counts(row)
    top = sorted(((v, k) for k, v in counts.items() if v), reverse=True)
    if top:
        parts = [f"{v:,} {k if k in ('email',) else {'linkedin': 'LinkedIn', 'imessage': 'iMessage', 'whatsapp': 'WhatsApp', 'telegram': 'Telegram', 'messenger': 'Messenger', 'instagram': 'Instagram'}.get(k, k)} messages" for v, k in top[:2]]
        last = _last_seen(row)
        when = f" — last touch {last[:10]}" if last else ""
        bits.append("You've exchanged " + " and ".join(parts) + when + ".")
    elif row["connected_on"]:
        bits.append(f"Connected on LinkedIn since {row['connected_on']}.")
    summary = (row["auto_summary"] or "").strip() or (row["blurb"] or "").strip()
    if summary:
        bits.append(summary if summary.endswith((".", "!", "?")) else summary + ".")
    place = (row["home_metro"] or row["home_city"] or "").strip()
    try:
        topics = [t for t in json.loads(row["topics"] or "[]") if t]
    except Exception:
        topics = []
    tail = []
    if place:
        tail.append(f"based in {place}")
    if topics:
        tail.append("into " + ", ".join(topics[:3]))
    if tail:
        bits.append(sentence_case(" · ".join(tail)) + ".")
    return " ".join(bits)
