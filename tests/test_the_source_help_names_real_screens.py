"""NC-291. The source registry's help text names screens and fields that exist.

`crm/importers.py` calls itself the single source of truth for import sources,
and its `export_help` strings were written for the old Data page: paste the
calendar link "into `NC_ICS_GOOGLE_URL` under Settings", turn on "Allow
downloading calendars", look "under Found on this Mac", use "Enrich". Only the
Contacts and iMessage help is on screen today (the first-run steps), but the rest
is served by the API and read by anyone working from the code, so it is held to
the names the app shows.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import importers  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestTheSourceHelpNamesRealScreens(unittest.TestCase):

    def test_no_help_names_a_screen_that_is_gone(self):
        for key, spec in importers.REGISTRY.items():
            for gone in ("Found on this Mac", "under Enrich", "exported by hand",
                         "Allow downloading calendars", "`NC_ICS_"):
                with self.subTest(source=key, gone=gone):
                    self.assertNotIn(gone, spec.export_help or "")

    def test_every_settings_name_it_uses_is_on_the_settings_page(self):
        settings = _read("crm", "api_abilities.py")
        named = 0
        for key, spec in importers.REGISTRY.items():
            for name in re.findall(r"\*\*([^*]+)\*\*\)? in Settings", spec.export_help or ""):
                named += 1
                with self.subTest(source=key, name=name):
                    self.assertIn(f'"{name}"', settings)
        self.assertGreaterEqual(named, 4)

    def test_it_asks_for_fields_by_their_label_not_their_setting_name(self):
        for key, spec in importers.REGISTRY.items():
            with self.subTest(source=key):
                self.assertEqual(re.findall(r"`[A-Z][A-Z_]{3,}`", spec.export_help or ""), [])


if __name__ == "__main__":
    unittest.main()
