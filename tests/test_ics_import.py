"""Calendar (.ics) import: RFC 5545 parsing, matching that never creates, and the report.

Every fixture is synthetic and inline. No real calendar file is read and no network
call is made — the module under test takes `text` and a connection, which is what
makes that possible (invariant 6: this module is the route that needs no
permission; reading the OS calendar is the sync helper's job, not this one's).

Two things every DB-backed test asserts for free, through `IcsBase.plan()`:
  • the contact count and the `contact_dates` count are identical afterwards —
    invariant 1 (never create) and "plan() never writes", together. The module
    DOES write, in apply(), under --apply only — everything this suite drives is
    the read-only half;
  • `report()["accounting"]["unexplained"] == 0` — every event read is accounted
    for as matched, no-match, or skipped-with-a-reason. A silent drop shows up
    here as a non-zero number instead of a quietly shrinking count.

Four silent failures of crm/ics_import.py each keep an assertion behind them in
`TestRegressions`, because every one of the losses was silent:
  • an all-day event carrying a resolvable TZID was reported as an unknown zone;
  • an attendee with no email and no CN was dropped inside the parser;
  • the `anon:` guard that handles the previous case was unreachable, so two
    attendees with an EMPTY CAL-ADDRESS still collapsed — and since PARTSTAT
    travels with the survivor, file order decided whether the event was a
    meeting or "everyone declined";
  • a cancellation (METHOD:CANCEL, no per-event STATUS) was proposed as a meeting.
Ten further known limitations were listed at the bottom of this file as notes. NC-370
(2026-10-17) fixed nine of them and tested each in `TestTheReceiptTellsTheWholeStory`;
what is still true, with the reason it stays, is listed at the bottom.
"""

import os, sys  # noqa: E401 — the bootstrap shim below must run before any crm import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

import contextlib  # noqa: E402
import datetime  # noqa: E402
import io  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

import ics_import as ics  # noqa: E402


CRLF = "\r\n"
ANNA = "ATTENDEE;CN=Anna Nilsson;PARTSTAT=ACCEPTED:mailto:anna@example.com"
ME = "ORGANIZER;CN=Sam Berg:mailto:me@example.com"


def vevent(*lines: str) -> str:
    """A VEVENT block from bare content lines, CRLF-terminated like a real file."""
    return "BEGIN:VEVENT" + CRLF + "".join(ln + CRLF for ln in lines) + "END:VEVENT" + CRLF


def cal(*blocks: str, prodid: str = "-//Test//Network CRM//EN", calname: str = "Work") -> str:
    return ("BEGIN:VCALENDAR" + CRLF + "VERSION:2.0" + CRLF + f"PRODID:{prodid}" + CRLF
            + f"X-WR-CALNAME:{calname}" + CRLF + "".join(blocks) + "END:VCALENDAR" + CRLF)


def _utc_today() -> datetime.date:
    """The module compares proposals against UTC today (`ics._today`), so every
    relative fixture date in this file has to be built off the same clock."""
    return datetime.datetime.now(timezone.utc).date()


def stamp(days: int = 0) -> str:
    """'YYYYMMDD' for an .ics value, `days` from UTC today."""
    return (_utc_today() + timedelta(days=days)).strftime("%Y%m%d")


def iso(days: int = 0) -> str:
    return (_utc_today() + timedelta(days=days)).isoformat()


def summaries(events):
    return [e["summary"] for e in events]


# ─────────────────────────────────────────────────────────────────────────────
# Pure parsing — no database, no settings
# ─────────────────────────────────────────────────────────────────────────────
class TestUnfold(unittest.TestCase):
    """RFC 5545 folding. Getting this wrong corrupts names, so it is tested on
    its own before anything that depends on it."""

    def test_line_endings_and_bom(self):
        for body in ("A:1\r\nB:2\r\n", "A:1\nB:2\n", "A:1\rB:2\r", "﻿A:1\r\nB:2"):
            self.assertEqual(ics.unfold(body), ["A:1", "B:2"], repr(body))

    def test_blank_lines_and_empty_input(self):
        self.assertEqual(ics.unfold(""), [])
        self.assertEqual(ics.unfold("\r\n\r\n"), [])
        self.assertEqual(ics.unfold("A:1\r\n\r\nB:2\r\n"), ["A:1", "B:2"])

    def test_fold_removes_exactly_one_character_and_joins_with_nothing(self):
        # Google folds at 75 octets, which lands mid-word. Inserting anything —
        # even a space — corrupts the value.
        self.assertEqual(ics.unfold("SUMMARY:Kaffe med Bö\r\n rje"),
                         ["SUMMARY:Kaffe med Börje"])
        self.assertEqual(ics.unfold("SUMMARY:Kaffe med Bö\r\n\trje"),
                         ["SUMMARY:Kaffe med Börje"])
        # a continuation whose own first character is a space keeps that space
        self.assertEqual(ics.unfold("SUMMARY:Coffee\r\n  with Anna"),
                         ["SUMMARY:Coffee with Anna"])

    def test_a_word_split_across_a_fold_survives_into_the_summary(self):
        ev = ics.parse_events(cal(vevent(
            "UID:fold-1", f"DTSTART:{stamp(3)}T090000Z",
            "SUMMARY:Kaffe med Bö" + CRLF + " rje Söderström", ANNA)))[0]
        self.assertEqual(ev["summary"], "Kaffe med Börje Söderström")

    def test_fold_inside_a_quoted_parameter_value(self):
        # The fold can land anywhere, including between the comma and the space
        # of a quoted CN. Unfolding must happen before the params are split.
        ev = ics.parse_events(cal(vevent(
            "UID:fold-2", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Fika",
            'ATTENDEE;CN="Nilsson,' + CRLF + '  Anna":mailto:anna@example.com')))[0]
        self.assertEqual(ev["attendees"][0]["name"], "Nilsson, Anna")
        self.assertEqual(ev["attendees"][0]["email"], "anna@example.com")

    def test_an_attendee_line_folded_at_every_75_octets_google_style(self):
        line = ("ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=TRUE"
                ";CN=Anna Nilsson;X-NUM-GUESTS=0:mailto:anna@example.com")
        folded = line[:60] + CRLF + " " + line[60:100] + CRLF + " " + line[100:]
        ev = ics.parse_events(cal(vevent(
            "UID:fold-3", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Folded roster",
            folded)))[0]
        self.assertEqual([(a["name"], a["email"], a["partstat"], a["rsvp"])
                          for a in ev["attendees"]],
                         [("Anna Nilsson", "anna@example.com", "ACCEPTED", True)])

    def test_orphan_continuation_becomes_its_own_line(self):
        # A file that begins mid-fold (a truncated download) must not lose the text.
        self.assertEqual(ics.unfold(" orphan\r\nSUMMARY:x"), ["orphan", "SUMMARY:x"])

    def test_fold_that_splits_a_utf8_multibyte_character(self):
        """A fold placed between the two bytes of 'ö'.

        RFC 5545 folds at 75 OCTETS, so a producer can and does split a
        multi-byte character in half. The module's entry point is `str`, so
        whoever reads the file owns this: a strict utf-8 decode raises before
        `unfold` is ever reached, and a lenient one (what `main()` does) loses
        exactly that character. What must NOT happen either way is a separator
        appearing in the value or the rest of the name going missing.
        """
        raw = (b"BEGIN:VEVENT\r\nUID:mb-1\r\nDTSTART:" + stamp(3).encode() + b"T090000Z\r\n"
               b"SUMMARY:Kaffe med B\xc3" b"\r\n " b"\xb6rje\r\nEND:VEVENT\r\n")
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")
        lenient = ics.parse_events(raw.decode("utf-8", "replace"))[0]["summary"]
        self.assertTrue(lenient.startswith("Kaffe med B"), lenient)
        self.assertTrue(lenient.endswith("rje"), lenient)
        self.assertNotIn(" rje", lenient)          # no separator was introduced
        # The fix belongs to the caller, and is to unfold the BYTES first. There
        # is no bytes entry point today; this is what one would have to do.
        fixed = ics.parse_events(raw.replace(b"\r\n ", b"").decode("utf-8"))[0]
        self.assertEqual(fixed["summary"], "Kaffe med Börje")


class TestContentLine(unittest.TestCase):
    def test_quoted_cn_containing_a_comma(self):
        name, params, value = ics.parse_content_line(
            'ATTENDEE;CN="Berg, Sam";PARTSTAT=ACCEPTED:mailto:me@example.com')
        self.assertEqual(name, "ATTENDEE")
        self.assertEqual(params["CN"], ["Berg, Sam"])
        self.assertEqual(params["PARTSTAT"], ["ACCEPTED"])
        self.assertEqual(value, "mailto:me@example.com")

    def test_unquoted_comma_in_a_param_is_a_value_separator(self):
        # Not a bug — RFC 5545 says an unquoted comma separates param values.
        # This is exactly why the quoted form above has to work.
        _, params, _ = ics.parse_content_line("ATTENDEE;CN=Nilsson, Anna:mailto:a@b.example")
        self.assertEqual(params["CN"], ["Nilsson", "Anna"])

    def test_value_may_contain_colons_and_quotes(self):
        self.assertEqual(ics.parse_content_line("DESCRIPTION:Call at 10:00 sharp"),
                         ("DESCRIPTION", {}, "Call at 10:00 sharp"))
        self.assertEqual(ics.parse_content_line("SUMMARY:He said \"hi\": then left")[2],
                         "He said \"hi\": then left")

    def test_rfc6868_caret_escapes_in_a_parameter(self):
        _, params, _ = ics.parse_content_line(
            "ATTENDEE;CN=Sales ^'Team^' ^nfloor 2 ^^:mailto:sales@example.com")
        self.assertEqual(params["CN"], ['Sales "Team" \nfloor 2 ^'])

    def test_case_insensitive_names_and_params(self):
        name, params, _ = ics.parse_content_line("attendee;cn=Anna;partstat=declined:mailto:a@b.example")
        self.assertEqual(name, "ATTENDEE")
        self.assertEqual(params["CN"], ["Anna"])

    def test_bare_parameter_and_missing_colon(self):
        _, params, _ = ics.parse_content_line("ATTENDEE;RSVP:mailto:a@b.example")
        self.assertEqual(params["RSVP"], [""])
        self.assertEqual(ics.parse_content_line("THIS IS NOT A PROPERTY"), ("", {}, ""))

    def test_escaped_comma_semicolon_and_newline_in_summary(self):
        ev = ics.parse_events(cal(vevent(
            "UID:esc-1", f"DTSTART:{stamp(3)}T090000Z",
            r"SUMMARY:Coffee\, cake\; and a chat\nagenda attached",
            r"LOCATION:Café Ritz\, Stockholm",
            r"DESCRIPTION:line one\nline two\\end")))[0]
        self.assertEqual(ev["summary"], "Coffee, cake; and a chat\nagenda attached")
        self.assertEqual(ev["location"], "Café Ritz, Stockholm")
        self.assertEqual(ev["description"], "line one\nline two\\end")

    def test_a_label_collapses_the_newline_it_never_stores_a_raw_one(self):
        ev = ics.parse_events(cal(vevent(
            "UID:esc-2", f"DTSTART:{stamp(3)}T090000Z",
            r"SUMMARY:Coffee\, cake\nand a chat")))[0]
        label, clean = ics._label_for(ev, 60)
        self.assertEqual(clean, "Coffee, cake and a chat")
        self.assertEqual(label, "Coffee, cake and a chat")
        self.assertNotIn("\n", label)


class TestDatesAndZones(unittest.TestCase):
    """DTSTART in the four forms that actually arrive, plus the DST and all-day
    traps. `on_date` is the load-bearing value: it becomes the stored day."""

    def test_tzid_stockholm_across_dst_in_both_directions(self):
        summer = ics.parse_events(cal(vevent(
            "UID:dst-s", "DTSTART;TZID=Europe/Stockholm:20260715T140000")))[0]
        winter = ics.parse_events(cal(vevent(
            "UID:dst-w", "DTSTART;TZID=Europe/Stockholm:20260115T140000")))[0]
        self.assertEqual((summer["on_date"], winter["on_date"]), ("2026-07-15", "2026-01-15"))
        self.assertEqual(summer["tzid"], "Europe/Stockholm")
        self.assertFalse(summer["floating"] or winter["floating"])
        # CEST is +02:00, CET is +01:00 — the same wall clock, two real instants.
        self.assertEqual(summer["start"].utcoffset(), timedelta(hours=2))
        self.assertEqual(winter["start"].utcoffset(), timedelta(hours=1))
        self.assertEqual(summer["start"].astimezone(timezone.utc).strftime("%H:%M"), "12:00")
        self.assertEqual(winter["start"].astimezone(timezone.utc).strftime("%H:%M"), "13:00")

    def test_the_wall_clock_day_survives_both_dst_transition_days(self):
        # 2026-03-29 02:30 Stockholm does not exist; 2026-10-25 02:30 happens
        # twice. Neither may move the day, which is all that gets stored.
        gap = ics.parse_events(cal(vevent(
            "UID:dst-gap", "DTSTART;TZID=Europe/Stockholm:20260329T023000")))[0]
        fold = ics.parse_events(cal(vevent(
            "UID:dst-fold", "DTSTART;TZID=Europe/Stockholm:20261025T023000")))[0]
        self.assertEqual(gap["on_date"], "2026-03-29")
        self.assertEqual(fold["on_date"], "2026-10-25")
        self.assertEqual(gap["start"].utcoffset(), timedelta(hours=1))    # PEP 495 fold=0
        self.assertEqual(fold["start"].utcoffset(), timedelta(hours=2))

    def test_apple_freeassociation_tzid_resolves_to_its_iana_tail(self):
        ev = ics.parse_events(cal(vevent(
            "UID:apple-tz",
            "DTSTART;TZID=/freeassociation.sourceforge.net/Europe/Stockholm:20260715T140000")))[0]
        self.assertEqual(ev["tzid"], "Europe/Stockholm")
        self.assertFalse(ev["floating"])
        self.assertEqual(ev["warnings"], [])

    def test_windows_tzid_is_unresolved_out_loud_not_silently(self):
        ev = ics.parse_events(cal(vevent(
            "UID:win-tz", 'DTSTART;TZID="W. Europe Standard Time":20260715T140000',
            "SUMMARY:Outlook invite")))[0]
        self.assertTrue(ev["floating"])                     # read as local wall clock
        self.assertEqual(ev["tzid"], "")
        self.assertEqual(ev["raw_tzid"], "W. Europe Standard Time")   # the raw id is kept
        self.assertEqual(ev["on_date"], "2026-07-15")
        self.assertEqual(len(ev["warnings"]), 1)
        self.assertIn("W. Europe Standard Time", ev["warnings"][0])
        rep = ics.report([ev])
        self.assertEqual(rep["unknown_timezones"], 1)
        self.assertIn("timezone", rep["summary_text"])

    def test_utc_z_form(self):
        ev = ics.parse_events(cal(vevent("UID:z", "DTSTART:20260617T120000Z")))[0]
        self.assertEqual((ev["tzid"], ev["on_date"], ev["floating"]),
                         ("UTC", "2026-06-17", False))
        self.assertEqual(ev["start"].utcoffset(), timedelta(0))

    def test_a_z_time_is_filed_on_the_day_it_is_on_this_mac(self):
        """NC-370. WAS: "a Z time stores the UTC day, not the reader's local day".

        It was the UTC day, so 23:00Z was filed a day EARLY for anyone east of
        Greenwich (it is already tomorrow in Stockholm) and an early-morning Z
        time a day LATE for anyone west of it. UTC is nobody's wall clock, so a
        time written in UTC is now filed on the day it falls on for the person
        running the import. The instant itself stays UTC.
        """
        late = cal(vevent("UID:z-late", "DTSTART:20260617T230000Z"))
        early = cal(vevent("UID:z-early", "DTSTART:20260617T030000Z"))
        for zone, late_day, early_day in (
                ("Europe/Stockholm", "2026-06-18", "2026-06-17"),      # +2: 01:00 / 05:00
                ("Asia/Seoul", "2026-06-18", "2026-06-17"),            # +9: 08:00 / 12:00
                ("UTC", "2026-06-17", "2026-06-17"),
                ("America/Los_Angeles", "2026-06-17", "2026-06-16")):  # -7: 16:00 / 20:00 the day before
            with mock.patch.dict(os.environ, {"NC_ICS_TZ": zone}):
                got_late = ics.parse_events(late)[0]
                got_early = ics.parse_events(early)[0]
            self.assertEqual(got_late["on_date"], late_day, zone)
            self.assertEqual(got_early["on_date"], early_day, zone)
            self.assertEqual(got_late["tzid"], "UTC")            # still read as UTC...
            self.assertEqual(got_late["start"].utcoffset(), timedelta(0))   # ...and kept as that instant

    def test_a_z_time_follows_the_macs_own_zone_through_the_year(self):
        """NC-370. With no zone named in settings, "this Mac" means its own zone as
        it was on THAT day, daylight saving included. A fixed offset read off the
        clock today would file a January event by the summer's rules."""
        winter = cal(vevent("UID:z-win", "DTSTART:20260117T223000Z"))   # 23:30 in Stockholm
        summer = cal(vevent("UID:z-sum", "DTSTART:20260717T223000Z"))   # 00:30 the next day
        old_tz = os.environ.get("TZ")

        def restore():
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()
        self.addCleanup(restore)
        with mock.patch.dict(os.environ, {"NC_ICS_TZ": "", "TZ": "Europe/Stockholm"}):
            time.tzset()
            self.assertEqual(ics.parse_events(winter)[0]["on_date"], "2026-01-17")
            self.assertEqual(ics.parse_events(summer)[0]["on_date"], "2026-07-18")
        with mock.patch.dict(os.environ, {"NC_ICS_TZ": "", "TZ": "America/Los_Angeles"}):
            time.tzset()
            self.assertEqual(ics.parse_events(winter)[0]["on_date"], "2026-01-17")  # 14:30
            self.assertEqual(ics.parse_events(summer)[0]["on_date"], "2026-07-17")  # 15:30

    def test_events_that_carry_their_own_day_are_not_moved(self):
        """NC-370. Only a time written in UTC is re-read on the local clock. A time
        with its own zone, an all-day date and a floating time are wall-clock
        facts, and stay on the day the file wrote, whatever zone this Mac is in."""
        paris = cal(vevent("UID:own-zone",
                           "DTSTART;TZID=Europe/Paris:20260617T233000"))
        all_day = cal(vevent("UID:own-day", "DTSTART;VALUE=DATE:20260617"))
        floating = cal(vevent("UID:own-clock", "DTSTART:20260617T233000"))
        for zone in ("Asia/Seoul", "America/Los_Angeles", "Europe/Stockholm"):
            with mock.patch.dict(os.environ, {"NC_ICS_TZ": zone}):
                for text in (paris, all_day, floating):
                    self.assertEqual(ics.parse_events(text)[0]["on_date"], "2026-06-17",
                                     f"{zone}: {text[:60]!r}")

    def test_floating_time_has_no_zone_and_keeps_its_wall_clock_day(self):
        text = cal(vevent("UID:float", "DTSTART:20260617T210000", "SUMMARY:Dinner"))
        with mock.patch.dict(os.environ, {"NC_ICS_TZ": "Europe/Stockholm"}):
            ev = ics.parse_events(text)[0]
            self.assertTrue(ev["floating"])
            self.assertEqual(ev["tzid"], "")
            self.assertEqual(ev["raw_tzid"], "")
            self.assertEqual(ev["on_date"], "2026-06-17")
            self.assertEqual(ev["start"].utcoffset(), timedelta(hours=2))
        # NC_ICS_TZ is re-read on every call, so a Settings edit needs no restart
        with mock.patch.dict(os.environ, {"NC_ICS_TZ": "Asia/Seoul"}):
            ev = ics.parse_events(text)[0]
            self.assertEqual(ev["start"].utcoffset(), timedelta(hours=9))
            self.assertEqual(ev["on_date"], "2026-06-17")     # the day never moves
        # and a garbage zone name falls back rather than raising
        with mock.patch.dict(os.environ, {"NC_ICS_TZ": "Not/AZone"}):
            self.assertEqual(ics.parse_events(text)[0]["on_date"], "2026-06-17")

    def test_all_day_value_date_and_the_exclusive_dtend(self):
        """RFC 5545: DTEND on an all-day event is EXCLUSIVE. A one-day event on
        the 17th is written DTSTART 20260617 / DTEND 20260618, and reading the
        DTEND as the day would file it a day late."""
        ev = ics.parse_events(cal(vevent(
            "UID:allday", "DTSTART;VALUE=DATE:20260617", "DTEND;VALUE=DATE:20260618",
            "SUMMARY:Anna in town")))[0]
        self.assertTrue(ev["all_day"])
        self.assertEqual(ev["on_date"], "2026-06-17")          # not the 18th
        self.assertFalse(ev["floating"])
        self.assertEqual(ev["tzid"], "")
        # `end` is the raw exclusive boundary: a consumer wanting the inclusive
        # last day of an all-day event must subtract a day itself.
        self.assertEqual(ev["end"].date().isoformat(), "2026-06-18")

    def test_all_day_with_no_dtend_lasts_one_day(self):
        ev = ics.parse_events(cal(vevent(
            "UID:allday-2", "DTSTART;VALUE=DATE:20260617")))[0]
        self.assertEqual(ev["on_date"], "2026-06-17")
        self.assertEqual(ev["end"] - ev["start"], timedelta(days=1))

    def test_an_8_digit_dtstart_without_value_date_is_still_all_day(self):
        ev = ics.parse_events(cal(vevent("UID:bare-date", "DTSTART:20260617")))[0]
        self.assertTrue(ev["all_day"])
        self.assertEqual(ev["on_date"], "2026-06-17")

    def test_duration_instead_of_dtend(self):
        ev = ics.parse_events(cal(vevent(
            "UID:dur", "DTSTART:20260617T090000Z", "DURATION:PT1H30M")))[0]
        self.assertEqual(ev["end"] - ev["start"], timedelta(hours=1, minutes=30))
        self.assertEqual(ev["warnings"], [])
        self.assertEqual(ics.parse_duration("P2W"), timedelta(weeks=2))
        self.assertEqual(ics.parse_duration("-PT15M"), -timedelta(minutes=15))
        self.assertIsNone(ics.parse_duration("P"))
        self.assertIsNone(ics.parse_duration("banana"))

    def test_unreadable_duration_warns_and_still_yields_an_event(self):
        ev = ics.parse_events(cal(vevent(
            "UID:dur-bad", "DTSTART:20260617T090000Z", "DURATION:nonsense",
            "SUMMARY:Still a meeting")))[0]
        self.assertTrue(ev["parsed"])
        self.assertEqual(ev["end"], ev["start"])
        self.assertEqual(len(ev["warnings"]), 1)
        self.assertIn("DURATION", ev["warnings"][0])

    def test_impossible_dates_are_reported_not_guessed(self):
        events = ics.parse_events(cal(
            vevent("UID:bad-1", "DTSTART;VALUE=DATE:20260231", "SUMMARY:Feb 31st"),
            vevent("UID:bad-2", "DTSTART:20260617T250000Z", "SUMMARY:25 o'clock"),
            vevent("UID:bad-3", "DTSTART:not-a-date", "SUMMARY:Gibberish")))
        self.assertEqual([e["parsed"] for e in events], [False, False, False])
        self.assertIn("impossible date", events[0]["error"])
        self.assertIn("start date", events[1]["error"])        # in words, not the field name
        self.assertNotIn("DTSTART", events[1]["error"])
        self.assertIn("unrecognised date-time", events[2]["error"])
        rep = ics.report(events)
        self.assertEqual(rep["events_unparseable"], 3)
        self.assertEqual([u["summary"] for u in rep["unparseable"]],
                         ["Feb 31st", "25 o'clock", "Gibberish"])

    def test_a_bad_dtend_only_costs_the_end_time(self):
        ev = ics.parse_events(cal(vevent(
            "UID:bad-end", "DTSTART:20260617T090000Z", "DTEND:garbage",
            "SUMMARY:Fine start")))[0]
        self.assertTrue(ev["parsed"])
        self.assertEqual(ev["on_date"], "2026-06-17")
        self.assertTrue(any("DTEND" in w for w in ev["warnings"]))


class TestComponentScanning(unittest.TestCase):
    """A flat line scanner reads a VALARM's DTSTART as the event's. This one is
    component-aware, and these are the files that prove it."""

    def test_a_valarm_cannot_leak_its_own_dtstart_or_summary(self):
        events = ics.parse_events(cal(vevent(
            "UID:alarm", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee with Anna",
            "BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY",
            "DTSTART:19700101T000000Z", "SUMMARY:Reminder", "DESCRIPTION:Reminder",
            "END:VALARM", ANNA)))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary"], "Coffee with Anna")
        self.assertEqual(events[0]["on_date"], iso(3))
        self.assertEqual(len(events[0]["attendees"]), 1)

    def test_a_vtimezone_is_not_an_event(self):
        text = ("BEGIN:VCALENDAR" + CRLF + "PRODID:-//Apple Inc.//macOS//EN" + CRLF
                + "BEGIN:VTIMEZONE" + CRLF + "TZID:Europe/Stockholm" + CRLF
                + "BEGIN:DAYLIGHT" + CRLF + "TZOFFSETFROM:+0100" + CRLF
                + "TZOFFSETTO:+0200" + CRLF + "DTSTART:19810329T020000" + CRLF
                + "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU" + CRLF + "END:DAYLIGHT" + CRLF
                + "END:VTIMEZONE" + CRLF
                + vevent("UID:after-tz", "DTSTART;TZID=Europe/Stockholm:20260715T140000",
                         "SUMMARY:The only event")
                + "END:VCALENDAR" + CRLF)
        events = ics.parse_events(text)
        self.assertEqual(summaries(events), ["The only event"])
        self.assertEqual(events[0]["on_date"], "2026-07-15")
        self.assertFalse(events[0]["is_recurring"])       # the VTIMEZONE's RRULE is not the event's

    def test_an_unterminated_valarm_still_closes_the_event(self):
        events = ics.parse_events(cal(vevent(
            "UID:alarm-open", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Ragged",
            "BEGIN:VALARM", "TRIGGER:-PT5M")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary"], "Ragged")

    def test_header_properties_and_x_props(self):
        ev = ics.parse_events(cal(vevent(
            "UID:hdr", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:X",
            "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT30M", "X-MICROSOFT-CDO-BUSYSTATUS:BUSY",
            "CATEGORIES:Work,Coffee"), calname="Privat"))[0]
        self.assertEqual(ev["calendar"], "Privat")
        self.assertEqual(ev["prodid"], "-//Test//Network CRM//EN")
        self.assertEqual(ev["x_props_ignored"], 2)
        self.assertEqual(ev["categories"], ["Work", "Coffee"])

    def test_lowercase_component_names(self):
        text = ("begin:vcalendar" + CRLF + "begin:vevent" + CRLF + "uid:lower" + CRLF
                + f"dtstart:{stamp(3)}T090000Z" + CRLF + "summary:Lower case" + CRLF
                + "end:vevent" + CRLF + "end:vcalendar" + CRLF)
        self.assertEqual(summaries(ics.parse_events(text)), ["Lower case"])

    def test_two_concatenated_calendars_both_yield_their_events(self):
        # Merging two exports into one file, or a feed that concatenates. Both
        # events must survive, each stamped with its own calendar (NC-370).
        text = (cal(vevent("UID:cc-1", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:From work"),
                    calname="Work", prodid="-//A//EN")
                + cal(vevent("UID:cc-2", f"DTSTART:{stamp(4)}T090000Z", "SUMMARY:From privat"),
                      calname="Privat", prodid="-//B//EN"))
        events = ics.parse_events(text)
        self.assertEqual(summaries(events), ["From work", "From privat"])
        self.assertEqual([e["on_date"] for e in events], [iso(3), iso(4)])
        self.assertEqual([(e["calendar"], e["prodid"]) for e in events],
                         [("Work", "-//A//EN"), ("Privat", "-//B//EN")],
                         "the second calendar's events carry the first calendar's name")

    def test_a_vevent_outside_any_vcalendar_still_parses(self):
        events = ics.parse_events(vevent(
            "UID:naked", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:No wrapper") + "END:VCALENDAR")
        self.assertEqual(summaries(events), ["No wrapper"])
        self.assertTrue(events[0]["parsed"])

    def test_empty_file_and_a_file_with_no_vevent(self):
        for text in ("", "   \r\n", cal(), "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                     "Not an iCalendar file at all\r\n"):
            self.assertEqual(ics.parse_events(text), [], repr(text[:24]))
        rep = ics.report(ics.parse_events(cal()), [], [])
        self.assertEqual(rep["events_seen"], 0)
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        self.assertFalse(rep["wrote_anything"])
        self.assertIn("0 calendar events read.", rep["sentences"][0])

    def test_missing_uid_gets_a_stable_content_hash(self):
        body = vevent(f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:No id here", ANNA)
        first = ics.parse_events(cal(body))[0]
        again = ics.parse_events(cal(body))[0]
        self.assertTrue(first["uid_synthetic"])
        self.assertTrue(first["uid"].startswith("nc-nouid-"))
        self.assertEqual(first["uid"], again["uid"])        # stable across runs
        self.assertTrue(any("no UID" in w for w in first["warnings"]))
        other = ics.parse_events(cal(vevent(
            f"DTSTART:{stamp(4)}T090000Z", "SUMMARY:No id here", ANNA)))[0]
        self.assertNotEqual(first["uid"], other["uid"])     # different content, different id
        self.assertEqual(ics.report([first])["synthetic_uids"], 1)

    def test_truncated_file_is_reported_not_dropped(self):
        text = (cal(vevent("UID:whole", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Complete", ANNA))
                .replace("END:VCALENDAR" + CRLF, "")
                + "BEGIN:VEVENT" + CRLF + "UID:cut" + CRLF
                + f"DTSTART:{stamp(4)}T090000Z" + CRLF + "SUMMARY:Cut off mid-file" + CRLF)
        events = ics.parse_events(text)
        self.assertEqual(summaries(events), ["Complete", "Cut off mid-file"])
        self.assertTrue(events[0]["parsed"])
        self.assertFalse(events[1]["parsed"])
        self.assertIn("ends in the middle of this event", events[1]["error"])
        rep = ics.report(events)
        self.assertEqual(rep["events_unparseable"], 1)
        self.assertEqual(rep["unparseable"][0]["summary"], "Cut off mid-file")
        self.assertIn("ends in the middle of this event", rep["unparseable"][0]["reason"])

    def test_missing_end_before_the_next_begin(self):
        text = ("BEGIN:VCALENDAR" + CRLF
                + "BEGIN:VEVENT" + CRLF + "UID:a" + CRLF
                + f"DTSTART:{stamp(3)}T090000Z" + CRLF + "SUMMARY:First" + CRLF
                + vevent("UID:b", f"DTSTART:{stamp(4)}T090000Z", "SUMMARY:Second")
                + "END:VCALENDAR" + CRLF)
        events = ics.parse_events(text)
        self.assertEqual(summaries(events), ["First", "Second"])
        self.assertFalse(events[0]["parsed"])
        self.assertIn("never closed before the next event began", events[0]["error"])
        self.assertTrue(events[1]["parsed"])


class TestAttendeeParsing(unittest.TestCase):
    def test_cutype_and_partstat_are_normalised(self):
        ev = ics.parse_events(cal(vevent(
            "UID:att", f"DTSTART:{stamp(3)}T090000Z",
            "ATTENDEE;cutype=room;cn=Rum 3:mailto:rum3@example.com",
            "ATTENDEE;PARTSTAT=declined;CN=Nils:mailto:nils@example.com",
            "ATTENDEE;RSVP=true;CN=Anna:MAILTO:Anna@Example.COM")))[0]
        kinds = [(a["cutype"], a["partstat"], a["email"], a["rsvp"]) for a in ev["attendees"]]
        self.assertEqual(kinds, [
            ("ROOM", "NEEDS-ACTION", "rum3@example.com", False),
            ("INDIVIDUAL", "DECLINED", "nils@example.com", False),
            ("INDIVIDUAL", "NEEDS-ACTION", "anna@example.com", True),
        ])

    def test_non_mailto_cal_address_keeps_the_cn(self):
        ev = ics.parse_events(cal(vevent(
            "UID:urn", f"DTSTART:{stamp(3)}T090000Z",
            "ATTENDEE;CN=Anna Nilsson:urn:uuid:8f2a-1111")))[0]
        self.assertEqual(ev["attendees"][0]["email"], "")
        self.assertEqual(ev["attendees"][0]["name"], "Anna Nilsson")

    def test_the_same_invitee_twice_is_one_person(self):
        ev = ics.parse_events(cal(vevent(
            "UID:dupe-att", f"DTSTART:{stamp(3)}T090000Z",
            "ATTENDEE;CN=Anna Nilsson:mailto:anna@example.com",
            "ATTENDEE;CN=Anna N:MAILTO:ANNA@EXAMPLE.COM")))[0]
        self.assertEqual(len(ev["attendees"]), 1)

    def test_name_variants_flip_one_comma_only(self):
        self.assertEqual(ics._name_variants("Berg, Sam"),
                         ["Berg, Sam", "Sam Berg"])
        self.assertEqual(ics._name_variants("Nilsson, Anna, PhD"), ["Nilsson, Anna, PhD"])
        self.assertEqual(ics._name_variants("Anna Nilsson"), ["Anna Nilsson"])
        self.assertEqual(ics._name_variants(""), [])


# ─────────────────────────────────────────────────────────────────────────────
# Matching and proposing — needs crm.db
# ─────────────────────────────────────────────────────────────────────────────
class IcsBase(Base):
    """A temp crm.db with two known people, plus a deterministic environment.

    Five settings and the exclude file are read at CALL time, and `import config`
    means secrets/.env may legitimately have set any of them on the machine
    running this suite — so they are pinned here rather than assumed.
    """

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {
            "SELF_EMAIL": "me@example.com",
            "SELF_NAME": "Sam Berg",
            "NC_ICS_TZ": "Europe/Stockholm",
        })
        env.start()
        self.addCleanup(env.stop)
        for key in ("NC_ICS_MAX_ATTENDEES", "NC_ICS_BACK_DAYS",
                    "NC_ICS_FORWARD_DAYS", "NC_ICS_LABEL_MAX"):
            os.environ.pop(key, None)          # patch.dict restores these too
        # ics_import owns its own module-level EXCLUDE_FILE, like every other
        # importer. Patch the owner of the name, never a re-export.
        self.exclude = os.path.join(self.dir, "exclude_list.txt")
        self.write_exclude()
        excl = mock.patch.object(ics, "EXCLUDE_FILE", self.exclude)
        excl.start()
        self.addCleanup(excl.stop)

        self.anna = db.create_contact(self.conn, "Anna Nilsson")
        db.add_identifier(self.conn, self.anna, "email", "anna@example.com")
        self.bjorn = db.create_contact(self.conn, "Björn Ek")
        self.conn.commit()

    def write_exclude(self, *names):
        with open(self.exclude, "w", encoding="utf-8") as fh:
            fh.write("# names to skip\n" + "".join(n + "\n" for n in names))

    def counts(self):
        return (self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM contact_dates").fetchone()[0])

    def plan(self, text, **kw):
        """`ics.plan` plus the three assertions that must hold for every run:
        the accounting balances, nothing was written, and nobody was created."""
        before = self.counts()
        res = ics.plan(self.conn, text, **kw)
        rep = res["report"]
        self.assertEqual(
            rep["accounting"]["unexplained"], 0,
            f"events went missing from the report: {rep['accounting']}")
        self.assertFalse(rep["wrote_anything"])
        self.assertEqual(self.counts(), before,
                         "the importer touched crm.db — it must only read")
        return res


class TestMatching(IcsBase):
    def test_match_by_email(self):
        res = self.plan(cal(vevent(
            "UID:m-email", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee with Anna",
            ME, ANNA)))
        self.assertEqual([(p["contact_id"], p["on_date"], p["confidence"])
                          for p in res["proposals"]],
                         [(self.anna, iso(3), "email")])
        self.assertEqual(res["report"]["events_matched"], 1)
        self.assertEqual(res["report"]["attendees_matched"], 1)
        self.assertEqual(res["report"]["attendees_ignored_by_reason"], {"you": 1})

    def test_match_by_name_norm_when_the_email_is_unknown(self):
        res = self.plan(cal(vevent(
            "UID:m-name", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Lunch",
            "ATTENDEE;CN=björn ek:mailto:private-address@example.com")))
        self.assertEqual([(p["contact_id"], p["confidence"]) for p in res["proposals"]],
                         [(self.bjorn, "name")])

    def test_match_by_a_reordered_directory_name(self):
        res = self.plan(cal(vevent(
            "UID:m-reorder", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Dinner",
            'ATTENDEE;CN="Ek, Björn":mailto:unknown@example.com')))
        self.assertEqual([(p["contact_id"], p["confidence"]) for p in res["proposals"]],
                         [(self.bjorn, "name (reordered)")])

    def test_the_organizer_counts_as_a_person(self):
        # Somebody else invited you: ORGANIZER is them, ATTENDEE is you.
        res = self.plan(cal(vevent(
            "UID:m-org", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Their invite",
            "ORGANIZER;CN=Anna Nilsson:mailto:anna@example.com",
            "ATTENDEE;CN=Sam Berg;PARTSTAT=ACCEPTED:mailto:me@example.com")))
        self.assertEqual([p["contact_id"] for p in res["proposals"]], [self.anna])
        self.assertEqual(res["matches"][0]["matched"][0]["role"], "organizer")

    def test_an_attendee_who_matches_nobody_is_reported_never_dropped(self):
        res = self.plan(cal(vevent(
            "UID:m-ghost", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Intro call",
            ME, "ATTENDEE;CN=Ingrid Okänd:mailto:ingrid@example.com")))
        rep = res["report"]
        self.assertEqual(res["proposals"], [])
        self.assertEqual(rep["events_matched"], 0)
        self.assertEqual(rep["events_no_match"], 1)          # its own category
        self.assertEqual(rep["events_skipped"], 0)
        self.assertEqual(rep["attendees_unmatched"], 1)
        self.assertEqual(rep["unmatched_attendees"][0]["name"], "Ingrid Okänd")
        self.assertEqual(rep["unmatched_attendees"][0]["email"], "ingrid@example.com")
        self.assertIn("not in your contacts", rep["summary_text"])
        self.assertNotIn("CRM", rep["summary_text"])
        self.assertIn("Nothing was created for them", rep["summary_text"])

    def test_an_ambiguous_name_is_refused_with_both_ids_not_guessed(self):
        twin = db.create_contact(self.conn, "Björn Ek")
        self.conn.commit()
        res = self.plan(cal(vevent(
            "UID:m-amb", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Which one",
            "ATTENDEE;CN=Björn Ek:mailto:unknown2@example.com")))
        self.assertEqual(res["proposals"], [])
        reason = res["report"]["unmatched_attendees"][0]["reason"]
        self.assertIn("2 contacts share this name", reason)
        self.assertIn(str(self.bjorn), reason)
        self.assertIn(str(twin), reason)

    def test_nobody_is_ever_created_even_when_every_attendee_is_a_stranger(self):
        before = self.counts()[0]
        strangers = [f"ATTENDEE;CN=Stranger {i}:mailto:s{i}@example.com" for i in range(4)]
        self.plan(cal(vevent("UID:m-new", f"DTSTART:{stamp(3)}T090000Z",
                             "SUMMARY:All strangers", ME, *strangers)))
        self.assertEqual(self.counts()[0], before)           # invariant 1
        self.assertFalse(hasattr(ics, "create_new"))         # and no flag to turn it on

    def test_a_read_only_connection_survives_the_whole_pipeline(self):
        """The strongest form of "plan() never writes": run it against a connection
        SQLite itself refuses to let write. apply() is the writer and is not
        exercised here — that separation is the whole design."""
        guard = db.get_db(self.path)
        guard.execute("PRAGMA query_only = TRUE")
        self.addCleanup(guard.close)
        with self.assertRaises(sqlite3.OperationalError):    # the guard really is armed
            guard.execute("INSERT INTO contacts (name, name_norm) VALUES ('x', 'x')")
        res = ics.plan(guard, cal(
            vevent("UID:ro-1", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA),
            vevent("UID:ro-2", f"DTSTART:{stamp(-2000)}T090000Z", "SUMMARY:Ancient", ANNA),
            vevent("UID:ro-3", "SUMMARY:Broken", ANNA)))
        self.assertEqual(len(res["proposals"]), 1)
        self.assertEqual(self.counts(), (2, 0))

    def test_matching_by_email_reaches_an_excluded_contact_through_the_name_pass(self):
        # db.find_by_handle filters `NOT c.excluded`, so an excluded contact
        # reached only by email looks unmatched. The name pass is what labels
        # them honestly — that is the documented mitigation, tested.
        db.set_excluded(self.conn, self.anna, 1)
        self.conn.commit()
        res = self.plan(cal(vevent(
            "UID:m-excl-crm", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)))
        rep = res["report"]
        self.assertEqual(res["proposals"], [])
        self.assertEqual(rep["attendees_unmatched"], 0)      # not mislabelled
        self.assertEqual(list(rep["attendees_ignored_by_reason"]),
                         ["you", "contact excluded in the CRM"])
        self.assertEqual(rep["events_skipped_by_reason"], {"all_excluded": 1})


class TestExcludeList(IcsBase):
    def test_a_name_on_the_exclude_list_is_skipped_with_a_reason(self):
        self.write_exclude("Anna Nilsson")
        res = self.plan(cal(vevent(
            "UID:x-1", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)))
        rep = res["report"]
        self.assertEqual(res["proposals"], [])
        self.assertEqual(rep["events_skipped_by_reason"], {"all_excluded": 1})
        self.assertEqual(rep["events_all_attendees_excluded"], 1)
        self.assertEqual(rep["attendees_ignored_by_reason"].get("exclude_list"), 1)
        self.assertIn("exclude list", rep["summary_text"])

    def test_the_exclude_list_is_read_at_call_time(self):
        text = cal(vevent("UID:x-2", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA))
        self.assertEqual(len(self.plan(text)["proposals"]), 1)
        self.write_exclude("anna   nilsson")        # normalised, so spacing is irrelevant
        self.assertEqual(self.plan(text)["proposals"], [])

    def test_one_excluded_person_does_not_hide_the_others(self):
        self.write_exclude("Anna Nilsson")
        res = self.plan(cal(vevent(
            "UID:x-3", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Two of them", ME, ANNA,
            "ATTENDEE;CN=Björn Ek:mailto:bjorn@example.com")))
        self.assertEqual([p["contact_id"] for p in res["proposals"]], [self.bjorn])
        self.assertEqual(res["report"]["events_skipped"], 0)
        self.assertEqual(res["report"]["attendees_ignored_by_reason"].get("exclude_list"), 1)


class TestSkips(IcsBase):
    def test_a_room_and_a_declined_invitee_are_skipped_but_visible(self):
        res = self.plan(cal(vevent(
            "UID:s-room", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Board meeting", ME,
            "ATTENDEE;CUTYPE=ROOM;CN=Rum 3:mailto:rum3@example.com",
            "ATTENDEE;CUTYPE=RESOURCE;CN=Projector:mailto:proj@example.com",
            "ATTENDEE;CN=Nils Nej;PARTSTAT=DECLINED:mailto:nils@example.com",
            ANNA)))
        rep = res["report"]
        self.assertEqual([p["contact_id"] for p in res["proposals"]], [self.anna])
        self.assertEqual(rep["attendees_ignored_by_reason"], {
            "cutype ROOM": 1, "cutype RESOURCE": 1, "you": 1, "partstat DECLINED": 1})
        self.assertEqual(rep["attendees_unmatched"], 0)
        # furniture does not consume the attendee cap; a declined human does
        self.assertEqual(res["matches"][0]["others_count"], 2)

    def test_a_room_that_booked_itself_is_not_a_person(self):
        # Meeting-room mailboxes send invitations. The room must not consume the
        # attendee cap and must not be proposed as somebody you saw. (It is also
        # absent from `ignored`, unlike a ROOM *attendee* — see note 8.)
        res = self.plan(cal(vevent(
            "UID:s-roomorg", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Room booking",
            "ORGANIZER;CUTYPE=ROOM;CN=Rum 3:mailto:rum3@example.com", ANNA)))
        self.assertEqual([p["contact_id"] for p in res["proposals"]], [self.anna])
        self.assertEqual(res["matches"][0]["others_count"], 1)

    def test_everyone_declined(self):
        res = self.plan(cal(vevent(
            "UID:s-dec", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Nobody came", ME,
            "ATTENDEE;CN=Anna Nilsson;PARTSTAT=DECLINED:mailto:anna@example.com")))
        self.assertEqual(res["proposals"], [])
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"all_declined": 1})
        self.assertIn("declined", res["report"]["summary_text"])

    def test_a_personal_reminder_with_nobody_on_it(self):
        res = self.plan(cal(vevent(
            "UID:s-solo", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Gym")))
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"no_other_people": 1})
        self.assertIn("nobody else", res["report"]["summary_text"])

    def test_an_event_with_only_you_on_it(self):
        res = self.plan(cal(vevent(
            "UID:s-self", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Focus time", ME,
            "ATTENDEE;CN=Sam Berg:mailto:me@example.com")))
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"no_other_people": 1})
        self.assertEqual(res["report"]["attendees_ignored_by_reason"], {"you": 1})

    def test_a_cancelled_event_is_not_a_meeting(self):
        res = self.plan(cal(vevent(
            "UID:s-cancel", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Called off",
            "STATUS:CANCELLED", ME, ANNA)))
        self.assertEqual(res["proposals"], [])
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"cancelled": 1})
        self.assertIn("cancelled", res["report"]["summary_text"])

    def test_a_tentative_event_is_still_a_meeting(self):
        res = self.plan(cal(vevent(
            "UID:s-tent", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Maybe coffee",
            "STATUS:TENTATIVE", ME, ANNA)))
        self.assertEqual(len(res["proposals"]), 1)


class TestAttendeeCap(IcsBase):
    def all_hands(self, others, uid="cap"):
        lines = [f"ATTENDEE;CN=Person {i};PARTSTAT=ACCEPTED:mailto:p{i}@example.com"
                 for i in range(others - 1)]
        return cal(vevent(f"UID:{uid}", f"DTSTART:{stamp(3)}T090000Z",
                          "SUMMARY:All hands", ME, ANNA, *lines))

    def test_the_documented_default_is_six(self):
        self.assertEqual(ics.max_attendees_setting(), 6)
        self.assertEqual(len(self.plan(self.all_hands(6))["proposals"]), 1)
        self.assertEqual(self.plan(self.all_hands(7))["proposals"], [])

    def test_over_the_cap_the_event_is_refused_whole_and_says_the_number(self):
        res = self.plan(self.all_hands(7))
        rep = res["report"]
        self.assertEqual(rep["events_skipped_by_reason"], {"too_many_attendees": 1})
        self.assertEqual(rep["events_skipped_too_many_attendees"], 1)
        row = rep["skipped_detail"]["too_many_attendees"][0]
        self.assertEqual((row["others_count"], row["max_attendees"]), (7, 6))
        self.assertIn("more than 6 other people", rep["summary_text"])
        # No instruction to edit a setting: the limit has no box in Settings, and a
        # sentence naming an environment variable is not something a person can act on.
        self.assertNotIn("NC_ICS_MAX_ATTENDEES", rep["summary_text"])
        # refused whole: no per-attendee noise for a forty-person all-hands
        self.assertEqual((rep["attendees_matched"], rep["attendees_unmatched"]), (0, 0))
        self.assertEqual(res["matches"][0]["matched"], [])

    def test_the_cap_is_read_at_call_time_so_settings_need_no_restart(self):
        text = self.all_hands(4)
        with mock.patch.dict(os.environ, {"NC_ICS_MAX_ATTENDEES": "2"}):
            self.assertEqual(self.plan(text)["proposals"], [])
        with mock.patch.dict(os.environ, {"NC_ICS_MAX_ATTENDEES": "9"}):
            self.assertEqual(len(self.plan(text)["proposals"]), 1)

    def test_the_cap_setting_is_clamped_not_trusted(self):
        for raw, want in (("0", 1), ("-3", 1), ("", 6), ("banana", 6), ("99999", 500)):
            with mock.patch.dict(os.environ, {"NC_ICS_MAX_ATTENDEES": raw}):
                self.assertEqual(ics.max_attendees_setting(), want, raw)


class TestWindow(IcsBase):
    def test_an_event_outside_the_window_is_refused_visibly_and_counted_once(self):
        res = self.plan(cal(vevent(
            "UID:w-old", f"DTSTART:{stamp(-2000)}T090000Z", "SUMMARY:Ancient lunch",
            ME, ANNA)))
        rep = res["report"]
        self.assertEqual(res["proposals"], [])
        # it DID match somebody, so it must not also be counted as matched
        self.assertEqual(rep["events_matched"], 0)
        self.assertEqual(rep["events_skipped_by_reason"], {"out_of_window": 1})
        self.assertEqual(res["skipped"][0]["uid"], "w-old")
        self.assertIn("days back", res["skipped"][0]["reason"])
        self.assertNotIn("NC_ICS", res["skipped"][0]["reason"])
        self.assertIn("365 days back", rep["summary_text"])

    def test_a_recurrence_override_does_not_drop_its_sibling(self):
        # A modified instance carries the SAME UID as the series, so a uid-only
        # bookkeeping key would delete both rows and unbalance the accounting.
        res = self.plan(cal(
            vevent("UID:series", f"DTSTART:{stamp(-2000)}T090000Z", "SUMMARY:Old series",
                   "RRULE:FREQ=WEEKLY", ME, ANNA),
            vevent("UID:series", f"RECURRENCE-ID:{stamp(-2000)}T090000Z",
                   f"DTSTART:{stamp(-1999)}T140000Z", "SUMMARY:Old series moved",
                   ME, ANNA)))
        rep = res["report"]
        self.assertEqual(rep["events_seen"], 2)
        self.assertEqual(rep["events_skipped_by_reason"], {"out_of_window": 2})
        self.assertEqual(rep["events_matched"], 0)

    def test_the_window_is_read_at_call_time(self):
        text = cal(vevent("UID:w-30", f"DTSTART:{stamp(30)}T090000Z", "SUMMARY:Next month",
                          ME, ANNA))
        self.assertEqual(len(self.plan(text)["proposals"]), 1)
        with mock.patch.dict(os.environ, {"NC_ICS_FORWARD_DAYS": "7"}):
            self.assertEqual(self.plan(text)["proposals"], [])
            self.assertIn("7 days ahead", self.plan(text)["report"]["summary_text"])
        with mock.patch.dict(os.environ, {"NC_ICS_BACK_DAYS": "0"}):
            past = cal(vevent("UID:w-1", f"DTSTART:{stamp(-1)}T090000Z", "SUMMARY:Yesterday",
                              ME, ANNA))
            self.assertEqual(self.plan(past)["proposals"], [])
        self.assertEqual(ics.lookback_days(), 365)
        self.assertEqual(ics.lookahead_days(), 365)

    def test_todays_event_is_inside_the_window(self):
        res = self.plan(cal(vevent("UID:w-today", f"DTSTART:{stamp(0)}T090000Z",
                                   "SUMMARY:Today", ME, ANNA)))
        self.assertEqual([p["on_date"] for p in res["proposals"]], [iso(0)])


class TestRecurrence(IcsBase):
    """The documented position: the first occurrence only, flagged recurring only
    when the CRM can actually represent the rule (annual and nothing else)."""

    def series(self, rule, uid="rec"):
        lines = ["UID:" + uid, f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Repeating", ME, ANNA]
        if rule:
            lines.append("RRULE:" + rule)
        return cal(vevent(*lines))

    def test_a_plain_yearly_rule_is_the_only_one_stored_as_recurring(self):
        res = self.plan(self.series("FREQ=YEARLY"))
        self.assertEqual([(p["on_date"], p["recurring"]) for p in res["proposals"]],
                         [(iso(3), 1)])
        rep = res["report"]
        self.assertEqual((rep["events_recurring"], rep["recurring_kept_annual"],
                          rep["recurring_flattened"]), (1, 1, 0))
        self.assertEqual(rep["recurrence_policy"], "first_occurrence_only")
        self.assertEqual(rep["recurring_detail"][0]["treated_as"], "annual repeat")

    def test_every_other_rule_lands_as_one_dated_meeting(self):
        for rule, note in (("FREQ=WEEKLY;BYDAY=MO", "weekly"),
                           ("FREQ=MONTHLY;BYSETPOS=-1;BYDAY=FR", "monthly"),
                           ("FREQ=YEARLY;INTERVAL=2", "yearly"),
                           ("FREQ=YEARLY;COUNT=3", "yearly but ends"),
                           ("FREQ=YEARLY;UNTIL=20280101T000000Z", "yearly but ends"),
                           ("FREQ=DAILY", "daily")):
            res = self.plan(self.series(rule, uid="rec-" + rule[:12]))
            rows = [(p["on_date"], p["recurring"]) for p in res["proposals"]]
            self.assertEqual(rows, [(iso(3), 0)], rule)      # the anchor, once, not annual
            rep = res["report"]
            self.assertEqual(rep["recurring_flattened"], 1, rule)
            self.assertEqual(rep["recurring_detail"][0]["treated_as"], "first date only", rule)
            self.assertIn(note, rep["recurring_detail"][0]["note"], rule)
            self.assertIn("Repeating events were imported as their first date only",
                          rep["summary_text"])

    def test_an_annual_flag_survives_db_next_occurrence_the_way_the_note_claims(self):
        # The reason only FREQ=YEARLY earns recurring=1: db.next_occurrence
        # implements annual recurrence and nothing else.
        row = self.plan(self.series("FREQ=YEARLY"))["proposals"][0]
        nxt = db.next_occurrence(row["on_date"], row["recurring"])
        self.assertEqual(nxt, iso(3))
        weekly = self.plan(self.series("FREQ=WEEKLY", uid="rec-w"))["proposals"][0]
        self.assertEqual(db.next_occurrence(weekly["on_date"], weekly["recurring"]), iso(3))

    def test_rdate_and_exdate_are_counted_and_not_applied(self):
        res = self.plan(cal(vevent(
            "UID:rec-ex", f"DTSTART:{stamp(3)}T090000Z", "RRULE:FREQ=YEARLY",
            f"EXDATE:{stamp(370)}T090000Z,{stamp(740)}T090000Z",
            f"RDATE;VALUE=DATE:{stamp(10)}", "SUMMARY:Yearly with exceptions", ME, ANNA)))
        rep = res["report"]
        self.assertEqual((rep["exdates_not_applied"], rep["rdates_not_expanded"]), (2, 1))
        self.assertEqual([p["on_date"] for p in res["proposals"]], [iso(3)])   # RDATE not added
        self.assertIn("read but not applied", rep["summary_text"])

    def test_a_recurring_series_that_started_long_ago_is_refused_out_loud(self):
        # The cost of the anchor-only policy: a standup that began three years
        # ago yields nothing. It must say so rather than quietly yield nothing.
        res = self.plan(cal(vevent(
            "UID:rec-old", f"DTSTART:{stamp(-1100)}T080000Z", "RRULE:FREQ=WEEKLY;BYDAY=MO",
            "SUMMARY:Weekly standup", ME, ANNA)))
        self.assertEqual(res["proposals"], [])
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"out_of_window": 1})
        self.assertEqual(res["report"]["events_recurring"], 1)


class TestProposedRows(IcsBase):
    def test_the_row_shape_and_its_provenance(self):
        res = self.plan(cal(vevent(
            "UID:row-1", f"DTSTART;TZID=Europe/Stockholm:{stamp(3)}T140000",
            "SUMMARY:Coffee with Anna", "LOCATION:Café Ritz", ME, ANNA), calname="Privat"))
        row = res["proposals"][0]
        self.assertEqual(row["kind"], "meeting")
        self.assertIn(row["kind"], db.DATE_KINDS)
        self.assertEqual(row["contact_id"], self.anna)
        self.assertEqual(row["contact_name"], "Anna Nilsson")
        self.assertEqual(row["on_date"], iso(3))
        self.assertEqual(row["year_known"], 1)
        self.assertEqual(row["source"], "ics")
        self.assertNotEqual(row["source"], "added")       # not hand-entered, and says so
        prov = row["provenance"]
        self.assertEqual(prov["event_uid"], "row-1")
        self.assertEqual(prov["calendar"], "Privat")
        self.assertEqual(prov["location"], "Café Ritz")
        self.assertEqual(prov["tzid"], "Europe/Stockholm")
        self.assertFalse(prov["verified"])                # an invite is evidence, not a fact
        self.assertFalse(prov["floating"])

    def test_the_label_is_clean_now_that_provenance_has_a_column(self):
        """The label carries no '(calendar)' suffix.

        The suffix was never decoration — it was the only way to stop an inferred
        row reading as a typed one while the schema could not record where a row came
        from. contact_dates.source does that now, and people.js captions a calendar
        row "Calendar", so keeping the suffix would put the provenance in two places
        and bake it into the UNIQUE(contact_id, kind, on_date, label) dedupe key.
        """
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(contact_dates)")]
        self.assertIn("source", cols)
        self.assertIn("source_uid", cols)
        row = self.plan(cal(vevent(
            "UID:row-2", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee with Anna",
            ME, ANNA)))["proposals"][0]
        self.assertEqual(row["label"], "Coffee with Anna")
        self.assertEqual(row["summary_label"], "Coffee with Anna")
        self.assertNotIn("(calendar)", row["label"])
        self.assertEqual(row["source"], ics.PROVENANCE_SOURCE)  # provenance, own field

    def test_no_stored_value_carries_a_relative_phrase(self):
        """`api_dates._when_word` renders "today"/"tomorrow"/"in 3d". Freezing one
        of those into a label would be wrong the next morning AND would change the
        UNIQUE(contact_id, kind, on_date, label) dedupe key every day."""
        res = self.plan(cal(
            vevent("UID:rel-0", f"DTSTART:{stamp(0)}T090000Z", "SUMMARY:Coffee", ME, ANNA),
            vevent("UID:rel-1", f"DTSTART:{stamp(1)}T090000Z", "SUMMARY:Lunch", ME, ANNA),
            vevent("UID:rel-3", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Dinner", ME, ANNA)))
        self.assertEqual(len(res["proposals"]), 3)
        for row in res["proposals"]:
            blob = " ".join(str(v) for v in (row["label"], row["summary_label"],
                                             row["dedupe_key"])).lower()
            for phrase in ("today", "tomorrow", "yesterday", "in 3", " days", "in 1d"):
                self.assertNotIn(phrase, blob, row["label"])

    def test_the_same_file_twice_produces_byte_identical_rows(self):
        text = cal(vevent("UID:idem", f"DTSTART:{stamp(3)}T090000Z",
                          "SUMMARY:Coffee with Anna", ME, ANNA))
        first, second = self.plan(text)["proposals"], self.plan(text)["proposals"]
        self.assertEqual(first, second)

    def test_a_proposal_is_idempotent_through_add_contact_date(self):
        """The whole point of a deterministic label: `INSERT OR IGNORE` against
        UNIQUE(contact_id, kind, on_date, label) makes a re-import a no-op. This
        writes the proposal itself — the module still never does."""
        row = self.plan(cal(vevent("UID:idem-2", f"DTSTART:{stamp(3)}T090000Z",
                                   "SUMMARY:Coffee with Anna", ME, ANNA)))["proposals"][0]
        for _ in range(2):
            db.add_contact_date(self.conn, row["contact_id"], row["kind"], row["on_date"],
                                label=row["label"], year_known=row["year_known"],
                                recurring=row["recurring"])
        self.conn.commit()
        stored = db.get_contact_dates(self.conn, row["contact_id"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["label"], "Coffee with Anna")
        self.assertEqual(stored[0]["kind"], "meeting")
        self.assertEqual(stored[0]["on_date"], iso(3))

    def test_the_label_cap_is_a_setting_read_at_call_time(self):
        text = cal(vevent("UID:cap-label", f"DTSTART:{stamp(3)}T090000Z",
                          "SUMMARY:A really quite long conversation about absolutely everything",
                          ME, ANNA))
        self.assertEqual(ics.label_max(), 60)
        self.assertEqual(len(self.plan(text)["proposals"][0]["summary_label"]), 60)
        with mock.patch.dict(os.environ, {"NC_ICS_LABEL_MAX": "12"}):
            row = self.plan(text)["proposals"][0]
            self.assertEqual(row["summary_label"], "A really qu…")
            # label and summary_label are the same value now that provenance has its
            # own column, so the cap governs both.
            self.assertEqual(row["label"], "A really qu…")

    def test_an_event_with_no_summary_still_gets_a_readable_label(self):
        row = self.plan(cal(vevent("UID:no-sum", f"DTSTART:{stamp(3)}T090000Z",
                                   ME, ANNA)))["proposals"][0]
        self.assertEqual(row["label"], "Calendar event")

    def test_two_people_on_one_invite_get_one_row_each(self):
        res = self.plan(cal(vevent(
            "UID:pair", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Three-way call", ME, ANNA,
            "ATTENDEE;CN=Björn Ek:mailto:bjorn@example.com")))
        self.assertEqual(sorted(p["contact_id"] for p in res["proposals"]),
                         sorted([self.anna, self.bjorn]))
        self.assertEqual(res["report"]["contacts_touched"], 2)
        self.assertEqual(res["report"]["events_matched"], 1)

    def test_one_person_reached_twice_is_one_row_and_a_counted_duplicate(self):
        db.add_identifier(self.conn, self.anna, "email", "anna.nilsson@work.example.com")
        self.conn.commit()
        res = self.plan(cal(vevent(
            "UID:same-person", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA,
            "ATTENDEE;CN=Anna N:mailto:anna.nilsson@work.example.com")))
        self.assertEqual([p["contact_id"] for p in res["proposals"]], [self.anna])
        rep = res["report"]
        self.assertEqual(rep["duplicate_rows_folded"], 1)
        self.assertEqual(rep["events_matched"], 1)           # a row-level fold, not an event one
        self.assertIn("duplicate", rep["summary_text"])

    def test_a_multi_day_all_day_event_keeps_only_its_first_day(self):
        # A seven-day trip becomes one date. That is the anchor policy, and since
        # NC-370 the receipt says so (see TestTheReceiptTellsTheWholeStory).
        res = self.plan(cal(vevent(
            "UID:trip", f"DTSTART;VALUE=DATE:{stamp(3)}", f"DTEND;VALUE=DATE:{stamp(10)}",
            "SUMMARY:Anna in Stockholm", ME, ANNA)))
        self.assertEqual([p["on_date"] for p in res["proposals"]], [iso(3)])
        self.assertTrue(res["proposals"][0]["provenance"]["all_day"])


class TestReport(IcsBase):
    def test_every_event_in_a_messy_file_lands_in_exactly_one_bucket(self):
        self.write_exclude("Spam Recruiter")
        spam = db.create_contact(self.conn, "Spam Recruiter")
        db.add_identifier(self.conn, spam, "email", "spam@example.com")
        self.conn.commit()
        text = cal(
            vevent("UID:r-ok", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA),
            vevent("UID:r-ghost", f"DTSTART:{stamp(4)}T090000Z", "SUMMARY:Stranger",
                   ME, "ATTENDEE;CN=Ingrid Okänd:mailto:ingrid@example.com"),
            vevent("UID:r-excl", f"DTSTART:{stamp(5)}T090000Z", "SUMMARY:Recruiter",
                   ME, "ATTENDEE;CN=Spam Recruiter:mailto:spam@example.com"),
            vevent("UID:r-cancel", f"DTSTART:{stamp(6)}T090000Z", "SUMMARY:Off",
                   "STATUS:CANCELLED", ME, ANNA),
            vevent("UID:r-solo", f"DTSTART:{stamp(7)}T090000Z", "SUMMARY:Gym"),
            vevent("UID:r-old", f"DTSTART:{stamp(-2000)}T090000Z", "SUMMARY:Ancient",
                   ME, ANNA),
            vevent("UID:r-broken", "SUMMARY:No start at all", ME, ANNA),
            vevent("UID:r-big", f"DTSTART:{stamp(8)}T090000Z", "SUMMARY:All hands", ME, ANNA,
                   *[f"ATTENDEE;CN=P{i}:mailto:p{i}@example.com" for i in range(7)]),
        )
        rep = self.plan(text)["report"]
        self.assertEqual(rep["events_seen"], 8)
        self.assertEqual(rep["events_parsed"], 7)
        self.assertEqual(rep["events_unparseable"], 1)
        self.assertEqual(rep["events_matched"], 1)
        self.assertEqual(rep["events_no_match"], 1)
        self.assertEqual(rep["events_skipped_by_reason"], {
            "all_excluded": 1, "cancelled": 1, "no_other_people": 1,
            "out_of_window": 1, "too_many_attendees": 1, "unparseable": 1})
        self.assertEqual(rep["events_skipped"], 6)
        self.assertEqual(rep["accounting"],
                         {"events_seen": 8, "explained": 8, "unexplained": 0})
        self.assertEqual(rep["proposals"], 1)
        self.assertFalse(rep["wrote_anything"])
        self.assertIn("Nothing has been written", rep["summary_text"])

    def test_the_summary_text_is_renderable_prose_with_singulars_that_read_right(self):
        rep = self.plan(cal(vevent("UID:one", f"DTSTART:{stamp(3)}T090000Z",
                                   "SUMMARY:Coffee", ME, ANNA)))["report"]
        self.assertEqual(rep["sentences"][0], "1 calendar event read.")
        self.assertEqual(rep["sentences"][1], "1 date would be added across 1 person.")
        self.assertNotIn("1 events", rep["summary_text"])
        self.assertNotIn("1 people", rep["summary_text"])
        self.assertEqual(rep["summary_text"], " ".join(rep["sentences"]))

    def test_report_survives_being_handed_nothing(self):
        rep = ics.report([], None, None)
        self.assertEqual(rep["events_seen"], 0)
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        for key in ("unparseable", "matched_events", "no_match_events", "warnings",
                    "unmatched_attendees", "duplicate_rows", "recurring_detail"):
            self.assertEqual(rep[key], [], key)

    def test_warnings_travel_with_their_event_uid(self):
        rep = self.plan(cal(vevent(
            "UID:warn", 'DTSTART;TZID="W. Europe Standard Time":' + stamp(3) + "T140000",
            "DURATION:oops", "SUMMARY:Outlook", ME, ANNA)))["report"]
        self.assertEqual({w["uid"] for w in rep["warnings"]}, {"warn"})
        self.assertEqual(len(rep["warnings"]), 2)
        self.assertEqual(rep["unknown_timezones"], 1)
        self.assertEqual(rep["floating_times"], 1)


class TestPlanPipeline(IcsBase):
    def test_plan_returns_the_four_halves_and_a_report(self):
        res = self.plan(cal(vevent("UID:p-1", f"DTSTART:{stamp(3)}T090000Z",
                                   "SUMMARY:Coffee", ME, ANNA)))
        self.assertEqual(sorted(res), ["events", "matches", "proposals", "report", "skipped"])
        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(len(res["matches"]), len(res["events"]))

    def test_proposed_dates_reports_its_refusals_through_the_out_list(self):
        events = ics.parse_events(cal(
            vevent("UID:pd-1", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Keep", ME, ANNA),
            vevent("UID:pd-2", f"DTSTART:{stamp(-2000)}T090000Z", "SUMMARY:Drop", ME, ANNA)))
        sink = []
        rows = ics.proposed_dates(self.conn, events, max_attendees=6, skipped=sink)
        self.assertEqual([r["provenance"]["event_uid"] for r in rows], ["pd-1"])
        self.assertEqual([s["uid"] for s in sink], ["pd-2"])
        self.assertEqual(sink[0]["bucket"], "out_of_window")
        # and without the out-list nothing crashes; the refusal is simply invisible,
        # which is why report() should always be fed the sink.
        self.assertEqual(len(ics.proposed_dates(self.conn, events, max_attendees=6)), 1)

    def test_match_attendees_requires_the_cap_to_be_passed_in(self):
        event = ics.parse_events(cal(vevent("UID:m-cap", f"DTSTART:{stamp(3)}T090000Z",
                                            "SUMMARY:X", ME, ANNA)))[0]
        with self.assertRaises(TypeError):
            ics.match_attendees(self.conn, event)        # no import-time default to go stale
        out = ics.match_attendees(self.conn, event, max_attendees=6)
        self.assertEqual(out["contact_ids"], [self.anna])
        for key in ("matched", "unmatched", "ignored", "contact_ids"):
            self.assertIsInstance(out[key], list)


# ─────────────────────────────────────────────────────────────────────────────
# Regression guards: four silent failures of this module. Every one keeps an
# assertion behind it, with the triggering input written out, because none of
# these fixtures is something you would think to try.
# ─────────────────────────────────────────────────────────────────────────────
class TestRegressions(IcsBase):
    def test_a_tzid_on_an_all_day_date_value_is_not_an_unknown_timezone(self):
        """Input: DTSTART;TZID=Europe/Stockholm;VALUE=DATE:<date>.

        RFC 5545 §3.2.19 says TZID must not be applied to a DATE value, so this is
        malformed — as are a missing UID, a missing DTEND and an unterminated
        VEVENT, all of which this module survives on purpose. The bug was that it
        did not merely tolerate the input, it made a false statement about it:
        `parse_dt`'s all-day branch forwarded the TZID as `raw_tzid` while
        hardcoding `tzid: ""`, and two consumers read `raw_tzid and not tzid` as
        "this computer could not resolve the zone" — the warning in `_build_event` and
        `report()["unknown_timezones"]`. A Swedish all-day event therefore claimed
        "unknown timezone 'Europe/Stockholm'", indistinguishable from the real
        Windows-TZID case those fields exist to surface.
        """
        got = ics.parse_dt("20260617", {"VALUE": ["DATE"], "TZID": ["Europe/Stockholm"]})
        self.assertTrue(got["all_day"])
        self.assertEqual(got["on_date"], "2026-06-17")
        self.assertEqual(got["raw_tzid"], "")            # not forwarded …
        self.assertEqual(got["tzid_ignored_for_date"], "Europe/Stockholm")   # … but not lost
        res = self.plan(cal(vevent(
            "UID:allday-tz", f"DTSTART;TZID=Europe/Stockholm;VALUE=DATE:{stamp(3)}",
            "SUMMARY:Anna in town", ME, ANNA)))
        ev, rep = res["events"][0], res["report"]
        self.assertEqual(ev["on_date"], iso(3))
        self.assertEqual(ev["warnings"], [],
                         "Europe/Stockholm is resolvable; no unknown-timezone warning is due")
        self.assertEqual(rep["unknown_timezones"], 0)
        self.assertNotIn("doesn't know", rep["summary_text"])
        self.assertEqual([p["on_date"] for p in res["proposals"]], [iso(3)])

    def test_nameless_attendees_are_three_people_not_one(self):
        """Input: ATTENDEE lines with a non-mailto CAL-ADDRESS and no CN.

        Legal RFC 5545 — CAL-ADDRESS is a URI and CN is optional — and what a
        privacy-scrubbed published calendar or an Apple urn:uuid roster looks
        like. `ATTENDEE:mailto:` with an empty address gets there too. The
        attendee dedupe key was `email or ("cn:" + normalize_name(name))`, which
        collapses to the constant "cn:" for anyone with neither, so every nameless
        invitee after the first was dropped inside the parser: `others_count`
        under-read (a large meeting could slip under NC_ICS_MAX_ATTENDEES),
        `attendees_unmatched` under-counted, and report() said nothing at all.
        """
        text = cal(vevent(
            "UID:scrubbed", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Scrubbed invite", ME,
            "ATTENDEE;PARTSTAT=ACCEPTED:urn:uuid:1111-aaaa",
            "ATTENDEE;PARTSTAT=ACCEPTED:urn:uuid:2222-bbbb",
            "ATTENDEE;PARTSTAT=ACCEPTED:urn:uuid:3333-cccc"))
        ev = ics.parse_events(text)[0]
        self.assertEqual(len(ev["attendees"]), 3,
                         "three distinct CAL-ADDRESSes are three people")
        res = self.plan(text)
        self.assertEqual(res["matches"][0]["others_count"], 3)
        self.assertEqual(res["report"]["attendees_unmatched"], 3)
        # and the cap now sees the real size of the room
        with mock.patch.dict(os.environ, {"NC_ICS_MAX_ATTENDEES": "2"}):
            self.assertEqual(self.plan(text)["report"]["events_skipped_by_reason"],
                             {"too_many_attendees": 1})

    def test_a_cancellation_file_is_refused_on_its_method_alone(self):
        """Input: an iTIP cancellation — METHOD:CANCEL at calendar level with no
        per-event STATUS.

        That is what the .ics attached to a "Meeting cancelled" email contains,
        and RFC 5546 §3.2.5 makes STATUS optional on a CANCEL because the METHOD
        is the authoritative signal. Those files land in ~/Downloads, which is
        exactly where importers.py's picker text points the user. `method` was
        parsed and stored but never consulted — only per-event STATUS was — so a
        meeting that was called off was proposed as a date that happened.
        """
        res = self.plan("BEGIN:VCALENDAR" + CRLF + "METHOD:CANCEL" + CRLF
                        + "PRODID:-//Microsoft Exchange//EN" + CRLF
                        + vevent("UID:cancelled-invite", "SEQUENCE:2",
                                 f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee with Anna",
                                 ME, ANNA)
                        + "END:VCALENDAR" + CRLF)
        self.assertEqual(res["events"][0]["method"], "CANCEL")
        self.assertEqual(res["events"][0]["status"], "")        # no per-event signal at all
        self.assertEqual(res["proposals"], [],
                         "a cancelled invitation must not become a contact date")
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"cancelled": 1})

    def test_a_normal_publish_feed_is_not_mistaken_for_a_cancellation(self):
        for method in ("PUBLISH", "REQUEST", ""):
            head = "BEGIN:VCALENDAR" + CRLF + (f"METHOD:{method}" + CRLF if method else "")
            res = self.plan(head + vevent(
                "UID:m-" + (method or "none"), f"DTSTART:{stamp(3)}T090000Z",
                "SUMMARY:Coffee", ME, ANNA) + "END:VCALENDAR" + CRLF)
            self.assertEqual(len(res["proposals"]), 1, method)

    def test_the_same_named_invitee_twice_is_still_one_person(self):
        # The fix above must not have cost the dedupe it was guarding.
        ev = ics.parse_events(cal(vevent(
            "UID:dedupe-still", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:One person",
            "ATTENDEE;CN=Anna Nilsson:urn:uuid:9999",
            "ATTENDEE;CN=anna   nilsson:urn:uuid:9999")))[0]
        self.assertEqual(len(ev["attendees"]), 1)


    def test_two_addressless_attendees_are_two_people(self):
        """Input: two ATTENDEE lines whose CAL-ADDRESS is empty (`ATTENDEE:`) —
        a scrubbed feed, or an exporter that wrote the parameters and forgot the
        address.

        This was the tail of the fix above. The key chain became

            key = (person["email"]
                   or ("cn:" + db.normalize_name(person["name"]) if person["name"] else "")
                   or ("addr:" + (value or "").strip().lower()))
            if not key:
                key = f"anon:{len(ev['attendees'])}"

        and for an empty CAL-ADDRESS the third branch evaluates to the string
        "addr:" — truthy — so `if not key` could never fire, the `anon:` fallback
        was dead code, and every addressless attendee shared one key exactly like
        the "cn:" bug it replaced.

        The cost was not only a count. PARTSTAT rides along with whichever
        attendee survives, so file ORDER decided the verdict: accepted-then-
        declined left an accepted invitee (a meeting), declined-then-accepted left
        a declined one (skipped as "everyone declined"). Same two people, same
        event, two different answers.
        """
        ev = ics.parse_events(cal(vevent(
            "UID:blank-att", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Two blanks",
            "ATTENDEE:", "ATTENDEE:")))[0]
        self.assertEqual(len(ev["attendees"]), 2,
                         "two ATTENDEE lines are two people; the anon: guard never fires")

        def outcome(*lines):
            return self.plan(cal(vevent("UID:order", f"DTSTART:{stamp(3)}T090000Z",
                                        "SUMMARY:Order decides", *lines)))["report"]
        accepted_first = outcome("ATTENDEE;PARTSTAT=ACCEPTED:", "ATTENDEE;PARTSTAT=DECLINED:")
        declined_first = outcome("ATTENDEE;PARTSTAT=DECLINED:", "ATTENDEE;PARTSTAT=ACCEPTED:")
        self.assertEqual(accepted_first["events_skipped_by_reason"],
                         declined_first["events_skipped_by_reason"],
                         "the order two invitees appear in must not change the verdict")


# WHAT IS STILL TRUE (documented, not asserted)
# ------------------------------------------------------------------
# NC-370 (2026-10-17) went through the ten notes that used to be listed here. Nine
# were defects and are fixed, each with a test in TestTheReceiptTellsTheWholeStory:
# the double-counting docstring, UTC-day filing, multi-day events, unreadable
# lines, `--max-attendees 0`, the second calendar's name, the room organizer, the
# escaped comma in CATEGORIES, and the "(calendar)" note that was out of date.
# Two things remain true, and neither is a defect:
#
# 1. `plan()` matches every event twice: once for `matches`, then again inside
#    proposed_dates(), and db.load_exclude_list() re-reads the file on each pass
#    (400 reads for a 200-event file). Speed only: 200 events still run in about
#    0.02 s and the answer is the same both times, so it stays until a calendar
#    big enough to feel it turns up.
# 2. The 365-day window is measured from today in UTC (`_today()`), while a
#    meeting's date is now the local day. At the very edge of the window the two
#    can differ by one day. The week view and `db.next_occurrence` use the same
#    UTC clock, so changing only this one would make the importer disagree with
#    the screen that shows its rows.


class TestTheReceiptTellsTheWholeStory(IcsBase):
    """NC-370 (2026-10-17). A day filed by the wrong clock, a trip cut to one day
    without a word, and the nine small notes that used to sit in a comment at the
    bottom of this file. Each fix has its own test here, so none of them is a
    note any more."""

    # -- the day an event is filed on -----------------------------------------
    def test_a_23_00_z_event_lands_on_the_local_day_end_to_end(self):
        """NC-370. 23:00Z is already tomorrow in Stockholm (IcsBase names it), so
        the proposal, not just the parsed event, is filed on the next day."""
        res = self.plan(cal(vevent(
            "UID:late", f"DTSTART:{stamp(3)}T230000Z", "SUMMARY:Late call", ME, ANNA)))
        self.assertEqual([p["on_date"] for p in res["proposals"]], [iso(4)])
        self.assertEqual(res["proposals"][0]["provenance"]["tzid"], "UTC")

    def test_an_event_stored_on_the_utc_day_earlier_moves_to_the_local_day(self):
        """NC-370. Rows written before the fix sit on the UTC day. Importing the
        same file again moves them, and says it moved them, rather than adding a
        second meeting."""
        db.upsert_calendar_date(self.conn, self.anna, iso(3), source_uid="late",
                                label="Late call")
        self.conn.commit()
        planned = ics.plan(self.conn, cal(vevent(
            "UID:late", f"DTSTART:{stamp(3)}T230000Z", "SUMMARY:Late call", ME, ANNA)))
        receipt = ics.apply(self.conn, planned)
        self.assertEqual((receipt["moved"], receipt["created"]), (1, 0))
        rows = self.conn.execute(
            "SELECT on_date FROM contact_dates WHERE source_uid = 'late'").fetchall()
        self.assertEqual([r["on_date"] for r in rows], [iso(4)])

    # -- events that run for several days -------------------------------------
    def test_days_covered_counts_the_days_an_event_runs(self):
        def one(*lines):
            return ics.parse_events(cal(vevent("UID:d", *lines)))[0]
        self.assertEqual(ics.days_covered(one("DTSTART;VALUE=DATE:20260617")), 1)
        self.assertEqual(ics.days_covered(one(
            "DTSTART;VALUE=DATE:20260617", "DTEND;VALUE=DATE:20260618")), 1)
        # The end of an all-day event is the day AFTER its last day.
        self.assertEqual(ics.days_covered(one(
            "DTSTART;VALUE=DATE:20260617", "DTEND;VALUE=DATE:20260620")), 3)
        # A dinner that runs past midnight is still one day...
        self.assertEqual(ics.days_covered(one(
            "DTSTART:20260617T220000Z", "DTEND:20260618T020000Z")), 1)
        # ...a conference with set hours is not.
        self.assertEqual(ics.days_covered(one(
            "DTSTART:20260617T090000Z", "DTEND:20260620T170000Z")), 3)
        self.assertEqual(ics.days_covered(one(
            "DTSTART:20260617T090000Z", "DTEND:20260617T100000Z")), 1)
        self.assertEqual(ics.days_covered({}), 1)

    def test_a_trip_that_is_cut_to_its_first_day_is_counted_and_said(self):
        """NC-370. A seven-day trip becomes one date, as it always did. The receipt
        now says so, with the count, the days it ran and a plain sentence."""
        res = self.plan(cal(vevent(
            "UID:trip", f"DTSTART;VALUE=DATE:{stamp(3)}", f"DTEND;VALUE=DATE:{stamp(10)}",
            "SUMMARY:Anna in Stockholm", ME, ANNA)))
        rep = res["report"]
        self.assertEqual([p["on_date"] for p in res["proposals"]], [iso(3)])
        self.assertEqual(rep["events_multi_day"], 1)
        self.assertEqual([(d["uid"], d["days"]) for d in rep["multi_day_detail"]],
                         [("trip", 7)])
        self.assertIn("1 event ran for more than one day", rep["summary_text"])
        self.assertIn("It is saved on its first day only", rep["summary_text"])

    def test_only_events_that_yield_a_date_are_counted_as_cut(self):
        """NC-370. A week of "Holiday" with nobody else on it leaves nothing behind,
        and a one-day event loses nothing. Neither is mentioned. A timed
        three-day conference is, and the plural reads right."""
        res = self.plan(cal(
            vevent("UID:holiday", f"DTSTART;VALUE=DATE:{stamp(3)}",
                   f"DTEND;VALUE=DATE:{stamp(10)}", "SUMMARY:Holiday"),
            vevent("UID:oneday", f"DTSTART;VALUE=DATE:{stamp(4)}",
                   f"DTEND;VALUE=DATE:{stamp(5)}", "SUMMARY:Anna in town", ME, ANNA),
            vevent("UID:party", f"DTSTART:{stamp(5)}T220000Z",
                   f"DTEND:{stamp(6)}T020000Z", "SUMMARY:Party", ME, ANNA),
            vevent("UID:conf", f"DTSTART:{stamp(6)}T090000Z",
                   f"DTEND:{stamp(9)}T170000Z", "SUMMARY:Conference", ME, ANNA),
            vevent("UID:trip", f"DTSTART;VALUE=DATE:{stamp(7)}",
                   f"DTEND;VALUE=DATE:{stamp(9)}", "SUMMARY:Weekend away", ME, ANNA)))
        rep = res["report"]
        self.assertEqual(sorted(d["uid"] for d in rep["multi_day_detail"]),
                         ["conf", "trip"])
        self.assertEqual(rep["events_multi_day"], 2)
        self.assertIn("2 events ran for more than one day", rep["summary_text"])
        self.assertIn("Each is saved on its first day only", rep["summary_text"])

    def test_an_event_that_ran_for_one_day_adds_no_sentence_about_days(self):
        rep = self.plan(cal(vevent(
            "UID:one", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)))["report"]
        self.assertEqual(rep["events_multi_day"], 0)
        self.assertNotIn("more than one day", rep["summary_text"])

    # -- note 1: the report's own contract ------------------------------------
    def test_an_unreadable_event_is_inside_the_skipped_count_not_beside_it(self):
        """NC-370, note 1. `report()`'s docstring said the unreadable events were
        added on top of the skipped ones, which counts each of them twice. Two
        events, one with no start date: seen 2, and the parts add up to 2 without
        adding `events_unparseable` a second time."""
        res = self.plan(cal(
            vevent("UID:fine", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA),
            vevent("UID:broken", "SUMMARY:No start", ME, ANNA)))
        rep = res["report"]
        self.assertEqual(rep["events_seen"], 2)
        self.assertEqual(rep["events_unparseable"], 1)
        self.assertEqual(rep["events_skipped_by_reason"], {"unparseable": 1})
        self.assertEqual(rep["events_matched"] + rep["events_no_match"]
                         + rep["events_skipped"], rep["events_seen"])
        self.assertEqual(rep["events_matched"] + rep["events_no_match"]
                         + rep["events_skipped"] + rep["events_unparseable"], 3,
                         "the old docstring's sum, which double counts")
        self.assertIn("events_seen == events_matched + events_no_match + events_skipped",
                      ics.report.__doc__)
        self.assertNotIn("sum(events_skipped)", ics.report.__doc__)

    def test_the_unreadable_events_are_counted_alone_when_matching_has_not_run(self):
        """Before matching, nothing is in `events_skipped`, so the unreadable events
        are counted on their own. A readable event nobody has matched yet is the
        one thing `unexplained` still shows, and it should."""
        broken = vevent("UID:broken", "SUMMARY:No start", ME, ANNA)
        rep = ics.report(ics.parse_events(cal(broken)))
        self.assertEqual((rep["events_seen"], rep["events_unparseable"],
                          rep["events_skipped"]), (1, 1, 0))
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        both = ics.parse_events(cal(
            vevent("UID:fine", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA),
            broken))
        self.assertEqual(ics.report(both)["accounting"]["unexplained"], 1,
                         "the readable event has not been matched yet")

    # -- note 4: lines the parser could not read ------------------------------
    def test_a_line_with_no_colon_inside_an_event_reaches_the_receipt(self):
        """NC-370, note 4. The parser counted such lines on the event and the
        report never mentioned them, so "records it rather than dropping it" stopped
        one step short of the person."""
        res = self.plan(cal(
            vevent("UID:bad-1", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee",
                   "this line has no colon at all", ME, ANNA),
            vevent("UID:bad-2", f"DTSTART:{stamp(4)}T090000Z", "SUMMARY:Lunch",
                   "neither does this", "or this one", ME, ANNA)))
        rep = res["report"]
        self.assertEqual(rep["unreadable_lines"], 3)
        self.assertEqual(rep["events_with_unreadable_lines"], 2)
        self.assertEqual([(d["uid"], d["lines"]) for d in rep["unreadable_line_detail"]],
                         [("bad-1", 1), ("bad-2", 2)])
        self.assertEqual(len(res["proposals"]), 2, "the events were still read")
        self.assertIn("3 lines in the file could not be read and were skipped. "
                      "The events around them were still read.", rep["summary_text"])

    def test_one_unreadable_line_reads_in_the_singular(self):
        rep = self.plan(cal(vevent(
            "UID:bad", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee",
            "no colon here", ME, ANNA)))["report"]
        self.assertIn("1 line in the file could not be read and was skipped. "
                      "The event around it was still read.", rep["summary_text"])

    def test_a_clean_file_says_nothing_about_unreadable_lines(self):
        rep = self.plan(cal(vevent(
            "UID:ok", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)))["report"]
        self.assertEqual(rep["unreadable_lines"], 0)
        self.assertNotIn("could not be read and", rep["summary_text"])

    # -- note 5: the attendee limit on the command line -----------------------
    def _cli(self, text, *argv):
        """Run `ics.main()` on `text` against the test database. The connection is
        a second one to the same file, because main() closes what it opens."""
        path = os.path.join(self.dir, "in.ics")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        real_get_db = db.get_db
        out, err, code = io.StringIO(), io.StringIO(), None
        with mock.patch.object(ics.db, "get_db",
                               side_effect=lambda *a, **k: real_get_db(self.path)), \
                mock.patch.object(sys, "argv", ["ics_import.py", path, *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ics.main()
            except SystemExit as exc:
                code = exc.code
        return out.getvalue(), err.getvalue(), code

    def _three_people(self):
        return cal(vevent(
            "UID:three", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Dinner", ME, ANNA,
            "ATTENDEE;CN=Björn Ek:mailto:bjorn@example.com",
            "ATTENDEE;CN=Cecilia Ny:mailto:cecilia@example.com"))

    def test_a_limit_of_zero_on_the_command_line_is_refused_in_words(self):
        """NC-370, note 5. The command line skipped the floor the setting has, so
        `--max-attendees 0` skipped every event and the receipt said "more than
        None other people". It is now refused up front, in the setting's range."""
        out, err, code = self._cli(self._three_people(), "--max-attendees", "0")
        self.assertEqual(code, 2)
        self.assertIn("from 1 to 500", err)
        self.assertIn("A limit of 0 would skip every event", err)
        self.assertEqual(out, "", "nothing may be previewed with a limit that was refused")
        for bad in ("-3", "501", "many"):
            self.assertEqual(self._cli(self._three_people(), "--max-attendees", bad)[2], 2, bad)

    def test_a_limit_on_the_command_line_is_the_one_the_receipt_names(self):
        out, err, code = self._cli(self._three_people(), "--max-attendees", "2")
        self.assertIsNone(code)
        self.assertIn("more than 2 other people on the invite", out)
        self.assertNotIn("None", out)

    def test_the_range_the_command_line_accepts_is_the_settings_range(self):
        self.assertEqual(ics.MAX_ATTENDEES_RANGE, (1, 500))
        with mock.patch.dict(os.environ, {"NC_ICS_MAX_ATTENDEES": "0"}):
            self.assertEqual(ics.max_attendees_setting(), 1)
        with mock.patch.dict(os.environ, {"NC_ICS_MAX_ATTENDEES": "9999"}):
            self.assertEqual(ics.max_attendees_setting(), 500)

    def test_a_limit_of_zero_given_to_plan_still_says_zero_not_none(self):
        """NC-370, note 5. A caller that does pass 0 gets a true sentence. 0 is a
        number; the old test for "was a limit given" treated it as missing."""
        rep = self.plan(self._three_people(), max_attendees=0)["report"]
        self.assertIn("more than 0 other people on the invite", rep["summary_text"])
        self.assertNotIn("None", rep["summary_text"])

    # -- the command line's own receipt ----------------------------------------
    def test_a_preview_says_nothing_was_written_and_shows_no_raw_numbers(self):
        out, _, code = self._cli(cal(vevent(
            "UID:pv", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)),
            "--dry-run")
        self.assertIsNone(code)
        self.assertIn("1 date would be added across 1 person.", out)
        self.assertIn("Nothing has been written. This is a proposal.", out)
        self.assertIn("Nothing was written.", out)
        self.assertNotIn("accounting=", out)
        self.assertEqual(self.counts()[1], 0)

    def test_a_run_that_wrote_does_not_also_say_it_wrote_nothing(self):
        """NC-370. After `--apply` the log said "3 dates would be added", "Nothing
        has been written. This is a proposal." and "Nothing was written." around a
        line saying the meetings WERE added."""
        out, _, code = self._cli(cal(vevent(
            "UID:ap", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)),
            "--apply")
        self.assertIsNone(code)
        self.assertIn("1 meeting added.", out)
        self.assertEqual(self.counts()[1], 1)
        for stale in ("would be added", "Nothing has been written", "Nothing was written",
                      "accounting="):
            self.assertNotIn(stale, out)
        self.assertIn("1 calendar event read.", out)

    def test_a_summary_that_does_not_add_up_says_so_in_words(self):
        """NC-370. The command line used to print the raw accounting dictionary on
        every run. It now says nothing when the numbers add up, and a plain
        sentence when they do not."""
        real = ics.report

        def broken(*a, **k):
            rep = real(*a, **k)
            rep["accounting"]["unexplained"] = 2
            return rep
        with mock.patch.object(ics, "report", side_effect=broken):
            out, _, _ = self._cli(cal(vevent(
                "UID:gap", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)),
                "--dry-run")
        self.assertIn("The summary does not account for 2 events. That is a fault in "
                      "this app, not in your file.", out)

    # -- the words a person reads ---------------------------------------------
    def test_the_receipt_uses_no_setting_names_field_names_or_acronyms(self):
        """NC-370. Every kind of sentence the receipt can print, in one run. None of
        them names an environment variable, an .ics field or an acronym: a person
        cannot act on `NC_ICS_MAX_ATTENDEES` (it has no box), and "RDATE" means
        nothing to them. This is the gate that keeps the next sentence honest too."""
        text = cal(
            vevent("UID:big", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:All hands", ME, ANNA,
                   *[f"ATTENDEE;CN=P{i}:mailto:p{i}@example.com" for i in range(7)]),
            vevent("UID:old", f"DTSTART:{stamp(-2000)}T090000Z", "SUMMARY:Ancient", ME, ANNA),
            vevent("UID:rep", f"DTSTART:{stamp(4)}T090000Z", "SUMMARY:Weekly", ME, ANNA,
                   "RRULE:FREQ=WEEKLY", f"RDATE:{stamp(11)}T090000Z",
                   f"EXDATE:{stamp(18)}T090000Z"),
            vevent("UID:tz", 'DTSTART;TZID="W. Europe Standard Time":' + stamp(5) + "T140000",
                   "SUMMARY:Outlook", ME, ANNA),
            vevent("UID:ghost", f"DTSTART:{stamp(6)}T090000Z", "SUMMARY:Stranger", ME,
                   "ATTENDEE;CN=Ingrid Okänd:mailto:ingrid@example.com"),
            vevent("UID:broken", "SUMMARY:No start", ME, ANNA),
            vevent("UID:badstart", "DTSTART:nonsense", "SUMMARY:Gibberish", ME, ANNA),
            vevent("UID:noise", f"DTSTART:{stamp(7)}T090000Z", "SUMMARY:Noise",
                   "no colon on this line", ME, ANNA),
            vevent("UID:trip", f"DTSTART;VALUE=DATE:{stamp(8)}",
                   f"DTEND;VALUE=DATE:{stamp(12)}", "SUMMARY:Trip", ME, ANNA))
        rep = self.plan(text)["report"]
        shown = [rep["summary_text"]] + [u["reason"] for u in rep["unparseable"]] + [
            r["reason"] for rows in rep["skipped_detail"].values() for r in rows]
        for needle in ("NC_", "RDATE", "EXDATE", "DTSTART", "RRULE", "CRM", "VEVENT",
                       "VCALENDAR", "out_of_window", "too_many_attendees"):
            for line in shown:
                self.assertNotIn(needle, line)
        # and it did print every kind of sentence the test is guarding
        text_out = rep["summary_text"]
        for phrase in ("more than 6 other people", "window of 365 days back",
                       "added to repeating events", "timezone name", "not in your contacts",
                       "ran for more than one day", "could not be read and was skipped",
                       "first date only"):
            self.assertIn(phrase, text_out)

    # -- note 6: more than one calendar in a file ------------------------------
    def test_each_event_carries_the_name_and_producer_of_its_own_calendar(self):
        text = (cal(vevent("UID:a", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:A", ME, ANNA),
                    calname="Work", prodid="-//A//EN")
                + cal(vevent("UID:b", f"DTSTART:{stamp(4)}T090000Z", "SUMMARY:B", ME, ANNA),
                      calname="Privat", prodid="-//B//EN"))
        res = self.plan(text)
        self.assertEqual([p["provenance"]["calendar"] for p in res["proposals"]],
                         ["Work", "Privat"])
        self.assertEqual([p["provenance"]["prodid"] for p in res["proposals"]],
                         ["-//A//EN", "-//B//EN"])

    def test_a_cancellation_in_the_second_calendar_cancels_only_its_events(self):
        """NC-370, note 6. The note called this provenance only. It was not: the
        METHOD travelled the same way, so a cancelled meeting in the second
        calendar was read as an ordinary one and written as a meeting that
        never happened. And a cancelled FIRST calendar cancelled the second's
        events too."""
        def with_method(method, uid, day):
            return cal(vevent(f"UID:{uid}", f"DTSTART:{stamp(day)}T090000Z",
                              f"SUMMARY:{uid}", ME, ANNA)).replace(
                "VERSION:2.0" + CRLF, "VERSION:2.0" + CRLF + f"METHOD:{method}" + CRLF)
        res = self.plan(with_method("PUBLISH", "kept", 3) + with_method("CANCEL", "off", 4))
        self.assertEqual([p["provenance"]["event_uid"] for p in res["proposals"]], ["kept"])
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"cancelled": 1})
        res = self.plan(with_method("CANCEL", "off", 3) + with_method("PUBLISH", "kept", 4))
        self.assertEqual([p["provenance"]["event_uid"] for p in res["proposals"]], ["kept"])
        self.assertEqual(res["report"]["events_skipped_by_reason"], {"cancelled": 1})

    def test_the_header_returned_is_still_the_first_calendars(self):
        header, events = ics.parse_calendar(
            cal(vevent("UID:a", f"DTSTART:{stamp(3)}T090000Z"), calname="Work")
            + cal(vevent("UID:b", f"DTSTART:{stamp(4)}T090000Z"), calname="Privat"))
        self.assertEqual(header["X-WR-CALNAME"], "Work")
        self.assertEqual(len(events), 2)

    def test_a_calendar_that_never_closed_its_event_does_not_lend_it_the_next_header(self):
        text = ("BEGIN:VCALENDAR" + CRLF + "X-WR-CALNAME:First" + CRLF
                + "BEGIN:VEVENT" + CRLF + "UID:open" + CRLF
                + f"DTSTART:{stamp(3)}T090000Z" + CRLF
                + cal(vevent("UID:next", f"DTSTART:{stamp(4)}T090000Z"), calname="Second"))
        events = ics.parse_events(text)
        by_uid = {e["uid"]: e for e in events}
        self.assertEqual(by_uid["open"]["calendar"], "First")
        self.assertEqual(by_uid["next"]["calendar"], "Second")
        self.assertFalse(by_uid["open"]["parsed"])
        self.assertIn("next calendar began", by_uid["open"]["error"])

    # -- note 8: a room that sent the invite ----------------------------------
    def test_a_room_that_sent_the_invite_is_on_the_ignored_list_like_an_invited_room(self):
        """NC-370, note 8. The verdict was already right: a room is not a person.
        The receipt now lists it, as it does for a ROOM that was invited."""
        rep = self.plan(cal(vevent(
            "UID:roomorg", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Room booking",
            "ORGANIZER;CUTYPE=ROOM;CN=Rum 3:mailto:rum3@example.com", ANNA)))["report"]
        self.assertEqual(rep["attendees_ignored_by_reason"], {"cutype ROOM": 1})

    def test_a_room_that_is_organizer_and_attendee_is_listed_once(self):
        rep = self.plan(cal(vevent(
            "UID:roomboth", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Room booking",
            "ORGANIZER;CUTYPE=ROOM;CN=Rum 3:mailto:rum3@example.com",
            "ATTENDEE;CUTYPE=ROOM;CN=Rum 3:mailto:rum3@example.com", ANNA)))["report"]
        self.assertEqual(rep["attendees_ignored_by_reason"], {"cutype ROOM": 1})

    def test_you_as_organizer_are_listed_as_you_and_not_as_a_room(self):
        rep = self.plan(cal(vevent(
            "UID:personorg", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME, ANNA)
        ))["report"]
        self.assertEqual(rep["attendees_ignored_by_reason"], {"you": 1})

    # -- note 9: an escaped comma in CATEGORIES --------------------------------
    def test_an_escaped_comma_in_a_category_is_one_category(self):
        """NC-370, note 9. `Work\\,Life` is one category with a comma in it. It was
        un-escaped first and then split, which made two."""
        def cats(value):
            return ics.parse_events(cal(vevent(
                "UID:cat", f"DTSTART:{stamp(3)}T090000Z", f"CATEGORIES:{value}")))[0]["categories"]
        self.assertEqual(cats("Work,Coffee"), ["Work", "Coffee"])
        self.assertEqual(cats("Work\\,Life,Coffee"), ["Work,Life", "Coffee"])
        self.assertEqual(cats("a\\\\,b"), ["a\\", "b"])      # an escaped backslash, then a split
        self.assertEqual(cats("One\\;Two"), ["One;Two"])
        self.assertEqual(cats(" , ,Solo, "), ["Solo"])
        self.assertEqual(cats(""), [])

    # -- note 10 ----------------------------------------------------------------
    def test_a_summary_that_already_says_calendar_is_stored_as_written(self):
        """NC-370, note 10. The note said a SUMMARY ending in "(calendar)" came out
        as "Sync (calendar) (calendar)". Nothing appends that suffix any more, so
        the label is the summary, whatever it ends with."""
        res = self.plan(cal(vevent(
            "UID:sync", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Sync (calendar)", ME, ANNA)))
        self.assertEqual(res["proposals"][0]["label"], "Sync (calendar)")
        ics.apply(self.conn, res)
        row = self.conn.execute(
            "SELECT label FROM contact_dates WHERE source_uid = 'sync'").fetchone()
        self.assertEqual(row["label"], "Sync (calendar)")


class TestApplyWritesHonestly(IcsBase):
    """apply() is the only function in this module that writes. Everything above it
    is read-only so a preview can never write by accident."""

    def _ics(self, day_offset, uid="meet-1", summary="Coffee with Anna"):
        return cal(vevent(f"UID:{uid}", f"SUMMARY:{summary}",
                          f"DTSTART;TZID=Europe/Stockholm:{stamp(day_offset)}T090000",
                          ME, ANNA))

    def _rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT contact_id, on_date, label, source, source_uid FROM contact_dates")]

    def test_it_creates_a_row_with_its_provenance(self):
        r = ics.apply(self.conn, ics.plan(self.conn, self._ics(3)))
        self.assertEqual(r["created"], 1)
        row = self._rows()[0]
        self.assertEqual(row["source"], "calendar")
        self.assertEqual(row["source_uid"], "meet-1")
        self.assertEqual(row["label"], "Coffee with Anna")   # no "(calendar)" suffix

    def test_re_importing_the_same_file_changes_nothing(self):
        """Invariant 3: re-importing a source must never double rows."""
        ics.apply(self.conn, ics.plan(self.conn, self._ics(3)))
        r = ics.apply(self.conn, ics.plan(self.conn, self._ics(3)))
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(r["created"], 0)
        self.assertEqual(r["unchanged"], 1)

    def test_a_rescheduled_event_moves_instead_of_duplicating(self):
        """The reason a UID key was needed. INSERT OR IGNORE on
        UNIQUE(contact_id, kind, on_date, label) is idempotent only while the date
        never changes — move the meeting and it gives you two meetings."""
        ics.apply(self.conn, ics.plan(self.conn, self._ics(3)))
        r = ics.apply(self.conn, ics.plan(self.conn, self._ics(9)))
        self.assertEqual(len(self._rows()), 1, "one meeting, on its new date")
        self.assertEqual(r["moved"], 1)
        self.assertEqual(self._rows()[0]["on_date"], iso(9))

    def test_it_never_overwrites_a_date_the_user_entered(self):
        """Invariant 4's precedence rule applied to dates: a later automatic pass
        must never overwrite a better source."""
        ics.apply(self.conn, ics.plan(self.conn, self._ics(3)))
        self.conn.execute("UPDATE contact_dates SET source='manual', label='MY OWN NOTE'")
        self.conn.commit()
        r = ics.apply(self.conn, ics.plan(self.conn, self._ics(9)))
        self.assertEqual(r["refused_manual"], 1)
        self.assertEqual(r["created"], 0)
        self.assertEqual(r["moved"], 0)
        row = self._rows()[0]
        self.assertEqual(row["label"], "MY OWN NOTE", "the user's text survives")
        self.assertEqual(row["on_date"], iso(3), "and the user's date survives")

    def test_it_creates_nobody(self):
        """Invariant 1. A calendar invite is not evidence someone belongs in the CRM."""
        before = self.conn.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
        text = cal(vevent("UID:stranger", f"DTSTART:{stamp(3)}T090000Z",
                          "SUMMARY:Coffee", ME,
                          "ATTENDEE;CN=Nobody Known:mailto:nobody@nowhere.example"))
        ics.apply(self.conn, ics.plan(self.conn, text))
        after = self.conn.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
        self.assertEqual(before, after)

    def test_the_receipt_reports_what_happened_not_what_was_asked(self):
        ics.apply(self.conn, ics.plan(self.conn, self._ics(3)))
        self.conn.execute("UPDATE contact_dates SET source='manual'")
        self.conn.commit()
        r = ics.apply(self.conn, ics.plan(self.conn, self._ics(3)))
        self.assertEqual(r["proposed"], 1)          # one row was offered
        self.assertEqual(r["created"], 0)           # and none was written
        self.assertIn("kept your own version", r["summary_text"])
        self.assertEqual(r["contacts_touched"], 0)

    def test_planning_alone_writes_nothing(self):
        """The read/write split is the safety property; assert it rather than trust it."""
        ics.plan(self.conn, self._ics(3))
        self.assertEqual(self._rows(), [])


class TestIdentityKeyIsOneDefinition(IcsBase):
    """The identity key ("how well does this line identify a human") existed twice,
    inline, and the two copies disagreed. Both evaluated to the constant "cn:" for
    anyone with neither an email nor a CN, which is legal RFC 5545 — a CAL-ADDRESS
    is a URI and CN is optional, so an Apple urn:uuid or privacy-scrubbed calendar
    looks exactly like that."""

    def test_a_nameless_organizer_is_not_dropped(self):
        """The organizer check compared its collapsed "cn:" against the attendees'
        collapsed "cn:", matched, and dropped the organizer with no counter and no
        line in report(). On an invite where someone else organised and only you
        are listed, that removes the one person the meeting is about."""
        text = cal(vevent(
            "UID:nameless-org", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee",
            "ORGANIZER:urn:uuid:organiser-9999",
            "ATTENDEE;PARTSTAT=ACCEPTED:urn:uuid:attendee-1111"))
        ev = ics.parse_events(text)[0]
        roster = ics._roster(ev)
        self.assertEqual(len(roster), 2, "the organizer and the attendee are two people")
        self.assertIn("organizer", [p["kind"] for p in roster])

    def test_an_organizer_who_is_also_an_attendee_is_one_person(self):
        """The dedupe still has to work — this is the case the collapsing key got
        right by accident, and it must survive the fix."""
        text = cal(vevent(
            "UID:dup-org", f"DTSTART:{stamp(3)}T090000Z", "SUMMARY:Coffee", ME,
            "ATTENDEE;CN=Sam Berg;PARTSTAT=ACCEPTED:mailto:me@example.com", ANNA))
        ev = ics.parse_events(text)[0]
        self.assertEqual(len(ics._roster(ev)), 2, "me once, Anna once")

    def test_identity_key_returns_empty_when_nothing_identifies_a_person(self):
        """"" is the load-bearing return value: it forces the caller to decide what
        to do about an unidentifiable person instead of a truthy placeholder
        deciding silently."""
        self.assertEqual(ics._identity_key({"email": "", "name": "", "raw": ""}), "")
        self.assertEqual(ics._identity_key({"email": "", "name": "", "raw": "   "}), "")
        self.assertTrue(ics._identity_key({"email": "", "name": "", "raw": "urn:uuid:x"}))
        self.assertTrue(ics._identity_key({"email": "", "name": "Anna", "raw": ""}))
        self.assertTrue(ics._identity_key({"email": "a@b.c", "name": "", "raw": ""}))


class TestFetchNeverLeaksTheUrl(IcsBase):
    """A calendar URL is a bearer credential: whoever holds a Google secret address
    can read that calendar. Every error message in fetch_calendar_ics names only the
    HOST, deliberately, because the secret is in the path.

    http.client.InvalidURL — raised for a URL with a stray space, i.e. exactly what
    a bad paste produces — subclasses HTTPException, which is neither OSError nor
    URLError, so unless it has its own except clause it escapes all the others,
    reaches app.py's handler and is returned to the browser as str(exc): the whole
    secret URL, in a response body.
    """

    SECRET = "SUPERSECRETCALENDARTOKEN"

    def _fetch_with(self, url):
        import api_import
        from api_core import ApiError
        with mock.patch.dict(os.environ, {"NC_ICS_FETCH": "1",
                                          "NC_ICS_GOOGLE_URL": url}):
            try:
                api_import.fetch_calendar_ics("calendar_google")
            except ApiError as exc:
                return str(getattr(exc, "message", exc))
            except Exception as exc:      # noqa: BLE001 — an escape IS the failure
                return f"UNCAUGHT {type(exc).__name__}: {exc}"
        return ""

    def test_a_url_with_a_stray_space_does_not_echo_the_token(self):
        msg = self._fetch_with(
            f"https://calendar.google.com/calendar/ical/{self.SECRET} x/basic.ics")
        self.assertNotIn(self.SECRET, msg)
        self.assertNotIn("UNCAUGHT", msg)   # nothing may escape to app.py's str(exc)

    def test_a_url_with_a_control_character_does_not_echo_the_token(self):
        msg = self._fetch_with(
            f"https://calendar.google.com/calendar/ical/{self.SECRET}\n/basic.ics")
        self.assertNotIn(self.SECRET, msg)
        self.assertNotIn("UNCAUGHT", msg)

    def test_a_non_https_scheme_is_refused_without_echoing_the_token(self):
        msg = self._fetch_with(f"ftp://example.com/{self.SECRET}.ics")
        self.assertNotIn(self.SECRET, msg)
        self.assertNotIn("UNCAUGHT", msg)


if __name__ == "__main__":
    unittest.main()


class TestTheCalendarRowsSayWhatIsPossible(unittest.TestCase):
    """NC-031. The calendar rows on the Data page have to say what is possible:
    can the app keep itself up to date from Apple Calendar, and can a link from
    Google Calendar or a similar service stand in for a file.

    Both were already possible in the code and neither was said on the screen.
    Apple Calendar is read through the sync helper, which holds Full Disk Access.
    The link route sat two rows below the file export, where it was easy to miss.


    So the gate is on the two things a reader needs: the self-updating rows come
    first, and the row that cannot self-update says why there is no third option.
    Nothing here tests parsing; it tests what the page tells the reader."""

    def setUp(self):
        import importers
        self.registry = importers.REGISTRY
        self.spec = importers.REGISTRY["calendar"]

    def test_the_rows_are_ordered_by_how_little_they_ask_of_you(self):
        """Ordered by cost to the reader, not by age.

        `calendar_local` needs nothing typed at all; the two links need a secret
        address pasted and a switch found; the export needs a fresh file every
        time you want to be current. So: helper, links, file.
        """
        order = [k for k in self.registry if k.startswith("calendar")]
        self.assertEqual(
            order,
            ["calendar_local", "calendar_google", "calendar_icloud", "calendar"],
            "the calendar rows are not ordered by how much they ask of the user")

    def test_the_file_row_points_at_apple_calendar_instead_of_denying_it(self):
        """The fallback row must not deny that Apple Calendar can be read.

        A sentence like "There is no 'read my Apple Calendar' option", resting on
        the fact that `~/Library/Calendars` does not exist, would be wrong: that is
        the *pre-Ventura* location (before macOS 13), and the store has moved into
        a group container. NC-042 now reads it, so such copy would sit directly
        above the row providing the thing it denied — a test actively holding a
        lie in place is the worst kind.

        What is worth pinning is the shape: the fallback row must send the
        reader upward, and it must not resurrect the claim.
        """
        help_text = self.spec.export_help
        self.assertIn("Apple Calendar", help_text,
                      "the row never mentions the thing that was actually asked for")
        self.assertNotIn('There is no "read my Apple Calendar" option', help_text,
                         "the fallback row denies the existence of the row above it")
        self.assertIn("rows above", help_text,
                      "the fallback does not send the reader to the better routes")

    def test_no_calendar_row_still_claims_the_pre_ventura_path_is_the_wall(self):
        """The claim, wherever it appears. A version of "nothing reads
        ~/Library/Calendars, those need permissions this app will never ask for"
        is true about the path, and was used to argue something false about the
        machine. Any row may still NAME the path; none may present it
        as the reason the feature cannot exist."""
        for key in ("calendar", "calendar_google", "calendar_icloud"):
            with self.subTest(key=key):
                text = self.registry[key].export_help
                self.assertNotIn(
                    "those need permissions this app will never ask for", text,
                    "this row still argues from the old path to 'no such route'")
        # And the row that DOES provide it has to say what it needs instead.
        local = self.registry["calendar_local"].export_help
        self.assertIn("Full Disk Access", local,
                      "the Apple Calendar row does not say what it needs")
        self.assertIn("never holds it", local,
                      "it does not say the app itself stays without the permission")

    def test_the_file_row_is_named_as_the_fallback_it_is(self):
        """It was called "Calendar file (.ics)" — a file extension, which says
        nothing about why you would want it or what it costs you."""
        self.assertEqual(self.spec.label, "Calendar, exported by hand")
        self.assertNotIn(".ics)", self.spec.label)

    def test_no_row_points_at_the_old_name_or_the_wrong_direction(self):
        """The link rows must not send the reader to "Calendar file (.ics)
        above": the row is named differently and sits below them. NC-291: nor may
        they use the registry's own label ("Calendar, exported by hand below"),
        because the Data page names that row "Calendar file". They use the name
        on screen."""
        for key in ("calendar_google", "calendar_icloud"):
            with self.subTest(key=key):
                text = self.registry[key].export_help
                self.assertNotIn("Calendar file (.ics)", text)
                self.assertIn("**Calendar file** row", text)
