#!/usr/bin/env python3
"""
Local desktop web-app backend for the Network CRM.

Standard-library only (http.server) so it runs on the user's Mac with zero
installs. Binds to 127.0.0.1 by default (APP_HOST can serve the LAN, and
then APP_TOKEN guards every /api route) — your contacts never leave this
machine. The packaged app starts it as its own child with --socket, on a private
file instead of a port (NC-330); run from source it serves the web page in
static/ and the same JSON API on 127.0.0.1.

Run:  python3 app.py          (port from APP_PORT, default 8765)
"""

import sys

# ── Refuse to run on a Python this code cannot work on ───────────────────────
# NC-136. Apple ships Python 3.9.6 and calls it python3. On it, app.py imports
# fine, the server boots and the interface loads — and then Apple Contacts,
# iMessage and WhatsApp import every raise TypeError at import time, from
# `int | None` annotations those three files evaluate at runtime. (db.py and
# config.py write the same annotations and survive, because they carry
# `from __future__ import annotations`, which turns annotations into strings
# that are never evaluated.)
#
# So the failure shape without this block is the bad one: an app that looks
# like it works and silently cannot do the thing it exists for. Checking here
# and stopping is worse-looking and better.
#
# This must stay ABOVE every other import and use no syntax newer than the
# version it rejects, or the check never runs. `sys` is the one import it is
# allowed to make.
MIN_PYTHON = (3, 10)
if sys.version_info < MIN_PYTHON:
    _have = ".".join(str(n) for n in sys.version_info[:3])
    sys.stderr.write(
        "\nNetwork CRM needs Python %s or newer. This is Python %s, at %s.\n\n"
        "On this version the app would start, look fine, and then fail to import\n"
        "Apple Contacts, iMessage and WhatsApp — so it stops here instead.\n\n"
        "Get the macOS 64-bit universal2 installer from\n"
        "https://www.python.org/downloads/ and run Network CRM again.\n\n"
        "Do not use `xcode-select --install` for this. Apple's developer tools\n"
        "install this same Python %s, so it is a multi-gigabyte download that\n"
        "leaves you exactly where you are now.\n\n"
        % (".".join(str(n) for n in MIN_PYTHON), _have, sys.executable, _have))
    raise SystemExit(1)

import hashlib
import inspect
import json
import logging
import os
import re
import signal
import socket
import socketserver
import stat
import subprocess  # noqa: F401 — no direct use left, but tests patch app.subprocess.run
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import config  # noqa: F401 — loads .env (e.g. CRM_DB_PATH) before db reads env
import archive as arc  # noqa: F401 — kept: tests patch app.arc.get_archive_db / channel_watermark
import db
import export_data
import engagement  # archive → per-contact interaction features (familiarity score)
import photos

# ── Moved out of this file, re-exported so `app.X` keeps resolving ───────────
# The route table calls handlers by bare name and the tests reach them through
# the app module, so every symbol that leaves app.py comes straight back in
# here. Deleting one of these lines is how you break the route table.
import api_views  # noqa: F401 — module object, so app.api_views.X is reachable
from api_views import (  # noqa: F401 — re-exported: route table + tests reach these via app
    DESCRIBE_QUEUE_SIZE, _describe_queue, handle_describe_queue,
)
import api_instance  # noqa: F401 — module object, so app.api_instance.X is reachable
from api_instance import (  # noqa: F401 — re-exported: route table + tests reach these via app
    ONBOARDED_GRANDFATHER_CONTACTS, ONBOARDED_STATE_KEY, _settings_diagnostics,
    handle_diagnostics_get, handle_onboarding_get,
    handle_onboarding_post, onboarding_done, handle_settings_get, handle_settings_post,
    handle_update_get,
)
import api_contacts  # noqa: F401 — module object; tests patch app.api_contacts.EXCLUDE_FILE through it
from api_contacts import (  # noqa: F401 — re-exported: route table + tests reach these via app
    ALLOWED_CATEGORIES, ALLOWED_FIELDS, CONTACTS_MAX, CONTACTS_PAGE, EXCLUDE_FILE,
    RECONNECT_MONTHS, REVIEW_MAX_SIZE, REVIEW_STATUS_ACTIONS, _channel_coverage,
    _excluded_count, _new_count, _parse_months, _parse_size, handle_create_contact,
    handle_delete_contact, handle_delete_photo, handle_exclude_list_add,
    handle_exclude_list_get, handle_exclude_list_remove, handle_facets,
    handle_get_contact, handle_list_contacts, handle_locations, handle_purge_contact,
    handle_reconnect, handle_reconnect_skip, handle_review_status,
    handle_semantic_search, handle_set_category, handle_set_excluded,
    handle_set_field, handle_set_howwemet, handle_set_photo, handle_skip,
)
import api_briefs  # noqa: F401 — module object; tests patch app.api_briefs.run_claude through it
from api_briefs import (  # noqa: F401 — re-exported: route table + tests reach these via app
    BRIEF_MAX_EPISODES, _ai_context, _run_claude, handle_draft_message,
    handle_make_brief,
)
import api_duplicates  # noqa: F401 — module object, so app.api_duplicates.X is reachable
from api_duplicates import (  # noqa: F401 — re-exported: route table + tests reach these via app
    _dup_member, handle_duplicates, handle_merge,
)
import api_lists  # noqa: F401 — module object, so app.api_lists.X is reachable
from api_lists import (  # noqa: F401 — re-exported: route table + tests reach these via app
    _csv_safe, _list_export_csv, handle_list_add, handle_list_create,
    handle_list_delete, handle_list_detail, handle_list_member, handle_list_meta,
    handle_list_rename, handle_lists_get,
)
import api_messages  # noqa: F401 — module object, so app.api_messages.X is reachable
from api_messages import (  # noqa: F401 — re-exported: route table + tests reach these via app
    EPISODE_GAP_HOURS, EPISODE_INLINE_MSGS, EPISODE_MAX_MSGS, EPISODE_SCAN_MSGS,
    _last_messages, handle_conversation_search, handle_get_episodes,
    handle_get_history,
)
import api_identifiers  # noqa: F401 — module object, so app.api_identifiers.X is reachable
from api_identifiers import handle_add_identifier  # noqa: F401 — re-exported for the route table
import api_serialize  # noqa: F401 — module object, so app.api_serialize.X is reachable
from api_serialize import (  # noqa: F401 — re-exported: route table + tests reach these via app
    _archive_preview, _brief_meta, _channel_counts, _contact_detail,
    _contact_summary, _last_seen, _li_mutuals, _next_action, _story_text,
)
import api_notes  # noqa: F401 — module object, so app.api_notes.X is reachable
from api_notes import (  # noqa: F401 — re-exported: route table + tests reach these via app
    _note_dict, _note_streak, handle_add_note, handle_delete_note,
    handle_update_note,
)
import api_dates  # noqa: F401 — module object, so app.api_dates.X is reachable
from api_dates import (  # noqa: F401 — re-exported: route table + tests reach these via app
    _contact_dates, _date_reason, _dates_lookahead, _days_since,
    _days_until, _fmt_past_day, _past_date_reason, _when_word, handle_set_date,
    handle_week,
)
from api_guards import (  # noqa: F401 — 27 call sites here + tests
    _require_contact, _require_conversation, _require_list,
)
from api_core import ApiError  # noqa: F401 — re-exported: 20 assertRaises sites + do_GET/_handle_write catch it

# ── The route registry: how a NEW domain gets served ─────────────────────────
# Everything above this line is the original arrangement: a module's handlers are
# re-exported here by name, and each endpoint is a hand-written branch in
# _route_get / _route_write. That made app.py the one file every feature had to
# edit: a finished, tested module such as crm/api_intake.py stayed unreachable
# until someone edited this one shared file.
#
# A module listed in ROUTE_MODULES needs neither. It declares its own table:
#
#     ROUTES = {"GET":  {"/api/thing": "handle_thing"},
#               "POST": {"/api/thing": "handle_thing_post"}}
#
# and app.py discovers it. No re-export, no branch, no edit here beyond adding
# the module to this list once.
#
# Handlers are named as strings and resolved on the module at dispatch time, so
# a typo surfaces as a loud 500 naming the module and handler rather than an
# import-time crash that takes the whole app down.
#
# Whether a handler is handed a db connection is read from its OWN signature: a
# first parameter named `conn` gets one, anything else does not. That contract
# already existed — tests/test_api_intake.py asserts handle_intake_lookup has no
# `conn` parameter precisely because its fetch path must not hold a crm.db handle
# open across a socket wait.
#
# SECURITY: the registry is consulted at the END of _route_get / _route_write,
# which both run *after* _require_api_auth (and, for writes, after the
# cross-origin check). Registry routes cannot shadow a built-in route and cannot
# reach a request that has not already passed auth.
import api_intake  # noqa: F401 — module object, so app.api_intake.X is reachable

import api_setup  # noqa: F401 — module object, so app.api_setup.X is reachable

import api_fda  # noqa: F401 — module object, so app.api_fda.X is reachable

import api_linkedin  # noqa: F401 — module object, so app.api_linkedin.X is reachable

# NC-117. Gmail's setup — the file Google gives you, and the sign-in — as three
# endpoints rather than a paragraph telling you to click a button that was never
# built. Its own module for the same reason api_linkedin has one: a source whose
# setup is a trip through somebody else's developer console needs more than a
# generic import row can carry.
import api_gmail  # noqa: F401 — module object, so app.api_gmail.X is reachable

import api_provenance
import api_sources  # noqa: F401 — module object, so app.api_sources.X is reachable
import api_ledger  # noqa: F401 — the new Data page's one endpoint (NC-088)
import api_abilities  # noqa: F401 — the new Settings page's one endpoint (NC-114)
import api_remove  # noqa: F401 — Settings' Remove everything (NC-314)

# owner.py is the substrate three prompts read (Ask, brief, draft) AND, since
# NC-010's second half, the two routes that let the user type into it. Same
# module on purpose: the ceiling the endpoint enforces is the constant the
# prompt block is sized against, and splitting them would put a limit in one
# file and the reason for it in another.
import owner  # noqa: F401 — module object, so app.owner.X is reachable

# api_dates is a HYBRID and that is deliberate: its contact-date handlers keep
# their existing hand-written branches below (they carry captured ids in the
# path, which the registry does not do), while its one new flat URL —
# /api/upcoming, NC-040 — is declared in its own ROUTES table. Mixing the two in
# one module is fine: the registry is consulted last, so it can only claim a path
# nothing above it matched.
#
# api_setup and api_sources are imported above and no longer listed (NC-237):
# /api/setup/progress and /api/sources were asked only by the old Data and
# Settings pages. The modules stay, because the Data page's one answer
# (api_ledger) reads parts of both directly; only the addresses went.
ROUTE_MODULES = [api_intake, api_fda, api_gmail, api_linkedin,
                 api_provenance, api_dates, owner, api_ledger,
                 api_abilities, api_remove]
import api_import  # noqa: F401 — module object, so app.api_import.X is reachable
from api_import import (  # noqa: F401
    API_ATTEMPT_KEY, API_FIRST_RUN_DAYS, API_OVERLAP_DAYS, API_SOURCES,
    AUTOSYNC_MINUTES_DEFAULT, AUTOSYNC_STATE_KEY, DEFAULT_EMAIL_YEARS,
    ENRICH_AFTER_IMPORT_SEC, FACTS_CHAIN_MIN_MINUTES, FACTS_CHAIN_STATE_KEY,
    STYLE_CHAIN_MIN_MINUTES, STYLE_CHAIN_STATE_KEY,
    IMSG_PASTE_FP_KEY, WA_PASTE_FP_KEY,
    PICK_SCRIPTS, import_timeout_sec,
    _api_autosync_pending, _api_hours, _api_since, _autosync_loop,
    _autosync_pending, _build_command, _chain_is_due, _facts_chain_is_due,
    _style_chain_is_due, _imsg_paste_fp,
    _parse_stamp, _run_chained_facts_pass, _run_chained_style_pass,
    _parse_years, _stamp_pasted_copy, autosync_interval_seconds,
    handle_import_detect, handle_import_open, handle_import_pick_file,
    handle_import_run, handle_import_sources, run_autosync,
)
import api_ask  # noqa: F401 — module object, so app.api_ask.X is reachable
# api_ask is a hybrid for the same reason api_dates is: /api/chat and the chat
# history URLs keep their hand-written branches (two carry a captured id), while
# its one new flat URL — /api/chat/stop, NC-154 — is declared in its own ROUTES
# table. Appended here rather than written into the list above, because api_ask
# is imported below that line and the list cannot name it before it exists.
# The registry is consulted last, so a declared path can only be reached when no
# literal branch matched it — and the literal ones are exact-match `/api/chat`,
# `/api/chats` and `/api/chat/history`, none of which is `/api/chat/stop`.
ROUTE_MODULES.append(api_ask)
from api_ask import (  # noqa: F401
    CHAT_HISTORY_CONTEXT, CHAT_TRANSCRIPT_MAX, _ask_fallback, _ask_reason,
    _cards_for_reply, _chat_searched, _conversation_dict, _newest_or_unfiltered,
    _parse_ask_query, _resolve_conversation, _shape_from_json, chat_history_context,
    chat_max_cards, chat_transcript_max, handle_ask, handle_chat, handle_chat_delete,
    handle_chat_history_clear, handle_chat_history_get, handle_chat_rename,
    handle_chats_get, handle_chats_post,
)

log = logging.getLogger("networkcrm")
# What a crash inside a request answers (NC-363). Both apps show their own plain
# sentence for any 500; this is for anything else that reads the answer.
INTERNAL_ERROR_SENTENCE = "Something went wrong inside Network CRM."

# ── Constants ─────────────────────────────────────────────────────────────────
# Bind to loopback by default (only this Mac can reach the app). Set APP_HOST=0.0.0.0
# to serve on the LAN — e.g. an always-on Mac that another computer on your
# network browses to. When bound off-loopback, an APP_TOKEN bearer check guards every /api
# route.
HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_TOKEN = os.environ.get("APP_TOKEN", "").strip()
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORT = 8765
PORT = int(os.environ.get("APP_PORT", DEFAULT_PORT))
STATIC_DIR = config.STATIC_DIR
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")
MAX_BODY_BYTES = 10_000_000  # roomy enough for a base64 photo upload; loopback-only by default
# Origins the real front end can legitimately be served from. Loopback-only by
# default, so any OTHER origin reaching a write endpoint is a browser on some
# unrelated page — not this app. See _reject_cross_origin.
_ORIGIN_HOSTS = sorted(LOOPBACK_HOSTS | {HOST}) if HOST in LOOPBACK_HOSTS else [HOST]
ALLOWED_ORIGINS = {f"{s}://{h}:{PORT}" for h in _ORIGIN_HOSTS for s in ("http", "https")}

def _origin_is_this_address(origin: str, headers: Any) -> bool:
    """NC-268. Bound off loopback, a page's address is whatever the laptop typed
    (http://192.168.1.20:8765), which the bind address cannot predict: with
    APP_HOST=0.0.0.0 the fixed list held only 0.0.0.0, and every save from the
    laptop was refused. So there, a page served from the very address the request
    arrived at is this app. The token still guards every /api route off loopback,
    and loopback keeps the fixed list, where no token stands behind it."""
    if HOST in LOOPBACK_HOSTS:
        return False
    host = (headers.get("Host") or "").strip().lower()
    return bool(host) and urlparse(origin).netloc.lower() == host


def _host_is_this_machine(host_header: str, port: int) -> bool:
    """NC-309. Whether a request was addressed to this Mac, judged by its Host.

    Bound to loopback the app asks for no token, so the browser's Host header is
    the one thing that tells the app's own window from a web page that pointed
    its own name at this Mac (DNS rebinding). The window's requests say
    127.0.0.1:8765; a rebinding page's say evil.example:8765, and until this
    check every read answered both, the full export included.

    Off loopback the token guards every /api route instead, and the address a
    second device typed cannot be predicted, so this stands aside there. No Host
    at all is a non-browser caller speaking HTTP/1.0 — every browser sends one —
    and goes through, as the CLI does at the write gate."""
    if HOST not in LOOPBACK_HOSTS:
        return True
    host = host_header.strip().lower()
    if not host:
        return True
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
    else:
        name, _, port_text = host.partition(":")
    if name not in LOOPBACK_HOSTS:
        return False
    if not port_text:
        return port == 80
    return port_text.isdigit() and int(port_text) == port


# ── Import: subprocess wiring ───────────────────────────────────────────────────
# Export download: format -> (content-type, file extension).
EXPORT_CONTENT_TYPES = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "json": ("application/json; charset=utf-8", "json"),
}

# Static asset content types, by extension — only these are servable outside index.html.
STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}
# Folders under static/ that may be served (plus the PWA manifest at the root).
STATIC_PREFIXES = ("/css/", "/js/", "/app/", "/vendor/", "/fonts/")
STATIC_ROOT_FILES = {"/manifest.webmanifest", "/sw.js"}

INDEX_MISSING_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>Network CRM</title></head><body style='font-family:sans-serif'>"
    "<h1>index.html missing</h1>"
    "<p>The front-end file <code>static/index.html</code> was not found.</p>"
    "<p>The API is running at <code>/api/status</code>.</p>"
    "</body></html>"
)


# ── Errors ────────────────────────────────────────────────────────────────────
# ── Serialization helpers ─────────────────────────────────────────────────────
# ── Endpoint implementations ──────────────────────────────────────────────────
def _dismiss_db_recovery_notice(conn: Any) -> Dict[str, Any]:
    """Clear the "your database was moved aside" notice once the user has read it.

    POST /api/db-recovery-notice/dismiss. It changes one state row and nothing
    else. NC-342: it was a GET, and a read that changes something can be set off
    by a prefetch or a second window without anyone pressing the button.
    """
    db.state_set(conn, "db_recovery_notice", "")
    conn.commit()
    return {"ok": True}


def handle_status(conn: Any) -> Dict[str, Any]:
    c = api_contacts.status_counts(conn)
    return {
        "running": True,
        "mode": "local",
        "db_path": db.DB_PATH,
        "host": "" if SOCKET_PATH else HOST,
        "port": 0 if SOCKET_PATH else PORT,
        # NC-330: true when the app reaches this engine through its private file
        # rather than a network address.
        "private": bool(SOCKET_PATH),
        # NC-202: the fingerprint the app started this engine with (its code and
        # data folders). "" for an engine started any other way.
        "instance": os.environ.get("NC_INSTANCE", ""),
        # NC-203: which build of the bundle this server loaded, so opening the
        # app after a rebuild restarts it rather than showing the old version.
        "build": os.environ.get("NC_BUILD", ""),
        # whether AI features (Ask, story rewrites, drafts) are available, so
        # the UI can say "AI off — add a key" instead of degrading silently
        "ai": bool(os.environ.get("ANTHROPIC_API_KEY")),
        # Set only when startup found the database unreadable and moved it aside
        # (see _init_db_or_recover). It rides on /api/status because the shell
        # already polls that every 15s, and because printing this to a terminal
        # nobody is watching is how the .app would lose it entirely. Cleared by
        # POST /api/db-recovery-notice/dismiss when the user dismisses it.
        "db_recovery": db.state_get(conn, "db_recovery_notice", "") or "",
        "counts": {
            "total": c["total"],
            "new": c["new"],
            "described": c["described"],
            "skipped": c["skipped"],
            "excluded": c["excluded"],
        },
    }


# ── Today (the daily ritual) ──────────────────────────────────────────────────
# ── Attention queue (Today's spine) ─────────────────────────────────────────
# ── Episodes (conversation timeline) + story-so-far ──────────────────────────
# ── Saved lists / event pipelines ─────────────────────────────────────────────
# ── Settings (in-app secrets/.env editor) ───────────────────────────────────────
# ── Update check ────────────────────────────────────────────────────────────────
# ── Onboarding ──────────────────────────────────────────────────────────────────
# ── HTTP handler ──────────────────────────────────────────────────────────────
class CrmHandler(BaseHTTPRequestHandler):
    server_version = "NetworkCRM/1.0"
    protocol_version = "HTTP/1.1"
    # Whether this request's body has been read off the connection. One handler
    # object serves every request on a kept-alive connection, so do_GET and
    # _handle_write set it to False at the start of each request.
    _body_consumed = True

    # Keep the startup banner readable — drop the default per-request logging.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # ── low-level response helpers ────────────────────────────────────────────
    def _settle_unread_body(self) -> None:
        """NC-335. Take an unread request body off the connection before answering.

        A write refused before its body is read (wrong Content-Type, the Host,
        token or cross-origin checks) used to leave those bytes waiting on the
        connection. Browsers and the native app reuse a connection, so the NEXT
        request on it was read starting with that leftover body and failed for
        no visible reason. So the body is read and thrown away; and where that
        cannot be done safely (larger than any body this app accepts, a chunked
        body this server does not decode, a length it cannot read) the
        connection is closed after this answer instead, and the client opens a
        fresh one."""
        if self._body_consumed:
            return
        self._body_consumed = True
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length == 0:
            return
        if 0 < length <= MAX_BODY_BYTES:
            try:
                self.rfile.read(length)
                return
            except OSError:
                pass
        self.close_connection = True

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        self._settle_unread_body()
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, content: str, content_type: str, filename: str) -> None:
        """Send a file as an attachment so the browser downloads instead of rendering it."""
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        # Require application/json. This is not pedantry — it is what stops any
        # web page you happen to have open from driving this API. A cross-origin
        # POST with Content-Type text/plain is a CORS "simple request": the
        # browser sends it with no preflight and the side effect happens, even
        # though the attacker cannot read the reply. Demanding application/json
        # forces a preflight, which fails for want of an Access-Control-Allow-
        # Origin header this server never sends. The app's own fetch wrapper
        # (static/app/api.js) has always set this header.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                           "Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            # A chunked body has no length and is not read here, so it is still
            # on the connection: _settle_unread_body closes it after the answer.
            self._body_consumed = not self.headers.get("Transfer-Encoding")
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body too large")
        raw = self.rfile.read(length)
        self._body_consumed = True
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    def _serve_photo(self, contact_id: int) -> None:
        """Stream a contact's stored photo with long-lived caching (URLs carry a
        ?v= mtime, so replacing the file busts the cache naturally)."""
        data = photos.read_photo(contact_id)
        if data is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no photo")
        ctype = photos.sniff_content_type(data) or "image/jpeg"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(data)

    # ── index page ────────────────────────────────────────────────────────────
    def _serve_index(self) -> None:
        if not os.path.isfile(INDEX_PATH):
            self._send_html(HTTPStatus.OK, INDEX_MISSING_HTML)
            return
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as fh:
                html = fh.read()
        except OSError as exc:
            self._send_html(HTTPStatus.OK, INDEX_MISSING_HTML)
            self.log_error("could not read index.html: %s", exc)
            return
        # NC-062. A data-boot attribute was stamped here for the five-variant
        # picker. One animation now, scoped to #boot in boot.css, so the shell
        # is served exactly as it sits on disk and there is nothing to inject.
        self._send_html(HTTPStatus.OK, html)

    # ── static assets (css/js) ───────────────────────────────────────────────────
    def _serve_static_asset(self, path: str) -> bool:
        """Serve css/app-modules/vendor/fonts assets from static/. Returns True if handled.

        Only extensions in STATIC_CONTENT_TYPES under STATIC_PREFIXES (plus the PWA
        manifest) are servable, and the resolved path must stay inside STATIC_DIR —
        blocks `..` traversal outside the static folder.
        """
        ext = os.path.splitext(path)[1]
        content_type = STATIC_CONTENT_TYPES.get(ext)
        if content_type is None or not (path.startswith(STATIC_PREFIXES) or path in STATIC_ROOT_FILES):
            return False
        full = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(os.path.normpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        with open(full, "rb") as fh:
            body = fh.read()

        # Everything that is not a font or a vendored library — i.e. /css/ and
        # /app/, the two trees that change on every single build — must be
        # revalidated. Sending no Cache-Control AND no validator (which is what
        # this did) is the worst of both worlds: with nothing to revalidate
        # against, WebKit falls back to heuristic freshness and reuses a stale
        # stylesheet for an unbounded period. That silently defeats sw.js's
        # network-first strategy too, because the SW's own network fetch is the
        # thing being answered from the HTTP cache. Symptom: ship an update, and
        # the user keeps the old CSS.
        #
        # "no-cache" does not mean "do not store" — it means "store, but always
        # revalidate", so the ETag below turns the usual case into a 304 with no
        # body. Correct on the first load, and still nearly free.
        etag = None
        if not path.startswith(("/fonts/", "/vendor/")):
            etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return True

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if etag is not None:
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
        if path.startswith(("/fonts/", "/vendor/")):  # immutable-ish assets
            self.send_header("Cache-Control", "public, max-age=604800")
        if path == "/sw.js":
            # Service-Worker-Allowed header lets the SW control "/" even when
            # sw.js is served from "/" — required for standalone PWA installs.
            # Its Cache-Control: no-cache now comes from the etag branch above,
            # which covers every non-font/vendor path — sending it here too
            # would just duplicate the header.
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(body)
        return True

    # ── export download ─────────────────────────────────────────────────────────
    def _handle_export(self, query: Dict[str, List[str]]) -> None:
        """Build a full CRM export and stream it as a file download (not JSON)."""
        fmt = (query.get("format", ["md"])[0] or "md").strip().lower()
        if fmt not in EXPORT_CONTENT_TYPES:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                f"format must be one of {sorted(EXPORT_CONTENT_TYPES)}",
            )
        content = self._with_conn(lambda c: export_data.build_export(c, fmt))
        content_type, ext = EXPORT_CONTENT_TYPES[fmt]
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._send_download(content, content_type, f"network-crm-export-{date}.{ext}")

    # ── auth (only when bound off-loopback) ─────────────────────────────────────
    def _require_api_auth(self, path: str, query: Dict[str, List[str]]) -> None:
        """Guard /api routes with a bearer token when the server is bound to the
        LAN (APP_HOST != loopback). Loopback-only mode — the zero-config default
        — skips this entirely. The token may arrive as an `Authorization: Bearer
        <token>` header or a `?token=` query param (so the laptop can open
        http://<server-ip>:8765/?token=... directly).

        NC-140: this used to begin `if HOST in LOOPBACK_HOSTS or not APP_TOKEN`,
        so leaving APP_TOKEN unset switched the lock OFF instead of refusing —
        the one configuration nobody would pick on purpose was the one that let
        the whole Wi-Fi read and rewrite the CRM. `main()` now refuses to bind in
        that state at all; this branch is the second lock, covering any caller
        that builds the server without going through main().

        The page itself and the static assets stay open on purpose: they are
        served before this runs, so the browser can load and show the overlay in
        api.js that says where the token goes. Gating them would leave whoever
        opened the address with a bare 401 and no way to read what to do."""
        if HOST in LOOPBACK_HOSTS:
            return
        if not path.startswith("/api/"):
            return
        if not APP_TOKEN:
            raise ApiError(
                HTTPStatus.UNAUTHORIZED,
                "this copy is being served on a network with no APP_TOKEN set, so "
                "it cannot let anyone in. Set APP_TOKEN in secrets/.env on the "
                "machine running it and restart.")
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:].strip() if auth.startswith("Bearer ") else (query.get("token", [""])[0])
        if supplied != APP_TOKEN:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid token")

    def _reject_foreign_host(self) -> None:
        """NC-309. Refuse anything not addressed to this Mac, before routing, so
        the page, the static files and every /api route are covered alike. See
        _host_is_this_machine for why the Host header is the check.

        NC-330: on the app's private file there is no address to check. No web
        page can open a file, so the rebinding this guards against cannot reach
        it, and the file itself opens only for this account (PrivateSocketServer)."""
        if getattr(self.server, "private_socket", False):
            return
        if not _host_is_this_machine(self.headers.get("Host") or "",
                                     self.server.server_address[1]):
            raise ApiError(HTTPStatus.FORBIDDEN,
                           "this app only answers requests addressed to this Mac")

    def _reject_cross_site_read(self, path: str) -> None:
        """NC-323. A page on another site can still make the browser LOAD an /api
        address without reading it, as an <img> or a link: enough to dismiss the
        database-recovery notice (a GET with a side effect) or to learn which
        contact numbers have a photo from which images load. Browsers label those
        loads Sec-Fetch-Site: cross-site. The app's own window sends same-origin,
        a typed address sends none, and a non-browser caller sends nothing."""
        if not path.startswith("/api/"):
            return
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            raise ApiError(HTTPStatus.FORBIDDEN, "other sites cannot load this app's data")

    # ── routing ───────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        # No GET reads a body, so one sent anyway comes off the connection first,
        # whichever kind of answer follows (NC-335).
        self._body_consumed = False
        self._settle_unread_body()
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._reject_foreign_host()
            if path == "/" or path == "/index.html":
                self._serve_index()
                return
            if self._serve_static_asset(path):
                return
            query = parse_qs(parsed.query)
            self._require_api_auth(path, query)
            self._reject_cross_site_read(path)
            if path == "/api/export":
                self._handle_export(query)
                return
            m = re.fullmatch(r"/api/list/(\d+)/export", path)
            if m:
                content, name = self._with_conn(lambda c: _list_export_csv(c, int(m.group(1))))
                safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or ("list-" + m.group(1))
                self._send_download(content, "text/csv; charset=utf-8", safe + ".csv")
                return
            m = re.fullmatch(r"/api/photo/(\d+)", path)
            if m:
                self._serve_photo(int(m.group(1)))
                return
            payload = self._route_get(path, query)
            if payload is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            self._send_json(HTTPStatus.OK, payload)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception:  # noqa: BLE001 - last-resort 500
            # NC-363: the exception goes to the log, never into the answer, where it
            # would reach a screen as program text (or carry someone's name).
            log.exception("unhandled error on %s %s", self.command, self.path.split("?", 1)[0])
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": INTERNAL_ERROR_SENTENCE})

    def do_POST(self) -> None:  # noqa: N802
        self._handle_write("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_write("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_write("DELETE")

    def _reject_cross_origin(self) -> None:
        """Refuse a write that some other site's page initiated.

        Loopback needs no bearer token (that is the zero-config default), which
        means every write endpoint is reachable by anything running in a browser
        on this machine — including a page on an unrelated domain. Belt to the
        Content-Type gate's braces, and the part that also covers DELETE, which
        carries no body. Browsers send Origin on same-origin writes too, so an
        absent header means a non-browser caller (curl, the CLI) and is allowed;
        a present one has to be ours."""
        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin not in ALLOWED_ORIGINS and not _origin_is_this_address(origin, self.headers):
            raise ApiError(HTTPStatus.FORBIDDEN, "cross-origin writes are not allowed")
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            raise ApiError(HTTPStatus.FORBIDDEN, "cross-site writes are not allowed")

    def _handle_write(self, method: str) -> None:
        self._body_consumed = False
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._reject_foreign_host()
            self._require_api_auth(path, parse_qs(parsed.query))
            self._reject_cross_origin()
            body = self._read_json_body() if method in ("POST", "PUT") else {}
            payload = self._route_write(method, path, body)
            if payload is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            self._send_json(HTTPStatus.OK, payload)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception:  # noqa: BLE001 - last-resort 500
            # NC-363: the exception goes to the log, never into the answer, where it
            # would reach a screen as program text (or carry someone's name).
            log.exception("unhandled error on %s %s", self.command, self.path.split("?", 1)[0])
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": INTERNAL_ERROR_SENTENCE})

    # ── route tables ──────────────────────────────────────────────────────────
    def _route_get(self, path: str, query: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
        if path == "/api/status":
            return self._with_conn(lambda c: handle_status(c))
        if path == "/api/db-recovery-notice/dismiss":
            # NC-342: dismissing changes something, so it is a POST now. A GET
            # here is a caller built for the old route; say so rather than 404.
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED,
                           "dismissing the notice is a POST to this address")
        # GET /api/today and GET /api/feed are gone with the Today surface
        # (NC-009). /api/describe-queue is what survives of the first: the
        # onboarding wizard's third step used to ask /api/today for a whole
        # dashboard and keep three entries of it.
        if path == "/api/describe-queue":
            return self._with_conn(lambda c: handle_describe_queue(c))
        if path == "/api/contacts":
            return self._with_conn(lambda c: handle_list_contacts(c, query))
        if path == "/api/search/semantic":
            return self._with_conn(lambda c: handle_semantic_search(c, query))
        if path == "/api/search/conversations":
            return self._with_conn(lambda c: handle_conversation_search(c, query))
        if path == "/api/reconnect":
            return self._with_conn(lambda c: handle_reconnect(c, query))
        if path == "/api/duplicates":
            return self._with_conn(lambda c: handle_duplicates(c))
        if path == "/api/exclude-list":
            return self._with_conn(lambda c: handle_exclude_list_get(c))
        if path == "/api/import/sources":
            return self._with_conn(lambda c: handle_import_sources(c))
        if path == "/api/import/detect":
            return self._with_conn(lambda c: handle_import_detect(c))
        if path == "/api/locations":
            return self._with_conn(lambda c: handle_locations(c))
        if path == "/api/facets":
            return self._with_conn(lambda c: handle_facets(c))
        if path == "/api/settings":
            return self._with_conn(lambda c: handle_settings_get(c))
        if path == "/api/diagnostics":
            return self._with_conn(lambda c: handle_diagnostics_get(c))
        if path == "/api/onboarding":
            return self._with_conn(lambda c: handle_onboarding_get(c))
        if path == "/api/update":
            # NC-237: no page asks this today. Kept on purpose: it is what update
            # controls will ask once releases are published (NC-118).
            return self._with_conn(lambda c: handle_update_get(c, query))
        if path == "/api/chats":
            return self._with_conn(lambda c: handle_chats_get(c))
        # The legacy alias, kept literal and kept ABOVE the /api/chat/(\d+)/history
        # pattern below. It means "the newest chat" now (handle_chat_history_get
        # resolves that itself). A packaged build older than chats still calls it:
        # the product is the .app, which people keep, so a stale bundle is a live
        # client. It must not 404.
        if path == "/api/chat/history":
            return self._with_conn(lambda c: handle_chat_history_get(c))
        m = re.fullmatch(r"/api/chat/(\d+)/history", path)
        if m:
            conv_id = int(m.group(1))
            return self._with_conn(lambda c: handle_chat_history_get(c, conv_id))
        if path == "/api/lists":
            return self._with_conn(lambda c: handle_lists_get(c))
        m = re.fullmatch(r"/api/list/(\d+)", path)
        if m:
            lid = int(m.group(1))
            return self._with_conn(lambda c: handle_list_detail(c, lid))
        m = re.fullmatch(r"/api/contact/(\d+)", path)
        if m:
            cid = int(m.group(1))
            return self._with_conn(lambda c: handle_get_contact(c, cid))
        # /history goes through _with_conn now (NC-106): threads=1 shows other
        # people's lines in a shared conversation, and naming them needs crm.db.
        # Without a connection the handler ignores threads=1 rather than serve
        # anonymous lines, so passing it here is what turns the feature on.
        # /episodes deliberately does not — see its docstring. It only borrows
        # one long enough to answer 404 for a person who does not exist (NC-340),
        # rather than an empty list that reads as someone you never wrote to.
        m = re.fullmatch(r"/api/contact/(\d+)/history", path)
        if m:
            cid = int(m.group(1))
            return self._with_conn(lambda c: handle_get_history(cid, query, c))
        m = re.fullmatch(r"/api/contact/(\d+)/episodes", path)
        if m:
            cid = int(m.group(1))
            self._with_conn(lambda c: _require_contact(c, cid))
            return handle_get_episodes(cid, query)
        return self._route_registered("GET", path, query)

    def _route_write(
        self, method: str, path: str, body: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        cid, action = self._parse_contact_action(path)
        if method == "POST":
            if path == "/api/contacts":
                return self._with_conn(lambda c: handle_create_contact(c, body))
            if path == "/api/merge":
                return self._with_conn(lambda c: handle_merge(c, body))
            if path == "/api/ask":
                return self._with_conn(lambda c: handle_ask(c, body))
            if path == "/api/chat":
                return self._with_conn(lambda c: handle_chat(c, body))
            if path == "/api/chats":
                return self._with_conn(lambda c: handle_chats_post(c, body))
            m = re.fullmatch(r"/api/chat/(\d+)/rename", path)
            if m:
                conv_id = int(m.group(1))
                return self._with_conn(lambda c: handle_chat_rename(c, conv_id, body))
            if path == "/api/import/run":
                return handle_import_run(body)
            if path == "/api/import/pick-file":
                return handle_import_pick_file(body)
            if path == "/api/import/open":
                return handle_import_open(body)
            if path == "/api/exclude-list/remove":
                return self._with_conn(lambda c: handle_exclude_list_remove(body, c))
            if path == "/api/db-recovery-notice/dismiss":
                return self._with_conn(_dismiss_db_recovery_notice)
            if path == "/api/exclude-list/add":
                return self._with_conn(lambda c: handle_exclude_list_add(c, body))
            if path == "/api/settings":
                return handle_settings_post(body)
            if path == "/api/semantic/install":
                # Installs the ~2GB semantic stack on demand. Returns immediately;
                # the work runs on a background thread and progress is polled from
                # the diagnostics payload. No _with_conn: it touches no crm.db.
                return api_instance.handle_semantic_install(body)
            if path == "/api/onboarding":
                return self._with_conn(lambda c: handle_onboarding_post(c, body))
            if path == "/api/lists":
                return self._with_conn(lambda c: handle_list_create(c, body))
            m = re.fullmatch(r"/api/list/(\d+)/(add|member|rename|meta)", path)
            if m:
                lid, list_action = int(m.group(1)), m.group(2)
                fn = {"add": handle_list_add, "member": handle_list_member,
                      "rename": handle_list_rename, "meta": handle_list_meta}[list_action]
                return self._with_conn(lambda c: fn(c, lid, body))
            if cid is not None:
                return self._dispatch_contact_post(cid, action, body)
        if method == "PUT":
            nid = self._parse_note_id(path)
            if nid is not None:
                return self._with_conn(lambda c: handle_update_note(c, nid, body))
        if method == "DELETE":
            # Legacy alias first, and literal, so the digit patterns below cannot
            # shadow it. It clears the NEWEST chat, not every chat — see
            # handle_chat_history_clear for why widening it would be a bug.
            if path == "/api/chat/history":
                return self._with_conn(lambda c: handle_chat_history_clear(c))
            m = re.fullmatch(r"/api/chat/(\d+)/history", path)
            if m:
                conv_id = int(m.group(1))
                return self._with_conn(lambda c: handle_chat_history_clear(c, conv_id))
            m = re.fullmatch(r"/api/chat/(\d+)", path)
            if m:
                conv_id = int(m.group(1))
                return self._with_conn(lambda c: handle_chat_delete(c, conv_id))
            m = re.fullmatch(r"/api/list/(\d+)", path)
            if m:
                lid = int(m.group(1))
                return self._with_conn(lambda c: handle_list_delete(c, lid))
            if cid is not None and action == "photo":
                return self._with_conn(lambda c: handle_delete_photo(c, cid))
            nid = self._parse_note_id(path)
            if nid is not None:
                return self._with_conn(lambda c: handle_delete_note(c, nid))
        return self._route_registered(method, path, body)

    def _dispatch_contact_post(
        self, cid: int, action: Optional[str], body: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        status = REVIEW_STATUS_ACTIONS.get(action or "")
        if status is not None:
            return self._with_conn(lambda c: handle_review_status(c, cid, status))
        routes = {
            "note": handle_add_note,
            "category": handle_set_category,
            "exclude": handle_set_excluded,
            "howwemet": handle_set_howwemet,
            "field": handle_set_field,
            "identifier": handle_add_identifier,
            "photo": handle_set_photo,
            "delete": handle_delete_contact,
            "purge": handle_purge_contact,
            "dates": handle_set_date,
        }
        if action == "brief":
            return self._with_conn(lambda c: handle_make_brief(c, cid))
        if action == "draftmessage":
            return self._with_conn(lambda c: handle_draft_message(c, cid))
        if action == "reconnectskip":
            return self._with_conn(lambda c: handle_reconnect_skip(c, cid))
        fn = routes.get(action or "")
        if fn is None:
            return None
        return self._with_conn(lambda c: fn(c, cid, body))

    def _route_registered(
        self, method: str, path: str, payload: Any
    ) -> Optional[Dict[str, Any]]:
        """Dispatch to a ROUTE_MODULES module, or None if none claims this path.

        `payload` is the parsed query for GET and the JSON body for a write —
        whichever one this method's handlers take as their data argument.
        """
        for mod in ROUTE_MODULES:
            table = getattr(mod, "ROUTES", {}).get(method) or {}
            name = table.get(path)
            if not name:
                continue
            fn: Any = getattr(mod, name, None)
            if not callable(fn):
                # A ROUTES entry naming a handler that does not exist is a bug in
                # that module, not a bad request — say so instead of 404ing.
                raise ApiError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"{mod.__name__}.ROUTES points at '{name}', which is not callable",
                )
            params = list(inspect.signature(fn).parameters)
            wants_conn = bool(params) and params[0] == "conn"
            wants_payload = len(params) > (1 if wants_conn else 0)
            if wants_conn:
                if wants_payload:
                    return self._with_conn(lambda c: fn(c, payload))
                return self._with_conn(lambda c: fn(c))
            return fn(payload) if wants_payload else fn()
        return None

    @staticmethod
    def _parse_contact_action(path: str) -> Tuple[Optional[int], Optional[str]]:
        m = re.fullmatch(r"/api/contact/(\d+)/([a-z]+)", path)
        if not m:
            return None, None
        return int(m.group(1)), m.group(2)

    @staticmethod
    def _parse_note_id(path: str) -> Optional[int]:
        m = re.fullmatch(r"/api/note/(\d+)", path)
        return int(m.group(1)) if m else None

    @staticmethod
    def _with_conn(fn: Any) -> Dict[str, Any]:
        _hold_databases_open()
        conn = db.get_db()
        try:
            return fn(conn)
        finally:
            conn.close()


def _init_db_or_recover() -> str:
    """Open the database, and if the file is unusable, move it aside and start fresh.

    NC-011: this was a bare `db.init_db()`. A malformed file — an interrupted
    write, a half-finished restore, a full disk — raised here, before the HTTP
    server existed, so the app simply never opened and there was no in-app way
    back. The user's only recourse was a terminal, which is the one thing the
    packaged .app exists to avoid.

    Renaming rather than deleting, always: a corrupt SQLite file is still the only
    copy of those notes, and a later version of this app (or `sqlite3 .recover`)
    may well get them out. Losing them quietly to make startup work would be a
    much worse bug than the one this fixes.

    Returns "" when all was well, or a human-readable notice naming the file it
    moved and where it went.
    """
    import shutil
    try:
        db.init_db()
        return ""
    except Exception as first:  # noqa: BLE001 — sqlite3.DatabaseError, OSError, any
        path = db.DB_PATH
        if not os.path.exists(path):
            raise                     # not a corrupt file: a real, un-recoverable fault
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        aside = f"{path}.unreadable-{stamp}"
        try:
            shutil.move(path, aside)
            for sidecar in ("-wal", "-shm"):
                if os.path.exists(path + sidecar):
                    shutil.move(path + sidecar, aside + sidecar)
        except OSError:
            raise first from None     # cannot even move it — nothing safe left to try
        db.init_db()                  # a fresh, empty database in its place
        notice = (f"Your database could not be read ({first}). It has been moved to "
                  f"{aside} and a new empty one created. Nothing was deleted.")
        print("\n" + notice + "\n", flush=True)
        return notice


# NC-212. One idle connection to each database, held for as long as the server
# runs. When a connection opens a database that no other connection has open,
# SQLite rebuilds the database's index file from scratch. The server opens a
# connection per request, and the window asks for /api/status every 15 seconds,
# so without a held connection every request would write to disk for nothing.
# With one connection held, an open writes nothing, and that holds for the
# importers' own processes too.
#
# The held connections are never used, so they never hold a transaction open and
# never stop the log being folded back in. A database that does not exist yet
# is skipped, because opening it would create it; the next request tries again,
# which is how archive.db gets held after a fresh install's first import.
#
# Off until main() switches it on, so a test process that handles requests
# against a new temporary database each time does not collect connections.
_HOLD_DATABASES = False
_HELD_CONNECTIONS: Dict[str, Any] = {}


def _hold_databases_open() -> None:
    if not _HOLD_DATABASES:
        return
    for path, opener in ((db.DB_PATH, db.get_db), (arc.ARCHIVE_DB_PATH, arc.get_archive_db)):
        if path in _HELD_CONNECTIONS or not os.path.exists(path):
            continue
        try:
            conn = opener(path)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except Exception:  # noqa: BLE001 — a convenience; the request opens its own anyway
            continue
        _HELD_CONNECTIONS[path] = conn


# ── Entrypoint ────────────────────────────────────────────────────────────────
def _open_lan_refusal(host: str, token: str) -> str:
    """NC-140. The message that must stop startup, or "" to go ahead.

    Serving on the network with no token is not a weaker setting — it is no
    setting. Every device on the Wi-Fi can read and rewrite the whole CRM. The
    old code read an empty APP_TOKEN as "no check wanted", served anyway, and
    printed a warning; the packaged .app has no terminal, so that warning
    reached nobody. Refusing is the only version of this that works on the
    surface people actually use.

    Takes its two values as arguments rather than reading the globals, so the
    tests can ask it about a configuration this process is not in.
    """
    if host in LOOPBACK_HOSTS or token.strip():
        return ""
    env_file = os.path.join(config.SECRETS_DIR, ".env")
    return (
        "Network CRM did not start.\n"
        "\n"
        f"APP_HOST is set to {host}, which would serve your contacts to every\n"
        "device on this network — but APP_TOKEN is empty, so nothing would ask\n"
        "those devices who they are. Anyone on the same Wi-Fi could read and\n"
        "change the whole CRM.\n"
        "\n"
        "Pick one and start it again:\n"
        "\n"
        "  Serve only this Mac — the default, and it needs no token:\n"
        "      unset APP_HOST\n"
        "\n"
        "  Serve this network, behind a password:\n"
        "      make a long random one with   openssl rand -hex 16\n"
        f"      put it in {env_file} as\n"
        "          APP_TOKEN=<that value>\n"
        f"      then open   http://<this-machine-ip>:{PORT}/?token=<that value>\n"
    )


# ── NC-330: the app's private connection ───────────────────────────────────────
# The packaged app starts this engine as its own child and reaches it through a
# socket FILE in the app's own folder, not a network address. Nothing on this Mac
# can open a URL to it, a browser least of all; the file opens only for this
# account (umask 077, and its folder is 0700); and when the app goes, by Cmd-Q or a
# crash, the pipe on this process's standard input closes and the engine stops,
# taking the import programs it started with it. Running from the source code
# still serves 127.0.0.1:PORT for a browser, as before.
SOCKET_PATH = ""


class PrivateSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """The same CrmHandler, served on a socket file instead of a port."""
    daemon_threads = True
    private_socket = True

    def get_request(self):  # noqa: D401 — socketserver's name
        # A socket file has no client address; BaseHTTPRequestHandler expects a
        # (host, port) pair wherever it names the caller.
        sock, _addr = super().get_request()
        return sock, ("this-mac", 0)


def _socket_answers(path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def open_private_socket(path: str) -> PrivateSocketServer:
    """Bind the private socket file, refusing to start beside a live engine.

    A file left behind by an engine that was killed answers nothing and is
    removed. One that answers belongs to an engine already holding this data, and
    a second engine beside it would run its own automatic imports into the same
    databases, so this refuses instead. Anything at the path that is not a socket
    is left alone and refused."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    if os.path.lexists(path):
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise SystemExit(f"{path} exists and is not this app's connection file; "
                             "not touching it.")
        if _socket_answers(path):
            raise SystemExit("Another Network CRM engine is already running for this "
                             "data. Quit the other copy of the app first.")
        os.unlink(path)
    old = os.umask(0o077)
    try:
        server = PrivateSocketServer(path, CrmHandler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return server


def _stop_from_another_thread(server: Any) -> None:
    # serve_forever() returns only when shutdown() is called from a different
    # thread; a signal handler runs on the serving thread itself.
    threading.Thread(target=server.shutdown, daemon=True, name="shutdown").start()


def _watch_parent(server: Any) -> None:
    """Stop when the app that started this engine goes away.

    The app holds a pipe on this process's standard input and never writes to it.
    Cmd-Q, a crash or a force quit all close it, and read() then returns nothing.
    os.read, not sys.stdin: a thread still blocked inside the buffered reader at
    exit holds its lock, and Python aborts the whole process over it."""
    try:
        while os.read(0, 1):
            pass
    except (OSError, ValueError):
        pass
    _stop_from_another_thread(server)


def _stop_children() -> None:
    """Stop the import programs this engine started, so quitting the app leaves
    nothing running. Only when this engine leads its own process group (main()
    makes it so in private mode); otherwise the group is the app's."""
    try:
        if os.getpgrp() != os.getpid():
            return
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except OSError:
        pass


def _parse_args(argv: List[str]) -> Any:
    import argparse
    parser = argparse.ArgumentParser(prog="crm/app.py", add_help=True)
    parser.add_argument("--socket", default="",
                        help="serve on this socket file instead of 127.0.0.1:PORT "
                             "(the packaged app's private connection)")
    parser.add_argument("--watch-stdin", action="store_true",
                        help="stop when standard input closes (the app that "
                             "started this engine went away)")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main() -> None:
    global SOCKET_PATH
    args = _parse_args(sys.argv[1:])
    SOCKET_PATH = os.path.abspath(os.path.expanduser(args.socket)) if args.socket else ""
    if SOCKET_PATH:
        # Lead a process group of its own, so _stop_children reaches exactly the
        # programs this engine started and never the app above it.
        try:
            os.setpgrp()
        except OSError:
            pass
    # NC-321. Every file the engine makes from here on is readable by this
    # account only: the databases and their side files, exports, copies. Set
    # here rather than by whatever starts the engine, so the app, start.command
    # and `python3 crm/app.py` all get it.
    os.umask(0o077)
    refusal = _open_lan_refusal(HOST, APP_TOKEN)
    if refusal:
        # Before init_db, before the autosync thread, and well before the socket:
        # a server that is not going to run should not first touch the database
        # or start importing in the background.
        print(refusal, file=sys.stderr, flush=True)
        raise SystemExit(2)
    recovery_notice = _init_db_or_recover()
    global _HOLD_DATABASES
    _HOLD_DATABASES = True
    _hold_databases_open()
    if recovery_notice:
        try:
            conn = db.get_db()
            db.state_set(conn, "db_recovery_notice", recovery_notice)
            conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001 — the notice is a bonus, the app is the point
            pass
    try:  # fold any archive changes since last run into the familiarity features
        conn = db.get_db()
        engagement.refresh(conn)
        conn.close()
    except Exception:  # noqa: BLE001 — never block startup on a stats refresh
        pass
    try:
        # NC-147. Files this project writes are hardened where they are written.
        # This is for the ones it does not write: a Google OAuth client file is
        # downloaded from Google Cloud and dropped in by hand, arriving 0644
        # with a client id and secret in it.
        tightened = config.harden_secrets_dir()
        if tightened:
            print(f"Made {tightened} credential file(s) readable only by you.",
                  flush=True)
        # NC-321: databases made before the umask above, on a source checkout.
        tightened = config.harden_databases()
        if tightened:
            print(f"Made {tightened} database file(s) readable only by you.",
                  flush=True)
    except Exception:  # noqa: BLE001 — never block startup on a permission bit
        pass
    try:
        # NC-146. Before the autosync thread, because the autosync thread is what
        # runs the importers, and the importers read exclude_list.txt rather than
        # the `excluded` column. Deletes made before that mirror existed left the
        # file short of the column; this is how it catches up, and on every run
        # after that it adds nothing.
        conn = db.get_db()
        added = db.mirror_excluded_to_file(conn, EXCLUDE_FILE)
        conn.close()
        if added:
            print(f"Excluded {added} name(s) the importers had not been told about.",
                  flush=True)
    except Exception:  # noqa: BLE001 — never block startup on a list the UI can fix
        pass
    if SOCKET_PATH:
        # Before the autosync thread, like the LAN refusal above: an engine that
        # will not run must not first start importing.
        httpd = open_private_socket(SOCKET_PATH)
    threading.Thread(target=_autosync_loop, daemon=True,
                     name="autosync").start()
    if SOCKET_PATH:
        _serve_private(httpd, watch_stdin=args.watch_stdin)
        return
    httpd = ThreadingHTTPServer((HOST, PORT), CrmHandler)
    loopback = HOST in LOOPBACK_HOSTS
    open_url = f"http://{'127.0.0.1' if loopback else HOST}:{PORT}"
    if loopback:
        print("\nNetwork CRM is running LOCALLY at " + open_url, flush=True)
        print("Your data stays on this Mac. Close this window (or press Ctrl+C) to stop.\n", flush=True)
    else:
        # No `if APP_TOKEN:` here any more — _open_lan_refusal above means we
        # never reach this line without one. The warning that used to live in
        # the else branch was the whole bug (NC-140): a printed warning is not a
        # lock, and in the packaged .app it is not even a warning.
        print(f"\nNetwork CRM is serving on the LAN at http://{HOST}:{PORT}", flush=True)
        print(f"Browse from another device to:  http://<this-machine-ip>:{PORT}/?token={APP_TOKEN}", flush=True)
        print("A bearer token is required on all /api routes.\n", flush=True)
    if loopback and not os.environ.get("NC_NO_BROWSER"):
        try:
            webbrowser.open(open_url)
        except Exception:  # noqa: BLE001 - opening a browser must never crash the server
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("\nStopped.")
        sys.exit(0)


def _serve_private(httpd: Any, watch_stdin: bool) -> None:
    """Serve the app's private socket until the app quits, then clean up."""
    signal.signal(signal.SIGTERM, lambda *_: _stop_from_another_thread(httpd))
    if watch_stdin:
        threading.Thread(target=_watch_parent, args=(httpd,), daemon=True,
                         name="parent-watch").start()
    print("Network CRM's engine is running privately for the app "
          f"({SOCKET_PATH}).", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        _stop_children()
        print("Stopped.", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
