"""NC-215 to NC-219. The rebuilt Data and Settings pages.

The pages once said things that were not true ("nothing leaves it"), had buttons
that could only fail, a switch the server sent that nothing drew, setup text
written for the old page, and links between the two pages that landed at the top
instead of on the row. These tests pin each fix on the endpoint that decides it,
or on the page source where the page is the only place it happens.
"""

import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, js_code_of  # noqa: E402
from tests.test_the_data_ledger_is_one_answer import _Ledger, fda, finding  # noqa: E402
from tests.test_the_settings_ledger_is_one_answer import _Settings  # noqa: E402

import api_ledger  # noqa: E402
import api_linkedin  # noqa: E402
import api_setup  # noqa: E402
import api_sources  # noqa: E402
import archive as arc  # noqa: E402
import importers  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "app")


def js(name):
    return js_code_of(os.path.join(APP, name))


def hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


class _Rows(_Ledger, Base):
    """The Data ledger, with a way to say what api_sources reports per source."""

    sources: dict = {}

    def setUp(self):
        super().setUp()
        real = api_sources._sources

        def sources(conn):
            out = real(conn)
            for r in out:
                r.update(self.sources.get(r.get("key"), {}))
            return out
        p = mock.patch.object(api_sources, "_sources", sources)
        p.start()
        self.addCleanup(p.stop)

    def acts(self, out, key):
        return self.row(out, key)["detail"]["acts"]


# ── NC-215: nothing on the page is untrue ────────────────────────────────────

class TestThePageDoesNotSayNothingLeaves(unittest.TestCase):

    def test_the_headline_names_what_does_reach_the_internet(self):
        code = " ".join(js("data.js").split())   # the sentence wraps in the source
        self.assertNotIn("and nothing leaves it", code)
        for says in ("kept only on this Mac",
                     "LinkedIn messages, Gmail, Google contact photos and calendar link",
                     "LinkedIn profile facts come from your other",
                     "Anthropic, the company that makes Claude"):
            with self.subTest(says=says):
                self.assertIn(says, code)


class TestGreenMeansStillArriving(_Rows):

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"NC_GMAIL_AUTO": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_timer_source_three_checks_late_is_behind(self):
        """Gmail on a 12-hour clock whose last good check was 40 hours ago."""
        self.gmail_status = {"accounts": ["a@example.com"], "ready": ["a@example.com"],
                             "needs_file": [], "needs_sign_in": []}
        self.sources = {"gmail": {"last_run": hours_ago(40), "last_run_known": True}}
        r = self.row(self.ledger(), "gmail")
        self.assertEqual(r["state"]["tone"], "behind", r["state"])
        self.assertIn("40 hours", r["state"]["says"])
        self.assertIn("every 12 hours", r["state"]["says"])

    def test_two_checks_late_is_still_working(self):
        self.gmail_status = {"accounts": ["a@example.com"], "ready": ["a@example.com"],
                             "needs_file": [], "needs_sign_in": []}
        self.sources = {"gmail": {"last_run": hours_ago(25), "last_run_known": True}}
        self.assertEqual(self.row(self.ledger(), "gmail")["state"]["tone"], "ok")

    def test_the_clock_says_how_often_it_checks(self):
        self.assertIn("every_hours", repr(api_sources._how_it_runs(
            "gmail", {}, {"gmail": {"hours": 12, "hours_env": "UNSET_FOR_TEST"}})))


class TestASavedCalendarLinkSaysWhatItWaitsFor(_Rows):

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"NC_ICS_GOOGLE_URL": "https://calendar.example/x.ics"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_row_is_waiting_and_carries_the_switch(self):
        out = self.ledger()
        r = self.row(out, "calendar_google")
        self.assertEqual(r["state"]["tone"], "ready")
        switch = [a for a in r["detail"]["acts"] if a["kind"] == "switch"]
        self.assertEqual([(a["key"], a["on"]) for a in switch], [("NC_ICS_FETCH", False)])
        self.assertNotIn("import", [a["kind"] for a in r["detail"]["acts"]],
                         "Import could only fail while downloading is off")
        self.assertIn("your yes to this app fetching it from Google", r["takes"])

    def test_it_reaches_needs_you_with_the_same_switch(self):
        (need,) = [n for n in self.ledger()["needs"] if n["row"] == "calendar_google"]
        self.assertIn("switch", [a["kind"] for a in need["acts"]])

    def test_the_page_draws_the_switch(self):
        code = js("data.js")
        acts = code[code.index("function Acts("):code.index("function Record(")]
        self.assertIn('case "switch"', acts)

    def test_the_refusal_names_the_switch_by_its_label(self):
        import inspect
        import api_import
        src = inspect.getsource(api_import)
        self.assertIn('Switch on \\"Download linked calendars\\"', src)
        self.assertNotIn("the only request this app makes to the internet", src)


class TestAutomaticImportingOffIsANeed(_Rows):

    gmail_status = {"accounts": ["a@example.com"], "ready": ["a@example.com"],
                    "needs_file": [], "needs_sign_in": []}

    def test_it_says_how_many_sources_stop_and_carries_the_switch_back_on(self):
        with mock.patch.dict(os.environ, {"NC_AUTOSYNC": "0", "NC_GMAIL_AUTO": "1"}):
            needs = self.ledger()["needs"]
        (need,) = [n for n in needs if n["tone"] == "paused"]
        self.assertIn("Automatic importing is off", need["says"])
        self.assertEqual([(a["kind"], a["key"], a["on"]) for a in need["acts"]],
                         [("switch", "NC_AUTOSYNC", False)])

    def test_with_it_on_there_is_no_such_need(self):
        self.assertEqual([n for n in self.ledger()["needs"] if n["tone"] == "paused"], [])


class TestTheAllClearSaysWhatItIsBasedOn(_Rows):

    def test_the_answer_carries_the_count_and_the_newest_import(self):
        self.fda_status = fda("ok", ok=[importers.HELPER_IMESSAGE])
        self.sources = {"imessage": {"last_run": hours_ago(2), "last_run_known": True}}
        out = self.ledger()
        self.assertGreaterEqual(out["in_use"], 1)
        self.assertEqual(out["newest"]["row"], "imessage")
        self.assertAlmostEqual(out["newest"]["seconds"], 7200, delta=120)

    def test_the_page_waits_for_the_scan_before_saying_nothing(self):
        code = js("data.js")
        clear = code[code.index("function AllClear("):code.index("export function DataPage(")]
        self.assertLess(clear.index("if (!data.scanned)"), clear.index("Nothing."))
        self.assertIn("sources you use are all working", clear)
        self.assertIn("The newest import was", clear)


# ── NC-216: no dead ends ─────────────────────────────────────────────────────

class TestNoButtonThatCanOnlyFail(_Rows):

    def test_whatsapp_not_on_this_mac_is_not_the_place_to_start(self):
        out = self.ledger(scan=True)
        r = self.row(out, "whatsapp")
        self.assertTrue(r.get("absent"))
        self.assertIn("WhatsApp Desktop, installed and signed in", r["takes"])
        self.assertNotEqual((out["next"] or {}).get("row"), "whatsapp")

    def test_before_the_scan_whatsapp_is_not_judged_absent(self):
        self.assertFalse(self.row(self.ledger(), "whatsapp").get("absent"))

    def test_the_file_the_scan_found_is_already_in_the_box(self):
        self.found = [finding("whatsapp", "/x/ChatStorage.sqlite", auto=False)]
        (act,) = [a for a in self.acts(self.ledger(scan=True), "whatsapp") if a["kind"] == "import"]
        self.assertEqual(act["path"], "/x/ChatStorage.sqlite")
        self.assertTrue(act["needs_path"], "you can still choose another")

    def test_a_copied_source_imports_the_copy_without_asking_for_a_file(self):
        self.fda_status = fda("ok", ok=[importers.HELPER_IMESSAGE])
        (act,) = self.acts(self.ledger(), "imessage")
        self.assertEqual((act["kind"], act["path"], act["needs_path"]),
                         ("import", importers.IMSG_AUTO_DB, False))
        self.assertEqual(act["verb"], "Import now")
        self.assertEqual(act["path_says"], "The copy automatic copying made")

    def test_apple_calendar_without_the_copy_points_at_automatic_copying(self):
        self.assertEqual(self.acts(self.ledger(), "calendar_local"),
                         [{"kind": "row", "row": "helper", "label": "Set up automatic copying"}])

    def test_google_photos_before_gmail_points_at_gmail(self):
        self.assertEqual(self.acts(self.ledger(), "google_photos"),
                         [{"kind": "row", "row": "gmail", "label": "Set up Gmail first"}])

    def test_google_photos_with_gmail_can_import(self):
        self.gmail_status = {"accounts": ["a@example.com"], "ready": ["a@example.com"],
                             "needs_file": [], "needs_sign_in": []}
        self.assertEqual([a["kind"] for a in self.acts(self.ledger(), "google_photos")], ["import"])

    def test_ai_facts_without_a_key_opens_the_key_in_settings(self):
        (act,) = self.acts(self.ledger(), "facts_llm")
        self.assertEqual((act["kind"], act["row"]), ("settings", "ai"))

    def test_a_linkedin_token_with_no_messages_yet_says_to_press_import(self):
        with mock.patch.dict(os.environ, {"LINKEDIN_TOKEN": "made-up-token-for-a-test"}):
            r = self.row(self.ledger(), "linkedin_messages")
        self.assertTrue(r["state"]["says"].startswith("Connected. Press Import"), r["state"])


class TestYourNameIsAskedForWhereItMatters(_Rows):
    """Without your name and email the importers cannot tell your side of a
    conversation from anyone else's. Only the Settings page used to say so."""

    gmail_status = {"accounts": ["a@example.com"], "ready": ["a@example.com"],
                    "needs_file": [], "needs_sign_in": []}

    def test_a_source_that_needs_it_is_in_use_so_it_is_asked_for(self):
        with mock.patch.dict(os.environ, {"SELF_NAME": "", "SELF_EMAIL": ""}):
            needs = self.ledger()["needs"]
        (need,) = [n for n in needs if n["acts"] and n["acts"][0].get("row") == "identity"]
        self.assertEqual((need["tone"], need["acts"][0]["kind"]), ("ready", "settings"))

    def test_on_an_empty_install_it_is_not(self):
        self.gmail_status = {"accounts": [], "ready": [], "needs_file": [], "needs_sign_in": []}
        with mock.patch.dict(os.environ, {"SELF_NAME": "", "SELF_EMAIL": ""}):
            self.assertEqual(self.ledger()["needs"], [])


class TestTheLinkedinScraperSaysWhenToLeaveItAlone(_Rows):

    def status(self, **scraper):
        return {"state": "ok", "api": {}, "download": {},
                "scraper": {"coverage": {"enriched": 3}, "quiet_hours": 36, **scraper}}

    def ledger_with(self, status):
        with mock.patch.object(api_linkedin, "handle_linkedin_status", lambda conn: status), \
                mock.patch.dict(os.environ, {"IMAC": "me@scraper-host"}):
            return self.row(self.ledger(), "linkedin_facts")

    def test_a_cooldown_is_paused_and_offers_no_import(self):
        r = self.ledger_with(self.status(state={"in_cooldown": True}, queued=5))
        self.assertEqual(r["state"]["tone"], "paused")
        self.assertIn("Leave the other Mac alone", r["state"]["says"])
        self.assertNotIn("import", [a["kind"] for a in r["detail"]["acts"]])

    def test_nothing_new_for_longer_than_it_should_take_is_behind(self):
        r = self.ledger_with(self.status(state={}, queued=40, last_fact_age_hours=72))
        self.assertEqual(r["state"]["tone"], "behind")
        self.assertIn("3 days", r["state"]["says"])
        self.assertIn("40 people still waiting", r["state"]["says"])

    def test_a_recent_profile_is_working(self):
        r = self.ledger_with(self.status(state={}, queued=40, last_fact_age_hours=2))
        self.assertEqual(r["state"]["tone"], "ok")


class TestLinksBetweenThePagesOpenTheRow(unittest.TestCase):

    def test_a_row_link_is_repeatable_and_both_pages_get_it(self):
        main = js("main.js")
        one_shot = main[main.index("const ONE_SHOT_PARAMS"):main.index("const NONCE_PARAM")]
        self.assertIn('"row"', one_shot)
        for view in ("DataPage", "SettingsPage"):
            with self.subTest(view=view):
                self.assertRegex(main, rf"<\${{{view}}} key=\"[a-z-]+\" params=\${{route\.params}}")

    def test_each_page_opens_the_linked_row(self):
        for name in ("data.js", "settings.js"):
            with self.subTest(page=name):
                self.assertIn("useRowFromLink(params, !!data, openRow", js(name))

    def test_the_data_page_scrolls_again_once_the_scan_has_landed(self):
        """Its second answer can add needs above the row after the first scroll."""
        self.assertIn("useRowFromLink(params, !!data, openRow, !!data?.scanned, bringIntoView)",
                      js("data.js"))

    def test_the_data_page_links_to_the_settings_row(self):
        """It linked to #settings-next while the old Settings page held
        #settings. Since the switch-over (NC-236) that page is at #settings."""
        code = js("data.js")
        self.assertIn("go(a.row ? `settings?row=${a.row}` : \"settings\")", code)
        self.assertNotIn("settings-next", code)

    def test_the_settings_page_keeps_the_row_it_names(self):
        self.assertIn("go(a.row ? `${a.page}?row=${a.row}` : a.page)", js("settings.js"))


class TestAnImportFromNeedsYouKeepsItsResult(unittest.TestCase):

    def test_the_result_is_lifted_out_of_the_need_that_goes_away(self):
        code = js("data.js")
        self.assertIn("onResult=${setRan}", code)
        self.assertIn("log !== null && !onResult", code, "and is not drawn twice")


# ── NC-217: words written for this page ──────────────────────────────────────

class TestEachRowHasItsOwnSetupText(_Rows):

    OLD_PAGE = ("the panel at the top", "Found on this Mac", "Run import", "this button",
                "used to say", "under Settings", "Enrich")

    def test_no_row_points_at_the_old_page_or_names_a_setting_it_has_a_box_for(self):
        """A row that names a setting's internal name where it could point at
        the box is talking to whoever built it. NC-236 narrowed this: a setting
        with no box anywhere (LinkedIn's app id and secret, which only put a
        date on the token's expiry) can only be set in the settings file, and
        then its name is the one useful thing to say. So a named setting must be
        a real one, and must have no box on either page."""
        import api_abilities
        import settings as settings_mod
        rows = self.ledger()["rows"]
        boxed = {a.get("key") for r in rows + api_abilities.handle_settings_ledger(self.conn)["rows"]
                 for a in r["detail"]["acts"] if a.get("key")}
        for r in rows:
            text = r["detail"]["help"] + r["detail"]["manual"] + r["takes"]
            with self.subTest(row=r["key"]):
                for words in self.OLD_PAGE:
                    self.assertNotIn(words, text)
                for name in re.findall(r"\b(NC_[A-Z_]+|[A-Z]{3,}_[A-Z_]+)\b", text):
                    self.assertIn(name, settings_mod.MANAGED_KEYS, text)
                    self.assertNotIn(name, boxed, f"{name} has a box: point at it instead")

    def test_every_source_row_has_its_own_help(self):
        for r in self.ledger()["rows"]:
            if r["key"] != "helper":   # its panel is its help
                with self.subTest(row=r["key"]):
                    self.assertTrue(r["detail"]["help"])

    def test_the_group_limits_come_from_the_importers(self):
        with mock.patch.dict(os.environ, {"WA_GROUP_MAX": "11", "IMSG_GROUP_MAX": "5"}):
            out = self.ledger()
        self.assertIn("up to 11 members", self.row(out, "whatsapp")["detail"]["help"])
        self.assertIn("up to 5 members", self.row(out, "imessage")["detail"]["help"])

    def test_the_helper_is_called_by_what_it_does(self):
        out = self.ledger()
        self.assertEqual(self.row(out, "helper")["label"], "Automatic copying")
        self.assertNotIn("sync helper", repr([r["takes"] for r in out["rows"]]))


class TestAFoundFileIsDescribedTheWayFinderWould(_Rows):

    def test_a_date_a_folder_and_the_step(self):
        this_year = datetime.now().year
        path = os.path.join(os.path.expanduser("~"), "Downloads", "cards.vcf")
        self.found = [finding("contacts", path, mtime=f"{this_year}-03-14", count=27)]
        (need,) = self.ledger(scan=True)["needs"]
        self.assertIn("file from 14 March is in Downloads", need["says"])
        self.assertTrue(need["says"].endswith("Press Import to bring it in."), need["says"])

    def test_another_year_says_the_year(self):
        self.assertEqual(api_ledger._day("2024-03-01"), "1 March 2024")

    def test_folders_by_their_finder_names(self):
        home = os.path.expanduser("~")
        self.assertEqual(api_ledger._where(os.path.join(home, "Desktop", "x.ics")), "on your Desktop")
        self.assertEqual(api_ledger._where(os.path.join(home, "Downloads", "Takeout", "a.mbox")),
                         "in Takeout, inside Downloads")
        self.assertEqual(api_ledger._where("/Volumes/Stick/x.vcf"), "in the folder Stick")


class TestThisInstallInPlainNames(_Rows):

    def test_the_parts_have_names_not_file_names(self):
        with mock.patch.object(api_ledger.api_instance, "data_dir_bytes",
                               lambda: (9_000_000, {"archive.db": 4_000_000, "archive.db-wal": 1_000_000,
                                                    "chunk_vectors.npz": 3_000_000, "crm.db": 1_000_000})):
            named = self.ledger()["install"]["disk"]["named"]
        self.assertEqual(named, [{"name": "Saved messages", "bytes": 5_000_000},
                                 {"name": "Message search index", "bytes": 3_000_000},
                                 {"name": "People and notes", "bytes": 1_000_000}])

    def test_an_empty_install_has_nothing_to_back_up(self):
        self.assertTrue(self.ledger()["install"]["empty"])
        self.assertIn("Nothing to back up yet", js("data.js"))

    def test_the_export_buttons_say_what_you_get(self):
        code = js("data.js")
        for label in ("A document to read", "A spreadsheet", "For another program"):
            self.assertIn(label, code)


class TestTheMessageCountNeverCreatesAnArchive(Base):

    def test_a_fresh_install_gets_no_archive_file_from_a_page(self):
        missing = os.path.join(os.path.dirname(arc.ARCHIVE_DB_PATH), "no-archive-here.db")
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", missing):
            self.assertEqual(api_setup._archive_messages_by_channel(), {})
        self.assertFalse(os.path.exists(missing))


class TestTheNowBlock(unittest.TestCase):

    def test_it_does_not_round_to_the_day(self):
        code = js("data.js")
        last = code[code.index("function lastRun("):code.index("const PART_LIMIT")]
        self.assertIn("agoWords(detail.last_run_seconds)", last)

    def test_an_off_row_does_not_say_once_it_is_on_only_when(self):
        code = js("data.js")
        row = code[code.index("function SourceRow("):code.index("function AllClear(")]
        self.assertIn("/^Only when/.test(d.runs)", row)
        self.assertIn("row.state.says ? \"\"", row)

    def test_a_working_row_folds_its_setup_steps(self):
        self.assertIn("Show how it was set up", js("data.js"))


# ── NC-218: Settings ─────────────────────────────────────────────────────────

class TestSettingsWords(_Settings):

    def test_reasons_are_under_how_not_the_other_way_in(self):
        for key in ("identity", "intake_web"):
            with self.subTest(row=key):
                r = self.row(key)
                self.assertEqual(r["detail"]["manual"], "")
                self.assertTrue(r["detail"]["help"])
        self.assertIn("how=${d.help", js("settings.js"))

    def test_goals_names_what_reads_it(self):
        r = self.row("goals")
        self.assertIn("what you are working on", r["gives"])
        self.assertNotIn("five features", r["takes"])

    def test_meaning_search_once_installed_costs_nothing(self):
        self.search = {"available": True, "size_gb": 2, "install": {"state": "succeeded"}}
        self.assertEqual(self.row("semantic")["cost"]["kind"], "free")

    def test_the_key_page_is_on_the_allowlist(self):
        self.assertEqual(importers.OPEN_TARGETS["anthropic_keys"]["url"],
                         "https://console.anthropic.com/settings/keys")

    def test_the_calendar_link_act_names_the_row_it_opens(self):
        acts = self.row("ics_fetch")["detail"]["acts"]
        (page,) = [a for a in acts if a["kind"] == "page"]
        self.assertEqual((page["page"], page["row"]), ("data", "calendar_google"))


class TestTheBoxesCheckWhatYouType(unittest.TestCase):
    """NC-218. The old page refused these; the rebuilt boxes had lost the checks."""

    def run_node(self, probe):
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        code = js("ledger.js")
        body = (code[code.index("const CALENDAR_ADDRESS"):code.index("export function CredentialBox(")]
                + code[code.index("export function textProblem("):code.index("export function TextSetting(")])
        body = body.replace("export ", "")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(body + "\n" + probe)
        try:
            return subprocess.run([node, fh.name], capture_output=True, text=True,
                                  timeout=20).stdout.strip().splitlines()
        finally:
            os.unlink(fh.name)

    def test_each_rule(self):
        out = self.run_node("""
const say = (k, v) => console.log(JSON.stringify(secretProblem(v, "", k)));
say("ANTHROPIC_API_KEY", "sk-proj-abc"); say("ANTHROPIC_API_KEY", "sk-ant-abc");
say("NC_ICS_GOOGLE_URL", "calendar.google.com/x"); say("NC_ICS_ICLOUD_URL", "webcal://p1.icloud.com/x");
console.log(JSON.stringify(textProblem("SELF_EMAIL", "me@home")));
console.log(JSON.stringify(textProblem("SELF_EMAIL", "me@example.com")));
console.log(JSON.stringify(textProblem("SELF_EMAIL", "")));
""")
        self.assertEqual(len(out), 7, out)
        self.assertIn("start with sk-ant-", out[0])
        self.assertEqual(out[1], '""')
        self.assertIn("https:// or webcal://", out[2])
        self.assertEqual(out[3], '""')
        self.assertIn("whole email address", out[4])
        self.assertEqual(out[5:], ['""', '""'], "a real address, and an empty box that clears")

    def test_a_typed_value_says_it_is_not_saved_yet(self):
        self.assertIn("Not saved yet. Press Save or Return.", js("ledger.js"))


# ── NC-219: keyboard and screen readers ──────────────────────────────────────

class TestRowsForKeyboardsAndScreenReaders(unittest.TestCase):

    def test_a_row_has_a_short_spoken_name_and_is_a_list_item(self):
        code = js("ledger.js")
        row = code[code.index("export function LedgerRow("):code.index("export const DETAIL_BLOCKS")]
        self.assertIn('role="listitem"', row)
        self.assertIn("aria-label=${[name, TONE_SAYS[tone] || tone, have]", row)
        self.assertIn("aria-controls=${`${id}-body`}", row)

    def test_opening_a_row_moves_focus_and_respects_less_motion(self):
        code = js("ledger.js")
        opener = code[code.index("export function useOpenRow("):code.index("export function useRowFromLink(")]
        self.assertIn("prefers-reduced-motion: reduce", opener)
        self.assertIn(".focus({ preventScroll: true })", opener)

    def test_the_helper_panel_sits_under_the_rows_headings(self):
        self.assertIn('<${FdaPanel} onChanged=${onChanged} heading="h4" />', js("data.js"))
        self.assertNotIn("<h2>", js("fda.js"))


# ── NC-236: what came across from the old pages when they were deleted ──────

class TestTheSwitchOverKeptWhatTheOldDataPageDid(_Rows):

    def test_a_check_that_fails_is_not_a_check_that_found_nothing(self):
        """NC-033 / NC-034. The scan blocks on a folder permission nobody has
        seen, or fails; either way the page must not say "Nothing"."""
        import api_import
        with mock.patch.object(api_import, "handle_import_detect", side_effect=OSError("no")):
            out = self.ledger(scan=True)
        self.assertTrue(out["scan_failed"])
        self.assertEqual(out["needs"], [])
        self.assertFalse(self.ledger(scan=True)["scan_failed"])
        self.assertFalse(self.ledger()["scan_failed"], "no scan asked for, none failed")

    def test_the_page_says_the_check_failed_and_keeps_the_rows(self):
        code = js("data.js")
        load = code[code.index("const load = useCallback"):code.index("useEffect(() => { load(true); }")]
        self.assertIn("if (!shown) throw e;", load, "a failed check blanks rows already drawn")
        self.assertIn('/did not answer/.test(e?.message || "") ? "timeout" : "error"', load)
        self.assertIn('setScanFailed(full.scan_failed ? "error" : "")', load)
        self.assertIn("Files\n        and Folders", code)
        clear = code[code.index("function AllClear("):code.index("export function DataPage(")]
        self.assertLess(clear.index("if (scanFailed)"), clear.index("if (!data.scanned)"),
                        "a failed check is said before 'Checking…', which would never end")

    def test_the_page_waits_as_long_as_the_server_lets_an_import_run(self):
        """NC-176. The old page told the import runner the server's ceiling;
        without it a long import reads as failed while it goes on and succeeds."""
        import api_import
        with mock.patch.object(api_import, "import_timeout_sec", lambda: 1234):
            self.assertEqual(self.ledger()["import_timeout_sec"], 1234)
        code = js("data.js")
        self.assertEqual(code.count("noteImportCeiling("), 2, "both answers, quick and full")

    def test_a_gmail_address_can_be_removed(self):
        """The setup can only add one; the old Settings page's list box was the
        only way to take one away."""
        self.gmail_status = {"accounts": ["a@example.com"], "ready": ["a@example.com"],
                             "needs_file": [], "needs_sign_in": []}
        box = [a for a in self.acts(self.ledger(), "gmail") if a["kind"] == "text"]
        self.assertEqual([(a["key"], a["name"]) for a in box], [("GMAIL_ACCOUNTS", "Gmail addresses")])
        self.gmail_status = {"accounts": [], "ready": [], "needs_file": [], "needs_sign_in": []}
        self.assertEqual([a["kind"] for a in self.acts(self.ledger(), "gmail")], ["gmail"],
                         "with no address, the setup is the way in")


class TestTheSwitchOverKeptWhatTheOldSettingsPageDid(_Settings):

    def test_a_failed_install_names_its_log(self):
        self.deps = {"ok": False, "message": "x", "log_file": "/tmp/nc-install.log"}
        (need,) = [n for n in self.ledger()["needs"] if n["tone"] == "blocked"]
        self.assertIn("What went wrong is written in /tmp/nc-install.log.", need["says"])
        self.deps = {"ok": False, "message": "x"}
        (need,) = [n for n in self.ledger()["needs"] if n["tone"] == "blocked"]
        self.assertNotIn("written in", need["says"])

    def test_ask_opens_the_ai_row(self):
        """Ask's "Open Settings" landed at the top of the old page; the key box
        is in the AI row now."""
        code = js("ask.js")
        self.assertEqual(code.count('go("settings?row=ai")'), 2)
        self.assertNotIn('go("settings")', code)


if __name__ == "__main__":
    unittest.main()


# ── The decisions of NC-220 ──────────────────────────────────────────────────

class TestDuplicatesLiveInThisInstall(_Rows):
    """NC-228. The finder moves from the old page into This install."""

    def test_the_ledger_counts_the_groups(self):
        import db as crm_db
        for name in ("Ada Lovelace", "Ada Lovelace", "Grace Hopper"):
            self.conn.execute("INSERT INTO contacts (name, name_norm) VALUES (?, ?)",
                              (name, crm_db.normalize_name(name)))
        self.conn.commit()
        self.assertEqual(self.ledger()["install"]["duplicates"], 1)

    def test_none_on_an_empty_install(self):
        self.assertEqual(self.ledger()["install"]["duplicates"], 0)

    def test_the_page_draws_it_with_the_old_pages_merge_control(self):
        code = js("data.js")
        self.assertIn("Possible duplicates", code)
        self.assertIn("<${DupGroup}", code)
        self.assertIn("<${MergeModal}", code)
        # The old page's group of possible duplicates came with the switch-over.
        self.assertIn("export function DupGroup(", code)


class TestRowsYouUseComeFirst(unittest.TestCase):
    """NC-229. The rule itself is pinned in test_the_data_ledger_is_one_answer."""

    def test_the_note_says_so_only_when_some_are_in_use(self):
        code = " ".join(js("data.js").split())
        self.assertIn('data.in_use > 0 ? "The ones you use first', code)


class TestEffortIsWords(unittest.TestCase):
    """NC-230. Colour means state only; the chips are plain words."""

    def test_the_labels(self):
        self.assertEqual({k: v[1] for k, v in api_ledger.COSTS.items()
                          if k in ("secret", "sign_in", "machine")},
                         {"secret": "a code to paste", "sign_in": "a Google setup",
                          "machine": "a second Mac"})

    def test_the_chip_has_no_dot_and_the_column_is_effort(self):
        code = js("ledger.js")
        chip = code[code.index("export function CostChip("):code.index("const TONE_SAYS")]
        self.assertNotIn("lg-cost-dot", chip)
        self.assertNotIn("is-${cost.tone}", chip)
        for page in ("data.js", "settings.js"):
            with self.subTest(page=page):
                self.assertIn("<span>Effort</span>", js(page))
                self.assertNotIn("<span>Cost</span>", js(page))


class TestTheAutomaticWorkIsOneRow(_Rows):
    """NC-231. Closeness, Facts from your messages and How you write, folded."""

    def test_one_row_last_with_its_three_parts_in_order(self):
        out = self.ledger()
        r = out["rows"][-1]
        self.assertEqual((r["key"], r["label"]), ("worked_out", "Worked out on this Mac"))
        self.assertEqual([p["key"] for p in r["detail"]["parts"]], ["engagement", "facts", "style"])
        for label in ("**Closeness.**", "**Facts from your messages.**", "**How you write.**"):
            self.assertIn(label, r["detail"]["help"])
        self.assertFalse({"engagement", "facts", "style"} & {x["key"] for x in out["rows"]})

    def test_it_is_never_the_next_thing_or_counted_as_a_source(self):
        import db as crm_db
        crm_db.state_set(self.conn, "import_synced_engagement", hours_ago(1))
        self.conn.commit()
        out = self.ledger()
        self.assertTrue(self.row(out, "worked_out")["in_use"])
        self.assertEqual(out["in_use"], 0, "no source is in use, only the automatic work")
        self.assertNotEqual((out["next"] or {}).get("row"), "worked_out")
        self.assertIsNone(out["newest"], "the automatic work is not an import")

    def test_with_importing_off_it_is_not_one_of_the_sources_that_stop(self):
        import db as crm_db
        crm_db.state_set(self.conn, "import_synced_engagement", hours_ago(1))
        self.conn.commit()
        with mock.patch.dict(os.environ, {"NC_AUTOSYNC": "0"}):
            needs = self.ledger()["needs"]
        self.assertEqual([n for n in needs if n["tone"] == "paused"], [])

    def test_the_row_says_what_is_wrong_with_its_worst_part(self):
        import db as crm_db
        for key in ("engagement", "facts"):
            crm_db.state_set(self.conn, f"import_synced_{key}", hours_ago(1))
        self.conn.commit()
        self.blockers = {"facts": "The facts pass could not open the archive"}
        r = self.row(self.ledger(), "worked_out")
        self.assertEqual(r["state"]["tone"], "blocked", r["state"])
        self.assertIn("could not open the archive", r["state"]["says"])

    def test_the_page_lists_the_parts_under_now(self):
        code = js("data.js")
        row = code[code.index("function SourceRow("):code.index("function AllClear(")]
        self.assertIn("if (d.parts)", row)
        self.assertIn("<${Parts} parts=${d.parts} />", row)
