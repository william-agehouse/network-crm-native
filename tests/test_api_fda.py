"""The Full Disk Access panel's API — crm/api_fda.py.

WHAT THESE PROTECT
The state machine in _status(). It is a seven-way decision about what to tell the
user about a permission the app cannot ask for, and getting it wrong does not
produce an error — it produces a confident, wrong sentence. That failure mode
is not hypothetical here. An installer can tell the user Full Disk Access was
already granted when it never had been, if one boolean answers "did any work get
done" while the question being asked is "must the user go and authorise
something".

So every state gets a test, built from a synthetic report file rather than from
a real machine. Nothing in this file reads a protected path, installs anything,
or runs launchctl: tests/helpers.py redirects all four helper paths into a temp
dir, which is also what makes these deterministic on a machine where the helper
IS installed.
"""
import json
import os
import sys
import time
import unittest
from http import HTTPStatus
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import importers  # noqa: E402 — CRM_DATA_DIR bootstrap first

import api_fda  # noqa: E402
from api_core import ApiError  # noqa: E402


def _every_source(outcome="current", **extra):
    """One report row per source THIS BUILD's helper copies.

    Built from importers.HELPER_SOURCE_LABELS rather than written out, because a
    hardcoded three-row version of these fixtures breaks unrelated tests every
    time a source is added — and, worse, goes on asserting "ok" about a report
    that is missing the new source, which is the exact thing `outdated` exists to
    catch. A test that has to be edited to add a source is fine; a test that
    silently stops testing is not."""
    return [dict({"label": label, "outcome": outcome}, **extra)
            for label in importers.HELPER_SOURCE_LABELS]


def _write_report(sources, ran_at=None, exit_code=0):
    """A report file shaped exactly as packaging/src/imessage_sync.swift writes one."""
    os.makedirs(os.path.dirname(importers.IMSG_HELPER_REPORT), exist_ok=True)
    with open(importers.IMSG_HELPER_REPORT, "w", encoding="utf-8") as fh:
        json.dump({
            "version": 1,
            "ran_at": time.time() if ran_at is None else ran_at,
            "exit": exit_code,
            "binary": importers.IMSG_HELPER_BIN,
            "sources": sources,
        }, fh)


class FdaBase(unittest.TestCase):
    """Every test starts from "nothing installed, nothing reported"."""

    def setUp(self):
        for path in (importers.IMSG_HELPER_REPORT, importers.IMSG_HELPER_PLIST):
            if os.path.exists(path):
                os.remove(path)

    def install(self):
        os.makedirs(os.path.dirname(importers.IMSG_HELPER_PLIST), exist_ok=True)
        with open(importers.IMSG_HELPER_PLIST, "w", encoding="utf-8") as fh:
            fh.write("<plist/>")

    def carry_installer(self):
        """Pretend this copy of the app ships the installer script."""
        return mock.patch.object(importers, "imsg_helper_installer",
                                 lambda: "/nowhere/install_imessage_sync.sh")


class TestStatusStates(FdaBase):
    def test_unavailable_when_this_build_ships_no_helper(self):
        """A bundle whose helper did not compile or sign carries none (appify.sh
        warns and goes on, except for a handout, which refuses; since NC-330 swiftc is
        needed for the window itself, so the helper is what can still be missing), and
        the UI must then offer nothing — an option that cannot be delivered is worse
        than silence."""
        with mock.patch.object(importers, "imsg_helper_installer", lambda: ""):
            self.assertEqual(api_fda._status()["state"], "unavailable")

    def test_not_installed_when_the_launchagent_is_absent(self):
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["state"], "not_installed")
        self.assertFalse(s["installed"])

    def test_no_report_when_installed_but_never_run(self):
        self.install()
        with self.carry_installer():
            self.assertEqual(api_fda._status()["state"], "no_report")

    def test_blocked_names_the_blocked_source(self):
        self.install()
        _write_report([{"label": "iMessage", "outcome": "blocked"},
                       {"label": "WhatsApp", "outcome": "copied", "bytes": 70}])
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["state"], "blocked")
        self.assertEqual(s["blocked"], ["iMessage"])
        self.assertEqual(s["ok"], ["WhatsApp"])

    def test_a_partial_block_is_still_blocked(self):
        """THE regression. iMessage blocked + WhatsApp copied is the DEFAULT shape
        on a Mac with WhatsApp Desktop, and the old code called it success. If this
        ever reports "ok", the app is back to telling the user a permission it
        never got was already granted."""
        self.install()
        _write_report([{"label": "iMessage", "outcome": "blocked"},
                       {"label": "WhatsApp", "outcome": "current"}])
        with self.carry_installer():
            self.assertEqual(api_fda._status()["state"], "blocked")

    def test_ok_when_both_sources_are_fine(self):
        self.install()
        report = _every_source()
        report[0] = {"label": "iMessage", "outcome": "copied", "bytes": 12}
        _write_report(report)
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["state"], "ok")
        self.assertEqual(s["blocked"], [])
        self.assertLess(s["ran_ago_seconds"], 5)

    def test_absent_is_not_reported_as_a_permission_problem(self):
        """Neither app installed. Nothing to grant, so nagging about Full Disk
        Access would be an invented problem."""
        self.install()
        report = _every_source("absent", hint="not installed")
        report[0]["hint"] = "you have never used Messages on this Mac"
        _write_report(report)
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["state"], "absent")
        self.assertEqual(s["sources"][0]["hint"],
                         "you have never used Messages on this Mac")

    def test_quiet_when_the_last_good_report_is_ancient(self):
        """Installed, last we heard everything was fine — but that was longer ago
        than the staleness limit, so the helper has stopped running and the copies
        are going stale silently.
        Saying "it refreshes itself" here would be the comfortable lie."""
        self.install()
        _write_report(_every_source(),
                      ran_at=time.time() - (api_fda.STALE_HOURS + 1) * 3600)
        with self.carry_installer():
            self.assertEqual(api_fda._status()["state"], "quiet")

    def test_an_older_installed_helper_is_reported_as_outdated(self):
        """A LaunchAgent keeps running the binary it was installed with. Adding a
        source to the helper (Contacts, NC-029) therefore does nothing at all
        until the user sets it up again — and the only symptom is an absence,
        which reads as "the feature is broken" rather than "run the installer".

        Two of three copied and reported is not "ok"."""
        self.install()
        # Everything except the LAST source this build knows about — whichever
        # that is. Naming it would date the test to the source added that week.
        newest = importers.HELPER_SOURCE_LABELS[-1]
        _write_report([r for r in _every_source() if r["label"] != newest])
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["state"], "outdated")
        self.assertEqual(s["missing"], [newest],
                         "the panel cannot name what a reinstall would add")

    def _carry_helper(self, bundled: bytes, installed: bytes):
        """An app whose installer folder holds a prebuilt helper, and an installed
        helper beside the LaunchAgent; both written to temporary folders."""
        import tempfile
        folder = tempfile.mkdtemp(prefix="nc223-")
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        with open(os.path.join(folder, "networkcrm-imessage-sync"), "wb") as fh:
            fh.write(bundled)
        os.makedirs(os.path.dirname(importers.IMSG_HELPER_BIN), exist_ok=True)
        with open(importers.IMSG_HELPER_BIN, "wb") as fh:
            fh.write(installed)
        self.addCleanup(lambda: os.path.exists(importers.IMSG_HELPER_BIN)
                        and os.remove(importers.IMSG_HELPER_BIN))
        api_fda._HASHES.clear()
        return mock.patch.object(importers, "imsg_helper_installer",
                                 lambda: os.path.join(folder, "install_imessage_sync.sh"))

    def test_an_older_helper_program_is_outdated_even_when_it_copies_everything(self):
        """NC-223. Every source reported, but the installed program is not the one
        this app carries: the person was never told to set it up again, so an
        improved helper reached nobody."""
        self.install()
        _write_report(_every_source())
        with self._carry_helper(b"new helper", b"old helper"):
            s = api_fda._status()
        self.assertEqual(s["state"], "outdated")
        self.assertTrue(s["newer_in_app"])
        self.assertEqual(s["missing"], [])
        self.assertIn("writes less to your disk", s["newer_says"])

    def test_the_same_program_is_not_outdated(self):
        self.install()
        _write_report(_every_source())
        with self._carry_helper(b"same helper", b"same helper"):
            s = api_fda._status()
        self.assertEqual(s["state"], "ok")
        self.assertFalse(s["newer_in_app"])
        self.assertEqual(s["newer_says"], "")

    def test_a_checkout_with_no_prebuilt_helper_compares_nothing(self):
        """A source checkout compiles at install time and keeps no prebuilt
        program beside its installer, so there is nothing to call newer."""
        self.install()
        _write_report(_every_source())
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["state"], "ok")
        self.assertFalse(s["newer_in_app"])

    def test_blocked_still_outranks_an_older_program(self):
        """After setting it up again the new program is blocked until the person
        switches the permission on: that is what the page must say then."""
        self.install()
        _write_report([{"label": "iMessage", "outcome": "blocked"}] + _every_source()[1:])
        with self._carry_helper(b"new helper", b"old helper"):
            self.assertEqual(api_fda._status()["state"], "blocked")

    def test_blocked_outranks_outdated(self):
        """A refused permission is the more actionable of the two, and an old
        helper that is ALSO blocked needs the permission granted either way."""
        self.install()
        _write_report([{"label": "iMessage", "outcome": "blocked"},
                       {"label": "WhatsApp", "outcome": "current"}])
        with self.carry_installer():
            self.assertEqual(api_fda._status()["state"], "blocked")

    def test_blocked_outranks_quiet(self):
        """An old report saying "blocked" still describes something the user must
        go and do; its age does not make it less actionable."""
        self.install()
        _write_report([{"label": "iMessage", "outcome": "blocked"}],
                      ran_at=time.time() - (api_fda.STALE_HOURS + 1) * 3600)
        with self.carry_installer():
            self.assertEqual(api_fda._status()["state"], "blocked")


class TestStatusIsDefensive(FdaBase):
    def test_a_corrupt_report_reads_as_no_report_not_as_success(self):
        self.install()
        os.makedirs(os.path.dirname(importers.IMSG_HELPER_REPORT), exist_ok=True)
        with open(importers.IMSG_HELPER_REPORT, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        with self.carry_installer():
            self.assertEqual(api_fda._status()["state"], "no_report")

    def test_junk_rows_are_dropped_not_rendered(self):
        self.install()
        _write_report(["not a dict", {"label": "iMessage", "outcome": "blocked"}])
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual([r["label"] for r in s["sources"]], ["iMessage"])

    def test_a_row_with_no_outcome_is_unknown_rather_than_ok(self):
        """Defaulting an unreadable row to "fine" is how a status panel lies."""
        self.install()
        _write_report([{"label": "iMessage"}])
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["sources"][0]["outcome"], "unknown")
        self.assertEqual(s["ok"], [])

    def test_the_file_count_reaches_the_panel(self):
        """NC-093. The helper writes `matched` for EVERY source, copied or not,
        and its own comment says it is "the count the app needs to notice a
        source that is technically fine and substantively empty". This function
        was passing `bytes` and dropping both `matched` and `files`, so the count
        added specifically to make a silent failure visible could not reach the
        interface.

        The failure it exists for, from the helper's own notes: on a Mac that
        syncs contacts, the flat address-book store can hold a single person with
        everyone else in per-account stores, so nothing is reported absent — the
        helper says `current`, the row goes green, and the import brings in one
        contact out of thousands, with no error, no warning and no count anywhere
        to notice it by.

        `current` is the load-bearing case, and it is the one where `files` is
        absent — so a fix that passed only `files` would display nothing in the
        state that actually failed. In a real report every `current` source has
        `files` absent and `matched` present, each with its own count.
        """
        self.install()
        _write_report([
            {"label": "iMessage", "outcome": "current", "matched": 3},
            {"label": "Contacts", "outcome": "current", "matched": 18},
        ])
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual([r.get("matched") for r in s["sources"]], [3, 18],
                         "the count the helper wrote did not survive the API")

    def test_a_one_file_source_is_still_a_number_not_a_silence(self):
        """The single-store case itself: a source that matched a single store must
        report 1, because 1 beside a green line is the only signal there is."""
        self.install()
        _write_report([{"label": "Contacts", "outcome": "current", "matched": 1}])
        with self.carry_installer():
            s = api_fda._status()
        self.assertEqual(s["sources"][0]["matched"], 1)

    def test_a_copied_source_carries_both_counts(self):
        self.install()
        _write_report([{"label": "WhatsApp", "outcome": "copied",
                        "bytes": 4096, "files": 7, "matched": 9}])
        with self.carry_installer():
            s = api_fda._status()
        row = s["sources"][0]
        self.assertEqual((row["bytes"], row["files"], row["matched"]), (4096, 7, 9))

    def test_a_hand_edited_count_cannot_reach_the_panel_as_a_string(self):
        """Same reason bytes is coerced: this is where a file on disk becomes a
        JSON response, and the panel formats these with toLocaleString()."""
        self.install()
        _write_report([{"label": "iMessage", "outcome": "current",
                        "matched": "lots", "files": None}])
        with self.carry_installer():
            s = api_fda._status()
        self.assertNotIn("matched", s["sources"][0])
        self.assertNotIn("files", s["sources"][0])

    def test_a_report_from_the_future_does_not_produce_negative_age(self):
        self.install()
        _write_report([{"label": "iMessage", "outcome": "copied", "bytes": 1}],
                      ran_at=time.time() + 9999)
        with self.carry_installer():
            self.assertEqual(api_fda._status()["ran_ago_seconds"], 0.0)


class TestActionsRefuseTheImpossible(FdaBase):
    def test_install_refuses_when_no_installer_is_carried(self):
        with mock.patch.object(importers, "imsg_helper_installer", lambda: ""):
            with self.assertRaises(ApiError) as ctx:
                api_fda.handle_fda_install({})
        self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)

    def test_recheck_refuses_when_the_helper_is_not_installed(self):
        """There is nothing for launchd to kickstart, and pretending otherwise
        would surface as an opaque launchctl error."""
        with self.assertRaises(ApiError) as ctx:
            api_fda.handle_fda_recheck()
        self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)

    def test_install_validates_the_interval_instead_of_passing_it_through(self):
        """The interval reaches a bash script as an env var, and the installer
        rejects a non-number itself — but 0 seconds would be accepted there as a
        launchd StartInterval and hammer the disk every tick."""
        with self.carry_installer():
            for bad in (0, -5, 10_000, "soon"):
                with self.subTest(bad=bad), self.assertRaises(ApiError):
                    api_fda.handle_fda_install({"interval_hours": bad})

    def test_install_passes_a_valid_interval_as_the_env_var_the_script_reads(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["interval"] = kw["env"].get("NC_IMESSAGE_SYNC_INTERVAL")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        with self.carry_installer(), mock.patch.object(api_fda.subprocess, "run", fake_run):
            api_fda.handle_fda_install({"interval_hours": 2})
        self.assertEqual(captured["interval"], "7200")
        self.assertEqual(captured["argv"][0], "/bin/bash")
        self.assertEqual(captured["argv"][1], "/nowhere/install_imessage_sync.sh")

    def test_install_never_runs_a_path_from_the_request(self):
        """The client may not name the script. It is derived from the install
        layout, exactly like every path in importers.OPEN_TARGETS."""
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.carry_installer(), mock.patch.object(api_fda.subprocess, "run", fake_run):
            api_fda.handle_fda_install({"installer": "/tmp/evil.sh",
                                        "script": "/tmp/evil.sh"})
        self.assertNotIn("/tmp/evil.sh", captured["argv"])

    def test_install_reports_a_failing_installer_rather_than_raising(self):
        """A failed install is information for the panel, not a 500: the output is
        the only thing that explains why."""
        with self.carry_installer(), mock.patch.object(
                api_fda.subprocess, "run",
                lambda *a, **k: mock.Mock(returncode=1, stdout="", stderr="swiftc missing")):
            out = api_fda.handle_fda_install({})
        self.assertFalse(out["ok"])
        self.assertIn("swiftc missing", out["output"])
        self.assertIn("state", out["status"])


class TestRecheckGoesThroughLaunchd(FdaBase):
    """Not through a subprocess of the app.

    The permission is pinned to the helper binary, and launchd starting it is the
    exact path the 6-hourly refresh takes. Running it as a child of the app would
    make the answer depend on how macOS attributes a TCC check across a process
    chain — a subtlety this panel exists to stop the user having to think about,
    and one the app has no business betting a green checkmark on.
    """

    def test_it_kickstarts_the_launchagent_by_label(self):
        self.install()
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(api_fda.subprocess, "run", fake_run), \
             mock.patch.object(api_fda, "RECHECK_TIMEOUT", 0):
            out = api_fda.handle_fda_recheck()
        self.assertEqual(captured["argv"][:3], ["launchctl", "kickstart", "-k"])
        self.assertIn(importers.IMSG_HELPER_LABEL, captured["argv"][3])
        self.assertFalse(out["ran"])          # nothing wrote a report

    def test_it_waits_for_a_report_that_CHANGED(self):
        """A stale report must not be read back as this run's answer. That is the
        same mistake as the exit code that said "already granted": reusing an old
        signal as a fresh one."""
        self.install()
        _write_report([{"label": "iMessage", "outcome": "blocked"}],
                      ran_at=time.time() - 3600)

        def fake_run(argv, **kw):
            # Stand in for the helper: launchd starts it, and it reports.
            report = _every_source()
            report[0] = {"label": "iMessage", "outcome": "copied", "bytes": 5}
            _write_report(report)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(api_fda.subprocess, "run", fake_run), \
             mock.patch.object(api_fda, "RECHECK_POLL_SECONDS", 0.01):
            out = api_fda.handle_fda_recheck()
        self.assertTrue(out["ran"])
        self.assertEqual(out["status"]["state"], "ok")

    def test_a_launchctl_failure_is_explained_not_swallowed(self):
        self.install()
        with mock.patch.object(
                api_fda.subprocess, "run",
                lambda *a, **k: mock.Mock(returncode=3, stdout="",
                                          stderr="Could not find service")):
            with self.assertRaises(ApiError) as ctx:
                api_fda.handle_fda_recheck()
        self.assertIn("Could not find service", str(ctx.exception))


class TestOpenTargetsForTheTwoButtons(unittest.TestCase):
    """The panel's two buttons post an allowlist KEY, never a path. If either key
    stops resolving, the button silently does nothing at all."""

    def test_the_settings_button_opens_the_full_disk_access_pane(self):
        t = importers.resolve_open_target("fda_settings")
        self.assertIsNotNone(t)
        self.assertEqual(t["argv"][0][0], "open")
        self.assertIn("Privacy_AllFiles", t["argv"][0][1])

    def test_the_finder_button_reveals_the_binary_the_user_must_drag(self):
        os.makedirs(os.path.dirname(importers.IMSG_HELPER_BIN), exist_ok=True)
        with open(importers.IMSG_HELPER_BIN, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        try:
            t = importers.resolve_open_target("fda_binary")
            self.assertEqual(t["argv"][0][:2], ["open", "-R"])
            self.assertEqual(t["argv"][0][2], importers.IMSG_HELPER_BIN)
            # …and falls back to the plain folder if Finder refuses the reveal.
            self.assertEqual(t["argv"][-1], ["open", os.path.dirname(importers.IMSG_HELPER_BIN)])
        finally:
            os.remove(importers.IMSG_HELPER_BIN)

    def test_the_keys_the_status_hands_the_client_are_the_ones_that_resolve(self):
        """The status payload names its own buttons. A rename on one side only is
        a dead control with no error anywhere."""
        with mock.patch.object(importers, "imsg_helper_installer", lambda: "/x/y.sh"):
            targets = api_fda._status()["targets"]
        for key in targets.values():
            self.assertIsNotNone(importers.resolve_open_target(key),
                                 f"/api/fda/status offers '{key}', which is not "
                                 "in importers.OPEN_TARGETS")


class TestRoutesAreReachable(unittest.TestCase):
    def test_every_declared_route_names_a_real_handler(self):
        for method, table in api_fda.ROUTES.items():
            for path, name in table.items():
                with self.subTest(route=f"{method} {path}"):
                    self.assertTrue(callable(getattr(api_fda, name, None)),
                                    f"{method} {path} points at {name}, which is "
                                    "not callable — the registry 500s on that")

    def test_the_module_is_in_app_route_modules(self):
        """A module with a ROUTES table that nobody added to ROUTE_MODULES is
        finished code that nothing can reach."""
        import app
        self.assertIn(api_fda, app.ROUTE_MODULES)

    def test_no_handler_asks_for_a_db_connection(self):
        """None of this touches crm.db, and the registry reads that off the
        signature: a first parameter named `conn` would open one for no reason."""
        import inspect
        for table in api_fda.ROUTES.values():
            for name in table.values():
                params = list(inspect.signature(getattr(api_fda, name)).parameters)
                self.assertNotEqual(params[:1], ["conn"], f"{name} takes a conn")


if __name__ == "__main__":
    unittest.main()


class TestTheSwitchHasAnOffPosition(FdaBase):
    """NC-187. Invariant 6 calls the two ways in one switch, and the Data page
    could only move it one way: POST /api/fda/install existed and nothing ran
    packaging/uninstall_imessage_sync.sh."""

    def carry_uninstaller(self):
        return mock.patch.object(importers, "imsg_helper_uninstaller",
                                 lambda: "/nowhere/uninstall_imessage_sync.sh")

    def test_the_route_exists(self):
        self.assertEqual(api_fda.ROUTES["POST"]["/api/fda/uninstall"],
                         "handle_fda_uninstall")

    def test_it_runs_the_bundled_uninstaller_and_nothing_else(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout="Uninstalled.", stderr="")

        with self.carry_uninstaller(), mock.patch.object(api_fda.subprocess, "run", fake_run):
            out = api_fda.handle_fda_uninstall()
        self.assertEqual(captured["argv"], ["/bin/bash", "/nowhere/uninstall_imessage_sync.sh"])
        self.assertTrue(out["ok"])
        self.assertIn("Uninstalled.", out["output"])

    def test_a_build_with_no_helper_refuses(self):
        with mock.patch.object(importers, "imsg_helper_uninstaller", lambda: ""):
            with self.assertRaises(ApiError) as ctx:
                api_fda.handle_fda_uninstall()
        self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)

    def test_the_uninstaller_sits_beside_the_installer(self):
        """appify.sh ships both scripts side by side, and so does packaging/."""
        real = importers.imsg_helper_installer()
        self.assertTrue(real, "this checkout should carry the installer")
        self.assertEqual(os.path.dirname(importers.imsg_helper_uninstaller()),
                         os.path.dirname(real))


class TestTurningItOffFallsBackOnItsOwn(FdaBase):
    """After the helper is off, the rows say "Your copy in the import folder".
    The uninstaller removes the LaunchAgent and leaves the report
    and every copy behind — so without these two rules the rows would go on
    saying "Updated automatically" about a program that no longer exists."""

    def test_a_report_left_by_an_uninstalled_helper_counts_for_nothing(self):
        self.install()
        _write_report(_every_source("copied"))
        self.assertEqual(importers.auto_copy_label(importers.HELPER_IMESSAGE),
                         importers.AUTO_COPY_HELPER)
        os.remove(importers.IMSG_HELPER_PLIST)        # what the uninstaller does
        self.assertEqual(importers.imsg_helper_report(), {})
        self.assertEqual(importers.auto_copy_label(importers.HELPER_IMESSAGE),
                         importers.AUTO_COPY_MANUAL)
        self.assertFalse(importers.helper_current(importers.HELPER_IMESSAGE))

    def test_a_blocked_report_does_not_outlive_the_helper_either(self):
        self.install()
        _write_report(_every_source("blocked"))
        self.assertTrue(importers.helper_blocked(importers.HELPER_IMESSAGE))
        os.remove(importers.IMSG_HELPER_PLIST)
        self.assertFalse(importers.helper_blocked(importers.HELPER_IMESSAGE))


class TestThePanelOffersTheOffPosition(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "static", "app", "fda.js"), encoding="utf-8") as fh:
            self.js = fh.read()

    def test_it_calls_the_uninstall_route(self):
        self.assertIn('"/api/fda/uninstall"', self.js)

    def test_every_installed_state_has_the_control(self):
        """Working, outdated, and blocked-or-silent are three render branches;
        the switch is off-able from each."""
        self.assertEqual(self.js.count("<${TurnOff}"), 3)

    def test_the_second_step_says_the_copies_stay(self):
        start = self.js.index("function TurnOff(")
        body = self.js[start:self.js.index("\nfunction ", start + 1)]
        self.assertIn("already copied stays", " ".join(body.split()))

    def test_the_one_line_state_can_show_what_the_script_said(self):
        """A refused "Turn it off" on the working line must not toast "see the
        details below" while that branch renders no details."""
        start = self.js.index("// Working.")
        branch = self.js[start:self.js.index("// The two states that need", start)]
        self.assertIn("fda-output", branch)
