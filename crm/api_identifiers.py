#!/usr/bin/env python3
"""Identifiers — the phones, emails and socials that let importers find a person.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

Every importer matches incoming messages to an existing contact through these:
name-norm, last-9-digits of a phone, or an email. Adding one here is what makes a
previously unmatched WhatsApp thread attach to the right person on the next run.
"""

from http import HTTPStatus
from typing import Any, Dict

import db
from api_core import ApiError
from api_guards import _require_contact


def handle_add_identifier(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/contact/{id}/identifier — add a phone/email, or remove one with
    {remove: true}. Identifiers were previously add-only and read-only in the UI,
    so a wrong or outdated number could never be corrected except in SQLite."""
    kind = str(body.get("kind") or "").strip().lower()
    value = str(body.get("value") or "").strip()
    if kind not in ("phone", "email"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "kind must be 'phone' or 'email'")
    if not value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "value is required")
    _require_contact(conn, contact_id)
    if body.get("remove"):
        db.delete_identifier(conn, contact_id, kind, value)
    else:
        # Typed by hand on the person page, so it outranks every importer
        # (invariant 4): a number added here survives as the stored spelling
        # even when iMessage later writes the same number its own way.
        db.add_identifier(conn, contact_id, kind, value, source="manual")
    conn.commit()
    idents = [{"kind": i["kind"], "value": i["value"]}
              for i in db.get_identifiers(conn, contact_id)]
    return {"ok": True, "identifiers": idents}
