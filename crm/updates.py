"""
Update checker: asks
GitHub Releases whether a newer Network CRM is available so the packaged app
can nudge the user toward a fresh DMG. Pure stdlib (urllib) — the same
no-third-party-dependency rule the rest of crm/ follows.

Privacy: nothing about the user is sent. The check is a single unauthenticated
GET to the public releases API — no query string, no body, no identifiers. It
is also failure-safe: offline, rate-limited, a repo with no releases yet, or
malformed JSON all degrade to "couldn't check", never an exception the UI has
to handle. The last good answer is cached in `state` so we hit GitHub at most
once a day.

There is no default repo, and that is deliberate. A hardcoded owner/name would
404 for any fork or build with no releases behind it: the check would be fully
wired and permanently inert, and it would fire a real outbound request every
24 hours to learn nothing. An app whose pitch is that it works offline should not
be talking to github.com to no purpose. So `NC_UPDATE_REPO` starts empty, `check()`
returns `configured: False` without opening a socket, and the app's version row
says "This copy doesn't check for updates" instead of implying the check works.
Set the variable at release time and the whole path — cache, banner, "Check now" —
lights up with no code change.

It is deliberately NOT an in-app editable field: a release should ship already
knowing its own repo, not ask each recipient to type one in.

NC-315. The copy built for handing out does ship knowing it: `appify.sh
--handout` writes PUBLIC_REPO below into the app, and the app hands it over
as NC_UPDATE_REPO. Running from source, or any other build, still asks nothing.
A repository with no release yet (or not public yet) answers 404; that is a
calm "nothing published yet", remembered for the day like any other answer, not
a failure to retry on every call.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

import db

# Canonical app version — the single source of truth the server reports. Keep
# in lockstep with CFBundleShortVersionString in packaging/appify.sh on release.
CURRENT_VERSION = "1.0.0"

# "owner/name" of the repo whose GitHub Releases we read. EMPTY BY DEFAULT — see
# the module docstring. Set it at build/release time (or in the environment of a
# fork) to switch update checking on; leave it unset and no request is ever made.
GITHUB_REPO = os.environ.get("NC_UPDATE_REPO", "").strip()
RELEASES_URL = "https://api.github.com/repos/{repo}/releases/latest"

# The public repository a handed-out copy reads (NC-315). Not a default: nothing
# here uses it unless a build sets NC_UPDATE_REPO to it. packaging/appify.sh
# reads this line, so it stays one plain string.
PUBLIC_REPO = "william-agehouse/network-crm-native"

# Don't hammer GitHub: at most one live fetch per this window; between fetches
# the cached answer is served.
CHECK_INTERVAL_SECONDS = 24 * 3600

_STATE_LAST_CHECK = "update_last_check"    # ISO timestamp of last successful fetch
_STATE_LATEST = "update_latest_version"    # newest tag we've seen (no v-prefix)
_STATE_URL = "update_latest_url"           # release page to send the user to


def parse_version(s: str) -> Tuple[int, ...]:
    """'v1.2.3' / '1.2.3' / '1.2' -> (1, 2, 3) / (1, 2). Any pre-release or
    build suffix ('-beta1', '+build') is dropped — only the numeric core is
    compared. Unparseable input yields (), which ranks below every real
    version."""
    s = (s or "").strip().lstrip("vV")
    core = re.match(r"[0-9]+(?:\.[0-9]+)*", s)
    if not core:
        return ()
    return tuple(int(p) for p in core.group(0).split("."))


def is_newer(latest: str, current: str) -> bool:
    """True iff `latest` is a strictly higher version than `current`. Lengths
    are zero-padded so 1.0 and 1.0.0 compare equal; a missing or unparseable
    `latest` is never treated as an update."""
    lt, cur = parse_version(latest), parse_version(current)
    if not lt:
        return False
    n = max(len(lt), len(cur))
    lt += (0,) * (n - len(lt))
    cur += (0,) * (n - len(cur))
    return lt > cur


def releases_page(repo: str) -> str:
    """Where a person goes to download a new version."""
    return f"https://github.com/{repo}/releases"


def _fetch_latest(repo: str, timeout: float = 6.0) -> Optional[Dict[str, str]]:
    """Live GET of the latest release. Returns {'version', 'url'}; a version of
    "" when GitHub answers 404, which is what a repository with no release yet
    (or one that is not public yet) says; or None on any other failure (offline,
    rate-limit, bad JSON). Never raises — the caller reads None as 'couldn't
    check'."""
    req = urllib.request.Request(
        RELEASES_URL.format(repo=repo),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "NetworkCRM-update-check",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"version": "", "url": releases_page(repo)}
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    return {
        "version": tag.lstrip("vV"),
        "url": str(data.get("html_url") or f"https://github.com/{repo}/releases/latest"),
    }


def _is_due(last_iso: str, now: datetime) -> bool:
    """Has the cache aged past CHECK_INTERVAL_SECONDS? An empty or malformed
    timestamp is always due."""
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= CHECK_INTERVAL_SECONDS


def channel_says(repo: Optional[str] = None) -> str:
    """One plain sentence on where this copy's new versions come from, for a
    screen that must not go online to draw itself (NC-315)."""
    repo = GITHUB_REPO if repo is None else repo
    if not repo:
        return "This copy does not check for updates."
    return (f"New versions are published at {releases_page(repo)}. "
            "To update, replace the app; your data stays.")


def _says(configured: bool, latest: str, url: str, page: str, checked: bool) -> str:
    """The answer in words, so whatever shows it never has to work out what a
    missing version means. `checked` is whether GitHub has ever answered."""
    if not configured:
        return "This copy does not check for updates."
    if latest and is_newer(latest, CURRENT_VERSION):
        return (f"Version {latest} is out: {url}. "
                "To update, replace the app; your data stays.")
    if latest:
        return f"This is the newest version. New versions appear at {page}."
    if checked:
        # GitHub answered, and there is no release: nothing is wrong.
        return f"No new version has been published yet. When one is, it appears at {page}."
    return (f"Could not reach GitHub to check just now; it tries again later. "
            f"New versions appear at {page}.")


def check(
    conn: Any,
    force: bool = False,
    fetcher: Optional[Callable[[], Optional[Dict[str, str]]]] = None,
    now: Optional[datetime] = None,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    """Current update status, served from cache unless a live check is due (or
    forced). Shape:

        {current, latest, update_available, url, checked_at, stale, configured,
         says}

    `says` is the answer as one plain sentence (NC-315), including the calm
    "nothing published yet" a repository with no release gives.

    `configured` is False when this build has nowhere to ask — no `NC_UPDATE_REPO`
    and no injected `fetcher`. That case returns immediately: no socket is opened,
    no `state` row is written, and the UI is told to say so rather than showing a
    check that silently never succeeds. Passing a `fetcher` counts as configured
    because the fetcher *is* the source — it says where to ask, so a test doesn't
    also have to name a repo.

    `stale` is True when the answer came from cache without a fresh fetch (or a
    fetch was attempted but failed). `fetcher`/`now`/`repo` are injectable for
    tests; they default to the real GitHub call, the real clock and GITHUB_REPO."""
    repo = GITHUB_REPO if repo is None else repo
    configured = bool(repo) or fetcher is not None
    if not configured:
        return {
            "current": CURRENT_VERSION,
            "latest": None,
            "update_available": False,
            "url": "",
            "checked_at": None,
            "stale": False,
            "configured": False,
            "says": _says(False, "", "", "", False),
        }

    fetcher = fetcher or (lambda: _fetch_latest(repo))
    now = now or datetime.now(timezone.utc)

    last_check = db.state_get(conn, _STATE_LAST_CHECK, "") or ""
    latest = db.state_get(conn, _STATE_LATEST, "") or ""
    url = db.state_get(conn, _STATE_URL, "") or (
        f"https://github.com/{repo}/releases/latest" if repo else "")
    page = releases_page(repo) if repo else url
    stale = True

    if force or _is_due(last_check, now):
        got = fetcher()
        if got:
            latest = got["version"]
            url = got["url"]
            last_check = now.isoformat(timespec="seconds")
            db.state_set(conn, _STATE_LATEST, latest)
            db.state_set(conn, _STATE_URL, url)
            db.state_set(conn, _STATE_LAST_CHECK, last_check)
            conn.commit()
            stale = False
        # got is None -> keep the cached answer and DON'T stamp last_check, so
        # the next call retries instead of going quiet for a whole day.

    return {
        "current": CURRENT_VERSION,
        "latest": latest or None,
        "update_available": bool(latest) and is_newer(latest, CURRENT_VERSION),
        "url": url,
        "checked_at": last_check or None,
        "stale": stale,
        "configured": True,
        "says": _says(True, latest, url, page, checked=bool(last_check)),
    }
