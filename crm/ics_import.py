#!/usr/bin/env python3
"""Calendar (.ics) → proposed contact dates. Parsing, matching and reporting only.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

WHY .ics AND NOTHING ELSE
-------------------------
Google Calendar and Apple/macOS Calendar both speak RFC 5545, so one parser
serves both. It is fed three ways, all of them the *user* handing us data:

  1. Google → Settings → "Secret address in iCal format" (an https URL).
  2. iCloud → a published calendar's `webcal://` URL (same bytes over https).
  3. A file exported from Calendar.app (File → Export…).

There is no EventKit here and nothing reads `~/Library/Calendars`: this module
is the route that needs no permission, which invariant 6 says every source must
keep. The calendar database itself is protected by macOS, and reading it is the
sync helper's job — it copies Calendar's database into the app's own folder and
the `calendar_local` row reads that copy. (The two routes are kept apart
because this one works on a Mac where nobody has granted anything.) This module
also does no network I/O at all: it takes `text`, so the fetching side (and its
call-time timeout) is a separate, testable decision.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It writes, under `--apply` only. `apply()` calls `db.add_contact_date()` for each
proposed meeting and commits; without `--apply` the run stops after
`proposed_dates()` plus `report()`, whose whole output is "here is what I would
write, here is what I refused to write, and why". `--dry-run` is that preview,
and `importers.REGISTRY` passes one flag or the other, never neither.

The refusals-first design is deliberate and worth keeping: the write is the
irreversible half, and making the refusals visible is what stops this shipping as
a machine that quietly invents facts about people.

`match_attendees()` and `proposed_dates()` still use their connection strictly
for SELECTs, and still never call `db.init_db()` or `db.ensure_columns()` — both
of those COMMIT. Only `apply()` writes.

**The calendar importers do write (NC-005).** `apply()` is the writer, and the
`--dry-run` preview is the part that writes nothing. Describing them as
read-only is wrong: it is why three cards on the Data page once said "Nothing is
written to your CRM yet" beside a button that wrote.

WHAT THE WRITER USES
--------------------
One function: `db.upsert_calendar_date(conn, contact_id, on_date, source_uid=…,
label=…, recurring=…)`, then `conn.commit()`. `contact_dates` carries `source` and
`source_uid` columns (invariant 9), so a row says where it came from, and a re-import
finds its own earlier row by the event's UID instead of by its text. That gives
invariant 3 (idempotent re-import) and lets a rescheduled meeting MOVE instead of
duplicating. It never touches a row the owner typed (`source` = "manual").

  • every `label` this module emits is the event's SUMMARY, trimmed. It is built
    from stored event text only — never from `api_dates._when_word`, which produces
    RELATIVE phrases ("in 3 days", "tomorrow"). Freezing one of those into a row
    would make the row wrong the next morning.
  • there is no "(calendar)" suffix on a label. It was a workaround from before
    `source` existed; nothing appends it any more (NC-370 removed the note that
    still described it).
  • `proposed_dates()` de-duplicates on (contact_id, on_date, label) itself, so
    the writer's insert count matches the proposal count.

Two adjacent traps, for whoever builds the UI on top:
  • `db.upcoming_dates()` ends with `return out[:limit]` and `limit` defaults to
    50. `api_dates.handle_week()` does not pass it. Any "how many upcoming dates
    to show" setting must be threaded into that argument or it governs nothing.
  • `api_dates.handle_week`'s docstring says the only sources are `contact_dates`
    and `lists.event_date`. That stays true: this importer proposes
    `contact_dates` rows. It is not a new source for that view, and the sentence
    "this app never reads the OS calendar" is still literally true.

RECURRENCE — THE DECISION
-------------------------
**A repeating event is imported as its first occurrence only (its DTSTART), is
flagged as recurring, and is reported in a sentence that says so. RRULE is never
expanded. RDATE and EXDATE are counted and reported, not applied.** And a series
only gets `recurring=1` when its RRULE is a plain yearly one.

Why not expand RRULE: doing it correctly by hand is BYSETPOS/BYDAY/WKST/UNTIL-vs
-COUNT/DST-boundary work — hundreds of lines of stdlib date arithmetic whose bugs
are silent wrong dates about real people. Why not drop repeating events: silent
loss is the failure mode this codebase has already fixed elsewhere. The anchor
occurrence is a fact that came out of the file; every later occurrence would be
this module's arithmetic. So: keep the fact, refuse the arithmetic, say so out
loud.

Why the yearly restriction: `db.next_occurrence(on_date, recurring)` treats
`recurring` as **annual** and nothing else (`_mk(ty)` / `_mk(ty + 1)`). The CRM
date model cannot represent "every Monday". Storing a weekly standup with
`recurring=1` would silently reinterpret it as an annual event — a lie the UI
would then repeat forever. Non-yearly series therefore land as a single dated
meeting, and `report()` says how many.

SETTINGS (all read at call time — the `api_import.import_timeout_sec` idiom)
----------------------------------------------------------------------------
Not added to `settings.MANAGED_KEYS` by this module. Whoever does, these are the
keys and their defaults:

    NC_ICS_MAX_ATTENDEES   6    other people on the invite before the whole
                                event is refused as "not a meeting with someone"
    NC_ICS_BACK_DAYS      365   how far back a calendar event may be and still
                                be proposed
    NC_ICS_FORWARD_DAYS   365   how far forward
    NC_ICS_LABEL_MAX       60   characters of the event SUMMARY kept in a label
    NC_ICS_TZ             ""    zone for floating times, and for the day an event
                                written in UTC falls on (empty = this Mac's)

Every one of them is a cap, and no limit may be a silent constant: the defaults
live in the reader functions below, one comment per default, and each reader
re-reads `os.environ` on every call so a Settings edit takes effect without a
restart.
"""

import hashlib
import os
import re
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import config  # noqa: F401 — loads secrets/.env before anything reads os.environ
import db

# The same file every other importer honours (invariant 2). Module-level and
# patchable, exactly like imessage_import.EXCLUDE_FILE — tests patch the owner.
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))

# Attendee kinds that are furniture, not people (invariant 1: never create, and
# never propose a date "with" a conference room).
SKIP_CUTYPES = {"ROOM", "RESOURCE", "GROUP"}
# Someone who said no was not seen.
SKIP_PARTSTATS = {"DECLINED"}

# Properties whose values are RFC 5545 TEXT (backslash-escaped). Everything else
# is taken verbatim.
TEXT_PROPS = {"SUMMARY", "DESCRIPTION", "LOCATION", "COMMENT", "CATEGORIES",
              "RESOURCES", "CONTACT", "X-WR-CALNAME"}

PROVENANCE_SOURCE = "ics"        # what wrote the row, for a future source column
DATE_KIND = "meeting"            # the only db.DATE_KINDS value a calendar earns


# ── Call-time settings readers ──────────────────────────────────────────────
# What the attendee limit may be, for the setting and for `--max-attendees` alike.
# NC-370: the command line used to skip the floor, so 0 refused every event.
MAX_ATTENDEES_RANGE = (1, 500)


def _int_env(key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(key, default) or default)))
    except (TypeError, ValueError):
        return default


def max_attendees_setting() -> int:
    """Other people on an invite before the event is refused (NC_ICS_MAX_ATTENDEES).

    Default 6: a coffee, a dinner, a three-way call are meetings with someone;
    a 40-person all-hands is not. You are never counted — this is the number of
    OTHER humans on the invite. 0 would refuse everything, so the floor is 1.
    """
    return _int_env("NC_ICS_MAX_ATTENDEES", 6, *MAX_ATTENDEES_RANGE)


def lookback_days() -> int:
    """How far back an event may be and still be proposed (NC_ICS_BACK_DAYS).
    Default 365 — one year of "when did I last actually see this person"."""
    return _int_env("NC_ICS_BACK_DAYS", 365, 0, 36500)


def lookahead_days() -> int:
    """How far forward (NC_ICS_FORWARD_DAYS). Default 365."""
    return _int_env("NC_ICS_FORWARD_DAYS", 365, 0, 36500)


def label_max() -> int:
    """Characters of SUMMARY kept in a label (NC_ICS_LABEL_MAX). Default 60 —
    long enough to be recognisable in the week view, short enough not to wrap."""
    return _int_env("NC_ICS_LABEL_MAX", 60, 8, 400)


def _local_zone() -> Any:
    """Zone for floating (zoneless) times — NC_ICS_TZ, default this Mac's zone.

    A floating time means "whatever the wall clock says where you are", so the
    honest default is the machine's own zone rather than UTC: UTC would shift a
    21:00 dinner onto the next day.
    """
    name = (os.environ.get("NC_ICS_TZ") or "").strip()
    if name:
        zone, _ = _zone_for(name)
        if zone is not None:
            return zone
    return datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc


def _local_day(instant: datetime) -> str:
    """The calendar day an instant falls on, on the clock of whoever runs the
    import. NC-370.

    Used only for events a file stores in UTC ("...Z"). UTC is nobody's wall
    clock: 23:00Z is already tomorrow in Stockholm, so filing it on the UTC day
    put the meeting a day early for anyone east of Greenwich, and a day late for
    anyone west of it at the other end of the day.

    NC_ICS_TZ names the zone when it is set. Otherwise this is the Mac's own zone,
    asked for THIS instant: `astimezone()` with no argument applies the daylight
    saving rules that were in force on the day, where a fixed offset read off the
    clock today would be an hour wrong for half the year.
    """
    name = (os.environ.get("NC_ICS_TZ") or "").strip()
    zone = _zone_for(name)[0] if name else None
    local = instant.astimezone(zone) if zone is not None else instant.astimezone()
    return local.strftime("%Y-%m-%d")


def _today() -> _date:
    """UTC today — the same clock `db.next_occurrence` and `db.upcoming_dates`
    use, so a proposal inside the window can't be a day off from what the week
    view then computes."""
    return datetime.now(timezone.utc).date()


# ── RFC 5545 line unfolding ─────────────────────────────────────────────────
def unfold(text: str) -> List[str]:
    """Split an iCalendar body into logical lines, undoing RFC 5545 folding.

    A line whose first character is a space or a horizontal tab continues the
    previous line; that ONE character is removed and the remainder appended with
    no separator. Google folds aggressively and mid-word, so inserting anything
    (even a space) corrupts values — "Zoë" arrives as "Zo" + "ë".

    Accepts CRLF, LF and lone-CR line endings, and strips a leading BOM (a file
    saved out of Calendar.app or downloaded through a browser may carry one).
    Blank lines carry no property and are dropped. A continuation with nothing
    to continue (a file that begins mid-fold) becomes its own logical line
    rather than vanishing.
    """
    if not text:
        return []
    if text[:1] == "\ufeff":          # BOM from a browser download / Calendar.app
        text = text[1:]
    out: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t"):
            cont = raw[1:]          # exactly one char, per the spec
            if out:
                out[-1] += cont
            elif cont.strip():
                out.append(cont)
            continue
        if not raw.strip():
            continue
        out.append(raw)
    return out


# ── Content lines: NAME;PARAM=value;PARAM="quoted, value":VALUE ─────────────
def _split_unquoted(text: str, sep: str) -> List[str]:
    """Split on `sep` only outside double quotes."""
    parts: List[str] = []
    buf: List[str] = []
    in_q = False
    for ch in text:
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
        elif ch == sep and not in_q:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _unescape_param(value: str) -> str:
    """Strip surrounding quotes and apply RFC 6868 caret escaping.

    Parameter values are quoted, not backslash-escaped: CN="Nilsson, Anna" holds
    a real comma, and the quotes are the reason the comma isn't a value
    separator. RFC 6868 adds ^n / ^' / ^^ for the three characters quoting still
    can't carry.
    """
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    if "^" not in v:
        return v
    out: List[str] = []
    i = 0
    while i < len(v):
        if v[i] == "^" and i + 1 < len(v):
            nxt = v[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "'":
                out.append('"'); i += 2; continue
            if nxt == "^":
                out.append("^"); i += 2; continue
        out.append(v[i])
        i += 1
    return "".join(out)


def _unescape_text(value: str) -> str:
    r"""Un-escape an RFC 5545 TEXT value: \\ \, \; \n \N (and the \: some
    producers emit) become backslash, comma, semicolon, newline, colon."""
    if "\\" not in value:
        return value
    out: List[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in (",", ";", ":", "\\", '"'):
                out.append(nxt)
            else:
                out.append(nxt)      # unknown escape: keep the character
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_text_list(value: str) -> List[str]:
    r"""A TEXT list value ("Work,Coffee") → its items, un-escaped. NC-370.

    The split happens on commas that are NOT escaped, and the un-escaping happens
    afterwards. Un-escaping first turned `Work\,Life` (one category with a comma in
    it) into `Work,Life` and then split it into two, discarding the escape the
    file wrote on purpose.
    """
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            buf.append(value[i:i + 2])        # an escaped character stays together
            i += 2
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    items = (_unescape_text(p).strip() for p in parts)
    return [item for item in items if item]


def parse_content_line(line: str) -> Tuple[str, Dict[str, List[str]], str]:
    """One logical line → (NAME, {PARAM: [values]}, raw value).

    The name/value split is the first colon that is NOT inside double quotes —
    which is the whole reason ATTENDEE;CN="Doe, Jane":mailto:x@y.z parses. A
    line with no colon at all is malformed; it comes back as ("", {}, "") and the
    caller records it rather than dropping it.
    """
    in_q = False
    head = value = ""
    found = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value, found = line[:i], line[i + 1:], True
            break
    if not found:
        return "", {}, ""
    segs = _split_unquoted(head, ";")
    name = segs[0].strip().upper()
    params: Dict[str, List[str]] = {}
    for seg in segs[1:]:
        if not seg.strip():
            continue
        key, eq, val = seg.partition("=")
        key = key.strip().upper()
        if not eq:                      # bare parameter (illegal, but seen)
            params.setdefault(key, []).append("")
            continue
        params.setdefault(key, []).extend(
            _unescape_param(v) for v in _split_unquoted(val, ",")
        )
    return name, params, value


def _p1(params: Dict[str, List[str]], key: str, default: str = "") -> str:
    """First value of a parameter, or `default`."""
    vals = params.get(key) or []
    return vals[0] if vals else default


# ── Dates and times ────────────────────────────────────────────────────────
_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DUR_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


class IcsValueError(ValueError):
    """A property this module could not make sense of. Always ends up in
    report(), never in a silent `continue`."""


def _zone_for(tzid: str) -> Tuple[Optional[Any], str]:
    """(tzinfo, resolved name) for a TZID, or (None, tzid) if unknown.

    `zoneinfo` is stdlib and is the whole timezone story here — no pytz, no
    dateutil. Apple writes TZIDs like
    "/freeassociation.sourceforge.net/Europe/Paris", so the leading junk
    path is peeled back to the IANA tail. Windows-style ids ("W. Europe
    Standard Time") are NOT mapped: a hand-written alias table would be a
    hundred lines of guesswork that goes stale. Those come back unresolved and
    the event is treated as floating, with the raw id kept in `raw_tzid` and the
    fallback counted in report() — a stated approximation beats a silent one.
    """
    key = (tzid or "").strip().strip('"')
    if not key:
        return None, ""
    if key.upper() in ("UTC", "GMT", "Z", "ETC/UTC", "ETC/GMT"):
        return timezone.utc, "UTC"
    candidates = [key]
    if "/" in key:
        segs = [s for s in key.split("/") if s]
        if len(segs) >= 2:
            candidates.append("/".join(segs[-2:]))
        if segs:
            candidates.append(segs[-1])
    for cand in candidates:
        try:
            return ZoneInfo(cand), cand
        except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError, no tzdata
            continue
    return None, key


def parse_duration(value: str) -> Optional[timedelta]:
    """An RFC 5545 DURATION ("PT1H30M", "P1D", "-PT15M", "P2W") → timedelta.
    None when it isn't one."""
    m = _DUR_RE.match((value or "").strip())
    if not m or not any(m.group(g) for g in ("w", "d", "h", "m", "s")):
        return None
    part = {g: int(m.group(g) or 0) for g in ("w", "d", "h", "m", "s")}
    delta = timedelta(weeks=part["w"], days=part["d"], hours=part["h"],
                      minutes=part["m"], seconds=part["s"])
    return -delta if m.group("sign") == "-" else delta


def parse_dt(value: str, params: Dict[str, List[str]]) -> Dict[str, Any]:
    """A DTSTART/DTEND value + its parameters → a resolved instant.

    Returns {dt, all_day, on_date, tzid, raw_tzid, floating}. Handles the four
    forms that actually arrive:

      DTSTART;VALUE=DATE:20240301            all-day, no time at all
      DTSTART;TZID=Europe/Paris:…T140000     zoned wall-clock
      DTSTART:20240301T120000Z               UTC
      DTSTART:20240301T140000                floating (no zone in the file)

    `on_date` is the day in the event's OWN zone, never converted. "I saw Anna
    on the 1st" is a wall-clock fact; re-projecting a 09:00 Paris meeting
    into UTC (or a 23:00 one into anything) is how an importer silently moves a
    date by a day. All-day values are never zone-converted for the same reason —
    `dt` for them is midnight UTC purely so the field is an aware datetime as
    specified, and `all_day` says not to read a time out of it.

    The one exception is a time written in UTC ("...Z"): it has no wall clock of
    its own, so `on_date` is the day it falls on for the person running the
    import (`_local_day`, NC-370). `dt` stays the UTC instant.
    """
    raw = (value or "").strip()
    vtype = _p1(params, "VALUE").upper()
    tzid_raw = _p1(params, "TZID").strip()

    m = _DATE_RE.match(raw)
    if m and (vtype == "DATE" or "T" not in raw):
        y, mo, d = (int(x) for x in m.groups())
        try:
            day = _date(y, mo, d)
        except ValueError as exc:
            raise IcsValueError(f"impossible date {raw!r}: {exc}")
        # raw_tzid is deliberately NOT forwarded for a DATE value. RFC 5545 §3.2.19
        # says TZID must not be applied to a DATE, so an all-day event carrying one
        # is malformed input this module tolerates — like a missing UID or DTEND.
        # But two consumers read `raw_tzid and not tzid` as "this Mac could not
        # resolve the zone", so forwarding it made an all-day event claim
        # "unknown timezone 'Europe/Paris'" and report an unknown_timezones
        # count. That is a false statement, and worse, it is indistinguishable from
        # the real Windows-TZID case those fields exist to surface. The TZID is
        # kept under its own name so nothing is lost and no consumer misreads it.
        return {"dt": datetime(y, mo, d, tzinfo=timezone.utc), "all_day": True,
                "on_date": day.isoformat(), "tzid": "", "raw_tzid": "",
                "tzid_ignored_for_date": tzid_raw, "floating": False}

    m = _DT_RE.match(raw)
    if not m:
        raise IcsValueError(f"unrecognised date-time {raw!r}")
    y, mo, d, hh, mm, ss, zulu = (m.group(i) for i in range(1, 8))
    floating = False
    if zulu:
        zone, resolved = timezone.utc, "UTC"
    elif tzid_raw:
        zone, resolved = _zone_for(tzid_raw)
        if zone is None:
            zone, resolved, floating = _local_zone(), "", True
    else:
        zone, resolved, floating = _local_zone(), "", True
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), tzinfo=zone)
    except ValueError as exc:
        raise IcsValueError(f"impossible date-time {raw!r}: {exc}")
    on_date = dt.strftime("%Y-%m-%d")
    if zulu:
        try:
            on_date = _local_day(dt)
        except (OverflowError, OSError, ValueError):
            # A year at the very edge of the calendar cannot be moved onto a local
            # clock. Refuse the event with a reason rather than file it on a day
            # that is a guess.
            raise IcsValueError(f"cannot work out which local day {raw!r} falls on")
    return {"dt": dt, "all_day": False, "on_date": on_date,
            "tzid": resolved or ("UTC" if zulu else ""), "raw_tzid": tzid_raw,
            "floating": floating}


# ── Attendees ──────────────────────────────────────────────────────────────
def _address(value: str) -> str:
    """The email out of a CAL-ADDRESS ("mailto:a@b.example" → "a@b.example").

    Anything that isn't a mailto: (a URN, an Exchange X400 address, an empty
    value) yields "" — the person can still be matched by CN.
    """
    v = (value or "").strip()
    if v[:7].lower() == "mailto:":
        v = v[7:].strip()
    return v if "@" in v else ""


def _person(name: str, params: Dict[str, List[str]], value: str,
            role: str) -> Dict[str, Any]:
    """One ATTENDEE/ORGANIZER line → the dict shape match_attendees consumes."""
    return {
        "email": _address(value).lower(),
        "email_raw": _address(value),
        "name": _p1(params, "CN").strip(),
        "partstat": _p1(params, "PARTSTAT", "NEEDS-ACTION").strip().upper(),
        "cutype": _p1(params, "CUTYPE", "INDIVIDUAL").strip().upper(),
        "role": _p1(params, "ROLE", "REQ-PARTICIPANT").strip().upper(),
        "rsvp": _p1(params, "RSVP").strip().upper() == "TRUE",
        "kind": role,              # "attendee" | "organizer"
        "prop": name,
        "raw": value,
    }


def _identity_key(person: Dict[str, Any]) -> str:
    """How well this line identifies a human, as a comparable key — or "" when it
    does not identify anyone at all.

    ONE definition, because there were two and they disagreed. Both the attendee
    dedupe in _build_event and the organizer check in _roster built this key
    inline as `email or ("cn:" + normalize_name(name))`, which evaluates to the
    constant "cn:" for anyone with neither an email nor a CN — so every nameless
    invitee collapsed into one, and a nameless ORGANIZER was silently dropped for
    matching a nameless attendee. Legal input, too: a CAL-ADDRESS is a URI and CN
    is optional, which is what an Apple urn:uuid or a privacy-scrubbed published
    calendar looks like.

    Returning "" for "no identity" is the load-bearing part — the callers must
    decide what to do about an unidentifiable person rather than have a truthy
    placeholder decide silently for them.
    """
    if person.get("email"):
        return person["email"]
    name = (person.get("name") or "").strip()
    if name:
        return "cn:" + db.normalize_name(name)
    addr = (person.get("raw") or "").strip().lower()
    return "addr:" + addr if addr else ""


def _name_variants(name: str) -> List[str]:
    """Spellings of a CN worth trying against `db.find_exact`.

    Directory servers write "Nilsson, Anna" where the CRM holds "Anna Nilsson",
    and `db.normalize_name` strips the comma without reordering, so the two never
    match. One comma and nothing else is unambiguous enough to flip; two commas
    (a title, a suffix) is not, and is left alone rather than guessed at.
    """
    base = (name or "").strip()
    if not base:
        return []
    out = [base]
    if base.count(",") == 1:
        last, _, first = base.partition(",")
        last, first = last.strip(), first.strip()
        if last and first:
            out.append(f"{first} {last}")
    return out


# ── Parsing a whole calendar ───────────────────────────────────────────────
def _synthetic_uid(lines: List[str]) -> str:
    """A stable id for an event whose file forgot UID. Deterministic across runs
    (it hashes the event's own text), so it can still be a dedupe key."""
    digest = hashlib.sha1("\n".join(lines).encode("utf-8", "replace")).hexdigest()
    return f"nc-nouid-{digest[:16]}"


def _blank_event() -> Dict[str, Any]:
    """Every key parse_events guarantees. A caller must never have to `.get()`
    its way around a partially-built event."""
    return {
        "uid": "", "uid_synthetic": False, "summary": "", "description": "",
        "location": "", "status": "", "transparency": "",
        "start": None, "end": None, "all_day": False, "on_date": "",
        "tzid": "", "raw_tzid": "", "floating": False,
        "attendees": [], "organizer": None,
        "is_recurring": False, "rrule": "", "rrule_parts": {},
        "recurrence_id": "", "is_override": False,
        "exdates": [], "rdates": [], "sequence": 0, "categories": [],
        "calendar": "", "prodid": "", "method": "",
        "x_props_ignored": 0, "unknown_lines": 0,
        "parsed": True, "error": "", "warnings": [], "raw": "",
    }


def _rrule_parts(value: str) -> Dict[str, str]:
    """"FREQ=WEEKLY;BYDAY=MO" → {"FREQ": "WEEKLY", "BYDAY": "MO"}. Not expanded —
    see the recurrence decision in the module docstring."""
    parts: Dict[str, str] = {}
    for chunk in _split_unquoted(value or "", ";"):
        key, eq, val = chunk.partition("=")
        if eq and key.strip():
            parts[key.strip().upper()] = val.strip()
    return parts


def _build_event(block: Dict[str, Any], header: Dict[str, str]) -> Dict[str, Any]:
    """Assemble one VEVENT. Never raises: a bad event comes back with
    parsed=False and an `error` a human can act on."""
    ev = _blank_event()
    ev["calendar"] = header.get("X-WR-CALNAME", "")
    ev["prodid"] = header.get("PRODID", "")
    ev["method"] = header.get("METHOD", "")
    ev["raw"] = "\n".join(block["raw"])
    ev["unknown_lines"] = block.get("bad_lines", 0)

    dtstart: Optional[Tuple[str, Dict[str, List[str]]]] = None
    dtend: Optional[Tuple[str, Dict[str, List[str]]]] = None
    duration = ""
    seen_emails = set()

    for name, params, value in block["props"]:
        if name.startswith("X-"):
            ev["x_props_ignored"] += 1
            continue
        text = _unescape_text(value) if name in TEXT_PROPS else value
        if name == "UID":
            ev["uid"] = text.strip()
        elif name == "SUMMARY":
            ev["summary"] = text.strip()
        elif name == "DESCRIPTION":
            ev["description"] = text
        elif name == "LOCATION":
            ev["location"] = text.strip()
        elif name == "STATUS":
            ev["status"] = value.strip().upper()
        elif name == "TRANSP":
            ev["transparency"] = value.strip().upper()
        elif name == "SEQUENCE":
            try:
                ev["sequence"] = int(value.strip() or 0)
            except ValueError:
                ev["warnings"].append(f"bad SEQUENCE {value.strip()!r}")
        elif name == "CATEGORIES":
            ev["categories"] += _split_text_list(value)
        elif name == "DTSTART":
            dtstart = (value, params)
        elif name == "DTEND":
            dtend = (value, params)
        elif name == "DURATION":
            duration = value.strip()
        elif name == "RRULE":
            ev["rrule"] = value.strip()
            ev["rrule_parts"] = _rrule_parts(value)
            ev["is_recurring"] = True
        elif name == "RECURRENCE-ID":
            ev["recurrence_id"] = value.strip()
            ev["is_override"] = True
        elif name == "EXDATE":
            ev["exdates"] += [v.strip() for v in _split_unquoted(value, ",") if v.strip()]
        elif name == "RDATE":
            ev["rdates"] += [v.strip() for v in _split_unquoted(value, ",") if v.strip()]
        elif name in ("ATTENDEE", "ORGANIZER"):
            role = "organizer" if name == "ORGANIZER" else "attendee"
            person = _person(name, params, value, role)
            if role == "organizer":
                ev["organizer"] = person
            else:
                # _identity_key is the single owner of "how well does this line
                # identify a human" — see its docstring for the bug that came from
                # having two copies of this logic that disagreed.
                key = _identity_key(person)
                if not key:
                    # No email, no name, no address either. Positional, so two
                    # empty ATTENDEE lines stay two people rather than becoming one.
                    key = f"anon:{len(ev['attendees'])}"
                if key not in seen_emails:      # the same invitee twice is one person
                    seen_emails.add(key)
                    ev["attendees"].append(person)

    if dtstart is None:
        ev["parsed"] = False
        ev["error"] = "it has no start date, so it cannot become a date"
        return ev
    try:
        start = parse_dt(*dtstart)
    except IcsValueError as exc:
        ev["parsed"] = False
        ev["error"] = f"start date: {exc}"
        return ev
    ev.update({"start": start["dt"], "all_day": start["all_day"],
               "on_date": start["on_date"], "tzid": start["tzid"],
               "raw_tzid": start["raw_tzid"], "floating": start["floating"]})
    if start["raw_tzid"] and not start["tzid"]:
        ev["warnings"].append(
            f"unknown timezone {start['raw_tzid']!r} — read as local wall-clock time")

    if dtend is not None:
        try:
            ev["end"] = parse_dt(*dtend)["dt"]
        except IcsValueError as exc:
            ev["warnings"].append(f"DTEND: {exc} — end time unknown")
    elif duration:
        delta = parse_duration(duration)
        if delta is None:
            ev["warnings"].append(f"unreadable DURATION {duration!r}")
        else:
            ev["end"] = ev["start"] + delta
    if ev["end"] is None:
        # RFC 5545: a DATE-valued DTSTART with no DTEND lasts one day; a
        # DATE-TIME one takes no time. Either way the DAY is unchanged, which is
        # all this importer stores.
        ev["end"] = ev["start"] + timedelta(days=1) if ev["all_day"] else ev["start"]

    if not ev["uid"]:
        ev["uid"] = _synthetic_uid(block["raw"])
        ev["uid_synthetic"] = True
        ev["warnings"].append("no UID — a content hash is standing in as the id")
    return ev


def parse_calendar(text: str) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """(calendar-level properties, every VEVENT) — the guts of parse_events.

    Component-aware on purpose. A VALARM lives inside a VEVENT and has its own
    TRIGGER/DESCRIPTION, and a VTIMEZONE has DTSTART lines of its own; a flat
    line scanner picks those up as event properties and produces events with the
    wrong start date. Only properties whose innermost component is VEVENT are
    collected.

    NC-370. A file can hold more than one VCALENDAR (two exports merged, a feed
    that concatenates them). Each event is stamped with the name, producer and
    METHOD of the calendar it sits in. The scanner used to keep one header with
    `setdefault`, so every event took the FIRST calendar's: the second calendar's
    name was lost, and so was its METHOD, which matters, because a cancellation
    in the second calendar was then read as an ordinary meeting. The header
    returned is still the first calendar's.
    """
    header: Dict[str, str] = {}
    this_cal = header           # the calendar the scanner is inside right now
    calendars = 0
    events: List[Dict[str, Any]] = []
    stack: List[str] = []
    cur: Optional[Dict[str, Any]] = None

    def close(error: str = "") -> None:
        nonlocal cur
        if cur is None:
            return
        built = _build_event(cur, this_cal)
        if error:
            built["parsed"] = False
            built["error"] = error
        events.append(built)
        cur = None

    for line in unfold(text):
        name, params, value = parse_content_line(line)
        if not name:
            if cur is not None:
                cur["raw"].append(line)
                cur["bad_lines"] += 1
            continue
        if name == "BEGIN":
            comp = value.strip().upper()
            if comp == "VEVENT":
                if cur is not None:
                    close("this event was never closed before the next event began")
                cur = {"props": [], "raw": [line], "bad_lines": 0}
            elif cur is not None:
                cur["raw"].append(line)
            if comp == "VCALENDAR":
                calendars += 1
                if calendars > 1:
                    if cur is not None:         # a calendar that never closed its event
                        close("this event was never closed before the next calendar began")
                    this_cal = {}
            stack.append(comp)
            continue
        if name == "END":
            comp = value.strip().upper()
            if cur is not None:
                cur["raw"].append(line)
            if stack and stack[-1] == comp:
                stack.pop()
            elif comp in stack:                 # unbalanced: unwind to it
                while stack and stack.pop() != comp:
                    pass
            if comp == "VEVENT":
                close()
            continue
        inner = stack[-1] if stack else ""
        if cur is not None:
            cur["raw"].append(line)
            if inner == "VEVENT":
                cur["props"].append((name, params, value))
            # else: inside a VALARM — ignored deliberately
        elif inner in ("VCALENDAR", ""):
            this_cal.setdefault(name, _unescape_text(value)
                                if name in TEXT_PROPS else value.strip())
    close("the file ends in the middle of this event")
    return header, events


def parse_events(text: str) -> List[Dict[str, Any]]:
    """Every VEVENT in the file, in file order, as dicts.

    Guaranteed keys include uid, summary, start (aware datetime), end, all_day,
    on_date, attendees, organizer, is_recurring, raw_tzid — see `_blank_event`
    for the full set.

    **Nothing is dropped.** An event this module could not read still appears,
    with `parsed=False`, an `error` sentence, and its raw lines in `raw`. That
    is the point: a `continue` that swallows a malformed VEVENT means the user is
    told "12 events imported" about a file with 13, and has no way to find the
    13th. `report()` turns those entries into a count plus a reason per event.
    """
    return parse_calendar(text)[1]


# ── Matching against crm.db (read-only) ────────────────────────────────────
def _self_identity() -> Tuple[frozenset, str]:
    """(own email addresses lowered, own name-norm) from SELF_EMAIL / SELF_NAME,
    read at call time — both are settings.MANAGED_KEYS entries the user can edit
    live. SELF_EMAIL may list several addresses, separated by commas, semicolons
    or spaces (NC-328): an invite sent to your other address is still you."""
    raw = (os.environ.get("SELF_EMAIL") or "").lower()
    return (frozenset(a for a in re.split(r"[,;\s]+", raw) if a),
            db.normalize_name(os.environ.get("SELF_NAME") or ""))


def _is_self(person: Dict[str, Any], self_emails: frozenset, self_norm: str) -> bool:
    if person["email"] and person["email"] in self_emails:
        return True
    return bool(self_norm) and db.normalize_name(person["name"]) == self_norm


def _roster(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Humans on the invite: every ATTENDEE, plus the ORGANIZER when they aren't
    already one.

    The organizer belongs in here. When someone else invites you to coffee their
    file often carries ORGANIZER: them and ATTENDEE: you and nobody else —
    ignoring ORGANIZER would make the one meeting that matters most look like a
    solo appointment.
    """
    people = [p for p in event.get("attendees") or []
              if p["cutype"] not in SKIP_CUTYPES]
    org = event.get("organizer")
    if org and org["cutype"] not in SKIP_CUTYPES:
        keys = {k for k in (_identity_key(p) for p in people) if k}
        okey = _identity_key(org)
        # No identity at all means we CANNOT know whether this is one of the
        # attendees, so include them. A nameless ORGANIZER line is a real person on
        # a real invite; dropping them loses someone, while keeping them at worst
        # counts one participant twice — and report() shows the count either way.
        if not okey or okey not in keys:
            people.append(org)
    return people


def _lookup(conn: Any, person: Dict[str, Any],
            exclude_norms: set) -> Tuple[str, Any, str]:
    """(outcome, contact_row_or_None, detail) for one person. Read-only.

    Uses only existing read helpers — `db.find_by_handle` for the email,
    `db.find_exact` for the name. Invariant 1: matching only, never creation,
    not even behind a flag. This module has no --create-new and should never get
    one; a calendar invite is not evidence that someone belongs in the network.
    """
    if person["email"]:
        row = db.find_by_handle(conn, person["email"])
        if row is not None:
            if db.normalize_name(row["name"]) in exclude_norms:
                return "excluded", row, "exclude_list"
            return "matched", row, "email"
    for variant in _name_variants(person["name"]):
        rows = db.find_exact(conn, variant)
        if not rows:
            continue
        if len(rows) > 1:
            ids = ",".join(str(r["id"]) for r in rows)
            return "ambiguous", None, f"{len(rows)} contacts share this name ({ids})"
        row = rows[0]
        if db.normalize_name(row["name"]) in exclude_norms:
            return "excluded", row, "exclude_list"
        # find_by_handle already filters `NOT c.excluded`, so an excluded contact
        # reached only by email reads as unmatched. The name pass is what catches
        # them and labels them honestly.
        if row["excluded"] or (("deleted" in row.keys()) and row["deleted"]):
            return "excluded", row, "contact excluded in the CRM"
        return "matched", row, "name" + ("" if variant == person["name"] else " (reordered)")
    return "unmatched", None, "nobody in the CRM with this email or name"


def match_attendees(conn: Any, event: Dict[str, Any], *,
                    max_attendees: int) -> Dict[str, Any]:
    """Resolve one event's people to contact ids. Read-only, never creates.

    Refusals, in order, and each of them ends up in `report()`:
      • the event never parsed, or its STATUS is CANCELLED
      • CUTYPE ROOM / RESOURCE / GROUP — furniture and mailing lists
      • PARTSTAT DECLINED — somebody who said no was not seen
      • you (SELF_EMAIL / SELF_NAME) — you are not a contact
      • anyone on `exclude_list.txt` (invariant 2)
      • more than `max_attendees` OTHER people on the invite: a 40-person
        all-hands is not a meeting with someone. You are not counted, and
        declined invitees are, because they show how big the meeting was.

    Returns both halves, always:
      {skipped, skip_reason, others_count, contact_ids,
       matched[], unmatched[], ignored[]}
    `max_attendees` is a required keyword with no default here on purpose — the
    default lives in `max_attendees_setting()`, which re-reads os.environ per
    call, so nothing in this module can pin a stale cap at import time.
    """
    out: Dict[str, Any] = {
        "uid": event.get("uid", ""), "summary": event.get("summary", ""),
        "on_date": event.get("on_date", ""), "skipped": False, "skip_reason": "",
        "others_count": 0, "max_attendees": max_attendees,
        "contact_ids": [], "matched": [], "unmatched": [], "ignored": [],
    }
    if not event.get("parsed", True):
        out.update(skipped=True, skip_reason="unparseable")
        return out
    # Two independent ways a calendar says "this is off", and only the first was
    # being read. STATUS:CANCELLED is per-event; METHOD:CANCEL is an iTIP header on
    # the whole file, and RFC 5546 §3.2.5 makes STATUS *optional* on a CANCEL
    # because the METHOD is the authoritative signal. So the .ics attached to a
    # "Meeting cancelled" email — which is precisely what lands in ~/Downloads,
    # where the picker text sends the user — was proposed as a meeting that
    # happened. Wrong in the worst direction: it writes a fact into the CRM about
    # a conversation that never took place.
    if event.get("status") == "CANCELLED" or event.get("method") == "CANCEL":
        out.update(skipped=True, skip_reason="cancelled")
        return out

    self_emails, self_norm = _self_identity()
    people = _roster(event)
    for p in event.get("attendees") or []:
        if p["cutype"] in SKIP_CUTYPES:
            out["ignored"].append({"email": p["email"], "name": p["name"],
                                   "reason": f"cutype {p['cutype']}"})
    org = event.get("organizer")
    if org and org["cutype"] in SKIP_CUTYPES:
        # NC-370. `_roster` already leaves a room that sent the invite out of the
        # people; the receipt has to say so too, as it does for a room that was
        # invited. Skipped when the same room is also in the attendee list, where
        # it was just counted.
        okey = _identity_key(org)
        listed = {_identity_key(p) for p in event.get("attendees") or []}
        if not okey or okey not in listed:
            out["ignored"].append({"email": org["email"], "name": org["name"],
                                   "reason": f"cutype {org['cutype']}"})
    others = [p for p in people if not _is_self(p, self_emails, self_norm)]
    out["others_count"] = len(others)
    for p in people:
        if _is_self(p, self_emails, self_norm):
            out["ignored"].append({"email": p["email"], "name": p["name"],
                                   "reason": "you"})

    if not others:
        out.update(skipped=True, skip_reason="no other people on the invite")
        return out
    if len(others) > max_attendees:
        # Deliberately no per-attendee work here: the event is refused whole, and
        # listing forty names is noise, not honesty. The count, the summary and
        # the cap are what a user needs to decide whether to raise the setting.
        out.update(skipped=True, skip_reason="too many attendees")
        return out

    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    considered = 0
    for p in others:
        if p["partstat"] in SKIP_PARTSTATS:
            out["ignored"].append({"email": p["email"], "name": p["name"],
                                   "reason": f"partstat {p['partstat']}"})
            continue
        considered += 1
        outcome, row, detail = _lookup(conn, p, exclude_norms)
        if outcome == "matched":
            out["matched"].append({
                "contact_id": row["id"], "name": row["name"],
                "email": p["email"], "cn": p["name"],
                "matched_by": detail, "partstat": p["partstat"],
                "role": p["kind"],
            })
            if row["id"] not in out["contact_ids"]:
                out["contact_ids"].append(row["id"])
        elif outcome == "excluded":
            out["ignored"].append({
                "email": p["email"], "name": p["name"] or (row["name"] if row else ""),
                "reason": detail,
            })
        else:
            out["unmatched"].append({"email": p["email"], "name": p["name"],
                                     "reason": detail})
    if not considered:
        out.update(skipped=True, skip_reason="everyone declined")
    elif not out["matched"] and not out["unmatched"]:
        out.update(skipped=True, skip_reason="every attendee is excluded")
    return out


# ── Recurrence policy ──────────────────────────────────────────────────────
RECURRENCE_POLICY = "first_occurrence_only"
RECURRENCE_SENTENCE = (
    "Repeating events were imported as their first date only — this app can store "
    "a yearly repeat and nothing else, so a weekly or monthly series is kept as "
    "one dated meeting rather than invented forward."
)


def recurrence_plan(event: Dict[str, Any]) -> Dict[str, Any]:
    """How one event's repetition is handled: {recurring, freq, treated_as, note}.

    `recurring` is the value `db.add_contact_date` should receive. It is 1 only
    for a plain yearly rule, because `db.next_occurrence` implements exactly one
    recurrence — annual. See the module docstring.
    """
    if not event.get("is_recurring"):
        return {"recurring": 0, "freq": "", "treated_as": "single date", "note": ""}
    parts = event.get("rrule_parts") or {}
    freq = (parts.get("FREQ") or "").upper()
    interval = parts.get("INTERVAL", "1").strip() or "1"
    bounded = bool(parts.get("COUNT") or parts.get("UNTIL"))
    if freq == "YEARLY" and interval == "1" and not bounded:
        return {"recurring": 1, "freq": freq, "treated_as": "annual repeat",
                "note": "yearly rule — stored as an annual date"}
    if freq == "YEARLY" and bounded:
        return {"recurring": 0, "freq": freq, "treated_as": "first date only",
                "note": "yearly but ends (COUNT/UNTIL); an annual flag would "
                        "outlive the series, so only the first date is kept"}
    return {"recurring": 0, "freq": freq or "unspecified",
            "treated_as": "first date only",
            "note": f"{(freq or 'unspecified').lower()} rule — the CRM stores only "
                    "annual repeats, so the first date is kept and the repetition "
                    "is not"}


# ── The honesty surface ────────────────────────────────────────────────────
# The last line of every preview. A run that writes (`--apply`) leaves it out of
# what it prints, because by then it is false (NC-370).
PREVIEW_SENTENCE = "Nothing has been written. This is a proposal."


def _n(count: int, singular: str, plural: str = "") -> str:
    """"1 event" / "3 events" — these sentences are shown to a person, and
    "1 events were skipped" reads like a bug even when the number is right."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _skip_bucket(reason: str) -> str:
    r = (reason or "").lower()
    if "too many" in r:
        return "too_many_attendees"
    if "cancel" in r:
        return "cancelled"
    if "unparseable" in r:
        return "unparseable"
    if "excluded" in r:
        return "all_excluded"
    if "declined" in r:
        return "all_declined"
    if "no other people" in r:
        return "no_other_people"
    return "other"


def days_covered(event: Dict[str, Any]) -> int:
    """How many calendar days an event runs for: 1 for a meeting, 3 for a trip.

    An all-day event ends the day AFTER its last day (RFC 5545 makes DTEND
    exclusive), so Monday to Wednesday is stored as Monday to Thursday and counts
    3. A timed event counts whole 24-hour blocks, so a dinner that runs past
    midnight is still 1 and a three-day conference with set hours is 3. An event
    with no readable end counts 1. NC-370: the importer keeps the first day only,
    and `report()` uses this to say when that left days out.
    """
    start, end = event.get("start"), event.get("end")
    if start is None or end is None or end <= start:
        return 1
    if event.get("all_day"):
        return max(1, (end.date() - start.date()).days)
    return max(1, int((end - start) / timedelta(days=1)))


def report(events: Any, matches: Any = None, skipped: Any = None) -> Dict[str, Any]:
    """Everything that happened, as numbers a caller can render — and a
    `sentences` list a human can read.

    Nothing may leave this pipeline without appearing here. The contract, once
    matching has run (the usual case):

      events_seen == events_matched + events_no_match + events_skipped

    An event that could not be read is ALREADY inside `events_skipped`, under
    the reason "unparseable", and `events_unparseable` is only a second view of
    those same events. Adding it to the sum counts each of them twice (NC-370:
    the docstring used to say to).

    When matching has NOT run (`matches` empty) nothing has been sorted yet. The
    unreadable events are then counted on their own, and every readable event is
    still unsorted, so `accounting.unexplained` is the number of readable events
    nobody has matched or skipped yet. That is the true answer, not an error.

    `accounting.unexplained` is this arithmetic, exposed. If a future change
    starts dropping events, this number goes non-zero and the receipt says so
    instead of the count quietly shrinking.

    `events`   list from parse_events (including any parsed=False entries)
    `matches`  list of match_attendees results, or None if matching hasn't run
    `skipped`  any extra skip records the caller collected — the `skipped`
               out-list from proposed_dates, typically. Accepts dicts with
               {uid, summary, reason} and an optional `bucket`. An event-level
               refusal here (a date outside the window) MOVES that event out of
               `matched_events`, because it matched somebody and still produced
               nothing; a row-level one (a duplicate row) is counted separately
               and leaves the event where it is. Without that distinction the
               same event would be counted twice and the accounting would lie.
    """
    events = list(events or [])
    matches = list(matches or [])
    extra = list(skipped or [])
    # Buckets that mean "this event yielded no row at all", as opposed to
    # "duplicate", which is about one row of an event that yielded others.
    event_level = {"out_of_window", "other"}

    unparseable = [{"uid": e.get("uid", ""), "summary": e.get("summary", ""),
                    "reason": e.get("error") or "unreadable",
                    "raw_first_line": (e.get("raw") or "").split("\n")[0][:120]}
                   for e in events if not e.get("parsed", True)]
    parsed = [e for e in events if e.get("parsed", True)]

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    matched_events: List[Dict[str, Any]] = []
    no_match: List[Dict[str, Any]] = []
    att_matched = att_unmatched = 0
    unmatched_people: List[Dict[str, Any]] = []
    ignored_reasons: Dict[str, int] = {}

    for m in matches:
        att_matched += len(m.get("matched") or [])
        att_unmatched += len(m.get("unmatched") or [])
        unmatched_people += list(m.get("unmatched") or [])
        for ig in m.get("ignored") or []:
            key = ig.get("reason", "unknown")
            ignored_reasons[key] = ignored_reasons.get(key, 0) + 1
        if m.get("skipped"):
            rec = {"uid": m.get("uid", ""), "summary": m.get("summary", ""),
                   "on_date": m.get("on_date", ""), "reason": m.get("skip_reason", ""),
                   "others_count": m.get("others_count", 0),
                   "max_attendees": m.get("max_attendees")}
            buckets.setdefault(_skip_bucket(m.get("skip_reason", "")), []).append(rec)
        elif m.get("contact_ids"):
            matched_events.append({"uid": m.get("uid", ""), "summary": m.get("summary", ""),
                                   "on_date": m.get("on_date", ""),
                                   "contact_ids": list(m.get("contact_ids") or [])})
        else:
            no_match.append({"uid": m.get("uid", ""), "summary": m.get("summary", ""),
                             "on_date": m.get("on_date", ""),
                             "reason": "attendees present, none of them are in the CRM"})

    duplicate_rows: List[Dict[str, Any]] = []
    for rec in extra:
        uid = rec.get("uid", "")
        reason = rec.get("reason") or rec.get("error") or "skipped"
        bucket = rec.get("bucket") or _skip_bucket(reason)
        entry = {"uid": uid, "summary": rec.get("summary", ""),
                 "on_date": rec.get("on_date", ""), "reason": reason}
        if bucket == "duplicate":
            duplicate_rows.append(entry)       # one row of an event, not the event
            continue
        if bucket in event_level and uid:
            # It matched somebody and still produced nothing: take it out of
            # matched_events so it is counted once, in the bucket that explains it.
            # Keyed on (uid, on_date), not uid alone — a modified instance of a
            # series carries the SAME UID as the series (RECURRENCE-ID), so a uid
            # match would drop the sibling event too.
            day = rec.get("on_date", "")
            matched_events = [m for m in matched_events
                              if not (m["uid"] == uid and m["on_date"] == day)]
        buckets.setdefault(bucket, []).append(entry)

    recurring = []
    for e in parsed:
        if not (e.get("is_recurring") or e.get("is_override")):
            continue
        plan_ = recurrence_plan(e)
        recurring.append({"uid": e.get("uid", ""), "summary": e.get("summary", ""),
                          "rrule": e.get("rrule", ""), "freq": plan_["freq"],
                          "treated_as": plan_["treated_as"], "note": plan_["note"],
                          "is_override": bool(e.get("is_override")),
                          "recurring_flag": plan_["recurring"]})
    annual = sum(1 for r in recurring if r["recurring_flag"] == 1)
    flattened = len(recurring) - annual
    rdates = sum(len(e.get("rdates") or []) for e in parsed)
    exdates = sum(len(e.get("exdates") or []) for e in parsed)
    floating = sum(1 for e in parsed if e.get("floating"))
    unknown_tz = sum(1 for e in parsed if e.get("raw_tzid") and not e.get("tzid"))
    synthetic = sum(1 for e in parsed if e.get("uid_synthetic"))
    # NC-370. Lines inside an event with no colon in them. The parser counts them
    # on the event ("records it rather than dropping it"); this is where the
    # person is told.
    bad_line_events = [{"uid": e.get("uid", ""), "summary": e.get("summary", ""),
                        "lines": int(e.get("unknown_lines") or 0)}
                       for e in events if e.get("unknown_lines")]
    bad_lines = sum(b["lines"] for b in bad_line_events)
    # NC-370. An event that runs over several days is saved on its first day only.
    # Counted only for the events that actually yield a date: a week of "Holiday"
    # with nobody else on it leaves nothing behind, so there is nothing to tell.
    # Needs matching to have run, since until then nothing has been sorted into
    # "yields a date" or not.
    matched_keys = {(m["uid"], m["on_date"]) for m in matched_events}
    multi_day = []
    for e in parsed:
        days = days_covered(e)
        if days >= 2 and (e.get("uid", ""), e.get("on_date", "")) in matched_keys:
            multi_day.append({"uid": e.get("uid", ""), "summary": e.get("summary", ""),
                              "on_date": e.get("on_date", ""), "days": days,
                              "all_day": bool(e.get("all_day"))})
    warnings = [{"uid": e.get("uid", ""), "warning": w}
                for e in events for w in (e.get("warnings") or [])]

    skipped_counts = {k: len(v) for k, v in sorted(buckets.items())}
    skipped_total = sum(skipped_counts.values())
    explained = len(matched_events) + len(no_match) + skipped_total
    # unparseable events reach `matches` as skip_reason "unparseable"; only count
    # them separately when matching never ran.
    if not matches:
        explained += len(unparseable)

    out: Dict[str, Any] = {
        "events_seen": len(events),
        "events_parsed": len(parsed),
        "events_unparseable": len(unparseable),
        "unparseable": unparseable,
        "events_matched": len(matched_events),
        "matched_events": matched_events,
        "events_no_match": len(no_match),
        "no_match_events": no_match,
        "events_skipped": skipped_total,
        "events_skipped_by_reason": skipped_counts,
        "skipped_detail": buckets,
        "events_skipped_too_many_attendees": skipped_counts.get("too_many_attendees", 0),
        "events_all_attendees_excluded": skipped_counts.get("all_excluded", 0),
        "duplicate_rows_folded": len(duplicate_rows),
        "duplicate_rows": duplicate_rows,
        "events_recurring": len(recurring),
        "recurrence_policy": RECURRENCE_POLICY,
        "recurring_kept_annual": annual,
        "recurring_flattened": flattened,
        "recurring_detail": recurring,
        "rdates_not_expanded": rdates,
        "exdates_not_applied": exdates,
        "attendees_matched": att_matched,
        "attendees_unmatched": att_unmatched,
        "unmatched_attendees": unmatched_people,
        "attendees_ignored": sum(ignored_reasons.values()),
        "attendees_ignored_by_reason": ignored_reasons,
        "floating_times": floating,
        "unknown_timezones": unknown_tz,
        "synthetic_uids": synthetic,
        "events_multi_day": len(multi_day),
        "multi_day_detail": multi_day,
        "unreadable_lines": bad_lines,
        "events_with_unreadable_lines": len(bad_line_events),
        "unreadable_line_detail": bad_line_events,
        "warnings": warnings,
        "accounting": {
            "events_seen": len(events), "explained": explained,
            "unexplained": len(events) - explained,
        },
        # About report() itself, which is read-only — not about the module, which
        # writes under --apply. A run that wrote puts its receipt in the separate
        # `applied` key; don't read this one to mean nothing happened.
        "wrote_anything": False,
    }

    s: List[str] = [f"{_n(len(events), 'calendar event')} read."]
    if unparseable:
        s.append(f"{_n(len(unparseable), 'event')} could not be read and "
                 f"{'was' if len(unparseable) == 1 else 'were'} skipped — each one is "
                 "listed with its reason, nothing was silently dropped.")
    if matches:
        s.append(f"{_n(len(matched_events), 'event')} matched somebody in your contacts "
                 f"({_n(att_matched, 'attendee match', 'attendee matches')}); "
                 f"{_n(len(no_match), 'event')} had people on the invite who aren't "
                 "in your contacts.")
    if skipped_counts.get("too_many_attendees"):
        # NC-370. `if r.get("max_attendees")` was falsy for a limit of 0, so the
        # sentence read "more than None other people". A limit of 0 is a number.
        cap = next((r.get("max_attendees") for r in buckets["too_many_attendees"]
                    if r.get("max_attendees") is not None), None)
        s.append(f"{_n(skipped_counts['too_many_attendees'], 'event')} skipped for having "
                 f"more than {cap if cap is not None else 'the allowed number of'} "
                 "other people on the invite. A big meeting is not a meeting "
                 "with someone.")
    if skipped_counts.get("all_excluded"):
        s.append(f"{_n(skipped_counts['all_excluded'], 'event')} involved only people on "
                 "your exclude list, so nothing was proposed for them.")
    if skipped_counts.get("no_other_people"):
        one = skipped_counts["no_other_people"] == 1
        s.append(f"{_n(skipped_counts['no_other_people'], 'event')} had nobody else on "
                 f"{'it' if one else 'them'} at all (personal reminders like \"Focus time\"), "
                 f"so there is nobody to attach {'it' if one else 'them'} to.")
    if skipped_counts.get("all_declined"):
        s.append(f"{_n(skipped_counts['all_declined'], 'event')} was declined by "
                 "everyone invited." if skipped_counts["all_declined"] == 1 else
                 f"{skipped_counts['all_declined']} events were declined by everyone invited.")
    if skipped_counts.get("cancelled"):
        s.append(f"{_n(skipped_counts['cancelled'], 'event')} "
                 f"{'was' if skipped_counts['cancelled'] == 1 else 'were'} cancelled "
                 "in the calendar.")
    if skipped_counts.get("out_of_window"):
        s.append(f"{_n(skipped_counts['out_of_window'], 'event')} fell outside the "
                 f"window of {lookback_days()} days back and {lookahead_days()} days "
                 "ahead, so no date was added for them.")
    if duplicate_rows:
        s.append(f"{_n(len(duplicate_rows), 'row')} folded together as a duplicate — "
                 "the same person, day and event text appeared more than once."
                 if len(duplicate_rows) == 1 else
                 f"{len(duplicate_rows)} rows folded together as duplicates — the same "
                 "person, day and event text appeared more than once.")
    if multi_day:
        one = len(multi_day) == 1
        s.append(f"{_n(len(multi_day), 'event')} ran for more than one day, such as a "
                 f"trip or a conference. {'It is' if one else 'Each is'} saved on its "
                 "first day only, because a meeting is stored with a single date.")
    if recurring:
        s.append(f"{_n(len(recurring), 'repeating event')}: {annual} stored as an annual "
                 f"date, {flattened} kept as a single date. " + RECURRENCE_SENTENCE)
    if rdates or exdates:
        s.append(f"{_n(rdates, 'extra date')} added to repeating events and "
                 f"{_n(exdates, 'date')} removed from them were read but not applied.")
    if unknown_tz:
        s.append(f"{_n(unknown_tz, 'event')} used a timezone name this Mac doesn't know; "
                 "the times were read as local wall-clock time.")
    if bad_lines:
        one_event = len(bad_line_events) == 1
        s.append(f"{_n(bad_lines, 'line')} in the file could not be read and "
                 f"{'was' if bad_lines == 1 else 'were'} skipped. "
                 f"{'The event' if one_event else 'The events'} around "
                 f"{'it' if bad_lines == 1 else 'them'} "
                 f"{'was' if one_event else 'were'} still read.")
    if att_unmatched:
        s.append(f"{_n(att_unmatched, 'person', 'people')} on your invites "
                 f"{'is' if att_unmatched == 1 else 'are'} not in your contacts. Nothing "
                 "was created for them: an import only adds dates to people you already "
                 "have.")
    s.append(PREVIEW_SENTENCE)
    out["sentences"] = s
    out["summary_text"] = " ".join(s)
    return out


# ── What would be written ──────────────────────────────────────────────────
def _label_for(event: Dict[str, Any], limit: int) -> Tuple[str, str]:
    """(attributed label, clean summary label) for an event.

    Deterministic — built only from stored event text, so `INSERT OR IGNORE`
    against UNIQUE(contact_id, kind, on_date, label) makes a re-import a no-op.
    Never call `api_dates._when_word` here: it renders "in 3 days" / "tomorrow",
    which would be stale by the following morning and would change the dedupe
    key every single day.
    """
    summary = " ".join((event.get("summary") or "").split()) or "Calendar event"
    if len(summary) > limit:
        summary = summary[:limit - 1].rstrip() + "…"
    # Both values are now the clean summary, and the second element is kept only so
    # every caller keeps working.
    #
    # This used to return "Coffee with Anna (calendar)" as the label. That suffix was
    # a workaround, and its own comment said so: contact_dates had no provenance
    # column and people.js hardcoded source:"added", so smuggling the origin into the
    # text a human reads was the only way to stop an inferred row passing as a typed
    # one. Both of those are fixed — contact_dates carries source/source_uid, and
    # people.js captions a calendar row "Calendar" — so the workaround is now just
    # noise in the label, and worse, it would be baked into the UNIQUE dedupe key.
    return summary, summary


def proposed_dates(conn: Any, events: Any, *, max_attendees: int,
                   skipped: Optional[List[Dict[str, Any]]] = None
                   ) -> List[Dict[str, Any]]:
    """What WOULD be written to `contact_dates`, as plain dicts. Writes nothing.

    Each dict is one prospective row plus its provenance:

        {kind: "meeting", contact_id, on_date, label, summary_label,
         recurring, year_known, source, confidence, dedupe_key, provenance{…}}

    This function proposes; it does not write. `apply()` is the writer, and it
    needs exactly one function — `db.add_contact_date(conn, contact_id, kind,
    on_date, label=…, year_known=…, recurring=…)` followed by `conn.commit()`.
    Keeping the two apart is what lets `--dry-run` show the refusals below before
    anything becomes durable. `provenance` is the evidence for each row; only its
    event UID is stored (as `source_uid`), the rest travels with the preview.

    `skipped` is an optional out-list. Everything this function declines —
    out-of-window dates, events with no match, duplicate rows — is appended to
    it so `report()` can state it. Pass one in; a refusal nobody can see is the
    bug this codebase has fixed before.
    """
    sink = skipped if skipped is not None else []
    back, forward = lookback_days(), lookahead_days()
    today = _today()
    earliest, latest = today - timedelta(days=back), today + timedelta(days=forward)
    limit = label_max()

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for event in list(events or []):
        match = match_attendees(conn, event, max_attendees=max_attendees)
        if match["skipped"] or not match["contact_ids"]:
            continue        # already visible through report(events, matches)
        on_date = event.get("on_date") or ""
        try:
            day = _date(*(int(x) for x in on_date.split("-")))
        except (TypeError, ValueError):
            sink.append({"uid": event.get("uid", ""), "summary": event.get("summary", ""),
                         "on_date": on_date, "bucket": "other",
                         "reason": f"unusable date {on_date!r}"})
            continue
        if not (earliest <= day <= latest):
            sink.append({"uid": event.get("uid", ""), "summary": event.get("summary", ""),
                         "on_date": on_date, "bucket": "out_of_window",
                         "reason": f"outside the window of {back} days back and "
                                   f"{forward} days ahead"})
            continue
        label, summary_label = _label_for(event, limit)
        plan_ = recurrence_plan(event)
        for m in match["matched"]:
            key = (m["contact_id"], on_date, label)
            if key in seen:
                sink.append({"uid": event.get("uid", ""), "summary": event.get("summary", ""),
                             "on_date": on_date, "bucket": "duplicate",
                             "reason": f"same date and label already proposed for "
                                       f"contact {m['contact_id']}"})
                continue
            seen.add(key)
            out.append({
                "kind": DATE_KIND,
                "contact_id": m["contact_id"],
                "contact_name": m["name"],
                "on_date": on_date,
                "label": label,                  # the event's summary, trimmed
                "summary_label": summary_label,  # the same text; kept for old callers
                "recurring": plan_["recurring"],
                "year_known": 1,                 # an .ics always carries a real year
                "source": PROVENANCE_SOURCE,     # NOT "added" — this is not hand-entered
                "confidence": m["matched_by"],
                "dedupe_key": f"{PROVENANCE_SOURCE}:{event.get('uid', '')}:{m['contact_id']}",
                "provenance": {
                    "source": PROVENANCE_SOURCE,
                    "event_uid": event.get("uid", ""),
                    "uid_synthetic": bool(event.get("uid_synthetic")),
                    "calendar": event.get("calendar", ""),
                    "prodid": event.get("prodid", ""),
                    "summary": event.get("summary", ""),
                    "location": event.get("location", ""),
                    "start": event["start"].isoformat() if event.get("start") else "",
                    "all_day": bool(event.get("all_day")),
                    "tzid": event.get("tzid", ""),
                    "raw_tzid": event.get("raw_tzid", ""),
                    "floating": bool(event.get("floating")),
                    "rrule": event.get("rrule", ""),
                    "recurrence": plan_["treated_as"],
                    "matched_by": m["matched_by"],
                    "matched_email": m["email"],
                    "matched_cn": m["cn"],
                    "verified": False,   # a calendar invite is evidence, not a fact
                },
            })
    return out


def plan(conn: Any, text: str, *, max_attendees: Optional[int] = None) -> Dict[str, Any]:
    """The whole read-only pipeline in one call: parse → match → propose → report.

    `max_attendees` defaults to `max_attendees_setting()`, read here rather than
    captured at import so a Settings change applies to the next run instead of
    the next restart (the `api_import.import_timeout_sec` idiom).

    Returns {events, matches, proposals, skipped, report}. Writes nothing.
    """
    cap = max_attendees_setting() if max_attendees is None else max_attendees
    events = parse_events(text)
    matches = [match_attendees(conn, e, max_attendees=cap) for e in events]
    sink: List[Dict[str, Any]] = []
    proposals = proposed_dates(conn, events, max_attendees=cap, skipped=sink)
    rep = report(events, matches, sink)
    rep["proposals"] = len(proposals)
    rep["contacts_touched"] = len({p["contact_id"] for p in proposals})
    rep["proposal_sentence"] = (
        f"{_n(len(proposals), 'date')} would be added across "
        f"{_n(rep['contacts_touched'], 'person', 'people')}.")
    rep["sentences"].insert(1, rep["proposal_sentence"])
    rep["summary_text"] = " ".join(rep["sentences"])
    return {"events": events, "matches": matches, "proposals": proposals,
            "skipped": sink, "report": rep}


def apply(conn: Any, planned: Dict[str, Any]) -> Dict[str, Any]:
    """Write the proposals from plan(). Returns a receipt of what ACTUALLY happened.

    Separate from plan() on purpose: everything above this line is read-only, so a
    preview can never write by accident, and the write is one short function that
    is easy to audit.

    The label written is `summary_label`, the CLEAN one — not the attributed
    "Coffee with Anna (calendar)" form. That attribution only existed because
    contact_dates had no way to record where a row came from, so the origin had to
    be smuggled into the text a human reads. There is a `source` column now, so the
    provenance lives in the schema where the UI can style it, and the label goes
    back to being just the label.

    Every verdict db.upsert_calendar_date can return is counted, including
    "refused_manual" — an import that declines to overwrite something the owner
    typed has done the right thing, and they should be told rather than left to
    wonder why a row they expected did not appear.
    """
    tally = {"created": 0, "moved": 0, "unchanged": 0, "refused_manual": 0, "failed": 0}
    touched: set = set()
    failures: List[Dict[str, Any]] = []

    for row in planned.get("proposals") or []:
        uid = (row.get("provenance") or {}).get("event_uid") or ""
        if not uid:
            # No UID means no way to reconcile this row on a later run, so writing it
            # would guarantee a duplicate next import. Refuse, and say so.
            tally["failed"] += 1
            failures.append({"contact_id": row.get("contact_id"),
                             "on_date": row.get("on_date"),
                             "reason": "no event UID — cannot be reconciled on a re-import"})
            continue
        try:
            verdict = db.upsert_calendar_date(
                conn, int(row["contact_id"]), row["on_date"],
                source_uid=uid,
                label=row.get("summary_label") or row.get("label") or "",
                recurring=int(row.get("recurring") or 0))
        except Exception as exc:   # noqa: BLE001 — one bad row must not lose the batch
            tally["failed"] += 1
            failures.append({"contact_id": row.get("contact_id"),
                             "on_date": row.get("on_date"),
                             "reason": f"{type(exc).__name__}: {exc}"})
            continue
        tally[verdict] = tally.get(verdict, 0) + 1
        if verdict in ("created", "moved"):
            touched.add(int(row["contact_id"]))

    conn.commit()

    receipt = dict(tally)
    receipt["contacts_touched"] = len(touched)
    receipt["failures"] = failures
    # Reports what happened, not what was asked for. Those differ whenever a manual
    # row was protected or a row had no UID, and the difference is the interesting
    # part — a receipt that echoes the request is a receipt that cannot be trusted.
    receipt["proposed"] = len(planned.get("proposals") or [])
    receipt["sentences"] = _apply_sentences(receipt)
    receipt["summary_text"] = " ".join(receipt["sentences"])
    return receipt


def _apply_sentences(r: Dict[str, Any]) -> List[str]:
    """Plain sentences for the receipt: active voice, one fact each."""
    out = [f"{_n(r['created'], 'meeting')} added."]
    if r["moved"]:
        out.append(f"{_n(r['moved'], 'meeting')} moved to a new date.")
    if r["unchanged"]:
        out.append(f"{_n(r['unchanged'], 'meeting')} already matched and stayed as it was.")
    if r["refused_manual"]:
        out.append(f"{_n(r['refused_manual'], 'meeting')} kept your own version. "
                   "The import does not overwrite a date you entered.")
    if r["failed"]:
        out.append(f"{_n(r['failed'], 'meeting')} could not be written. "
                   "Each one is listed with its reason.")
    return out


# ── CLI: preview by default, --apply to write ──────────────────────────────
def main() -> None:
    """`python3 ics_import.py <file.ics>` — prints the report and the proposed
    rows. Add --apply to write them; without it nothing is written, which is the
    same --dry-run-by-default shape every other importer here has."""
    import argparse
    import json
    import sqlite3

    ap = argparse.ArgumentParser(
        description="Preview a calendar import; --apply to write it")
    ap.add_argument("path", help="an .ics file you exported or downloaded")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    # Preview is the default and --apply is the opt-in, not the reverse. Every
    # other importer here treats writing as the thing you ask for.
    ap.add_argument("--apply", action="store_true",
                    help="write the proposed meetings (default: preview only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-write; the default, accepted for symmetry "
                         "with the other importers")
    low, high = MAX_ATTENDEES_RANGE

    def attendee_limit(raw: str) -> int:
        # The same range as the setting. A limit of 0 would skip every event, so it
        # is refused with a sentence rather than quietly changed or obeyed.
        try:
            n = int(raw)
        except ValueError:
            n = None
        if n is None or not low <= n <= high:
            raise argparse.ArgumentTypeError(
                f"it must be a whole number from {low} to {high}. A limit of 0 "
                "would skip every event.")
        return n

    ap.add_argument("--max-attendees", type=attendee_limit, default=None,
                    help=f"other people per invite, {low} to {high} (default "
                         f"{max_attendees_setting()}, NC_ICS_MAX_ATTENDEES)")
    args = ap.parse_args()

    with open(args.path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    # ensure_columns() only when we intend to write: the source/source_uid columns
    # must exist before apply(), and a preview must not migrate anything.
    write = bool(args.apply) and not args.dry_run
    conn = db.get_db()
    try:
        if write:
            db.ensure_columns(conn)
        result = plan(conn, text, max_attendees=args.max_attendees)
        if write:
            result["applied"] = apply(conn, result)
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"Can't read crm.db ({exc}). Launch the app once so the "
                         "database exists, then re-run this preview.")
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"report": result["report"], "proposals": result["proposals"],
                          "applied": result.get("applied")}, indent=2, default=str))
        return
    rep = result["report"]
    for line in rep["sentences"]:
        # A run that wrote has a receipt of its own, below. The preview's "would be
        # added" and "Nothing has been written" are no longer true of it, and
        # printing them next to "3 meetings added" made the log contradict itself.
        if write and line in (rep.get("proposal_sentence"), PREVIEW_SENTENCE):
            continue
        print(f"  {line}")
    if result.get("applied"):
        print()
        for line in result["applied"]["sentences"]:
            print(f"  {line}")
    print()
    for p in result["proposals"]:
        print(f"  + {p['on_date']}  {p['contact_name']}  «{p['label']}»  "
              f"[matched by {p['confidence']}"
              f"{', annual' if p['recurring'] else ''}]")
    for bucket, rows in sorted(rep["skipped_detail"].items()):
        if bucket == "unparseable":
            continue          # printed below with its actual reason, not twice
        for r in rows:
            print(f"  - skipped: {r.get('summary') or r.get('uid')} — {r['reason']}")
    for u in rep["unparseable"]:
        print(f"  ! unreadable: {u['summary'] or u['uid'] or '(no summary)'} — {u['reason']}")
    if rep["accounting"]["unexplained"]:
        print(f"\n  The summary does not account for "
              f"{_n(abs(rep['accounting']['unexplained']), 'event')}. That is a fault "
              "in this app, not in your file.")
    if not write:
        print("\n  Nothing was written.")


if __name__ == "__main__":
    main()
