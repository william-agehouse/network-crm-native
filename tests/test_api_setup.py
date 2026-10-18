"""crm/api_setup.py + db.source_coverage: the setup-progress panel's numbers.

The point of these tests is the panel's central claim — that `history` is the
honest number and the contact count is the flattering one. If a future change
makes `history` move when you import a bare address book, the panel starts
telling the user setup worked when it did not, and it does so silently, because
every number involved is a plausible-looking integer.
"""
import inspect
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

import api_setup  # noqa: E402


class TestSourceCoverage(Base):
    def test_contacts_alone_do_not_move_history(self):
        """The whole thesis of the panel, as an assertion.

        Three names with no conversation on any channel: `people` is 3 and
        `history` is 0. If these two ever move together, the setup screen is
        congratulating the user for importing a phone book.
        """
        for name in ("Ada Lovelace", "Alan Turing", "Grace Hopper"):
            db.create_contact(self.conn, name)
        cov = db.source_coverage(self.conn)
        self.assertEqual(cov["people"], 3)
        self.assertEqual(cov["history"], 0)

    def test_history_counts_a_person_once_across_channels(self):
        """`history` is people, not conversations.

        Someone you both iMessage and WhatsApp is one person with history, not
        two — the SQL uses an OR across the channel counters for exactly this,
        and summing the per-channel columns instead would double-count the
        people you talk to most.
        """
        cid = db.create_contact(self.conn, "Katherine Johnson")
        self.conn.execute(
            "UPDATE contacts SET imsg_count = 40, wa_count = 12 WHERE id = ?", (cid,))
        self.conn.commit()
        cov = db.source_coverage(self.conn)
        self.assertEqual(cov["people"], 1)
        self.assertEqual(cov["history"], 1)
        self.assertEqual(cov["imessage"], 1)
        self.assertEqual(cov["whatsapp"], 1)

    def test_excluded_people_are_out_of_every_number(self):
        """Excluded contacts are excluded from the denominator too.

        Otherwise the panel's share-of-network bars are drawn against a
        population the rest of the app does not show.
        """
        keep = db.create_contact(self.conn, "Rosalind Franklin")
        drop = db.create_contact(self.conn, "Spam Recruiter")
        self.conn.execute("UPDATE contacts SET imsg_count = 5 WHERE id = ?", (keep,))
        self.conn.execute(
            "UPDATE contacts SET imsg_count = 99, excluded = 1 WHERE id = ?", (drop,))
        self.conn.commit()
        cov = db.source_coverage(self.conn)
        self.assertEqual(cov["people"], 1)
        self.assertEqual(cov["history"], 1)
        self.assertEqual(cov["imessage"], 1)


class TestMeetingsCountTheSamePeopleAsEverythingElse(Base):
    """NC-201. Every figure in source_coverage leaves hidden people out, and
    `meetings` was a separate query that never looked at who they were."""

    def _meeting(self, cid, uid):
        self.conn.execute(
            "INSERT INTO contact_dates (contact_id, kind, label, on_date, source, "
            "source_uid) VALUES (?, 'meeting', 'Lunch', '2026-06-01', 'calendar', ?)",
            (cid, uid))

    def test_a_hidden_persons_meeting_is_not_counted(self):
        seen = db.create_contact(self.conn, "Met Openly")
        hid = db.create_contact(self.conn, "Met And Hidden")
        self._meeting(seen, "e1")
        self._meeting(hid, "e2")
        db.set_excluded(self.conn, hid, 1)
        self.conn.commit()
        self.assertEqual(db.source_coverage(self.conn)["meetings"], 1)


class TestPhotoCoverageCountsTheColumnThatHoldsPhotos(Base):
    """NC-091. `photos` counted `photo_file_id` — a retired Telegram profile
    picture handle, declared in db.py as "telegram file_id for a profile
    picture". Nothing writes it any more, so the column reads zero for nearly
    every row, while `photo` holds most of the pictures.

    Nothing rendered it, which is the only reason nobody saw it: no entry in
    api_setup.STEPS carries `"metric": "photos"`, so the number shipped to the
    browser inside /api/setup/progress and was dropped. That makes it a trap
    rather than a live lie — the day a Photos step is added it would read 0
    beside a Data page panel reading the real count.

    These assert the two AGREE rather than asserting either number, so they
    survive the data changing.
    """

    def _other_sites_figures(self):
        """What api_import, api_instance and api_provenance each report, read
        from the functions themselves rather than from a stand-in query.

        NC-122: all three now count the same `active` people as
        source_coverage. They used to count the whole table while this counted
        everyone not hidden, and a stand-in query that assumed "whole table"
        kept passing the day that stopped being true."""
        import api_import
        import api_instance
        import api_provenance
        return {
            "api_import": api_import.handle_import_sources(self.conn)["photos_count"],
            "api_instance": api_instance._settings_diagnostics(self.conn)["photos"],
            "api_provenance": api_provenance._crm_counts(self.conn)["photos"]["total"],
        }

    def test_coverage_agrees_with_the_three_places_that_count_photos(self):
        with_photo = ("Ada Lovelace", "Alan Turing", "Grace Hopper")
        for name in with_photo:
            cid = db.create_contact(self.conn, name)
            self.conn.execute(
                "UPDATE contacts SET photo = ? WHERE id = ?",
                (f"/photos/{cid}.jpg", cid))
        db.create_contact(self.conn, "Nobody Pictured")
        self.conn.commit()

        cov = db.source_coverage(self.conn)
        for site, n in self._other_sites_figures().items():
            with self.subTest(site=site):
                self.assertEqual(cov["photos"], n,
                                 "the setup panel and the Data page must not "
                                 "report two different photo counts")
        # Not vacuous: a fixture where both were 0 would pass the line above
        # while the bug was still there.
        self.assertEqual(cov["photos"], len(with_photo))

    def test_the_telegram_bots_column_no_longer_moves_the_number(self):
        """Filling `photo_file_id` and nothing else must
        leave `photos` at zero — otherwise the expression is still reading the
        dead column, and the test above would pass on a fixture that happens to
        set both."""
        cid = db.create_contact(self.conn, "Katherine Johnson")
        self.conn.execute(
            "UPDATE contacts SET photo_file_id = 'AgACAgQAAxkBAAI' WHERE id = ?",
            (cid,))
        self.conn.commit()
        self.assertEqual(db.source_coverage(self.conn)["photos"], 0,
                         "photo_file_id is a retired Telegram handle, "
                         "not a picture this app can show")

    def test_a_hidden_or_deleted_persons_photo_is_counted_nowhere(self):
        """NC-122. This test used to pin the opposite: that the setup panel left
        a hidden person's photo out while the other three sites counted it. That
        divergence was the bug — two photo counts on one page, differing by the
        people you hid. Now all four leave out the same people."""
        keep = db.create_contact(self.conn, "Rosalind Franklin")
        hid = db.create_contact(self.conn, "Spam Recruiter")
        gone = db.create_contact(self.conn, "Deleted Only")
        for cid in (keep, hid, gone):
            self.conn.execute(
                "UPDATE contacts SET photo = ? WHERE id = ?", (f"/p/{cid}.jpg", cid))
        self.conn.execute("UPDATE contacts SET excluded = 1 WHERE id = ?", (hid,))
        # Deleted without being hidden, which db.set_deleted never writes but a
        # direct write can: the one case where `visible` and `active` differ.
        self.conn.execute("UPDATE contacts SET deleted = 1 WHERE id = ?", (gone,))
        self.conn.commit()

        self.assertEqual(db.source_coverage(self.conn)["photos"], 1)
        for site, n in self._other_sites_figures().items():
            with self.subTest(site=site):
                self.assertEqual(n, 1, f"{site} counts a photo of someone hidden "
                                       "or deleted")

    def test_no_setup_step_claims_the_photos_metric_yet(self):
        """Why this was invisible, stated as a fact rather than a comment. When
        someone does add a Photos step, this test fails and its message points
        at the reason the number is now trustworthy."""
        metrics = [s.get("metric") for s in api_setup.STEPS]
        self.assertNotIn("photos", metrics,
                         "a step now renders `photos` — good; it reads the "
                         "`photo` column since NC-091, so delete this test")


class TestSetupProgress(Base):
    def test_empty_install_returns_zeroes_not_an_error(self):
        """A fresh install is this endpoint's FIRST caller, and it has no
        archive.db at all. That has to read as "nothing yet", never as a 500 on
        the one screen a new user cannot navigate away from."""
        out = api_setup.handle_setup_progress(self.conn)
        self.assertEqual(out["people"], 0)
        self.assertEqual(out["history"], 0)
        self.assertEqual(out["history_pct"], 0.0)
        self.assertEqual(out["messages"], 0)
        self.assertEqual(out["steps_done"], 0)
        self.assertEqual(out["steps_total"], len(api_setup.STEPS))
        self.assertTrue(all(s["done"] is False for s in out["steps"]))

    def test_history_pct_does_not_divide_by_zero(self):
        """Guarded because the zero-contact case is the common one, not the edge
        one — it is what every single install looks like on first run."""
        self.assertEqual(api_setup.handle_setup_progress(self.conn)["history_pct"], 0.0)

    def test_a_step_is_done_only_when_its_own_metric_moved(self):
        """`done` is per-step, driven by that step's own metric.

        Adding contacts must not mark iMessage done. This is the failure that
        would let the panel show "3/7 connected" for one import.
        """
        db.create_contact(self.conn, "Ada Lovelace")
        out = api_setup.handle_setup_progress(self.conn)
        done = {s["key"] for s in out["steps"] if s["done"]}
        self.assertEqual(done, {"contacts"})
        self.assertEqual(out["steps_done"], 1)

    def test_every_step_metric_is_a_real_coverage_key(self):
        """The two halves of this feature are joined by a string, so nothing but
        a test notices when one side is renamed.

        A `metric` that no longer exists in source_coverage() does not raise —
        `cov.get()` returns 0 — so the step just reads "not yet" forever, on a
        screen whose entire job is to tell you whether the step worked.
        """
        cov = db.source_coverage(self.conn)
        for spec in api_setup.STEPS:
            self.assertIn(spec["metric"], cov,
                          f"{spec['key']}'s metric {spec['metric']!r} is not in "
                          "db.source_coverage() — that step can never report done")

    def test_every_step_carries_the_copy_the_panel_renders(self):
        """The UI renders `gives`, `cost`, `unit` and `label` directly. A missing
        one is an empty element rather than an error, so it ships looking merely
        odd."""
        for spec in api_setup.STEPS:
            for field in ("key", "label", "metric", "unit", "cost", "gives"):
                self.assertTrue(str(spec.get(field, "")).strip(),
                                f"step {spec.get('key')!r} is missing {field}")

    def test_every_unit_names_the_population_it_counted(self):
        """NC-067. Every metric in `db.source_coverage()` is a count of CONTACTS
        — the query is one SUM(...) per column over the contacts table — so every
        unit describes some set of people and should say so.

        LinkedIn's said "connections". Its metric is SUM(linkedin_url <> ''),
        which counts people who have a LinkedIn link from ANY source, including
        ones the scraper attached to address-book contacts. That can read a
        larger number of "connections" than the LinkedIn panel on the same page,
        which reads people from COUNT(source = 'linkedin'). Two numbers for one
        question, and the label was what made them look comparable.

        A bare noun invites exactly that: it reads as a fact about LinkedIn
        rather than a fact about this database's rows.
        """
        for spec in api_setup.STEPS:
            unit = spec["unit"]
            self.assertTrue(
                unit.startswith("people"),
                f"step {spec['key']!r} has unit {unit!r}. Every source_coverage "
                f"metric counts rows in `contacts`, so the unit has to name "
                f"which people — a bare noun like 'connections' claims to "
                f"measure the outside world instead")

    def test_step_keys_are_unique_and_ordered_for_the_next_hint(self):
        """The client picks "do this next" as the first not-done step in this
        order, so the order is behaviour, not presentation. Contacts must lead:
        every other importer only enriches people who are already in crm.db
        (invariant 1), so any other first step imports into an empty table."""
        keys = [s["key"] for s in api_setup.STEPS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys[0], "contacts")

    def test_the_old_address_is_gone(self):
        """NC-237. /api/setup/progress was asked only by the old Data page. The
        address is gone; the function stays, because these tests and the Data
        page's own answer still read this module directly."""
        import app
        self.assertFalse(getattr(api_setup, "ROUTES", {}))
        self.assertNotIn(api_setup, app.ROUTE_MODULES)
        static = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "static")
        for root, _dirs, files in os.walk(static):
            for name in files:
                if name.endswith((".js", ".html")):
                    with open(os.path.join(root, name), encoding="utf-8") as fh:
                        self.assertNotIn("/api/setup/progress", fh.read(), name)

    def test_handler_takes_conn_first_so_the_dispatcher_supplies_one(self):
        """app.py decides whether to hand over a db connection by reading the
        handler's first parameter name. Rename it and the route breaks at
        runtime only."""
        params = list(inspect.signature(api_setup.handle_setup_progress).parameters)
        self.assertEqual(params[0], "conn")

    def test_read_only(self):
        """The panel is encouragement, and must never mutate the database.

        Checked with sqlite's own authorizer rather than by grepping the source
        for "UPDATE": a grep trips on a *comment* containing the word "update",
        and the substring "CREATE" matches the word "created". The authorizer
        asks the database what was actually attempted, which is the thing the
        invariant is about, and it catches DDL as well as writes.
        """
        denied = []

        def authorizer(action, arg1, arg2, dbname, source):
            # SQLITE_SELECT / _READ / _FUNCTION are the only ops a read path needs.
            if action not in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                              sqlite3.SQLITE_FUNCTION):
                denied.append((action, arg1, arg2))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        before = self.conn.total_changes
        self.conn.set_authorizer(authorizer)
        try:
            out = api_setup.handle_setup_progress(self.conn)
        finally:
            self.conn.set_authorizer(None)
        # It has to still WORK read-only, not merely avoid writing.
        self.assertEqual(out["people"], 1)
        self.assertEqual(denied, [], f"api_setup attempted a write: {denied}")
        self.assertEqual(self.conn.total_changes, before)


if __name__ == "__main__":
    unittest.main()
