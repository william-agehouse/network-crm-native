"""NC-101 gates: the per-source display cannot claim more than it knows.

WHY THIS FILE EXISTS
--------------------
This endpoint replaced a single "last sync" figure, and it replaced it because
one number cannot be honest about seventeen sources that start in four
different ways. Every gate here is tied to a way the old figure lied, or to a
way a display like it could:

* Three answers, not two. A source with nothing to configure is not
  "connected"; calling it connected is how a status page reports seventeen
  working sources on an install where four have never been set up.
* An environment variable name is not a sentence. "NC_ICS_GOOGLE_URL is not set"
  on screen is exactly what the app's wording rule forbids: plain words first,
  the identifier afterwards and only if it earns its place.
* Two stamps that disagree. facts and style are written by two paths that never
  reconcile; reading the wrong one calls a working pass stale.
* A status page must never 500. It is the page a person opens BECAUSE
  something is wrong.
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402 — CRM_DATA_DIR bootstrap

import api_import                    # noqa: E402
import api_sources                   # noqa: E402
import importers                     # noqa: E402

# An ALL-CAPS token with an underscore: what an environment variable looks like
# and what a sentence for a person never does. Four characters minimum so that
# real words in caps cannot trip it.
ENV_SHAPED = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}\b")


class TestSourcesPayload(Base):
    def setUp(self):
        super().setUp()
        self.out = api_sources.handle_sources(self.conn)
        self.rows = self.out["sources"]
        self.sum = self.out["summary"]

    def test_every_registered_source_appears_exactly_once(self):
        keys = [r["key"] for r in self.rows]
        self.assertEqual(sorted(keys), sorted(importers.REGISTRY),
                         "the display is the registry, not a list kept beside it")
        self.assertEqual(len(keys), len(set(keys)))

    def test_connected_counts_partition_the_total_by_construction(self):
        """Not arithmetic that happens to work out — three branches of one
        function returning True / False / None, so the three counts sum to the
        total or one of them is being computed somewhere else."""
        self.assertEqual(
            self.sum["connected"] + self.sum["not_connected"]
            + self.sum["nothing_to_connect"], self.sum["total"])

    def test_how_it_runs_partitions_the_total_too(self):
        self.assertEqual(self.sum["automatic"] + self.sum["yours"],
                         self.sum["total"])

    def test_collecting_and_enriching_partition_the_total(self):
        self.assertEqual(self.sum["collecting"] + self.sum["enriching"],
                         self.sum["total"])

    def test_nothing_to_configure_is_a_third_answer_not_a_yes(self):
        """None, never True. A source with no prerequisite has not been set up;
        there is simply nothing to set up, and the two read differently to a
        person deciding whether to go and fix something."""
        for r in self.rows:
            self.assertIn(r["connected"], (True, False, None), r["key"])
            if r["connected"] is None:
                self.assertEqual(r["needs"], "",
                                 f"{r['key']}: 'nothing to set up' must not also "
                                 f"name a prerequisite")

    def test_a_source_with_no_prerequisite_reports_none_by_name(self):
        """Named, not counted. A test that only loops over rows already
        reporting None cannot fail when NOTHING reports None (the change
        `connected: None` -> `True`), so the source that must report None is
        named."""
        row = next(r for r in self.rows if r["key"] == "imessage")
        self.assertIsNone(
            row["connected"],
            "iMessage has no setting to fill in; that is 'nothing to set up', "
            "not 'connected'. Reporting it as connected is how a status page "
            "claims seventeen working sources on an install with four unset.")
        self.assertTrue(any(r["connected"] is None for r in self.rows))

    def test_no_row_says_an_environment_variable_name_to_a_person(self):
        """The defect to prevent: "NC_ICS_GOOGLE_URL is not set" on screen.

        `needs` still carries the identifier for anyone who wants to go and edit
        it — it is the SENTENCES that must be in words."""
        for r in self.rows:
            for field in ("connected_says", "runs_says", "last_run_caveat"):
                said = r.get(field) or ""
                self.assertFalse(
                    ENV_SHAPED.search(said),
                    f"{r['key']}.{field} says {said!r} — that is an environment "
                    f"variable name on screen, not a sentence")

    def test_every_reachable_prerequisite_has_words_written_for_it(self):
        """The gate with teeth: a NEW source that needs a NEW setting fails here
        until someone writes the plain sentence for it. Without this, the fix
        above holds only for the variables that exist now."""
        reachable = {(getattr(s, "needs", "") or "").strip()
                     for s in importers.REGISTRY.values()}
        reachable |= {(v.get("needs") or "").strip()
                      for v in api_import.API_SOURCES.values()}
        reachable.discard("")
        missing = sorted(reachable - set(api_sources.NEEDS_SAYS))
        self.assertEqual(missing, [], f"no plain-words sentence for {missing}")

    def test_a_row_can_say_it_ran_and_is_not_connected_at_once(self):
        """The most useful pair on the page, and the one a single number can
        never show: it really did run, and has since been disconnected."""
        db.state_set(self.conn, "import_synced_gmail", "2025-03-01T10:00:00")
        with mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": ""}):
            row = self._row("gmail", api_sources.handle_sources(self.conn))
        self.assertIs(row["connected"], False)
        self.assertTrue(row["last_run_known"])
        self.assertEqual(row["last_run"], "2025-03-01T10:00:00")

    @staticmethod
    def _row(key, out):
        return next(r for r in out["sources"] if r["key"] == key)


class TestTwoStampsThatDisagree(Base):
    """facts and style are stamped by two paths that never reconcile: the Data
    page button writes import_synced_<key>, the background chain writes
    <key>_chain_last. These can read weeks apart."""

    def test_the_later_stamp_wins_and_says_which_one_it_was(self):
        db.state_set(self.conn, "import_synced_facts", "2025-01-06T08:00:00")
        db.state_set(self.conn, "facts_chain_last", "2025-02-17T22:00:00")
        row = next(r for r in api_sources.handle_sources(self.conn)["sources"]
                   if r["key"] == "facts")
        self.assertEqual(row["last_run"], "2025-02-17T22:00:00")
        self.assertEqual(row["last_run_from"], "chain")

    def test_and_the_other_way_round(self):
        db.state_set(self.conn, "import_synced_facts", "2025-02-17T22:00:00")
        db.state_set(self.conn, "facts_chain_last", "2025-01-06T08:00:00")
        row = next(r for r in api_sources.handle_sources(self.conn)["sources"]
                   if r["key"] == "facts")
        self.assertEqual(row["last_run_from"], "button")

    def test_never_run_is_reported_not_guessed(self):
        row = next(r for r in api_sources.handle_sources(self.conn)["sources"]
                   if r["key"] == "email")
        self.assertFalse(row["last_run_known"])
        self.assertEqual(row["last_run"], "")

    def test_a_stamp_known_to_understate_says_so(self):
        """engagement.refresh() runs after every autosync batch without
        restamping, so its date is the last time a person pressed the button.
        Published as a caveat rather than absorbed silently — the same thing
        crm/intake.py does with its unexplained residue."""
        db.state_set(self.conn, "import_synced_engagement", "2025-01-20T10:00:00")
        row = next(r for r in api_sources.handle_sources(self.conn)["sources"]
                   if r["key"] == "engagement")
        self.assertTrue(row["last_run_caveat"],
                        "engagement's stamp understates and must say so")


class TestStatusPageNeverFails(Base):
    """It is the page a person opens BECAUSE something is wrong. Every probe in
    it is wrapped, and these prove the wrapping rather than assuming it."""

    def test_survives_auto_import_paths_raising(self):
        with mock.patch.object(api_import, "auto_import_paths",
                               side_effect=RuntimeError("boom")):
            with mock.patch.object(api_sources.log, "debug"):
                out = api_sources.handle_sources(self.conn)
        self.assertEqual(len(out["sources"]), len(importers.REGISTRY))

    def test_survives_source_blockers_raising(self):
        with mock.patch.object(api_import, "_source_blockers",
                               side_effect=RuntimeError("boom")):
            out = api_sources.handle_sources(self.conn)
        self.assertTrue(all(r["blocked"] == "" for r in out["sources"]))

    def test_the_old_address_is_gone(self):
        """NC-237. /api/sources was asked only by the old Data and Settings
        pages. The address is gone; the Data page's one answer reads _sources
        directly, so the function stays."""
        import app
        self.assertFalse(getattr(api_sources, "ROUTES", {}))
        self.assertNotIn(api_sources, app.ROUTE_MODULES)
        static = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "static")
        for root, _dirs, files in os.walk(static):
            for name in files:
                if name.endswith((".js", ".html")):
                    with open(os.path.join(root, name), encoding="utf-8") as fh:
                        self.assertNotRegex(fh.read(), r"/api/sources(?![/\w-])", name)


if __name__ == "__main__":
    unittest.main()
