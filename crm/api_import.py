#!/usr/bin/env python3
"""Import/enrich endpoints and the background auto-sync loop.

Flat sibling of app.py. `python3 crm/app.py` puts crm/ at sys.path[0], so the
imports below are plain `import db`, exactly as crm/feed.py does. Do NOT convert
them to `from crm import db`: there is no crm/__init__.py, and start.command,
the packaging launcher and tests/conftest.py all depend on the flat form.

Reach every external module THROUGH its module name (subprocess.run, db.get_db,
engagement.refresh, arc.channel_watermark) rather than `from subprocess import
run` — the tests monkeypatch the shared module objects via app.subprocess,
app.db, app.engagement and app.arc, and a from-import would bind past the patch.
"""

import http.client  # noqa: F401 — the InvalidURL class this module must not leak
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

import config  # noqa: F401 — loads secrets/.env before anything reads os.environ
import archive as arc
import db
import engagement
import figures  # NC-122: photos counted over the same people as the rest of the page
import importers
import linkedin_token
# For onboarding_done() alone — the consent gate in _autosync_pending. Safe in
# this direction only: api_instance imports no api_* module except api_core, so
# this adds an edge, not a cycle. Keep it that way.
import api_instance
from api_core import ApiError

# Where a failed calendar fetch's real reason goes. It must not go into the HTTP
# response: the exception text can contain the secret calendar URL.
log = logging.getLogger("networkcrm.import")

# Directory holding every importer script. importers.REGISTRY builds argv from
# BARE filenames ("contacts_import.py", …) and they run with cwd=APP_DIR, so
# this MUST be crm/. config.PKG_DIR is that directory by construction and stays
# correct no matter where this module ends up.
APP_DIR = config.PKG_DIR


# ── Import: subprocess wiring ────────────────────────────────────────────────
# How long any one importer/enricher may run. 5 minutes covers every source
# here except a first full iMessage import of a very large chat.db, hence
# the override rather than a bigger silent constant.
IMPORT_TIMEOUT_DEFAULT = 300


def import_timeout_sec() -> int:
    """Seconds before an import subprocess is killed (NC_IMPORT_TIMEOUT_SEC).

    Read at call time, not at import: NC-003 took this out of the in-app
    config screen as pure tuning plumbing, so it is a plain env var now — but
    it still applies live to a value hand-edited into secrets/.env, with no
    restart needed. Captured once at import, that edit would silently do
    nothing until restart.
    """
    try:
        secs = int(os.environ.get("NC_IMPORT_TIMEOUT_SEC", IMPORT_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        secs = IMPORT_TIMEOUT_DEFAULT
    return max(10, min(3600, secs))
# time box for the free offline facts pass chained after a successful import —
# short on purpose: the import already succeeded, this is a bonus
ENRICH_AFTER_IMPORT_SEC = int(os.environ.get("ENRICH_AFTER_IMPORT_SEC", "120"))

# The background loop may import every couple of minutes — a live chat.db changes
# constantly — so the chained facts pass is rate-limited there, unlike the manual
# path where the user just asked for it and expects it now. 60 minutes: the free
# pass is a full offline sweep over every contact, and nothing it fills changes
# faster than that. Manual runs ignore this entirely.
FACTS_CHAIN_MIN_MINUTES = int(os.environ.get("NC_FACTS_CHAIN_MIN_MINUTES", "60"))
FACTS_CHAIN_STATE_KEY = "facts_chain_last"

# Re-measuring how the user writes keeps AI drafts in their voice. Nothing ran it
# automatically before, so the profile froze at whatever the last manual click
# produced. Weekly, because it reads the whole message archive and nobody's
# writing voice moves faster than that.
STYLE_CHAIN_MIN_MINUTES = int(os.environ.get("NC_STYLE_CHAIN_MIN_MINUTES", str(7 * 24 * 60)))
STYLE_CHAIN_STATE_KEY = "style_chain_last"

DEFAULT_EMAIL_YEARS = 3
# Native file/folder picker prompts, by kind. Used by /api/import/pick-file.
PICK_SCRIPTS = {
    "vcf":     'POSIX path of (choose file with prompt "Select your Contacts.vcf export")',
    "csv":     'POSIX path of (choose file with prompt "Select LinkedIn Connections CSV" of type {"csv", "public.comma-separated-values-text"})',
    "mbox":    'POSIX path of (choose file with prompt "Select email archive (.mbox)")',
    "chatdb":  'POSIX path of (choose file with prompt "Select your chat.db copy")',
    "wadb":    'POSIX path of (choose file with prompt "Select your ChatStorage.sqlite copy")',
    "profilefolder": 'POSIX path of (choose folder with prompt "Select the WhatsApp Media/Profile folder")',
    # A calendar the user exported by hand (Calendar.app → File → Export, or a
    # Google Takeout .ics). Same picker idiom as "csv": extension plus the UTI,
    # because Calendar.app's own exports are typed and a bare extension filter
    # greys them out in some macOS releases. Only the FILE source uses this —
    # the two URL-fed calendars have kind="none" and pick_kind=None.
    "ics":     'POSIX path of (choose file with prompt "Select a calendar export (.ics)" of type {"ics", "com.apple.ical.ics"})',
    # NC-117. The sign-in file Google hands over from the Cloud Console. It
    # lands in Downloads under a name nobody would recognise —
    # client_secret_<a very long client id>.apps.googleusercontent.com.json —
    # so the prompt says what it IS rather than what it is called, and the type
    # filter carries the UTI as well as the extension for the same reason "csv"
    # does: a bare extension greys out perfectly good files in some macOS
    # releases.
    "json":    'POSIX path of (choose file with prompt "Select the sign-in file Google gave you" of type {"json", "public.json"})',
}


# ── Import endpoints ────────────────────────────────────────────────────────────
def _parse_years(value: Any) -> int:
    try:
        years = int(value)
    except (TypeError, ValueError):
        years = DEFAULT_EMAIL_YEARS
    return max(1, min(10, years))


def human_path(raw: Any) -> str:
    r"""Tidy a path a person typed or pasted into the box on the Data page.

    NC-178. The box is new; before it, every path arrived from the native
    picker, already perfect. A person's own paste is not, and it arrives in four
    shapes depending on where they copied it from:

    * with a trailing newline, from almost any copy;
    * wrapped in quotes, because that is how people write paths;
    * with backslash-escaped spaces, which is what dragging a file onto
      Terminal produces — `/Users/you/My\ Export.vcf`;
    * starting with `~`, which means nothing to a subprocess.

    All four are the same file, and refusing them would make the box a worse
    picker rather than a second way in. The escaped-space case is the only one
    that involves a judgement, and it is not a guess: the backslashes are
    removed only when the literal path does not exist and the unescaped one
    does. A real filename containing a backslash is therefore left alone.
    """
    path = str(raw or "").strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in ("'", '"'):
        path = path[1:-1].strip()
    path = os.path.expanduser(path)
    if "\\ " in path and not os.path.exists(path):
        unescaped = path.replace("\\ ", " ")
        if os.path.exists(unescaped):
            path = unescaped
    return path


def _build_command(source: str, body: Dict[str, Any]) -> List[str]:
    """Look up `source` in importers.REGISTRY and build its argv from the form body."""
    spec = importers.REGISTRY.get(source)
    if spec is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"source must be one of {sorted(importers.REGISTRY)}")
    form = dict(body)
    # NC-178. The one place a human's own typing becomes an argv. The background
    # loop builds its forms from paths this app wrote itself and never comes
    # through here, so nothing machine-generated is touched by this.
    if "path" in form:
        form["path"] = human_path(form.get("path"))
    form["years"] = _parse_years(body.get("years", DEFAULT_EMAIL_YEARS))
    form["dry_run"] = bool(body.get("dry_run"))
    # Refuse a preview we can't actually deliver. Silently ignoring the flag
    # would run the real, irreversible thing while the user believed they'd
    # asked for a dry run — the exact mistake the feature exists to prevent.
    if form["dry_run"] and not spec.supports_dry_run:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       f"'{spec.label}' has no preview mode — it would run for real")
    try:
        return spec.build_cmd(form)
    except importers.ImporterError as exc:
        raise ApiError(exc.status, exc.message) from exc


def _run_chained_facts_pass() -> List[Dict[str, Any]]:
    """Run the FREE offline facts pass, the one that fills locations and roles so a
    fresh import arrives already described. Returns its receipts, or [].

    Never the paid LLM pass — that stays an explicit click, so an import can never
    cost money by surprise. Non-fatal and time-boxed: whatever triggered this has
    already succeeded, and enrichment is a bonus.

    The argv comes from importers.facts_pass_cmd(), NOT from
    _build_command("facts") — routing it through the registry would make this
    automatic behaviour depend on a UI row existing. Retiring that row would then
    raise ApiError inside the `except Exception: pass` below, and the free pass
    would silently stop after every import while Settings still said it ran.

    Shared by BOTH callers on purpose. Inlined in handle_import_run only, it
    would run after a clicked import and never after a background one — and
    almost everything is moving to background syncing, so it would never run at
    all. One function, two callers, so the two cannot drift.
    """
    try:
        done = subprocess.run(
            importers.facts_pass_cmd(), capture_output=True, text=True,
            cwd=APP_DIR, env=os.environ.copy(), timeout=ENRICH_AFTER_IMPORT_SEC,
        )
        if done.returncode == 0:
            return importers.parse_receipts((done.stdout or "") + (done.stderr or ""))
    except Exception:  # noqa: BLE001 — enrichment is a bonus, never a blocker
        pass
    return []


def _chain_is_due(conn: Any, state_key: str, min_minutes: int) -> bool:
    """Rate limit for the background callers only — a click is never rate-limited.

    An absent, empty or unparseable stamp all mean DUE. Failing closed here would
    be the worse bug by far: a single bad value in the state table would silently
    stop the pass forever, and nothing surfaces that.
    """
    last = db.state_get(conn, state_key, "") or ""
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - when).total_seconds() / 60
    return age_min >= min_minutes


def _facts_chain_is_due(conn: Any) -> bool:
    return _chain_is_due(conn, FACTS_CHAIN_STATE_KEY, FACTS_CHAIN_MIN_MINUTES)


def _style_chain_is_due(conn: Any) -> bool:
    return _chain_is_due(conn, STYLE_CHAIN_STATE_KEY, STYLE_CHAIN_MIN_MINUTES)


def _run_chained_style_pass() -> None:
    """Re-measure the user's writing voice. Same contract as the facts pass:
    non-fatal, time-boxed, and its argv comes from importers.style_pass_cmd()
    rather than the registry, so it survives the UI row being retired."""
    try:
        subprocess.run(
            importers.style_pass_cmd(), capture_output=True, text=True,
            cwd=APP_DIR, env=os.environ.copy(), timeout=ENRICH_AFTER_IMPORT_SEC,
        )
    except Exception:  # noqa: BLE001 — a stale voice profile is not worth a crash
        pass


# NC-177. Where the background loop writes "this source is waiting for you to
# sign in again". One row per source, the value is the sentence the Data page
# shows, and an empty value means nothing is waiting. It is a blocker in the
# NC-076 sense — a source that cannot collect right now — so it is served on the
# same `blocked` key that LinkedIn and the Apple Contacts helper already use,
# and it gets the same treatment on the row: the warm tone, "Not collecting",
# and the sentence above the stamp rather than under it.
NEEDS_SIGN_IN_KEY = "import_needs_signin_"

# NC-183. And, beside it, WHO. The sentence above already names the accounts, but
# a sentence is prose: the panel that wants to put a Sign in button next to one
# address cannot get the address back out of it without parsing English. So the
# addresses are stored separately, comma-joined, written and cleared in the same
# breath as the sentence — never derived from it, and never the other way round.
NEEDS_SIGN_IN_WHO_KEY = "import_needs_signin_who_"


def _sign_in_sentence(source: str, accounts: Any = ()) -> str:
    """What the Data page says about a source that is waiting for a person.

    Written for somebody who has not been thinking about OAuth (the handshake
    where Google, not this app, collects the password and hands back a
    permission slip). It says what stopped, what to do, and — the part that
    matters most here — that nothing opened a window at them, because a Google
    sign-in tab appearing on its own is exactly what this text exists to prevent
    and exactly what a person would read as "I have been hacked".

    NC-181: it also says WHICH account, when the child named one. GMAIL_ACCOUNTS
    is a list and everything else the Data page knows about a source is keyed by
    source, so without this a person with several addresses configured was told
    "Gmail needs you to sign in again" and left to open Google once per address
    to find out which. The child stops at the first account that needs consent, so this is
    the first one and not necessarily the only one — which is why it says "this
    account" rather than implying the rest are fine.
    """
    label = importers.REGISTRY[source].label if source in importers.REGISTRY else source
    named = [a for a in (accounts or ()) if a]
    who = f" for {', '.join(named)}" if named else ""
    return (f"{label} needs you to sign in again{who}. The permission it was saved "
            f"with was withdrawn or has expired, so the automatic sync has stopped "
            f"and is not collecting. Open this row and run the import when you are "
            f"ready — that is when the Google tab opens, and not before.")


def _needs_sign_in(conn: Any, source: str) -> str:
    return db.state_get(conn, NEEDS_SIGN_IN_KEY + source, "") or ""


def _set_needs_sign_in(conn: Any, source: str, sentence: str,
                       accounts: Any = ()) -> None:
    """Record, or clear, the sign-in that is waiting on a person.

    Clearing is as important as setting and has one rule: a run that exited 0
    clears it. That covers the cure — the person clicks the button, the
    interactive run opens the Google tab, consent is given, the next exit is 0 —
    without anything having to know that is what happened.

    NC-183 adds `accounts`, cleared by the same call that clears the sentence so
    the two cannot drift apart into a page that offers a Sign in button for an
    address nothing is waiting on.
    """
    db.state_set(conn, NEEDS_SIGN_IN_KEY + source, sentence)
    who = ",".join(a for a in (accounts or ()) if a) if sentence else ""
    db.state_set(conn, NEEDS_SIGN_IN_WHO_KEY + source, who)


def needs_sign_in_accounts(conn: Any, source: str) -> List[str]:
    """Which addresses the last blocked run named, or [].

    Empty is a real answer and not the same as "nobody": the child stops at the
    first account that needs consent and does not always name it, so an empty
    list here means "something is waiting and we do not know which", which the
    caller has to handle rather than read as "nothing is waiting".
    """
    raw = db.state_get(conn, NEEDS_SIGN_IN_WHO_KEY + source, "") or ""
    return [a.strip() for a in raw.split(",") if a.strip()]


def _not_ready(source: str) -> str:
    """Why an API source cannot be attempted at all, or "".

    Narrower than _source_blockers() on purpose, and they are not the same
    question. A blocker says "this cannot collect right now" and LinkedIn's is a
    live token check — a source whose token expired should still be attempted,
    because the attempt is how it finds out it works again. This asks the
    smaller thing: is a piece of the setup simply absent, so that running it
    could not possibly do anything but fail?

    Never raises, for the same reason _source_blockers() never does: inventing a
    reason to skip a source is worse than missing one.
    """
    if source != "gmail":
        return ""
    try:
        import gmail_accounts
        missing = gmail_accounts.missing_credentials()
        unsigned = gmail_accounts.never_signed_in()
    except Exception:  # noqa: BLE001
        return ""
    if missing:
        return f"no client file for {', '.join(sorted(missing))}"
    # NC-117. An account that has never been signed in is just as un-runnable as
    # one with no file, and queueing it is actively worse than skipping it: the
    # unattended run exits 4 and writes "the permission it was saved with was
    # withdrawn or has expired" (NC-177's sentence) over a row whose permission
    # never existed. Wrong in a way the person cannot act on — they would go
    # looking for a revocation that never happened.
    if unsigned:
        return f"never signed in for {', '.join(sorted(unsigned))}"
    return ""


# NC-351. The three checks below — the sync helper, LinkedIn, Gmail — were asked
# again on every call of GET /api/import/sources, so a first-run screen that
# loads several steps in a row waited on all three each time, for an answer that
# had not changed. The answer is now remembered, and forgotten the moment
# something it was worked out from changes.
#
# "Something changed" is read, not announced: the settings those checks look at
# (the LinkedIn token, the Gmail addresses) and the small files they read (the
# helper's report and its installed marker, LinkedIn's check result, each Gmail
# account's sign-in file and saved permission) are compared with how they looked
# when the answer was worked out. Waiting for each place that changes one of them
# to say so would miss the ones that live outside this module — the helper
# rewrites its own report every couple of minutes — and a missed one is a Data
# page saying something that stopped being true.
#
# The lifetime is a backstop for an input this list does not know about, not the
# mechanism; 0 turns the memory off.
SOURCE_CHECK_CACHE_DEFAULT = 30


def source_check_cache_sec() -> int:
    """How long an unchanged answer to "can each source collect?" may be reused
    (NC_SOURCE_CHECK_CACHE_SEC), read at call time. 0 asks every time; clamped
    0–600."""
    try:
        secs = int(os.environ.get("NC_SOURCE_CHECK_CACHE_SEC",
                                  SOURCE_CHECK_CACHE_DEFAULT))
    except (TypeError, ValueError):
        secs = SOURCE_CHECK_CACHE_DEFAULT
    return max(0, min(600, secs))


_checks_lock = threading.Lock()
_checks_memory: Dict[str, Any] = {"at": 0.0, "seen": None, "answer": {}}


def forget_source_checks() -> None:
    """Drop the remembered answer, so the next ask works it out again. For tests,
    and for any code that changes one of the inputs by a route the comparison
    below cannot see."""
    with _checks_lock:
        _checks_memory.update({"at": 0.0, "seen": None, "answer": {}})


def _file_mark(path: str) -> Any:
    """How a file looks right now (when it was last written, how big), or None
    when it is not there — enough to notice that it changed, without reading it."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _source_check_inputs() -> tuple:
    """Everything the three source checks are worked out from, as one value that
    compares equal while none of it has changed.

    Secrets are only noted as present or absent: this value is held in memory for
    the length of the cache and has no business holding a token.
    """
    import gmail_accounts      # light on purpose, like the probes (see NC-180 below)
    accounts = gmail_accounts.accounts_from_env()
    li = linkedin_token.credentials()
    return (
        bool(li["token"]), bool(li["client_id"]), bool(li["client_secret"]),
        _file_mark(linkedin_token.REPORT_PATH),
        _file_mark(importers.IMSG_HELPER_PLIST),
        _file_mark(importers.IMSG_HELPER_REPORT),
        tuple((email, creds, _file_mark(creds),
               _file_mark(gmail_accounts.token_path(email)))
              for email, creds in sorted(accounts.items())),
    )


def _source_blockers() -> Dict[str, str]:
    """{source_key: reason} for sources that cannot collect right now.

    The three checks themselves are in _probe_source_blockers. This is the
    answer remembered between calls (NC-351): worked out again when anything the
    checks read has changed, or when NC_SOURCE_CHECK_CACHE_SEC has run out.
    Returns a copy, because callers add to what they are given.
    """
    ttl = source_check_cache_sec()
    seen = None
    if ttl:
        try:
            seen = _source_check_inputs()
        except Exception:  # noqa: BLE001 — can't tell what changed, so don't remember
            seen = None
        if seen is not None:
            with _checks_lock:
                fresh = time.monotonic() - _checks_memory["at"] < ttl
                if fresh and _checks_memory["seen"] == seen:
                    return dict(_checks_memory["answer"])
    answer = _probe_source_blockers()
    if ttl and seen is not None:
        with _checks_lock:
            _checks_memory.update({"at": time.monotonic(), "seen": seen,
                                   "answer": dict(answer)})
    return dict(answer)


def _probe_source_blockers() -> Dict[str, str]:
    """The three checks, run for real: {source_key: reason} for sources that
    cannot collect right now.

    NC-076. The same two owners api_setup._blockers() asks, for the same reason:
    the module that makes a decision exports it, and nobody reimplements it.
    Kept as its own function rather than imported from api_setup so this module
    does not depend on the setup panel merely to describe its own sources.

    Never raises — a probe that fails reports nothing, because inventing a
    blocker is worse than missing one.
    """
    out: Dict[str, str] = {}
    try:
        import api_linkedin
        reason = api_linkedin.collection_blocked()
        if reason:
            out["linkedin"] = reason
    except Exception:  # noqa: BLE001
        pass
    try:
        import api_fda
        out.update(api_fda.collection_blocked())
    except Exception:  # noqa: BLE001
        pass
    try:
        # NC-180. gmail_accounts, NOT gmail_import: this runs on every Data page
        # load and gmail_import pulls in google.auth, google_auth_oauthlib and
        # googleapiclient at import time. The light module exists for exactly
        # this call.
        import gmail_accounts
        reason = gmail_accounts.collection_blocked()
        if reason:
            out["gmail"] = reason
    except Exception:  # noqa: BLE001
        pass
    return out


def source_blockers(conn: Any) -> Dict[str, str]:
    """Every reason a source cannot collect right now, keyed by source.

    The probes (LinkedIn's token, the Apple Contacts helper's permission) and
    the sign-ins waiting on a person (NC-177), in one dictionary, with the
    waiting sign-in winning where both are set — it is the specific one, and the
    only one with something to click.

    It is one function because it had two callers on one screen, and still
    has: the old Data page's expandable rows and its source list each read it,
    and a blocker fed to one and not the other was the page disagreeing with
    itself in two places a person could see at once. That is the whole defect
    NC-076 was about: the sentence NC-177 added had to reach the row endpoint and
    the list alike, or the list went on saying "Has not run here yet" underneath
    it. The rebuilt Data page (crm/api_ledger.py, since
    NC-236 the only one) reads it once, for every row.
    """
    out = dict(_source_blockers())
    for key in importers.REGISTRY:
        waiting = _needs_sign_in(conn, key)
        if waiting:
            out[key] = waiting
    return out


def handle_import_sources(conn: Any) -> Dict[str, Any]:
    """GET /api/import/sources — the registry plus per-source last-synced stamps
    (recorded by handle_import_run in the state table)."""
    sources = importers.list_sources()
    # NC-076. `synced_at` is a stamp from the LAST run that exited 0. It is a
    # fact about the past, and a row that renders it as a green "Synced
    # yesterday" — the freshest tone on the ramp — for a source whose token has
    # since been removed would sit on the same page as a panel headlined "Your
    # LinkedIn messages are not being collected". Each reads true data alone.
    #
    # `blocked` rides along on the spec rather than being threaded down as a
    # prop because SourceRow is rendered from six places; a fact that travels
    # with the thing it describes cannot be passed at five of them. A fact that
    # reaches only some of the places that show it is a scoping failure.
    blocked = source_blockers(conn)
    for s in sources:
        s["synced_at"] = db.state_get(conn, f"import_synced_{s['key']}", "") or ""
        s["blocked"] = blocked.get(s["key"], "")
    # NC-122: over `active`, like every other count of people on the Data page.
    # These counted the whole table, so a hidden person's photo was in "N with
    # photos" while their history was not in "N with history" on the same row.
    active = figures.population_where("active")
    photos_count = conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE photo != '' "
        f"AND {active}").fetchone()["n"] or 0
    # Per source as well as in total, because the total was being displayed AS a
    # single source's own figure: the Data page's photo rows read `photos_count`,
    # so WhatsApp's photo row claimed every face on the list, including the ones
    # LinkedIn and the vCard put there. contacts.photo_source is the winning
    # token photos.save_photo() writes (crm/photos.py:178), so grouping on it is
    # the real attribution — "whatsapp", "linkedin", "contacts", "google",
    # "manual". Needed by NC-030: WhatsApp's own row now reports the photos the
    # folded photo import brought in, and a row must not overstate that.
    photos_by_source = {
        (r["photo_source"] or ""): r["n"] for r in conn.execute(
            "SELECT photo_source, COUNT(*) AS n FROM contacts "
            f"WHERE photo != '' AND {active} GROUP BY photo_source")
    }
    return {"sources": sources, "photos_count": photos_count,
            "photos_by_source": photos_by_source,
            # NC-176. How long one import subprocess is allowed to take. The page
            # needs it because it has to outlast this rather than race it: it was
            # giving up after 20 seconds while the server ran on for 300, so an
            # import that took half a minute was reported as a failure and then
            # quietly succeeded. Sent rather than duplicated in the JS, because
            # NC_IMPORT_TIMEOUT_SEC can move it.
            "import_timeout_sec": import_timeout_sec()}


def handle_import_detect(conn: Any) -> Dict[str, Any]:
    """GET /api/import/detect — importable data found on this Mac right now
    (the WhatsApp container, when the helper has not copied it; exports/copies in
    Downloads and Desktop), each ready to run with a prefilled path. No copying, no pickers.

    `auto` marks the ones that need no click at all (see run_autosync): the
    WhatsApp container, the iMessage import folder the user pastes into, and the
    Apple Contacts copy the FDA helper writes.
    It is a promise the Data page repeats to the user in words — its `auto` rows
    say a source "syncs itself and is current" and offer no control — so it must
    match what run_autosync will really do, path shape alone is not enough. Before
    setup is finished _autosync_pending returns nothing at all, so on a first run
    every finding is a click, and saying otherwise would be a lie on the one screen
    a stranger reads first. Those findings surface under "Needs you" instead, which
    is where something you have to act on belongs.

    `live` is the narrower claim — read in place, so always current. Only the
    WhatsApp container qualifies. Everything else, the pasted chat.db included,
    is a snapshot, so it gets a real `stale_days`: this used to be computed only
    for non-auto findings, which meant a pasted copy reported "0 days old"
    forever no matter how old it actually was.

    There is a second way to be current, added because the Apple Contacts row
    would otherwise keep asking to be pasted over: a copy the FDA helper vouched
    for on its last run (`helper_current`). That is not the same as `live` — it is a
    snapshot — but its freshness is guaranteed by the helper's comparison rather
    than by its own mtime, and the helper skips files it already has, so mtime is
    the wrong question for exactly these sources."""
    found = []
    sync_may_run = (api_instance.onboarding_done(conn)
                    and os.environ.get("NC_AUTOSYNC", "1") != "0")
    # The switch, not just the path: a source whose own NC_*_AUTOIMPORT is off is
    # not automatic, and a row that says "syncs itself" while its switch is off
    # would be the promise in this docstring being broken in the other direction.
    auto_paths = {p for p, switch, _fp in auto_import_paths()
                  if os.environ.get(switch, "1") != "0"}
    for f in importers.detect_sources():
        path = f.get("path", "")
        live = path.startswith(importers.WA_CONTAINER)
        auto = sync_may_run and (live or path in auto_paths)
        # A copy the helper vouched for on its last run is current, whatever its
        # mtime says. The helper deliberately does not re-copy a file it already
        # has — that is what makes a two-minute timer cheap — so an address book
        # nobody has edited for a fortnight keeps a fortnight-old mtime while
        # being byte-identical to the live original.
        #
        # Reading mtime for these sources is what put the Apple Contacts row in
        # "Needs you" saying "This copy stopped on the day it was made. Paste a
        # fresh copy into the import folder" — advice with no possible action
        # behind it, since Contacts and Calendar have no paste-it-yourself route
        # at all, on a source the helper had checked minutes earlier.
        vouched = bool(f.get("helper_current"))
        stale = 0
        if not live and not vouched:
            try:
                stale = max(0, int((datetime.now(timezone.utc).timestamp()
                                    - os.path.getmtime(path)) // 86400))
            except OSError:
                stale = 0
        found.append({**f, "auto": auto, "live": live, "stale_days": stale})
    # The opt-in Full Disk Access helper used to be reported here too, as
    # "imessage_helper": {installed, installer}. It moved to GET /api/fda/status
    # (crm/api_fda.py), because two of the three things the UI needs
    # were never here: which sources macOS is actually blocking right now, and the
    # actions to fix it. Two endpoints answering "what about the helper" with
    # different amounts of the truth is how the UI ended up telling the user it was
    # already granted when it was not. One source of truth, and this is not it.
    return {"found": found}


def handle_import_open(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/import/open — open a Finder folder or export page.

    Targets come only from the server-side allowlist (importers.OPEN_TARGETS);
    the client sends just a key from it, never a path. Some targets try more than
    one command — iMessage reveals the three chat.db files pre-selected and falls
    back to opening the plain folder if macOS refuses that.

    NC-348. The fallback used to work only when the first command ran and failed.
    If it could not run at all (the program missing) or hung past the time limit,
    the whole call gave up with ok:false and never tried the next command, so the
    folder stayed shut. Every command is tried in turn now; only when all of them
    have failed does the answer say so, with a sentence the page can show — the
    button used to do nothing and say nothing."""
    source = str(body.get("source") or "").strip().lower()
    target = importers.resolve_open_target(source)
    if target is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"no open helper for '{source}'")
    for argv in target["argv"]:
        try:
            if subprocess.run(argv, capture_output=True, timeout=15).returncode == 0:
                return {"ok": True}
        except (OSError, subprocess.TimeoutExpired):
            continue
    return {"ok": False,
            "message": "macOS would not open it. Try the button again; if it "
                       "still does nothing, open the folder or page yourself."}


# ── Calendar download — NEW OUTBOUND NETWORK EGRESS ──────────────────────────
# READ THIS BEFORE CHANGING ANYTHING BELOW.
#
# This is the FIRST and ONLY request this application makes to the internet that
# is not an Ask/Anthropic call. Every other import in this file runs a local
# subprocess over a file the user already has. ARCHITECTURE.md §7 lists this GET
# among what leaves the machine, and has to keep listing it.
#
# What leaves the Mac: one HTTPS GET to the calendar address the user pasted into
# Settings, carrying that address, a User-Agent and an Accept header — nothing
# else. No contact, no message, no note, no counts, no telemetry; there is no
# request body and nothing is uploaded. What comes back is written to a snapshot
# file under data/ that the user can open in Calendar.app, because "the app read
# my calendar" should be inspectable rather than a claim.
#
# It is opt-in three times over, and all three are deliberate:
#   1. NC_ICS_FETCH must be "1". Default "0" — a pasted URL does nothing at all
#      until the switch is on, so upgrading cannot start traffic by surprise.
#   2. The URL must be present in Settings; there is no discovery, no probing and
#      no default endpoint anywhere in this code.
#   3. ~~A human must click Run import on a calendar source.~~ **Gone (NC-031),
#      and this is the one thing here that changed.** The
#      background loop now fetches these on a clock — API_SOURCES has calendar
#      entries, NC_CALENDAR_AUTO turns them off, NC_CALENDAR_EVERY_HOURS sets the
#      period (default 24h). The row was otherwise
#      a button on something that keeps itself current everywhere else.
#      Gates 1 and 2 are untouched and they are the ones that matter — an install
#      that has never set NC_ICS_FETCH to "1" makes no request on any schedule,
#      and there is still no URL anywhere until the user pastes one.
# The .ics FILE import ignores all of this — a file you exported needs no network.
#
# The URL is a bearer credential (see settings.py: anyone holding it reads the
# whole calendar, no sign-in, no trace). Consequences enforced below: https only,
# never downgraded across a redirect, never logged, never put in a query string,
# and never echoed back in an error message.
CALENDAR_FETCH_TIMEOUT_DEFAULT = 20
CALENDAR_MAX_KB_DEFAULT = 5120
CALENDAR_READ_CHUNK = 65536
# Identifies us honestly to Google/Apple; carries no version of anything private.
CALENDAR_USER_AGENT = "NetworkCRM/1 (local calendar import; stdlib urllib)"


def calendar_fetch_enabled() -> bool:
    """Is downloading a calendar allowed right now? (NC_ICS_FETCH, default off.)

    Read at call time, never captured at import — same rule as
    import_timeout_sec(). A kill switch that only took effect after a restart
    would be worse than no kill switch, because the user would believe the
    traffic had stopped.
    """
    return os.environ.get("NC_ICS_FETCH", "0").strip() == "1"


def calendar_fetch_timeout_sec() -> int:
    """Seconds before a calendar download is abandoned (NC_ICS_FETCH_TIMEOUT_SEC).

    Read at call time. Clamped 5–300: the clamp bounds a user setting, it does
    not invent one — the value itself is a MANAGED_KEYS entry with a documented
    default, because no limit may be a silent constant.
    """
    try:
        secs = int(os.environ.get("NC_ICS_FETCH_TIMEOUT_SEC",
                                  CALENDAR_FETCH_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        secs = CALENDAR_FETCH_TIMEOUT_DEFAULT
    return max(5, min(300, secs))


def calendar_max_bytes() -> int:
    """Size ceiling on an accepted download (NC_ICS_MAX_KB), read at call time.

    A year of a busy calendar is well under 1 MB. The cap exists so a wrong
    address — a video, a log endpoint, an infinite stream — cannot fill data/.
    """
    try:
        kb = int(os.environ.get("NC_ICS_MAX_KB", CALENDAR_MAX_KB_DEFAULT))
    except (TypeError, ValueError):
        kb = CALENDAR_MAX_KB_DEFAULT
    return max(16, min(102400, kb)) * 1024


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while it stays on https.

    urllib's default handler replays the request at whatever Location says,
    including plain http. For a bearer-credential URL that is a downgrade to
    clear text on someone else's network, so it is refused instead of followed.
    Cross-HOST https redirects are allowed — Google and iCloud both bounce
    between hostnames, and refusing that would break the feature for no gain.
    urllib's own max_redirections (10) still applies.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        scheme = urllib.parse.urlparse(newurl).scheme.lower()
        if scheme != "https":
            # urllib's http_error_302 only drains and closes fp on the paths
            # where redirect_request RETURNS, so refusing has to close it here or
            # a long-lived server process leaks a socket per refusal.
            try:
                fp.close()
            except OSError:
                pass
            raise ApiError(
                HTTPStatus.BAD_GATEWAY,
                f"That calendar address redirected to a non-https ({scheme or 'unknown'}) "
                "location. Refused: the link is a password and this would have sent it "
                "in clear text. Nothing was downloaded.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _calendar_https_url(raw: str) -> str:
    """Normalize a pasted calendar address to https, or refuse it.

    `webcal://` is what Calendar.app copies and it is just https with another
    name — rewriting it is the single transformation allowed here. Anything that
    is not then https (http, file, ftp, a bare path) is refused rather than
    coerced: silently upgrading http would hide that the user pasted the wrong
    thing, and file:// would turn a network setting into a local file read.
    """
    url = (raw or "").strip()
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in ("webcal", "webcals"):
        url = urllib.parse.urlunsplit(("https",) + tuple(parts[1:]))
        parts = urllib.parse.urlsplit(url)
        scheme = "https"
    if scheme != "https" or not parts.netloc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That calendar address is not an https:// or webcal:// URL, so it was not "
            "fetched. Copy the whole link from Google Calendar's 'Secret address in "
            "iCal format' or Calendar.app's Share Calendar sheet. If you pasted "
            "more than one, put a comma between them.")
    return url


# Where one pasted address ends and the next begins (NC-303). Commas, semicolons
# and any white space separate them, so one per line works wherever a box keeps
# its line breaks. The scheme split catches the other way a paste goes wrong: a
# single-line box drops the line breaks, and the addresses arrive end to end.
# Google's and Apple's addresses never contain a second "https://", so a scheme
# in the middle of a piece is always the start of the next address.
_ADDRESS_SEPARATORS = re.compile(r"[\s,;]+")
_ADDRESS_START = re.compile(r"(?=(?:https?|webcals?)://)", re.IGNORECASE)


def calendar_addresses(raw: str) -> List[str]:
    """The separate addresses stored on a calendar row, in the order given.

    NC-303. The row held one address, so a person with two Google calendars could
    read only one. Several may be stored in the same setting, separated by commas
    or line breaks; a single address comes back as a list of one. Nothing here
    checks that a piece is a good address: that is _calendar_https_url's job, so
    a refusal reads the same for one address as for ten.
    """
    pieces: List[str] = []
    for chunk in _ADDRESS_SEPARATORS.split(raw or ""):
        pieces.extend(p for p in _ADDRESS_START.split(chunk) if p)
    return pieces


def _download_calendar(source: str, url: str, host: str, where: str, limit: int,
                       timeout: int) -> bytes:
    """One calendar address, fetched and checked: its bytes, or an ApiError.

    `where` is how an error names this address — the host alone when the row
    holds one address, the host and its place in the list when it holds several —
    and never the address itself (see fetch_calendar_ics).
    """
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": CALENDAR_USER_AGENT,
        "Accept": "text/calendar, text/plain;q=0.5",
    })
    # A local opener, never install_opener(): installing ours globally would
    # change the redirect policy of any other urllib caller in the process.
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    body = b""
    content_type = ""
    try:
        with opener.open(request, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            declared = resp.headers.get("Content-Length") or ""
            if declared.isdigit() and int(declared) > limit:
                raise ApiError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"{where} offered {int(declared) // 1024} KB of calendar, over the "
                    f"{limit // 1024} KB ceiling (NC_ICS_MAX_KB). Nothing was saved.")
            # Read to the cap plus one byte: that extra byte is how an
            # unbounded/chunked response is caught when Content-Length lied or
            # was absent, instead of streaming forever into memory.
            while len(body) <= limit:
                chunk = resp.read(CALENDAR_READ_CHUNK)
                if not chunk:
                    break
                body += chunk
    except urllib.error.HTTPError as exc:
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            f"{where} answered {exc.code} for that calendar address, so nothing was "
            "downloaded. If it is a Google secret address, it changes whenever you "
            "reset it — copy the current one from Google Calendar settings.") from exc
    except urllib.error.URLError as exc:
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            f"Could not reach {where if host else 'that calendar address'} within "
            f"{timeout}s ({exc.reason}). Nothing was downloaded.") from exc
    except OSError as exc:   # socket.timeout, TLS failures, a dropped connection
        raise ApiError(HTTPStatus.BAD_GATEWAY,
                       f"The calendar download from {where} failed ({exc}). Nothing "
                       "was saved.") from exc
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 — see below; this clause exists to
        # STOP the exception text reaching the browser, not to diagnose.
        #
        # The URL is a bearer credential: whoever holds a Google secret address can
        # read that calendar. Every message above names only `host`, deliberately,
        # because the secret lives in the PATH. But http.client.InvalidURL — raised
        # for a URL with a stray space or control character, i.e. exactly what a
        # bad paste produces — subclasses HTTPException, which is NOT OSError and
        # NOT URLError. So it escaped all three clauses, reached app.py's handler,
        # and was returned to the browser as str(exc): the full secret URL, in a
        # response body, from a mistyped setting.
        #
        # The type name is safe to show (it is a class name, never the value); the
        # message is not, so it goes to the log and not to the response.
        log.warning("calendar fetch failed for %s: %s: %s",
                    source, type(exc).__name__, exc)
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            f"The calendar address for {where or 'that calendar'} could not be used "
            f"({type(exc).__name__}). Check it for a stray space or a missing "
            "https:// prefix, then paste it again. Nothing was downloaded.") from exc

    if len(body) > limit:
        raise ApiError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"That address kept sending past the {limit // 1024} KB ceiling "
            "(NC_ICS_MAX_KB), so the download was abandoned and nothing was saved. "
            "A year of a busy calendar is normally under 1 MB.")
    # Cheap identity check before anything hits the disk. A wrong or expired
    # address usually answers 200 with a sign-in page, and saving that as
    # google.ics would make the next import report "0 events" as though the
    # calendar were empty — a silent drop. Refuse loudly instead.
    head = body[:200].lstrip(b"\xef\xbb\xbf").lstrip()
    if not head.upper().startswith(b"BEGIN:VCALENDAR"):
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            f"{where} returned {len(body)} bytes that are not a calendar"
            f"{f' (Content-Type: {content_type})' if content_type else ''} — most "
            "likely a sign-in or error page, which means the stored link is stale. "
            "Nothing was saved.")
    return body


def fetch_calendar_ics(source: str) -> Dict[str, Any]:
    """Download one URL-fed calendar into its snapshot file. NEW EGRESS — see the
    block comment above; nothing else in this app makes this kind of call.

    Returns {"source", "path", "bytes", "host", "calendars", "note"} on success.
    Every refusal raises ApiError with a sentence that says what was refused and
    why, because what an import declined has to be as visible as what it
    accepted. The URL itself never appears in a return value, a message or a log
    line — only its host, which is always google.com or icloud.com and tells an
    onlooker nothing.

    NC-303. The row may hold several addresses (see calendar_addresses). All of
    them are checked before any is fetched, and all of them must come back good:
    one that fails stops the whole download and saves nothing, naming which one,
    because a snapshot missing a calendar would read as that calendar having no
    meetings. The calendars are written one after another into the one snapshot
    file — a file of several calendars is valid and ics_import reads every event
    in it — so nothing after the download needs to know there was more than one.
    """
    spec = importers.CALENDAR_URL_SOURCES.get(source)
    if spec is None:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       f"'{source}' is not a URL-fed calendar (expected one of "
                       f"{sorted(importers.CALENDAR_URL_SOURCES)})")
    if not calendar_fetch_enabled():
        raise ApiError(
            HTTPStatus.FORBIDDEN,
            f"Downloading your {spec['label']} is switched off, so nothing was fetched. "
            "Switch on \"Download linked calendars\", on this row or in Settings. "
            "Only the address you pasted is sent to the calendar's own service. "
            "Prefer to send nothing at all? Save the calendar to a file and use the "
            "Calendar file row instead.")
    addresses = calendar_addresses(os.environ.get(spec["env"], "") or "")
    if not addresses:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"No {spec['label']} address is stored, so nothing was fetched. Paste the "
            f"link into {spec['env']} under Settings first. Treat it like a password: "
            "anyone holding it can read that whole calendar.")
    urls = [_calendar_https_url(a) for a in addresses]     # all checked before any is fetched
    limit = calendar_max_bytes()
    timeout = calendar_fetch_timeout_sec()

    bodies: List[bytes] = []
    hosts: List[str] = []
    for i, url in enumerate(urls, 1):
        host = urllib.parse.urlsplit(url).hostname or ""
        where = host if len(urls) == 1 else f"{host or 'a calendar address'} (address {i} of {len(urls)})"
        bodies.append(_download_calendar(source, url, host, where, limit, timeout))
        if host not in hosts:
            hosts.append(host)
    host = ", ".join(h for h in hosts if h)
    if len(bodies) == 1:
        body = bodies[0]
    else:
        # One calendar after another, each ending on a line break, so the last
        # line of one can never run into the first line of the next, and with
        # any byte-order mark at the start of a download dropped, so it cannot
        # end up in the middle of the file.
        bodies = [b.removeprefix(b"\xef\xbb\xbf") for b in bodies]
        body = b"".join(b if b.endswith(b"\n") else b + b"\r\n" for b in bodies)

    path = importers.calendar_snapshot_path(source)
    # 0700/0600: this file lists every meeting the user has, same reasoning as
    # the iMessage import folder. Written to a .part and renamed, so an
    # interrupted download can never leave a truncated snapshot that a later
    # import would silently read as a shorter calendar.
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                       f"Downloaded the calendar but could not save it ({exc}).") from exc
    many = f", {len(bodies)} calendars" if len(bodies) > 1 else ""
    return {"source": source, "path": path, "bytes": len(body), "host": host,
            "calendars": len(bodies),
            "note": (f"Downloaded {len(body)} bytes of calendar from {host} to "
                     f"{path} — {spec['label']}{many}. Nothing was uploaded.")}


def handle_import_run(body: Dict[str, Any]) -> Dict[str, Any]:
    """Run the importer/enricher for `source` as a subprocess on the same venv
    interpreter, using the argv the registry builds for it.

    Captures stdout+stderr and returns it verbatim so the UI can show the
    importer's own progress lines. Never raises on importer failure — a non-zero
    exit becomes ok=False with the output, so the front end can render it.
    `receipts` distills the output into verb-count chips ("Archived
    1,204") via importers.parse_receipts; the raw log stays for the disclosure.
    """
    source = str(body.get("source") or "").strip().lower()
    # The one place a calendar is downloaded (NEW EGRESS — see the block comment
    # above fetch_calendar_ics). It happens HERE, in the endpoint a human clicked,
    # and not inside the importer subprocess: the argv that follows names a plain
    # local file, so ics_import.py never sees the secret URL and makes no network
    # call of its own. A refusal raises ApiError and the import does not run —
    # deliberately louder than falling back to a stale snapshot on disk, which
    # would show yesterday's calendar as though it were today's.
    fetched: Optional[Dict[str, Any]] = None
    if source in importers.CALENDAR_URL_SOURCES:
        fetched = fetch_calendar_ics(source)
        body = {**body, "path": fetched["path"]}
    # Whatever the run prints, prefixed with what came off the network, so the
    # receipt in the UI accounts for the request as well as the parse.
    prelude = f"{fetched['note']}\n" if fetched else ""
    cmd = _build_command(source, body)
    dry_run = bool(body.get("dry_run"))
    # No preparation of the copy any more: imessage_import.snapshot_uri() reads
    # the -wal alongside the main file, so a manual run and the background import
    # see the same messages without either of them writing to the snapshot.
    # how many contacts existed before, so we can tell the user how many NEW
    # people this import placed — and bridge them into describing them
    before = 0
    try:
        _c = db.get_db(); before = db.counts(_c)["total"]; _c.close()
    except Exception:  # noqa: BLE001
        before = 0
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=APP_DIR,
            env=os.environ.copy(), timeout=import_timeout_sec(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "output": prelude + f"Timed out after {import_timeout_sec()}s.",
                "fetched": fetched}
    output = prelude + (result.stdout or "") + (result.stderr or "")
    newly_added = 0
    enriched: List[Dict[str, Any]] = []
    # Everything below this line is bookkeeping that only makes sense because an
    # import CHANGED the database. A source with writes=False (the three calendar
    # previews) changed nothing, so it gets none of it — no engagement refresh
    # over unchanged rows, no chained enrichment chip, and above all no "last
    # synced" stamp, which the Data card renders as though data had landed.
    spec = importers.REGISTRY.get(source)
    wrote = bool(spec.writes) if spec is not None else True
    if result.returncode == 0 and not dry_run and wrote \
            and spec is not None and spec.category == "import" \
            and (os.environ.get("NC_ENRICH_AUTO", "1").strip() != "0"):
        enriched = _run_chained_facts_pass()
    if result.returncode == 0 and not dry_run and wrote:
        # Stamp the source as synced, then keep the familiarity score honest (fold
        # new archive rows into the engagement features).
        #
        # NC-198. These were one try block with the score FIRST and `except: pass`
        # after, so when the score threw — as it did on every fresh install, whose
        # empty archive.db had no messages table — the stamp, the sign-in clear,
        # the pasted-copy mark and the new-people count were all skipped, silently.
        # The first import a new user ever ran then read "Has not run here yet"
        # beside the people it had just added. The stamp goes first now, the score
        # runs on its own, and a failure in either is logged, not swallowed.
        conn = None
        try:
            conn = db.get_db()
            db.state_set(conn, f"import_synced_{source}",
                         datetime.now(timezone.utc).isoformat(timespec="seconds"))
            # NC-177. The cure for a waiting sign-in, and the only one: this is
            # the interactive run, the Google tab opened because a person asked
            # for it, and it came back 0. Clear the line off the Data page.
            if _needs_sign_in(conn, source):
                _set_needs_sign_in(conn, source, "")
            # this snapshot is now imported — the background loop can skip it
            _stamp_pasted_copy(conn, str(body.get("path") or ""))
            newly_added = max(0, db.counts(conn)["total"] - before)
            conn.commit()
        except Exception:  # noqa: BLE001 — bookkeeping must never fail an import
            log.exception("import of %s succeeded; recording that it ran failed", source)
        try:
            if conn is not None:
                engagement.refresh(conn)
        except Exception:  # noqa: BLE001 — nor may the familiarity score
            log.exception("import of %s succeeded; updating familiarity failed", source)
        finally:
            if conn is not None:
                conn.close()
    return {"ok": result.returncode == 0, "output": output.strip(),
            "receipts": importers.parse_receipts(output), "newly_added": newly_added,
            "enriched": enriched, "dry_run": dry_run,
            # None for every source but the two URL-fed calendars. Carries the
            # host, the byte count and the snapshot path — never the URL.
            "fetched": fetched}


def handle_import_pick_file(body: Dict[str, Any]) -> Dict[str, Any]:
    """Show a native macOS file/folder picker via osascript; return the chosen path.

    Returns {"path": "...", "cancelled": bool, "error": str}. Exactly one of those
    last two is truthy when path is empty, and the caller MUST tell them apart.

    WHY. This used to return a bare {"path": ""} for every non-success — the user
    cancelling, osascript missing, the dialog failing to open, a timeout — and the
    frontend's `if (d.path)` then did nothing at all. So a picker that could not
    open was indistinguishable from a picker the user dismissed: you click
    "Choose file", nothing happens, no message, no error, forever. From the Data
    page that looks like an import of contact cards that does not work, when the
    importer itself is fine end to end — including Apple's folded base64 photos
    and item-grouped TEL/EMAIL properties.

    Cancelling is the one case that must stay silent, because a toast saying "you
    cancelled" after you cancelled is noise. Everything else has to say something.
    """
    kind = str(body.get("kind") or "").strip().lower()
    script = PICK_SCRIPTS.get(kind)
    if script is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"kind must be one of {sorted(PICK_SCRIPTS)}")
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return {"path": "", "cancelled": False,
                "error": "This Mac has no osascript, so the file picker cannot open. "
                         "Type or paste the path into the field instead."}
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": False,
                "error": "The file picker was open for two minutes without a choice, "
                         "so it was closed. Try again."}
    if r.returncode == 0:
        return {"path": r.stdout.strip().rstrip("\n"), "cancelled": False, "error": ""}
    # osascript signals a dismissed dialog as error -128. That is a normal outcome
    # and not worth a message; anything else is a real failure the user needs told.
    err = (r.stderr or "").strip()
    if "-128" in err or "user canceled" in err.lower() or "user cancelled" in err.lower():
        return {"path": "", "cancelled": True, "error": ""}
    return {"path": "", "cancelled": False,
            "error": ("The file picker could not open"
                      + (f": {err.splitlines()[-1]}" if err else ".")
                      + " You can paste the path into the field instead.")}


# ── Auto-sync: the imports that need no click ────────────────────────────────
# Two kinds, gated differently:
#
#  • WhatsApp Desktop's container, only when the helper has not copied it:
#    reading it in place raises a macOS consent dialog (NC-192), so at most
#    once a day (the daily stamp), and never once the helper covers it.
#  • The iMessage import folder holds a copy the user pasted there. A date gate
#    is wrong for it: a paste at 4pm must not wait for tomorrow, and re-pasting
#    the same bytes must not re-run a large import. So it is gated on the
#    file's own fingerprint instead — new snapshot in, one import out.
#
# Cadence and the iMessage half are settings, not constants: NC_AUTOSYNC=0 turns
# everything off, NC_IMSG_AUTOIMPORT=0 only the pasted-copy half, and
# NC_AUTOSYNC_MINUTES sets how often the (cheap) check runs.
AUTOSYNC_STATE_KEY = "autosync_last"
IMSG_PASTE_FP_KEY = "imsg_paste_fp"
# Separate from the above on purpose — see _paste_fp_key.
WA_PASTE_FP_KEY = "wa_paste_fp"
# Apple Contacts. The autosync loop gained the AB_AUTO_DB path when NC-029 made
# contacts automatic, but _paste_fp_key was never taught about it, so it returned
# "" — which meant `done` was read from the empty state key and
# _stamp_pasted_copy returned early without recording anything. Net effect: the
# address book re-imported on EVERY two-minute tick, forever, over the whole
# address book, and nothing anywhere said so. A fingerprint key per copy is not
# boilerplate; a missing one is a silent hot loop.
#
# NC-103: a key alone is not enough. The stamping CALL was guarded by
# `if f["path"] == importers.IMSG_AUTO_DB`, so having a key changed nothing for
# this source or three others — only `imsg_paste_fp` was ever written.
AB_PASTE_FP_KEY = "ab_paste_fp"
# WhatsApp's profile-photo cache, once the helper copies it (NC-047). A DIRECTORY
# rather than a single file, so it is fingerprinted by walking it — see _dir_fp.
WA_PHOTOS_PASTE_FP_KEY = "wa_photos_paste_fp"
CAL_PASTE_FP_KEY = "calendar_local_paste_fp"
# 2 minutes, down from 15. Safe at any value because the tick is
# genuinely free when nothing changed: two os.path.getmtime calls, two state-table
# point reads, and `if not pending: return 0` before anything is written, logged or
# spawned. An actual import pass costs a few seconds, and it only runs when the
# copy's mtime has moved.
#
# It does NOT speed up Gmail or LinkedIn, and that is not luck — _api_hours()
# floors every API source at one hour and run_autosync stamps the attempt BEFORE
# making the call, both against a persisted timestamp rather than this loop's
# sleep. A fast tick only makes them answer "not yet" more often. Those two
# properties are what make this knob safe to turn down; don't remove them.
AUTOSYNC_MINUTES_DEFAULT = 2

# ── The API-backed half ───────────────────────────────────────────────────────
# Gmail and LinkedIn need no file at all: Gmail holds an OAuth refresh token that
# renews itself, LinkedIn a member token in .env. So unlike every source above,
# they can't be found by looking at the disk — nothing appears in
# detect_sources() — and they're due on a clock instead.
#
# Two reasons the clock is hours and not "every launch": both call somebody
# else's rate-limited API, and this app gets restarted often. `hours` is the
# floor between *attempts*, not successes — a dead token stamps the attempt like
# any other, so a broken source retries once per period rather than on every tick
# of the loop. Same discipline as the daily WhatsApp stamp.
#
# `needs` names the setting without which the source cannot run at all. The
# switch defaults to on, so without this gate a brand-new install — which has
# configured nothing — would queue Gmail every 12 hours and LinkedIn every 24,
# each one failing on a missing credential, forever. Stamping the attempt keeps
# that from becoming a hot loop but does not make it any less pointless. An
# unconfigured source is not a broken source; it should simply not be attempted.
API_SOURCES: Dict[str, Dict[str, Any]] = {
    "gmail":    {"switch": "NC_GMAIL_AUTO",    "hours_env": "NC_GMAIL_EVERY_HOURS",
                 "hours": 12, "channel": "email", "needs": "GMAIL_ACCOUNTS"},
    "linkedin": {"switch": "NC_LINKEDIN_AUTO", "hours_env": "NC_LINKEDIN_EVERY_HOURS",
                 "hours": 24, "channel": "linkedin", "needs": "LINKEDIN_TOKEN"},
    # The two link-fed calendars (NC-031). Adding them here REVERSES a decision
    # this file used to document as permanent — the block comment above
    # fetch_calendar_ics said a background thread could never phone out on a
    # timer, and it said so on purpose. The decision (NC-031) was a daily
    # fetch, because calendars should sync by themselves: from Apple Calendar,
    # and where the files cannot do that, from links to Google Calendar or
    # similar. The row was already a button on something that
    # keeps itself current everywhere else, which is the defect NC-036 names.
    #
    # What did NOT change is either of the gates that actually protect the user.
    # `needs_fetch` keeps NC_ICS_FETCH (default "0") as the master switch, so an
    # install that has never opted in makes no request on any schedule, and
    # `needs` keeps "there is no URL until you paste one". Only the third gate,
    # "a human clicks each time", is gone.
    "calendar_google": {"switch": "NC_CALENDAR_AUTO",
                        "hours_env": "NC_CALENDAR_EVERY_HOURS", "hours": 24,
                        "needs": "NC_ICS_GOOGLE_URL", "needs_fetch": True},
    "calendar_icloud": {"switch": "NC_CALENDAR_AUTO",
                        "hours_env": "NC_CALENDAR_EVERY_HOURS", "hours": 24,
                        "needs": "NC_ICS_ICLOUD_URL", "needs_fetch": True},
}
API_ATTEMPT_KEY = "autosync_api_attempt_"   # + source key -> ISO timestamp
# First scheduled run has no watermark to work from. Reach back this far rather
# than the importer's 3-year default: automatic means "stay current", and a
# multi-year backfill is a deliberate thing you start from the Data page.
API_FIRST_RUN_DAYS = 30
# Overlap re-fetched each run, so a message that landed mid-run isn't skipped
# forever. Imports are idempotent, so the only cost is re-reading a few days.
API_OVERLAP_DAYS = 2


def _parse_stamp(value: str) -> Optional[datetime]:
    """Parse a timestamp we may have written ourselves (ISO 8601, "T", usually
    with an offset) or read out of archive.db (space-separated, never any
    offset), and always return it as aware UTC.

    Worth the eleven lines: archive._parse_ts only accepts the space form and
    returns a naive value, so feeding it an ISO stamp yields None — which reads
    as "never ran" and would make a scheduled source due on every single pass of
    the loop. Anything naive is treated as UTC, which is what both writers use."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _api_hours(key: str) -> int:
    spec = API_SOURCES[key]
    try:
        hours = int(os.environ.get(spec["hours_env"], spec["hours"]))
    except (TypeError, ValueError):
        hours = spec["hours"]
    return max(1, min(24 * 30, hours))


def _api_since(key: str) -> str:
    """The `--since` date for a scheduled run: how far back this source should
    look. Derived from what is already archived for its channel, minus an
    overlap, so each run only fetches genuinely new mail."""
    spec = API_SOURCES[key]
    watermark = ""
    # Deliberately NOT arc.open_if_present(): this is the one archive open with
    # no os.path.exists guard, and it must stay that way. The autosync tests
    # patch arc.get_archive_db to hand back an in-memory connection while
    # ARCHIVE_DB_PATH points at nothing on disk; an exists check would make this
    # return the API_FIRST_RUN_DAYS fallback instead of the real watermark.
    try:
        arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        try:
            watermark = arc.channel_watermark(arc_conn, spec["channel"])
        finally:
            arc_conn.close()
    except Exception:  # noqa: BLE001 — no archive yet, or unreadable: fall back
        watermark = ""
    if watermark:
        stamp = _parse_stamp(watermark)
        if stamp:
            return (stamp - timedelta(days=API_OVERLAP_DAYS)).strftime("%Y-%m-%d")
    return (datetime.now(timezone.utc)
            - timedelta(days=API_FIRST_RUN_DAYS)).strftime("%Y-%m-%d")


def _api_autosync_pending(conn: Any) -> List[Dict[str, Any]]:
    """API-backed sources whose period has elapsed. Shaped like a detect_sources()
    finding so run_autosync can treat both halves the same way; `path` is empty
    because there is no file, which is also what tells the importers to take
    their API route rather than a file one."""
    now = datetime.now(timezone.utc)
    pending: List[Dict[str, Any]] = []
    for key, spec in API_SOURCES.items():
        if os.environ.get(spec["switch"], "1") == "0":
            continue
        needs = spec.get("needs")
        if needs and not os.environ.get(needs, "").strip():
            continue
        # NC-180. `needs` is one environment key, and Gmail needs two things:
        # the address, which is that key, and a client file from Google, which
        # is not on any screen. Without this the loop spawned a subprocess every
        # 12 hours that could only exit 1 on a missing file, forever, silently —
        # the same "an unconfigured source is not a broken source, it should
        # simply not be attempted" the block comment above API_SOURCES already
        # states, applied to the half of the configuration it could not see.
        if _not_ready(key):
            continue
        # A source that reaches the internet for a FILE (the calendars) stays
        # behind NC_ICS_FETCH as well as its own switch. Two independent things:
        # "may this app download calendars at all", which the user turns on once,
        # and "do it on a clock", which is the source's own switch.
        if spec.get("needs_fetch") and not calendar_fetch_enabled():
            continue
        last = _parse_stamp(db.state_get(conn, API_ATTEMPT_KEY + key, ""))
        if last and (now - last) < timedelta(hours=_api_hours(key)):
            continue
        # NC-177. `background` marks a run nobody started, and the importers
        # that could stop and ask a human something read it: gmail's build_cmd
        # turns it into --no-browser. Set for every API source rather than only
        # for gmail, because the fact it states — "there is no person watching
        # this" — is true of all of them and will be true of the next one.
        form: Dict[str, Any] = {"path": "", "background": True}
        if key == "gmail":
            form["since"] = _api_since(key)
        pending.append({"source": key, "path": "", "api": True, "form": form})
    return pending


def autosync_interval_seconds() -> int:
    """How often the background loop looks, in seconds (NC_AUTOSYNC_MINUTES)."""
    try:
        minutes = int(os.environ.get("NC_AUTOSYNC_MINUTES", AUTOSYNC_MINUTES_DEFAULT))
    except (TypeError, ValueError):
        minutes = AUTOSYNC_MINUTES_DEFAULT
    return max(1, min(1440, minutes)) * 60


def _imsg_paste_fp(path: str) -> str:
    """Fingerprint of the pasted copy: its size and mtime, whole seconds.

    Finder preserves the original's modification date when it copies, so this is
    the moment the snapshot was taken — re-pasting the same file yields the same
    fingerprint (skip), a fresher copy yields a new one (import).

    Size used to be left out, because fold_wal() checkpointed the copy and grew
    it while leaving mtime alone, so size changed without the snapshot changing.
    Nothing writes to the snapshot any more (see snapshot_uri), so size is honest
    signal again — and it catches a re-copy that lands in the same whole second."""
    try:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def _dir_fp(path: str) -> str:
    """Fingerprint of a whole DIRECTORY of copies: how many files, their total
    size, and the newest mtime among them.

    os.stat on the directory is not enough. A directory's own mtime moves when a
    direct child is added or removed and stays still when an existing file is
    rewritten in place — which is most of what happens to an avatar cache, since
    WhatsApp overwrites `<jid>-<id>.jpg` when someone changes their picture.

    Walking is cheap because it is stat-only and inside our own container: no file
    is opened, and no consent dialog is possible. That second property is the
    whole point of NC-047 and is why this is safe to run on the two-minute tick.

    Same contract as _imsg_paste_fp: same content, same string; anything new, a
    new string; "" when the directory is missing, which reads as "nothing to do"
    rather than as a change.
    """
    try:
        count = 0
        total = 0
        newest = 0
        for dirpath, _dirs, names in os.walk(path, followlinks=False):
            for name in names:
                if name.startswith("."):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                count += 1
                total += st.st_size
                newest = max(newest, int(st.st_mtime))
        if not count:
            return ""
        return f"{count}:{total}:{newest}"
    except OSError:
        return ""


def _paste_fp_key(path: str) -> str:
    """Which state key fingerprints this import-folder copy, or "" if the path is
    not one of them.

    Two copies now live in our own container — the pasted-or-helper-written
    chat.db and the helper-written ChatStorage.sqlite — and
    they need SEPARATE fingerprints. Sharing one key would make a fresh WhatsApp
    snapshot mark iMessage as already done, and vice versa, so whichever the
    helper wrote second would silently never import."""
    for auto_path, _switch, fp_key in auto_import_paths():
        if path == auto_path:
            return fp_key
    return ""


def _stamp_pasted_copy(conn: Any, path: str) -> bool:
    """Record that this exact snapshot has been through an import, so the
    background loop leaves it alone until a fresher copy appears. Stamped
    after an *attempt*, success or not — same reasoning as the daily stamp:
    never loop on the same bytes. A retry is one paste (or one helper run) away.

    NC-103: this used to be called `_stamp_imsg_paste`, and the name is why the
    bug happened. The autosync loop's call was guarded by
    `if f["path"] == importers.IMSG_AUTO_DB` — reasonable-looking, given the
    name, and it meant the other four automatic sources were NEVER stamped.
    With no stamp, `_autosync_pending` compares a live fingerprint against ""
    and finds them due on every two-minute pass, forever. A correct pass leaves
    one fingerprint key per automatic source in the state table, not only
    `imsg_paste_fp`.

    It has always handled every path — `_paste_fp_key` returns "" for anything
    without a key, including an API source with no local file, so this returns
    early rather than needing a guard at the call site. Passing it every path is
    the correct use and was always the correct use.

    Returns whether anything was written, so the caller commits only when there
    is something to commit."""
    key = _paste_fp_key(path)
    if not key:
        return False
    fp = _dir_fp(path) if os.path.isdir(path) else _imsg_paste_fp(path)
    db.state_set(conn, key, fp)
    return True


def auto_import_paths() -> List[Tuple[str, str, str]]:
    """Every copy the app imports with no click: (path, off-switch, fingerprint key).

    The third column is here because a copy with no fingerprint key re-imports on
    EVERY two-minute tick, forever, and says nothing: that is NC-043's address
    book, re-imported in a hot loop because the autosync loop gained AB_AUTO_DB
    and _paste_fp_key was never taught about it. Keeping the key beside the path
    means adding a source cannot forget one.

    **The single source of truth for "is this source automatic".** Two places
    need the answer and they used to answer it separately: the autosync loop
    below, which imports these paths, and handle_import_detect, which tells the
    Data page which rows to show a button on. The second list had fallen two
    entries behind the first — it named IMSG_AUTO_DB and AB_AUTO_DB but not
    WA_AUTO_DB or WA_PHOTOS_AUTO_DIR, which NC-047 added to the loop — so the
    helper-written WhatsApp copy was imported automatically every pass AND
    reported under "Needs you" with a "Sync now" beside text reading "kept
    current by the sync helper". That is NC-036's exact example of a defect: a
    button on a row that already refreshes itself. The symptom was
    `auto: false` on a file the helper had just rewritten.

    A FUNCTION, not a module constant, and that is not style. tests/helpers.py
    redirects importers.WA_AUTO_DB and friends into a temp dir *after* app.py
    (and therefore this module) has been imported, so a tuple built at import
    time would freeze the real paths and the suite would read the machine's actual
    message databases. That is the NC-044 failure exactly, and it passes green
    while it happens.
    """
    return [
        (importers.IMSG_AUTO_DB, "NC_IMSG_AUTOIMPORT", IMSG_PASTE_FP_KEY),
        (importers.WA_AUTO_DB, "NC_WA_AUTOIMPORT", WA_PASTE_FP_KEY),
        # Apple Contacts, once the helper copies it (NC-029). Same treatment as
        # the other two and for the same reason: the copy is inside our own
        # container, so polling it can never raise a consent dialog.
        (importers.AB_AUTO_DB, "NC_CONTACTS_AUTOIMPORT", AB_PASTE_FP_KEY),
        # WhatsApp's photo cache, once the helper copies it (NC-047). It shares
        # NC_WA_AUTOIMPORT rather than getting a switch of its own: NC-030 folded
        # the photo row into WhatsApp's because they are one source, and a second
        # switch would be a control for a row that no longer exists. Every pass,
        # not once a day — the copy is ours, so there is no dialog left to ration.
        (importers.WA_PHOTOS_AUTO_DIR, "NC_WA_AUTOIMPORT", WA_PHOTOS_PASTE_FP_KEY),
        # Apple Calendar's store, once the helper copies it (NC-042). Its own
        # switch, unlike the photo cache, because this is a source in its own
        # right with its own row — not a sub-source folded into another one.
        # Every pass rather than once a day: the copy is inside our container, so
        # there is no consent dialog left to ration.
        (importers.CAL_AUTO_DB, "NC_CALENDAR_LOCAL_AUTOIMPORT", CAL_PASTE_FP_KEY),
    ]


def _autosync_pending(conn: Any, probe_protected: bool = True) -> List[Dict[str, Any]]:
    """Findings due for a hands-off import right now — [] when there is nothing
    new, when setup has not happened yet, or when the relevant switch is off.

    `probe_protected` picks how hard we look. True runs the full scan, which
    touches WhatsApp's group container plus Desktop/ and Downloads/ — all
    TCC-protected, so it can raise a macOS consent dialog. False checks only our
    own import folder and can never raise one. The loop below passes True once,
    at boot, and False on every pass afterwards: see _autosync_loop.

    It defaults to True so every existing caller and test keeps the behaviour it
    had; only the repeating loop opts out."""
    # Nothing is imported before the user has been through (or skipped) the
    # first-run wizard. This is a consent gate, not an optimisation.
    #
    # WhatsApp is why it has to exist. Its finding comes from the live app
    # container (importers.WA_CONTAINER), NOT from a copy the user made — so on
    # a first launch this function used to hand run_autosync() the user's entire
    # WhatsApp history before any screen had told them the app would read it.
    # That breaks the wizard's first panel, which exists to say what the app will
    # read before it reads anything, and the soft-ask rule in
    # static/app/onboarding.js: the user must never meet a macOS consent dialog
    # they were not warned about, and reading another app's container can raise
    # one on macOS 15 and later. (This used to quote the panel's "files you copy or
    # export yourself" as its whole promise; since NC-123/NC-190 the panel names
    # the sync helper first and that as the route that needs no permission.)
    #
    # It also broke the wizard outright. See api_instance.onboarding_done for
    # the loop: the unasked-for import pushed the contact count past the
    # grandfather threshold, so the install was flagged "already set up" and the
    # wizard never showed at all.
    #
    # NC_AUTOSYNC stays the user's own off switch and is unchanged. This gate is
    # about *when* sync may first run, not whether the user wants it.
    if not api_instance.onboarding_done(conn):
        return []
    if os.environ.get("NC_AUTOSYNC", "1") == "0":
        return []
    findings = (importers.detect_sources() if probe_protected
                else importers.detect_unprotected_sources(count=False))
    pending: List[Dict[str, Any]] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # The live WhatsApp container, at most once a day, and ONLY on a pass that is
    # allowed to touch protected paths — which is the boot pass. This is the route
    # for someone who has not installed the FDA helper; it costs a consent dialog,
    # so it is deliberately rare and never on a timer.
    #
    # "Deliberately rare" was still once per app launch, which is a defect: a
    # permission question should be answered once, not once per startup of the
    # program. So when the helper has already
    # copied the container there is nothing here worth a dialog, and the branch is
    # skipped outright — helper_covers_whatsapp(). detect_sources() no longer
    # returns container paths in that state either, so this is belt and braces:
    # the guard is here as well because this is the function that decides to RUN
    # an import, and a future finding source must not be able to smuggle one in.
    if (db.state_get(conn, AUTOSYNC_STATE_KEY, "") != today
            and not importers.helper_covers_whatsapp()):
        pending += [f for f in findings
                    if f["path"].startswith(importers.WA_CONTAINER)]
    # The copies in our own container, on EVERY pass, gated on the snapshot
    # actually having changed. No consent dialog is possible here, so there is no
    # reason to ration it — and rationing it is exactly what once broke WhatsApp:
    # the only WhatsApp branch was the one
    # above, matching WA_CONTAINER paths that the timer's own
    # detect_unprotected_sources() can never return. So WhatsApp updated once per
    # app launch and then went stale in silence.
    for path, switch, _fp_key in auto_import_paths():
        if os.environ.get(switch, "1") == "0":
            continue
        fp = _dir_fp if os.path.isdir(path) else _imsg_paste_fp
        done = db.state_get(conn, _paste_fp_key(path), "")
        pending += [f for f in findings
                    if f["path"] == path
                    and fp(f["path"]) not in ("", done)]
    pending += _api_autosync_pending(conn)
    return pending


def run_autosync(probe_protected: bool = True) -> int:
    """Run whatever is due right now; returns how many sources ran OK.

    `probe_protected` is passed straight through to _autosync_pending — see
    there for why the repeating loop turns it off."""
    conn = db.get_db()
    try:
        # NC-068. Ask LinkedIn how long the token has left, at most twice a day,
        # and write the answer down. Deliberately BEFORE the `not pending` return:
        # a token that expired is exactly the case where nothing is pending —
        # every LinkedIn attempt now fails — so putting the check behind that
        # early return would switch off the monitoring precisely when it matters.
        #
        # Wrapped because this is a network call inside the loop that keeps the
        # rest of the app current. A LinkedIn outage must not stop iMessage
        # importing; introspect() already swallows its own errors, and this is the
        # belt to that braces.
        try:
            if linkedin_token.is_due():
                linkedin_token.check_and_record()
        except Exception:  # noqa: BLE001 — monitoring must never break syncing
            pass
        pending = _autosync_pending(conn, probe_protected)
        if not pending:
            return 0
        ran = 0
        imsg = 0
        api_ran: List[str] = []
        for f in pending:
            try:
                # Stamp a pasted copy before importing it, not after, so a
                # failure can't spin on the same snapshot every pass. The copy
                # itself needs no preparation — see snapshot_uri().
                # Every automatic copy, not just iMessage — see NC-103 in
                # _stamp_pasted_copy. A path with no fingerprint key (an API
                # source, a link-fed calendar) returns early inside.
                if _stamp_pasted_copy(conn, f["path"]):
                    conn.commit()
                # Stamp an API attempt before running it, not after: a source whose
                # token has expired must wait out its period like any other, or it
                # would be retried on every pass of the loop.
                if f.get("api"):
                    db.state_set(conn, API_ATTEMPT_KEY + f["source"],
                                 datetime.now(timezone.utc).isoformat(timespec="seconds"))
                    conn.commit()
                form = f.get("form") or {"path": f["path"]}
                # A link-fed calendar has no local file until we download one,
                # and build_cmd refuses without a path rather than importing a
                # stale snapshot. The download happens here, in the loop, for the
                # same reason it happens in handle_import_run and not inside the
                # importer: ics_import.py never sees the secret URL and makes no
                # network call of its own.
                if f["source"] in importers.CALENDAR_URL_SOURCES:
                    # Same reason as the release below: a previous iteration's
                    # stamp must not hold the writer lock across an HTTPS GET.
                    db.release_write_lock(conn)
                    form = {**form, "path": fetch_calendar_ics(f["source"])["path"]}
                cmd = importers.REGISTRY[f["source"]].build_cmd(form)
                # NC-082. The previous iteration's `import_synced_` stamp (below)
                # is still uncommitted at this point, and that stamp holds crm.db's
                # single writer lock. Without this line, source B's importer runs
                # for up to import_timeout_sec() (300s by default) while source A's
                # bookkeeping holds the lock the child needs to write its own rows —
                # so the child waits WRITE_LOCK_TIMEOUT_SEC (20s) and fails, and so
                # does any Ask question or edit that lands in the window.
                #
                # With two due WhatsApp folders and a child that tries to write,
                # subprocess #1 gets the lock free and #2 is blocked. The
                # single-source case is safe only because the facts-chain commit
                # lands right after the stamp — which is luck, not design, and
                # stops being true the moment there are two.
                db.release_write_lock(conn)
                # NC-177, the backstop to --no-browser. The flag is the explicit
                # gate and is what gmail actually reads; this is the one that
                # holds for an importer added later, or a build_cmd that forgets
                # to pass it. A copy is modified, never os.environ itself — the
                # server must not acquire this for its own interactive runs.
                #
                # Deliberately NOT the existing NC_NO_BROWSER, which means "the
                # native window is showing the UI, don't also open a browser
                # tab" and which the packaged app sets on the engine it starts
                # (native/Sources/Engine.swift). Every child inherits the server's environment, so
                # borrowing that name would have made the shipped .app refuse to
                # sign in when somebody pressed the button.
                child_env = os.environ.copy()
                child_env["NC_UNATTENDED"] = "1"
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        cwd=APP_DIR, env=child_env,
                                        timeout=import_timeout_sec())
                if result.returncode == importers.EXIT_NEEDS_SIGN_IN:
                    # NC-177. The one non-zero exit that is news. Everything else
                    # a child can fail with is discarded here on purpose — it
                    # waits out its period and tries again — but this one cannot
                    # cure itself by waiting, because the cure is a person. So it
                    # is written down where the Data page reads it.
                    named = importers.parse_sign_in_accounts(
                        (result.stdout or "") + (result.stderr or ""))
                    _set_needs_sign_in(conn, f["source"],
                                       _sign_in_sentence(f["source"], named),
                                       named)
                    conn.commit()
                    continue
                if result.returncode == 0:
                    # Whatever was waiting on a person is not waiting any more:
                    # a run that exited 0 authenticated, which is the only cure.
                    if _needs_sign_in(conn, f["source"]):
                        _set_needs_sign_in(conn, f["source"], "")
                    ran += 1
                    imsg += 1 if f["path"] == importers.IMSG_AUTO_DB else 0
                    if f.get("api"):
                        api_ran.append(importers.REGISTRY[f["source"]].label)
                    db.state_set(conn, f"import_synced_{f['source']}",
                                 datetime.now(timezone.utc).isoformat(timespec="seconds"))
            except (subprocess.TimeoutExpired, OSError, ApiError,
                    importers.ImporterError):
                # ApiError/ImporterError are new here, and both come from the
                # calendar fetch: a revoked link, an expired publish, a timeout.
                # Already stamped above, so it waits out its period like any
                # other attempt instead of retrying every pass — and it must not
                # take the sources behind it in the list down with it.
                continue
        if ran:
            # engagement.refresh() reads every row of archive.db before it writes
            # anything (a fraction of a second even over a large archive), and the
            # last iteration's stamp is still uncommitted here. Small next to the
            # 300s above, but it is the same mistake and costs one no-op call.
            db.release_write_lock(conn)
            engagement.refresh(conn)
            # The same follow-up work a clicked import gets. Rate-limited here
            # because this loop can fire every couple of minutes; a click is not.
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if _facts_chain_is_due(conn):
                db.state_set(conn, FACTS_CHAIN_STATE_KEY, now_iso)
                conn.commit()
                _run_chained_facts_pass()
            # Stamp BEFORE running, both times: the pass can take up to
            # ENRICH_AFTER_IMPORT_SEC, and the loop ticks every couple of
            # minutes. Stamping after would let a slow pass be started again.
            if _style_chain_is_due(conn):
                db.state_set(conn, STYLE_CHAIN_STATE_KEY, now_iso)
                conn.commit()
                _run_chained_style_pass()
        # stamp the day even on partial failure — retry tomorrow, don't loop today
        db.state_set(conn, AUTOSYNC_STATE_KEY,
                     datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        conn.commit()
        if ran:
            wa = ran - imsg - len(api_ran)
            what = " and ".join(
                ([f"WhatsApp ({wa} source{'s' if wa != 1 else ''})"] if wa else [])
                + (["your pasted iMessage copy"] if imsg else [])
                + api_ran)
            print(f"Auto-synced {what} — messages are up to date.", flush=True)
        return ran
    finally:
        conn.close()


def _autosync_loop() -> None:
    """Body of the daemon thread main() starts; `time` comes from the module.

    The full scan runs ONCE, on the first pass after launch, and never again for
    the life of the process. Every pass after that looks only where macOS does
    not guard.

    Running the full scan every time would be wrong. That scan stats WhatsApp's
    group container and globs Desktop/ and Downloads/, so macOS would raise a
    consent dialog every NC_AUTOSYNC_MINUTES — attributed to "python3", since the
    app is a plain interpreter and not a signed bundle, so it reads as something
    foreign asking for the user's Messages. Two instances running at once (a dev
    server alongside the app) would halve the gap again.

    Boot is the honest moment to ask: the user just opened the app, so a prompt
    has an obvious cause. A prompt 40 minutes later, while they are reading a
    contact, does not.

    The loop discarded almost everything the scan returned anyway — it only ever
    matches WA_CONTAINER and IMSG_AUTO_DB, so the Desktop/Downloads globs were
    pure consent-dialog surface for findings nothing here could act on. Those are
    still offered on the Data page, where the user asked and a prompt makes sense.
    """
    first = True
    while True:
        try:
            run_autosync(probe_protected=first)
        except Exception:  # noqa: BLE001 — background sync must never die loudly
            pass
        first = False
        time.sleep(autosync_interval_seconds())
