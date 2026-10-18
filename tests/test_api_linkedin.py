"""crm/api_linkedin.py: the three-lane LinkedIn status panel.

Most of what is asserted here is what the endpoint must NOT do. It runs on every
visit to the Data page, and the three cheapest ways to write it are all hazards:
probing the token costs a live LinkedIn API call, probing the scraper Mac costs a
15-second SSH against a machine that may be asleep, and probing ~/Downloads for
the export CSV can HANG the process on a macOS TCC consent dialog that
`except OSError` cannot catch. Each of those has a test below, because each would
look fine on a developer's machine on a good day and fail on the one that
matters.
"""

import inspect
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db  # noqa: E402
import api_linkedin  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "crm", "api_linkedin.py")


def source() -> str:
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


def code_only() -> str:
    """The module's code with comments and docstrings stripped.

    Every prose gate in this file goes through here. The reason is a common trap:
    a test that greps the source for a forbidden string matches the comment
    explaining why the string is forbidden, and passes for exactly the wrong
    reason. This module's header talks at length about ssh,
    subprocess and ~/Downloads precisely because it must not use them.
    """
    import ast
    text = source()
    tree = ast.parse(text)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            spans.append((node.lineno, node.end_lineno))
    lines = text.splitlines()
    for start, end in spans:
        for i in range(start - 1, min(end, len(lines))):
            lines[i] = ""
    return "\n".join(l.split("#")[0] if "#" in l else l for l in lines)


class LinkedInBase(Base):
    """A synthetic crm.db with a known LinkedIn shape, so every count below is a
    number this file chose rather than whatever is on the machine today."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="li_status_")
        self._env = {k: os.environ.get(k) for k in (
            "LINKEDIN_TOKEN", "IMAC", "NC_LINKEDIN_AUTO", "NC_AUTOSYNC",
            "NC_LINKEDIN_EVERY_HOURS")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def seed(self, rows):
        """rows: list of (name, linkedin_url, facts_source, facts_updated_at, li_extra)."""
        for name, url, fsrc, fat, extra in rows:
            cid = db.create_contact(self.conn, name)
            self.conn.execute(
                "UPDATE contacts SET linkedin_url=?, facts_source=?, "
                "facts_updated_at=?, li_extra=? WHERE id=?",
                (url, fsrc, fat, extra, cid))
        self.conn.commit()

    def status(self):
        return api_linkedin._status(self.conn)


# ════════════════════════════════════════════════════════════════════════════
class TestItNeverReachesOffTheMachine(LinkedInBase):
    """The three probes that would make this endpoint correct and unusable.

    None of them is hypothetical. `linkedin_import.py` really does test the token
    by calling api.linkedin.com; `merge_scraped_facts.py` really does reach the
    scraper Mac by ssh; and `importers.detect_sources()` really does glob ~/Downloads.
    Each is the obvious way to fill a field in this payload, and each would be
    paid on every page render.
    """

    def test_the_status_path_starts_no_subprocess_and_opens_no_socket(self):
        """The strongest form of the check: run the real thing with subprocess
        and urllib sabotaged, rather than grepping for their names."""
        self.seed([("A", "https://li/a", "linkedin_scrape", "2026-08-01T00:00:00+00:00", "{\"x\":1}")])

        def boom(*a, **k):
            raise AssertionError("the status endpoint reached off the machine")

        import subprocess as sp
        import socket as sk
        with mock.patch.object(sp, "run", boom), \
             mock.patch.object(sp, "Popen", boom), \
             mock.patch.object(sp, "check_output", boom), \
             mock.patch.object(sk.socket, "connect", boom):
            out = self.status()
        self.assertIn("scraper", out)

    def test_it_does_not_import_the_modules_that_do_the_reaching(self):
        code = code_only()
        for banned in ("import subprocess", "import requests", "import urllib",
                       "import ssh", "from subprocess", "from requests"):
            self.assertNotIn(banned, code,
                             f"api_linkedin imports {banned!r} — a status endpoint "
                             f"on the page-render path must not")

    def test_it_never_globs_the_tcc_protected_folders(self):
        """~/Downloads and ~/Desktop can BLOCK on a consent dialog on current macOS,
        and a blocked syscall raises nothing, so the process hangs rather than
        failing. The export lane is fed from the Data page's existing
        detect_sources() call instead — see the module docstring."""
        code = code_only()
        for banned in ("Downloads", "Desktop", "glob(", "iglob", "scandir"):
            self.assertNotIn(banned, code,
                             f"api_linkedin touches {banned!r}; that probe can hang "
                             f"the app on a TCC prompt")

    def test_it_does_not_call_detect_sources_either(self):
        """Same hazard one level up: detect_sources() is what does the globbing."""
        self.assertNotIn("detect_sources", code_only())


# ════════════════════════════════════════════════════════════════════════════
class TestTheTokenNeverLeavesInThePayload(LinkedInBase):
    """`LINKEDIN_TOKEN` is a live credential. The panel needs to know whether one
    is set; it never needs the value, and this endpoint is reachable from the
    browser."""

    TOKEN = "AQV-thisIsTheWholeSecretValue-0123456789-XYZW"

    def test_the_raw_token_appears_nowhere_in_the_response(self):
        os.environ["LINKEDIN_TOKEN"] = self.TOKEN
        blob = json.dumps(self.status())
        self.assertNotIn(self.TOKEN, blob)
        # Not just the whole string — no run of it long enough to be useful.
        self.assertNotIn(self.TOKEN[:20], blob)
        self.assertNotIn(self.TOKEN[8:28], blob)

    def test_presence_is_reported_and_is_the_truth(self):
        os.environ["LINKEDIN_TOKEN"] = self.TOKEN
        self.assertTrue(self.status()["api"]["token_present"])
        os.environ["LINKEDIN_TOKEN"] = "   "        # whitespace is not a token
        self.assertFalse(self.status()["api"]["token_present"])
        os.environ.pop("LINKEDIN_TOKEN")
        self.assertFalse(self.status()["api"]["token_present"])

    def test_it_reports_presence_and_never_validity(self):
        """There is no offline way to know a token still works, and the online
        way is a billed call to LinkedIn on the page-render path. A field called
        anything like `token_valid` would therefore be a guess presented as a
        fact."""
        os.environ["LINKEDIN_TOKEN"] = self.TOKEN
        keys = set(self.status()["api"])
        for forbidden in ("token_valid", "token_ok", "token_works", "token_expired"):
            self.assertNotIn(forbidden, keys)


# ════════════════════════════════════════════════════════════════════════════
class TestTheThreeLanesAreActuallySeparate(LinkedInBase):
    """The whole point of the panel: the data for LinkedIn comes from three
    places, the download, the API and the scraper, and that is a lot going on.
    Collapsing any two of them back into one number is the bug this module
    exists to prevent."""

    def test_all_three_lanes_are_present(self):
        out = self.status()
        for lane in ("download", "api", "scraper"):
            self.assertIn(lane, out, f"the {lane} lane is missing from the payload")
            self.assertIsInstance(out[lane], dict)

    def test_the_scraper_lane_separates_covered_queued_and_unreachable(self):
        now = datetime.now(timezone.utc).isoformat()
        self.seed([
            ("Scraped One", "https://li/1", "linkedin_scrape", now, '{"a":1}'),
            ("Scraped Two", "https://li/2", "linkedin_scrape", now, '{"a":1}'),
            ("Queued One",  "https://li/3", "", "", ""),
            ("Queued Two",  "https://li/4", "linkedin_csv", now, ""),
            ("No URL",      "",             "", "", ""),
        ])
        sc = self.status()["scraper"]
        self.assertEqual(sc["scraped"], 2)
        self.assertEqual(sc["queued"], 2, "a contact with a URL and no scrape is queued")
        self.assertEqual(sc["with_url"], 4)
        self.assertEqual(sc["no_url"], 1, "someone with no URL is unreachable, not queued")

    def test_a_manual_override_is_not_queued_for_rescraping(self):
        """facts_source precedence (invariant 4): `manual` outranks
        `linkedin_scrape`, so re-scraping that person could only overwrite a
        better answer with a worse one. pick_candidates excludes them and so must
        the count, or the panel advertises work that will never be done."""
        self.seed([("Hand Edited", "https://li/m", "manual", "", "")])
        self.assertEqual(self.status()["scraper"]["queued"], 0)

    def test_extras_are_counted_not_derived_from_the_scrape_total(self):
        """`scraped - extras` looks like the pending count and is not: the two
        populations overlap without nesting, because a contact can keep extras
        from an earlier scrape after a manual edit takes over facts_source. Here
        the subtraction would say -1 (clamped to 0) while the truth is 1."""
        now = datetime.now(timezone.utc).isoformat()
        self.seed([
            ("Scraped, has extras",  "https://li/1", "linkedin_scrape", now, '{"a":1}'),
            ("Scraped, NO extras",   "https://li/2", "linkedin_scrape", now, ""),
            ("Manual, kept extras",  "https://li/3", "manual", now, '{"a":1}'),
            ("Manual, kept extras2", "https://li/4", "manual", now, '{"a":1}'),
        ])
        sc = self.status()["scraper"]
        self.assertEqual(sc["scraped"], 2)
        self.assertEqual(sc["extras"], 3)
        self.assertEqual(sc["extras_pending"], 1,
                         "one scraped contact has no extras; subtraction would say 0")

    def test_an_empty_li_extra_json_does_not_count_as_extras(self):
        """`{}` is what the extractor writes when it found nothing, and it must
        not read as coverage — mirrors enrich_li_extras._is_populated."""
        now = datetime.now(timezone.utc).isoformat()
        self.seed([
            ("Empty dict", "https://li/1", "linkedin_scrape", now, "{}"),
            ("Empty list", "https://li/2", "linkedin_scrape", now, "[]"),
            ("Null",       "https://li/3", "linkedin_scrape", now, "null"),
            ("Real",       "https://li/4", "linkedin_scrape", now, '{"headline":"x"}'),
        ])
        sc = self.status()["scraper"]
        self.assertEqual(sc["extras"], 1)
        self.assertEqual(sc["extras_pending"], 3)


# ════════════════════════════════════════════════════════════════════════════
class TestTheAttemptStampIsNotCalledASuccess(LinkedInBase):
    """api_import writes `autosync_api_attempt_linkedin` BEFORE the call, so a
    dead token backs off instead of retrying every tick. Rendering it as "last
    synced" would lie in precisely the case that matters most: a token that
    expired, where the panel would show a fresh green timestamp."""

    def test_the_field_is_named_for_what_it_is(self):
        stamp = datetime.now(timezone.utc).isoformat()
        db.state_set(self.conn, "autosync_api_attempt_linkedin", stamp)
        self.conn.commit()
        api = self.status()["api"]
        self.assertEqual(api["last_attempt"], stamp)
        self.assertNotIn("last_success", api)
        self.assertNotIn("last_sync", api)
        self.assertNotIn("synced_at", api)

    def test_the_next_due_time_follows_the_configured_interval(self):
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        db.state_set(self.conn, "autosync_api_attempt_linkedin", base.isoformat())
        self.conn.commit()
        os.environ["NC_LINKEDIN_EVERY_HOURS"] = "6"
        api = self.status()["api"]
        self.assertEqual(api["every_hours"], 6)
        due = datetime.fromisoformat(api["next_attempt_due"])
        self.assertAlmostEqual((due - base).total_seconds(), 6 * 3600, delta=2)

    def test_a_garbage_interval_falls_back_instead_of_500ing(self):
        os.environ["NC_LINKEDIN_EVERY_HOURS"] = "not a number"
        self.assertEqual(self.status()["api"]["every_hours"], 24)

    def test_the_interval_is_clamped_the_same_way_api_import_clamps_it(self):
        os.environ["NC_LINKEDIN_EVERY_HOURS"] = "0"
        self.assertEqual(self.status()["api"]["every_hours"], 1)
        os.environ["NC_LINKEDIN_EVERY_HOURS"] = "99999"
        self.assertEqual(self.status()["api"]["every_hours"], 720)


# ════════════════════════════════════════════════════════════════════════════
class TestAbsentIsNotTheSameAsOff(LinkedInBase):
    """The two-machine trap. When the scraper runs on a second Mac, its
    LaunchAgent runs there, so `scrape_state.json` lives in that Mac's data/ and
    this Mac has no copy at all. Reading "file not found" as "no scraping
    configured" would report a working scraper as switched off."""

    def test_a_missing_state_file_is_reported_as_missing_not_as_zero(self):
        st = self.status()["scraper"]["state"]
        self.assertFalse(st["present"])
        # It must not silently invent the fields a present file would have.
        self.assertNotIn("target", st)
        self.assertNotIn("in_cooldown", st)

    def test_a_present_state_file_is_parsed(self):
        path = os.path.join(self.tmp, "scrape_state.json")
        future = (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"day": "2000-01-01", "target": 10, "next_ok": "",
                       "cooldown_until": future, "last_session": ""}, fh)
        with mock.patch.object(api_linkedin.config, "data_path", lambda n: path):
            st = self.status()["scraper"]["state"]
        self.assertTrue(st["present"])
        self.assertEqual(st["target"], 10)
        self.assertTrue(st["in_cooldown"])

    def test_a_past_cooldown_is_not_a_cooldown(self):
        path = os.path.join(self.tmp, "scrape_state.json")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"cooldown_until": past}, fh)
        with mock.patch.object(api_linkedin.config, "data_path", lambda n: path):
            self.assertFalse(self.status()["scraper"]["state"]["in_cooldown"])

    def test_a_corrupt_state_file_degrades_instead_of_500ing(self):
        path = os.path.join(self.tmp, "scrape_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        with mock.patch.object(api_linkedin.config, "data_path", lambda n: path):
            self.assertFalse(self.status()["scraper"]["state"]["present"])


# ════════════════════════════════════════════════════════════════════════════
class TestTheHeadlineSaysTheMostActionableThing(LinkedInBase):
    """One word for the UI to switch on, decided server-side next to the data it
    reads — the same call api_fda makes and for the same reason."""

    def _cooldown_file(self):
        path = os.path.join(self.tmp, "scrape_state.json")
        future = (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"cooldown_until": future}, fh)
        return path

    def test_cooldown_outranks_every_other_complaint(self):
        """A LinkedIn checkpoint is the one state where the correct action is to
        do nothing. Burying it under a token nag is how somebody ends up poking
        the scraper during a block — the single most important operating rule
        for the scraper."""
        self.seed([("Queued", "https://li/1", "", "", "")])
        os.environ.pop("LINKEDIN_TOKEN", None)      # would otherwise say no_token
        with mock.patch.object(api_linkedin.config, "data_path",
                               lambda n: self._cooldown_file()):
            self.assertEqual(self.status()["state"], "cooldown")

    def test_nothing_imported_reads_as_empty_not_as_broken(self):
        self.assertEqual(self.status()["state"], "empty")

    def test_a_missing_token_is_named_once_something_exists(self):
        self.seed([("Scraped", "https://li/1", "linkedin_scrape",
                    datetime.now(timezone.utc).isoformat(), '{"a":1}')])
        self.assertEqual(self.status()["state"], "no_token")

    def test_a_queue_with_nothing_arriving_reads_as_quiet(self):
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        os.environ["LINKEDIN_TOKEN"] = "tok"
        self.seed([
            ("Scraped long ago", "https://li/1", "linkedin_scrape", old, '{"a":1}'),
            ("Still queued",     "https://li/2", "", "", ""),
        ])
        self.assertEqual(self.status()["state"], "scraper_quiet")

    def test_a_queue_that_is_actively_draining_is_not_quiet(self):
        """The distinction the threshold exists for: same queue, recent arrival."""
        now = datetime.now(timezone.utc).isoformat()
        os.environ["LINKEDIN_TOKEN"] = "tok"
        self.seed([
            ("Scraped just now", "https://li/1", "linkedin_scrape", now, '{"a":1}'),
            ("Still queued",     "https://li/2", "", "", ""),
        ])
        self.assertNotEqual(self.status()["state"], "scraper_quiet")


# ════════════════════════════════════════════════════════════════════════════
class TestADeadTokenOutranksEveryReassurance(LinkedInBase):
    """NC-068: the regression gate for a dead LinkedIn token.

    `token_present` is a claim about `.env` and not about LinkedIn. An expired
    token is still a non-empty string, so token_present must not be read as proof
    the token works: the panel must not report **state "ok" — "LinkedIn is up to
    date"** for a token LinkedIn no longer accepts, even while autosync keeps
    attempting on schedule, every attempt fails, and the attempt stamp stays
    reassuringly fresh.

    This class exists so that deleting the `token_dead` branch from `_overall`
    turns the suite red.
    """

    def _overall(self, token_state, present=True, msg_age_days=1.0):
        return api_linkedin._overall(
            {"contacts_from_linkedin": 500},
            {"token_present": present, "token": {"state": token_state},
             "newest_message_age_days": msg_age_days},
            {"scraped": 300, "queued": 0, "extras_pending": 0,
             "last_fact_age_hours": 2.0, "quiet_hours": 48,
             "state": {"present": False}})

    def test_a_token_linkedin_has_expired_is_not_up_to_date(self):
        self.assertEqual(self._overall("dead"), "token_dead")

    def test_it_outranks_every_state_that_would_read_as_fine(self):
        """Above scraper_quiet and extras_pending too: those are "you could do
        more", and this is "the messages have stopped arriving"."""
        got = api_linkedin._overall(
            {"contacts_from_linkedin": 500},
            {"token_present": True, "token": {"state": "dead"},
             "newest_message_age_days": 1.0},
            # a queue, a quiet scraper AND pending extras — all louder-looking
            {"scraped": 300, "queued": 200, "extras_pending": 40,
             "last_fact_age_hours": 900.0, "quiet_hours": 48,
             "state": {"present": False}})
        self.assertEqual(got, "token_dead")

    def test_but_a_linkedin_cooldown_still_outranks_it(self):
        """The one thing above it. A block is the state where doing nothing is
        correct, and a token nag on top of it is how somebody pokes the scraper
        mid-block."""
        got = api_linkedin._overall(
            {"contacts_from_linkedin": 500},
            {"token_present": True, "token": {"state": "dead"},
             "newest_message_age_days": 1.0},
            {"scraped": 300, "queued": 0, "extras_pending": 0,
             "last_fact_age_hours": 2.0, "quiet_hours": 48,
             "state": {"present": True, "in_cooldown": True}})
        self.assertEqual(got, "cooldown")

    def test_a_token_running_out_is_said_before_it_runs_out(self):
        self.assertEqual(self._overall("expiring"), "token_expiring")

    def test_a_healthy_token_says_nothing(self):
        self.assertEqual(self._overall("ok"), "ok")


class TestTheZeroConfigurationSafetyNet(LinkedInBase):
    """NC-068's second half. Introspection needs the app's client ID and secret,
    and the user may never add them — so the dead-token case has to be catchable
    without them.

    It is, from evidence already on hand: a token LinkedIn has stopped accepting
    shows up as messages that stopped arriving. Less precise and later than
    introspection, but free."""

    def _overall(self, token_state, msg_age_days):
        return api_linkedin._overall(
            {"contacts_from_linkedin": 500},
            {"token_present": True, "token": {"state": token_state},
             "newest_message_age_days": msg_age_days},
            {"scraped": 300, "queued": 0, "extras_pending": 0,
             "last_fact_age_hours": 2.0, "quiet_hours": 48,
             "state": {"present": False}})

    def test_a_long_silence_with_no_way_to_check_is_reported(self):
        for state in ("unknown", "unchecked", "stale"):
            with self.subTest(token_state=state):
                self.assertEqual(self._overall(state, 60.0), "api_silent")

    def test_a_recent_message_proves_the_token_works_so_nothing_is_said(self):
        """The point of using this signal: a message arriving IS proof the token
        is alive, so no warning is warranted and none is given."""
        for state in ("unknown", "unchecked", "stale"):
            with self.subTest(token_state=state):
                self.assertEqual(self._overall(state, 2.0), "ok")

    def test_never_having_had_a_linkedin_message_is_not_silence(self):
        """`None` means the lane has never produced anything — a fresh install,
        or somebody with no LinkedIn DMs. Treating that as a broken token would
        put a warning on every new install."""
        self.assertEqual(self._overall("unchecked", None), "ok")

    def test_introspection_wins_when_it_is_available(self):
        """A real answer beats an inference. With introspection saying the token
        is fine, a long silence is just a quiet inbox — and it must not be
        reported as a token problem."""
        self.assertEqual(self._overall("ok", 90.0), "ok")

    def test_the_threshold_is_generous_enough_not_to_cry_wolf(self):
        """Three weeks. A fortnight without a LinkedIn DM is an ordinary
        fortnight, and a monitor that fires on it gets ignored."""
        self.assertGreaterEqual(api_linkedin.API_SILENT_DAYS, 14)


class TestTheTokenCheckStaysOffThePageRenderPath(LinkedInBase):
    """api_linkedin now imports linkedin_token, which can make a network call.
    The no-network guarantee has to survive that, so this pins WHICH functions it
    is allowed to reach: the ones that read the recorded answer, never the one
    that goes and asks."""

    def test_it_reads_the_recorded_answer_and_never_introspects(self):
        code = code_only()
        self.assertIn("linkedin_token.summary()", code)
        for banned in ("introspect(", "check_and_record("):
            self.assertNotIn(banned, code,
                             f"api_linkedin calls {banned!r} — that is a LinkedIn "
                             f"round trip on every render of the Data page")

    def test_the_real_status_call_makes_no_request_even_with_credentials_set(self):
        """The behavioural form, not the grep. With credentials present, a status
        call must still touch nothing."""
        os.environ["LINKEDIN_CLIENT_ID"] = "id"
        os.environ["LINKEDIN_CLIENT_SECRET"] = "secret"
        os.environ["LINKEDIN_TOKEN"] = "tok"

        def boom(*a, **k):
            raise AssertionError("the status endpoint called LinkedIn")

        import requests
        with mock.patch.object(requests, "post", boom), \
             mock.patch.object(requests, "get", boom):
            out = self.status()
        self.assertIn("token", out["api"])

    def test_the_token_block_is_always_the_same_shape(self):
        """Every consumer reads api.token.state. A status-dependent shape is a
        shape every caller has to guard, and the panel would render `undefined`
        for the one state that matters most — nobody has checked yet."""
        keys = set(self.status()["api"]["token"])
        for required in ("state", "days_left", "warn_days", "renewable",
                         "configured", "outcome", "checked_at"):
            self.assertIn(required, keys)

    def test_the_payload_still_carries_no_credential(self):
        os.environ["LINKEDIN_CLIENT_SECRET"] = "WPL_AP1.super-secret-value.xyz"
        os.environ["LINKEDIN_TOKEN"] = "AQV-the-actual-token-0123456789"
        blob = json.dumps(self.status())
        self.assertNotIn("WPL_AP1.super-secret-value.xyz", blob)
        self.assertNotIn("AQV-the-actual-token-0123456789", blob)


class TestItCannotOfferTheDangerousActions(unittest.TestCase):
    """Ban risk, written down in the file.

    The dispatcher's pacing IS the ban protection: a daily cap, a randomised
    inter-session gap, and a 24–48h cooldown after a checkpoint. A button that
    runs a scrape on demand defeats all three at once, and the account it would
    risk is the one the entire lane depends on. `enrich_li_extras --force` is the
    money equivalent: it re-bills every matched contact through the AI model,
    ignoring the already-populated skip."""

    def test_the_module_declares_only_a_read_only_route(self):
        self.assertEqual(list(api_linkedin.ROUTES), ["GET"],
                         "a POST here would be an action, and every action in "
                         "this lane is either a ban risk or billed spend")
        self.assertEqual(list(api_linkedin.ROUTES["GET"]), ["/api/linkedin/status"])

    def test_it_names_none_of_the_scripts_that_scrape_or_spend(self):
        code = code_only()
        for script in ("scrape_dispatch", "scrape_linkedin_profiles",
                       "enrich_li_extras", "merge_scraped_facts",
                       "import_scraped_photos"):
            self.assertNotIn(script + ".py", code)
            self.assertNotIn("import " + script, code)

    def test_it_never_writes_the_cooldown(self):
        code = code_only()
        self.assertNotIn("cooldown_until\"] =", code)
        self.assertNotIn("SCRAPE_COOLDOWN", code)
        # It reads the key, which is the point — it just must not set it.
        self.assertIn("cooldown_until", source())


# ════════════════════════════════════════════════════════════════════════════
class TestItIsWiredUpAtAll(unittest.TestCase):
    """NC-016's lesson, as a gate: a module with a ROUTES table and no entry
    in ROUTE_MODULES is finished, tested code that is unreachable behind URLs
    nobody registered, and every test above would still pass."""

    def test_the_module_is_in_route_modules(self):
        self.assertIn(api_linkedin, app.ROUTE_MODULES,
                      "api_linkedin declares ROUTES but app.py never registers it, "
                      "so /api/linkedin/status 404s")

    def test_the_handler_named_in_routes_exists_and_takes_a_conn(self):
        name = api_linkedin.ROUTES["GET"]["/api/linkedin/status"]
        self.assertTrue(hasattr(api_linkedin, name),
                        f"ROUTES names {name!r} and it does not exist")
        fn = getattr(api_linkedin, name)
        self.assertTrue(callable(fn))
        params = list(inspect.signature(fn).parameters)
        self.assertEqual(params[:1], ["conn"],
                         "the route registry hands a db connection to a handler "
                         "whose FIRST parameter is named conn — this one needs it")

    def test_the_endpoint_answers_through_the_real_registry(self):
        """Not the function — the URL, resolved the way a browser resolves it."""
        found = None
        for mod in app.ROUTE_MODULES:
            table = getattr(mod, "ROUTES", {}) or {}
            if "/api/linkedin/status" in (table.get("GET") or {}):
                found = mod
        self.assertIs(found, api_linkedin)


# ════════════════════════════════════════════════════════════════════════════
class TestTheDuplicatedConstantCannotDrift(unittest.TestCase):
    """NC-095 removed the duplication rather than checking it.

    RESCRAPE_DAYS used to be `int(os.environ.get("NC_LI_RESCRAPE_DAYS", "180"))`
    here while the scraper carried a bare `180`, and this class parsed the
    scraper's source to prove the two literals agreed. They did — but only at the
    default. Setting the variable made the panel predict a queue computed at the
    new window while the scraper kept working one computed at 180, and the
    literal-comparison test still passed, because both literals were still 180.

    Both sides now call db.li_rescrape_days(), so these assert that the copy is
    gone rather than that two copies match. Importing the scraper module for real
    is still not an option: it pulls in Selenium, which is deliberately not
    installed in the app's own environment.
    """

    def _scraper_source(self):
        with open(os.path.join(HERE, "crm", "scrape_linkedin_profiles.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_the_panel_reads_the_shared_window(self):
        self.assertEqual(api_linkedin.RESCRAPE_DAYS, db.li_rescrape_days())

    def test_the_scraper_reads_the_shared_window_too(self):
        self.assertTrue(
            re.search(r"^RESCRAPE_AFTER_DAYS\s*=\s*db\.li_rescrape_days\(\)",
                      self._scraper_source(), re.M),
            "the scraper has its own rescrape window again, so the panel's "
            "queue count no longer predicts what it will actually pick up")

    def test_neither_side_still_holds_a_bare_number(self):
        """The specific regression: a literal creeping back into either file."""
        self.assertIsNone(
            re.search(r"^RESCRAPE_AFTER_DAYS\s*=\s*\d", self._scraper_source(), re.M),
            "a bare number is back in the scraper's rescrape window")
        with open(os.path.join(HERE, "crm", "api_linkedin.py"),
                  encoding="utf-8") as fh:
            self.assertIsNone(
                re.search(r"^RESCRAPE_DAYS\s*=\s*(?:\d|int\()", fh.read(), re.M),
                "a bare number is back in the panel's rescrape window")

    def test_setting_the_variable_moves_both_sides_together(self):
        """The case the old literal comparison could not see. This is why the
        window is a function call rather than a module constant: a value the
        Settings view can write has to take effect without a restart."""
        with mock.patch.dict(os.environ, {"NC_LI_RESCRAPE_DAYS": "45"}):
            self.assertEqual(db.li_rescrape_days(), 45)
        with mock.patch.dict(os.environ, {"NC_LI_RESCRAPE_DAYS": "nonsense"}):
            self.assertEqual(db.li_rescrape_days(), 180)
        with mock.patch.dict(os.environ, {"NC_LI_RESCRAPE_DAYS": "0"}):
            self.assertEqual(db.li_rescrape_days(), 1,
                             "a zero-day window would re-scrape everyone every "
                             "night; clamp it rather than honour it")

    def test_the_source_token_is_one_literal_not_two(self):
        self.assertEqual(api_linkedin.FACTS_SCRAPE, db.LI_FACTS_SOURCE)
        self.assertTrue(
            re.search(r"^FACTS_SOURCE\s*=\s*db\.LI_FACTS_SOURCE",
                      self._scraper_source(), re.M),
            "the scraper writes its own 'linkedin_scrape' literal again")


# ════════════════════════════════════════════════════════════════════════════
class TestThePanelAndTheScraperAgreeOnTheQueue(Base):
    """NC-095. "N queued" on the Data page is the one number in this app that is
    a PREDICTION ABOUT ANOTHER PROGRAM'S BEHAVIOUR — the scraper may run on a
    second Mac. So a mismatch is the least recoverable kind: no single screen
    shows both figures, because the scraper prints its own count where nobody
    looks.

    The two clauses had drifted three ways. These prove each one is gone, using
    the clause the panel counts and the clause the scraper selects — never a
    hand-written copy of either, or the test would just be a fourth definition.
    """

    def _both(self):
        """(panel count, scraper count) over the same connection.

        The scraper's own pick_candidates cannot be imported here: that module
        pulls in Selenium, which is deliberately not installed in the app's own
        environment. So this reads the clause the scraper now uses — the point of
        the fix being that there is only one — and the source-level tests in
        test_api_linkedin.py prove the scraper really calls it.
        """
        cutoff = db.li_scrape_cutoff()
        where, params = db.li_queue_where(cutoff)
        scraper = len(self.conn.execute(
            f"SELECT id FROM contacts WHERE {where}", params).fetchall())
        panel = api_linkedin.handle_linkedin_status(self.conn)["scraper"]["queued"]
        return panel, scraper

    def _person(self, name, **cols):
        cid = db.create_contact(self.conn, name)
        cols.setdefault("linkedin_url", f"https://linkedin.com/in/{cid}")
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(f"UPDATE contacts SET {sets} WHERE id = ?",
                          (*cols.values(), cid))
        self.conn.commit()
        return cid

    def test_a_soft_deleted_person_with_a_linkedin_address_is_counted_the_same(self):
        """The bug. The panel filtered `deleted = 0`; the scraper did not, so the
        panel under-promised the moment such a person existed. Normally nobody
        is in this state, which is the only reason both sides agreed — this test
        creates the case that rarely occurs.
        """
        self._person("Ada Lovelace")
        self._person("Deleted Person", deleted=1)
        panel, scraper = self._both()
        self.assertEqual(panel, scraper)
        self.assertEqual(panel, 1, "the deleted person must be in neither count")

    def test_deleted_wins_rather_than_the_scraper_winning(self):
        """Which way the tie was broken, as an assertion rather than a comment.
        Scraping someone the user deleted spends a night of scraping writing
        facts onto a record that should not exist."""
        self._person("Deleted Person", deleted=1)
        panel, scraper = self._both()
        self.assertEqual((panel, scraper), (0, 0))

    def test_a_null_column_does_not_make_the_two_sides_disagree(self):
        """The second drift. Every column here
        carries a DEFAULT but none is NOT NULL, so a direct INSERT can write
        NULL — and the scraper's old `facts_source != 'manual'` was NULL, not
        true, for such a row. It would have skipped that person forever while
        the panel kept counting them."""
        cid = db.create_contact(self.conn, "Null Facts")
        self.conn.execute(
            "UPDATE contacts SET linkedin_url = ?, facts_source = NULL, "
            "facts_updated_at = NULL WHERE id = ?",
            ("https://linkedin.com/in/null", cid))
        self.conn.commit()
        panel, scraper = self._both()
        self.assertEqual(panel, scraper)
        self.assertEqual(panel, 1, "a NULL facts_source is not a manual edit")

    def test_a_manual_edit_is_left_alone_by_both(self):
        """The 'never clobber manual' convention, still intact after the merge."""
        self._person("Hand Edited", facts_source="manual")
        self.assertEqual(self._both(), (0, 0))

    def test_a_fresh_scrape_is_left_alone_and_a_stale_one_is_picked_up(self):
        """The rescrape window itself, from both directions, so the clause is
        exercised rather than merely present."""
        fresh = db.li_scrape_cutoff()          # exactly at the cutoff: not stale
        stale = "2000-01-01T00:00:00+00:00"
        self._person("Fresh", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at=fresh)
        self.assertEqual(self._both(), (0, 0))
        self._person("Stale", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at=stale)
        panel, scraper = self._both()
        self.assertEqual(panel, scraper)
        self.assertEqual(panel, 1)

    def test_an_excluded_person_is_out_of_both(self):
        self._person("Spam Recruiter", excluded=1)
        self.assertEqual(self._both(), (0, 0))

    def test_the_window_is_read_at_call_time_by_both_sides(self):
        """A person scraped 90 days ago is fresh at the 180-day default and
        stale at 45 — and both sides must flip together, without a restart."""
        import datetime as dt
        ninety = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=90)).isoformat(timespec="seconds")
        self._person("Scraped In June", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at=ninety)
        self.assertEqual(self._both(), (0, 0))
        with mock.patch.dict(os.environ, {"NC_LI_RESCRAPE_DAYS": "45"}):
            panel, scraper = self._both()
        self.assertEqual((panel, scraper), (1, 1),
                         "narrowing the window must move the panel's promise "
                         "and the scraper's queue together")


# ════════════════════════════════════════════════════════════════════════════
class TestTheCoverageBarCannotOverflow(Base):
    """NC-097. The bar stacked three independently-filtered counts with a zero
    floor and no ceiling. They summed to the total only through FOUR separate
    coincidences, each of which stops holding as soon as one case appears:

      * contacts due for a re-scrape — a contact scraped over 180 days ago is
        in `scraped` AND in `queued`, so the two double-count. Nothing is that
        old until a scrape has been running for about half a year.
      * contacts with a LinkedIn address AND excluded — such a person drops out
        of `with_url`, landing in `no_url`, while still counting in `scraped`,
        which has no `excluded` filter.
      * contacts with an address and `facts_source = 'manual'` — in neither
        `scraped` nor `queued`, so the bar UNDER-fills.
      * soft-deleted contacts with an address (see NC-095).

    Each test below creates one of those, rather than waiting for it to happen.
    The assertion is always the IDENTITY — the buckets sum to the total — never a
    particular number, because the identity is the thing that has to hold.
    """

    BUCKETS = ("enriched", "due", "queued", "manual", "no_url")

    def _cov(self):
        return api_linkedin.handle_linkedin_status(self.conn)["scraper"]["coverage"]

    def _assert_partitions(self, cov):
        self.assertEqual(sum(cov[k] for k in self.BUCKETS), cov["total"],
                         f"the buckets no longer partition the population: {cov}")
        self.assertEqual(cov["unexplained"], 0)
        for k in self.BUCKETS:
            self.assertGreaterEqual(cov[k], 0, f"{k} went negative")

    def _person(self, name, **cols):
        cid = db.create_contact(self.conn, name)
        cols.setdefault("linkedin_url", f"https://linkedin.com/in/{cid}")
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(f"UPDATE contacts SET {sets} WHERE id = ?",
                          (*cols.values(), cid))
        self.conn.commit()
        return cid

    def _fresh(self):
        import datetime as dt
        return (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(days=1)).isoformat(timespec="seconds")

    def test_a_contact_backdated_past_the_window_does_not_double_count(self):
        """The dated bug, tested by backdating one rather than by waiting
        half a year. Before the fix this person was in `scraped` and in `queued`,
        so the two segments together drew wider than the whole population."""
        self._person("Scraped In 2026", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at="2020-01-01T00:00:00+00:00")
        cov = self._cov()
        self._assert_partitions(cov)
        self.assertEqual(cov["due"], 1)
        self.assertEqual(cov["enriched"], 0,
                         "a contact due for a re-scrape is in ONE bucket, and "
                         "the bucket that says there is work left")

    def test_a_hidden_contact_is_outside_the_population_entirely(self):
        """NC-122. This test used to put a hidden person in `no_url`, which made
        the bar's total count the people you hid while every other count of
        people on the Data page left them out. Now a hidden person is in no
        bucket, because they are not in the total."""
        self._person("Spam Recruiter", excluded=1,
                     facts_source=db.LI_FACTS_SOURCE, facts_updated_at=self._fresh())
        cov = self._cov()
        self._assert_partitions(cov)
        self.assertEqual(cov["total"], 0)
        self.assertEqual(cov["population"], "active")

    def test_a_hand_edited_contact_is_neither_enriched_nor_queued(self):
        """Before the fix this person was in no segment at all, so the bar
        under-filled and nothing said who was missing."""
        self._person("Hand Edited", facts_source="manual")
        cov = self._cov()
        self._assert_partitions(cov)
        self.assertEqual(cov["manual"], 1)

    def test_a_soft_deleted_contact_is_outside_the_population_entirely(self):
        self._person("Deleted", deleted=1, facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at=self._fresh())
        cov = self._cov()
        self._assert_partitions(cov)
        self.assertEqual(cov["total"], 0)

    def test_all_four_cases_at_once_still_partition(self):
        """Each case alone is easy. The gate is that they compose — the old
        arithmetic held only because every one of them was zero at the same
        time."""
        self._person("Due", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at="2020-01-01T00:00:00+00:00")
        self._person("Excluded", excluded=1, facts_source=db.LI_FACTS_SOURCE)
        self._person("Manual", facts_source="manual")
        self._person("Deleted", deleted=1)
        self._person("Fresh", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at=self._fresh())
        self._person("Never Scraped")
        db.create_contact(self.conn, "No Address At All")
        self.conn.commit()

        cov = self._cov()
        self._assert_partitions(cov)
        self.assertEqual(cov["total"], 5,
                         "the deleted person and the hidden person are not shown")
        self.assertEqual(
            {k: cov[k] for k in self.BUCKETS},
            {"enriched": 1, "due": 1, "queued": 1, "manual": 1, "no_url": 1})

    def test_the_queue_prediction_is_the_two_buckets_that_still_need_work(self):
        """An identity worth publishing rather than leaving implicit: the "N
        queued" the scraper will work through is exactly `queued` + `due`. If
        this ever fails, the bar and the queue count have drifted the way the
        panel and the scraper had in NC-095."""
        self._person("Due", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at="2020-01-01T00:00:00+00:00")
        self._person("Never Scraped")
        self._person("Fresh", facts_source=db.LI_FACTS_SOURCE,
                     facts_updated_at=self._fresh())
        sc = api_linkedin.handle_linkedin_status(self.conn)["scraper"]
        self.assertEqual(sc["queued"], sc["coverage"]["queued"] + sc["coverage"]["due"])

    def test_a_missing_column_degrades_to_zeroes_rather_than_a_wrong_bar(self):
        """Same contract as every other figure on this panel: an older database
        must degrade the panel, not 500 the page that would have explained what
        is wrong. `unexplained` then goes non-zero, and the panel says so."""
        real = self.conn.execute

        def sabotage(sql, *a, **k):
            if "facts_updated_at" in sql and "CASE" in sql:
                raise sqlite3.OperationalError("no such column: facts_updated_at")
            return real(sql, *a, **k)

        db.create_contact(self.conn, "Someone")
        self.conn.commit()
        with mock.patch.object(self.conn, "execute", sabotage):
            cov = self._cov()
        self.assertEqual([cov[k] for k in self.BUCKETS], [0] * 5)
        self.assertEqual(cov["unexplained"], cov["total"],
                         "everyone is unaccounted for, and the panel must say "
                         "so rather than draw an empty bar as if it were true")


# ════════════════════════════════════════════════════════════════════════════
class TestItDegradesInsteadOfCrashing(LinkedInBase):
    """A status endpoint's job on a broken install is to describe the breakage,
    not to become part of it."""

    def test_a_missing_archive_reports_zero_messages(self):
        import archive as arc
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", "/nonexistent/archive.db"):
            api = self.status()["api"]
        self.assertEqual(api["messages"], 0)
        self.assertEqual(api["people_with_messages"], 0)
        self.assertEqual(api["newest_message"], "")

    def test_a_missing_column_degrades_that_field_only(self):
        """An older crm.db predating li_extra must still render the panel."""
        real = self.conn.execute

        def sabotage(sql, *a, **k):
            if "li_extra" in sql:
                raise sqlite3.OperationalError("no such column: li_extra")
            return real(sql, *a, **k)

        with mock.patch.object(self.conn, "execute", sabotage):
            out = self.status()
        self.assertEqual(out["scraper"]["extras"], 0)
        self.assertIn("state", out)

    def test_the_payload_is_json_serialisable(self):
        """It goes out over HTTP. A datetime or a sqlite3.Row in here is a 500
        that only happens in the browser."""
        json.dumps(self.status())


if __name__ == "__main__":
    unittest.main()
