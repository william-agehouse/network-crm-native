"""Birthdays: getting them in, and keeping the one a human typed (NC-041).

`contact_dates`, the recurrence maths, the phrasing and the person-page UI all
existed and were tested. Nothing ever imported a birthday. The table could hold
only calendar meetings and no birthdays, so "when is X's
birthday" had no answer anywhere in the app — a feature whose input is never
written is indistinguishable from one that was never built.

Two things are gated here:

  * the vCard `BDAY` parser, across every form Apple and Google actually emit,
    including the year sentinels that mean "the day is real, the year is not";
  * `db.upsert_birthday`, which keys on the PERSON rather than on the date. Both
    the alternatives are wrong in a way that only shows up on the second import:
    `add_contact_date` leaves a corrected birthday sitting next to the old one,
    and a blind UPDATE overwrites a date the owner fixed by hand (invariant 4).

The Core Data timestamp half lives in test_addressbook_import.py, next to the
store fixture it needs.
"""
import io
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

import unittest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "crm"))

import api_dates  # noqa: E402
import contacts_import  # noqa: E402


class TestTheBdayFieldIsParsedInEveryFormItArrivesIn(unittest.TestCase):
    def test_the_forms_apple_and_google_actually_write(self):
        cases = {
            "19120623": ("1912-06-23", 1),          # vCard 3.0, no separators
            "1912-06-23": ("1912-06-23", 1),        # vCard 4.0
            "1912-06-23T00:00:00Z": ("1912-06-23", 1),   # datetime value
            " 1912-06-23 ": ("1912-06-23", 1),      # whitespace
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(contacts_import.parse_bday(raw), want)

    def test_a_day_with_no_year_keeps_the_day_and_admits_the_year(self):
        # vCard 4.0's own no-year form, plus the two sentinel years seen in
        # exports. All three must land on year_known=0 — the day is the useful
        # part and an age computed from a placeholder is a wrong age.
        for raw in ("--0623", "--06-23", "1900-06-23", "16040623"):
            with self.subTest(raw=raw):
                day, known = contacts_import.parse_bday(raw)
                self.assertEqual(day[5:], "06-23", raw)
                self.assertEqual(known, 0, raw)
                self.assertEqual(day[:4], str(contacts_import.NO_YEAR_PLACEHOLDER))

    def test_nonsense_yields_nothing_rather_than_a_guess(self):
        # A birthday guessed wrong is worse than one missing: it surfaces on the
        # wrong day, every year, and looks authoritative doing it.
        for raw in ("", "   ", "unknown", "1912", "1912-13-01", "1912-02-31",
                    "March 15th", "0000-00-00"):
            with self.subTest(raw=raw):
                self.assertEqual(contacts_import.parse_bday(raw), ("", 1))

    def test_a_card_absorbs_bday_only_once(self):
        card = contacts_import.VCard()
        contacts_import._absorb_field(card, "BDAY", "1912-06-23")
        contacts_import._absorb_field(card, "BDAY", "1999-01-01")
        self.assertEqual(card.birthday, "1912-06-23")
        self.assertEqual(card.birth_year_known, 1)

    def test_a_card_with_no_bday_line_has_no_birthday(self):
        card = contacts_import.VCard()
        contacts_import._absorb_field(card, "FN", "Ada Lovelace")
        self.assertEqual(card.birthday, "")
        self.assertEqual(card.birth_year_known, 1)


class TestOnePersonHasOneBirthday(Base):
    def _cid(self, name="Ada Lovelace"):
        cid = db.create_contact(self.conn, name)
        self.conn.commit()
        return cid

    def _rows(self, cid):
        return self.conn.execute(
            "SELECT on_date, year_known, recurring, source FROM contact_dates "
            "WHERE contact_id = ? AND kind = 'birthday' ORDER BY id", (cid,)
        ).fetchall()

    def test_a_first_import_creates_it_and_makes_it_annual(self):
        cid = self._cid()
        self.assertEqual(db.upsert_birthday(self.conn, cid, "1912-06-23"), "created")
        rows = self._rows(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["on_date"], "1912-06-23")
        self.assertEqual(rows[0]["recurring"], 1, "a birthday that doesn't repeat "
                         "surfaces once and then never again")
        self.assertEqual(rows[0]["source"], "contacts")

    def test_reimporting_the_same_date_changes_nothing(self):
        cid = self._cid()
        db.upsert_birthday(self.conn, cid, "1912-06-23")
        self.assertEqual(db.upsert_birthday(self.conn, cid, "1912-06-23"), "unchanged")
        self.assertEqual(len(self._rows(cid)), 1)

    def test_a_correction_moves_the_row_instead_of_adding_a_second(self):
        """The whole reason this is not add_contact_date. Fix the year in Apple
        Contacts, sync, and the contact must hold ONE birthday — the new one."""
        cid = self._cid()
        db.upsert_birthday(self.conn, cid, "1912-06-23")
        self.assertEqual(db.upsert_birthday(self.conn, cid, "1815-12-10"), "moved")
        rows = self._rows(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["on_date"], "1815-12-10")

    def test_learning_the_year_is_a_move_not_a_duplicate(self):
        cid = self._cid()
        db.upsert_birthday(self.conn, cid, "1604-06-23", year_known=0)
        self.assertEqual(
            db.upsert_birthday(self.conn, cid, "1912-06-23", year_known=1), "moved")
        rows = self._rows(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["on_date"], rows[0]["year_known"]),
                         ("1912-06-23", 1))

    def test_a_hand_typed_birthday_is_never_overwritten(self):
        """Invariant 4 applied to dates: manual beats every automatic source, so
        an import that disagrees leaves it alone and says so."""
        cid = self._cid()
        db.add_contact_date(self.conn, cid, "birthday", "1912-06-23")
        self.assertEqual(db.upsert_birthday(self.conn, cid, "1999-01-01"),
                         "refused_manual")
        rows = self._rows(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["on_date"], "1912-06-23")
        self.assertEqual(rows[0]["source"], "manual")

    def test_two_people_keep_their_own_birthdays(self):
        # keying on kind alone, without contact_id, would make the second import
        # "move" the first person's date onto the second person's
        a, b = self._cid("Ada Lovelace"), self._cid("Grace Hopper")
        db.upsert_birthday(self.conn, a, "1815-12-10")
        self.assertEqual(db.upsert_birthday(self.conn, b, "1906-12-09"), "created")
        self.assertEqual(self._rows(a)[0]["on_date"], "1815-12-10")
        self.assertEqual(self._rows(b)[0]["on_date"], "1906-12-09")

    def test_an_empty_date_is_refused_rather_than_written(self):
        cid = self._cid()
        with self.assertRaises(ValueError):
            db.upsert_birthday(self.conn, cid, "")

    def test_a_meeting_on_the_same_day_is_not_mistaken_for_the_birthday(self):
        # contact_dates holds meetings too. Keying on the person WITHOUT the kind
        # would make a calendar meeting look like an existing birthday.
        cid = self._cid()
        db.upsert_calendar_date(self.conn, cid, "2026-06-23", source_uid="uid-1")
        self.assertEqual(db.upsert_birthday(self.conn, cid, "1912-06-23"), "created")
        self.assertEqual(len(self._rows(cid)), 1)
        kinds = {r["kind"] for r in db.get_contact_dates(self.conn, cid)}
        self.assertEqual(kinds, {"birthday", "meeting"})


class TestTheImporterWritesWhatItParsed(Base):
    """The parser and the writer are both right and still import nothing if
    apply() never calls one from the other."""

    def _apply(self, cards):
        from unittest import mock
        with mock.patch.object(db, "DB_PATH", self.path):
            return contacts_import.apply(cards, enrich_only=False, dry_run=False)

    def _card(self, name, bday=""):
        card = contacts_import.VCard()
        card.name = name
        if bday:
            card.birthday, card.birth_year_known = contacts_import.parse_bday(bday)
        return card

    def test_a_new_contact_arrives_with_their_birthday(self):
        stats = self._apply([self._card("Ada Lovelace", "1815-12-10")])
        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["birthdays"], 1)
        conn = db.get_db(self.path)
        try:
            row = conn.execute(
                "SELECT c.name, d.on_date, d.recurring FROM contact_dates d "
                "JOIN contacts c ON c.id = d.contact_id WHERE d.kind = 'birthday'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["name"], "Ada Lovelace")
        self.assertEqual(row["on_date"], "1815-12-10")
        self.assertEqual(row["recurring"], 1)

    def test_an_existing_contact_is_enriched_with_one(self):
        db.create_contact(self.conn, "Grace Hopper")
        self.conn.commit()
        stats = self._apply([self._card("Grace Hopper", "1906-12-09")])
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["birthdays"], 1)

    def test_a_second_run_reports_no_new_birthdays(self):
        card = self._card("Ada Lovelace", "1815-12-10")
        self._apply([card])
        stats = self._apply([self._card("Ada Lovelace", "1815-12-10")])
        self.assertEqual(stats["birthdays"], 0, "an unchanged birthday must not "
                         "be counted as imported on every sync")

    def test_a_manual_date_is_counted_as_kept_not_as_a_failure(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.add_contact_date(self.conn, cid, "birthday", "1815-12-10")
        self.conn.commit()
        stats = self._apply([self._card("Ada Lovelace", "1999-01-01")])
        self.assertEqual(stats["birthdays"], 0)
        self.assertEqual(stats["birthdays_kept"], 1)

    def test_a_dry_run_writes_nothing(self):
        from unittest import mock
        with mock.patch.object(db, "DB_PATH", self.path):
            contacts_import.apply([self._card("Ada Lovelace", "1815-12-10")],
                                  enrich_only=False, dry_run=True)
        conn = db.get_db(self.path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM contact_dates").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)


class TestTheStripThatShowsThem(Base):
    """NC-040. Deleting Today took the only aggregated view of upcoming dates
    with it, so a birthday tomorrow surfaced nowhere — you had to already be
    looking at that person. /api/upcoming is the narrow replacement. What is
    gated here is the three ways it could quietly become useless: showing
    calendar meetings (a second, worse agenda), truncating in silence, and
    ignoring the setting that says how far ahead to look."""

    def _person(self, name, days, kind="birthday"):
        import datetime
        cid = db.create_contact(self.conn, name)
        when = datetime.date.today() + datetime.timedelta(days=days)
        if kind == "birthday":
            db.upsert_birthday(self.conn, cid, when.replace(year=1912).isoformat())
        else:
            db.add_contact_date(self.conn, cid, kind, when.isoformat())
        self.conn.commit()
        return cid

    def test_the_shape_the_strip_renders_from(self):
        self._person("Ada Lovelace", 1)
        out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(sorted(out), ["days", "more", "shown"])
        self.assertEqual(out["days"], 14)
        self.assertEqual(out["more"], 0)
        self.assertEqual(len(out["shown"]), 1)
        row = out["shown"][0]
        # Every key the strip reads. A dropped one is a silent 200 that renders
        # a chip with no name or no destination.
        for key in ("title", "kind", "days_until", "reason", "contact_id", "date_id"):
            self.assertIn(key, row)
        self.assertEqual(row["title"], "Ada Lovelace")
        self.assertEqual(row["days_until"], 1)

    def test_soonest_first(self):
        self._person("Later", 6)
        self._person("Sooner", 2)
        out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual([r["title"] for r in out["shown"]], ["Sooner", "Later"])

    def test_a_birthday_today_counts(self):
        self._person("Today", 0)
        out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(out["shown"][0]["days_until"], 0)

    def test_calendar_meetings_are_left_out(self):
        """The person's calendar app already shows these. Repeating them here turns
        a "you'd otherwise forget this" strip into a second agenda — and
        meetings can make up most of the rows in the table."""
        cid = db.create_contact(self.conn, "Standup Person")
        import datetime
        db.upsert_calendar_date(
            self.conn, cid,
            (datetime.date.today() + datetime.timedelta(days=1)).isoformat(),
            source_uid="uid-1", label="Standup")
        self.conn.commit()
        out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(out["shown"], [])
        self.assertEqual(out["more"], 0, "a meeting must not be counted as "
                         "something the strip declined to show either")
        self.assertNotIn("meeting", api_dates.UPCOMING_KINDS)

    def test_an_anniversary_and_a_custom_date_do_count(self):
        self._person("Anniversary Person", 2, kind="anniversary")
        self._person("Custom Person", 3, kind="custom")
        out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual([r["kind"] for r in out["shown"]],
                         ["anniversary", "custom"])

    def test_what_does_not_fit_is_counted_not_dropped(self):
        for i in range(api_dates.UPCOMING_ROWS + 2):
            self._person(f"Person {i}", i + 1)
        out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(len(out["shown"]), api_dates.UPCOMING_ROWS)
        self.assertEqual(out["more"], 2)

    def test_nothing_upcoming_is_an_empty_list_not_an_error(self):
        self._person("Far Away", 300)
        out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(out["shown"], [])
        self.assertEqual(out["more"], 0)

    def test_the_lookahead_setting_actually_governs_it(self):
        """The whole reason NC_DATES_LOOKAHEAD_DAYS is a field again: for its
        entire previous life it rendered, saved, and controlled nothing."""
        from unittest import mock
        self._person("In Twenty Days", 20)
        with mock.patch.dict(os.environ, {"NC_DATES_LOOKAHEAD_DAYS": "14"}):
            self.assertEqual(api_dates.handle_upcoming(self.conn, {})["shown"], [])
        with mock.patch.dict(os.environ, {"NC_DATES_LOOKAHEAD_DAYS": "30"}):
            out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(out["days"], 30)
        self.assertEqual(len(out["shown"]), 1)

    def test_a_days_override_is_honoured_and_clamped(self):
        self._person("In Forty Days", 40)
        self.assertEqual(api_dates.handle_upcoming(
            self.conn, {"days": ["60"]})["days"], 60)
        self.assertEqual(len(api_dates.handle_upcoming(
            self.conn, {"days": ["60"]})["shown"]), 1)
        # out of range and junk both fall back rather than raising
        for raw in ("9999", "0", "-3", "", "lots", None):
            with self.subTest(raw=raw):
                got = api_dates.handle_upcoming(self.conn, {"days": [raw]})["days"]
                self.assertTrue(1 <= got <= 365, got)

    def test_an_excluded_contact_never_appears(self):
        cid = self._person("Excluded Person", 1)
        self.conn.execute("UPDATE contacts SET excluded = 1 WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual(api_dates.handle_upcoming(self.conn, {})["shown"], [])


class TestTheRouteAndTheFieldExist(unittest.TestCase):
    """Three ways this ships broken with every unit test green: the handler is
    never routed (a module with no route is invisible), the strip is never
    rendered, or the setting it reads is not writable."""

    def test_the_route_is_declared_and_registered(self):
        import app
        self.assertIn(api_dates, app.ROUTE_MODULES)
        self.assertIn("/api/upcoming", api_dates.ROUTES["GET"])
        name = api_dates.ROUTES["GET"]["/api/upcoming"]
        self.assertTrue(callable(getattr(api_dates, name, None)))

    def test_the_handler_takes_a_conn_and_a_payload(self):
        """The registry reads both off the signature: a first parameter not
        named `conn` gets no database, and a one-parameter handler is never
        handed the query string."""
        import inspect
        params = list(inspect.signature(api_dates.handle_upcoming).parameters)
        self.assertEqual(params[:1], ["conn"])
        self.assertEqual(len(params), 2)

    def test_handle_week_is_still_the_one_place_recurrence_is_resolved(self):
        """handle_upcoming must read THROUGH handle_week, not reimplement the
        query, or a person's own page and the strip can disagree about when a
        birthday is."""
        src = io.open(os.path.join(REPO, "crm", "api_dates.py"),
                      encoding="utf-8").read()
        body = src[src.index("def handle_upcoming"):]
        self.assertIn("handle_week(conn", body)
        for banned in ("conn.execute", "SELECT ", "upcoming_dates("):
            self.assertNotIn(banned, body, f"handle_upcoming re-queries ({banned})")

    def test_the_lookahead_is_writable_from_settings(self):
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import settings as settings_mod
        self.assertIn("NC_DATES_LOOKAHEAD_DAYS", settings_mod.MANAGED_KEYS)
        # NC-236: the old Settings page rendered it as a field. The rebuilt one
        # has it as its own row, built on the server.
        import api_abilities
        builders = {key: build for key, _label, build in api_abilities.ROWS}
        self.assertIn("dates", builders, "the Settings page no longer lists the dates row")
        boxes = [a.get("key") for a in builders["dates"](None)["detail"]["acts"]]
        self.assertIn("NC_DATES_LOOKAHEAD_DAYS", boxes,
                      "the key is writable but no box on the Settings page sets it")

    def test_the_strip_is_rendered_and_disappears_when_empty(self):
        js = io.open(os.path.join(REPO, "static", "app", "people.js"),
                     encoding="utf-8").read()
        self.assertIn("SoonStrip", js)
        self.assertIn("/api/upcoming", js)
        # The one constraint that keeps this from becoming Today again: nothing
        # upcoming means zero pixels, not an empty container or a placeholder.
        self.assertIn('if (hidden || !data || !(data.shown || []).length) return null;', js)
        # And it must actually be mounted, not merely defined — a component
        # nobody renders is a feature nobody can reach.
        self.assertIn("<${SoonStrip}", js)

    def test_the_strip_has_its_own_styles(self):
        css = io.open(os.path.join(REPO, "static", "css", "view-network.css"),
                      encoding="utf-8").read()
        for cls in (".nx-soon", ".nx-soon-i", ".nx-soon-l", ".nx-soon-w", ".nx-soon-m"):
            self.assertIn(cls, css, f"{cls} is used in people.js but unstyled")
        # No literal colour: every one resolves through tokens.css, which is what
        # makes dark mode work without a second rule.
        block = css[css.index("/* ── Coming up"):css.index("/* ── The table")]
        import re
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", block), [],
                         "a hardcoded colour in the strip breaks one of the themes")


if __name__ == "__main__":
    unittest.main()


class TestTheStripUsesTheCalendarOnTheWallNotUTC(Base):
    """NC-055. Between local midnight and UTC midnight the strip was a day out.

    Wherever local time runs ahead of UTC, there is a window every night from
    local midnight until the offset has passed, and inside it a birthday TODAY
    rendered as "tomorrow" — the hours in which being right matters most, since
    the whole point of the strip is catching the day itself.

    The bug's own shape is why it survived: `upcoming_dates` asked UTC what day
    it was, the tests built their fixtures from `date.today()` which is local,
    and for most of the day those agree. It shows up as tests that pass all day
    and fail shortly after local midnight.

    So this class fakes the clock rather than waiting for midnight. A test that
    only fails in the early hours is not a regression test — it is the original
    bug with extra steps.
    """

    def _at(self, local_naive, utc_aware):
        """Freeze db's clock so local and UTC disagree about the date.

        Both accessors are stubbed on the same object: the fixed code asks for
        `datetime.now()` and the buggy code asked for `datetime.now(timezone.utc)`,
        so a fake that answers them differently is what tells the two apart. A
        stub that returned one value for both would pass either way.
        """
        import datetime as _dt

        real = _dt.datetime

        class Frozen(real):
            @classmethod
            def now(cls, tz=None):
                return utc_aware if tz is not None else local_naive

        return mock.patch.object(db, "datetime", Frozen)

    def _birthday_on(self, name, iso):
        cid = db.create_contact(self.conn, name)
        db.upsert_birthday(self.conn, cid, "1912" + iso[4:])
        self.conn.commit()
        return cid

    def test_a_birthday_today_is_today_at_half_past_midnight(self):
        import datetime as _dt
        # 00:30 on the 22nd, local. In UTC it is still 19:30 on the 21st.
        local = _dt.datetime(2026, 3, 22, 0, 30)
        utc = _dt.datetime(2026, 3, 21, 19, 30, tzinfo=_dt.timezone.utc)
        self._birthday_on("Born Today", "2026-03-22")
        with self._at(local, utc):
            out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(len(out["shown"]), 1, "the birthday vanished entirely")
        self.assertEqual(
            out["shown"][0]["days_until"], 0,
            "a birthday today reported as a future day — the strip is measuring "
            "in UTC while the person reading it lives in a timezone")

    def test_a_birthday_tomorrow_is_one_day_out_at_half_past_midnight(self):
        """The other side of the same skew: off-by-one in the window applies to
        every row, not only to today's."""
        import datetime as _dt
        local = _dt.datetime(2026, 3, 22, 0, 30)
        utc = _dt.datetime(2026, 3, 21, 19, 30, tzinfo=_dt.timezone.utc)
        self._birthday_on("Born Tomorrow", "2026-03-23")
        with self._at(local, utc):
            out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(out["shown"][0]["days_until"], 1)

    def test_midday_is_unaffected(self):
        """The control. For most of the day local and UTC agree about the date,
        and this is what made the bug invisible — so the fix must not have moved
        it."""
        import datetime as _dt
        local = _dt.datetime(2026, 3, 22, 12, 0)
        utc = _dt.datetime(2026, 3, 22, 7, 0, tzinfo=_dt.timezone.utc)
        self._birthday_on("Born Today", "2026-03-22")
        with self._at(local, utc):
            out = api_dates.handle_upcoming(self.conn, {})
        self.assertEqual(out["shown"][0]["days_until"], 0)

    def test_one_definition_of_today_not_three(self):
        """upcoming_dates, upcoming_events and the recurrence resolver each had
        their own copy of the expression, which is how they came to disagree
        with the tests. Three copies is the defect, not the timezone."""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "crm", "db.py"), encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn('datetime.now(timezone.utc).strftime("%Y-%m-%d")', code,
                         "a calendar date is being derived from UTC again")
        self.assertEqual(code.count("def _today_local_str"), 1)
