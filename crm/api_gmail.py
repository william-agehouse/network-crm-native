#!/usr/bin/env python3
"""Gmail's setup, as something a person can do from the screen.

NC-117. The instructions on the Data page said, verbatim, "Click **Connect**
below — a browser tab opens for Google sign-in." There was no Connect control
anywhere in the front end, and there never had been. The same help named
`secrets/gmail_credentials_<slug>.json` — a folder the app never opens for you,
under a name derived by a regular expression nobody can see — and pointed at a
"README §Setup" that does not exist. So the one source in this app that could
reach somebody's whole correspondence was unreachable to anyone not already
holding the answer.

WHAT THIS MODULE IS
Three endpoints and the state behind them:

    GET  /api/gmail/status       what is set up, what is missing, what is waiting
    POST /api/gmail/credentials  take the file Google gave you, put it where the
                                 app looks, under the name the app expects
    POST /api/gmail/connect      sign in — and say so while it waits

THE SHAPE IT COPIES
The semantic-search install (crm/api_instance.py:330-511 plus
static/app/settings.js's SemanticSearch): a POST that returns at once, work on a
daemon thread, state in memory, a status endpoint the page polls every couple of
seconds and stops polling the moment it settles. It is copied rather than shared
because the two have almost nothing in common underneath — one is pip, one is a
person clicking Allow — and the thing worth being consistent about is what the
screen does, not what the plumbing looks like.

WHY SIGNING IN IS ITS OWN ACTION
`gmail_import.py --connect` obtains the permission and stops: no database, no
contacts, no mail. Before this, the only way to trigger the consent flow was to
run a full three-year sweep of the mailbox, which meant the app could not
honestly say what it was waiting for — "importing" covered both the ten seconds
a person spends clicking Allow and the several minutes of fetching afterwards.
Split, the waiting state is true: the only thing happening is a Google tab.

WHAT IT REFUSES TO DO
It never asks for a broad macOS permission, and it never reads the secrets
folder looking for something that might be a credential. The file arrives one
way only: the person picks it in a native dialog, and this module copies that
one path. That is the same shape as every other file this app takes in — the
person chooses, the app reads what they chose — and it is the reason none of
this needs Full Disk Access.
"""

import json
import os
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

import api_import
import config
import db
import gmail_accounts
import importers
import subprocess
import sys
from api_core import ApiError

ROUTES = {
    "GET": {"/api/gmail/status": "handle_gmail_status"},
    "POST": {"/api/gmail/credentials": "handle_gmail_credentials",
             "/api/gmail/connect": "handle_gmail_connect"},
}

# How long a sign-in may stay unfinished before the app stops waiting on it. It
# is a person reading a consent screen, not a machine, so this is generous —
# but it is not unbounded, because `run_local_server` has no timeout of its own
# (that is the bug NC-177 ran into from the other direction) and a subprocess
# holding a listening socket forever is not something to leave behind.
#
# Exposed, because it is a waiting time somebody can feel: NC_GMAIL_CONNECT_
# TIMEOUT_SEC in the environment moves it. Clamped 60s–1h.
CONNECT_TIMEOUT_DEFAULT = 600

# Last lines of the child's own output, kept so a failure can show what it said
# rather than a shrug. Same size as the semantic install's tail.
_TAIL_LINES = 40

_lock = threading.Lock()
# In memory on purpose, exactly like the semantic install's: a restart during a
# sign-in just clears "running", and the real answer — is there a token file —
# is recomputed from disk on the next status read. Nothing here is worth
# persisting, because the thing being tracked cannot survive a restart either
# (the local redirect server the child is holding open dies with it).
_state: Dict[str, Any] = {
    "state": "idle",        # idle | running | succeeded | failed
    "running": False,
    "account": "",
    "message": "",
    "tail": [],
    "started_at": "",
    "finished_at": "",
}


def connect_timeout_sec() -> int:
    """Seconds a sign-in may stay unfinished (NC_GMAIL_CONNECT_TIMEOUT_SEC).

    Read at call time, like import_timeout_sec, so a value hand-edited into
    secrets/.env applies without a restart."""
    try:
        secs = int(os.environ.get("NC_GMAIL_CONNECT_TIMEOUT_SEC",
                                  CONNECT_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        secs = CONNECT_TIMEOUT_DEFAULT
    return max(60, min(3600, secs))


# How long the app waits for macOS to let it read a file the person picked
# (NC-355). A file in Downloads, Desktop or Documents sits behind a macOS question
# — "may this app open files in that folder?" — and a read that reaches it waits
# for the answer instead of failing. A person who is not looking at the screen
# would otherwise leave the whole request hanging. Exposed because it is a wait
# somebody can feel: NC_PICKED_FILE_WAIT_SEC moves it. Clamped 1s-2min.
PICKED_FILE_WAIT_DEFAULT = 8


def picked_file_wait_sec() -> int:
    """Seconds to wait for a picked file to be read (NC_PICKED_FILE_WAIT_SEC),
    read at call time like connect_timeout_sec."""
    try:
        secs = int(os.environ.get("NC_PICKED_FILE_WAIT_SEC",
                                  PICKED_FILE_WAIT_DEFAULT))
    except (TypeError, ValueError):
        secs = PICKED_FILE_WAIT_DEFAULT
    return max(1, min(120, secs))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── What is set up ───────────────────────────────────────────────────────────

def _account_rows() -> List[Dict[str, Any]]:
    """One row per configured address: what it has, and what it is missing.

    Two separate facts, and conflating them is how this got confusing in the
    first place. The CLIENT FILE is what Google gives you once, from the Cloud
    Console, and it says which application is asking. The TOKEN is what you get
    back after clicking Allow, and it says you said yes. You need both, in that
    order, and only the first can be prepared in advance.
    """
    rows = []
    for email, creds in gmail_accounts.accounts_from_env().items():
        token = gmail_accounts.token_path(email)
        rows.append({
            "email": email,
            # The basename only. The absolute path is this person's own home
            # directory and there is nothing to do with it on screen; the folder
            # has its own button.
            "credentials_file": os.path.basename(creds),
            "has_credentials": os.path.exists(creds),
            "has_token": os.path.exists(token),
        })
    return rows


def _stale(conn: Any, rows: List[Dict[str, Any]]) -> List[str]:
    """Addresses whose saved permission Google has taken back.

    NC-183. Revocation leaves the token file exactly where it was — Google has
    no way to reach into a Mac and delete it — so a file on disk is evidence
    that somebody once signed in, and evidence of nothing about now. Judging
    readiness by the file alone made the panel print "Ready to collect" directly
    above a row saying "Gmail needs you to sign in again", and withheld the Sign
    in button at the one moment it was built for.

    The 12-hourly loop already knows: it gets exit 4 from the child and writes
    both a sentence and the addresses the child named (NC-177, NC-181). This
    reads that.

    When the marker is set but names nobody — the child stops at the first
    account needing consent and does not always print which — every account that
    HAS a token is treated as stale. Offering a sign-in to an account that turns
    out to be fine costs one extra trip through a Google tab. Withholding it
    from the one that is broken costs a source that silently never collects
    again, and that is the failure to avoid.
    """
    if conn is None:
        return []
    try:
        if not db.state_get(conn, api_import.NEEDS_SIGN_IN_KEY + "gmail", ""):
            return []
        named = [a.lower() for a in
                 api_import.needs_sign_in_accounts(conn, "gmail")]
    except Exception:  # noqa: BLE001 — a page that cannot read this still renders
        return []
    have_token = [r["email"] for r in rows if r["has_token"]]
    if not named:
        return have_token
    return [e for e in have_token if e in named]


def status(conn: Any = None) -> Dict[str, Any]:
    """Everything the Gmail panel needs, in one read.

    `conn` is optional because most of this is files on disk and a caller with
    no database still gets a true answer about them. What it cannot get without
    one is NC-183's stale list, and the default is the safe direction: an
    account is called ready rather than being sent to sign in again for no
    reason.
    """
    rows = _account_rows()
    stale = set(_stale(conn, rows))
    with _lock:
        connect = dict(_state)
        connect["tail"] = list(connect["tail"])
    return {
        "accounts": [dict(r, sign_in_expired=r["email"] in stale) for r in rows],
        "configured": bool(rows),
        "ready": [r["email"] for r in rows
                  if r["has_credentials"] and r["has_token"]
                  and r["email"] not in stale],
        "needs_file": [r["email"] for r in rows if not r["has_credentials"]],
        "needs_sign_in": [r["email"] for r in rows
                          if r["has_credentials"]
                          and (not r["has_token"] or r["email"] in stale)],
        "connect": connect,
        "connect_timeout_sec": connect_timeout_sec(),
    }


def reset_for_test() -> None:
    """Forget any in-flight sign-in. For tests only.

    The state is module-level and deliberately in memory (a restart clearing
    "running" is the correct answer to a run whose subprocess died with the
    server), so one test leaving it set would be another test's mystery.
    """
    with _lock:
        _state.update({"state": "idle", "running": False, "account": "",
                       "message": "", "tail": [], "started_at": "",
                       "finished_at": ""})


def handle_gmail_status(conn: Any) -> Dict[str, Any]:
    """GET /api/gmail/status — what is set up, what is missing, what is waiting.

    Takes `conn` because the router hands one to any handler whose first
    parameter is named that, and NC-183's answer — has Google taken the
    permission back — is written in the database by the background loop, not
    readable from the secrets folder.
    """
    return status(conn)


# ── The file Google gives you ────────────────────────────────────────────────

# What a desktop client file looks like from the outside. Google writes the
# whole client under one top-level key naming its type, and the type is the
# thing people get wrong: a "Web application" client has no way to use the
# loopback redirect this app needs, and picking it produces a failure several
# screens later with a message about redirect_uri that explains nothing.
_DESKTOP_KEYS = ("installed",)
_WEB_KEY = "web"
# A client file is a couple of kilobytes. Anything much larger is not one, and
# reading an arbitrary file the size of a disk image into memory to find that
# out is not a thing to do.
_MAX_BYTES = 64 * 1024


def _read_picked_bytes(typed: Any) -> bytes:
    """The bytes of the picked file, or an ApiError saying what is wrong with it.

    Every touch of the person's path is in here — tidying a pasted path (which
    checks whether it exists), whether it is a file, how big it is, reading it —
    so that _picked_file can run the lot off the request thread.
    """
    path = api_import_human_path(typed)
    if not path:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Choose the file first.")
    if not os.path.isfile(path):
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       f"There is no file at {path}.")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"That file could not be read: {exc}") from exc
    if size > _MAX_BYTES:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That file is far too big to be a sign-in file from Google — those "
            "are a couple of kilobytes. Check you picked the JSON Google gave "
            "you and not something else.")
    try:
        with open(path, "rb") as fh:
            return fh.read(_MAX_BYTES + 1)
    except OSError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"That file could not be read: {exc}") from exc


def _picked_file(typed: Any) -> bytes:
    """_read_picked_bytes with a time limit, so a macOS question cannot hang us.

    NC-355. The engine used to look at and read the picked path itself, on the
    thread answering the request. A file in Downloads is behind a macOS consent
    question, and a read blocked there waits instead of failing; nothing in
    Python can interrupt it. So the read runs on a helper thread and the request
    waits for it only picked_file_wait_sec() seconds. If the question is still
    unanswered then, the person is told what it probably is and what to do; the
    helper thread finishes whenever macOS lets it, reads into memory and
    discards the bytes. It writes nothing anywhere, so a late answer cannot
    quietly save a file after the screen said it had not.

    The native app avoids the wait altogether by handing over a private copy it
    made itself; a path outside the protected folders comes back at once.
    """
    box: Dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["bytes"] = _read_picked_bytes(typed)
        except BaseException as exc:  # noqa: BLE001 — handed back and re-raised below
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, name="read-picked-file", daemon=True).start()
    if not done.wait(picked_file_wait_sec()):
        raise ApiError(
            HTTPStatus.REQUEST_TIMEOUT,
            "macOS has not let this app read that file yet. Most likely a box is "
            "waiting for you, asking whether this app may open files in that "
            "folder (Downloads, Desktop and Documents all ask). Choose Allow, "
            "then choose the file again. If there is no box, copy the file to "
            "another folder first and choose the copy.")
    if "error" in box:
        raise box["error"]
    return box["bytes"]


def _read_client_file(typed: Any) -> Tuple[Dict[str, Any], bytes]:
    """Load and sanity-check a picked file: (what it says, its bytes), or raise
    ApiError saying what it is.

    Every message here names what was wrong and what to do instead, because this
    is the step where somebody who has never used Google Cloud Console is most
    likely to hand over the wrong thing — and the failure they would otherwise
    get arrives much later, from Google, in Google's words.

    The bytes come back too, because they are what gets saved: the file is read
    once, within a time limit (see _picked_file), and never touched again.
    """
    raw = _picked_file(typed)
    if len(raw) > _MAX_BYTES:       # grew between the size check and the read
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That file is far too big to be a sign-in file from Google — those "
            "are a couple of kilobytes. Check you picked the JSON Google gave "
            "you and not something else.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:       # not UTF-8, or not JSON
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That file is not the JSON Google gives you — it could not be read "
            "as JSON at all. Download it again from the Cloud Console: on your "
            "client, use the download button, and keep the file it saves.") from exc
    if not isinstance(data, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "That file is JSON, but not the shape Google's sign-in "
                       "file has. Download it again from the Cloud Console.")
    if _WEB_KEY in data and not any(k in data for k in _DESKTOP_KEYS):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That is a Web application client. This app signs you in on this "
            "Mac, so it needs a Desktop app client. Make a new one in the Cloud "
            "Console and choose Desktop app as the type.")
    block = None
    for key in _DESKTOP_KEYS:
        if isinstance(data.get(key), dict):
            block = data[key]
            break
    if block is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That JSON is not a sign-in file from Google — it has none of the "
            "parts one has. Download it again from the Cloud Console, from the "
            "client you made, and keep the file it saves.")
    if not str(block.get("client_id") or "").strip():
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That file looks like Google's but has no client id in it, so "
            "nothing could sign in with it. Download it again.")
    return data, raw


def handle_gmail_credentials(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/gmail/credentials — put the picked file where the app looks.

    {"email": "...", "path": "..."} → copied into the secrets folder under the
    name this account's importer will look for, owner-only.

    The copy is the whole point. Before this the person was told the file
    belonged at `secrets/gmail_credentials_<slug>.json`, where `<slug>` was
    their address with every character that is not a lowercase letter or digit
    removed — a rule stated nowhere on screen, applied to a folder with no
    button to open it. Doing the naming for them is the difference between a
    step somebody can follow and a step somebody can guess.
    """
    email = str(body.get("email") or "").strip().lower()
    if not email:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Say which address this file is for.")
    configured = gmail_accounts.accounts_from_env()
    if email not in configured:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{email} is not one of the addresses set up yet. Add the address "
            f"first, then give it its file.")
    _data, raw = _read_client_file(body.get("path"))
    target = configured[email]
    try:
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        # Made owner-only from the start (a file Google hands over arrives at
        # 0644), and again below for a file that was already there.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                       f"That file could not be saved: {exc}") from exc
    # It arrives from Downloads at 0644 — world-readable, with a client id and
    # secret in it. config.harden exists for exactly this file; see the comment
    # on harden_secrets_dir, which exists because such files arrive like that.
    hardened = config.harden(target)
    out = {"ok": True, "saved_as": os.path.basename(target), "hardened": hardened,
           "status": status()}
    if not hardened:
        out["warning"] = ("Saved, but this Mac would not let the app restrict who "
                          "can read it. It holds a Google client id and secret.")
    return out


def api_import_human_path(raw: Any) -> str:
    """The same tidying a typed path gets on the import row (NC-178).

    Imported lazily rather than at module import time: api_import imports a
    great deal, and this module is reached from the route registry, which app.py
    builds before api_import is finished importing on some paths. One function,
    one place — this is a call, not a copy.
    """
    import api_import
    return api_import.human_path(raw)


# ── Signing in ───────────────────────────────────────────────────────────────

def _finish(rc: Optional[int], tail: List[str], error: str = "") -> None:
    """Record a terminal result under the lock. Never raises."""
    with _lock:
        _state["running"] = False
        _state["finished_at"] = _now_iso()
        _state["tail"] = list(tail)
        if rc == 0 and not error:
            _state["state"] = "succeeded"
            _state["message"] = "Signed in. Gmail can collect now."
        else:
            _state["state"] = "failed"
            _state["message"] = error or _failure_message(rc, tail)


def _failure_message(rc: Optional[int], tail: List[str]) -> str:
    """Say what went wrong in the app's own words, not the child's.

    The child's last line is kept and shown in the log, but it is Google's
    vocabulary and a person who has just met the Cloud Console for the first
    time is the least equipped reader for it.
    """
    if rc == importers.EXIT_NEEDS_SIGN_IN:
        # Should not be reachable — this run is the interactive one, so it is
        # allowed to open a browser. If it happens, saying so plainly beats a
        # message that pretends to know why.
        return ("The sign-in was refused before it opened. Try again, and if it "
                "keeps happening, restart the app.")
    joined = " ".join(tail[-6:]).lower()
    if "access_denied" in joined or "denied" in joined:
        return ("Google says the permission was declined. Run this again and "
                "choose Allow on the Google screen.")
    if "invalid_client" in joined or "unauthorized" in joined:
        return ("Google did not recognise the sign-in file. Check you picked the "
                "one from the client you made, and that it is a Desktop app "
                "client.")
    if "accessnotconfigured" in joined or "has not been used" in joined:
        return ("The Gmail API is not switched on for that Google project yet. "
                "Turn it on in the Cloud Console, then try again.")
    return f"The sign-in did not finish (exit {rc})."


def _run_connect(account: str) -> None:
    """Background worker: run the sign-in-only mode and record what happened.

    Never raises — every failure lands as state=failed with the child's last
    lines, because a background thread that throws leaves the page polling a
    "running" that will never end.
    """
    cmd = [sys.executable, "gmail_import.py", "--connect", account]
    tail: List[str] = []
    try:
        proc = subprocess.run(
            cmd, cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, env=os.environ.copy(),
            timeout=connect_timeout_sec())
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-_TAIL_LINES:]
        _finish(proc.returncode, tail)
    except subprocess.TimeoutExpired:
        _finish(None, tail,
                f"Nothing came back from Google within "
                f"{connect_timeout_sec() // 60} minutes, so the app stopped "
                f"waiting. The Google tab may still be open — close it and "
                f"start again when you are ready.")
    except OSError as exc:
        _finish(None, tail, f"The sign-in could not be started: {exc}")


def handle_gmail_connect(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/gmail/connect — the button the help text has been promising.

    Returns at once. A browser tab opens for Google's consent screen, and the
    row says what it is waiting for until the person has clicked Allow, so it
    does more than give instructions. Poll /api/gmail/status for the outcome.
    """
    email = str(body.get("email") or "").strip().lower()
    configured = gmail_accounts.accounts_from_env()
    if not configured:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "Add the Google address you want to collect from first.")
    if not email:
        if len(configured) != 1:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           "Say which address to sign in to.")
        email = next(iter(configured))
    if email not in configured:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       f"{email} is not one of the addresses set up here.")
    if not os.path.exists(configured[email]):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That address has no sign-in file from Google yet, so there is "
            "nothing to sign in with. Choose the file Google gave you first.")
    started = False
    with _lock:
        if _state["running"]:
            message = (f"Already waiting for {_state['account']} — finish that "
                       f"in the Google tab first.")
        else:
            _state.update(state="running", running=True, account=email,
                          started_at=_now_iso(), finished_at="", tail=[],
                          message="A Google tab is open — choose Allow there.")
            started = True
            message = "A Google tab is opening. Choose Allow there."
    if started:
        threading.Thread(target=_run_connect, args=(email,),
                         name="gmail-connect", daemon=True).start()
    return {"started": started, "message": message, "status": status()}
