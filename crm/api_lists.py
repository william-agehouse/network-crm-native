#!/usr/bin/env python3
"""Lists — named groups of contacts, and their CSV/markdown export.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

_list_export_csv returns (csv_text, list_name) rather than an HTTP response: the
filename sanitising and the Content-Disposition attachment header stay in
app.py's do_GET, which is the only place that knows about the wire.
"""

from http import HTTPStatus
from typing import Any, Dict, List

import db
from api_core import ApiError
from api_dates import NOT_A_DAY, is_calendar_day
from api_guards import _require_list
from api_serialize import _contact_summary


def handle_lists_get(conn: Any) -> Dict[str, Any]:
    return {"lists": [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"],
         "event_date": r["event_date"] or "", "venue": r["venue"] or "",
         "members": r["members"] or 0, "invited": r["invited"] or 0,
         "replied": r["replied"] or 0, "attending": r["attending"] or 0}
        for r in db.get_lists(conn)
    ]}


def _known_people(conn: Any, contact_ids: List[Any]) -> List[int]:
    """NC-337. The ids as numbers, each one a person who exists, or a 400 naming
    the ones that are not. An unknown id was a 500 "FOREIGN KEY constraint
    failed", and a word a 500 too. A number sent as text still counts."""
    def number(c: Any) -> bool:
        return not isinstance(c, bool) and str(c).strip().isdecimal()
    bad = [str(c) for c in contact_ids if not number(c)]
    if bad:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "contact_ids must be numbers; these are not: " + ", ".join(bad))
    ids = [int(str(c).strip()) for c in contact_ids]
    known = {r["id"] for r in conn.execute("SELECT id FROM contacts")}
    missing = [str(i) for i in dict.fromkeys(ids) if i not in known]
    if missing:
        raise ApiError(HTTPStatus.BAD_REQUEST, "no person has these ids: " + ", ".join(missing))
    return ids


def _add_people(conn: Any, list_id: int, ids: List[int]) -> int:
    """Put these people on the list; returns how many were not on it before.

    NC-336. Removing someone only marks their row removed, and the plain insert
    skips a row that is there, so Add left them off and answered 0 ("already
    holds everyone you selected"). They now come back as "To invite", counted."""
    removed = {r["contact_id"] for r in conn.execute(
        "SELECT contact_id FROM list_members WHERE list_id = ? AND status = '__removed__'",
        (list_id,))}
    added = db.add_list_members(conn, list_id, ids)
    for cid in dict.fromkeys(ids):
        if cid in removed:
            db.set_list_member_status(conn, list_id, cid, "")
            added += 1
    return added


def handle_list_create(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError(HTTPStatus.BAD_REQUEST, "name is required")
    contact_ids = body.get("contact_ids") or []
    if not isinstance(contact_ids, list):
        raise ApiError(HTTPStatus.BAD_REQUEST, "contact_ids must be a list")
    ids = _known_people(conn, contact_ids)     # before the list exists, so a refusal leaves none
    list_id = db.create_list(conn, name)
    added = _add_people(conn, list_id, ids)
    conn.commit()
    return {"id": list_id, "added": added}


def handle_list_detail(conn: Any, list_id: int) -> Dict[str, Any]:
    row = _require_list(conn, list_id)
    members = [
        {**_contact_summary(m), "list_status": m["list_status"] or ""}
        for m in db.get_list_members(conn, list_id)
    ]
    # The counts come from the server, not from len()-ing `members` in the
    # browser: the pane that renders them sits beside the rail, which reads
    # db.get_lists(), and the two used to count differently. NC-089.
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"],
            "event_date": row["event_date"] or "", "venue": row["venue"] or "",
            "members": members, "counts": db.get_list_counts(conn, list_id)}


def handle_list_add(conn: Any, list_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    _require_list(conn, list_id)
    contact_ids = body.get("contact_ids") or []
    if not isinstance(contact_ids, list) or not contact_ids:
        raise ApiError(HTTPStatus.BAD_REQUEST, "contact_ids (non-empty list) required")
    added = _add_people(conn, list_id, _known_people(conn, contact_ids))
    conn.commit()
    return {"ok": True, "added": added}


def handle_list_member(conn: Any, list_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/list/{id}/member — set a member's pipeline status, or remove
    them with {remove: true}."""
    _require_list(conn, list_id)
    contact_id = body.get("contact_id")
    if not isinstance(contact_id, int):
        raise ApiError(HTTPStatus.BAD_REQUEST, "contact_id must be an integer")
    if body.get("remove"):
        db.remove_list_member(conn, list_id, contact_id)
    else:
        status = str(body.get("status") or "")
        if status not in db.LIST_MEMBER_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           f"status must be one of {sorted(db.LIST_MEMBER_STATUSES)}")
        db.set_list_member_status(conn, list_id, contact_id, status)
    conn.commit()
    return {"ok": True}


def handle_list_rename(conn: Any, list_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    _require_list(conn, list_id)
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError(HTTPStatus.BAD_REQUEST, "name is required")
    db.rename_list(conn, list_id, name)
    conn.commit()
    return {"ok": True}


def handle_list_meta(conn: Any, list_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/list/{id}/meta — set the event date and/or venue. An absent or
    null key leaves a field alone, "" clears it. NC-338: an event date must be
    a real day, YYYY-MM-DD; "soon" was stored, and the calendar never showed it."""
    _require_list(conn, list_id)
    ed = body.get("event_date")
    vn = body.get("venue")
    if isinstance(ed, str):
        ed = ed.strip()
    if ed is not None and ed != "" and not is_calendar_day(ed):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"event_date: {NOT_A_DAY}")
    db.set_list_fields(conn, list_id,
                       event_date=None if ed is None else str(ed),
                       venue=None if vn is None else str(vn))
    conn.commit()
    return {"ok": True}


def handle_list_delete(conn: Any, list_id: int) -> Dict[str, Any]:
    _require_list(conn, list_id)
    db.delete_list(conn, list_id)
    conn.commit()
    return {"ok": True}


def _csv_safe(v: Any) -> str:
    """Neutralize spreadsheet formula-injection: a cell starting with = + - @
    (or a control char) is prefixed with an apostrophe so Excel/Sheets render
    it as text, not a formula. Contact fields can come from scraped LinkedIn."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _list_export_csv(conn: Any, list_id: int):
    """A day-of attendee roster for a list: one CSV row per member with
    their role/company/location, invite status, and contact handles."""
    import csv
    import io
    row = _require_list(conn, list_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "role", "company", "location", "status", "phones", "emails"])
    for m in db.get_list_members(conn, list_id):
        idents = db.get_identifiers(conn, m["id"])
        phones = "; ".join(i["value"] for i in idents if i["kind"] == "phone")
        emails = "; ".join(i["value"] for i in idents if i["kind"] == "email")
        w.writerow([_csv_safe(x) for x in (
            m["name"], m["position"] or m["role_type"] or "", m["company"] or "",
            m["home_metro"] or m["home_city"] or "", m["list_status"] or "to invite",
            phones, emails,
        )])
    return buf.getvalue(), (row["name"] or f"list-{list_id}")
