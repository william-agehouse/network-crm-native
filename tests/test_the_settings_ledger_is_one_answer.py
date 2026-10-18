"""NC-114. The rebuilt Settings page asks one endpoint, and it tells the truth.

GET /api/settings/ledger is the eight things the app can do that are off until you
give it something, what needs you, and what this install is. The same row shape
as the Data page's ledger, drawn by the same parts. These tests pin what each
row says in each state it can be in, with no setting borrowed from the
machine running them.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app  # noqa: E402

import api_abilities  # noqa: E402
import api_instance  # noqa: E402
import owner  # noqa: E402

QUIET_ENV = {"SELF_NAME": "", "SELF_EMAIL": "", "ANTHROPIC_API_KEY": "",
             "ANTHROPIC_MODEL": "", "NC_AUTOSYNC": "1", "NC_INTAKE_WEB": "0",
             "NC_ICS_FETCH": "0", "SCRAPE_OVERRIDES_MANUAL": "0",
             "NC_ICS_GOOGLE_URL": "", "NC_ICS_ICLOUD_URL": "", "ENRICH_LLM_MAX": ""}
NO_SEARCH = {"available": False, "size_gb": 2, "install": {"state": "idle"}, "reason": ""}


class _Settings(Base):
    search = NO_SEARCH
    deps = {"ok": True}

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, QUIET_ENV)
        env.start()
        self.addCleanup(env.stop)
        for attr, value in (("semantic_search_status", lambda: dict(self.search)),
                            ("deps_status", lambda: dict(self.deps))):
            p = mock.patch.object(api_instance, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def ledger(self):
        return api_abilities.handle_settings_ledger(self.conn)

    def row(self, key, out=None):
        return next(r for r in (out or self.ledger())["rows"] if r["key"] == key)


class TestTheEightRows(_Settings):

    def test_eight_rows_in_the_designed_order(self):
        """Nine at one point: the scrape switch moved to its LinkedIn row on
        Data (NC-234) and setting up again into This install (NC-235). Then the
        dates look-ahead, which the old page had a box for, became a row of its
        own when that page went (NC-236)."""
        self.assertEqual([r["key"] for r in self.ledger()["rows"]],
                         ["identity", "goals", "autosync", "intake_web", "ics_fetch",
                          "dates", "ai", "semantic"])

    def test_none_of_the_six_moved_credentials_is_here(self):
        """They live on their sources' rows on Data now, with no copy here."""
        text = repr(self.ledger())
        for key in ("LINKEDIN_TOKEN", "GMAIL_ACCOUNTS", '"IMAC"', '"KEY"',
                    "NC_ICS_GOOGLE_URL", "NC_ICS_ICLOUD_URL"):
            with self.subTest(key=key):
                self.assertNotIn(key, text)

    def test_a_row_whose_facts_fail_is_still_listed(self):
        with mock.patch.object(api_abilities, "ROWS",
                               [(k, lbl, (lambda conn: 1 / 0) if k == "goals" else f)
                                for k, lbl, f in api_abilities.ROWS]):
            r = self.row("goals")
        self.assertEqual(r["state"]["tone"], "na")
        self.assertEqual(len(self.ledger()["rows"]), 8)

    def test_the_endpoint_is_reachable(self):
        self.assertIn(api_abilities, app.ROUTE_MODULES)


class TestWhatEachRowSays(_Settings):

    def test_no_name_or_email_is_a_need(self):
        out = self.ledger()
        # Waiting for you, not "Stopped": a name never typed has not stopped (NC-218).
        self.assertEqual(self.row("identity", out)["state"], {"tone": "ready", "says": "Not set"})
        self.assertEqual(out["needs"][0]["tone"], "ready")
        self.assertEqual([n["row"] for n in out["needs"]], ["identity"])
        self.assertIn("the user", out["needs"][0]["says"],
                      "the whole sentence is under Needs you, once, not on the row too")

    def test_with_both_set_nothing_needs_you(self):
        with mock.patch.dict(os.environ, {"SELF_NAME": "Plain Person", "SELF_EMAIL": "p@example.com"}):
            out = self.ledger()
        self.assertEqual(out["needs"], [])
        self.assertEqual(self.row("identity", out)["have"]["says"], "Plain Person · p@example.com")

    def test_what_you_are_working_on_counts_its_words(self):
        owner.save_goals(self.conn, "Planning a community garden for our street")
        self.assertEqual(self.row("goals")["have"]["says"], "7 words")

    def test_automatic_work_off_is_paused_not_off(self):
        with mock.patch.dict(os.environ, {"NC_AUTOSYNC": "0"}):
            r = self.row("autosync")
        self.assertEqual(r["state"]["tone"], "paused")
        self.assertIn("press Import", r["takes"])

    def test_calendar_downloads_on_with_nothing_linked_says_so(self):
        with mock.patch.dict(os.environ, {"NC_ICS_FETCH": "1"}):
            r = self.row("ics_fetch")
        self.assertIn("no calendar is linked", r["state"]["says"])
        with mock.patch.dict(os.environ, {"NC_ICS_FETCH": "1",
                                          "NC_ICS_GOOGLE_URL": "https://calendar.example/x.ics"}):
            r = self.row("ics_fetch")
        self.assertEqual((r["state"]["tone"], r["have"]["says"]), ("ok", "On · Google linked"))

    def test_the_ai_row_names_its_price_and_its_cap(self):
        with mock.patch.dict(os.environ, {"ENRICH_LLM_MAX": "12"}):
            r = self.row("ai")
        self.assertEqual(r["cost"]["kind"], "money")
        self.assertIn("at most 12 people", r["takes"])
        self.assertEqual([a["kind"] for a in r["detail"]["acts"]], ["secret", "open"],
                         "the model is chosen once there is a key to use it with")
        # NC-218. The box alone was a dead end: it now says where a key is made,
        # and what Anthropic is.
        self.assertEqual(r["detail"]["acts"][1]["target"], "anthropic_keys")
        self.assertIn("the company that makes Claude", r["takes"])

    def test_with_a_key_the_model_can_be_chosen_and_the_key_is_masked(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-made-up-9999"}):
            r = self.row("ai")
        # NC-364: the spend cap and the two prices come with the key, like the model.
        self.assertEqual([a["kind"] for a in r["detail"]["acts"]], ["secret", "model", "text", "text", "text"])
        self.assertNotIn("made-up", repr(r))

    def test_a_search_install_that_needs_a_restart_is_a_need(self):
        self.search = {**NO_SEARCH, "install": {"state": "succeeded", "restart_required": True}}
        out = self.ledger()
        self.assertEqual(self.row("semantic", out)["state"]["tone"], "behind")
        self.assertIn("semantic", [n["row"] for n in out["needs"]])

    def test_a_search_install_still_running_is_not_a_need(self):
        """Nothing for you to do while it downloads, so it stays on its row."""
        self.search = {**NO_SEARCH, "install": {"state": "running"}}
        out = self.ledger()
        self.assertEqual(self.row("semantic", out)["state"], {"tone": "behind", "says": "Installing"})
        self.assertNotIn("semantic", [n["row"] for n in out["needs"]])

    def test_missing_features_are_a_need_with_the_fix_in_the_sentence(self):
        """NC-218. The marker's own text reached the page, and it can be
        "dependency status file is malformed"; the need now says what to do."""
        self.deps = {"ok": False, "message": "dependency status file is malformed",
                     "retry": "Reopen Network CRM while online to retry automatically"}
        says = [n["says"] for n in self.ledger()["needs"]][-1]
        self.assertIn("Quit and reopen the app while connected to the internet", says)
        self.assertIn("Everything else works without them", says)
        self.assertNotIn("malformed", says)

    def test_this_install_says_whether_it_checks_for_updates(self):
        out = self.ledger()["install"]
        self.assertTrue(out["version"])
        self.assertIn("does not check for updates", out["updates_says"])


class TestTwoRowsMovedOut(_Settings):
    """NC-234 and NC-235."""

    def test_the_scrape_switch_is_not_a_settings_row(self):
        self.assertNotIn("scrape_overrides", [r["key"] for r in self.ledger()["rows"]])

    def test_setting_up_again_is_in_this_install(self):
        with mock.patch.object(api_instance, "onboarding_done", lambda conn: True):
            self.assertEqual(self.ledger()["install"]["setup_again"], {"done": True})
        with mock.patch.object(api_instance, "onboarding_done", lambda conn: False):
            self.assertEqual(self.ledger()["install"]["setup_again"], {"done": False})

    def test_a_failing_onboarding_check_still_draws_this_install(self):
        def boom(conn):
            raise RuntimeError("no state table")
        # NC-350: a failed read counts as done, as the web page has always
        # counted it, so setup is not offered again by mistake.
        with mock.patch.object(api_instance, "onboarding_done", boom):
            self.assertEqual(self.ledger()["install"]["setup_again"], {"done": True})

    def test_the_page_draws_it_there(self):
        from tests.helpers import js_code_of
        code = js_code_of(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "static", "app", "settings.js"))
        self.assertIn("<${SetupAgain} />", code)
        self.assertNotIn('"setup_again"', code, "no row act for it is left")


class TestTheDatesRow(_Settings):
    """NC-236. A row of its own on Settings."""

    def test_it_says_how_far_ahead_it_looks(self):
        r = self.row("dates")
        self.assertEqual(r["have"]["says"], "Looks 14 days ahead", "the default")
        with mock.patch.dict(os.environ, {"NC_DATES_LOOKAHEAD_DAYS": "30"}):
            self.assertEqual(self.row("dates")["have"]["says"], "Looks 30 days ahead")
        with mock.patch.dict(os.environ, {"NC_DATES_LOOKAHEAD_DAYS": "1"}):
            self.assertEqual(self.row("dates")["have"]["says"], "Looks 1 day ahead")

    def test_it_says_what_the_strip_actually_uses(self):
        """A value the strip would clamp is shown clamped, not as typed."""
        with mock.patch.dict(os.environ, {"NC_DATES_LOOKAHEAD_DAYS": "9000"}):
            self.assertEqual(self.row("dates")["have"]["says"], "Looks 365 days ahead")

    def test_its_box_is_the_setting_the_strip_reads(self):
        (act,) = self.row("dates")["detail"]["acts"]
        self.assertEqual((act["kind"], act["key"]), ("text", "NC_DATES_LOOKAHEAD_DAYS"))
        import settings as settings_mod
        self.assertIn("NC_DATES_LOOKAHEAD_DAYS", settings_mod.MANAGED_KEYS)

    def test_the_box_refuses_what_is_not_a_number_of_days(self):
        from tests.helpers import js_code_of
        code = js_code_of(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "static", "app", "ledger.js"))
        rules = code[code.index("export const FORMAT"):code.index("export function secretProblem")]
        self.assertIn("NC_DATES_LOOKAHEAD_DAYS:", rules)
        self.assertIn("+v >= 1 && +v <= 365", rules)


if __name__ == "__main__":
    unittest.main()
