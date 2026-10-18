"""Apple Calendar's own store → contact_dates, with nothing to paste (NC-042).

The calendar row used to ask you to go into Google or iCloud, find the secret
iCal address, paste it into Settings and turn on a switch that defaults to off.
Four manual steps to fetch *over the internet* data already sitting on this Mac.
By NC-036's standard the link is a defect.

**This module parses nothing.** Everything after "read the rows" is
`crm/ics_import.py`: attendee matching against crm.db, the self-identity test,
the attendee cap, the timezone handling, the provenance rules, the write. That
module is ~1,400 lines with about a hundred tests on it, and a second
implementation of "which contact is this attendee" is exactly the kind of
duplication that drifts into two different answers. So this file's whole job is
SQLite rows → the dict shape `ics_import._blank_event()` documents, and its tests
are about that translation and nothing else.

WHY NOT ~/Library/Calendars, which is what NC-042 originally proposed: it does
not exist on macOS Ventura or later. That is the classic location; the store
moved into a group container. An absence is only evidence about the path you
named.

WHY THE HELPER'S COPY and not the container directly: invariant 6. The app never
holds Full Disk Access. `packaging/src/imessage_sync.swift` copies the store into
`~/Library/Application Support/NetworkCRM/calendar/` the same way it does
iMessage, WhatsApp and Contacts, and this reads the copy. Read-only, via a
`file:...?mode=ro` URI, because the file is a live database snapshot and this
process has no business writing to it even by accident.

DATES are Core Data timestamps: seconds since 2001-01-01 UTC, so +978307200 puts
them on the Unix epoch. A store can hold dates from far in the past to far in
the future: the far end is ordinary recurring-event bookkeeping, and the year
1604 is a sentinel in rows that never had a real date. Both are outside any
window worth importing, which is why the window is a WHERE clause and not a
nicety.

THE ENUMS are EventKit's, and the values stores hold confirm it:
Participant.type 1=person/3=resource,
status 1=pending 2=accepted 3=declined, role 1=required 3=chair,
CalendarItem.status 1=confirmed 3=cancelled. They are mapped to their iCalendar
spellings so ics_import sees exactly what it would see from a file.

Unrecognised enum values **fail open** — an unknown `type` becomes INDIVIDUAL
rather than being skipped. That direction is deliberate and it matches
ics_import's own stance that nothing is dropped silently: including a stranger
costs a row that matches no contact and disappears, while excluding a real person
loses a meeting with no trace of having done so. Many participants have type 0,
"unknown", so this is the common case and not an edge one.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config  # noqa: F401  — .env side effect
import ics_import

# Seconds between 2001-01-01 (Core Data) and 1970-01-01 (Unix).
CORE_DATA_EPOCH_OFFSET = 978_307_200

DEFAULT_DB = os.path.join(
    os.path.expanduser("~/Library/Application Support/NetworkCRM"),
    "calendar", "Calendar.sqlitedb")

# EventKit → iCalendar. Anything absent from these falls back to the neutral
# value, which is the "fail open" decision in the module docstring.
CUTYPE_BY_TYPE = {2: "ROOM", 3: "RESOURCE", 4: "GROUP"}
PARTSTAT_BY_STATUS = {2: "ACCEPTED", 3: "DECLINED", 4: "TENTATIVE",
                      5: "DELEGATED", 6: "COMPLETED", 7: "IN-PROCESS"}
ROLE_BY_ROLE = {2: "OPT-PARTICIPANT", 3: "CHAIR", 4: "NON-PARTICIPANT"}
# EKEventStatus. Only CANCELLED changes what ics_import does with an event, but
# the others are carried through so `report()` can say what it saw.
STATUS_BY_STATUS = {1: "CONFIRMED", 2: "TENTATIVE", 3: "CANCELLED"}


def _own_addresses(conn: Any) -> set:
    """Every address that is the account owner, learned from the store itself.

    The `is_self` flag alone is not enough. A calendar with several accounts
    can flag `is_self = 1` rows under several email domains, and SELF_EMAIL is
    only one of them — so ics_import's self-filter, which tests that single
    setting, cannot recognise the others. Every invite sent to those would be
    matched BY EMAIL to the owner's own contact record: the proposals would be
    meetings between the owner and themselves, dated and labelled on their own
    contact page.

    Dropping flagged rows does not fix it either, because **the same address
    appears flagged on some events and unflagged on others** — Apple only marks
    "you" on events from an account that knows your identity. A whole run of
    events can come from one address that is flagged elsewhere in the very same
    table.

    So the flag is read as evidence about the ADDRESS rather than about the row:
    an address that is ever the owner is always the owner. That needs no list of
    the user's addresses to be invented, kept up to date, or asked for — the
    calendar already knows all of them, which is more than SELF_EMAIL can.

    Degrades correctly: on a store with nothing flagged this is empty, nothing is
    dropped, and ics_import's SELF_EMAIL test still applies as before.
    """
    rows = conn.execute(
        """SELECT DISTINCT lower(trim(email)) AS e FROM Participant
            WHERE is_self = 1 AND email LIKE '%@%'"""
    ).fetchall()
    return {r["e"] for r in rows if r["e"]}


def _dt(core_data_seconds: Optional[float]) -> Optional[datetime]:
    """Core Data timestamp → aware UTC datetime, or None."""
    if core_data_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(
            float(core_data_seconds) + CORE_DATA_EPOCH_OFFSET, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _is_owner(row: Any, own: set) -> bool:
    """Is this participant the account owner? The flag on this row, OR the
    address appearing on any flagged row — see `_own_addresses`."""
    if row["is_self"]:
        return True
    email = (row["email"] or "").strip().lower()
    return bool(email) and email in own


def _person(row: Any, kind: str) -> Dict[str, Any]:
    """One Participant row → the dict `ics_import.match_attendees` consumes.

    Keys and defaults mirror `ics_import._person` exactly. They are not imported
    from it because that function's inputs are iCalendar parameter lists, which is
    the one part of that module this file does not have — but the SHAPE has to
    agree, and `tests/test_calendar_local.py` asserts key-for-key that it does.
    """
    email = (row["email"] or "").strip()
    return {
        "email": email.lower(),
        "email_raw": email,
        # The store keeps the display name on Identity, not Participant, so a
        # participant with no email and no joined identity is anonymous. That is
        # honest rather than lossy: ics_import's _identity_key already treats
        # "no identity at all" as unmatchable and reports it.
        "name": (row["display_name"] or "").strip() if "display_name" in row.keys() else "",
        "partstat": PARTSTAT_BY_STATUS.get(row["status"], "NEEDS-ACTION"),
        "cutype": CUTYPE_BY_TYPE.get(row["type"], "INDIVIDUAL"),
        "role": ROLE_BY_ROLE.get(row["role"], "REQ-PARTICIPANT"),
        "rsvp": False,
        "kind": kind,
        "prop": "ATTENDEE" if kind == "attendee" else "ORGANIZER",
        "raw": email,
    }


def read_events(path: str = "", *, back_days: Optional[int] = None,
                forward_days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every dated event in the window, as `ics_import._blank_event()` dicts.

    The window comes from the same two settings the .ics path uses
    (`NC_ICS_BACK_DAYS` / `NC_ICS_FORWARD_DAYS`), read at call time so a change
    applies to the next run rather than the next restart.
    """
    path = path or DEFAULT_DB
    back = ics_import.lookback_days() if back_days is None else back_days
    fwd = ics_import.lookahead_days() if forward_days is None else forward_days
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(days=back)).timestamp() - CORE_DATA_EPOCH_OFFSET
    hi = (now + timedelta(days=fwd)).timestamp() - CORE_DATA_EPOCH_OFFSET

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        own = _own_addresses(conn)
        items = conn.execute(
            """SELECT ci.ROWID AS rowid, ci.summary, ci.start_date, ci.end_date,
                      ci.all_day, ci.status, ci.UUID, ci.unique_identifier,
                      ci.organizer_id, ci.start_tz,
                      cal.title AS calendar_title,
                      (SELECT COUNT(*) FROM Recurrence r
                        WHERE r.owner_id = ci.ROWID) AS recurrences
                 FROM CalendarItem ci
                 LEFT JOIN Calendar cal ON cal.ROWID = ci.calendar_id
                WHERE ci.start_date IS NOT NULL
                  AND ci.start_date BETWEEN ? AND ?
                ORDER BY ci.start_date""",
            (lo, hi),
        ).fetchall()

        # One query for every participant in the window rather than one per
        # event: a window holds hundreds of events with attendees, and a query
        # each would be hundreds of round trips on a file the helper may replace
        # mid-run.
        people: Dict[int, List[Any]] = {}
        by_rowid: Dict[int, Any] = {}
        if items:
            marks = ",".join("?" * len(items))
            ids = [r["rowid"] for r in items]
            for p in conn.execute(
                f"""SELECT p.ROWID AS rowid, p.owner_id, p.email, p.status,
                           p.type, p.role, p.is_self, i.display_name
                      FROM Participant p
                      LEFT JOIN Identity i ON i.ROWID = p.identity_id
                     WHERE p.owner_id IN ({marks})""", ids):
                people.setdefault(p["owner_id"], []).append(p)
                by_rowid[p["rowid"]] = p
            # Organizers are Participant rows too, and an organizer is often NOT
            # in the attendee list — ics_import._roster exists precisely because
            # "they invited you and nobody else" is the meeting that matters most.
            org_ids = [r["organizer_id"] for r in items
                       if r["organizer_id"] and r["organizer_id"] not in by_rowid]
            if org_ids:
                marks = ",".join("?" * len(org_ids))
                for p in conn.execute(
                    f"""SELECT p.ROWID AS rowid, p.owner_id, p.email, p.status,
                               p.type, p.role, p.is_self, i.display_name
                          FROM Participant p
                          LEFT JOIN Identity i ON i.ROWID = p.identity_id
                         WHERE p.ROWID IN ({marks})""", org_ids):
                    by_rowid[p["rowid"]] = p
    finally:
        conn.close()

    events: List[Dict[str, Any]] = []
    for row in items:
        ev = ics_import._blank_event()
        start, end = _dt(row["start_date"]), _dt(row["end_date"])
        uid = (row["UUID"] or row["unique_identifier"] or "").strip()
        ev.update({
            # A stable uid is what makes re-importing idempotent, so a
            # synthesised one is flagged rather than passed off as real.
            "uid": uid or f"networkcrm-local-{row['rowid']}",
            "uid_synthetic": not uid,
            "summary": (row["summary"] or "").strip(),
            "start": start,
            "end": end,
            "all_day": bool(row["all_day"]),
            "on_date": start.strftime("%Y-%m-%d") if start else "",
            "status": STATUS_BY_STATUS.get(row["status"], ""),
            "tzid": (row["start_tz"] or ""),
            "raw_tzid": (row["start_tz"] or ""),
            "calendar": (row["calendar_title"] or "").strip(),
            "is_recurring": bool(row["recurrences"]),
            # is_self participants are dropped here, and this is the single most
            # load-bearing line in the file. ics_import has its own self-filter,
            # but it tests against SELF_EMAIL — ONE address — and a calendar
            # knows every address you are invited at. A calendar with several
            # accounts can have several domains on is_self rows, of which
            # SELF_EMAIL is only one. So invites sent to the others would not be
            # recognised as the owner, would be matched by email to the owner's
            # OWN contact record, and would produce bogus proposals: "meetings"
            # between the owner and themselves, on their own contact page, dated
            # and labelled.
            #
            # Apple already answers the question, so the answer is not guessed at
            # here and no second list of the user's addresses is invented. This
            # also degrades correctly: on a store where nothing is flagged,
            # nothing is dropped and ics_import's SELF_EMAIL test still applies.
            "attendees": [_person(p, "attendee")
                          for p in people.get(row["rowid"], [])
                          if not _is_owner(p, own)],
            "prodid": "Apple Calendar (local store)",
        })
        # Same test for the organizer. A meeting you organised has you as the
        # organizer, and ics_import._roster deliberately folds the organizer in
        # with the attendees — so leaving this out would put every meeting the
        # user called back onto their own contact record by the other route.
        org = by_rowid.get(row["organizer_id"]) if row["organizer_id"] else None
        if org is not None and not _is_owner(org, own):
            ev["organizer"] = _person(org, "organizer")
        events.append(ev)
    return events


def plan(conn: Any, path: str = "", *,
         max_attendees: Optional[int] = None) -> Dict[str, Any]:
    """Read-only: rows → events → matches → proposals → report.

    Deliberately the same shape and the same stage names as `ics_import.plan`,
    because `api_import` and the Data page's receipts render one of them without
    caring which produced it.
    """
    cap = (ics_import.max_attendees_setting()
           if max_attendees is None else max_attendees)
    events = read_events(path)
    matches = [ics_import.match_attendees(conn, e, max_attendees=cap)
               for e in events]
    skipped: List[Dict[str, Any]] = []
    proposals = ics_import.proposed_dates(conn, events, max_attendees=cap,
                                          skipped=skipped)
    rep = ics_import.report(events, matches, skipped)
    return {"events": events, "matches": matches, "proposals": proposals,
            "skipped": skipped, "report": rep}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="", help=f"default: {DEFAULT_DB}")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="show, change nothing")
    g.add_argument("--apply", action="store_true", help="write the proposals")
    args = ap.parse_args()

    path = args.db or DEFAULT_DB
    if not os.path.exists(path):
        # The actionable half matters more than the diagnosis: this file is
        # written by the sync helper, so its absence is a permission story and
        # never something the user can fix by finding a file themselves.
        print(f"No calendar copy at {path}.\n"
              "The sync helper writes it, and it needs Full Disk Access granted "
              "to that helper — see the Data page.", file=sys.stderr)
        raise SystemExit(2)

    import db as crm_db
    conn = crm_db.get_db()
    try:
        planned = plan(conn, path)
        rep = planned["report"]
        matched = rep.get('matched_events') or []
        print(f"{len(planned['events'])} events in window, "
              f"{len(matched) if isinstance(matched, list) else matched} "
              f"with a known contact, "
              f"{len(planned['proposals'])} dates proposed")
        if args.apply:
            receipt = ics_import.apply(conn, planned)
            print(" ".join(f"{k}={v}" for k, v in sorted(receipt.items())
                           if isinstance(v, int)))
        else:
            print("(dry run — nothing written)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
