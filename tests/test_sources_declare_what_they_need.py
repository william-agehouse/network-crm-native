"""NC-144 / NC-116: a source that needs a setting has to say so.

THE BUG. Asked to run the Gmail import with no account set, it printed "No Gmail
accounts configured" and exited 0 — while the newest email in the archive could
be stale. `GMAIL_ACCOUNTS` could stay empty indefinitely and nothing
anywhere said so. The Data page showed Gmail as a working source with a date
beside it, and the date was perfectly real: it was the last time it worked, not
evidence that it still could.

`list_sources()` computes

    "configured": (not spec.needs or bool(os.environ.get(spec.needs, "").strip()))

so a spec that declares no `needs` is configured *unconditionally*. Exactly one
of the seventeen specs declared one (`linkedin_facts`, `needs="IMAC"`). One out
of seventeen is not a convention, it is an accident — and it had already cost a
second bug, NC-116, where "Fill facts with AI (Claude)" offered a live Run
button on an install with no Anthropic key.

The knowledge was not even missing. `CALENDAR_URL_SOURCES` in importers.py knew
the two calendar links needed their URLs, `api_import.py` declared `needs` for
them in a different payload, and `api_sources.py` had friendly labels for both.
Three files knew; the registry did not, and the registry is what the Data page
renders.

WHAT THESE GUARD

  * every source in the registry is classified — a new one fails this file
    until someone decides whether it needs a setting, which is the only way the
    next Gmail cannot happen quietly
  * a declared key is a REAL settings key, so a typo cannot leave a source
    permanently and invisibly unconfigured
  * the behaviour, not the declaration: with the setting empty the source
    reports unconfigured, and setting it flips without a restart
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import importers                       # noqa: E402
import settings                        # noqa: E402


# Every source, and the one setting it cannot run without. This table is the
# point of the file: it has to name EVERY key in the registry, so adding a
# source without deciding this fails the suite rather than defaulting to the
# silent yes.
#
# None means "there is genuinely nothing to configure" — a file the user picks,
# or a pass that runs entirely offline. That is a real answer and a different
# one from "nobody thought about it", which is what an undeclared `needs` used
# to look like.
EXPECTED = {
    # picked by hand, or read from a place that needs no setting
    "contacts":         None,
    "linkedin":         None,
    "imessage":         None,
    "whatsapp":         None,
    "whatsapp_photos":  None,
    "email":            None,
    "calendar_local":   None,
    "calendar":         None,
    # reads a private URL the user pastes into Settings
    "calendar_google":  "NC_ICS_GOOGLE_URL",
    "calendar_icloud":  "NC_ICS_ICLOUD_URL",
    # Google OAuth, sharing the Gmail import's clients
    "gmail":            "GMAIL_ACCOUNTS",
    "google_photos":    "GMAIL_ACCOUNTS",
    # offline enrichment
    "engagement":       None,
    "facts":            None,
    "style":            None,
    # costs money / runs on a separate scraper Mac (an optional setup)
    "facts_llm":        "ANTHROPIC_API_KEY",
    "linkedin_facts":   "IMAC",
}


class TestEverySourceIsClassified(unittest.TestCase):

    def test_the_table_covers_the_registry_exactly(self):
        """The gate that makes this file worth having. Add a source and this
        fails until you say whether it needs a setting."""
        registry = set(importers.REGISTRY)
        table = set(EXPECTED)
        self.assertEqual(
            registry - table, set(),
            f"new source(s) {sorted(registry - table)} are not classified — add "
            "them to EXPECTED with the settings key they need, or None if there "
            "is genuinely nothing to configure")
        self.assertEqual(
            table - registry, set(),
            f"EXPECTED names {sorted(table - registry)}, which no longer exist")

    def test_each_spec_declares_what_the_table_says(self):
        wrong = {k: (importers.REGISTRY[k].needs or None, v)
                 for k, v in EXPECTED.items()
                 if (importers.REGISTRY[k].needs or None) != v}
        self.assertEqual(wrong, {},
                         "spec.needs disagrees with the table (declared, expected): "
                         f"{wrong}")

    def test_a_declared_key_is_one_settings_can_actually_write(self):
        """A typo here is invisible and permanent: the source reports
        unconfigured forever, and no field on Settings can ever satisfy it."""
        for key, spec in sorted(importers.REGISTRY.items()):
            if spec.needs:
                self.assertIn(
                    spec.needs, settings.MANAGED_KEYS,
                    f"{key} needs {spec.needs!r}, which is not a managed setting, "
                    "so nothing in the app can ever set it")


class TestItIsTheBehaviourThatMatters(unittest.TestCase):
    """Reading spec.needs proves the declaration. These prove the consequence —
    which is the half that reaches the screen."""

    def _row(self, key):
        return next(s for s in importers.list_sources() if s["key"] == key)

    def test_an_empty_setting_reports_unconfigured(self):
        needy = [k for k, v in EXPECTED.items() if v]
        self.assertTrue(needy)
        for key in needy:
            with mock.patch.dict(os.environ, {EXPECTED[key]: ""}, clear=False):
                self.assertFalse(
                    self._row(key)["configured"],
                    f"{key} claims to be set up with {EXPECTED[key]} empty — this "
                    "is exactly how a dead Gmail passes for "
                    "quiet")

    def test_whitespace_is_not_a_value(self):
        with mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": "   "}, clear=False):
            self.assertFalse(self._row("gmail")["configured"])

    def test_setting_it_makes_the_source_live_without_a_restart(self):
        """`configured` is computed per call for this reason — the Settings page
        and the Data page are the same session."""
        with mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": ""}, clear=False):
            self.assertFalse(self._row("gmail")["configured"])
        with mock.patch.dict(os.environ,
                             {"GMAIL_ACCOUNTS": "a@b.example:c.json"}, clear=False):
            self.assertTrue(self._row("gmail")["configured"])

    def test_a_source_with_nothing_to_configure_stays_configured(self):
        """The counter-test. A gate that reports everything as broken is as
        useless as one that reports everything as fine."""
        for key, need in EXPECTED.items():
            if need is None:
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertTrue(
                        self._row(key)["configured"],
                        f"{key} needs no setting but reports unconfigured")

    def test_the_row_names_the_key_so_the_page_can_say_which(self):
        """"Not configured" without naming the setting is a dead end for whoever
        reads it."""
        for key, need in EXPECTED.items():
            self.assertEqual(self._row(key)["needs"], need or "")


if __name__ == "__main__":
    unittest.main()
