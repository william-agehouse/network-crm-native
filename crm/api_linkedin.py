"""LinkedIn's three lanes, as one status object — because one source row cannot
tell the truth about three different machines.

WHY THIS MODULE EXISTS
LinkedIn's data comes from three places: the download, the API and the
scraper. That is a lot going on, so it gets its own row, as the Full Disk Access
helper does.

Every other source on the Data page is one file that one thing copies, and a row
saying "Updated automatically · 600 cards" tells the whole story. LinkedIn is not
that. It is:

  1. THE DOWNLOAD — a `Connections*.csv` out of LinkedIn's data-export archive,
     which you request by hand, wait a day for, and drop in Downloads.
  2. THE API — the official Member Snapshot API (`crm/linkedin_import.py`),
     needing a `LINKEDIN_TOKEN` you have to go and mint, which expires.
  3. THE SCRAPER — Selenium driving Firefox, possibly on a second Mac over SSH
     (`crm/scrape_linkedin_profiles.py`), paced by a LaunchAgent dispatcher
     (`crm/scrape_dispatch.py`), then pulled back here in three separate rsyncs
     (facts, photos, extras).

Those three fail independently, on different machines, for different reasons. The
generic `SourceRow` shows one export file's mtime, which is unrelated to all of
them: it says nothing about how many people have a LinkedIn URL and have never
been scraped.

WHAT THIS ENDPOINT IS ALLOWED TO DO, WHICH SHAPES ALL OF IT
It is called on every visit to Data, so it must be cheap and it must not block.
Three rules, each of which is a real hazard rather than a style preference:

  • NO NETWORK, NO SSH. The only way to test the token is to spend a live API
    call, and the only way to reach a scraper Mac is an `ssh` with a 15-second connect
    timeout against a machine that may be asleep. Both would be on the
    critical path of rendering a page. So this reports `token_present`, never
    `token_valid`, and reports the scraper from what has already landed in the
    database rather than from asking the scraper Mac.
  • NO ~/Downloads OR ~/Desktop GLOB. Since Sequoia that can block on a TCC
    consent dialog, and `except OSError` cannot save you because a blocked
    syscall raises nothing. The Data page already calls `detect_sources()` once;
    the panel reads the export lane off THAT, passed down as a prop. Adding a
    second probe here would be the bug.
  • NO SECRETS IN THE PAYLOAD. `LINKEDIN_TOKEN` is reported as a boolean and, at
    most, a masked tail — never the value.

WHAT IT DELIBERATELY DOES NOT OFFER
There is no "scrape now" here, and there must never be one. The dispatcher's
pacing IS the ban protection (`crm/scrape_dispatch.py:8`), so a button that
bypasses the daily cap, the inter-session gap and the cooldown is a button that
risks the account this whole lane depends on. Same for anything that shortens
`cooldown_until`, and same for `enrich_li_extras.py --force`, which re-bills
every contact through Haiku. The panel may DISPLAY a cooldown; it may not end it.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import archive as arc
import config
import db
import figures  # NC-087: the one spelling of a contact-scope filter
import linkedin_token
import settings

ROUTES = {"GET": {"/api/linkedin/status": "handle_linkedin_status"}}

# ── Tunables ─────────────────────────────────────────────────────────────────
# NC-095. This used to be its own `int(os.environ.get("NC_LI_RESCRAPE_DAYS",
# "180"))` while the scraper had a bare `180`, so setting the variable made the
# panel predict a queue the other Mac would never work. Both now call
# db.li_rescrape_days(). Still not imported from the scraper module: importing
# that pulls in Selenium, which is not installed alongside the app and is not
# supposed to be.
RESCRAPE_DAYS = db.li_rescrape_days()
# How long since the last scraped fact before the lane reads as stopped rather
# than merely idle. The dispatcher does 40–55 profiles/day across several
# sessions, so a day with nothing at all means the LaunchAgent, the Mac or the
# session is down — not that it was a quiet afternoon.
SCRAPER_QUIET_HOURS = int(os.environ.get("NC_LI_SCRAPER_QUIET_HOURS", "48"))
# How long the message lane may be silent before that is worth saying, when we
# have no introspection to ask. This exists so the dead-token case is caught with
# ZERO configuration: without a client id and secret the expiry is unknowable,
# but a token LinkedIn has stopped accepting shows up anyway as messages that
# stopped arriving. Generous on purpose — three weeks, so an ordinary quiet
# fortnight on LinkedIn never trips it, and it only speaks when introspection
# cannot.
API_SILENT_DAYS = int(os.environ.get("NC_LI_API_SILENT_DAYS", "21"))

# The facts_source token the scraper writes (crm/enrich_facts.py), versus the one
# the CSV seed writes. Two lanes, two tokens, and telling them apart is most of
# why this endpoint can say anything useful.
FACTS_SCRAPE = db.LI_FACTS_SOURCE   # NC-095: one literal, two readers
FACTS_CSV = "linkedin_csv"


# ── "Is this lane able to run right now?" ────────────────────────────────────
def collection_blocked() -> str:
    """Why LinkedIn direct messages cannot be collected right now, or "".

    NC-076. `api_setup` needs this to answer "is this source still working",
    and it must NOT reimplement the rule: the token half of `_overall` below is
    the decision, and two places deciding the same thing is how they come to
    disagree. So the owner of the decision exports it and everyone else asks.

    Deliberately cheap — one environment read and one small JSON file, no
    database and no network — because the setup panel renders on every visit to
    the Data page and `_status()`'s archive queries are too much to pay there.

    Note the asymmetry with `linkedin_token.configured()`, which is True only
    when a client id and secret are ALSO present. Those two were taken off the
    Settings screen in NC-073 and are optional; a token alone collects messages
    perfectly well. Using `configured()` here would report every install without
    them as blocked, which is the same class of bug in the opposite direction.
    """
    if not (os.environ.get("LINKEDIN_TOKEN") or "").strip():
        return "no token, so direct messages are not being collected"
    if linkedin_token.summary().get("state") == "dead":
        return "LinkedIn is refusing the token, so direct messages stopped"
    return ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def _count(conn, sql: str, args: tuple = ()) -> int:
    """One scalar count, and never an exception out of a status endpoint. A
    missing column on an older database must degrade the panel, not 500 the page
    that would have explained what is wrong."""
    try:
        row = conn.execute(sql, args).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0


def _bucket_counts(conn, sql: str, args: tuple = ()) -> dict:
    """A GROUP BY read as {bucket: count}, degrading to {} the way _count does.
    Same contract: a missing column on an older database must degrade the panel,
    not 500 the page that would have explained what is wrong."""
    try:
        return {r[0]: int(r[1] or 0) for r in conn.execute(sql, args).fetchall()}
    except sqlite3.Error:
        return {}


def _scalar(conn, sql: str, args: tuple = ()) -> str:
    try:
        row = conn.execute(sql, args).fetchone()
        return str(row[0]) if row and row[0] is not None else ""
    except sqlite3.Error:
        return ""


# ── The ONE partition behind the coverage bar ────────────────────────────────
# NC-097. The bar stacked three independently-filtered COUNTs — `scraped`,
# `queued`, `no_url` — and drew each as a share of the total with a zero floor
# and no ceiling. They add up to exactly the total only while four conditions
# all happen to hold, and that is FOUR separate coincidences, not a design:
#
#   * No contact is due for a re-scrape. A contact scraped over 180 days ago is
#     in `scraped` AND in `queued`, so once any scrape is that old the two
#     begin double-counting and the segments run past the track.
#   * No contact has a LinkedIn address and is excluded. Such a person falls
#     out of `with_url` — so they land in `no_url` — while still counting in
#     `scraped`, which has no `excluded` filter.
#   * No contact has a LinkedIn address and `facts_source = 'manual'`. They are
#     in neither `scraped` nor `queued`, so the bar would UNDER-fill.
#   * No contact is soft-deleted with an address (see NC-095).
#
# NC-122 changed who the bar is about. It covered `live` — everyone not deleted —
# and put a hidden person with an address in `no_url`, so its total counted the
# people you hid while the rest of the Data page did not. It now covers `active`,
# the people the People page lists, and a hidden person is in no bucket because
# they are not in the total.
#
# A CASE chain answers instead of five WHERE clauses, because a CASE returns its
# FIRST matching branch: every row lands in exactly one bucket, so the buckets
# sum to the total by construction rather than by arithmetic that happens to
# work out. That is the same move `crm/intake.py` makes — and `unexplained`
# below is its residue idea, kept even though this partition cannot leak, so a
# future edit that breaks exclusivity says so instead of drawing a wrong bar.
def _scrape_coverage(conn: Any, cutoff: str) -> Dict[str, Any]:
    got = _bucket_counts(
        conn,
        f"""SELECT CASE
               WHEN TRIM(COALESCE(linkedin_url, '')) = ''          THEN 'no_url'
               WHEN COALESCE(facts_source, '') = 'manual'          THEN 'manual'
               WHEN COALESCE(facts_source, '') <> ?                THEN 'queued'
               WHEN COALESCE(facts_updated_at, '') = ''
                    OR facts_updated_at < ?                        THEN 'due'
               ELSE                                                     'enriched'
             END AS bucket,
             COUNT(*) AS n
           FROM contacts
           WHERE {figures.population_where('active')}
           GROUP BY bucket""",
        (db.LI_FACTS_SOURCE, cutoff))
    out = {k: got.get(k, 0) for k in
           ("enriched", "due", "queued", "manual", "no_url")}
    total = _count(conn, f"SELECT COUNT(*) FROM contacts WHERE {figures.population_where('active')}")
    out["total"] = total
    # Zero unless a future edit makes the CASE non-exhaustive. Rendered, not
    # swallowed: a bar that silently drops people is the bug to avoid.
    out["unexplained"] = total - sum(out[k] for k in
                                     ("enriched", "due", "queued", "manual", "no_url"))
    out["population"] = "active"
    return out


def _age_hours(stamp: str) -> Optional[float]:
    """Hours since an ISO stamp, or None if it is absent or unparseable. Clamped
    at zero: a clock change can put a real write in the future, and "-3 hours
    ago" reads as a bug in the app rather than a bug in time."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (_utc_now() - dt).total_seconds() / 3600.0)


# ── Lane 1: the download ─────────────────────────────────────────────────────
def _download_lane(conn) -> Dict[str, Any]:
    """The export archive. Note what is NOT here: whether a Connections*.csv is
    sitting in Downloads right now. That answer costs a glob over a TCC-protected
    folder, which can hang — the Data page already has it from detect_sources()
    and hands it to the panel directly."""
    # NC-122: these two had no scope at all, so "people from the export" counted
    # the people you hid or deleted while every other number on the page did not.
    active = figures.population_where("active")
    seeded = _count(conn, "SELECT COUNT(*) FROM contacts WHERE source = 'linkedin' "
                          f"AND {active}")
    from_csv = _count(
        conn, f"SELECT COUNT(*) FROM contacts WHERE facts_source = ? AND {active}",
        (FACTS_CSV,))
    # Shared with the API lane: `_cmd_linkedin` is ONE registry row with two
    # routes (a path means seed_from_csv.py, no path means linkedin_import.py),
    # so this single stamp cannot say which of the two last ran. Reported once,
    # under both lanes, labelled for what it actually is.
    synced = db.state_get(conn, "import_synced_linkedin", "") or ""
    return {
        "contacts_from_linkedin": seeded,
        "facts_from_csv": from_csv,
        "last_import": synced,
        "last_import_age_hours": _age_hours(synced),
        "shared_stamp": True,
    }


# ── Lane 2: the Member Snapshot API ──────────────────────────────────────────
def _api_lane(conn) -> Dict[str, Any]:
    """Configuration plus what actually landed. The token is reported as a
    boolean because the ONLY way to know it is still valid is to spend a live
    call against LinkedIn, and this endpoint runs on every page visit."""
    raw = (os.environ.get("LINKEDIN_TOKEN") or "").strip()
    auto_on = os.environ.get("NC_LINKEDIN_AUTO", "1") != "0"
    global_auto = os.environ.get("NC_AUTOSYNC", "1") != "0"

    try:
        every_hours = int(os.environ.get("NC_LINKEDIN_EVERY_HOURS", "24"))
    except ValueError:
        every_hours = 24
    every_hours = max(1, min(720, every_hours))

    # An ATTEMPT stamp, not a success stamp — api_import writes it BEFORE the
    # call so that a dead token backs off instead of retrying every tick.
    # Reporting it as "last synced" would be a lie in exactly the case that
    # matters most: a token that has expired.
    attempt = db.state_get(conn, "autosync_api_attempt_linkedin", "") or ""
    next_due = ""
    if attempt:
        try:
            dt = datetime.fromisoformat(attempt.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            next_due = _iso(dt + timedelta(hours=every_hours))
        except ValueError:
            next_due = ""

    messages = people = 0
    newest = ""
    # A context manager, not a connection — it yields None when archive.db is
    # absent or locked, and closes for us. A fresh install has no archive at all,
    # so "no messages" and "no archive" must both render as a quiet zero here
    # rather than as a 500 on the page that would have explained why.
    #
    # NC-122: `people` is the people you can see and use, like every other count
    # of people on the page. It was COUNT(DISTINCT contact_id) over the archive,
    # which also counted the people you hid or deleted — and a group
    # conversation's own row as one more person (the NC-111 mistake, missed
    # here). The archive has no `excluded` column, so the ids to count come from
    # crm.db — the ids IN the group, not "not in" the ones out of it, because an
    # archive row can name a person with no row left at all. `messages` stays
    # every LinkedIn message saved on this Mac, the same figure the setup panel's
    # "messages archived" adds up.
    with arc.open_if_present() as arc_conn:
        if arc_conn is not None:
            try:
                messages = int(arc_conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE channel = 'linkedin'"
                ).fetchone()[0] or 0)
                inside = db.ids_in(conn, "active")
                people = sum(1 for (cid,) in arc_conn.execute(
                    "SELECT DISTINCT contact_id FROM messages "
                    f"WHERE channel = 'linkedin' AND {arc.is_a_person()}")
                    if cid in inside)
                newest = arc.channel_watermark(arc_conn, "linkedin") or ""
            except sqlite3.Error:
                pass

    # NC-068. `token_present` is a claim about .env, not about LinkedIn — an
    # expired token is still a non-empty string, and this lane reported one as
    # "Token set" for as long as it sat there. `linkedin_token.summary()` reads
    # the RECORDED answer from LinkedIn's introspection endpoint; it makes no
    # network call, which is what keeps this endpoint's no-network guarantee true.
    # The check itself runs on the autosync loop.
    tok = linkedin_token.summary()

    return {
        "token_present": bool(raw),
        "token_masked": settings.mask(raw) if raw else "",
        "token": tok,
        "auto_on": auto_on and global_auto,
        "auto_on_for_linkedin": auto_on,
        "global_autosync_on": global_auto,
        "every_hours": every_hours,
        "last_attempt": attempt,
        "last_attempt_age_hours": _age_hours(attempt),
        "next_attempt_due": next_due,
        "messages": messages,
        "people_with_messages": people,
        "newest_message": newest,
        # archive.db stores timestamps space-separated with no offset; _age_hours
        # handles both shapes. None means no LinkedIn message has ever landed,
        # which is not the same as "none lately" and must not read as silence.
        "newest_message_age_days": (lambda h: round(h / 24.0, 2) if h is not None
                                    else None)(_age_hours(newest)),
        "silent_after_days": API_SILENT_DAYS,
    }


# ── Lane 3: the scraper ──────────────────────────────────────────────────────
def _scrape_state() -> Dict[str, Any]:
    """The dispatcher's own state file, IF it is on this Mac.

    On a two-machine setup it is not — the LaunchAgent runs on the second Mac,
    so the file lives in that Mac's data/ and this Mac has no copy. Absent
    therefore means "the scraper runs elsewhere", never "no scraping is
    configured", and the panel must say it that way. Getting this wrong would
    report a working scraper as switched off."""
    path = config.data_path("scrape_state.json")
    out: Dict[str, Any] = {"present": False, "path": path}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out

    cooldown = str(raw.get("cooldown_until") or "")
    in_cooldown = False
    if cooldown:
        try:
            dt = datetime.fromisoformat(cooldown.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            in_cooldown = _utc_now() < dt
        except ValueError:
            in_cooldown = False

    out.update({
        "present": True,
        "day": str(raw.get("day") or ""),
        "target": int(raw.get("target") or 0),
        "next_ok": str(raw.get("next_ok") or ""),
        "cooldown_until": cooldown,
        "in_cooldown": in_cooldown,
        "last_session": str(raw.get("last_session") or ""),
    })
    return out


def _scraper_lane(conn) -> Dict[str, Any]:
    """Coverage and queue depth, read from what has already arrived in crm.db.

    This is the lane the Data page was most wrong about. It judged LinkedIn by an
    export file's mtime, which has nothing to do with how much of the network has
    actually been enriched."""
    # NC-122: one population for the whole lane, `active`. `total` and `scraped`
    # were `live` (hidden people counted) while `with_url` was `active`, so
    # `no_url = total - with_url` counted a hidden person as having no link.
    active = figures.population_where("active")
    with_url = _count(
        conn,
        "SELECT COUNT(*) FROM contacts WHERE TRIM(COALESCE(linkedin_url,'')) <> '' "
        f"AND {active}")
    total = _count(conn, f"SELECT COUNT(*) FROM contacts WHERE {active}")
    scraped = _count(
        conn,
        f"SELECT COUNT(*) FROM contacts WHERE facts_source = ? AND {active}",
        (FACTS_SCRAPE,))

    # NC-095. The cutoff and the clause both come from db, so this COUNT and the
    # scraper's own SELECT cannot drift apart again. Counting rather than calling
    # pick_candidates: that would SELECT * over every eligible row and then run
    # relationship_strength per row in Python — fine for a scrape session, far
    # too slow for a page render.
    cutoff = db.li_scrape_cutoff()
    where, params = db.li_queue_where(cutoff)
    queued = _count(conn, f"SELECT COUNT(*) FROM contacts WHERE {where}", params)
    stale = _count(
        conn,
        "SELECT COUNT(*) FROM contacts WHERE facts_source = ? "
        "AND COALESCE(facts_updated_at,'') <> '' AND facts_updated_at < ? "
        f"AND {active}", (FACTS_SCRAPE, cutoff))
    no_url = max(0, total - with_url)

    last_fact = _scalar(
        conn,
        "SELECT MAX(facts_updated_at) FROM contacts WHERE facts_source = ?",
        (FACTS_SCRAPE,))

    # The extras pass: a second, separate step that re-reads the sidecars the
    # scraper already saved and bills one Haiku call per contact. Its coverage is
    # not the scraper's coverage and conflating them hides a stalled pass.
    extras = _count(
        conn,
        "SELECT COUNT(*) FROM contacts WHERE COALESCE(li_extra,'') NOT IN "
        f"('', '{{}}', '[]', 'null') AND {active}")
    # Counted, not derived. `scraped - extras` looks like the pending count and
    # is not: the two populations overlap without nesting, because a contact can
    # carry extras from an earlier scrape while their facts_source has since been
    # overwritten by a manual edit. Subtracting them can produce 0, or a number
    # that means nothing.
    extras_pending = _count(
        conn,
        "SELECT COUNT(*) FROM contacts WHERE facts_source = ? "
        "AND COALESCE(li_extra,'') IN ('', '{}', '[]', 'null') "
        f"AND {active}", (FACTS_SCRAPE,))
    last_extra = _scalar(conn, "SELECT MAX(li_extra_updated_at) FROM contacts")

    photos = _count(
        conn,
        "SELECT COUNT(*) FROM contacts WHERE photo_source = 'linkedin' "
        f"AND COALESCE(photo,'') <> '' AND {active}")

    age = _age_hours(last_fact)
    return {
        "imac_configured": bool((os.environ.get("IMAC") or "").strip()),
        "contacts_total": total,
        "with_url": with_url,
        "scraped": scraped,
        "queued": queued,
        "due_for_rescrape": stale,
        "no_url": no_url,
        "rescrape_days": RESCRAPE_DAYS,
        "last_fact": last_fact,
        "last_fact_age_hours": age,
        "quiet_hours": SCRAPER_QUIET_HOURS,
        "extras": extras,
        "extras_pending": extras_pending,
        "last_extra": last_extra,
        "photos": photos,
        # NC-097: the bar renders THIS, not the four counts above. They are
        # different questions — `scraped` is "has scraped facts at all";
        # `coverage.enriched` is one branch of a partition over everyone this
        # app shows. Since NC-122 both count the same people.
        "coverage": _scrape_coverage(conn, cutoff),
        "last_merge": db.state_get(conn, "import_synced_linkedin_facts", "") or "",
        "state": _scrape_state(),
    }


# ── The one word the UI switches on ──────────────────────────────────────────
def _overall(download: Dict[str, Any], api: Dict[str, Any],
             scraper: Dict[str, Any]) -> str:
    """Decided here rather than in JS, for the same reason api_fda does it: the
    ordering IS the product decision about what to say first, and it belongs next
    to the data it reads.

    Cooldown outranks everything, including "no token". A LinkedIn checkpoint is
    the one state where the right action is to do nothing, and burying that under
    a token nag is how somebody ends up poking the scraper during a block."""
    if scraper["state"].get("in_cooldown"):
        return "cooldown"
    if not download["contacts_from_linkedin"] and not scraper["scraped"]:
        return "empty"              # nothing has ever come from LinkedIn
    if not api["token_present"]:
        return "no_token"           # the DMs lane cannot run at all
    # NC-068. A token LinkedIn has expired or revoked is functionally no token,
    # and it ranks HERE — above every "you could do more" state — because the
    # symptom without it is silence: autosync keeps attempting on schedule, every
    # attempt fails, the attempt stamp stays fresh, and the panel used to say
    # "LinkedIn is up to date". A dead token used to produce state "ok".
    if api["token"]["state"] == "dead":
        return "token_dead"
    if api["token"]["state"] == "expiring":
        return "token_expiring"     # still working, but not for long
    # We cannot ask LinkedIn whether the token lives (no client id and secret),
    # so fall back to the evidence we already have: messages have stopped. This
    # is the zero-configuration safety net — it catches the same dead token the
    # introspection check catches, later and less precisely, but without needing
    # anything set up. `is None` is excluded deliberately: never having received
    # a LinkedIn message is not silence, it is a lane that has not started.
    if api["token"]["state"] in ("unknown", "unchecked", "stale"):
        age = api.get("newest_message_age_days")
        if isinstance(age, (int, float)) and age > API_SILENT_DAYS:
            return "api_silent"
    age = scraper["last_fact_age_hours"]
    if scraper["queued"] and (age is None or age > scraper["quiet_hours"]):
        return "scraper_quiet"      # work waiting, and nothing has arrived
    if scraper["extras_pending"]:
        return "extras_pending"
    return "ok"


def _status(conn) -> Dict[str, Any]:
    download = _download_lane(conn)
    api = _api_lane(conn)
    scraper = _scraper_lane(conn)
    return {
        "state": _overall(download, api, scraper),
        "download": download,
        "api": api,
        "scraper": scraper,
        # Keys into importers.OPEN_TARGETS — the client posts a key to
        # /api/import/open and never a URL, same contract as every other
        # "Open …" button in the app.
        "targets": {"token": "linkedin"},
        "generated_at": time.time(),
    }


def handle_linkedin_status(conn) -> Dict[str, Any]:
    """GET /api/linkedin/status — all three LinkedIn lanes in one object.

    Takes only `conn`, which the route registry reads off this signature. No
    query parameters: there is no variant of this question worth asking, and a
    parameter would only ever be a way to make the endpoint do something
    expensive on a path that renders a page."""
    return _status(conn)
