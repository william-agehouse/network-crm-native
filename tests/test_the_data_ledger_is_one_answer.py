"""NC-088 / NC-114. The new Data page asks one endpoint, and it tells the truth.

GET /api/data/ledger is every row the rebuilt Data page draws, the "Needs you"
list above them, and the figures for "This install". The page renders it and
decides nothing, so every rule the current page keeps in JavaScript — what needs
you, what a finding is covered by, what switching a source on costs on THIS install —
has to hold here instead. These tests pin each rule on an install where the
things that usually coincide do not: four people in four states, a sync helper
in every state it can be in, and no setting borrowed from the machine running
them.
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db  # noqa: E402
from tests.test_every_people_count_on_the_data_page_is_one_group import _FourPeople  # noqa: E402

import api_fda  # noqa: E402
import api_gmail  # noqa: E402
import api_import  # noqa: E402
import api_ledger  # noqa: E402
import api_sources  # noqa: E402
import figures  # noqa: E402
import importers  # noqa: E402
import linkedin_token  # noqa: E402

# Every setting the ledger reads, pinned empty. Without this the rows would read
# whatever the machine running the suite has in its .env — a real LinkedIn token
# would make "LinkedIn messages" look switched on in an empty database.
QUIET_ENV = {"LINKEDIN_TOKEN": "", "ANTHROPIC_API_KEY": "", "IMAC": "", "KEY": "",
             "NC_ICS_GOOGLE_URL": "", "NC_ICS_ICLOUD_URL": "", "NC_ICS_FETCH": "0",
             "NC_AUTOSYNC": "1",
             # Set, so the "who you are" need (NC-216) appears only in the tests
             # that ask for it. An earlier test in the suite can leave these empty.
             "SELF_NAME": "Plain Person", "SELF_EMAIL": "plain@example.com"}


def fda(state="not_installed", ok=(), blocked=(), available=True):
    """The sync helper's status, in the shape api_fda.handle_fda_status returns."""
    return {"state": state, "available": available, "ok": list(ok),
            "blocked": list(blocked), "copies": list(importers.HELPER_SOURCE_LABELS),
            "targets": {"settings": "fda_settings", "binary": "fda_binary"},
            "waits": {"install": 60}}


NO_GMAIL = {"accounts": [], "ready": [], "needs_file": [], "needs_sign_in": []}


def finding(source, path, mtime="2026-09-20", count=0, auto=False, live=False,
            stale_days=0, blocked=False, label=""):
    return {"source": source, "path": path, "label": label or source, "mtime": mtime,
            "count": count, "blocked": blocked, "helper_current": False,
            "auto": auto, "live": live, "stale_days": stale_days}


class _Ledger:
    """Pins everything the ledger reads that is not the database."""

    fda_status = fda()
    gmail_status = NO_GMAIL
    blockers: dict = {}
    found: list = []

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, QUIET_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.detect = mock.MagicMock(side_effect=lambda conn: {"found": list(self.found)})
        for target, attr, value in (
                (api_fda, "handle_fda_status", lambda: dict(self.fda_status)),
                (api_gmail, "status", lambda conn=None: dict(self.gmail_status)),
                (api_import, "source_blockers", lambda conn: dict(self.blockers)),
                (api_import, "handle_import_detect", self.detect)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def ledger(self, scan=False):
        return api_ledger.handle_data_ledger(self.conn, {"scan": ["1"]} if scan else {})

    def row(self, out, key):
        return next(r for r in out["rows"] if r["key"] == key)


class TestEveryRowIsAlwaysThere(_Ledger, Base):

    def test_every_row_is_listed_on_an_empty_install(self):
        """Every source is listed, used or not. An off row is an offer.
        The three automatic passes are one row (NC-231)."""
        out = self.ledger()
        self.assertEqual(sorted(r["key"] for r in out["rows"]),
                         sorted([r["key"] for r in api_ledger.ROWS
                                 if r["key"] not in api_ledger.FOLDED] + ["worked_out"]))

    def test_rows_sort_by_what_they_cost_then_by_their_place(self):
        out = self.ledger()
        self.assertEqual(out["rows"][0]["key"], "helper",
                         "automatic copying leads, above what it copies (NC-232)")
        sources = out["rows"][1:-1]
        self.assertEqual(out["rows"][-1]["key"], "worked_out", "the automatic work comes last")
        keys = [(r["cost"]["rank"], api_ledger.ORDER[r["key"]]) for r in sources]
        self.assertEqual(keys, sorted(keys))
        # And the scale is not flat: a free row really does come before a machine.
        ranks = [r["cost"]["rank"] for r in sources]
        self.assertLess(ranks[0], ranks[-1])

    def test_rows_in_use_come_first(self):
        """NC-229. Gmail costs more than Contacts, and is in use."""
        self.gmail_status = {"accounts": ["a@example.com"], "ready": ["a@example.com"],
                             "needs_file": [], "needs_sign_in": []}
        out = self.ledger()
        self.assertEqual(out["rows"][1]["key"], "gmail", "first after automatic copying")
        in_use = [r["in_use"] for r in out["rows"][1:-1]]
        self.assertEqual(in_use, sorted(in_use, reverse=True))

    def test_every_row_has_the_one_shape(self):
        out = self.ledger()
        for r in out["rows"]:
            with self.subTest(row=r["key"]):
                for k in ("label", "gives", "have", "takes", "cost", "state",
                          "in_use", "detail"):
                    self.assertIn(k, r)
                self.assertTrue(r["gives"])
                self.assertTrue(r["takes"], "every row says what switching it on takes")
                self.assertIn(r["state"]["tone"],
                              ("ok", "off", "behind", "blocked", "paused", "na"))
                self.assertIn(r["cost"]["tone"], ("none", "low", "mid", "stop"))

    def test_a_row_whose_facts_fail_is_still_listed(self):
        """One unhappy owner must not take a row off the page."""
        def boom(row, ctx):
            raise RuntimeError("the owner broke")
        with mock.patch.dict(api_ledger.BUILDERS, {"email": boom}):
            out = self.ledger()
        self.assertEqual(self.row(out, "email")["label"], "Email")

    def test_the_endpoint_is_reachable(self):
        """NC-016's lesson: a ROUTES table nobody registered is a 404."""
        self.assertIn(api_ledger, app.ROUTE_MODULES)
        name = api_ledger.ROUTES["GET"]["/api/data/ledger"]
        import inspect
        self.assertEqual(list(inspect.signature(getattr(api_ledger, name)).parameters)[:1],
                         ["conn"])


class TestWhatItTakesDependsOnThisMac(_Ledger, Base):

    def test_a_source_the_helper_copies_costs_nothing(self):
        self.fda_status = fda("ok", ok=[importers.HELPER_IMESSAGE])
        out = self.ledger()
        self.assertEqual(self.row(out, "imessage")["cost"]["kind"], "free")
        self.assertIn("Automatic copying keeps it up to date", self.row(out, "imessage")["takes"])

    def test_without_the_helper_the_manual_route_is_the_price(self):
        """Invariant 6: the way in that needs no permission keeps working, and
        the row says what it costs."""
        out = self.ledger()
        r = self.row(out, "imessage")
        self.assertEqual(r["cost"]["kind"], "file")
        self.assertIn("Finder", r["takes"])
        self.assertIn("automatic copying", r["detail"]["manual"], "and it names the other way in")

    def test_a_copy_of_the_app_without_the_helper_says_so(self):
        self.fda_status = fda("unavailable", available=False)
        out = self.ledger()
        self.assertEqual(self.row(out, "helper")["state"]["tone"], "na")
        self.assertEqual(self.row(out, "calendar_local")["state"]["tone"], "na")
        self.assertNotIn(out["next"]["row"], ("helper", "calendar_local"),
                         "never offer the thing this copy cannot do")

    def test_a_calendar_imported_by_another_copy_is_not_green_in_this_one(self):
        """A calendar imported by a copy with the helper and opened in a copy
        without it must not say "working" beside "no sync helper"."""
        self.fda_status = fda("unavailable", available=False)
        db.state_set(self.conn, "import_synced_calendar_local", "2026-09-20T10:00:00")
        self.conn.commit()
        r = self.row(self.ledger(), "calendar_local")
        self.assertTrue(r["in_use"])
        self.assertEqual(r["state"]["tone"], "na")

    def test_the_calendar_fallback_names_the_rows_rather_than_a_direction(self):
        r = self.row(self.ledger(), "calendar_local")
        self.assertNotIn("below", r["detail"]["manual"])
        self.assertIn("Calendar file row", r["detail"]["manual"])

    def test_a_refused_helper_is_a_need_and_the_grant_is_the_first_button(self):
        self.fda_status = fda("blocked", blocked=[importers.HELPER_CONTACTS])
        out = self.ledger()
        need = next(n for n in out["needs"] if n["row"] == "helper")
        self.assertIn("Contacts", need["says"])
        self.assertEqual(need["acts"][0]["target"], "fda_settings")

    def test_a_newer_helper_in_the_app_is_a_need_that_names_the_permission(self):
        """NC-223. The helper copied everything but was an older program than the
        app's, and nothing on the page told the person to set it up again."""
        self.fda_status = dict(fda("outdated", ok=importers.HELPER_SOURCE_LABELS),
                               missing=[], newer_in_app=True)
        out = self.ledger()
        need = next(n for n in out["needs"] if n["row"] == "helper")
        self.assertIn("A newer copy came with this version of the app", need["says"])
        self.assertIn("Full Disk Access again", need["says"])
        self.assertEqual(need["acts"][-1]["kind"], "row")

    def test_the_cheapest_thing_not_yet_on_is_what_an_empty_install_is_told(self):
        out = self.ledger()
        self.assertIsNotNone(out["next"])
        nxt = self.row(out, out["next"]["row"])
        self.assertFalse(nxt["in_use"])
        self.assertNotIn(nxt["key"], api_ledger.CHAINED,
                         "an automatic pass has nothing to switch on")
        cheaper = [r for r in out["rows"] if r["cost"]["rank"] < nxt["cost"]["rank"]
                   and not r["in_use"] and r["key"] not in api_ledger.CHAINED
                   and r["state"]["tone"] != "na"]
        self.assertEqual(cheaper, [])


    def test_the_next_thing_is_never_an_automatic_pass(self):
        """With every free source already on, the automatic passes are the only
        free rows left off — and they have nothing to click. The next thing is
        the cheapest real source."""
        self.fda_status = fda("ok", ok=[importers.HELPER_WHATSAPP])
        db.state_set(self.conn, "import_synced_whatsapp", "2026-09-26T10:00:00")
        self.conn.commit()
        out = self.ledger()
        self.assertTrue(self.row(out, "whatsapp")["in_use"])
        self.assertEqual(out["next"]["row"], "contacts")

    def test_a_blocker_on_a_source_nobody_uses_is_not_shown(self):
        self.blockers = {"contacts": "macOS is refusing the helper"}
        r = self.row(self.ledger(), "contacts")
        self.assertEqual((r["blocked"], r["state"]["tone"]), ("", "off"))


class TestNothingIsGreenBeforeItHasRun(_Ledger, Base):

    def part(self, out, key):
        """One of the three passes folded into "Worked out on this Mac" (NC-231)."""
        return next(p for p in self.row(out, "worked_out")["detail"]["parts"] if p["key"] == key)

    def test_the_automatic_passes_wait_for_a_first_import(self):
        out = self.ledger()
        self.assertEqual(self.row(out, "worked_out")["state"]["tone"], "off")
        for key in api_ledger.CHAINED:
            with self.subTest(row=key):
                p = self.part(out, key)
                self.assertFalse(p["in_use"])
                self.assertEqual(p["state"]["tone"], "off")

    def test_and_run_once_they_have(self):
        db.state_set(self.conn, "import_synced_engagement", "2026-09-26T10:00:00")
        self.conn.commit()
        out = self.ledger()
        p = self.part(out, "engagement")
        self.assertTrue(p["in_use"])
        self.assertEqual(p["state"]["tone"], "ok")
        r = self.row(out, "worked_out")
        self.assertTrue(r["in_use"])
        self.assertEqual(r["state"]["tone"], "ok")
        self.assertTrue(r["have"]["says"].startswith("Last worked out "), r["have"])

    def test_automatic_work_switched_off_reads_as_paused(self):
        db.state_set(self.conn, "import_synced_engagement", "2026-09-26T10:00:00")
        self.conn.commit()
        with mock.patch.dict(os.environ, {"NC_AUTOSYNC": "0"}):
            out = self.ledger()
        self.assertEqual(self.part(out, "engagement")["state"]["tone"], "paused")
        self.assertEqual(self.row(out, "worked_out")["state"]["tone"], "paused",
                         "the folded row takes its worst part's state")

    def test_a_source_that_imports_itself_says_paused_when_that_is_switched_off(self):
        """An instance running with NC_AUTOSYNC=0 must not have its iMessage row
        say "Imports itself when new data appears" all the same."""
        db.state_set(self.conn, "import_synced_imessage", "2026-09-26T10:00:00")
        self.conn.commit()
        self.assertEqual(self.row(self.ledger(), "imessage")["state"]["tone"], "ok")
        with mock.patch.dict(os.environ, {"NC_AUTOSYNC": "0"}):
            state = self.row(self.ledger(), "imessage")["state"]
        self.assertEqual(state["tone"], "paused")
        self.assertNotIn("Imports itself", state["says"])

    def test_and_so_does_one_whose_own_switch_is_off(self):
        db.state_set(self.conn, "import_synced_imessage", "2026-09-26T10:00:00")
        self.conn.commit()
        switch = next(r for r in api_sources._sources(self.conn)
                      if r["key"] == "imessage")["off_switch"]
        self.assertTrue(switch.startswith("NC_"), switch)
        with mock.patch.dict(os.environ, {switch: "0"}):
            state = self.row(self.ledger(), "imessage")["state"]
        self.assertEqual(state, {"tone": "paused",
                                 "says": "Paused: automatic importing is off for this source"})

    def test_an_empty_install_needs_nothing(self):
        """No LinkedIn token on a fresh install is an offer, not a need."""
        self.blockers = {"linkedin": "No LinkedIn token is set"}
        out = self.ledger()
        self.assertEqual(out["needs"], [])
        self.assertEqual(self.row(out, "linkedin_messages")["state"]["tone"], "off")


class TestEveryFigureCountsTheSamePeople(_Ledger, _FourPeople):
    """The NC-122 fixture: one person in each state, each carrying every kind of
    data. So every count of people is 1, and a figure counting any other group
    reads 2, 3 or 4."""

    def test_the_counts_of_people(self):
        out = self.ledger()
        for r in out["rows"]:
            for f in r["have"]["figures"]:
                if f["unit"] == "people":
                    with self.subTest(row=r["key"], says=f["says"]):
                        self.assertEqual(f["n"], 1)
                        self.assertEqual(f["population"], "active")
        self.assertEqual(out["install"]["people"]["figure"]["n"], 1)

    def test_messages_are_every_message_on_this_mac(self):
        """Four people, a group conversation's own row and a person deleted for
        good: six messages saved, and the figure says whose they are."""
        out = self.ledger()
        msgs = out["install"]["messages"]["figure"]
        self.assertEqual((msgs["n"], msgs["population"]), (6, "all"))
        self.assertEqual(out["install"]["messages"]["by_channel"], {"linkedin": 6})
        # NC-233: the row itself counts the last 7 days, and these are older.
        r = self.row(out, "linkedin_messages")
        self.assertNotIn("messages", [f["unit"] for f in r["have"]["figures"]])
        self.assertIn("no messages in the last 7 days", r["have"]["says"])

    def test_the_whole_payload_passes_the_figure_checks(self):
        self.assertEqual(figures.check_payload(self.ledger()), [])

    def test_a_row_says_what_it_has_in_words(self):
        r = self.row(self.ledger(), "whatsapp")
        self.assertIn("1 person with history", r["have"]["says"])
        self.assertNotIn("1 people", r["have"]["says"])


    def test_a_row_nobody_switched_on_claims_nothing(self):
        """Everyone in the fixture has email history, and Email has never run —
        so that history came from somewhere else, and the Email row says nothing."""
        r = self.row(self.ledger(), "email")
        self.assertFalse(r["in_use"])
        self.assertEqual(r["have"], {"says": "", "figures": []})

    def test_the_people_you_met_are_on_every_calendar_in_use(self):
        """The four calendars share one importer and one provenance token, so
        the figure cannot be split between them — it is on each one in use,
        saying so, and on none that is not."""
        for key in ("calendar_local", "calendar", "calendar_google"):
            db.state_set(self.conn, f"import_synced_{key}", "2026-09-26T10:00:00")
        self.conn.commit()
        with mock.patch.dict(os.environ, {"NC_ICS_GOOGLE_URL": "https://calendar.example/x.ics"}):
            out = self.ledger()
        for key in ("calendar_local", "calendar", "calendar_google"):
            with self.subTest(row=key):
                (fig,) = self.row(out, key)["have"]["figures"]
                self.assertEqual(fig["n"], 1)
                self.assertIn("all your calendars together", fig["says"])
        self.assertEqual(self.row(out, "calendar_icloud")["have"]["figures"], [])


class TestABlockerBelongsToTheRowItBlocks(_Ledger, _FourPeople):

    def test_a_blocker_starts_the_rows_sentence(self):
        self.blockers = {"linkedin": "no token, so direct messages are not being collected"}
        r = self.row(self.ledger(), "linkedin_messages")
        self.assertEqual(r["state"]["says"][:9], "No token,")

    def test_a_refused_token_blocks_messages_and_nothing_else(self):
        self.blockers = {"linkedin": "LinkedIn refused the token"}
        with mock.patch.dict(os.environ, {"LINKEDIN_TOKEN": "AQV-test-token"}):
            out = self.ledger()
        self.assertEqual(self.row(out, "linkedin_messages")["state"]["tone"], "blocked")
        self.assertNotEqual(self.row(out, "linkedin_connections")["state"]["tone"],
                            "blocked", "the export file needs no token")
        self.assertEqual([n["row"] for n in out["needs"]], ["linkedin_messages"])

    def test_a_token_running_out_is_a_need_before_it_is_a_failure(self):
        with mock.patch.dict(os.environ, {"LINKEDIN_TOKEN": "AQV-test-token"}), \
                mock.patch.object(linkedin_token, "summary",
                                  return_value={"state": "expiring", "days_left": 5.0}):
            out = self.ledger()
        r = self.row(out, "linkedin_messages")
        self.assertEqual(r["state"]["tone"], "behind")
        self.assertIn("5 days", r["state"]["says"])
        self.assertIn("linkedin_messages", [n["row"] for n in out["needs"]])

    def test_a_token_linkedin_stopped_accepting_is_stopped_not_working(self):
        """NC-068: a token LinkedIn stopped accepting must not read as working."""
        with mock.patch.dict(os.environ, {"LINKEDIN_TOKEN": "AQV-test-token"}), \
                mock.patch.object(linkedin_token, "summary",
                                  return_value={"state": "dead", "days_left": None}):
            out = self.ledger()
        r = self.row(out, "linkedin_messages")
        self.assertEqual(r["state"]["tone"], "blocked")
        self.assertIn("no longer accepts", r["state"]["says"])
        self.assertIn("linkedin_messages", [n["row"] for n in out["needs"]])

    def test_a_gmail_sign_in_that_ran_out_is_a_need(self):
        """The row must read needs_sign_in, not only needs_file."""
        self.gmail_status = {"accounts": [{"email": "me@example.com"}], "ready": [],
                             "needs_file": [], "needs_sign_in": ["me@example.com"]}
        out = self.ledger()
        r = self.row(out, "gmail")
        self.assertEqual(r["state"]["tone"], "blocked")
        self.assertIn("Sign in to Google again for me@example.com", r["state"]["says"])
        self.assertIn("gmail", [n["row"] for n in out["needs"]])

    def test_a_token_is_shown_by_its_last_four_only(self):
        with mock.patch.dict(os.environ, {"LINKEDIN_TOKEN": "AQV-secret-value-9876"}):
            out = self.ledger()
        box = next(a for a in self.row(out, "linkedin_messages")["detail"]["acts"]
                   if a["kind"] == "secret")
        self.assertTrue(box["set"])
        self.assertNotIn("secret-value", repr(out))


class TestFilesOnThisMac(_Ledger, Base):

    def test_without_a_scan_nothing_is_globbed(self):
        """The scan can wait on a macOS consent dialog, so it is asked for."""
        out = self.ledger()
        self.detect.assert_not_called()
        self.assertFalse(out["scanned"])

    def test_a_file_nothing_imports_is_a_need_with_its_path(self):
        self.found = [finding("contacts", "/x/cards.vcf", count=27, label="Contacts export")]
        out = self.ledger(scan=True)
        (need,) = out["needs"]
        self.assertEqual(need["row"], "contacts")
        self.assertIn("27 cards", need["says"])
        self.assertEqual(need["acts"][0]["path"], "/x/cards.vcf")

    def test_a_name_starting_with_a_vowel_takes_an(self):
        self.found = [finding("imessage", "/x/chat.db", count=2)]
        (need,) = self.ledger(scan=True)["needs"]
        self.assertTrue(need["says"].startswith("An iMessage file"), need["says"])
        self.assertIn("2 people in this copy", need["says"])

    def test_one_need_per_source_with_the_other_file_as_an_alternative(self):
        self.found = [finding("email", "/old.mbox", mtime="2026-01-01"),
                      finding("email", "/new.mbox", mtime="2026-09-01", label="new")]
        (need,) = self.ledger(scan=True)["needs"]
        self.assertEqual(need["acts"][0]["path"], "/new.mbox", "the freshest file leads")
        self.assertEqual([a["path"] for a in need["acts"][1:]], ["/old.mbox"])

    def test_a_copy_that_refreshes_itself_is_not_asked_for(self):
        self.found = [finding("imessage", "/auto/chat.db", auto=True)]
        self.assertEqual(self.ledger(scan=True)["needs"], [])

    def test_a_file_older_than_the_copy_that_refreshes_itself_is_not_asked_for(self):
        self.found = [finding("imessage", "/auto/chat.db", mtime="2026-09-20", auto=True),
                      finding("imessage", "/Downloads/chat.db", mtime="2026-09-01")]
        self.assertEqual(self.ledger(scan=True)["needs"], [])

    def test_a_copy_that_stopped_refreshing_is(self):
        self.found = [finding("imessage", "/auto/chat.db", auto=True, stale_days=9)]
        (need,) = self.ledger(scan=True)["needs"]
        self.assertEqual(need["tone"], "behind")
        self.assertIn("9 days old", need["says"])

    def test_a_stale_copy_the_helper_is_refused_does_not_say_copy_it_by_hand(self):
        self.fda_status = fda("blocked", blocked=[importers.HELPER_IMESSAGE])
        self.found = [finding("imessage", "/auto/chat.db", auto=True, stale_days=9,
                              blocked=True)]
        needs = self.ledger(scan=True)["needs"]
        self.assertEqual([n["row"] for n in needs], ["helper"],
                         "the grant is the fix, and the helper's need already says so")

    def test_a_calendar_file_older_than_the_calendar_it_duplicates_is_not_asked_for(self):
        db.state_set(self.conn, "import_synced_calendar_local", "2026-09-25T08:00:00")
        self.conn.commit()
        self.found = [finding("calendar", "/cal.ics", mtime="2026-09-20")]
        self.assertEqual(self.ledger(scan=True)["needs"], [])

    def _a_file_changed_at(self, source, when_iso, name="copy"):
        path = os.path.join(self.dir, f"{source}-{name}")
        with open(path, "w") as fh:
            fh.write("x")
        import datetime
        ts = datetime.datetime.fromisoformat(when_iso).timestamp()
        os.utime(path, (ts, ts))
        return path

    def test_a_file_you_already_imported_is_not_asked_for_again(self):
        """NC-208. A copy changed at 04:00 and imported at 04:10 the same day must
        not sit in "Needs you" as "Nothing imports it"."""
        path = self._a_file_changed_at("contacts", "2026-09-25T04:00:00+00:00")
        db.state_set(self.conn, "import_synced_contacts", "2026-09-25T04:10:00+00:00")
        self.conn.commit()
        self.found = [finding("contacts", path, mtime="2026-09-25")]
        self.assertEqual(self.ledger(scan=True)["needs"], [])

    def test_but_one_changed_later_the_same_day_is(self):
        """Exact times, not the day: a copy changed that evening is newer."""
        path = self._a_file_changed_at("contacts", "2026-09-25T18:00:00+00:00")
        db.state_set(self.conn, "import_synced_contacts", "2026-09-25T04:10:00+00:00")
        self.conn.commit()
        self.found = [finding("contacts", path, mtime="2026-09-25")]
        self.assertEqual([n["row"] for n in self.ledger(scan=True)["needs"]], ["contacts"])

    def test_a_source_on_a_clock_is_not_judged_by_its_stamp(self):
        """LinkedIn's token fetch stamps the same key from the background, so a
        recent stamp says nothing about the export file sitting in Downloads."""
        self.assertIn("linkedin", api_import.API_SOURCES)
        path = self._a_file_changed_at("linkedin", "2026-09-25T04:00:00+00:00")
        db.state_set(self.conn, "import_synced_linkedin", "2026-09-26T04:10:00+00:00")
        self.conn.commit()
        self.found = [finding("linkedin", path, mtime="2026-09-25")]
        self.assertEqual([n["row"] for n in self.ledger(scan=True)["needs"]],
                         ["linkedin_connections"])

    def test_whatsapp_photos_ride_with_whatsapp(self):
        self.found = [finding("whatsapp_photos", "/wa/photos")]
        self.assertEqual(self.ledger(scan=True)["needs"], [])


class TestTheSecondRound(_Ledger, Base):
    """NC-232 to NC-234."""

    def test_automatic_copying_leads(self):
        out = self.ledger()
        self.assertEqual(out["rows"][0]["key"], "helper")

    def test_but_not_on_a_copy_with_no_helper(self):
        self.fda_status = fda("unavailable", available=False)
        out = self.ledger()
        self.assertEqual(self.row(out, "helper")["state"]["tone"], "na")
        self.assertNotEqual(out["rows"][0]["key"], "helper")

    def test_the_next_step_is_the_cheapest_not_the_first(self):
        """With the helper leading, "the first row not in use" would be a
        second Mac's worth of setup on some installs; the next step is by cost."""
        out = self.ledger()
        nxt = self.row(out, out["next"]["row"])
        offers = [r for r in out["rows"] if not r["in_use"] and r["state"]["tone"] != "na"
                  and r["key"] not in api_ledger.CHAINED and r["key"] != "worked_out"
                  and not r.get("absent")]
        self.assertEqual(nxt["cost"]["rank"], min(r["cost"]["rank"] for r in offers))

    def test_a_closed_row_in_use_says_when_it_last_ran(self):
        db.state_set(self.conn, "import_synced_contacts", "2020-01-01T10:00:00")
        self.conn.commit()
        r = self.row(self.ledger(), "contacts")
        self.assertTrue(r["in_use"])
        self.assertRegex(r["when"], r"^Last import .+ ago$")

    def test_a_row_not_in_use_says_what_it_takes(self):
        r = self.row(self.ledger(), "contacts")
        self.assertFalse(r["in_use"])
        self.assertFalse(r.get("when"))
        self.assertTrue(r["takes"])

    def test_the_helper_says_when_it_last_copied(self):
        self.fda_status = dict(fda("ok", ok=[importers.HELPER_IMESSAGE]), ran_ago_seconds=30)
        r = self.row(self.ledger(), "helper")
        self.assertTrue(r["in_use"])
        self.assertEqual(r["when"], "Last copied just now")

    def test_a_message_row_counts_the_last_seven_days(self):
        with mock.patch.object(api_ledger, "_messages_this_week", lambda: {"whatsapp": 3}):
            db.state_set(self.conn, "import_synced_whatsapp", "2026-09-29T10:00:00")
            self.conn.commit()
            r = self.row(self.ledger(), "whatsapp")
        fig = next(f for f in r["have"]["figures"] if f["unit"] == "messages")
        self.assertEqual(fig["n"], 3)
        self.assertIn("in the last 7 days", fig["says"])
        self.assertNotIn("no messages in the last 7 days", r["have"]["says"])

    def test_the_seven_days_are_counted_in_the_archive(self):
        from tests.helpers import arc
        path = os.path.join(tempfile.mkdtemp(prefix="nc233-"), "archive.db")
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", path):
            arc.init_archive(path)
            ac = arc.get_archive_db(path)
            now = datetime.datetime.now(datetime.timezone.utc)
            for key, ts in (("old", "2001-01-01 10:00:00"),
                            ("new", now.strftime("%Y-%m-%d %H:%M:%S"))):
                arc.insert_message(ac, contact_id=1, channel="whatsapp", direction=0,
                                   ts=ts, body=key, source_key=key)
            ac.commit()
            ac.close()
            self.assertEqual(api_ledger._messages_this_week(), {"whatsapp": 1})
            plan = " ".join(r[3] for r in arc.get_archive_db(path).execute(
                "EXPLAIN QUERY PLAN " + api_ledger.WEEK_SQL))
        self.assertIn("idx_msg_ts", plan, "reads this week's messages, not all of them")

    def test_the_scrape_switch_is_on_the_row_it_affects(self):
        acts = self.row(self.ledger(), "linkedin_facts")["detail"]["acts"]
        switch = next(a for a in acts if a["kind"] == "switch")
        self.assertEqual(switch["key"], "SCRAPE_OVERRIDES_MANUAL")
        self.assertFalse(switch["on"])


if __name__ == "__main__":
    unittest.main()
