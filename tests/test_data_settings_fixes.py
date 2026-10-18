"""The Data page, Settings and first-run setup: one test class per fix.

NC-238, NC-239, NC-240, NC-314, NC-317, NC-318, NC-319, NC-320, NC-324, NC-325,
NC-326, NC-327, NC-353. Each class says what was wrong in one line. The front-end checks read the real files with their
comments blanked (tests.helpers.js_code_of), so a comment explaining a fix can
never be what satisfies its test.

Every folder NC-314's "Remove everything" deletes here is a temporary one, and
the helper's uninstaller is a stub script written into it.
"""

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, code_of, db, js_code_of  # noqa: E402,F401
from tests.test_the_data_ledger_is_one_answer import _Ledger, finding  # noqa: E402

import api_ledger  # noqa: E402
import api_linkedin  # noqa: E402
import api_provenance  # noqa: E402
import api_remove  # noqa: E402
import config  # noqa: E402
import importers  # noqa: E402
import style_profile  # noqa: E402
from api_core import ApiError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


def js(name):
    return js_code_of(os.path.join(APP, name))


def function_body(src, name):
    """One top-level function of a front-end file, up to the next one."""
    start = src.index(f"function {name}(")
    nxt = re.search(r"\n(?:export )?(?:async )?function |\nconst [A-Z_]+ = ", src[start + 1:])
    return src[start:start + 1 + nxt.start()] if nxt else src[start:]


def wizard_steps(src):
    m = re.search(r"const STEPS = \[([^\]]*)\]", src)
    return [s.strip() for s in m.group(1).split(",") if s.strip()]


def li_status(**over):
    """api_linkedin.handle_linkedin_status, in its shape, with nothing set."""
    out = {"state": "ok", "download": {"contacts_from_linkedin": 0},
           "api": {"token": {"state": "unknown"}, "messages": 0,
                   "people_with_messages": 0, "newest_message_age_days": None,
                   "last_attempt_age_hours": None},
           "scraper": {"coverage": {}, "extras_pending": 0, "queued": 0,
                       "last_fact_age_hours": None, "quiet_hours": 48, "state": {}}}
    for k, v in over.items():
        out[k] = {**out[k], **v}
    return out


# ── NC-317 ────────────────────────────────────────────────────────────────────
class TestTheProfileReaderNamesTheBanRisk(_Ledger, Base):
    """The LinkedIn profile row said the slow reader keeps the account "never
    flagged"; it breaks LinkedIn's User Agreement and can get it banned."""

    def test_the_row_names_the_risk_and_never_says_never_flagged(self):
        r = self.row(self.ledger(), "linkedin_facts")
        self.assertNotIn("never flagged", r["takes"])
        self.assertIn("User Agreement", r["takes"])
        self.assertIn("banned", r["takes"])
        self.assertIn("User Agreement", r["detail"]["help"])

    def test_no_screen_says_never_flagged(self):
        """Code only: the comment recording the fix may quote the old words."""
        for folder, ext, read in ((APP, ".js", js_code_of),
                                  (os.path.join(REPO, "crm"), ".py", code_of)):
            for name in os.listdir(folder):
                if name.endswith(ext):
                    self.assertNotIn("never flagged", read(os.path.join(folder, name)), name)


# ── NC-325 ────────────────────────────────────────────────────────────────────
class TestWhatsAppOnAPhoneOnlySaysWhatToDo(_Ledger, Base):
    """With no WhatsApp Desktop found, the row named the condition and not the
    next step."""

    def test_the_row_says_install_desktop_or_skip(self):
        self.found = []
        r = self.row(self.ledger(scan=True), "whatsapp")
        self.assertIn("install it and sign in with your phone, or skip this source",
                      r["takes"])
        self.assertIn("Install WhatsApp Desktop and sign in with your phone",
                      r["state"]["says"])

    def test_found_whatsapp_is_not_told_to_install_it(self):
        self.found = [finding("whatsapp", "/x/ChatStorage.sqlite")]
        r = self.row(self.ledger(scan=True), "whatsapp")
        self.assertNotIn("skip this source", r["takes"])


# ── NC-326 ────────────────────────────────────────────────────────────────────
class TestHowYouWriteNamesItsLanguages(_Ledger, Base):
    """The row said "Swedish and English kept apart"; it now names what it
    measures, from the measuring module itself."""

    def style_help(self):
        worked = self.row(self.ledger(), "worked_out")
        return worked["detail"]["help"]

    def test_the_row_names_the_languages_it_measures(self):
        text = self.style_help()
        self.assertNotIn("Swedish and English kept apart", text)
        self.assertIn("measured by language", text)
        self.assertIn("Swedish and English apart", text)
        self.assertIn("any other language", text)

    def test_the_names_come_from_the_measuring_module(self):
        self.assertEqual(style_profile.measured_languages(), ["Swedish", "English"])
        with mock.patch.object(style_profile, "LANG_VALUES", ("sv", "en", "fi", "unknown")), \
                mock.patch.dict(style_profile.LANG_WORD, {"fi": "Finnish"}):
            self.assertIn("Swedish, English and Finnish apart", self.style_help())


# ── NC-238 ────────────────────────────────────────────────────────────────────
class TestLinkedInDetailIsBackOnItsRows(_Ledger, Base):
    """The old LinkedIn panel's facts: the five-way split, profiles not read
    yet, the 60-day token, the newest message and the last attempt."""

    def ledger_with(self, status, **env):
        with mock.patch.object(api_linkedin, "handle_linkedin_status", lambda conn: status), \
                mock.patch.dict(os.environ, env):
            return self.ledger()

    def test_the_profile_row_splits_everyone_five_ways(self):
        cov = {"enriched": 3, "due": 1, "queued": 2, "manual": 1, "no_url": 4, "total": 11}
        out = self.ledger_with(li_status(scraper={"coverage": cov, "extras_pending": 2}),
                               IMAC="ada@other-mac")
        now = " ".join(self.row(out, "linkedin_facts")["detail"]["now"])
        for words in ("Of 11 people", "3 filled in", "1 due to be read again",
                      "2 waiting their turn", "1 kept as you typed them",
                      "4 with no LinkedIn profile link"):
            self.assertIn(words, now)
        self.assertIn("2 profiles the other Mac saved have not yet been read", now)

    def test_the_messages_row_says_when_the_newest_arrived_and_the_last_try(self):
        api = {"messages": 5, "people_with_messages": 2, "newest_message_age_days": 3.0,
               "last_attempt_age_hours": 5.0}
        out = self.ledger_with(li_status(api=api), LINKEDIN_TOKEN="AQV" + "x" * 40)
        r = self.row(out, "linkedin_messages")
        now = " ".join(r["detail"]["now"])
        self.assertIn("newest LinkedIn message saved is from 3 days ago", now)
        self.assertIn("last asked LinkedIn for new messages 5 hours ago", now)
        self.assertIn("60 days", r["detail"]["help"])

    def test_the_page_prints_the_extra_sentences(self):
        self.assertIn("...(d.now || [])", js("data.js"))


# ── NC-239 ────────────────────────────────────────────────────────────────────
class TestTheOldPagesSmallerSentencesAreBack(_Ledger, Base):

    def test_a_copy_that_fell_behind_offers_the_import_folder_on_its_need(self):
        self.found = [finding("imessage", "/x/chat.db", auto=True, stale_days=10)]
        (need,) = self.ledger(scan=True)["needs"]
        self.assertEqual(need["tone"], "behind")
        self.assertIn("imessage_inbox", [a.get("target") for a in need["acts"]])

    def test_a_need_offers_what_its_source_added(self):
        self.found = [finding("contacts", "/x/cards.vcf")]
        (need,) = self.ledger(scan=True)["needs"]
        self.assertEqual(need["record"], api_provenance.resolve_source("contacts"))
        self.assertTrue(need["record"])
        self.assertIn("n.record && html`<${Record}", js("data.js"))

    def test_what_a_source_added_opens_on_a_row_not_used_yet(self):
        src = js("data.js")
        self.assertIn("record=${d.provenance && html`<${Record}", src)
        self.assertNotIn("d.provenance && row.in_use", src)

    def test_a_fresh_install_is_told_contacts_before_imessage(self):
        self.found = []
        out = self.ledger(scan=True)
        self.assertEqual(out["next"]["row"], "contacts")
        self.assertIn("before iMessage", out["next"]["why"])
        self.assertIn(api_ledger.CONTACTS_FIRST, self.row(out, "imessage")["detail"]["now"])
        self.assertIn("data.next.why", js("data.js"))


# ── NC-319 ────────────────────────────────────────────────────────────────────
class TestWaitingForTheLinkedInFile(_Ledger, Base):
    """LinkedIn's file arrives a day or two after asking; setup records the ask
    and the app says it is waiting until a LinkedIn file brings someone in."""

    def test_the_routes_are_declared(self):
        self.assertEqual(api_ledger.ROUTES["GET"]["/api/data/linkedin-wait"],
                         "handle_linkedin_wait")
        self.assertEqual(api_ledger.ROUTES["POST"]["/api/data/linkedin-asked"],
                         "handle_linkedin_asked")

    def test_asking_starts_the_wait_and_a_linkedin_person_ends_it(self):
        self.assertFalse(api_ledger.handle_linkedin_wait(self.conn)["waiting"])
        out = api_ledger.handle_linkedin_asked(self.conn, {"asked": True})
        self.assertTrue(out["waiting"])
        self.assertIn("Waiting for your LinkedIn file", out["says"])
        row = self.row(self.ledger(), "linkedin_connections")
        self.assertIn("Waiting for your LinkedIn file", row["state"]["says"])
        db.create_contact(self.conn, "Ada Lovelace", source="linkedin")
        self.conn.commit()
        self.assertFalse(api_ledger.handle_linkedin_wait(self.conn)["waiting"])

    def test_it_can_be_taken_back(self):
        api_ledger.handle_linkedin_asked(self.conn, {"asked": True})
        self.assertFalse(api_ledger.handle_linkedin_asked(self.conn, {"asked": False})["waiting"])

    def test_after_a_few_days_it_says_to_look_for_the_email(self):
        db.state_set(self.conn, api_ledger.LI_ASKED_KEY, "2020-01-01T00:00:00+00:00")
        out = api_ledger.handle_linkedin_wait(self.conn)
        self.assertTrue(out["late"])
        self.assertIn("ask again", out["says"])

    def test_the_wizard_has_a_linkedin_step_that_links_to_the_request(self):
        src = js("onboarding.js")
        self.assertIn("StepLinkedIn", wizard_steps(src))
        step = function_body(src, "StepLinkedIn")
        self.assertIn("href=${LINKEDIN_DATA_PAGE}", step)
        self.assertIn('"/api/data/linkedin-asked"', step)
        self.assertIn('"https://www.linkedin.com/mypreferences/d/download-my-data"', src)
        self.assertNotIn("LinkedIn, WhatsApp, email and more wait in Data", src)

    def test_people_shows_the_waiting_line_above_the_table(self):
        """The wait was said only on the Data page's LinkedIn row, which someone
        who just finished setup has no reason to open."""
        src = js("people.js")
        line = function_body(src, "LinkedInWait")
        self.assertIn('api("/api/data/linkedin-wait")', line)
        self.assertIn("!wait.waiting) return null", line)
        self.assertIn("${wait.says}", line)
        self.assertIn('go("data?row=linkedin_connections")', line)
        self.assertIn("JSON.stringify({ asked: false })", line)
        page = src[src.index("<${SoonStrip} go=${go}"):src.index('<div class="nx-table">')]
        self.assertIn("<${LinkedInWait} go=${go} hidden=${compact} />", page)
        # People loads under setup, so it has to start afresh when setup closes,
        # or the line is missing until the next launch (seen on the scratch engine).
        self.assertIn('key=${route.name + (onboarded === false ? ":setup" : "")}', js("main.js"))


# ── NC-318 ────────────────────────────────────────────────────────────────────
class TestTheWizardAsksWhoYouAre(unittest.TestCase):

    def test_name_and_email_are_the_first_step_after_hello(self):
        src = js("onboarding.js")
        self.assertEqual(wizard_steps(src)[:2], ["StepHello", "StepYou"])
        step = function_body(src, "StepYou")
        self.assertIn('["SELF_NAME", name.trim()]', step)
        self.assertIn('["SELF_EMAIL", email.trim()]', step)
        self.assertIn('"/api/settings", { method: "POST"', step)
        # Prefilled from what is already set.
        self.assertIn('value("SELF_NAME")', step)


# ── NC-320 ────────────────────────────────────────────────────────────────────
class TestTheWizardWarnsBeforeTheFirstPrompt(unittest.TestCase):

    def test_the_step_before_the_first_search_carries_the_warning(self):
        src = js("onboarding.js")
        steps = wizard_steps(src)
        first = next(i for i, name in enumerate(steps)
                     if '"/api/import/detect"' in function_body(src, name))
        self.assertGreater(first, 0)
        self.assertIn("<${PromptWarning}", function_body(src, steps[first - 1]))
        for i in range(first):
            self.assertNotIn('"/api/import/detect"', function_body(src, steps[i]))

    def test_the_warning_names_python_and_the_folders(self):
        warning = function_body(js("onboarding.js"), "PromptWarning")
        for words in ("python3", "Desktop", "Downloads", "WhatsApp", "own engine"):
            self.assertIn(words, warning)


# ── NC-240 ────────────────────────────────────────────────────────────────────
class TestEverySaveShowsTheSettingsFileWarning(unittest.TestCase):
    """Gmail's Add and setup's key box said "added" or moved on over the
    server's warning that the settings file could not be locked down."""

    def test_every_settings_post_reads_the_warning(self):
        for name in ("gmail.js", "onboarding.js"):
            src = js(name)
            posts = [m.start() for m in re.finditer(r'api\("/api/settings", \{\s*method: "POST"', src)]
            self.assertTrue(posts, f"{name}: no settings save found")
            for at in posts:
                with self.subTest(file=name, at=at):
                    before = src[max(0, at - 30):at]
                    self.assertRegex(before, r"const \w+ = await $",
                                     "the save's answer is thrown away")
                    var = re.search(r"const (\w+) = await $", before).group(1)
                    self.assertIn(f"{var}.warning", src[at:at + 700])

    def test_gmail_add_toasts_the_warning_first(self):
        self.assertIn("toast(saved.warning ||", js("gmail.js"))


# ── NC-353 ────────────────────────────────────────────────────────────────────
class TestGmailStepFivePointsWhereTheButtonIs(unittest.TestCase):

    def test_it_says_above(self):
        steps = js("gmail.js")
        start = steps.index("const STEPS = [")
        block = steps[start:steps.index("];", start)]
        self.assertNotIn("the button below", block)
        self.assertIn("button above these steps", block)


# ── NC-324 ────────────────────────────────────────────────────────────────────
class TestTheOfferCardSaysWhatAnyProgramCanRead(unittest.TestCase):

    def test_the_offer_card_says_the_copies_are_unencrypted(self):
        src = js("fda.js")
        start = src.index('if (status.state === "not_installed")')
        offer = src[start:src.index("const path = status.binary", start)]
        self.assertIn("unencrypted", offer)
        self.assertIn("Any program you run can read them", offer)

    def test_the_native_offer_says_it_too(self):
        """Since NC-330 the native window is the only app; its offer sheet had
        the paragraph above this one and not this one."""
        native = os.path.join(REPO, "native", "Sources")
        with open(os.path.join(native, "FullDiskAccessStore.swift"), encoding="utf-8") as fh:
            words = fh.read()
        line = words[words.index("static let storedPlainly"):].split("\n", 1)[0]
        self.assertIn("unencrypted", line)
        self.assertIn("Any program you run can read them", line)
        with open(os.path.join(native, "FullDiskAccessSheet.swift"), encoding="utf-8") as fh:
            sheet = fh.read()
        offer = sheet[sheet.index("Set up automatic updates"):sheet.index("private struct FullDiskAccessOutdated")]
        self.assertIn("FullDiskAccessWords.storedPlainly", offer)


# ── NC-327 ────────────────────────────────────────────────────────────────────
class TestTheKeyBoxSaysHowToCapTheBill(unittest.TestCase):

    def test_both_key_boxes_carry_the_spending_limit_hint(self):
        ledger = js("ledger.js")
        self.assertRegex(ledger, r"export const ANTHROPIC_SPEND_HINT = ")
        self.assertIn("spending", ledger[ledger.index("ANTHROPIC_SPEND_HINT"):][:300])
        settings_js = js("settings.js")
        self.assertIn('a.key === "ANTHROPIC_API_KEY"', settings_js)
        self.assertIn("${ANTHROPIC_SPEND_HINT}", settings_js)
        self.assertIn("${ANTHROPIC_SPEND_HINT}", function_body(js("onboarding.js"), "StepAI"))

    def test_the_native_window_carries_the_same_hint_in_both_places(self):
        """Since NC-330 the native window is the only app; it had neither line."""
        native = os.path.join(REPO, "native", "Sources")

        def read(name):
            with open(os.path.join(native, name), encoding="utf-8") as fh:
                return fh.read()
        hint = re.search(r'static let anthropicSpendHint = "([^"]+)"', read("LedgerWords.swift"))
        web = re.search(r'ANTHROPIC_SPEND_HINT = ((?:"[^"]*"\s*\+?\s*)+);', js("ledger.js"))
        self.assertEqual(hint.group(1), "".join(re.findall(r'"([^"]*)"', web.group(1))))
        parts = read("SettingsParts.swift")
        secret = parts[parts.index('case "secret":'):parts.index('case "switch":')]
        self.assertIn('a.key == "ANTHROPIC_API_KEY"', secret)
        self.assertIn("LedgerWords.anthropicSpendHint", secret)
        steps = read("OnboardingSteps.swift")
        ai = steps[steps.index('OnbSectionLabel("Anthropic API key")'):][:600]
        self.assertIn("LedgerWords.anthropicSpendHint", ai)


# ── NC-314 ────────────────────────────────────────────────────────────────────
class TestRemoveEverything(unittest.TestCase):
    """Only ever against temporary folders, with the helper's uninstaller a stub."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ncrm-remove-")
        self.home = os.path.join(self.root, "Application Support", api_remove.FOLDER_NAME)
        self.logs = os.path.join(self.root, "Logs", api_remove.FOLDER_NAME)
        self.neighbour = os.path.join(self.root, "Application Support", "SomeOtherApp", "keep.txt")
        for path in ("data/crm.db", "secrets/.env", "venv/bin/python3",
                     "bin/networkcrm-imessage-sync", "imessage/chat.db"):
            self._touch(os.path.join(self.home, path))
        self._touch(os.path.join(self.logs, "server.log"))
        self._touch(self.neighbour)
        self.marker = os.path.join(self.root, "uninstaller-ran")
        self.script = self._script(0)
        self.stop = mock.MagicMock()
        patches = [
            mock.patch.object(config, "APP_SUPPORT_DIR", self.home),
            mock.patch.object(config, "DATA_DIR", os.path.join(self.home, "data")),
            mock.patch.object(config, "SECRETS_DIR", os.path.join(self.home, "secrets")),
            mock.patch.object(importers, "IMSG_HELPER_LOG", os.path.join(self.logs, "imessage-sync.log")),
            mock.patch.object(importers, "IMSG_HELPER_BIN",
                              os.path.join(self.home, "bin", "networkcrm-imessage-sync")),
            mock.patch.object(importers, "IMSG_INBOX", os.path.join(self.home, "imessage")),
            mock.patch.object(importers, "WA_INBOX", os.path.join(self.home, "whatsapp")),
            mock.patch.object(importers, "AB_INBOX", os.path.join(self.home, "addressbook")),
            mock.patch.object(importers, "CAL_INBOX", os.path.join(self.home, "calendar")),
            mock.patch.object(importers, "imsg_helper_uninstaller", lambda: self.script),
            mock.patch.object(importers, "imsg_helper_installed", lambda: True),
            mock.patch.object(api_remove, "_stop_engine_soon", self.stop),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")

    def _script(self, code):
        path = os.path.join(self.root, f"stub-uninstall-{code}.sh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/bash\necho stub > "{self.marker}"\necho "stub said {code}"\nexit {code}\n')
        return path

    def nothing_removed(self):
        self.assertTrue(os.path.exists(os.path.join(self.home, "data", "crm.db")))
        self.assertTrue(os.path.exists(os.path.join(self.logs, "server.log")))
        self.stop.assert_not_called()

    def test_it_turns_the_helper_off_then_removes_the_apps_folders(self):
        plan = api_remove.handle_remove_plan()
        self.assertTrue(plan["allowed"], plan["why_not"])
        self.assertEqual(plan["word"], api_remove.CONFIRM_WORD)
        self.assertGreaterEqual(len(plan["removes"]), 5)
        out = api_remove.handle_remove_everything({"confirm": "Remove "})
        self.assertTrue(out["ok"], out)
        self.assertTrue(os.path.exists(self.marker), "the uninstaller never ran")
        self.assertFalse(os.path.exists(self.home))
        self.assertFalse(os.path.exists(self.logs))
        self.assertTrue(os.path.exists(self.neighbour), "it reached past its own folder")
        self.assertEqual(out["left"], [])
        self.stop.assert_called_once()
        steps = " ".join(out["by_hand"])
        self.assertIn("Trash", steps)
        self.assertIn("Full Disk Access", steps)
        self.assertIn("networkcrm-imessage-sync", steps)

    def test_a_source_checkout_is_refused(self):
        checkout = os.path.join(self.root, "checkout")
        with mock.patch.object(config, "DATA_DIR", os.path.join(checkout, "data")), \
                mock.patch.object(config, "SECRETS_DIR", os.path.join(checkout, "secrets")):
            self.assertFalse(api_remove.handle_remove_plan()["allowed"])
            with self.assertRaises(ApiError) as caught:
                api_remove.handle_remove_everything({"confirm": "remove"})
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(os.path.exists(self.marker))
        self.nothing_removed()

    def test_a_checkout_whose_data_links_into_the_app_folder_is_still_refused(self):
        link = os.path.join(self.root, "checkout", "data")
        os.makedirs(os.path.dirname(link))
        os.symlink(os.path.join(self.home, "data"), link)
        with mock.patch.object(config, "DATA_DIR", link):
            with self.assertRaises(ApiError):
                api_remove.handle_remove_everything({"confirm": "remove"})
        self.nothing_removed()

    def test_a_home_not_called_networkcrm_is_refused(self):
        with mock.patch.object(config, "APP_SUPPORT_DIR", os.path.dirname(self.home)):
            with self.assertRaises(ApiError):
                api_remove.handle_remove_everything({"confirm": "remove"})
        self.nothing_removed()

    def test_it_needs_the_typed_word(self):
        for word in ("", "yes", "remove everything"):
            with self.subTest(word=word), self.assertRaises(ApiError) as caught:
                api_remove.handle_remove_everything({"confirm": word})
            self.assertEqual(caught.exception.status, 400)
        self.assertFalse(os.path.exists(self.marker))
        self.nothing_removed()

    def test_a_helper_that_will_not_turn_off_stops_everything(self):
        self.script = self._script(1)
        out = api_remove.handle_remove_everything({"confirm": "remove"})
        self.assertFalse(out["ok"])
        self.assertIn("stub said 1", out["helper"]["output"])
        self.assertIn("nothing was removed", out["says"])
        self.nothing_removed()

    def test_the_suite_itself_is_refused_without_these_patches(self):
        """The test data folder is not the app's home, so nothing happens."""
        with mock.patch.object(config, "APP_SUPPORT_DIR",
                               os.path.join(self.root, "elsewhere", api_remove.FOLDER_NAME)):
            self.assertFalse(api_remove.handle_remove_plan()["allowed"])

    def test_every_path_comes_from_the_apps_constants(self):
        code = code_of(os.path.join(REPO, "crm", "api_remove.py"))
        for literal in ("Library", "~/", "Application Support", "/Users/"):
            self.assertNotIn(literal, code)

    def test_the_routes_are_reachable(self):
        """The module and the Settings button were written but the module was left
        out of ROUTE_MODULES, so both addresses answered 404 and the button said the
        app could not tell what it would remove."""
        self.assertIn(api_remove, app.ROUTE_MODULES)
        for method, table in api_remove.ROUTES.items():
            for path, name in table.items():
                with self.subTest(route=f"{method} {path}"):
                    self.assertTrue(callable(getattr(api_remove, name, None)))

    def test_the_readme_and_the_read_me_say_how_to_remove_it(self):
        """README and the disk image's Read Me said nothing about removing the
        app, so the helper kept its Full Disk Access after the Trash."""
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        section = readme[readme.index("\n## Removing it\n"):]
        section = section[:section.index("\n## ", 5)]
        for words in ("Remove everything", "Full Disk Access", "(docs/UNINSTALL.md)"):
            self.assertIn(words, section)
        self.assertTrue(os.path.isfile(os.path.join(REPO, "docs", "UNINSTALL.md")))
        with open(os.path.join(REPO, "packaging", "make_dmg.sh"), encoding="utf-8") as fh:
            dmg = fh.read()
        read_me = dmg[dmg.index("<<'READMEEOF'"):dmg.index("\nREADMEEOF\n")]
        self.assertIn("REMOVING IT", read_me)
        self.assertIn("Remove everything", read_me)

    def test_settings_puts_it_last_and_asks_for_the_word(self):
        src = js("settings.js")
        page = function_body(src, "SettingsPage")
        self.assertGreater(page.index("<${RemoveEverything}"), page.index("Open the Data page"))
        self.assertEqual(page[page.index("<${RemoveEverything}"):].count("<${"), 1,
                         "something sits below Remove everything")
        remove = function_body(src, "RemoveEverything")
        self.assertIn('"/api/remove/plan"', remove)
        self.assertIn('"/api/remove/everything"', remove)
        self.assertIn("confirm: word.trim()", remove)
        self.assertIn("word.trim().toLowerCase() !== plan.word", remove)
        self.assertIn("cannot be undone", remove)

    def test_the_native_settings_page_has_it_too_and_last(self):
        """NC-330 made the native window the only app, and it had no Remove
        everything, while README, docs/UNINSTALL.md and the disk image's Read Me
        all send people to it."""
        native = os.path.join(REPO, "native", "Sources")
        with open(os.path.join(native, "SettingsScreen.swift"), encoding="utf-8") as fh:
            screen = fh.read()
        install = screen[screen.index("struct SettingsInstallSection"):]
        at = install.index("SettingsRemoveEverything()")
        self.assertGreater(at, install.index('"Open the Data Page"'))
        self.assertNotRegex(install[at + len("SettingsRemoveEverything()"):], r"\b[A-Z]\w+\(",
                            "something sits below Remove everything")
        with open(os.path.join(native, "SettingsRemove.swift"), encoding="utf-8") as fh:
            remove = fh.read()
        self.assertIn('"/api/remove/plan"', remove)
        self.assertIn('"/api/remove/everything"', remove)
        self.assertIn('["confirm": .string(word.trimmingCharacters(in: .whitespaces))]', remove)
        self.assertIn(".disabled(model.busy || !model.wordMatches)", remove,
                      "the button that removes must stay off until the word is typed")
        self.assertIn("cannot be undone", remove)
        # The self-tests read the plan and never send the removal.
        tests = "".join(open(os.path.join(native, f), encoding="utf-8").read()
                        for f in os.listdir(native) if f.startswith("SelfTest"))
        self.assertIn('"/api/remove/plan"', tests)
        self.assertNotIn('"/api/remove/everything"', tests)


if __name__ == "__main__":
    unittest.main()
