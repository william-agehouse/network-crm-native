#!/usr/bin/env python3
"""This instance's own config and health: settings, diagnostics, onboarding.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

Everything here is about the install rather than the network it holds — the
managed .env keys the Settings view edits, the diagnostics panel, the update
check, and the first-run onboarding flag.

handle_status deliberately stays in app.py: it reports HOST and PORT, which are
transport facts, and tests/test_http.py rebinds app.HOST/app.APP_TOKEN in place.
"""

import importlib
import json
import logging
import os
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Dict, List, Optional

import archive as arc
import figures
import config  # DATA_DIR, for the dependency-status marker crm/python_env.py writes
import db
import importers
import search_cli
import settings
import updates
from api_core import ApiError

log = logging.getLogger("networkcrm")

def identity_status() -> Dict[str, Any]:
    """Are SELF_NAME and SELF_EMAIL set? (NC-054)

    Neither one missing breaks anything — every request they touch still
    answers 200 — which is exactly why this needs its own signal. Read fresh
    from os.environ, like deps_status reads its marker file, so a save made
    in Settings a second ago is reflected without a restart.

    Three things degrade quietly on a missing key, each already true of the
    code before this function existed:
      • owner.display_name() falls back to "the user" (never an error), so
        every AI draft and Ask reply is written for no one in particular.
      • Every importer that decides which side of a conversation is the
        user's (imessage_import, whatsapp_import, meta_import: SELF_NAME;
        email_import, ics_import: SELF_EMAIL) falls back to keeping any
        address that appears, rather than the strict two-way match invariant
        2 asks for — a silent widening, not a failure.
      • style_profile's mbox pass needs SELF_EMAIL to separate the user's
        sent messages from everyone else's; unset, it says so in its own run
        rather than raising.
    """
    missing = [k for k in ("SELF_NAME", "SELF_EMAIL")
               if not (os.environ.get(k) or "").strip()]
    if not missing:
        return {"ok": True, "missing": []}
    if missing == ["SELF_NAME"]:
        message = ('Your name is not set. Drafts and Ask refer to you as '
                    '"the user" instead of your name.')
    elif missing == ["SELF_EMAIL"]:
        message = ('Your email is not set. Imports cannot tell your side of '
                    'a conversation from anyone else\'s, and the '
                    'writing-style pass cannot separate your messages from '
                    'theirs.')
    else:
        message = ('Your name and email are not set. Drafts and Ask refer '
                    'to you as "the user" instead of your name, and imports '
                    'cannot tell your side of a conversation from anyone '
                    'else\'s.')
    return {"ok": False, "missing": missing, "message": message}


def data_dir_bytes():
    """(bytes the data folder occupies, and where they are) — NC-124.

    The Data page says "on disk" and used to answer with
    `crm.db + archive.db`: two files out of eight. On a large install that left
    out most of the folder, and the two biggest omissions were the photos and the
    message-search vectors — exactly the two things a person weighs up before
    deciding whether to keep them.

    ALLOCATED blocks, not `st_size`. The label is "on disk", and on disk is what
    `du` reports: apparent size comes out lighter, because thousands of photo
    files each round up to a block. `st_blocks * 512` equals
    `du -skL` on the data folder to the byte.

    Symlinks are counted as the links they are and never followed. `data/` may
    itself be a symlink into Application Support, and a walk that
    followed links could wander out of the folder it claims to be measuring, or
    loop. Unreadable entries are skipped rather than raising: a diagnostics
    figure must not be the thing that breaks the Data page.

    Cheap enough over a few thousand files that there is nothing to cache.
    """
    parts: Dict[str, int] = {}
    total = 0
    try:
        top = sorted(os.scandir(config.DATA_DIR), key=lambda e: e.name)
    except OSError:
        return 0, {}
    for entry in top:
        n = _entry_bytes(entry)
        if n:
            parts[entry.name] = n
        total += n
    return total, parts


def _entry_bytes(entry: Any) -> int:
    """Blocks used by one directory entry, recursing into real directories."""
    try:
        st = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or entry.is_file(follow_symlinks=False):
            return st.st_blocks * 512
        if not entry.is_dir(follow_symlinks=False):
            return 0
    except OSError:
        return 0
    total = st.st_blocks * 512
    try:
        children = list(os.scandir(entry.path))
    except OSError:
        return total
    for child in children:
        total += _entry_bytes(child)
    return total


def _people_ledger(conn: Any, total: int) -> Dict[str, Any]:
    """How many people this app holds, and where the difference goes.

    Three disjoint parts that add up to the whole file:

        active   you can see and use them         (population "active")
        hidden   you hid them, and did not delete (population "hidden_only")
        deleted  you deleted them                 (population "deleted")

    `hidden` subtracts the overlap on purpose. `db.set_deleted` sets BOTH columns,
    so every deleted person is also excluded; counting them in both buckets makes
    the breakdown add up to more than the file holds, which is the exact bug the
    `unexplained` residue exists to catch. For example, with 5 excluded and 2
    deleted (both of them among the 5), `hidden` is 3, not 5.
    """
    def n(where: str) -> int:
        sql = "SELECT COUNT(*) AS n FROM contacts" + (f" WHERE {where}" if where else "")
        return conn.execute(sql).fetchone()["n"] or 0

    active = n(figures.population_where("active"))
    deleted = n(figures.population_where("deleted"))
    hidden = n(figures.population_where("hidden_only"))
    led = figures.ledger(total, {"active": active, "hidden": hidden,
                                 "deleted": deleted}, name="people in the file")
    led["figure"] = figures.figure(active, "people", "active",
                                   of=total, side=figures.SIDE_STORED)
    led["says"] = figures.ledger_says(led)
    return led


def _settings_diagnostics(conn: Any) -> Dict[str, Any]:
    """"This Mac" panel: local DB sizes/counts, no external calls. Every file
    check is guarded so a missing archive.db (fresh install) never 500s."""
    crm_db_bytes = os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else 0
    archive_db_bytes = 0
    archived_messages = 0
    if os.path.exists(arc.ARCHIVE_DB_PATH):
        archive_db_bytes = os.path.getsize(arc.ARCHIVE_DB_PATH)
        with arc.open_if_present() as arc_conn:
            if arc_conn is not None:
                try:
                    archived_messages = arc.total_archived(arc_conn)
                except Exception:  # noqa: BLE001 — diagnostics must never fail the page
                    archived_messages = 0

    counts = db.counts(conn)
    # NC-087. `counts["total"]` is every row, and the
    # People page one click away shows fewer, because it filters the people the
    # user hid. Both were rendered as bare numbers under the word "people", and
    # neither said which population it had counted.
    #
    # A ledger rather than a second bare number: the three parts PARTITION the
    # total, so they add up by construction and `unexplained` goes non-zero the
    # moment a fourth state appears. They are deliberately disjoint — "hidden"
    # here means hidden and NOT deleted, because db.set_deleted sets both, and
    # counting the overlap twice is how a breakdown ends up larger than its
    # total: visible + hidden + deleted = every row.
    people = _people_ledger(conn, counts["total"])
    notes_n = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"] or 0
    # NC-122: the same people as `people.figure` above, not the whole file.
    photos_n = conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE photo != '' "
        f"AND {figures.population_where('active')}").fetchone()["n"] or 0
    last_synced = {
        key: v for key in importers.REGISTRY
        if (v := (db.state_get(conn, f"import_synced_{key}", "") or ""))
    }
    # NC-124. The whole folder, not the two databases in it — see
    # data_dir_bytes(). `disk_parts` is the breakdown, so a caller can say WHAT
    # the number is made of instead of asking the reader to trust a total.
    disk_bytes, disk_parts = data_dir_bytes()

    # NC-094. This used to be `max(last_synced.values())` under the name
    # "last_sync", and the panel labelled it "last sync". One source that had
    # just run therefore made every source that had not run for a long time read
    # as fresh: the figure said "today" while `linkedin_facts` had last run long
    # before.
    #
    # The honest direction is the OLDEST, not the newest — a summary of many
    # sources should move with the worst one. Two decisions make that meaningful
    # rather than alarming:
    #
    #   * Only `category == "import"` counts. The five `enrich` passes (facts,
    #     facts_llm, style, engagement, linkedin_facts) derive from data already
    #     here; they are not collecting, and a stale one does not mean data is
    #     missing.
    #   * Only sources that have collected at least ONCE count. A source with no
    #     stamp has never been set up, which is not the same as stale, and
    #     letting it drag the summary to "never" would make the figure useless on
    #     every install that has not connected every source.
    #
    # `population` says both of those in the payload rather than in this comment,
    # because a bare timestamp is exactly the kind of figure NC-087 is about.
    collecting = {k: v for k, v in last_synced.items()
                  if getattr(importers.REGISTRY[k], "category", "") == "import"}
    oldest_key = min(collecting, key=collecting.get, default=None)
    sync_summary = {
        "oldest": collecting.get(oldest_key) if oldest_key else None,
        "oldest_source": oldest_key,
        "oldest_label": (getattr(importers.REGISTRY[oldest_key], "label", oldest_key)
                         if oldest_key else None),
        "newest": max(collecting.values(), default="") or None,
        "collecting": len(collecting),
        "population": "import sources that have collected at least once",
        "never": sorted(k for k, s in importers.REGISTRY.items()
                        if getattr(s, "category", "") == "import"
                        and k not in last_synced),
    }

    return {
        "crm_db_path": db.DB_PATH,
        "crm_db_bytes": crm_db_bytes,
        "archive_db_path": arc.ARCHIVE_DB_PATH,
        "archive_db_bytes": archive_db_bytes,
        "disk_bytes": disk_bytes,
        "disk_parts": disk_parts,
        "archived_messages": archived_messages,
        "contacts": counts["total"],   # kept: five callers read this bare int
        "people": people,
        "notes": notes_n,
        "photos": photos_n,
        "last_synced": last_synced,
        "sync_summary": sync_summary,
        # Truthful state of the optional semantic stack (available? installing?),
        # carried here so the Settings view can render and poll it off the same
        # read-only status endpoint it already self-refreshes.
        "semantic_search": semantic_search_status(),
        "deps_status": deps_status(),
        "identity": identity_status(),
    }


def deps_status() -> Dict[str, Any]:
    """Did the app fail to install its optional packages?

    The app installs them into its own Python environment on the first start of
    each version (crm/python_env.py, NC-330; a launcher script did it before).
    That used to end in `|| true`, so a network failure left the app running with
    packages missing and NOTHING said so — which is how a missing `anthropic`
    would make the Ask view answer badly while looking healthy. It now writes
    this marker instead, and deletes it once an install succeeds, so the failure
    has somewhere to surface. Absent file is the normal, healthy case.
    """
    path = os.path.join(config.DATA_DIR, "deps_status.json")
    if not os.path.exists(path):
        return {"ok": True}
    try:
        with open(path, encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError) as exc:
        # A corrupt marker still means something went wrong — say so rather than
        # swallowing it, which is the whole point of this mechanism.
        return {"ok": False, "message": f"dependency status file is unreadable: {exc}"}
    if not isinstance(marker, dict):
        return {"ok": False, "message": "dependency status file is malformed"}
    return {
        "ok": False,
        "status": str(marker.get("status") or "failed"),
        "phase": str(marker.get("phase") or ""),
        "message": str(marker.get("message") or "Some optional features could not be installed."),
        "timestamp": str(marker.get("timestamp") or ""),
        "log_file": str(marker.get("log_file") or ""),
        "retry": str(marker.get("retry") or ""),
    }


def handle_settings_get(conn: Any) -> Dict[str, Any]:
    """GET /api/settings — the managed keys (secrets masked) plus local
    diagnostics for the "This Mac" panel."""
    return {"settings": settings.read_all(), "diagnostics": _settings_diagnostics(conn)}


def handle_diagnostics_get(conn: Any) -> Dict[str, Any]:
    """GET /api/diagnostics — local status stats (DB sizes/counts, last sync)
    for the read-only diagnostics panel. Same numbers the Settings payload
    carries, exposed on their own endpoint so the panel can self-refresh."""
    return _settings_diagnostics(conn)


# ── Optional semantic search: the on-demand installer ────────────────────────
# Meaning-based ranking (the People view) and Ask's semantic_search tool need
# sentence-transformers + numpy, which pull PyTorch (~2 GB). We deliberately do
# NOT ship that by default — a 2 GB download on first launch reads as a hang —
# so it is installed on demand from here, into the interpreter the app is ALREADY
# running on (sys.executable): the packaged bundle's own venv or a dev checkout's
# venv, never a hardcoded path.
#
# pip-installing torch takes MINUTES, so the HTTP request never waits for it. The
# POST spawns a daemon thread (the shape api_import._autosync_loop already uses —
# api_import's own subprocess.run blocks, but that path is time-boxed to seconds
# and this is not) and returns at once; the UI polls the semantic_search section
# of GET /api/diagnostics for live pip output. search_cli.embeddings_available()
# stays the single source of truth for whether the feature actually works.
SEMANTIC_PACKAGES = ("sentence-transformers", "numpy")
SEMANTIC_SIZE_GB = 2            # ~2 GB once PyTorch is pulled in; stated before the click
_SEMANTIC_TAIL_LINES = 40       # pip output lines kept for the UI / failure log
SEMANTIC_INSTALL_TIMEOUT_DEFAULT = 1800  # 30 min backstop for a slow connection

_semantic_lock = threading.Lock()
# In-memory on purpose: a restart mid-install just clears "running", and the real
# state is recomputed from embeddings_available() on the next status read.
_semantic_state: Dict[str, Any] = {
    "state": "idle",            # idle | running | succeeded | failed
    "running": False,
    "started_at": "",
    "finished_at": "",
    "returncode": None,
    "message": "",
    "tail": [],                 # last _SEMANTIC_TAIL_LINES lines of pip output
    "restart_required": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def semantic_install_timeout_sec() -> int:
    """Seconds before the pip install is killed (NC_SEMANTIC_INSTALL_TIMEOUT_SEC).
    Read at call time — like import_timeout_sec — so a value hand-edited into
    secrets/.env applies for a slow connection without a restart. NC-003 took
    this out of the in-app config screen as pure tuning plumbing; it is a
    plain env var now. Clamped 60s–2h."""
    try:
        secs = int(os.environ.get("NC_SEMANTIC_INSTALL_TIMEOUT_SEC",
                                  SEMANTIC_INSTALL_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        secs = SEMANTIC_INSTALL_TIMEOUT_DEFAULT
    return max(60, min(7200, secs))


def _semantic_install_cmd() -> List[str]:
    """pip install argv targeting the running interpreter (sys.executable) — the
    packaged bundle's venv or a dev venv, never a hardcoded path.

    NC-316: exact versions with their fingerprints, never "whatever is newest
    today". requirements-semantic.txt pins sentence-transformers, numpy and all
    they pull in; requirements-lock.txt rides along because pip's fingerprint mode
    wants every package it touches pinned, and those are already installed."""
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--require-hashes", "--only-binary=:all:",
            "-r", os.path.join(config.ROOT, "requirements-lock.txt"),
            "-r", os.path.join(config.ROOT, "requirements-semantic.txt")]


def _semantic_reason() -> str:
    """Why semantic search is unavailable: packages absent vs a present-but-broken
    import. Availability itself is always search_cli.embeddings_available(); this
    only explains a False and is skipped when it is True, so we never pay the
    multi-second torch import just to render a status line."""
    try:
        import numpy  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ModuleNotFoundError as exc:
        return f"not installed ({exc.name} missing)"
    except ImportError as exc:
        return f"installed but failed to import: {exc}"
    except Exception as exc:  # noqa: BLE001 — a broken build must not 500 the page
        return f"import error: {exc}"
    return ""


def semantic_search_status() -> Dict[str, Any]:
    """Honest status of the optional semantic stack: whether it is installed,
    why not if not, and the live state of any install this process has run.

    embeddings_installed(), not embeddings_available(): the second imports
    PyTorch to answer, which takes seconds and hundreds of MB the first time, and
    this is a status line (NC-221). A search still checks by importing."""
    available = search_cli.embeddings_installed()
    with _semantic_lock:
        install = dict(_semantic_state)
        install["tail"] = list(install["tail"])
    return {
        "available": available,
        "packages": list(SEMANTIC_PACKAGES),
        "reason": "" if available else _semantic_reason(),
        "size_gb": SEMANTIC_SIZE_GB,
        "install": install,
    }


def _finish_semantic_install(rc: Optional[int], tail: List[str],
                             error: str = "", restart_required: bool = False) -> None:
    """Record a terminal install result under the lock (never raises)."""
    with _semantic_lock:
        _semantic_state["running"] = False
        _semantic_state["returncode"] = rc
        _semantic_state["finished_at"] = _now_iso()
        _semantic_state["tail"] = list(tail)
        if rc == 0 and not error:
            _semantic_state["state"] = "succeeded"
            _semantic_state["restart_required"] = restart_required
            _semantic_state["message"] = (
                "Restart the app to finish enabling semantic search."
                if restart_required else "Semantic search is ready.")
        else:
            _semantic_state["state"] = "failed"
            _semantic_state["message"] = error or f"Install failed (exit {rc})."


def _run_semantic_install() -> None:
    """Background worker: pip-install the semantic stack into this interpreter's
    venv, streaming pip's own output into _semantic_state so the UI shows real
    progress. Never raises — every failure lands as state=failed with a tail
    (no network, pip missing, disk full all surface as a non-zero exit here)."""
    cmd = _semantic_install_cmd()
    tail = deque(maxlen=_SEMANTIC_TAIL_LINES)
    timed_out = {"v": False}
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=os.environ.copy(),
        )
    except (OSError, ValueError) as exc:  # bad argv / a python with no pip at all
        _finish_semantic_install(None, [f"could not start pip: {exc}"],
                                 error=f"could not start pip: {exc}")
        return

    def _kill() -> None:
        timed_out["v"] = True
        try:
            proc.kill()
        except OSError:
            pass

    timer = threading.Timer(semantic_install_timeout_sec(), _kill)
    timer.start()
    try:
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if not line:
                    continue
                tail.append(line)
                with _semantic_lock:
                    _semantic_state["tail"] = list(tail)
                    _semantic_state["message"] = line
        proc.wait()
    except OSError as exc:  # the pipe died under us mid-read
        _finish_semantic_install(None, list(tail) + [f"error: {exc}"],
                                 error=f"install error: {exc}")
        return
    finally:
        timer.cancel()

    rc = proc.returncode
    if timed_out["v"]:
        _finish_semantic_install(rc, list(tail),
                                 error=f"timed out after {semantic_install_timeout_sec()}s")
        return
    if rc == 0:
        # The packages just landed in site-packages; drop the import machinery's
        # cached directory listings so a fresh import in THIS process finds them.
        # search_cli imports numpy/sentence_transformers lazily, so if this flips
        # embeddings_available() to true no restart is needed — and we report the
        # truth either way rather than guessing.
        importlib.invalidate_caches()
        restart_required = not search_cli.embeddings_available()
        _finish_semantic_install(0, list(tail), restart_required=restart_required)
    else:
        _finish_semantic_install(rc, list(tail), error=f"pip exited with code {rc}")


def handle_semantic_install(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """POST /api/semantic/install — install the ~2 GB semantic stack on demand.

    Returns immediately (the install runs minutes in a background thread); poll
    the semantic_search section of GET /api/diagnostics for progress. No-ops when
    the stack is already importable, and refuses a second concurrent run so two
    clicks can't launch two pips at the same site-packages."""
    if search_cli.embeddings_available():
        return {"started": False,
                "message": "Semantic search is already installed.",
                "status": semantic_search_status()}
    started = False
    with _semantic_lock:
        if _semantic_state["running"]:
            message = "An install is already running."
        else:
            _semantic_state.update(state="running", running=True,
                                   started_at=_now_iso(), finished_at="",
                                   returncode=None, restart_required=False,
                                   tail=[], message="Starting install…")
            started = True
            message = (f"Installing semantic search (~{SEMANTIC_SIZE_GB} GB). "
                       "This can take several minutes.")
    if started:
        threading.Thread(target=_run_semantic_install, name="semantic-install",
                         daemon=True).start()
    return {"started": started, "message": message, "status": semantic_search_status()}


# handle_bot_link_code() and _get_bot_username() lived here. They issued a
# one-time code you sent to a Telegram bot as /start <code> to pair your account.
# Removed with the bot itself — the whole phone-bot idea went, not
# just its transport, so there is nothing left to pair with. A 'bot_link_code'
# row in the state table is inert; db.ensure_columns never created it and
# nothing reads it now.
#
# Note this is the BOT, not the Telegram IMPORTER. crm/telegram_import.py and the
# "telegram" channel stay: that is message history, the same kind of source as
# WhatsApp or iMessage, and deleting it would drop data people already imported.


def handle_settings_post(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/settings {key, value} — the only path that ever writes
    secrets/.env. `value=""` removes the key; a non-empty value also lands in
    os.environ immediately so Ask and drafts pick it up without a restart."""
    key = str(body.get("key") or "").strip()
    if key not in settings.MANAGED_KEYS:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"'{key}' is not a managed setting")
    if "value" not in body:
        raise ApiError(HTTPStatus.BAD_REQUEST, "value is required")
    value = str(body.get("value") or "").strip()

    meta = settings.MANAGED_KEYS[key]
    if meta["secret"] and value:
        current_mask = settings.mask(os.environ.get(key, ""))
        if current_mask and value == current_mask:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "that's the masked placeholder — enter the real value to replace it",
            )

    try:
        secured = settings.write_key(key, value)
    except ValueError as exc:  # e.g. embedded newlines — user error, not a 500
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    out = {"settings": settings.read_all(), "secured": secured}
    if not secured:
        # NC-150. Saved, and the person is told what did NOT happen beside it.
        out["warning"] = (
            "Saved, but the permissions on secrets/.env could not be tightened, "
            "so it may be readable by other accounts on this Mac. That file "
            "holds your API key and calendar addresses.")
    return out


def handle_update_get(conn: Any, query: Dict[str, Any]) -> Dict[str, Any]:
    """GET /api/update — is a newer Network CRM available on GitHub Releases?
    Cached once a day unless ?force=1. Sends nothing about the user and degrades
    to a "couldn't check" shape rather than erroring when offline."""
    force = (query.get("force", [""])[0] or "").strip() in ("1", "true", "yes")
    return updates.check(conn, force=force)


ONBOARDED_STATE_KEY = "onboarded"


ONBOARDED_GRANDFATHER_CONTACTS = 20


def onboarding_done(conn: Any) -> bool:
    """The answer handle_onboarding_get gives, with no write (and since NC-342
    the handler writes nothing either).

    Background sync needs this question answered before it imports anything, and
    it must not be the thing that *decides* the answer. Asking through the
    handler would have made autosync persist the grandfather flag from a
    background thread — the exact loop this pair was split up to break:

      1. First launch. Autosync fires within seconds and imports WhatsApp from
         its own container (importers.WA_CONTAINER), so the DB gains many
         people before the user has agreed to anything.
      2. The UI then asks /api/onboarding. The flag was never set, but
         db.counts() is now far over ONBOARDED_GRANDFATHER_CONTACTS.
      3. So this install is grandfathered as "already set up" and **the wizard
         never appears at all** — suppressed by an import the user never asked
         for. On a clean data dir this imported contacts and messages before
         the user had agreed to anything, and answered {"done": true} on an
         install no human had touched.

    The grandfather rule itself is right and stays — someone upgrading with a
    real network should not be shown a first-run wizard. It just must not be
    reachable by data the app imported for itself, which is what gating
    _autosync_pending on this predicate ensures.
    """
    flag = db.state_get(conn, ONBOARDED_STATE_KEY, "")
    if flag:
        return flag == "1"
    return db.counts(conn)["total"] > ONBOARDED_GRANDFATHER_CONTACTS


def handle_onboarding_get(conn: Any) -> Dict[str, Any]:
    """GET /api/onboarding — has this Mac finished (or skipped) the first-run
    wizard? Grandfathers existing installs: if the flag was never set but the
    DB already has a real network in it, setup counts as done.

    NC-342. This used to store that answer on the first call, so asking the
    question changed it, and a prefetch or a second window could do the asking.
    The grandfathering is now worked out on every read instead, the same way
    onboarding_done() always did. The flag is written only by the POST below:
    finishing or skipping the wizard, or Settings' "Run setup again"."""
    return {"done": onboarding_done(conn)}


def handle_onboarding_post(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/onboarding {done} — set or clear the first-run flag. Clearing
    it (from Settings' "Run setup again") lets a user replay the wizard."""
    done = bool(body.get("done"))
    db.state_set(conn, ONBOARDED_STATE_KEY, "1" if done else "0")
    conn.commit()
    return {"done": done}
