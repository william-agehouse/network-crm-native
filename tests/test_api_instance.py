"""Instance config and health: the in-app Settings editor, the update checker, and first-run onboarding."""

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, arc, config, db, settings_mod, updates_mod,
)

# The module that owns the semantic-install handlers. Reached through app's
# re-exported module object (app.api_instance) so patches land on the same module
# the route table and the handlers themselves use — the "patch the owner, not the
# re-export" rule.
ai = app.api_instance


class TestSettings(unittest.TestCase):
    """In-app Settings surface: whitelist enforcement, secrets/.env
    round-trip with comment preservation, masking, os.environ sync, and
    diagnostics — all against temp paths so the real secrets/.env, crm.db and
    archive.db are never touched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_settings_")

        self._orig_secrets_dir = config.SECRETS_DIR
        config.SECRETS_DIR = self.dir

        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        self.conn = db.get_db(self.dbpath)

        self._orig_archive_path = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(self.dir, "archive.db")  # left absent on purpose

        # Isolate every managed key from whatever the real shell/dev env has set,
        # so assertions never depend on the machine running the tests.
        self._orig_env = {}
        for key in settings_mod.MANAGED_KEYS:
            self._orig_env[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        self.conn.close()
        config.SECRETS_DIR = self._orig_secrets_dir
        db.DB_PATH = self._orig_db_path
        arc.ARCHIVE_DB_PATH = self._orig_archive_path
        for key, val in self._orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _env_path(self):
        return os.path.join(self.dir, ".env")

    def _by_key(self):
        return {s["key"]: s for s in settings_mod.read_all()}

    def test_rejects_key_outside_whitelist(self):
        with self.assertRaises(app.ApiError):
            app.handle_settings_post({"key": "SOME_RANDOM_ENV_VAR", "value": "x"})
        self.assertFalse(os.path.exists(self._env_path()))

    def test_rejects_newline_injection(self):
        # a crafted value must never smuggle a second KEY=... line into .env
        with self.assertRaises(app.ApiError) as ctx:
            app.handle_settings_post({"key": "SELF_NAME", "value": "Ada\nEVIL_KEY=1"})
        self.assertEqual(ctx.exception.status, 400)
        if os.path.exists(self._env_path()):
            content = open(self._env_path(), encoding="utf-8").read()
            self.assertNotIn("EVIL_KEY", content)

    def test_write_read_roundtrip_preserves_comments_and_other_lines(self):
        with open(self._env_path(), "w", encoding="utf-8") as fh:
            fh.write("# a hand-written comment\nUNRELATED_KEY=keep-me\n")

        app.handle_settings_post({"key": "SELF_NAME", "value": "Ada Lovelace"})

        with open(self._env_path(), encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("# a hand-written comment", content)
        self.assertIn("UNRELATED_KEY=keep-me", content)
        self.assertIn("SELF_NAME=Ada Lovelace", content)

        row = self._by_key()["SELF_NAME"]
        self.assertEqual(row["value"], "Ada Lovelace")
        self.assertTrue(row["set"])
        self.assertFalse(row["secret"])

        # writing again replaces the line in place rather than duplicating it
        app.handle_settings_post({"key": "SELF_NAME", "value": "Ada L."})
        with open(self._env_path(), encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertEqual(sum(1 for l in lines if l.startswith("SELF_NAME=")), 1)
        self.assertIn("SELF_NAME=Ada L.\n", lines)

    def test_secret_is_masked_and_full_value_never_returned(self):
        app.handle_settings_post({"key": "ANTHROPIC_API_KEY", "value": "sk-ant-abcdef1234"})
        row = self._by_key()["ANTHROPIC_API_KEY"]
        self.assertNotIn("sk-ant-abcdef1234", row["value"])
        self.assertNotIn("abcdef", row["value"])
        self.assertTrue(row["value"].endswith("1234"))
        self.assertTrue(row["secret"])
        self.assertTrue(row["set"])
        # the raw JSON of the whole read_all() call is also clean
        for s in settings_mod.read_all():
            self.assertNotIn("sk-ant-abcdef1234", json.dumps(s))

    def test_empty_value_removes_line_and_clears_environ(self):
        app.handle_settings_post({"key": "SELF_EMAIL", "value": "ada@example.com"})
        self.assertEqual(os.environ.get("SELF_EMAIL"), "ada@example.com")

        app.handle_settings_post({"key": "SELF_EMAIL", "value": ""})
        self.assertIsNone(os.environ.get("SELF_EMAIL"))
        with open(self._env_path(), encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("SELF_EMAIL", content)
        row = self._by_key()["SELF_EMAIL"]
        self.assertEqual(row["value"], "")
        self.assertFalse(row["set"])

    def test_os_environ_updated_immediately_on_set(self):
        self.assertIsNone(os.environ.get("ANTHROPIC_MODEL"))
        app.handle_settings_post({"key": "ANTHROPIC_MODEL", "value": "claude-x-9"})
        self.assertEqual(os.environ["ANTHROPIC_MODEL"], "claude-x-9")

    def test_mask_value_post_is_rejected(self):
        app.handle_settings_post({"key": "LINKEDIN_TOKEN", "value": "123456:ABCDEFGHIJ"})
        masked = self._by_key()["LINKEDIN_TOKEN"]["value"]
        self.assertTrue(masked.startswith(settings_mod.MASK_PREFIX))
        with self.assertRaises(app.ApiError):
            app.handle_settings_post({"key": "LINKEDIN_TOKEN", "value": masked})
        # rejected save left the real value untouched
        self.assertEqual(os.environ["LINKEDIN_TOKEN"], "123456:ABCDEFGHIJ")

    def test_whitelist_rejects_write_key_directly(self):
        with self.assertRaises(ValueError):
            settings_mod.write_key("NOT_MANAGED", "x")

    def test_diagnostics_shape_and_counts(self):
        cid = db.create_contact(self.conn, "Diag Person")
        db.add_note(self.conn, cid, "a note")
        self.conn.execute("UPDATE contacts SET photo='1.jpg' WHERE id=?", (cid,))
        db.state_set(self.conn, "import_synced_whatsapp", "2026-07-01T10:00:00+00:00")
        self.conn.commit()

        out = app.handle_settings_get(self.conn)
        self.assertIn("settings", out)
        self.assertIn("diagnostics", out)
        diag = out["diagnostics"]

        self.assertEqual(diag["crm_db_path"], self.dbpath)
        self.assertGreater(diag["crm_db_bytes"], 0)
        # archive.db was never created in this test -> guarded, not an error
        self.assertEqual(diag["archive_db_bytes"], 0)
        self.assertEqual(diag["archived_messages"], 0)
        self.assertEqual(diag["contacts"], 1)
        self.assertEqual(diag["notes"], 1)
        self.assertEqual(diag["photos"], 1)
        self.assertEqual(diag["last_synced"].get("whatsapp"), "2026-07-01T10:00:00+00:00")

    def test_diagnostics_endpoint_rollups(self):
        db.create_contact(self.conn, "Diag Person")
        db.state_set(self.conn, "import_synced_whatsapp", "2026-07-01T10:00:00+00:00")
        db.state_set(self.conn, "import_synced_linkedin", "2026-07-03T10:00:00+00:00")
        self.conn.commit()

        diag = app.handle_diagnostics_get(self.conn)
        self.assertEqual(diag["contacts"], 1)
        # NC-124: disk_bytes is the WHOLE data folder now, not the two databases
        # in it — "on disk" used to answer with the two database files and leave
        # out the rest of the folder. It is no longer a function of crm_db_bytes and
        # archive_db_bytes at all, so this file no longer asserts a relation
        # between them; under this suite the test database deliberately lives
        # OUTSIDE the data folder, so the old equality could only be restored by
        # making the figure wrong again. The real claims are asserted against a
        # folder the test owns, in test_on_disk_means_the_whole_folder.py.
        self.assertIsInstance(diag["disk_bytes"], int)
        self.assertIsInstance(diag["disk_parts"], dict)
        # NC-094: the summary is the OLDEST collecting source, not the newest.
        sy = diag["sync_summary"]
        self.assertEqual(sy["oldest"], "2026-07-01T10:00:00+00:00")
        self.assertEqual(sy["oldest_source"], "whatsapp")
        self.assertEqual(sy["newest"], "2026-07-03T10:00:00+00:00")

    def test_diagnostics_endpoint_summary_empty_when_never_synced(self):
        sy = app.handle_diagnostics_get(self.conn)["sync_summary"]
        self.assertIsNone(sy["oldest"])
        self.assertIsNone(sy["oldest_source"])
        self.assertEqual(sy["collecting"], 0)

    def test_get_settings_route_wired(self):
        handler = app.CrmHandler
        self.assertTrue(hasattr(handler, "_route_get"))

    def test_identity_status_reports_unset_when_both_missing(self):
        """NC-054. setUp already popped every MANAGED_KEYS var, SELF_NAME and
        SELF_EMAIL included, so this is the fresh-install state."""
        out = ai.identity_status()
        self.assertFalse(out["ok"])
        self.assertEqual(sorted(out["missing"]), ["SELF_EMAIL", "SELF_NAME"])
        self.assertIn("the user", out["message"])       # owner.display_name()'s fallback
        self.assertIn("conversation", out["message"])   # the importer-side-matching claim

    def test_identity_status_reports_fine_when_both_set(self):
        os.environ["SELF_NAME"] = "Ada Lovelace"
        os.environ["SELF_EMAIL"] = "ada@example.com"
        self.assertEqual(ai.identity_status(), {"ok": True, "missing": []})

    def test_identity_status_names_only_the_missing_key(self):
        # Name set, email not: the message must not claim drafts are unsigned
        # when they aren't, and must still name the email-side gap.
        os.environ["SELF_NAME"] = "Ada Lovelace"
        out = ai.identity_status()
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing"], ["SELF_EMAIL"])
        self.assertNotIn("the user", out["message"])
        self.assertIn("conversation", out["message"])

        # Email set, name not: the reverse.
        os.environ.pop("SELF_NAME")
        os.environ["SELF_EMAIL"] = "ada@example.com"
        out2 = ai.identity_status()
        self.assertFalse(out2["ok"])
        self.assertEqual(out2["missing"], ["SELF_NAME"])
        self.assertIn("the user", out2["message"])

    def test_diagnostics_carries_identity_section(self):
        out = app.handle_diagnostics_get(self.conn)
        self.assertIn("identity", out)
        self.assertFalse(out["identity"]["ok"])
        os.environ["SELF_NAME"] = "Ada Lovelace"
        os.environ["SELF_EMAIL"] = "ada@example.com"
        out2 = app.handle_diagnostics_get(self.conn)
        self.assertTrue(out2["identity"]["ok"])
        self.assertEqual(out2["identity"]["missing"], [])

    def test_read_all_shape(self):
        rows = settings_mod.read_all()
        keys = {r["key"] for r in rows}
        self.assertEqual(keys, set(settings_mod.MANAGED_KEYS))
        for r in rows:
            self.assertIn("key", r)
            self.assertIn("value", r)
            self.assertIn("secret", r)
            self.assertIn("set", r)
        # ANTHROPIC_MODEL has a documented default when unset. It must stay one of
        # the values the settings picker offers (static/app/settings.js
        # MODEL_OPTIONS), or the select renders with nothing selected.
        model_row = self._by_key()["ANTHROPIC_MODEL"]
        self.assertEqual(model_row["value"], "claude-opus-5-5")
        self.assertFalse(model_row["set"])


class TestOneFreshSourceCannotHideStaleOnes(Base):
    """NC-094. The diagnostics panel used to answer "when did this last sync"
    with `max()` across all the sources, so one source that ran a minute ago made
    every out-of-date source read as fresh. The summary is the OLDEST collecting
    source, so a fresher one can never hide a stale one.
    """

    OLD = "2026-06-01T09:00:00+00:00"
    NEW = "2026-08-01T09:00:00+00:00"

    def _stamp(self, **pairs):
        for key, when in pairs.items():
            db.state_set(self.conn, f"import_synced_{key}", when)
        self.conn.commit()
        return app.handle_diagnostics_get(self.conn)["sync_summary"]

    def test_a_source_that_ran_today_does_not_hide_one_that_ran_in_June(self):
        """The bug itself. Both stamps exist; the summary must show June."""
        sy = self._stamp(gmail=self.OLD, imessage=self.NEW)
        self.assertEqual(sy["oldest"], self.OLD)
        self.assertEqual(sy["oldest_source"], "gmail")
        self.assertNotEqual(sy["oldest"], sy["newest"],
                            "the fixture must actually contain a disagreement, "
                            "or this test passes without proving anything")

    def test_the_summary_names_which_source_it_is_reporting(self):
        """A bare timestamp cannot be checked by the reader — the whole point of
        NC-087. The payload carries the key, a human label, and the population
        the figure was computed over."""
        sy = self._stamp(gmail=self.OLD, imessage=self.NEW)
        self.assertEqual(sy["oldest_source"], "gmail")
        self.assertTrue(sy["oldest_label"],
                        "the panel renders oldest_label, so it must be non-empty")
        self.assertIn("collected at least once", sy["population"])

    def test_an_enrichment_pass_does_not_count_as_a_collecting_source(self):
        """`style`, `facts`, `facts_llm`, `engagement` and `linkedin_facts` derive
        from data already here. A stale one means a derived field is old, not that
        data is missing, so it must not become the headline staleness figure."""
        sy = self._stamp(style=self.OLD, imessage=self.NEW)
        self.assertEqual(sy["oldest_source"], "imessage")
        self.assertEqual(sy["oldest"], self.NEW)
        self.assertEqual(sy["collecting"], 1)

    def test_a_source_that_has_never_run_is_not_counted_as_stale(self):
        """Never set up is a different state from out of date. If an unconnected
        source dragged the summary to "never", the figure would be useless on
        every install that has not connected all twelve — which is all of them."""
        sy = self._stamp(imessage=self.NEW)
        self.assertEqual(sy["oldest"], self.NEW)
        self.assertIn("email", sy["never"],
                      "the sources with no stamp are still reported, just not "
                      "folded into the staleness figure")
        self.assertNotIn("imessage", sy["never"])

    def test_every_never_entry_is_a_collecting_source(self):
        """`never` shares the summary's population, so an enrichment pass that
        has not run must not appear in it either."""
        import importers
        sy = self._stamp(imessage=self.NEW)
        for key in sy["never"]:
            self.assertEqual(importers.REGISTRY[key].category, "import",
                             f"{key} is not a collecting source")

    def test_the_old_max_based_field_is_gone_rather_than_redefined(self):
        """Keeping the name `last_sync` while changing it from newest to oldest
        would be exactly the one-field-two-definitions bug NC-087 is about. Any
        consumer still reading it should break loudly, not silently invert."""
        diag = app.handle_diagnostics_get(self.conn)
        self.assertNotIn("last_sync", diag)
        self.assertIn("last_synced", diag)   # the per-source dict stays

class TestUpdates(Base):
    """Update checker: version comparison, the daily
    cache, and offline/failure degradation — all with an injected fetcher and
    clock so no test ever touches the network or the wall clock."""

    def test_parse_version_and_is_newer(self):
        self.assertEqual(updates_mod.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updates_mod.parse_version("1.2.3-beta1"), (1, 2, 3))
        self.assertEqual(updates_mod.parse_version("1.4"), (1, 4))
        self.assertEqual(updates_mod.parse_version("garbage"), ())
        # strictly-greater only, length-insensitive
        self.assertTrue(updates_mod.is_newer("1.1.0", "1.0.0"))
        self.assertTrue(updates_mod.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(updates_mod.is_newer("2.0", "1.9.9"))
        self.assertFalse(updates_mod.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updates_mod.is_newer("1.0", "1.0.0"))     # 1.0 == 1.0.0
        self.assertFalse(updates_mod.is_newer("0.9.0", "1.0.0"))
        self.assertFalse(updates_mod.is_newer("", "1.0.0"))        # unparseable is never newer
        self.assertFalse(updates_mod.is_newer("nope", "1.0.0"))

    def test_check_reports_update_available_and_caches(self):
        calls = {"n": 0}

        def fetcher():
            calls["n"] += 1
            return {"version": "9.9.9", "url": "https://example.test/rel/9.9.9"}

        now = datetime.datetime(2026, 7, 25, 12, 0, 0, tzinfo=datetime.timezone.utc)
        out = updates_mod.check(self.conn, fetcher=fetcher, now=now)
        self.assertEqual(out["current"], updates_mod.CURRENT_VERSION)
        self.assertEqual(out["latest"], "9.9.9")
        self.assertTrue(out["update_available"])
        self.assertEqual(out["url"], "https://example.test/rel/9.9.9")
        self.assertFalse(out["stale"])
        self.assertEqual(calls["n"], 1)

        # a second call minutes later is served from cache — no new fetch
        soon = now + datetime.timedelta(minutes=5)
        cached = updates_mod.check(self.conn, fetcher=fetcher, now=soon)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["latest"], "9.9.9")
        self.assertTrue(cached["update_available"])

        # past the interval, it refetches
        later = now + datetime.timedelta(seconds=updates_mod.CHECK_INTERVAL_SECONDS + 1)
        updates_mod.check(self.conn, fetcher=fetcher, now=later)
        self.assertEqual(calls["n"], 2)

    def test_check_force_bypasses_cache(self):
        calls = {"n": 0}

        def fetcher():
            calls["n"] += 1
            return {"version": "1.0.0", "url": "u"}

        now = datetime.datetime(2026, 7, 25, 12, 0, 0, tzinfo=datetime.timezone.utc)
        updates_mod.check(self.conn, fetcher=fetcher, now=now)
        updates_mod.check(self.conn, force=True, fetcher=fetcher, now=now)
        self.assertEqual(calls["n"], 2)

    def test_same_version_is_not_an_update(self):
        out = updates_mod.check(
            self.conn,
            fetcher=lambda: {"version": updates_mod.CURRENT_VERSION, "url": "u"},
            now=datetime.datetime(2026, 7, 25, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(out["latest"], updates_mod.CURRENT_VERSION)
        self.assertFalse(out["update_available"])

    def test_offline_degrades_and_retries_next_call(self):
        # first ever check, fetch fails -> no crash, nothing cached, still due
        out = updates_mod.check(
            self.conn, fetcher=lambda: None,
            now=datetime.datetime(2026, 7, 25, 12, 0, 0, tzinfo=datetime.timezone.utc),
        )
        self.assertIsNone(out["latest"])
        self.assertFalse(out["update_available"])
        self.assertTrue(out["stale"])
        self.assertIsNone(out["checked_at"])
        # because the failure wasn't stamped, the very next call retries
        calls = {"n": 0}

        def ok():
            calls["n"] += 1
            return {"version": "2.0.0", "url": "u"}

        out2 = updates_mod.check(
            self.conn, fetcher=ok,
            now=datetime.datetime(2026, 7, 25, 12, 1, 0, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(out2["latest"], "2.0.0")

    def test_stale_cache_survives_a_later_offline_check(self):
        now = datetime.datetime(2026, 7, 25, 12, 0, 0, tzinfo=datetime.timezone.utc)
        updates_mod.check(self.conn, fetcher=lambda: {"version": "3.0.0", "url": "u"}, now=now)
        # a day later the network is down: we keep the last good answer
        later = now + datetime.timedelta(seconds=updates_mod.CHECK_INTERVAL_SECONDS + 1)
        out = updates_mod.check(self.conn, fetcher=lambda: None, now=later)
        self.assertEqual(out["latest"], "3.0.0")
        self.assertTrue(out["stale"])

    def test_route_wired(self):
        self.assertTrue(hasattr(app.CrmHandler, "_route_get"))
        # seed a fresh cached answer so the handler serves it WITHOUT any network
        db.state_set(self.conn, updates_mod._STATE_LAST_CHECK,
                     datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        db.state_set(self.conn, updates_mod._STATE_LATEST, updates_mod.CURRENT_VERSION)
        self.conn.commit()
        # A repo must be patched in: with the shipped empty default (NC-020) the
        # handler short-circuits before it ever looks at the cache, so without
        # this the assertions below would pass without testing the cache at all.
        with mock.patch.object(updates_mod, "GITHUB_REPO", "owner/name"):
            out = app.handle_update_get(self.conn, {"force": ["0"]})
        self.assertIn("current", out)
        self.assertIn("update_available", out)
        self.assertFalse(out["update_available"])
        self.assertTrue(out["configured"])
        self.assertEqual(out["latest"], updates_mod.CURRENT_VERSION)


class TestAnInertUpdateCheckSaysSoInsteadOfCheckingForever(Base):
    """NC-020. The checker used to default to a repo with no releases, so
    every install fired one GET a day at api.github.com that could only 404 —
    forever, invisibly, from an app whose whole pitch is that it works offline.
    The fix is not to make the 404 quieter: it is to not ask when there is
    nowhere to ask, and to say that in the UI rather than showing a check that
    never succeeds. These tests are what stops a repo name being hardcoded back
    in, which is the only way this regresses."""

    def test_no_repo_is_shipped_hardcoded(self):
        # The environment of a test run is not supposed to set this. If it does,
        # this assertion is the wrong thing to relax — unset it in the shell.
        self.assertEqual(os.environ.get("NC_UPDATE_REPO", ""), "",
                         "NC_UPDATE_REPO is set in this environment; the gate below "
                         "cannot see the shipped default")
        self.assertEqual(updates_mod.GITHUB_REPO, "")
        src = io.open(os.path.join(os.path.dirname(updates_mod.__file__),
                                  "updates.py"), encoding="utf-8").read()
        self.assertIn('os.environ.get("NC_UPDATE_REPO", "")', src)

    def test_an_unconfigured_check_opens_no_socket_and_writes_no_state(self):
        def explode():  # pragma: no cover - proving it is never called
            raise AssertionError("a fetch was attempted with no repo configured")

        with mock.patch.object(updates_mod, "_fetch_latest", lambda *a, **k: explode()):
            out = updates_mod.check(self.conn, repo="")
            forced = updates_mod.check(self.conn, repo="", force=True)

        for got in (out, forced):
            self.assertFalse(got["configured"])
            self.assertIsNone(got["latest"])
            self.assertFalse(got["update_available"])
            self.assertIsNone(got["checked_at"])
            self.assertFalse(got["stale"])       # nothing is stale; nothing exists
            self.assertEqual(got["url"], "")     # no link to a repo that 404s
            self.assertEqual(got["current"], updates_mod.CURRENT_VERSION)

        # and it left no trace in `state`, so switching a repo on later starts clean
        for key in (updates_mod._STATE_LAST_CHECK, updates_mod._STATE_LATEST,
                    updates_mod._STATE_URL):
            self.assertEqual(db.state_get(self.conn, key, ""), "")

    def test_the_shipped_default_path_is_the_unconfigured_one(self):
        # Belt and braces on the one that matters: the real no-argument call, as
        # the route makes it, must not reach the network.
        with mock.patch.object(updates_mod, "_fetch_latest",
                               lambda *a, **k: self.fail("hit the network")):
            out = updates_mod.check(self.conn)
        self.assertFalse(out["configured"])

    def test_naming_a_repo_switches_the_whole_path_back_on(self):
        now = datetime.datetime(2026, 8, 21, 9, 0, 0, tzinfo=datetime.timezone.utc)
        seen = {}

        def fake_fetch(repo, timeout=6.0):
            seen["repo"] = repo
            return {"version": "9.9.9", "url": "https://example.test/r"}

        with mock.patch.object(updates_mod, "GITHUB_REPO", "owner/name"), \
                mock.patch.object(updates_mod, "_fetch_latest", fake_fetch):
            out = updates_mod.check(self.conn, now=now)
        self.assertEqual(seen["repo"], "owner/name")   # not a stale module constant
        self.assertTrue(out["configured"])
        self.assertTrue(out["update_available"])
        self.assertEqual(out["latest"], "9.9.9")

    def test_an_injected_fetcher_counts_as_configured(self):
        # Every other test in TestUpdates passes only a fetcher. The fetcher IS
        # the source, so it must imply configured — otherwise this fix would have
        # silently turned those tests into assertions about the empty shape.
        out = updates_mod.check(
            self.conn, fetcher=lambda: {"version": "5.0.0", "url": "u"},
            now=datetime.datetime(2026, 8, 21, tzinfo=datetime.timezone.utc),
        )
        self.assertTrue(out["configured"])
        self.assertEqual(out["latest"], "5.0.0")

    def test_the_page_says_it_is_not_checking_and_draws_no_button(self):
        # NC-236: the old footer's `info.configured === false` branch and its
        # canCheck gate went with the old Settings page. The rebuilt page draws
        # no update control at all and prints one sentence the server decides,
        # so what is pinned is the sentence and the absence of the button. What
        # the decision does is run in
        # tests/test_no_update_control_appears_before_its_answer.py.
        import api_abilities
        js = io.open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(updates_mod.__file__))), "static", "app",
            "settings.js"), encoding="utf-8").read()
        self.assertIn("inst.updates_says", js)
        self.assertNotIn("Check now", js,
                         "a button that cannot work is the defect NC-006 fixed "
                         "on the Data page, and it is the defect here too")
        with mock.patch.object(updates_mod, "GITHUB_REPO", ""):
            says = api_abilities._install(self.conn)["updates_says"]
        self.assertEqual(says, "This copy does not check for updates.")


class TestOnboarding(Base):
    """First-run onboarding wizard: flag round-trip, grandfathering of
    existing installs, and the "run setup again" clear path."""

    def test_flag_unset_and_few_contacts_is_not_done(self):
        db.create_contact(self.conn, "A")
        db.create_contact(self.conn, "B")
        db.create_contact(self.conn, "C")
        self.conn.commit()
        self.assertEqual(app.handle_onboarding_get(self.conn), {"done": False})
        # unset + small network must NOT persist a flag either way
        self.assertEqual(db.state_get(self.conn, app.ONBOARDED_STATE_KEY, ""), "")

    def test_grandfathers_existing_network_without_writing(self):
        for i in range(25):
            db.create_contact(self.conn, f"Person {i}")
        self.conn.commit()
        out = app.handle_onboarding_get(self.conn)
        self.assertEqual(out, {"done": True})
        # NC-342: a read changes nothing. It used to store the flag here, so
        # asking the question (a prefetch, a second window) answered it.
        self.assertEqual(db.state_get(self.conn, app.ONBOARDED_STATE_KEY, ""), "")
        self.assertEqual(app.handle_onboarding_get(self.conn), {"done": True})

    def test_post_sets_and_clears_flag(self):
        out = app.handle_onboarding_post(self.conn, {"done": True})
        self.assertEqual(out, {"done": True})
        self.assertEqual(app.handle_onboarding_get(self.conn), {"done": True})

        out = app.handle_onboarding_post(self.conn, {"done": False})
        self.assertEqual(out, {"done": False})
        self.assertEqual(app.handle_onboarding_get(self.conn), {"done": False})

    def test_explicit_flag_wins_over_contact_count(self):
        for i in range(25):
            db.create_contact(self.conn, f"Person {i}")
        self.conn.commit()
        app.handle_onboarding_post(self.conn, {"done": False})
        # even with >20 contacts, an explicit done=False must stick
        self.assertEqual(app.handle_onboarding_get(self.conn), {"done": False})

    def test_routes_wired(self):
        handler = app.CrmHandler
        self.assertTrue(hasattr(handler, "_route_get"))
        self.assertTrue(hasattr(handler, "_route_write"))


class TestManagedKeysTakeEffectLive(unittest.TestCase):
    """NC_IMPORT_TIMEOUT_SEC and NC_CHAT_MAX_CARDS were in settings.MANAGED_KEYS
    (NC-003 removed both — see crm/settings.py's NC-003 comment — as pure
    tuning plumbing with no Settings field left to write them from), and even
    while they were, POST /api/settings writing them straight into os.environ
    was only half the fix: both used to also be captured into a module
    constant at import, which meant changing them did nothing until the app
    restarted — a setting that lies. These assert the read-at-call-time
    behaviour survives NC-003 unchanged: every one of these is still a plain
    `os.environ.get(KEY, default)` read in its owning module, independent of
    whether a Settings field still exists for it."""

    KEYS = ("NC_IMPORT_TIMEOUT_SEC", "NC_CHAT_MAX_CARDS",
            "NC_CHAT_TOOL_ROUNDS", "NC_CHAT_MAX_TOKENS",
            "NC_CHAT_SEARCH_LIMIT", "NC_CHAT_SEMANTIC_LIMIT")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_import_timeout_follows_the_env_without_restart(self):
        import api_import
        os.environ.pop("NC_IMPORT_TIMEOUT_SEC", None)
        self.assertEqual(api_import.import_timeout_sec(), api_import.IMPORT_TIMEOUT_DEFAULT)
        os.environ["NC_IMPORT_TIMEOUT_SEC"] = "900"
        self.assertEqual(api_import.import_timeout_sec(), 900)   # no reimport, no restart
        os.environ["NC_IMPORT_TIMEOUT_SEC"] = "not a number"
        self.assertEqual(api_import.import_timeout_sec(), api_import.IMPORT_TIMEOUT_DEFAULT)
        os.environ["NC_IMPORT_TIMEOUT_SEC"] = "99999"
        self.assertEqual(api_import.import_timeout_sec(), 3600)  # clamped
        os.environ["NC_IMPORT_TIMEOUT_SEC"] = "1"
        self.assertEqual(api_import.import_timeout_sec(), 10)    # clamped

    def test_chat_limits_follow_the_env_without_restart(self):
        """The four NC_CHAT_* tuning keys were the remaining offenders: while
        all four were still in MANAGED_KEYS (pre-NC-003), Settings rendered
        them and wrote them live, and chat.py captured every one into a module
        constant at import. Changing any of them did nothing until the app
        restarted. NC-003 removed the Settings fields; the live, no-restart
        read below is exactly what a hand-edit of secrets/.env still needs."""
        import chat
        cases = [
            ("NC_CHAT_TOOL_ROUNDS", chat.max_tool_rounds, chat.MAX_TOOL_ROUNDS_DEFAULT, "20", 20, "0", 1),
            ("NC_CHAT_MAX_TOKENS", chat.max_tokens, chat.MAX_TOKENS_DEFAULT, "4096", 4096, "1", 256),
            ("NC_CHAT_SEARCH_LIMIT", chat.search_people_limit, chat.SEARCH_PEOPLE_LIMIT_DEFAULT, "40", 40, "0", 1),
            ("NC_CHAT_SEMANTIC_LIMIT", chat.semantic_limit, chat.SEMANTIC_LIMIT_DEFAULT, "10", 10, "0", 1),
        ]
        for key, fn, default, raw, expect, low_raw, low_expect in cases:
            with self.subTest(key=key):
                os.environ.pop(key, None)
                self.assertEqual(fn(), default)
                os.environ[key] = raw
                self.assertEqual(fn(), expect)          # live, no restart
                os.environ[key] = "not a number"
                self.assertEqual(fn(), default)         # a typo must not break Ask
                os.environ[key] = low_raw
                self.assertEqual(fn(), low_expect)      # clamped at the floor

    def test_the_four_chat_tuning_keys_left_managed_keys_with_nc_003(self):
        """The mirror image of the old assertion here (which required these
        four IN MANAGED_KEYS, so a call-time read was only half the contract).
        NC-003 changed the contract: a pure tuning number no longer needs
        MANAGED_KEYS membership at all, because there is no Settings field
        left to write it from — only os.environ.get(...) does, unconditionally.
        This guards the NC-003 decision from a silent regression (someone
        re-adding one of these to MANAGED_KEYS without also giving it back a
        Settings field would leave a managed-but-invisible key, exactly the
        shape NC-004 was about)."""
        import settings as settings_module
        for key in ("NC_CHAT_TOOL_ROUNDS", "NC_CHAT_MAX_TOKENS",
                    "NC_CHAT_SEARCH_LIMIT", "NC_CHAT_SEMANTIC_LIMIT"):
            self.assertNotIn(key, settings_module.MANAGED_KEYS)

    def test_chat_max_cards_follows_the_env_without_restart(self):
        import api_ask
        os.environ.pop("NC_CHAT_MAX_CARDS", None)
        self.assertEqual(api_ask.chat_max_cards(), api_ask.CHAT_MAX_CARDS_DEFAULT)
        os.environ["NC_CHAT_MAX_CARDS"] = "5"
        self.assertEqual(api_ask.chat_max_cards(), 5)
        os.environ["NC_CHAT_MAX_CARDS"] = "junk"
        self.assertEqual(api_ask.chat_max_cards(), api_ask.CHAT_MAX_CARDS_DEFAULT)
        os.environ["NC_CHAT_MAX_CARDS"] = "0"
        self.assertEqual(api_ask.chat_max_cards(), 1)            # clamped

    def test_settings_post_reaches_the_live_value(self):
        """The whole point: the Settings write path -> os.environ -> the reader.

        NC_CHAT_MAX_CARDS is not the example any more — NC-003 removed it from
        MANAGED_KEYS, so write_key() rightly refuses it now (see
        test_the_four_chat_tuning_keys_left_managed_keys_with_nc_003). Use
        NC_ASK_FALLBACK_CAP instead: NC-003 kept it registered — unrendered,
        but still a real write path — precisely because invariant 8 makes it
        the one number that must never go invisible again, so it is the
        correct key to prove this path still works end to end."""
        import api_ask
        env = os.path.join(tempfile.mkdtemp(prefix="live_set_"), ".env")
        with mock.patch.object(settings_mod, "env_path", lambda: env):
            settings_mod.write_key("NC_ASK_FALLBACK_CAP", "7")
        self.assertEqual(api_ask.ask_fallback_cap(), 7)


def _repo_path(*parts):
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parts)


class TestIdentityWarningNC054(Base):
    """NC-054. `identity_status()` (crm/api_instance.py) has a signal; these
    prove the Settings page surfaces it. NC-236: the old page did it with a
    top-of-page notice and a gold "not set" line on the name and email fields;
    the rebuilt page puts it in the "Needs you" list at the top, built by the
    server from the same signal, and the name and email boxes are on the
    identity row below. The two separate warning styles (.identity-warn,
    .field-unset-warn) went with the old page."""

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.pop(k, None) for k in ("SELF_NAME", "SELF_EMAIL")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        super().tearDown()

    def _ledger(self):
        import api_abilities
        return api_abilities.handle_settings_ledger(self.conn)

    def test_an_unset_identity_is_the_first_thing_the_page_asks_for(self):
        needs = self._ledger()["needs"]
        self.assertTrue(needs, "name and email are unset and nothing asks for them")
        self.assertEqual(needs[0]["row"], "identity")
        self.assertEqual(needs[0]["says"], ai.identity_status()["message"])

    def test_the_need_takes_you_to_the_boxes(self):
        need = self._ledger()["needs"][0]
        self.assertIn({"kind": "row", "row": "identity", "label": "Set your name and email"},
                      need["acts"])

    def test_the_identity_row_carries_exactly_the_name_and_email_boxes(self):
        row = next(r for r in self._ledger()["rows"] if r["key"] == "identity")
        self.assertEqual([a["key"] for a in row["detail"]["acts"]],
                         ["SELF_NAME", "SELF_EMAIL"])

    def test_setting_both_clears_the_need(self):
        os.environ["SELF_NAME"] = "Ada Lovelace"
        os.environ["SELF_EMAIL"] = "ada@example.com"
        self.assertNotIn("identity", [n["row"] for n in self._ledger()["needs"]])

    def test_one_missing_key_still_asks_and_the_row_says_not_set(self):
        os.environ["SELF_NAME"] = "Ada Lovelace"
        out = self._ledger()
        self.assertIn("identity", [n["row"] for n in out["needs"]])
        row = next(r for r in out["rows"] if r["key"] == "identity")
        self.assertEqual(row["state"]["says"], "Not set")

    def test_the_page_draws_the_needs_above_the_abilities(self):
        with open(_repo_path("static", "app", "settings.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertLess(js.index('id="lg-needs"'), js.index('id="lg-abilities"'),
                        "a broken name or email must be read before the list of abilities")
        self.assertEqual(js.count("<${Section} id=\"lg-needs\""), 1)


class _FakePopen:
    """Stand-in for subprocess.Popen: yields canned pip lines, then a returncode.
    Lets the install worker run to completion with no network and no real pip."""

    def __init__(self, lines, returncode):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.killed = False

    def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


class TestSemanticSearch(Base):
    """Opt-in semantic-search installer: honest status, sys.executable targeting,
    no-second-concurrent-run, and failure surfacing. The subprocess boundary is
    always patched — no test hits the network or actually pip-installs anything."""

    _IDLE = {
        "state": "idle", "running": False, "started_at": "", "finished_at": "",
        "returncode": None, "message": "", "tail": [], "restart_required": False,
    }

    def setUp(self):
        super().setUp()
        # _semantic_state is a module global; snapshot and reset to idle so tests
        # don't leak install state into one another.
        self._orig_state = {k: (list(v) if isinstance(v, list) else v)
                            for k, v in ai._semantic_state.items()}
        ai._semantic_state.clear()
        ai._semantic_state.update(self._IDLE)
        ai._semantic_state["tail"] = []
        self._orig_timeout = os.environ.pop("NC_SEMANTIC_INSTALL_TIMEOUT_SEC", None)

    def tearDown(self):
        ai._semantic_state.clear()
        ai._semantic_state.update(self._orig_state)
        if self._orig_timeout is None:
            os.environ.pop("NC_SEMANTIC_INSTALL_TIMEOUT_SEC", None)
        else:
            os.environ["NC_SEMANTIC_INSTALL_TIMEOUT_SEC"] = self._orig_timeout
        super().tearDown()

    def test_status_when_unavailable_reports_why(self):
        with mock.patch.object(ai.search_cli, "embeddings_installed", lambda: False), \
                mock.patch.object(ai, "_semantic_reason",
                                  lambda: "not installed (numpy missing)"):
            st = ai.semantic_search_status()
        self.assertFalse(st["available"])
        self.assertEqual(st["packages"], ["sentence-transformers", "numpy"])
        self.assertEqual(st["reason"], "not installed (numpy missing)")
        self.assertEqual(st["size_gb"], 2)
        self.assertEqual(st["install"]["state"], "idle")

    def test_status_when_available_has_no_reason(self):
        with mock.patch.object(ai.search_cli, "embeddings_installed", lambda: True):
            st = ai.semantic_search_status()
        self.assertTrue(st["available"])
        self.assertEqual(st["reason"], "")
        self.assertIn("install", st)

    def test_diagnostics_carries_semantic_section(self):
        with mock.patch.object(ai.search_cli, "embeddings_installed", lambda: True):
            diag = app.handle_diagnostics_get(self.conn)
        self.assertIn("semantic_search", diag)
        self.assertTrue(diag["semantic_search"]["available"])
        self.assertEqual(diag["semantic_search"]["packages"],
                         ["sentence-transformers", "numpy"])

    def test_command_targets_sys_executable(self):
        import sys as _sys
        cmd = ai._semantic_install_cmd()
        self.assertEqual(cmd[0], _sys.executable)
        self.assertEqual(cmd[1:4], ["-m", "pip", "install"])

    def test_it_installs_only_pinned_fingerprinted_versions(self):
        """NC-316: never an unpinned "sentence-transformers numpy", which gave each
        person whatever was newest that day."""
        cmd = ai._semantic_install_cmd()
        self.assertIn("--require-hashes", cmd)
        self.assertIn("--only-binary=:all:", cmd)
        files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-r"]
        self.assertEqual([os.path.basename(f) for f in files],
                         ["requirements-lock.txt", "requirements-semantic.txt"])
        for f in files:
            self.assertTrue(os.path.isfile(f), f)
        self.assertNotIn("sentence-transformers", cmd, "an unpinned name on the command line")
        with open(files[1]) as fh:
            text = fh.read()
        names = [line.split("==")[0] for line in text.split("\n")
                 if line and not line.startswith((" ", "#"))]
        for package in ai.SEMANTIC_PACKAGES:
            self.assertIn(package, names)
        for line in text.split("\n"):
            if line and not line.startswith((" ", "#")):
                self.assertRegex(line, r"^[a-z0-9-]+==[0-9][0-9a-z.]* \\$")

    def test_install_refuses_second_concurrent_run(self):
        ai._semantic_state["running"] = True
        ai._semantic_state["state"] = "running"
        popen_calls = []

        def _never(*a, **k):
            popen_calls.append(a)
            return _FakePopen([], 0)

        with mock.patch.object(ai.search_cli, "embeddings_available", lambda: False), \
                mock.patch.object(ai.subprocess, "Popen", _never):
            out = ai.handle_semantic_install({})
        self.assertFalse(out["started"])
        self.assertIn("already running", out["message"])
        self.assertEqual(popen_calls, [])  # no second pip was launched

    def test_install_noops_when_already_available(self):
        with mock.patch.object(ai.search_cli, "embeddings_available", lambda: True), \
                mock.patch.object(ai.subprocess, "Popen",
                                  lambda *a, **k: _FakePopen([], 0)):
            out = ai.handle_semantic_install({})
        self.assertFalse(out["started"])
        self.assertIn("already installed", out["message"])
        self.assertTrue(out["status"]["available"])

    def test_failure_surfaces_output_tail(self):
        lines = ["Collecting numpy\n",
                 "ERROR: Could not find a version that satisfies the requirement\n"]
        with mock.patch.object(ai.subprocess, "Popen",
                               lambda *a, **k: _FakePopen(lines, 1)), \
                mock.patch.object(ai.search_cli, "embeddings_available", lambda: False):
            ai._run_semantic_install()
        state = ai._semantic_state
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["returncode"], 1)
        self.assertFalse(state["running"])
        self.assertIn("ERROR: Could not find a version that satisfies the requirement",
                      state["tail"])
        self.assertIn("code 1", state["message"])

    def test_could_not_start_pip_is_a_clean_failure(self):
        def _boom(*a, **k):
            raise FileNotFoundError("no pip here")

        with mock.patch.object(ai.subprocess, "Popen", _boom):
            ai._run_semantic_install()
        self.assertEqual(ai._semantic_state["state"], "failed")
        self.assertIn("could not start pip", ai._semantic_state["message"])

    def test_success_no_restart_when_import_takes_effect(self):
        lines = ["Collecting sentence-transformers\n", "Successfully installed\n"]
        with mock.patch.object(ai.subprocess, "Popen",
                               lambda *a, **k: _FakePopen(lines, 0)), \
                mock.patch.object(ai.search_cli, "embeddings_available", lambda: True):
            ai._run_semantic_install()
        self.assertEqual(ai._semantic_state["state"], "succeeded")
        self.assertFalse(ai._semantic_state["restart_required"])
        self.assertEqual(ai._semantic_state["returncode"], 0)

    def test_success_flags_restart_when_import_still_fails(self):
        lines = ["Successfully installed\n"]
        with mock.patch.object(ai.subprocess, "Popen",
                               lambda *a, **k: _FakePopen(lines, 0)), \
                mock.patch.object(ai.search_cli, "embeddings_available", lambda: False):
            ai._run_semantic_install()
        self.assertEqual(ai._semantic_state["state"], "succeeded")
        self.assertTrue(ai._semantic_state["restart_required"])
        self.assertIn("Restart", ai._semantic_state["message"])


class TestTheSearchStatusDoesNotLoadPyTorch(unittest.TestCase):
    """NC-221. Asking whether search by meaning is installed must not import it:
    loading PyTorch takes seconds and hundreds of megabytes the first time
    Settings opens. Run in a fresh interpreter, so nothing this suite imported
    earlier hides the import."""

    def test_the_status_imports_neither_package(self):
        """Stand-in packages that say so when imported, first on the path, so the
        test does not depend on whether the machine has PyTorch."""
        import subprocess
        crm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm")
        fake = tempfile.mkdtemp()
        for name in ("numpy", "sentence_transformers"):
            os.makedirs(os.path.join(fake, name))
            with open(os.path.join(fake, name, "__init__.py"), "w") as fh:
                fh.write("import sys; sys.stderr.write('IMPORTED %s\\n')\n" % name)
        probe = ("import sys; sys.path[:0] = [%r, %r]; import api_instance; "
                 "print(api_instance.semantic_search_status()['available'])" % (fake, crm))
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             timeout=120, env={**os.environ, "CRM_DATA_DIR": tempfile.mkdtemp()})
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(out.stdout.strip().splitlines()[-1], "True", "it still sees them installed")
        self.assertNotIn("IMPORTED", out.stderr)

    def test_a_search_still_checks_by_importing(self):
        """So a broken install still falls back to keyword search."""
        import inspect
        self.assertIn("if not embeddings_available():",
                      inspect.getsource(ai.search_cli.semantic_search))
