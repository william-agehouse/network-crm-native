#!/usr/bin/env python3
"""
Home feed (GET /api/feed) — conversation signals derived from what's already
stored: the feed talks about conversations ("Ada replied — unanswered 2d"),
not sync plumbing. No new schema; every row is one query away from crm.db /
archive.db / the state table.

Five signal kinds, each with its own lookback window and cap so the feed stays
a glance, not a log:

  unanswered — their last 1:1 message is inbound and you haven't replied
  fact       — the LinkedIn scraper learned where someone lives now
  group      — a group chat lit up in the last few days
  note       — you wrote a note recently (your own momentum, reflected back)
  import     — a source synced (one gray line, not a wall of receipts)

Rows are bucketed by day (Today / Yesterday / This week), newest first, and
empty buckets are dropped. app.py owns the route; this module owns the shape.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import archive as arc
import db
import figures  # NC-087: the one spelling of a contact-scope filter
import importers

# Lookback window (days) + row cap per signal.
UNANSWERED_DAYS = 14
UNANSWERED_CAP = 5
FACT_DAYS = 7
FACT_CAP = 5
GROUP_DAYS = 3
GROUP_CAP = 3
NOTE_DAYS = 7
NOTE_CAP = 3
IMPORT_DAYS = 7
IMPORT_CAP = 3
SNIPPET_LEN = 80

# channel key -> how the feed copy speaks it ("replied on LinkedIn — …")
CHANNEL_LABELS = {
    "linkedin": "LinkedIn", "imessage": "iMessage", "whatsapp": "WhatsApp",
    "email": "email", "telegram": "Telegram", "messenger": "Messenger",
    "instagram": "Instagram",
}


# ── Time helpers ──────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff(days: int) -> str:
    """'YYYY-MM-DD HH:MM:SS' `days` ago — compares lexicographically with
    normalized stored timestamps."""
    return (_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _norm_ts(ts: str) -> str:
    """Normalize any stored timestamp to the feed's 'YYYY-MM-DD HH:MM:SS'.
    Archive rows are already in that shape; state/facts stamps are ISO with a
    'T' (and sometimes an offset), which truncates away cleanly at 19 chars."""
    return (ts or "").replace("T", " ")[:19]


def _days_ago(ts: str) -> int:
    """Whole days since a stored timestamp, 0 for today/unparseable."""
    try:
        then = datetime.strptime(_norm_ts(ts)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0
    return max(0, (_now() - then).days)


def _row(kind: str, *, contact_id: Optional[int] = None, name: str = "",
         text: str = "", meta: str = "", ts: str = "",
         channel: Optional[str] = None, group: Optional[str] = None) -> Dict[str, Any]:
    """One feed row in the shape the Home view renders."""
    return {"kind": kind, "contact_id": contact_id, "name": name, "text": text,
            "meta": meta, "ts": _norm_ts(ts), "channel": channel, "group": group}


def _excluded_ids(conn: Any) -> set:
    return {r["id"] for r in conn.execute(
        f"SELECT id FROM contacts WHERE {figures.population_where('hidden')}")}


# ── Signals ───────────────────────────────────────────────────────────────────
# Today's attention queue looks ~180 days back for owed replies (aligns with
# find_reconnect's 6-month cutoff), closing the gap left by the feed's 14-day
# glance so a reply owed 3 months ago doesn't vanish before it goes "cold".
OWED_REPLY_TODAY_DAYS = 180


def owed_reply_candidates(conn: Any, arc_conn: Any, *, days: int, cap: int) -> List[Any]:
    """Contacts whose most recent 1:1 archived message is inbound, within
    `days`, and strictly newer than anything you sent back — i.e. they're
    waiting on you. Ordered by relationship strength then recency, capped.
    Returns tuples (strength, last_in, row, channel, snippet). Single source of
    truth shared by the 14-day Home feed glance and the ~180-day Today queue."""
    cutoff = _cutoff(days)
    # NC-227. Answered from the index idx_msg_owed (archive.py) alone, without
    # opening the messages themselves. It holds group_name, contact_id, ts and
    # direction and nothing else, so a column added to this query that is not one
    # of those four sends it back to reading every message.
    stats = arc_conn.execute(
        """SELECT contact_id,
                  MAX(CASE WHEN direction = 0 THEN ts END) AS last_in,
                  MAX(CASE WHEN direction = 1 THEN ts END) AS last_out
           FROM messages WHERE group_name = ''
           GROUP BY contact_id"""
    ).fetchall()
    candidates = []
    for s in stats:
        last_in, last_out = s["last_in"] or "", s["last_out"] or ""
        if not last_in or last_in < cutoff:
            continue
        if last_out and last_in <= last_out:  # you already answered (or tied)
            continue
        row = db.get_contact(conn, s["contact_id"])
        if row is None or row["excluded"]:
            continue
        candidates.append((db.relationship_strength(row), last_in, row))
    candidates.sort(key=lambda c: c[1], reverse=True)  # newest first…
    candidates.sort(key=lambda c: -c[0])               # …within strength order

    out = []
    for strength, last_in, row in candidates[:cap]:
        m = arc_conn.execute(
            "SELECT * FROM messages WHERE contact_id = ? AND group_name = '' "
            "ORDER BY ts DESC LIMIT 1", (row["id"],)).fetchone()
        channel = m["channel"] if m else None
        body = ""
        if m is not None:
            body = (m["subject"] + " — " if m["subject"] else "") + (m["body"] or "")
        snippet = body.strip().replace("\n", " ")[:SNIPPET_LEN]
        out.append((strength, last_in, row, channel, snippet))
    return out


def _unanswered_rows(conn: Any, arc_conn: Any) -> List[Dict[str, Any]]:
    """Feed glance (14-day window): who's waiting on a reply. Thin formatter
    over owed_reply_candidates, so the feed and the Today queue share one query
    and one definition of 'owed'."""
    out = []
    for _strength, last_in, row, channel, snippet in owed_reply_candidates(
            conn, arc_conn, days=UNANSWERED_DAYS, cap=UNANSWERED_CAP):
        label = CHANNEL_LABELS.get(channel or "", channel or "")
        out.append(_row(
            "unanswered", contact_id=row["id"], name=row["name"],
            text=f"replied on {label} — unanswered {_days_ago(last_in)}d",
            meta=snippet, ts=last_in, channel=channel,
        ))
    return out


def _fact_rows(conn: Any) -> List[Dict[str, Any]]:
    """Freshly scraped LinkedIn profile facts — only the location change is
    interesting enough for the feed, so home_city must be non-empty."""
    cutoff = _cutoff(FACT_DAYS)
    rows = conn.execute(
        f"""SELECT * FROM contacts
           WHERE {figures.population_where('visible')} AND facts_source = 'linkedin_scrape'
             AND home_city != '' AND facts_updated_at != ''
           ORDER BY facts_updated_at DESC"""
    ).fetchall()
    out = []
    for r in rows:
        if _norm_ts(r["facts_updated_at"]) < cutoff:  # stamps are ISO — compare normalized
            continue
        out.append(_row("fact", contact_id=r["id"], name=r["name"],
                        text=f"is now in {r['home_city']}",
                        ts=r["facts_updated_at"]))
        if len(out) >= FACT_CAP:
            break
    return out


def _group_rows(conn: Any, arc_conn: Any) -> List[Dict[str, Any]]:
    """Group chats with archived activity in the last few days, busiest context
    first by recency. Messages from excluded contacts never count."""
    cutoff = _cutoff(GROUP_DAYS)
    excluded = _excluded_ids(conn)
    msgs = arc_conn.execute(
        "SELECT group_name, group_key, contact_id, channel, ts FROM messages "
        "WHERE group_name != '' AND ts >= ?", (cutoff,)
    ).fetchall()
    # NC-110: tallied by KEY, shown by NAME. Keyed on the label, this counted
    # every unnamed three-person chat as one conversation and reported it as
    # "Group of 3 was active — N messages" when that was several unrelated
    # chats. The key falls back to the label for rows imported before the column
    # existed, which is the same answer as before rather than a worse one.
    tally: Dict[str, Dict[str, Any]] = {}
    for m in msgs:
        if m["contact_id"] in excluded:
            continue
        key = (m["group_key"] if "group_key" in m.keys() else "") or m["group_name"]
        g = tally.setdefault(key, {"n": 0, "ts": "", "channel": m["channel"],
                                   "name": m["group_name"]})
        g["n"] += 1
        if m["ts"] > g["ts"]:
            g["ts"], g["channel"] = m["ts"], m["channel"]
    ranked = sorted(tally.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:GROUP_CAP]
    return [
        _row("group", name=info["name"],
             text=f"{info['name']} was active — {info['n']} "
                  f"message{'s' if info['n'] != 1 else ''}",
             ts=info["ts"], channel=info["channel"], group=info["name"])
        for _key, info in ranked
    ]


def _note_rows(conn: Any) -> List[Dict[str, Any]]:
    """Notes you wrote this week — the habit loop reflecting back."""
    cutoff = _cutoff(NOTE_DAYS)
    rows = conn.execute(
        f"""SELECT n.body, n.created_at, c.id AS contact_id, c.name
           FROM notes n JOIN contacts c ON c.id = n.contact_id
           WHERE {figures.population_where('visible', 'c')} AND n.created_at >= ?
           ORDER BY n.created_at DESC, n.id DESC LIMIT ?""",
        (cutoff, NOTE_CAP),
    ).fetchall()
    return [
        _row("note", contact_id=r["contact_id"], name=r["name"],
             text=f"You noted how you met {r['name']}",
             meta=(r["body"] or "").replace("\n", " ")[:SNIPPET_LEN],
             ts=r["created_at"])
        for r in rows
    ]


def _import_rows(conn: Any) -> List[Dict[str, Any]]:
    """Sources synced this week — one quiet line each, from the same
    import_synced_* state keys handle_import_run stamps."""
    cutoff = _cutoff(IMPORT_DAYS)
    rows = conn.execute(
        "SELECT key, value FROM state WHERE key LIKE 'import_synced_%'"
    ).fetchall()
    out = []
    for r in rows:
        ts = _norm_ts(r["value"] or "")
        if not ts or ts < cutoff:
            continue
        key = r["key"][len("import_synced_"):]
        spec = importers.REGISTRY.get(key)
        # enrich runs (recompute closeness, fill facts…) stamp the same state
        # keys but aren't syncs — "Recompute closeness synced" is nonsense copy.
        #
        # An UNKNOWN key is skipped too, and that is the important half: this
        # used to read `if spec is not None and ...`, so a key with no registry
        # entry fell through to the title-cased fallback below. Retiring the
        # "Recompute closeness" row would have put a literal "Engagement synced"
        # on the Home feed, because import_synced_engagement was already stamped
        # and the state row outlives the registry entry. A stamp we cannot name
        # is not a sync we should announce.
        if spec is None or spec.category != "import":
            continue
        label = spec.label
        out.append(_row("import", name=label, text=f"{label} synced", ts=ts))
    out.sort(key=lambda row: row["ts"], reverse=True)
    return out[:IMPORT_CAP]


# ── Endpoint ──────────────────────────────────────────────────────────────────
# NOT ROUTED (NC-009). GET /api/feed existed for exactly one
# caller — the "Lately" drawer on the Today page — and Today was deleted. The
# route is gone from app.py; this function and its tests are kept whole, the same
# way meta_import.py and telegram_import.py were kept when their rows went, so
# rebuilding a home surface starts from working code rather than from git
# archaeology. Nothing in the app reaches it today, and its tests call it
# directly, so green here does not mean a Home feed exists.
#
# The rest of this module is NOT in that position: owed_reply_candidates,
# CHANNEL_LABELS and the time helpers are live, imported by api_messages.
def handle_feed(conn: Any) -> Dict[str, Any]:
    """GET /api/feed — all signals gathered, bucketed Today / Yesterday /
    This week (newest first within each), empty buckets dropped."""
    rows: List[Dict[str, Any]] = []
    if os.path.exists(arc.ARCHIVE_DB_PATH):
        arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        try:
            rows += _unanswered_rows(conn, arc_conn)
            rows += _group_rows(conn, arc_conn)
        finally:
            arc_conn.close()
    rows += _fact_rows(conn)
    rows += _note_rows(conn)
    rows += _import_rows(conn)

    today = _now().strftime("%Y-%m-%d")
    yesterday = (_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (_now() - timedelta(days=7)).strftime("%Y-%m-%d")
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "Today": [], "Yesterday": [], "This week": [], "Earlier": []}
    for r in sorted(rows, key=lambda row: row["ts"], reverse=True):
        d = r["ts"][:10]
        if d == today:
            label = "Today"
        elif d == yesterday:
            label = "Yesterday"
        elif d >= week_ago:
            label = "This week"
        else:
            # the widest lookback window is 14 days — don't call that "this week"
            label = "Earlier"
        buckets[label].append(r)
    return {"buckets": [{"label": lab, "rows": rs} for lab, rs in buckets.items() if rs]}
