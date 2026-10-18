"""NC-185. Every setting the app reads is written down in crm/settings.py.

The Settings page says that crm/settings.py is the list of what each setting does
(NC-121). That only holds if the list is complete, and a key can be read without its
name appearing next to os.environ.get: key names kept in a table and passed in as a
variable are reads too.

So every key read in crm/ must be in one of two tables:

* settings.MANAGED_KEYS — what the Settings page may write;
* settings.UNMANAGED_KEYS — everything else, marked "plumbing" (decided, not a
  person's choice) or "tunable" (a limit someone can feel, set by hand in
  secrets/.env), with one sentence saying what it does.

This file finds the keys with a reader that also sees those indirect reads, so
the two cannot drift apart: a new os.environ.get("NC_SOMETHING") with no row fails here.
"""

import glob
import io
import os
import re
import sys
import tokenize
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401 — Base is the CRM_DATA_DIR bootstrap

import settings  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")

# A direct read: os.environ.get("X"), os.getenv("X"), os.environ["X"], "X" in
# os.environ, or any reader helper with "env" in its name (_env_int, _int_env,
# _envint, _bool_env, _float_env …). This is the one that sees settings whose
# names have no underscore.
DIRECT = re.compile(
    r'''(?:os\.environ\.get|os\.getenv|os\.environ\.setdefault|os\.environ\.pop'''
    r'''|environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']'''
    r'''|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]'''
    r'''|["']([A-Z][A-Z0-9_]*)["']\s+(?:not\s+)?in\s+os\.environ'''
    r'''|\b\w*env\w*\(\s*["']([A-Z][A-Z0-9_]*)["']''', re.I)
# An indirect read: a setting-shaped string anywhere in the code — how
# "NC_GMAIL_AUTO" in api_import.API_SOURCES or "NC_WA_AUTOIMPORT" in
# auto_import_paths() reach os.environ.get(switch). This finds every indirect
# read in crm/ and exactly two strings that are not settings (NOT_SETTINGS).
SHAPED = re.compile(r'[rbuRBU]*(["\'])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\1')
NOT_SETTINGS = {
    "Z_PK": "a column of Apple's Contacts database (addressbook_import.py)",
    "API_SOURCES": "the name of a table, quoted in an error message (api_sources.py)",
}


def _code_files():
    yield from sorted(glob.glob(os.path.join(CRM, "**", "*.py"), recursive=True))


def keys_read():
    """{key: "file:line"} for every setting read anywhere in crm/."""
    found = {}
    for path in _code_files():
        rel = os.path.relpath(path, REPO)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for i, line in enumerate(src.split("\n"), 1):
            code = line.split("#")[0]
            for m in DIRECT.finditer(code):
                key = next(g for g in m.groups() if g)
                if key.isupper():
                    found.setdefault(key, f"{rel}:{i}")
        # settings.py: only its direct reads count (spend_cap reads ENRICH_LIMIT,
        # the old name of the spend cap). The registry names every key as a string,
        # and naming a key is not reading it.
        if os.path.basename(path) == "settings.py":
            continue
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING:
                m = SHAPED.fullmatch(tok.string)
                if m and m.group(2) not in NOT_SETTINGS:
                    found.setdefault(m.group(2), f"{rel}:{tok.start[0]}")
    return found


class TestEverySettingIsAccountedFor(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.read = keys_read()

    def test_the_reader_still_sees_the_indirect_reads(self):
        """The premise. If the extractor stops seeing a key that is only ever
        read through a table, every assertion below is weaker than it looks."""
        for key in ("NC_GMAIL_AUTO", "NC_WA_AUTOIMPORT", "NC_CALENDAR_EVERY_HOURS",
                    "IMAC", "NC_CHAT_MESSAGE_LIMIT", "SCRAPE_DAILY_MAX"):
            with self.subTest(key=key):
                self.assertIn(key, self.read)

    def test_every_key_read_is_in_one_of_the_two_tables(self):
        known = set(settings.MANAGED_KEYS) | set(settings.UNMANAGED_KEYS)
        missing = sorted(f"{k}  ({where})" for k, where in self.read.items()
                         if k not in known)
        self.assertEqual(missing, [], "\n".join([
            "", "These are read by the app and written down nowhere in",
            "crm/settings.py. Add each to UNMANAGED_KEYS as plumbing or tunable,",
            "with a sentence saying what it does:", *missing]))

    def test_no_key_is_in_both_tables(self):
        both = sorted(set(settings.MANAGED_KEYS) & set(settings.UNMANAGED_KEYS))
        self.assertEqual(both, [])

    def test_every_documented_key_is_still_read(self):
        """A row for a key nothing reads tells someone to set a value that does
        nothing — the lie this table exists to stop, in the other direction."""
        stale = sorted(k for k in settings.UNMANAGED_KEYS if k not in self.read)
        self.assertEqual(stale, [], f"documented but never read: {stale}")

    def test_every_managed_key_is_still_read(self):
        """NC-196. The same lie in the registry: a Settings field for a feature
        that is gone, which outlives the feature unless something checks."""
        stale = sorted(k for k in settings.MANAGED_KEYS if k not in self.read)
        self.assertEqual(stale, [], f"registered but never read: {stale}")

    def test_every_row_says_what_kind_and_what_it_does(self):
        for key, row in settings.UNMANAGED_KEYS.items():
            with self.subTest(key=key):
                self.assertIsInstance(row, tuple)
                kind, what = row
                self.assertIn(kind, ("plumbing", "tunable"))
                self.assertGreater(len(what.split()), 4,
                                   "a sentence, not a label")

    def test_the_money_pair_is_registered_and_checked(self):
        """Example: the prices the app uses to cost its own answers. NC-185 filed
        them as tunable; NC-195 registered them, with a validator, because a
        price that is not a number is silently ignored."""
        for key in ("NC_PRICE_IN_PER_MTOK", "NC_PRICE_OUT_PER_MTOK"):
            with self.subTest(key=key):
                self.assertIn(key, settings.MANAGED_KEYS)
                self.assertIn(key, settings.VALIDATORS)
                for bad in ("five", "-1", "5000"):
                    with self.assertRaises(ValueError):
                        settings.VALIDATORS[key](bad)
                settings.VALIDATORS[key]("3.75")

    def test_all_four_automatic_imports_can_be_switched_off_from_settings(self):
        """Apple Calendar's switch was missing from the registry (NC-185), so it
        was the only one of the four the Settings API refused to write."""
        for key in ("NC_IMSG_AUTOIMPORT", "NC_WA_AUTOIMPORT",
                    "NC_CONTACTS_AUTOIMPORT", "NC_CALENDAR_LOCAL_AUTOIMPORT"):
            with self.subTest(key=key):
                self.assertIn(key, settings.MANAGED_KEYS)


if __name__ == "__main__":
    unittest.main()
