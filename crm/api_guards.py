#!/usr/bin/env python3
"""Row lookups that 404 instead of returning None.

Flat sibling of app.py — `python3 crm/app.py` puts crm/ at sys.path[0], so these
are plain top-level imports exactly as crm/feed.py does. Do NOT convert them to
`from crm import db`: there is no crm/__init__.py and the launchers depend on the
flat form.

A leaf over db + api_core: it imports nothing else from crm/, so it can never
join an import cycle.
"""

from http import HTTPStatus
from typing import Any

import db
from api_core import ApiError


def _require_contact(conn: Any, contact_id: int) -> Any:
    row = db.get_contact(conn, contact_id)
    if row is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"contact {contact_id} not found")
    return row


def _require_list(conn: Any, list_id: int) -> Any:
    row = db.get_list(conn, list_id)
    if row is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"list {list_id} not found")
    return row


def _require_conversation(conn: Any, conversation_id: int) -> Any:
    """An Ask chat that exists and is not soft-deleted, or a 404.

    db.get_conversation already filters `deleted = 0`, so a thread the user
    removed 404s exactly like one that never existed — which is the point. Ask now
    holds many chats, and a client can hold a stale id: a tab left open on a
    deleted thread, or a packaged build that predates the chat list. Writing that
    turn into some *other* chat instead would be the silent drop this repo keeps
    fixing, and it would put the user's words in a thread he was not looking at.
    404 is the visible answer, and the caller decides whether to start a new chat.
    """
    row = db.get_conversation(conn, conversation_id)
    if row is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"chat {conversation_id} not found")
    return row
