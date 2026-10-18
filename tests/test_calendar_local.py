"""NC-042: Apple Calendar's own store → contact_dates, with nothing to paste.

Every test builds a synthetic Calendar.sqlitedb. None of them read a real one:
a real calendar store lives in another app's protected folder as far as macOS is
concerned, so a suite that reads it both needs a permission it should not have
and depends on whatever happens to be in someone's diary this week.

The bug worth reading this file for is TestTheOwnerIsNeverAContact. Everything
else is translation.
"""

import os
import sqlite3
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import calendar_local_import as loc  # noqa: E402
import ics_import  # noqa: E402

# Core Data epoch. Written out rather than imported so a change to the constant
# in the module cannot make these fixtures agree with it by definition.
EPOCH = 978_307_200

SCHEMA = """
CREATE TABLE CalendarItem (
    ROWID INTEGER PRIMARY KEY, summary TEXT, start_date REAL, end_date REAL,
    start_tz TEXT, end_tz TEXT, all_day INTEGER DEFAULT 0, status INTEGER DEFAULT 1,
    UUID TEXT, unique_identifier TEXT, calendar_id INTEGER, organizer_id INTEGER,
    self_attendee_id INTEGER, orig_date REAL, invitation_status INTEGER,
    external_tracking_status INTEGER);
CREATE TABLE Participant (
    ROWID INTEGER PRIMARY KEY, owner_id INTEGER, email TEXT, status INTEGER DEFAULT 1,
    type INTEGER DEFAULT 1, role INTEGER DEFAULT 1, is_self INTEGER DEFAULT 0,
    identity_id INTEGER, entity_type INTEGER, pending_status INTEGER,
    external_rep BLOB, UUID TEXT, phone_number TEXT, comment TEXT,
    schedule_agent INTEGER, flags INTEGER);
CREATE TABLE Identity (ROWID INTEGER PRIMARY KEY, display_name TEXT);
CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT, flags INTEGER,
                       last_sync_title TEXT);
CREATE TABLE Recurrence (ROWID INTEGER PRIMARY KEY, owner_id INTEGER,
                         frequency INTEGER);
"""


class Store:
    """A synthetic Calendar.sqlitedb, built one event at a time."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="cal_store_")
        self.path = os.path.join(self.dir, "Calendar.sqlitedb")
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO Calendar (ROWID, title) VALUES (1, 'Work')")
        self._item = 0
        self._part = 0
        self._ident = 0

    def close(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def event(self, summary, days_from_now, *, all_day=0, status=1,
              uuid=None, recurring=False):
        import datetime
        self._item += 1
        when = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=days_from_now))
        core = when.timestamp() - EPOCH
        self.conn.execute(
            """INSERT INTO CalendarItem
                 (ROWID, summary, start_date, end_date, start_tz, all_day,
                  status, UUID, calendar_id)
               VALUES (?, ?, ?, ?, 'UTC', ?, ?, ?, 1)""",
            (self._item, summary, core, core + 3600, all_day, status,
             uuid if uuid is not None else f"uid-{self._item}"))
        if recurring:
            self.conn.execute(
                "INSERT INTO Recurrence (owner_id, frequency) VALUES (?, 1)",
                (self._item,))
        self.conn.commit()
        return self._item

    def attendee(self, item, email, *, name="", is_self=0, status=1, type=1,
                 role=1, organizer=False):
        self._part += 1
        ident = None
        if name:
            self._ident += 1
            ident = self._ident
            self.conn.execute(
                "INSERT INTO Identity (ROWID, display_name) VALUES (?, ?)",
                (ident, name))
        self.conn.execute(
            """INSERT INTO Participant
                 (ROWID, owner_id, email, status, type, role, is_self, identity_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._part, item, email, status, type, role, is_self, ident))
        if organizer:
            self.conn.execute("UPDATE CalendarItem SET organizer_id = ? WHERE ROWID = ?",
                              (self._part, item))
        self.conn.commit()
        return self._part


class CalBase(Base):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self._env = {k: os.environ.get(k) for k in ("SELF_EMAIL", "SELF_NAME")}
        os.environ["SELF_EMAIL"] = "me@one.example"
        os.environ["SELF_NAME"] = "Me Myself"

    def tearDown(self):
        self.store.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def read(self):
        return loc.read_events(self.store.path)


class TestTheOwnerIsNeverAContact(CalBase):
    """A calendar must never turn the owner into one of their own contacts.

    Without a filter here, some proposals would be "meetings" between the owner
    and themselves, written onto the owner's own contact page with dates and
    labels, and it would be quiet. ics_import has a self-filter, but it tests
    SELF_EMAIL — ONE address — and a calendar knows every address you get invited
    at, so SELF_EMAIL is only one of them.

    And the obvious fix is not enough either: the same address is flagged on some
    events and not on others, because Apple only marks "you" on events from an
    account that knows your identity. The leaking rows come from an address that
    IS flagged elsewhere in the same table.
    """

    def test_an_address_flagged_anywhere_is_the_owner_everywhere(self):
        """The actual defect, reproduced exactly: flagged on one event, not on
        another, and the unflagged one is the one that used to leak through."""
        a = self.store.event("Flagged here", 1)
        self.store.attendee(a, "owner@two.example", is_self=1)
        b = self.store.event("Not flagged here", 2)
        self.store.attendee(b, "owner@two.example", is_self=0)
        self.store.attendee(b, "real@person.example", name="Real Person")

        events = {e["summary"]: e for e in self.read()}
        self.assertEqual(
            [p["email"] for p in events["Not flagged here"]["attendees"]],
            ["real@person.example"],
            "an address the store flags as the owner elsewhere was treated as a "
            "contact here — this is the self-meeting bug")
        self.assertEqual(events["Flagged here"]["attendees"], [])

    def test_self_email_alone_would_not_have_caught_it(self):
        """Why the fix lives here and not in ics_import: the leaking address is
        not the one SELF_EMAIL knows, and no amount of self-filtering downstream
        can learn an address nobody told it about."""
        ev = self.store.event("Second address", 1)
        self.store.attendee(ev, "owner@two.example", is_self=1)
        other = self.store.event("Same address, unflagged", 2)
        self.store.attendee(other, "owner@two.example")

        self.assertNotEqual(os.environ["SELF_EMAIL"], "owner@two.example",
                            "fixture must use an address SELF_EMAIL does not know")
        got = {e["summary"]: e for e in self.read()}
        self.assertEqual(got["Same address, unflagged"]["attendees"], [])

    def test_the_organizer_is_filtered_too(self):
        """A meeting you called has you as ORGANIZER, and ics_import._roster folds
        the organizer in with the attendees on purpose. Filtering only attendees
        would put every meeting the owner organised back on their own record by
        the other route."""
        flagged = self.store.event("Learns the address", 1)
        self.store.attendee(flagged, "owner@two.example", is_self=1)
        ev = self.store.event("I called this one", 2)
        self.store.attendee(ev, "owner@two.example", organizer=True)
        self.store.attendee(ev, "guest@person.example", name="Guest")

        got = {e["summary"]: e for e in self.read()}["I called this one"]
        self.assertIsNone(got["organizer"],
                          "the owner came through as the organizer")
        self.assertEqual([p["email"] for p in got["attendees"]],
                         ["guest@person.example"])

    def test_a_flagged_participant_with_no_email_is_still_the_owner(self):
        """The case that makes the flag load-bearing rather than redundant.

        `_own_addresses` is built from emails, so a flagged row carrying no
        address contributes nothing to it and can only be caught by the flag
        itself. Without this, deleting the flag check passes cleanly — every
        other fixture's flagged participant also had an email.
        """
        ev = self.store.event("Owner has no address here", 1)
        self.store.attendee(ev, "", name="Me Myself", is_self=1)
        self.store.attendee(ev, "guest@person.example", name="Guest")
        got = self.read()[0]
        self.assertEqual([p["email"] for p in got["attendees"]],
                         ["guest@person.example"],
                         "a participant the store flags as the owner came "
                         "through as a contact because it had no email")

    def test_nothing_is_dropped_when_the_store_flags_nobody(self):
        """Degradation matters: on a store where Apple has flagged nothing, this
        must be a no-op and leave ics_import's SELF_EMAIL test doing its job. A
        filter that silently empties every event on an unfamiliar store would be
        worse than the bug it fixes."""
        ev = self.store.event("Nobody flagged", 1)
        self.store.attendee(ev, "a@person.example", name="A")
        self.store.attendee(ev, "b@person.example", name="B")
        got = self.read()[0]
        self.assertEqual(len(got["attendees"]), 2)


class TestTheTranslationIsWhatIcsImportExpects(CalBase):
    """This module's only other job: rows → the dict shape ics_import documents.
    A missing key there is not a crash, it is a `.get()` somewhere downstream
    quietly reading None."""

    def test_every_key_blank_event_guarantees_is_present(self):
        self.store.event("Anything", 1)
        got = self.read()[0]
        for key in ics_import._blank_event():
            self.assertIn(key, got, f"event is missing {key!r}")

    def test_attendee_dicts_carry_every_key_ics_person_does(self):
        ev = self.store.event("With people", 1)
        self.store.attendee(ev, "someone@person.example", name="Some One")
        got = self.read()[0]["attendees"][0]
        reference = ics_import._person("ATTENDEE", {}, "mailto:x@y.example",
                                      "attendee")
        self.assertEqual(sorted(got), sorted(reference),
                         "the attendee shape has drifted from ics_import._person")

    def test_core_data_timestamps_land_on_the_right_day(self):
        import datetime
        self.store.event("Tomorrow", 1)
        got = self.read()[0]
        want = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(got["on_date"], want)
        self.assertEqual(got["start"].tzinfo, datetime.timezone.utc)

    def test_the_eventkit_enums_map_to_their_ical_spellings(self):
        ev = self.store.event("Enums", 1)
        self.store.attendee(ev, "room@rooms.example", type=2)
        self.store.attendee(ev, "declined@person.example", status=3)
        self.store.attendee(ev, "chair@person.example", role=3)
        by_email = {p["email"]: p for p in self.read()[0]["attendees"]}
        self.assertEqual(by_email["room@rooms.example"]["cutype"], "ROOM")
        self.assertEqual(by_email["declined@person.example"]["partstat"], "DECLINED")
        self.assertEqual(by_email["chair@person.example"]["role"], "CHAIR")

    def test_an_unknown_enum_value_fails_open(self):
        """Many participants in a store have type 0, "unknown", so this is the
        common path. Including a stranger costs a row that matches no
        contact and vanishes; excluding a real person loses a meeting silently."""
        ev = self.store.event("Unknown enums", 1)
        self.store.attendee(ev, "who@person.example", type=0, status=0, role=0)
        p = self.read()[0]["attendees"][0]
        self.assertEqual(p["cutype"], "INDIVIDUAL")
        self.assertEqual(p["partstat"], "NEEDS-ACTION")
        self.assertEqual(p["role"], "REQ-PARTICIPANT")

    def test_a_cancelled_event_is_marked_not_dropped(self):
        """ics_import refuses CANCELLED itself and counts it in report(). Dropping
        it here would make the two disagree about how many events the file had."""
        self.store.event("Called off", 1, status=3)
        got = self.read()[0]
        self.assertEqual(got["status"], "CANCELLED")

    def test_a_missing_uuid_is_synthesised_and_flagged(self):
        """The uid is what makes a re-import idempotent, so a made-up one must
        never pass for a real one."""
        self.store.event("No uuid", 1, uuid="")
        got = self.read()[0]
        self.assertTrue(got["uid"], "no uid at all breaks dedupe")
        self.assertTrue(got["uid_synthetic"])

    def test_recurrence_and_all_day_come_through(self):
        self.store.event("Repeats", 1, recurring=True)
        self.store.event("Whole day", 2, all_day=1)
        got = {e["summary"]: e for e in self.read()}
        self.assertTrue(got["Repeats"]["is_recurring"])
        self.assertTrue(got["Whole day"]["all_day"])
        self.assertFalse(got["Repeats"]["all_day"])


class TestTheWindowIsTheSameOneTheIcsPathUses(CalBase):
    def test_events_outside_the_window_are_not_read_at_all(self):
        """A WHERE clause, not a filter downstream: a store's dates can span
        centuries, because rows that never had a date carry a sentinel."""
        self.store.event("Ancient", -4000)
        self.store.event("Far future", 4000)
        self.store.event("Now-ish", 1)
        got = [e["summary"] for e in self.read()]
        self.assertEqual(got, ["Now-ish"])

    def test_the_window_honours_the_ics_settings(self):
        self.store.event("Fifty days out", 50)
        self.assertEqual(loc.read_events(self.store.path, forward_days=10), [])
        self.assertEqual(
            len(loc.read_events(self.store.path, forward_days=100)), 1)


class TestItNeverWritesToTheStore(CalBase):
    def test_the_connection_is_read_only(self):
        """Behavioural, not textual.

        Grepping the source for "mode=ro" would still pass with the URI deleted
        entirely, because the phrase also appears in the module docstring, which
        is not a # comment. That is a gate satisfied by the code's own prose.
        """
        self.store.event("Anything", 1)
        seen = {}
        real = sqlite3.connect

        def spy(target, *a, **kw):
            seen["target"] = target
            seen["uri"] = kw.get("uri", False)
            return real(target, *a, **kw)

        loc.sqlite3.connect = spy
        try:
            self.read()
        finally:
            loc.sqlite3.connect = real
        self.assertTrue(seen.get("uri"),
                        "not opened as a URI, so mode=ro cannot apply")
        self.assertIn("mode=ro", str(seen.get("target", "")),
                      f"the store was opened writable: {seen.get('target')!r}")

    def test_a_read_only_connection_really_refuses_a_write(self):
        """Prove the flag does what it is spelled to do, rather than trusting the
        spelling. The file is a live database the helper may replace under us."""
        self.store.event("Anything", 1)
        conn = sqlite3.connect(f"file:{self.store.path}?mode=ro", uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE CalendarItem SET summary = 'nope'")
        finally:
            conn.close()

    def test_no_write_statement_appears_in_the_module(self):
        with open(loc.__file__, encoding="utf-8") as fh:
            code = "\n".join(ln for ln in fh.read().splitlines()
                             if not ln.lstrip().startswith("#"))
        for write in ("INSERT INTO", "UPDATE CalendarItem", "DELETE FROM"):
            self.assertNotIn(write, code)


class TestItPlansAgainstRealContacts(CalBase):
    def test_a_known_attendee_becomes_a_proposal(self):
        cid = db.create_contact(self.conn, "Known Person")
        db.add_identifier(self.conn, cid, "email", "known@person.example")
        self.conn.commit()
        ev = self.store.event("Coffee", 3)
        self.store.attendee(ev, "known@person.example", name="Known Person")
        planned = loc.plan(self.conn, self.store.path)
        self.assertEqual([p["contact_id"] for p in planned["proposals"]], [cid])
        self.assertEqual(planned["proposals"][0]["source"],
                         ics_import.PROVENANCE_SOURCE)

    def test_an_unknown_attendee_proposes_nothing(self):
        """Invariant 1: importers only enrich people already in crm.db."""
        ev = self.store.event("Stranger", 3)
        self.store.attendee(ev, "nobody@person.example", name="No Body")
        planned = loc.plan(self.conn, self.store.path)
        self.assertEqual(planned["proposals"], [])
