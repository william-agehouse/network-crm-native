#!/usr/bin/env python3
"""Contact dates — birthdays, anniversaries, meetings — and the phrasing around them.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

Owns the recurrence resolution the UI never has to redo: a date's next occurrence,
how far away it is, and the human phrase for it ("in 3 days", "yesterday"). The
week view and the contact detail both read through here, so the two can never
disagree about when someone's birthday is.
"""

import logging
import os
import re
from datetime import datetime, date
from http import HTTPStatus
from typing import Any, Dict, List, Optional

log = logging.getLogger("networkcrm.dates")

import db
from api_core import ApiError
from api_guards import _require_contact


# DATE_GLYPH lived here and shipped an emoji per kind in every dates payload —
# presentation decided by the API, in a character the client could not restyle.
# The payload carries `kind` (it always did); static/app/icons.js maps that to a
# vector. Nothing here chooses a picture any more.


def sentence_case(text: str) -> str:
    """Upper-case the first character and leave every other one alone.

    NOT str.capitalize(), which lower-cases the whole rest of the string. That is
    the defect this exists to stop: `"based in Gothenburg".capitalize()` is
    `"Based in gothenburg"`, and that sentence was rendered on contact pages
    (NC-046) — the app destroying a proper noun the importer had got right.
    `_when_word` has the same shape, so "on Monday" shipped as "On monday".

    Lives here rather than in api_core because that module is deliberately the one
    exception type and nothing else, and because this is phrasing — which is what
    the rest of this module does. api_serialize already imports from here.
    """
    text = text or ""
    return text[:1].upper() + text[1:]


def _when_word(days: int, nxt: str) -> str:
    """How far off a date is, in words: "today", "tomorrow", "on Thursday"
    inside the coming week, and the day and month after that ("on 3 March"),
    with the year once it is a year or more away.

    NC-343. Every date past tomorrow used to read "on <weekday>", 300 days away
    included, which names the wrong Thursday for anything beyond the next six
    days."""
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    day = _parse_day(nxt)
    if day is None:
        return f"in {days}d"
    if days < 7:
        return "on " + day.strftime("%A")
    when = f"on {day.day} {day.strftime('%B')}"
    return f"{when} {day.year}" if days >= 365 else when


_DAY_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def is_calendar_day(text: Any, year_known: bool = True) -> bool:
    """Whether `text` is a real day written YYYY-MM-DD, the one form the
    calendar code reads.

    NC-338. Any text used to be stored: "tomorrow" as a birthday then showed as
    "Date · tomorrow", and a list's event date of "soon" was quietly skipped by
    the calendar. A date whose year is not known is checked as a day of a leap
    year, so a 29 February birthday with no year still counts."""
    if not isinstance(text, str) or not _DAY_SHAPE.fullmatch(text):
        return False
    y, m, d = (int(part) for part in text.split("-"))
    try:
        date(y if (year_known and y >= 1) else 2000, m, d)
    except ValueError:
        return False
    return True


# The refusal both date routes give, in the same words.
NOT_A_DAY = "dates are written YYYY-MM-DD, for example 2027-03-12"


# "Who needs you today", ranked reply_owed > going_cold > describe, first-wins
# dedup by contact id. Every kind serializes to the same superset shape so one
# frontend row component renders all three and can hand id/name to DraftModal.
# NC-055. Both of these compare CALENDAR DATES in the machine's own timezone, and
# neither may go back to arithmetic on UTC instants.
#
# Parsing "YYYY-MM-DD" as midnight UTC and subtracting datetime.now(utc) goes wrong
# every night between local midnight and UTC midnight: the two disagree about
# what day it is, so for those hours the whole upcoming-dates strip is off by
# one and a birthday TODAY renders as "tomorrow". Tests that build their
# fixtures with date.today(), which is local, fail only in those hours, because
# the code under test would be measuring in UTC.
#
# A `+ 1` in a forward version is the tell. A timedelta between two
# midnights is a whole number of days, so no correction should be needed; it was
# there to paper over the fact that `now` carried a time-of-day and the target
# did not, which truncates 0.9 days to 0. Comparing date objects removes the
# question rather than adjusting for it.
#
# A birthday is a date on a wall calendar, not an instant. "Is it today?" is
# answered by the clock in the room the person is standing in.
def _today_local() -> date:
    """The machine's own calendar date. One definition, so the two helpers below
    cannot drift apart the way the strip and its tests did."""
    return datetime.now().date()


def _parse_day(ts: str) -> "date | None":
    try:
        return datetime.strptime((ts or "")[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _days_since(ts: str) -> int:
    then = _parse_day(ts)
    return max(0, (_today_local() - then).days) if then else 0


def _days_until(ts: str) -> int:
    """Whole days from today to a future date (0 for today/unparseable) — the
    forward mirror of _days_since."""
    then = _parse_day(ts)
    return max(0, (then - _today_local()).days) if then else 0


def _fmt_past_day(on_date: str) -> str:
    """'YYYY-MM-DD' → 'Jan 5, 2030' (no platform-specific %-d)."""
    try:
        dt = datetime.strptime((on_date or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return (on_date or "")[:10]
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _date_reason(row: Any, nxt: str, days: int) -> str:
    """Honest, human phrasing for a date row — age only when the year is known,
    the user's own label when they gave one."""
    kind = (row["kind"] or "custom").lower()
    when = _when_word(days, nxt)
    label = (row["label"] or "").strip()
    if kind == "birthday":
        age = ""
        if row["year_known"]:
            try:
                age = f" (turns {int(nxt[:4]) - int((row['on_date'] or '')[:4])})"
            except (ValueError, TypeError):
                age = ""
        return f"Birthday {when}{age}"
    if kind == "anniversary":
        years = ""
        if row["year_known"]:
            try:
                n = int(nxt[:4]) - int((row["on_date"] or "")[:4])
                years = f" — {n} year{'s' if n != 1 else ''}"
            except (ValueError, TypeError):
                years = ""
        return f"{label or 'Anniversary'} {when}{years}"
    if kind == "meeting":
        return f"Seeing them {when}" + (f" · {label}" if label else "")
    return f"{label or 'Date'} {when}"


def _past_date_reason(row: Any) -> str:
    """Phrasing for a one-off date that has already passed — the actual day and
    the user's own label, never the bare word 'past'."""
    kind = (row["kind"] or "custom").lower()
    label = (row["label"] or "").strip()
    when = _fmt_past_day(row["on_date"])
    if kind == "meeting":
        return f"Met {when}" + (f" · {label}" if label else "")
    if kind == "birthday":
        return f"Birthday {when}"
    if kind == "anniversary":
        return f"{label or 'Anniversary'} · {when}"
    return f"{label or 'Date'} · {when}"


def _dates_lookahead() -> int:
    """How far ahead 'This week' looks (NC_DATES_LOOKAHEAD_DAYS setting).

    This was dead for its whole life. It is only the default for
    handle_week(days=None), and the sole caller — api_views.build_dashboard —
    hardcoded days=7, while handle_week is re-exported through app.py but not
    routed. So NC_DATES_LOOKAHEAD_DAYS sat in settings.MANAGED_KEYS, rendered as an
    editable field, and governed nothing anywhere. That is worse than the
    read-at-import bugs: those at least worked after a restart.
    """
    try:
        return max(1, min(365, int(os.environ.get("NC_DATES_LOOKAHEAD_DAYS") or 14)))
    except ValueError:
        return 14


# Rows one "upcoming dates" answer may carry. db.upcoming_dates defaults to
# limit=50 and handle_week never passed one, so a lookahead of 365 days silently
# stopped at 50 rows with nothing on screen to say so — and a calendar import that
# adds meetings in bulk walks straight into it. Not a MANAGED_KEYS entry yet;
# it belongs in crm/settings.py next to NC_DATES_LOOKAHEAD_DAYS.
WEEK_ROW_LIMIT_DEFAULT = 400


def _week_row_limit() -> int:
    """Read at call time, per the api_import.import_timeout_sec idiom."""
    try:
        return max(1, min(5000, int(os.environ.get("NC_DATES_MAX_ROWS")
                                    or WEEK_ROW_LIMIT_DEFAULT)))
    except ValueError:
        return WEEK_ROW_LIMIT_DEFAULT


def _contact_dates(conn: Any, contact_id: int) -> List[Dict[str, Any]]:
    """A contact's dates with the recurrence already resolved server-side — the
    frontend must never redo the calendar math."""
    out = []
    for r in db.get_contact_dates(conn, contact_id):
        nxt = db.next_occurrence(r["on_date"], r["recurring"])
        days = _days_until(nxt) if nxt else 0
        out.append({
            "id": r["id"], "kind": (r["kind"] or "custom").lower(),
            "label": r["label"] or "", "on_date": r["on_date"],
            "year_known": bool(r["year_known"]), "recurring": bool(r["recurring"]),
            "next_occurrence": nxt, "days_until": days,
            "reason": _date_reason(r, nxt, days) if nxt else _past_date_reason(r),
            # Where the row came from. Without this the person page could not tell a
            # meeting the user typed from one crm/ics_import.py inferred off an
            # invite, and the timeline caption hardcoded "added" for both — an
            # imported guess presented as a hand-entered fact. .get() with a
            # fallback because a row written before the column existed has no value
            # and every one of those WAS hand-entered.
            "source": (_row_get(r, "source") or "manual"),
        })
    return out


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """A sqlite3.Row raises KeyError for a missing column rather than returning
    None, and this module has to read columns that ensure_columns() may not have
    added yet on a database opened by an older build."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def handle_set_date(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/contact/{id}/dates — add a date, or remove one with
    {remove: true, date_id}. One multiplexed write route, same shape as the
    identifier editor, so no new URL pattern is needed."""
    _require_contact(conn, contact_id)
    if body.get("remove"):
        date_id = body.get("date_id")
        if not isinstance(date_id, int):
            raise ApiError(HTTPStatus.BAD_REQUEST, "date_id must be an integer")
        db.delete_contact_date(conn, date_id, contact_id)
    else:
        kind = str(body.get("kind") or "custom").strip().lower()
        on_date = str(body.get("on_date") or "").strip()
        if kind not in db.DATE_KINDS:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           f"kind must be one of {sorted(db.DATE_KINDS)}")
        if not on_date:
            raise ApiError(HTTPStatus.BAD_REQUEST, "on_date is required (YYYY-MM-DD)")
        year_known = body.get("year_known") is not False
        if not is_calendar_day(on_date, year_known):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"on_date: {NOT_A_DAY}")
        rec = body.get("recurring")
        try:
            db.add_contact_date(
                conn, contact_id, kind, on_date,
                label=str(body.get("label") or ""),
                year_known=1 if year_known else 0,
                recurring=None if rec is None else (1 if rec else 0),
            )
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))
    conn.commit()
    return {"ok": True, "dates": _contact_dates(conn, contact_id)}


# NOT ROUTED (NC-009). handle_week's only caller was
# api_views.handle_today, and Today was deleted. Kept whole, with its tests, like
# feed.handle_feed — but its two Settings fields were removed, because a field
# that renders and saves and does nothing is the defect _dates_lookahead's own
# docstring below was written about. Nothing aggregates upcoming dates any more;
# per-contact dates still live on a person's record.
def handle_week(conn: Any, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """"Who am I seeing this week" — manual contact dates merged with dated
    lists (events), soonest first. The ONLY sources are contact_dates and
    lists.event_date: this app never reads the OS calendar, by design."""
    span = days if days is not None else _dates_lookahead()
    limit = _week_row_limit()
    items: List[Dict[str, Any]] = []
    # The limit is passed explicitly. Omitting it took db.upcoming_dates's
    # limit=50 default, so a long lookahead truncated at 50 rows in silence.
    rows = db.upcoming_dates(conn, days_ahead=span, limit=limit)
    if len(rows) >= limit:
        # Nothing here can put a count on screen — this function returns a bare
        # list and today.js iterates it — so the honest move available is to say so
        # in the log rather than to drop rows quietly. Raise NC_DATES_MAX_ROWS, or
        # give this a shape that can carry the number.
        log.warning("upcoming dates hit the %d-row limit for a %d-day span; "
                    "some dates are not shown", limit, span)
    for nxt, d, row in rows:
        crow = db.get_contact(conn, row["contact_id"])
        if crow is None:
            continue
        items.append({
            "when": nxt, "days_until": d, "kind": (row["kind"] or "custom").lower(),
            "title": crow["name"], "contact_id": crow["id"], "date_id": row["id"],
            "reason": _date_reason(row, nxt, d),
        })
    for l in db.upcoming_events(conn, days_ahead=span):
        d = _days_until(l["event_date"])
        items.append({
            "when": l["event_date"], "days_until": d, "kind": "event",
            "title": l["name"], "list_id": l["id"], "venue": l["venue"] or "",
            "people": l["members"] or 0, "attending": l["attending"] or 0,
            "reason": f"{sentence_case(_when_word(d, l['event_date']))}"
                      + (f" · {l['venue']}" if l["venue"] else ""),
        })
    items.sort(key=lambda i: (i["when"], i["title"] or ""))
    return items


# ── The one surface that aggregates dates (NC-040) ───────────────────────────
# Deleting Today (NC-009) took the only aggregated view of upcoming dates with
# it, and a birthday tomorrow then surfaced nowhere: you had to already be
# looking at that person. This is the narrow replacement — not a daily review
# deck, just the next few dates on a page you are on anyway.
#
# It deliberately excludes kind='meeting'. Those come from your own calendar
# subscription, and your calendar app already shows them; repeating them here
# would turn a "you'd otherwise forget this" strip into a second, worse agenda.
# Meetings still show on the person's own page, where they are context about
# that person rather than a to-do list.
UPCOMING_KINDS = ("birthday", "anniversary", "custom", "event")
# How many land in the strip. More than a few stops being glanceable and starts
# being a daily agenda. The count of what did NOT fit is returned too —
# silently truncating is the defect handle_week's own row-limit comment is about.
UPCOMING_ROWS = 3


def handle_upcoming(conn: Any, query: Dict[str, Any]) -> Dict[str, Any]:
    """GET /api/upcoming — the next few personal dates, soonest first.

    Shape: {"days": int, "shown": [...], "more": int}. `days` is echoed back
    because the caller renders it ("2 more in the next 14 days") and must not
    guess at a window the server chose. `more` is how many matched beyond the
    ones returned, so nothing is dropped in silence.

    Reads through handle_week so recurrence, exclusion and phrasing cannot
    disagree between this and a person's own page.
    """
    span = _dates_lookahead()
    try:
        want = int((query.get("days", [""])[0] or "").strip())
    except (ValueError, TypeError, AttributeError):
        want = 0
    if want > 0:
        span = max(1, min(365, want))
    rows = [i for i in handle_week(conn, days=span) if i["kind"] in UPCOMING_KINDS]
    return {"days": span, "shown": rows[:UPCOMING_ROWS],
            "more": max(0, len(rows) - UPCOMING_ROWS)}


# Route registry (see the ROUTE_MODULES note in crm/app.py): this module now
# claims one URL, so it needs no branch in app.py's route table.
ROUTES = {"GET": {"/api/upcoming": "handle_upcoming"}}
