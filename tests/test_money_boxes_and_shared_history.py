"""NC-364, NC-371 and NC-372: three things that could only be set or run by hand.

NC-364  The spend cap (ENRICH_LLM_MAX) and the two prices the cost under an Ask
        answer is worked out from had no box: changing them meant editing the
        hidden settings file. They are boxes on the Settings page's AI row now,
        each with its default, its bounds and a plain line, and a saved value is
        the value the next run uses.
NC-371  LinkedIn's client ID and secret, which the token's expiry check needs,
        were the one registered secret with no box; the row told people to edit
        the settings file. Both are boxes on the LinkedIn messages row now.
NC-372  Reading the saved LinkedIn profiles for shared history and mutual
        connections ran only from a terminal, and the row said "no button for it
        yet". It is a button on the LinkedIn profile facts row, run through the
        ordinary import runner, capped by the same spend cap, with a line saying
        what one press can cost, and refused with a sentence when no key is set.

Every setting here is written into a temporary secrets folder; nothing touches
the real one, and no test starts a server or reaches Anthropic.
"""

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db, js_code_of  # noqa: E402
from tests.test_data_settings_fixes import li_status  # noqa: E402
from tests.test_the_data_ledger_is_one_answer import _Ledger  # noqa: E402

import api_abilities  # noqa: E402
import api_import  # noqa: E402
import api_intake  # noqa: E402
import api_ledger  # noqa: E402
import api_linkedin  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import enrich_facts  # noqa: E402
import enrich_li_extras  # noqa: E402
import importers  # noqa: E402
import settings  # noqa: E402
from api_core import ApiError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONEY = ("ENRICH_LLM_MAX", "NC_PRICE_IN_PER_MTOK", "NC_PRICE_OUT_PER_MTOK")
FAKE_KEY = "sk-ant-made-up-key-0000"


class _Stop(Exception):
    """Raised once a script has parsed its arguments, so nothing after runs."""


def parsed_args(module, argv):
    """What a script's main() parsed, with the defaults it worked out itself."""
    seen = {}
    real = argparse.ArgumentParser.parse_args

    def grab(parser, args=None, namespace=None):
        seen["ns"] = real(parser, args, namespace)
        raise _Stop

    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(argparse.ArgumentParser, "parse_args", grab):
        try:
            module.main()
        except _Stop:
            pass
    return seen["ns"]


class _TempSecrets:
    """A temporary secrets folder, and every managed money/LinkedIn key cleared."""

    KEYS = MONEY + ("ENRICH_LIMIT", "ANTHROPIC_API_KEY", "LINKEDIN_TOKEN",
                    "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "IMAC")

    def setUp(self):
        super().setUp()
        self.secrets = tempfile.mkdtemp(prefix="crm_money_secrets_")
        p = mock.patch.object(config, "SECRETS_DIR", self.secrets)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for k in self.KEYS:
            os.environ.pop(k, None)

    def save(self, key, value):
        return app.handle_settings_post({"key": key, "value": value})


# ── NC-364 ────────────────────────────────────────────────────────────────────
class TestASavedSpendCapIsWhatTheNextRunUses(_TempSecrets, unittest.TestCase):

    def test_facts_written_by_ai_follow_the_saved_cap(self):
        self.save("ENRICH_LLM_MAX", "12")
        ns = parsed_args(enrich_facts, ["enrich_facts.py", "--llm"])
        self.assertEqual(ns.limit, 12)
        # The command the Data page's button runs passes no --limit of its own.
        self.assertNotIn("--limit", importers.REGISTRY["facts_llm"].build_cmd({}))

    def test_shared_history_follows_the_same_cap(self):
        self.save("ENRICH_LLM_MAX", "7")
        ns = parsed_args(enrich_li_extras, ["enrich_li_extras.py", "--pull"])
        self.assertEqual(ns.limit, 7)

    def test_both_pages_say_the_saved_cap(self):
        self.save("ENRICH_LLM_MAX", "12")
        self.assertEqual(settings.spend_cap(), 12)
        self.assertEqual(api_ledger.api_import_cap(), 12)
        self.assertIn("at most 12 people", api_abilities._ai()["takes"])

    def test_an_empty_box_is_the_default_and_the_old_name_still_counts(self):
        self.assertEqual(settings.spend_cap(), 20)
        os.environ["ENRICH_LIMIT"] = "9"
        self.assertEqual(settings.spend_cap(), 9)

    def test_out_of_bounds_is_refused_with_a_sentence_and_not_stored(self):
        self.save("ENRICH_LLM_MAX", "12")
        for bad in ("0", "1001", "5000", "twelve", "-3", "2.5"):
            with self.subTest(value=bad):
                with self.assertRaises(ApiError) as caught:
                    self.save("ENRICH_LLM_MAX", bad)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("whole number of people from 1 to 1,000", str(caught.exception))
                self.assertEqual(os.environ.get("ENRICH_LLM_MAX"), "12")
        self.save("ENRICH_LLM_MAX", "1000")
        self.assertEqual(settings.spend_cap(), 1000)


class TestASavedPriceIsWhatTheCostIsWorkedOutFrom(_TempSecrets, unittest.TestCase):

    def test_ask_and_intake_cost_with_the_saved_prices(self):
        self.save("NC_PRICE_IN_PER_MTOK", "3")
        self.save("NC_PRICE_OUT_PER_MTOK", "15")
        self.assertEqual((chat.price_in(), chat.price_out()), (3.0, 15.0))
        self.assertEqual(api_intake._price("NC_PRICE_IN_PER_MTOK", 4.0), 3.0)
        self.assertEqual(api_intake._price("NC_PRICE_OUT_PER_MTOK", 20.0), 15.0)

    def test_a_price_out_of_bounds_is_refused(self):
        for key in ("NC_PRICE_IN_PER_MTOK", "NC_PRICE_OUT_PER_MTOK"):
            for bad in ("abc", "-1", "1001"):
                with self.subTest(key=key, value=bad), self.assertRaises(ApiError) as caught:
                    self.save(key, bad)
                self.assertIn("from 0 to 1,000", str(caught.exception))
                self.assertNotIn(key, os.environ)


class TestTheAIRowHasTheThreeBoxes(_TempSecrets, unittest.TestCase):

    def acts(self):
        return {a.get("key"): a for a in api_abilities._ai()["detail"]["acts"]}

    def test_with_a_key_each_box_has_its_default_bounds_and_a_line(self):
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        acts = self.acts()
        for key in MONEY:
            with self.subTest(key=key):
                box = acts[key]
                self.assertEqual(box["kind"], "text")
                self.assertEqual(box["placeholder"], settings.MANAGED_KEYS[key]["default"])
                self.assertTrue(box["name"])
                self.assertGreater(len(box["label"].split()), 6, "a plain line, not a name")
        self.assertIn("Most people one paid run may send to Anthropic, from 1 to 1,000",
                      acts["ENRICH_LLM_MAX"]["label"])
        for key in MONEY[1:]:
            self.assertIn("per million tokens", acts[key]["label"])
            self.assertIn("in US dollars, from 0 to 1,000", acts[key]["label"])
        help_text = api_abilities._ai()["detail"]["help"]
        self.assertIn("Facts written by AI", help_text)
        self.assertIn("Read shared history", help_text)
        self.assertIn("change nothing Anthropic bills", help_text)
        # The order a person reads: the key, the model, then what it may spend.
        kinds = [a["kind"] for a in api_abilities._ai()["detail"]["acts"]]
        self.assertEqual(kinds, ["secret", "model", "text", "text", "text"])

    def test_a_saved_value_shows_in_its_box(self):
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.save("ENRICH_LLM_MAX", "12")
        self.assertEqual(self.acts()["ENRICH_LLM_MAX"]["value"], "12")

    def test_without_a_key_they_wait_with_the_model(self):
        self.assertFalse(set(MONEY) & set(self.acts()))

    def test_the_header_no_longer_says_no_box(self):
        with open(os.path.join(REPO, "crm", "settings.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("no box yet", src)
        self.assertNotIn("Users must never hand-edit", src)


# ── NC-371 ────────────────────────────────────────────────────────────────────
class TestLinkedInsClientIdAndSecretHaveBoxes(_Ledger, Base):

    SECRET = "WPL_AP1.made-up-secret.Zq9X"

    def messages_row(self, status=None, **env):
        with mock.patch.object(api_linkedin, "handle_linkedin_status",
                               lambda conn: status or li_status()), \
                mock.patch.dict(os.environ, env):
            return self.row(self.ledger(), "linkedin_messages")

    def test_the_row_no_longer_sends_anyone_to_the_settings_file(self):
        r = self.messages_row()
        self.assertNotIn("secrets/.env", r["detail"]["help"])
        self.assertNotIn("settings file", r["detail"]["help"])
        self.assertIn("client ID and client secret", r["detail"]["help"])

    def test_with_a_token_both_boxes_are_on_the_row_and_the_secret_is_masked(self):
        r = self.messages_row(LINKEDIN_TOKEN="AQV" + "x" * 40, LINKEDIN_CLIENT_ID="77abc",
                              LINKEDIN_CLIENT_SECRET=self.SECRET)
        acts = {a.get("key"): a for a in r["detail"]["acts"]}
        self.assertEqual(acts["LINKEDIN_CLIENT_ID"]["kind"], "text")
        self.assertEqual(acts["LINKEDIN_CLIENT_ID"]["value"], "77abc")
        sec = acts["LINKEDIN_CLIENT_SECRET"]
        self.assertEqual(sec["kind"], "secret")
        self.assertTrue(sec["set"])
        self.assertEqual(sec["masked"], settings.mask(self.SECRET))
        self.assertNotIn("made-up-secret", json.dumps(r))

    def test_without_a_token_there_is_nothing_to_check_so_no_boxes(self):
        keys = {a.get("key") for a in self.messages_row()["detail"]["acts"]}
        self.assertFalse({"LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"} & keys)

    def test_a_token_with_no_credentials_says_why_there_is_no_expiry_date(self):
        status = li_status(api={"token": {"state": "unchecked", "configured": False}})
        now = " ".join(self.messages_row(status, LINKEDIN_TOKEN="AQV" + "x" * 40)["detail"]["now"])
        self.assertIn("The day the token runs out is not known yet", now)
        self.assertIn("within 12 hours", now)

    def test_both_keys_are_ones_the_settings_save_accepts(self):
        for key in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"):
            self.assertIn(key, settings.MANAGED_KEYS)
        self.assertTrue(settings.MANAGED_KEYS["LINKEDIN_CLIENT_SECRET"]["secret"])


# ── NC-372 ────────────────────────────────────────────────────────────────────
class TestReadSharedHistoryIsAButtonWithItsCost(_Ledger, Base):

    def facts_row(self, pending=3, cooldown=False, **env):
        scraper = {"extras_pending": pending, "state": {"in_cooldown": cooldown}}
        with mock.patch.object(api_linkedin, "handle_linkedin_status",
                               lambda conn: li_status(scraper=scraper)), \
                mock.patch.dict(os.environ, env):
            return self.row(self.ledger(), "linkedin_facts")

    def extras(self, r):
        return [a for a in r["detail"]["acts"] if a.get("form")]

    def test_with_a_key_and_the_other_mac_it_is_a_button_before_the_switch(self):
        r = self.facts_row(IMAC="ada@other-mac", ANTHROPIC_API_KEY=FAKE_KEY)
        (act,) = self.extras(r)
        self.assertEqual((act["kind"], act["source"], act["verb"], act["preview"]),
                         ("import", "linkedin_facts", "Read shared history", False))
        self.assertEqual(act["form"], {"extras": True})
        kinds = [a["kind"] for a in r["detail"]["acts"]]
        self.assertEqual(kinds[-1], "switch")
        self.assertEqual(r["detail"]["acts"][-2], act)

    def test_the_row_says_what_a_press_costs_and_no_longer_says_no_button(self):
        with mock.patch.dict(os.environ, {"ENRICH_LLM_MAX": "20"}):
            r = self.facts_row(pending=50, IMAC="ada@other-mac", ANTHROPIC_API_KEY=FAKE_KEY)
        now = " ".join(r["detail"]["now"])
        self.assertNotIn("no button", now)
        self.assertIn("50 profiles the other Mac saved have not yet been read", now)
        self.assertIn("Each press of Read shared history sends up to 20 profiles to Anthropic", now)
        per = enrich_li_extras.most_per_profile_usd()
        self.assertIn(api_ledger._usd(per * 20), now)
        self.assertIn(f"about {api_ledger._usd(per)} a profile", now)

    def test_fewer_waiting_than_the_cap_says_the_fewer(self):
        r = self.facts_row(pending=3, IMAC="ada@other-mac", ANTHROPIC_API_KEY=FAKE_KEY)
        self.assertIn("sends up to 3 profiles", " ".join(r["detail"]["now"]))

    def test_without_a_key_it_points_at_settings_instead(self):
        r = self.facts_row(IMAC="ada@other-mac")
        self.assertEqual(self.extras(r), [])
        self.assertIn({"kind": "settings", "row": "ai", "label": "Add the key in Settings"},
                      r["detail"]["acts"])
        self.assertIn("needs a key from Anthropic", " ".join(r["detail"]["now"]))

    def test_no_button_without_the_other_mac_nothing_waiting_or_in_a_pause(self):
        for kw in ({"pending": 3}, {"pending": 0, "IMAC": "ada@other-mac"},
                   {"pending": 3, "cooldown": True, "IMAC": "ada@other-mac"}):
            with self.subTest(**kw):
                self.assertEqual(self.extras(self.facts_row(ANTHROPIC_API_KEY=FAKE_KEY, **kw)), [])


class TestTheButtonRunsThroughTheImportRunner(_TempSecrets, Base):
    """The route handler, called directly: POST /api/import/run with the act's form."""

    def run_it(self, body):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=(
                "Enriched 2 contacts with shared history and mutual connections, from "
                "3 saved LinkedIn profiles.\n"), stderr="")
        # The run's bookkeeping writes to this test's own database.
        with mock.patch.object(api_import.subprocess, "run", fake_run), \
                mock.patch.object(db, "DB_PATH", self.path):
            out = app.handle_import_run(body)
        return out, calls

    def test_no_key_is_refused_with_a_sentence_and_nothing_runs(self):
        ran = []
        with mock.patch.object(api_import.subprocess, "run", lambda *a, **k: ran.append(a)), \
                self.assertRaises(ApiError) as caught:
            app.handle_import_run({"source": "linkedin_facts", "extras": True})
        self.assertEqual(ran, [])
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("needs a key from Anthropic", str(caught.exception))
        self.assertIn("Settings", str(caught.exception))

    def test_with_a_key_it_reads_the_saved_profiles_and_never_rebills(self):
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        out, calls = self.run_it({"source": "linkedin_facts", "extras": True})
        (cmd,) = calls
        self.assertEqual(cmd[1:], ["enrich_li_extras.py", "--pull"])
        self.assertNotIn("--force", cmd)
        self.assertTrue(out["ok"])
        self.assertIn({"verb": "Enriched", "count": 2}, out["receipts"])

    def test_the_plain_import_is_unchanged(self):
        _, calls = self.run_it({"source": "linkedin_facts"})
        self.assertEqual(calls[0][1:], ["merge_scraped_facts.py", "--pull"])


class TestOnePressSendsNoMoreThanTheCap(_TempSecrets, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="crm_extras_cap_")
        self.src = os.path.join(self.dir, "li_raw")
        os.makedirs(self.src)
        self.dest = os.path.join(self.dir, "crm.db")
        db.init_db(self.dest)
        conn = db.get_db(self.dest)
        self.ids = [db.create_contact(conn, n) for n in ("Ada One", "Bea Two", "Cy Three")]
        conn.commit()
        conn.close()
        for cid, name in zip(self.ids, ("Ada One", "Bea Two", "Cy Three")):
            with open(os.path.join(self.src, f"{cid}.json"), "w", encoding="utf-8") as fh:
                json.dump({"name": name, "profile_text": f"{name} builds things."}, fh)
        self.sent = []
        reply = json.dumps({"shared_history": ["You both studied at Example University"],
                            "mutual_names": ["Dee"]})

        def create(**kw):
            self.sent.append(kw)
            return mock.Mock(content=[mock.Mock(text=reply)])
        self.client = mock.Mock()
        self.client.messages.create = create

    def run_main(self):
        out = io.StringIO()
        with mock.patch.object(enrich_li_extras, "_make_client", lambda: self.client), \
                mock.patch.object(sys, "argv", ["enrich_li_extras.py", "--source-dir", self.src,
                                                "--dest", self.dest]), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            enrich_li_extras.main()
        return out.getvalue()

    def test_the_saved_cap_stops_the_run_and_says_how_many_wait(self):
        self.save("ENRICH_LLM_MAX", "2")
        text = self.run_main()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Enriched 2 contacts", text)
        self.assertIn("1 more are waiting", text)
        # The next press picks up the one left, and re-bills nobody.
        self.sent.clear()
        self.run_main()
        self.assertEqual(len(self.sent), 1)

    def test_the_cost_ceiling_is_a_fraction_of_a_cent_a_profile(self):
        per = enrich_li_extras.most_per_profile_usd()
        self.assertGreater(per, 0.002)
        self.assertLess(per, 0.01)
        self.assertEqual(api_ledger._usd(per), "half a US cent")
        self.assertEqual(api_ledger._usd(per * 20), "9 US cents")
        self.assertEqual(api_ledger._usd(0.011), "1 US cent")
        self.assertEqual(api_ledger._usd(2.4), "2.40 US dollars")

    def test_the_output_is_plain_words(self):
        self.save("ENRICH_LLM_MAX", "5")
        text = self.run_main()
        for jargon in ("LLM", "sidecar", "id/name", "master", "ANTHROPIC_API_KEY"):
            self.assertNotIn(jargon, text)


class TestTheWebPageSendsTheActsFields(unittest.TestCase):
    """The page side of NC-372: the run carries the act's own fields, and the
    three every run sends cannot be overridden by them."""

    def test_the_import_runner_spreads_the_form_first(self):
        js = js_code_of(os.path.join(REPO, "static", "app", "data.js"))
        self.assertIn("body: JSON.stringify({ ...(form || {}), source: sourceKey, path, "
                      "dry_run: !!dryRun })", js)
        self.assertIn("useImportRunner(act.source, label, onRan, act.form)", js)


if __name__ == "__main__":
    unittest.main()
