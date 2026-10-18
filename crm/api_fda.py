"""The Full Disk Access flow, as an API — so the app can explain it in its own
window instead of in a Terminal nobody opens.

WHY THIS MODULE EXISTS
Everything needed to grant Full Disk Access to the sync helper exists as
terminal tooling: the compiled helper, the installer that registers its
LaunchAgent, the exit-code contract that tells the installer to print the steps,
and the System Settings pane it opens. All of it prints to a terminal. A person
who uses this app only ever double-clicks `Network CRM.app` — so from where they
sit, nothing would tell them how to grant the permission, or whether it worked.
A working feature with no surface inside the product is invisible, and so are
the bugs in it.

WHAT MACOS ALLOWS, WHICH SHAPES ALL OF THIS
There is **no API to request Full Disk Access**. Nothing prompts, nothing can be
triggered, and there is no way to ask "do I have it?" other than trying a read
and seeing whether it fails. So this cannot be a permission button. The most an
application can honestly do is:

  1. install the helper for you,
  2. put you in front of the right System Settings pane with the right binary
     already selected in Finder, so the remaining step is one drag,
  3. tell you, afterwards and truthfully, whether it worked.

That is exactly what the three endpoints here do, and (3) is the one that makes
the flow trustworthy: without it, a grant that failed looks the same as one that
worked.

WHERE THE TRUTH COMES FROM
`importers.imsg_helper_report()` — the JSON the helper itself writes after every
run, inside the CRM's own Application Support folder. The app never looks at
~/Library/Messages or WhatsApp's container: not as a matter of taste, but because
being unable to look at them is the entire premise of the helper existing
(rule 6 in docs/EDITING.md: the grant goes to the helper, never to the app — the
one part of it that did not change when NC-123 made the helper the lead
route). Everything below is derived from that report plus four file existence
checks.

The recheck runs the helper **through launchd**, not as a child of this process.
Two reasons, and the second is the one that matters: it is the same path the
scheduled refresh takes, so a green result here means the real thing works; and
TCC attributes a permission check to the process that performs the read, which
for a subprocess of a subprocess is a question with a subtle answer nobody should
have to be right about. launchd launching it is exactly what the grant was given
for.
"""

import hashlib
import os
import subprocess
import time
from http import HTTPStatus
from typing import Any, Dict, List, Optional

import importers
from api_core import ApiError

ROUTES = {
    "GET": {"/api/fda/status": "handle_fda_status"},
    "POST": {
        "/api/fda/install": "handle_fda_install",
        "/api/fda/uninstall": "handle_fda_uninstall",
        "/api/fda/recheck": "handle_fda_recheck",
    },
}

# ── Tunables ─────────────────────────────────────────────────────────────────
# Every one of these is a number that would otherwise be buried in the code.
#
# INSTALL_TIMEOUT is generous because the two install paths are wildly different:
# inside the app bundle the helper is already compiled and the script is a copy
# plus a launchctl call, while a repo checkout compiles Swift first, which on a
# cold Xcode toolchain is tens of seconds. NC-206: the bundle path does not
# take under a second either, because the installer then runs the helper once
# through launchd and waits up to 60 s for its report, so a full install can
# take on the order of 40 s when several sources are blocked.
#
# RECHECK_TIMEOUT covers launchd noticing the kickstart and the helper copying
# both databases. WhatsApp's ChatStorage.sqlite can be large, and an unchanged
# source is skipped entirely, so the common case is under two seconds.
#
# STALE_HOURS is "the helper has gone quiet". It is deliberately NOT derived from
# the install interval, which is now 120 seconds — 24 hours is 720 missed runs, so
# this fires only on something genuinely broken (agent unloaded, binary deleted)
# and never on a laptop that spent the night closed. launchd runs the agent on
# wake, so a real report is minutes away in that case, and a threshold tight
# enough to catch "one run was skipped" would spend most of its time crying wolf.
INSTALL_TIMEOUT = int(os.environ.get("NC_FDA_INSTALL_TIMEOUT", "300"))
RECHECK_TIMEOUT = int(os.environ.get("NC_FDA_RECHECK_TIMEOUT", "45"))
# How long `launchctl kickstart` may take to start the helper, before the wait
# above begins. Plumbing, not a setting: it only ever takes a moment.
KICKSTART_TIMEOUT = 20
STALE_HOURS = int(os.environ.get("NC_FDA_STALE_HOURS", "24"))

# NC-223. What the helper this app carries does differently from an older one, in
# one sentence, for the "set it up again" line. Change it whenever
# packaging/src/imessage_sync.swift changes in a way a person would want to know
# about before they give the permission again.
HELPER_CHANGES = ("It writes less to your disk: it no longer rewrites its own "
                  "bookkeeping file every two minutes when nothing changed, and "
                  "its log stays under 1 MB.")

# path -> ((size, mtime), sha256). The status is read on every visit to the Data
# page; the two programs are hashed again only when one of them changed.
_HASHES: Dict[str, Any] = {}


def _sha256(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (st.st_size, st.st_mtime)
    hit = _HASHES.get(path)
    if hit and hit[0] == key:
        return hit[1]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 16), b""):
                h.update(block)
    except OSError:
        return ""
    _HASHES[path] = (key, h.hexdigest())
    return _HASHES[path][1]


def _newer_in_app(installer: str, installed: bool) -> bool:
    """True when the helper running on this Mac is not the one this app carries.

    NC-223. `outdated` used to mean only "a source is missing from its report",
    so a helper that copied every source but was an older program was never
    offered the newer one, and the person was never told to set it up again.
    The installer copies the app's prebuilt helper byte for byte (re-signing it
    would change the code hash the permission is tied to), so different bytes
    mean a different program. A source checkout carries no prebuilt helper next
    to its installer, so there is nothing to compare and this stays False."""
    if not (installer and installed):
        return False
    bundled = os.path.join(os.path.dirname(installer), "networkcrm-imessage-sync")
    mine, theirs = _sha256(bundled), _sha256(importers.IMSG_HELPER_BIN)
    return bool(mine and theirs and mine != theirs)
# How often the poll below looks at the report file while waiting for a new one.
RECHECK_POLL_SECONDS = float(os.environ.get("NC_FDA_RECHECK_POLL", "0.4"))
# Output kept from the installer, in lines. It prints a numbered set of
# instructions on the blocked path, and cutting that in half would be worse than
# showing nothing.
OUTPUT_LINES = int(os.environ.get("NC_FDA_OUTPUT_LINES", "80"))

# Which outcomes in the helper's report mean "this source is fine".
_GOOD = ("copied", "current")


# Helper report label -> the api_setup step key it feeds. The labels are
# importers.HELPER_* constants rather than literals so a rename cannot silently
# stop this mapping from matching anything, which would read as "nothing is
# blocked" — the failure mode NC-076 is about.
_STEP_FOR_LABEL = {
    importers.HELPER_IMESSAGE: "imessage",
    importers.HELPER_WHATSAPP: "whatsapp",
    importers.HELPER_CONTACTS: "contacts",
    importers.HELPER_CALENDAR: "calendar",
}


def collection_blocked() -> Dict[str, str]:
    """{step_key: reason} for sources macOS is currently refusing the helper.

    NC-076, and the same contract as api_linkedin.collection_blocked(): the
    module that owns the decision exports it, so api_setup can say "connected
    but not collecting" without reimplementing anything.

    ONLY `outcome == "blocked"` counts. A helper that is not installed is NOT a
    blocked source: invariant 6 means the supported path is the user copying the
    file themselves, so plenty of installs import iMessage with no helper at all.
    Reporting those as blocked would be this same bug pointing the other way —
    a signal that proves "no helper" rendered as "not collecting".

    Never raises: a missing or hand-edited report reads as "nothing known to be
    blocked", because inventing a blocker is worse than missing one here.
    """
    try:
        sources = _sources(importers.imsg_helper_report())
    except Exception:  # noqa: BLE001 — no report is a normal first-run state
        return {}
    out: Dict[str, str] = {}
    for row in sources:
        if row.get("outcome") != "blocked":
            continue
        key = _STEP_FOR_LABEL.get(row.get("label") or "")
        if key:
            out[key] = "macOS is refusing the sync helper, so nothing new arrives"
    return out


def _tail(text: str, lines: int = OUTPUT_LINES) -> str:
    """The last `lines` lines, so a wall of installer output stays a paragraph."""
    parts = (text or "").rstrip().splitlines()
    return "\n".join(parts[-lines:])


def _sources(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The report's per-source rows, defended against a hand-edited file.

    Only the keys the UI renders are passed through, and each is coerced, because
    this is the one place where a file on disk becomes a JSON response.
    """
    out: List[Dict[str, Any]] = []
    for row in report.get("sources") or []:
        if not isinstance(row, dict):
            continue
        entry: Dict[str, Any] = {
            "label": str(row.get("label") or "?"),
            "outcome": str(row.get("outcome") or "unknown"),
        }
        if row.get("hint"):
            entry["hint"] = str(row["hint"])
        if isinstance(row.get("bytes"), (int, float)):
            entry["bytes"] = int(row["bytes"])
        # NC-093. `matched` and `files` are passed through because they are the
        # whole reason the helper counts anything.
        #
        # `matched` is the important one: the helper writes it for EVERY source,
        # copied or not, and its own comment says it is "the count the app needs
        # to notice a source that is technically fine and substantively empty".
        # The case it exists for is a Mac whose flat address-book store exists
        # but holds almost nobody, with the real contacts living in an iCloud
        # store: a helper that reports `current` for that store shows a
        # healthy Apple Contacts row while the import brings in almost nobody,
        # with no error, no warning and no count anywhere to notice it by.
        #
        # `files` only appears on a run that actually copied, so it cannot cover
        # the `current` case — which is exactly why `matched` has to come
        # through too, and why passing only one of them would leave the hole
        # open.
        for key in ("matched", "files"):
            if isinstance(row.get(key), (int, float)):
                entry[key] = int(row[key])
        out.append(entry)
    return out


def _status() -> Dict[str, Any]:
    """Everything the panel needs, in one object. No subprocesses, no protected
    reads — four `os.path.exists` calls and one small JSON file."""
    installer = importers.imsg_helper_installer()
    installed = importers.imsg_helper_installed()
    report = importers.imsg_helper_report()
    sources = _sources(report)

    ran_at = report.get("ran_at")
    ran_ago = None
    if isinstance(ran_at, (int, float)):
        # max(0, …) because a clock change can put a real report in the future,
        # and "ran -3 minutes ago" reads as a bug in the app rather than in time.
        ran_ago = max(0.0, time.time() - float(ran_at))
    else:
        ran_at = None

    # Which sources this build's helper knows how to copy, versus which the
    # INSTALLED binary actually reported. They diverge every time a source is
    # added: the LaunchAgent keeps running yesterday's binary, which copies two
    # of the three and says nothing about the third. Adding Contacts (NC-029) is
    # exactly that case, and without this the symptom is "Apple Contacts never
    # became automatic and nothing anywhere said why".
    reported = {s["label"] for s in sources}
    missing = [lbl for lbl in importers.HELPER_SOURCE_LABELS if lbl not in reported]
    newer = _newer_in_app(installer, installed)

    blocked = [s["label"] for s in sources if s["outcome"] == "blocked"]
    ok = [s["label"] for s in sources if s["outcome"] in _GOOD]
    absent = [s["label"] for s in sources if s["outcome"] == "absent"]
    quiet = bool(installed and sources
                 and (ran_ago is None or ran_ago > STALE_HOURS * 3600))

    # ONE word for the UI to switch on, decided here rather than in JS: the
    # ordering below IS the product decision about what to say first, and it
    # belongs next to the data it reads. Blocked outranks quiet on purpose — if
    # the last thing we know is "macOS refused", that is the actionable fact
    # whatever its age.
    if not installer:
        state = "unavailable"      # this build ships no helper (no swiftc at build time)
    elif not installed:
        state = "not_installed"
    elif not sources:
        state = "no_report"        # installed but has never reported (or too old to)
    elif blocked:
        state = "blocked"
    elif missing or newer:
        # After blocked, because a refused permission is the more actionable
        # fact; before absent/quiet/ok, because none of those three are true of a
        # helper that is not copying everything it should be, or is an older
        # program than the one this app carries (NC-223).
        state = "outdated"
    elif not ok and absent:
        state = "absent"           # nothing to copy: no Messages, no WhatsApp Desktop
    elif quiet:
        state = "quiet"
    else:
        state = "ok"

    return {
        "state": state,
        "available": bool(installer),
        "installer": installer,
        "installed": installed,
        "binary": importers.IMSG_HELPER_BIN,
        "binary_dir": os.path.dirname(importers.IMSG_HELPER_BIN),
        "binary_exists": os.path.exists(importers.IMSG_HELPER_BIN),
        "log": importers.IMSG_HELPER_LOG,
        "sources": sources,
        "blocked": blocked,
        "ok": ok,
        # Labels the installed helper never mentioned — what a reinstall would add.
        "missing": missing,
        # NC-223. The installed helper is an older program than the one this app
        # carries, and what setting it up again would change.
        "newer_in_app": newer,
        "newer_says": HELPER_CHANGES if newer else "",
        # Every source this build's helper copies, for the offer shown before it
        # is installed (NC-194). That offer named "Messages and WhatsApp" by hand
        # and said "those two databases" long after Contacts and Calendar joined,
        # so it under-sold the grant on the screen where someone decides to give it.
        "copies": list(importers.HELPER_SOURCE_LABELS),
        "ran_at": ran_at,
        "ran_ago_seconds": ran_ago,
        "stale_hours": STALE_HOURS,
        # Keys into importers.OPEN_TARGETS. The client posts a key to
        # /api/import/open and never a path — same contract as every other
        # "Open …" button in the app.
        "targets": {"settings": "fda_settings", "binary": "fda_binary"},
        # NC-206. How long the server lets each button's work run, in seconds, so
        # the page can wait longer than that instead of giving up at its usual
        # 20 s (API_TIMEOUT_MS) while the script carries on and succeeds. Sent
        # rather than copied into the page, the NC-176 rule: the ceiling is a
        # setting, and it has one home.
        "waits": {"install": INSTALL_TIMEOUT, "uninstall": INSTALL_TIMEOUT,
                  "recheck": KICKSTART_TIMEOUT + RECHECK_TIMEOUT},
    }


def handle_fda_status() -> Dict[str, Any]:
    """GET /api/fda/status — what the helper's last run reported, and what the
    user can do about it. Cheap enough to call on every visit to Data.

    Takes no arguments at all, which the route registry reads off this signature:
    there is nothing to filter, and a query parameter here would only ever be a
    way to ask a question the answer cannot vary on."""
    return _status()


def _report_mtime() -> Optional[float]:
    try:
        return os.path.getmtime(importers.IMSG_HELPER_REPORT)
    except OSError:
        return None


def handle_fda_install(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/fda/install — run the bundled installer.

    Safe to call repeatedly: the installer will not recompile a helper whose
    input is unchanged, precisely because replacing the binary changes its code
    hash and macOS then silently drops the Full Disk Access grant. So a second
    press is not a re-install, and does not cost the user their permission.

    The script path comes from importers.imsg_helper_installer(), never from the
    request. The only thing the client may influence is the refresh interval.
    """
    installer = importers.imsg_helper_installer()
    if not installer:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "this copy of the app does not carry the sync helper")

    env = dict(os.environ)
    hours = (body or {}).get("interval_hours")
    if hours is not None:
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "interval_hours must be a number")
        if not 0.25 <= hours <= 168:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           "interval_hours must be between 0.25 and 168")
        env["NC_IMESSAGE_SYNC_INTERVAL"] = str(int(hours * 3600))

    try:
        proc = subprocess.run(["/bin/bash", installer], capture_output=True,
                              text=True, timeout=INSTALL_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise ApiError(HTTPStatus.GATEWAY_TIMEOUT,
                       f"Setting up automatic copying did not finish within "
                       f"{INSTALL_TIMEOUT} seconds. Try again in a moment.")
    except OSError as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                       f"could not run the installer: {exc}")

    # Both streams, in the installer's own order as far as buffering allows: the
    # numbered Full Disk Access steps go to stdout while the helper's "still
    # needs Full Disk Access" line goes to stderr, and either one alone is half
    # an explanation.
    return {
        # exit 0 means "the agent is installed", NOT "the permission is granted".
        # The installer succeeds and prints the steps in the same breath, which is
        # exactly the confusion this endpoint exists to stop repeating: read
        # status.state, not this.
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "output": _tail("\n".join(p for p in (proc.stdout, proc.stderr) if p)),
        "status": _status(),
    }


def handle_fda_uninstall() -> Dict[str, Any]:
    """POST /api/fda/uninstall — the switch's other position (NC-187).

    Invariant 6 says the two ways in are one switch, and the Data page could
    only move it one way: it ran the installer, and turning the helper off meant
    knowing that packaging/uninstall_imessage_sync.sh existed and opening a
    Terminal. This runs that script exactly as handle_fda_install runs its twin —
    path from importers, never from the request.

    It removes the LaunchAgent and the binary, and leaves every copy the helper
    made (the user's data). The rows then fall back on their own: with nothing
    installed, imsg_helper_report() is empty, so each reads as "Your copy in the
    import folder". WhatsApp keeps reading the helper's last copy rather than its
    live folder, which would raise a macOS dialog on every page load (see
    importers.helper_covers_whatsapp). The Full Disk Access entry stays in System
    Settings until the user removes it — no script can.
    """
    uninstaller = importers.imsg_helper_uninstaller()
    if not uninstaller:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "this copy of the app does not carry the sync helper")
    try:
        proc = subprocess.run(["/bin/bash", uninstaller], capture_output=True,
                              text=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ApiError(HTTPStatus.GATEWAY_TIMEOUT,
                       f"turning the helper off did not finish within {INSTALL_TIMEOUT}s")
    except OSError as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                       f"could not run the uninstaller: {exc}")
    return {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "output": _tail("\n".join(p for p in (proc.stdout, proc.stderr) if p)),
        "status": _status(),
        "targets": {"settings": "fda_settings"},
    }


def handle_fda_recheck() -> Dict[str, Any]:
    """POST /api/fda/recheck — ask launchd to run the helper now, and wait for
    its report.

    This is the endpoint that answers "did the thing I just did in System
    Settings work?", which is the question the app could not answer before. It
    runs the helper exactly the way the scheduled refresh runs it, so a green answer
    here is a statement about the real refresh and not about a special case.
    """
    if not importers.imsg_helper_installed():
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "the sync helper is not installed yet")

    before = _report_mtime()
    target = f"gui/{os.getuid()}/{importers.IMSG_HELPER_LABEL}"
    try:
        # -k so a run already in progress is restarted rather than making us wait
        # out someone else's copy.
        kick = subprocess.run(["launchctl", "kickstart", "-k", target],
                              capture_output=True, text=True, timeout=KICKSTART_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                       f"could not start the helper: {exc}")
    if kick.returncode != 0:
        raise ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "launchd would not start the helper "
            f"({(kick.stderr or kick.stdout or '').strip() or 'no output'}). "
            "Re-installing it usually fixes this.")

    # launchd returns as soon as it has spawned the helper, so the fresh report
    # only exists once the copy finishes. Wait for the file to CHANGE rather than
    # merely exist, or the previous run's answer would be read back as this one's
    # — which is the same class of mistake as the exit code that said "already
    # granted".
    deadline = time.time() + RECHECK_TIMEOUT
    ran = False
    while time.time() < deadline:
        now = _report_mtime()
        if now is not None and now != before:
            ran = True
            break
        time.sleep(RECHECK_POLL_SECONDS)

    return {
        "ran": ran,
        "waited": round(RECHECK_TIMEOUT - max(0.0, deadline - time.time()), 1),
        "timeout": RECHECK_TIMEOUT,
        "status": _status(),
    }
