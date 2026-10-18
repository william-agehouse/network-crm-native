"""NC-150 gate: a tick over a thing that did not happen.

The NC-140 family in its quietest form — not a check that fails open, but a
failure swallowed and a success reported on top of it. Three of them:

  * `add_to_exclude_file` caught OSError and passed, and `handle_set_excluded`
    returned {"ok": true} regardless. On a read-only or full data folder the
    Hide button said it worked and the file the ten importers read never
    changed. The person is then told the opposite of what is true, and finds
    out at the next import.
  * `_excluded_contacts` returned an empty set both when there was no
    connection and on ANY exception — a crm.db locked by a running import being
    the realistic one. The receipt then read "0 excluded", which is exactly
    what it reads when nobody is excluded.
  * `os.chmod(path, 0o600)` inside `try/except OSError: pass`, on the file that
    holds the Anthropic key and both calendar addresses.

None of these should raise. Swallowing is right in all three — losing a saved
key over a permission bit, or 500-ing a toggle, would each be worse than the
thing being fixed. What was wrong was going SILENT: the fix is that each one is
now *said*, on the screen that claimed it happened.
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, code_of, db, js_code_of  # noqa: E402,F401

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import config  # noqa: E402
import style_profile  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


class TestTheExcludeFileWritersReportBack(Base):
    def setUp(self):
        super().setUp()
        # A path whose parent does not exist: open() raises OSError exactly as a
        # full or read-only data folder does, with nothing to clean up after.
        self.unwritable = os.path.join(self.dir, "not-a-folder", "exclude_list.txt")
        self.ok = os.path.join(self.dir, "exclude_list.txt")

    def test_a_write_that_failed_returns_false(self):
        self.assertFalse(db.add_to_exclude_file(self.unwritable, "Someone"))

    def test_a_name_already_in_the_file_counts_as_written(self):
        db.add_to_exclude_file(self.ok, "Someone")
        self.assertTrue(db.add_to_exclude_file(self.ok, "someone"),
                        "an idempotent no-op is a success — the file says what "
                        "it should, which is the only thing being claimed")

    def test_a_removal_that_failed_returns_false(self):
        with open(self.ok, "w", encoding="utf-8") as fh:
            fh.write("Someone\n")
        os.chmod(self.ok, 0o444)
        try:
            self.assertFalse(db.remove_from_exclude_file(self.ok, "Someone"))
        finally:
            os.chmod(self.ok, 0o644)

    def test_removing_from_a_file_that_is_not_there_is_a_success(self):
        self.assertTrue(db.remove_from_exclude_file(self.unwritable, "Someone"),
                        "a name cannot be in a file that does not exist, so the "
                        "importers already agree and there is nothing to report")

    def test_the_backfill_does_not_count_a_line_it_could_not_write(self):
        cid = db.create_contact(self.conn, "Old Delete")
        db.set_deleted(self.conn, cid, 1)
        self.conn.commit()
        self.assertEqual(db.mirror_excluded_to_file(self.conn, self.unwritable), 0,
                         "startup prints 'Excluded N name(s)' from this number, "
                         "so counting an unwritten line makes the launch line a "
                         "second false claim")


class TestHidingSaysWhenTheSkipListDidNotChange(Base):
    def setUp(self):
        super().setUp()
        self.unwritable = os.path.join(self.dir, "not-a-folder", "exclude_list.txt")

    def test_hide_reports_the_failure_and_still_hides(self):
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.unwritable):
            cid = db.create_contact(self.conn, "Hide Me")
            self.conn.commit()
            out = app.handle_set_excluded(self.conn, cid, {"excluded": True})
        self.assertFalse(out["exclude_file_saved"])
        self.assertIn("could not be written", out["warning"])
        self.assertIn("next import", out["warning"],
                      "the warning must name the consequence, not the errno")
        self.assertEqual(db.get_contact(self.conn, cid)["excluded"], 1,
                         "the app's own half DID work — the warning replaces the "
                         "tick, it does not claim nothing happened")

    def test_delete_reports_it_too(self):
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.unwritable):
            cid = db.create_contact(self.conn, "Delete Me")
            self.conn.commit()
            out = app.handle_delete_contact(self.conn, cid, {"deleted": True})
        self.assertFalse(out["exclude_file_saved"])
        self.assertIn("could not be written", out["warning"])

    def test_a_successful_hide_says_so_without_a_warning(self):
        good = os.path.join(self.dir, "exclude_list.txt")
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", good):
            cid = db.create_contact(self.conn, "Hide Me")
            self.conn.commit()
            out = app.handle_set_excluded(self.conn, cid, {"excluded": True})
        self.assertTrue(out["exclude_file_saved"])
        self.assertNotIn("warning", out,
                         "a warning on the happy path is how people learn to "
                         "ignore warnings")

    def test_the_exclude_list_panel_reports_it(self):
        with mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.unwritable):
            out = app.handle_exclude_list_add(self.conn, {"name": "Spam Bot"})
            self.assertFalse(out["exclude_file_saved"])
            self.assertIn("warning", out)
            gone = app.handle_exclude_list_remove({"name": "Spam Bot"})
        self.assertTrue(gone["exclude_file_saved"],
                        "removing from a file that was never created is not a "
                        "failure")


class TestTheStyleProfileRefusesRatherThanReportingZero(unittest.TestCase):
    def test_an_unreadable_exclusion_list_is_distinguishable_from_an_empty_one(self):
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("database is locked")
        ids, ok = style_profile._excluded_contacts(Boom())
        self.assertEqual(ids, set())
        self.assertFalse(ok, "a locked crm.db still reports 'nobody is excluded'")

    def test_no_connection_is_also_not_a_readable_empty_list(self):
        self.assertEqual(style_profile._excluded_contacts(None), (set(), False))

    def test_the_builder_refuses_when_it_cannot_read_them(self):
        """Invariant 2 is not a tunable, so a profile built without the list is
        not less accurate — it may have been learned from the messages of
        someone the owner removed, and nothing downstream could tell."""
        src = code_of(os.path.join(REPO, "crm", "style_profile.py"))
        i = src.index("excluded_ids, exclusions_readable = _excluded_contacts(conn)")
        after = src[i:i + 900]
        self.assertIn("if not exclusions_readable:", after,
                      "the builder ignores whether the exclusion list was "
                      "readable and carries on with an empty set")
        self.assertIn('result["degraded"] = True', after)
        self.assertIn("return result", after)


class TestTheKeyFileSaysWhenItCouldNotBeSecured(unittest.TestCase):
    def test_harden_returns_false_when_it_cannot_chmod(self):
        d = tempfile.mkdtemp(prefix="crm_harden_")
        self.assertFalse(config.harden(os.path.join(d, "not-there")))

    def test_harden_returns_true_when_it_worked(self):
        d = tempfile.mkdtemp(prefix="crm_harden_")
        p = os.path.join(d, "f.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertTrue(config.harden(p))

    def test_the_settings_endpoint_carries_the_warning(self):
        """Behavioural, not a grep: a test that reads the source for
        `out["warning"]` still passes with the branch switched off by `if False:`,
        because the string is still in the file. A test that reads the code cannot
        see which branch runs.
        """
        d = tempfile.mkdtemp(prefix="crm_settings_")
        with mock.patch.object(config, "SECRETS_DIR", d), \
             mock.patch.object(config, "harden", return_value=False):
            out = app.handle_settings_post({"key": "SELF_NAME", "value": "Ada"})
        self.assertFalse(out["secured"])
        self.assertIn("readable by other accounts", out["warning"],
                      "a chmod that failed on secrets/.env is silent again, on "
                      "the file that holds the API key and calendar addresses")

    def test_a_settings_save_that_worked_carries_no_warning(self):
        d = tempfile.mkdtemp(prefix="crm_settings_")
        with mock.patch.object(config, "SECRETS_DIR", d):
            out = app.handle_settings_post({"key": "SELF_NAME", "value": "Ada"})
        self.assertTrue(out["secured"])
        self.assertNotIn("warning", out)


class TestTheBrowserShowsIt(unittest.TestCase):
    """A warning the server returns and the browser drops is the same bug with
    one more step in it."""

    def test_every_hide_and_delete_toast_prefers_the_warning(self):
        js = js_code_of(os.path.join(APP, "people.js"))
        for call in ("/exclude`", "/delete`"):
            i = js.index(call)
            self.assertIn("d.warning ||", js[i:i + 420],
                          f"the handler for {call} toasts a fixed success "
                          "string over whatever the server said")

    def test_the_exclude_list_panel_toasts_the_warning(self):
        js = js_code_of(os.path.join(APP, "people.js"))
        for call in ('"/api/exclude-list/add"', '"/api/exclude-list/remove"'):
            i = js.index(call)
            self.assertIn("d.warning ||", js[i:i + 360],
                          f"{call} still reports a flat success")

    def test_no_settings_save_toasts_a_bare_saved(self):
        """NC-236: re-pointed from the old Settings page's three save paths to
        where saves are made now. Every box and switch on both pages saves
        through saveSetting (ledger.js), and the model picker (settings.js)
        saves on its own. A toast replaces the one before it, so a warning
        toasted inside saveSetting is wiped by the "saved" toast that follows it
        in the same moment. Each caller has to put the warning in its own toast,
        as the model picker does."""
        self.assertNotIn('toast("Saved")',
                         js_code_of(os.path.join(APP, "settings.js")))
        picker = js_code_of(os.path.join(APP, "settings.js"))
        self.assertIn('toast(d.warning || "Model changed")', picker,
                      "the model picker toasts a bare tick over the warning")
        ledger = js_code_of(os.path.join(APP, "ledger.js"))
        calls = [m.end() for m in re.finditer(r"await saveSetting\(", ledger)]
        self.assertGreaterEqual(len(calls), 4, "fewer save paths than expected")
        for at in calls:
            seg = ledger[at:at + 400]
            toast_call = re.search(r"toast\(([^;]*);", seg)
            with self.subTest(after=seg[:40]):
                self.assertIsNotNone(toast_call)
                self.assertIn("warning", toast_call.group(1),
                              "a save toasts its success without the server's "
                              "warning, and that toast replaces the warning "
                              "saveSetting showed a moment before")


if __name__ == "__main__":
    unittest.main()
