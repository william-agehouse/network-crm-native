#!/usr/bin/env python3
"""Notes — the append-only timeline that makes a contact a story rather than a card.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

Notes are never edited in place by an import and never summarised away: every one
is timestamped and kept. _note_streak counts consecutive days with at least one
note, which is the one number the Today view uses to say you are keeping up.
"""

from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Dict, List

import db
from api_core import ApiError
from api_guards import _require_contact


def _note_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "body": row["body"],
        "kind": row["kind"],
        "created_at": row["created_at"],
    }


def _note_streak(conn: Any) -> Dict[str, int]:
    """Consecutive days (ending today or yesterday) with at least one note written,
    plus how many notes were written today. Notes timestamps are UTC."""
    rows = conn.execute(
        "SELECT DISTINCT DATE(created_at) AS d FROM notes WHERE kind != 'auto' "
        "ORDER BY d DESC LIMIT 120"
    ).fetchall()
    dates = [r["d"] for r in rows if r["d"]]
    today = datetime.now(timezone.utc).date()
    streak = 0
    if dates:
        from datetime import date as _date, timedelta as _td
        have = {_date.fromisoformat(d) for d in dates}
        cursor = today if today in have else (today - _td(days=1))
        while cursor in have:
            streak += 1
            cursor -= _td(days=1)
    today_count = conn.execute(
        "SELECT COUNT(*) AS n FROM notes WHERE kind != 'auto' "
        "AND DATE(created_at) = DATE('now')"
    ).fetchone()["n"] or 0
    return {"days": streak, "today": today_count}


def handle_add_note(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    text = str(body.get("body") or "").strip()
    if not text:
        raise ApiError(HTTPStatus.BAD_REQUEST, "body is required")
    _require_contact(conn, contact_id)
    note_id = db.add_note(conn, contact_id, text, kind="note")
    # We deliberately no longer seed how_we_met from the first note: that copy
    # was frozen and uneditable, and it fed Claude as ground truth. how_we_met
    # is now its own editable field on the person page (POST …/howwemet).
    conn.execute(
        "UPDATE contacts SET status='described', times_reviewed=times_reviewed+1, "
        "last_reviewed_at=datetime('now') WHERE id=?",
        (contact_id,),
    )
    conn.commit()
    note_row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return {"note": _note_dict(note_row)}


def _require_note(conn: Any, note_id: int) -> None:
    """404 for a note that does not exist or was already deleted.

    NC-339. Editing or deleting a missing note used to answer "ok", so a page
    or a second window believed it had saved a change that went nowhere. A
    deleted note counts as missing: it is gone from the person's page, and an
    edit to it would change text nobody can see."""
    row = conn.execute("SELECT deleted FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None or row["deleted"]:
        raise ApiError(HTTPStatus.NOT_FOUND, "that note no longer exists")


def handle_update_note(conn: Any, note_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    text = str(body.get("body") or "").strip()
    if not text:
        raise ApiError(HTTPStatus.BAD_REQUEST, "body is required")
    _require_note(conn, note_id)
    db.update_note(conn, note_id, text)
    conn.commit()
    return {"ok": True}


def handle_delete_note(conn: Any, note_id: int) -> Dict[str, Any]:
    _require_note(conn, note_id)
    db.delete_note(conn, note_id)
    conn.commit()
    return {"ok": True}


def recent_happenings(conn: Any, limit: int = 4) -> List[Dict[str, Any]]:
    """The last few notes written, as Today's 'what's been happening' strip."""
    rows = conn.execute(
        """SELECT n.body, n.kind, n.created_at, c.name, c.id AS contact_id
           FROM notes n JOIN contacts c ON c.id = n.contact_id
           ORDER BY n.id DESC LIMIT ?""", (limit,)
    ).fetchall()
    return [
        {"kind": "note", "contact_id": r["contact_id"], "name": r["name"],
         "body": (r["body"] or "")[:120], "ts": r["created_at"]}
        for r in rows
    ]


def total(conn: Any) -> int:
    """Every note ever written — the dashboard's running count."""
    row = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()
    return (row["n"] if row else 0) or 0
