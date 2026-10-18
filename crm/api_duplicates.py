#!/usr/bin/env python3
"""Duplicate detection and merge.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

_dup_member is a deliberately separate serializer from api_serialize's: the picker
needs each candidate's enrichment facts so it can default 'keep' to the most-filled
record rather than an arbitrary one. Merging must never lose the discarded
record's enrichment — there is a regression test for exactly that.
"""

import json
from http import HTTPStatus
from typing import Any, Dict

import db
from api_core import ApiError
from api_guards import _require_contact
from api_serialize import _channel_counts, _last_seen


def _dup_member(row: Any) -> Dict[str, Any]:
    """Compact serialization of one contact inside a duplicate group. Includes
    the enrichment facts so the picker can show what each candidate holds
    and default 'keep' to the most-filled record instead of a bare one."""
    try:
        topics = [t for t in json.loads(row["topics"] or "[]") if t]
    except Exception:
        topics = []
    return {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"] or "",
        "source": row["source"] or "",
        "role_type": row["role_type"] or "",
        "home_metro": row["home_metro"] or row["home_city"] or "",
        "topics": topics,
        "blurb": row["blurb"] or "",
        "counts": _channel_counts(row),
        "last": _last_seen(row),
    }


def handle_duplicates(conn: Any) -> Dict[str, Any]:
    groups = db.find_duplicate_groups(conn)
    return {"groups": [[_dup_member(r) for r in group] for group in groups]}


def handle_merge(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    keep_id = body.get("keep_id")
    dup_id = body.get("dup_id")
    if not isinstance(keep_id, int) or not isinstance(dup_id, int):
        raise ApiError(HTTPStatus.BAD_REQUEST, "keep_id and dup_id must be integers")
    if keep_id == dup_id:
        raise ApiError(HTTPStatus.BAD_REQUEST, "keep_id and dup_id must differ")
    _require_contact(conn, keep_id)
    _require_contact(conn, dup_id)
    # db.merge_contacts moves the archived messages too, so every caller (this
    # endpoint, the CLI, the bot) gets it rather than each remembering to.
    db.merge_contacts(conn, keep_id, dup_id)
    conn.commit()
    return {"ok": True, "kept_id": keep_id}
