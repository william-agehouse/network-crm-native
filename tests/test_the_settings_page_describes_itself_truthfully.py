"""NC-121. The Settings page told people not to do the only thing that works.

Its opening line was *"Every change here writes `secrets/.env` on this Mac. Do
not edit that file by hand."* That was true when the page held every setting. It
has been false since [[NC-003]] moved the tuning numbers out of the rendered UI
and left them in the registry: **17 of the 32 keys the server managed had no box
anywhere on this page**, and editing the file was the only way to set them. Among them a real spend ceiling (`ENRICH_LLM_MAX`) and a
secret (`LINKEDIN_CLIENT_SECRET`).

Telling somebody not to do the only thing that works is worse than saying
nothing, because they stop looking.

**Why this is a test and not just a better sentence.** The old line was accurate
when it was written and rotted without anyone touching it — a key moving out of
the UI makes it wronger, and nothing connected the two. The same is true of the
replacement. So the gate is on the *relationship*: while keys exist with no
field, the page may not claim the file is off limits, and it has to point at the
list of what those keys do.

The two counts in the source comments rotted the same way and are pinned here
too, because both were the registry described by a number read off the screen.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import app  # noqa: E402,F401
import settings as settings_mod  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(REPO, "static", "app", "settings.js")


def source():
    with open(JS, encoding="utf-8") as fh:
        return fh.read()


def server_code(name):
    """A server file with its comment lines dropped: what the pages are drawn from.

    NC-236: the old Settings page held its fields in JavaScript. The rebuilt
    Settings and Data pages draw every box from what crm/api_abilities.py and
    crm/api_ledger.py send, so a key with a box is a key named in those files.
    A key named only in a comment is not a mention, for the same reason as before."""
    with open(os.path.join(REPO, "crm", name), encoding="utf-8") as fh:
        return "\n".join(ln for ln in fh.read().splitlines()
                         if not ln.lstrip().startswith("#"))


def rendered_keys():
    """Every managed key that has a box on the Settings page or the Data page."""
    text = server_code("api_abilities.py") + "\n" + server_code("api_ledger.py")
    return {k for k in settings_mod.MANAGED_KEYS if re.search(r'"%s"' % k, text)}


def lede(js):
    """The paragraph under the title — the one sentence everybody reads."""
    m = re.search(r'<p class="data-sub">(.*?)</p>', js, re.S)
    assert m, "the Settings lede has moved"
    return " ".join(m.group(1).split())


class TestTheLedeMatchesWhatThePageActuallyHolds(unittest.TestCase):

    def setUp(self):
        self.js = source()
        self.managed = set(settings_mod.MANAGED_KEYS)
        self.on_screen = rendered_keys()
        self.file_only = self.managed - self.on_screen

    def test_there_really_are_settings_with_no_box(self):
        """The premise. If this ever goes to zero the test below can be dropped."""
        self.assertGreater(
            len(self.file_only), 0,
            "every managed key now has a field, so the page could honestly say "
            "the file never needs hand-editing again")

    def test_the_page_does_not_forbid_the_only_thing_that_works(self):
        low = lede(self.js).lower()
        for forbidding in ("do not edit that file", "never edit that file",
                           "should not edit", "no need to edit"):
            self.assertNotIn(
                forbidding, low,
                f"{len(self.file_only)} managed keys have no box on this page — "
                f"{', '.join(sorted(self.file_only)[:3])}… — so editing the file "
                f"is the only way to set them, and the page forbids it")

    # NC-236: "the page says where the rest of the settings are" (naming
    # crm/settings.py in the lede) went with the old lede. The rebuilt page is
    # framed around what the app can do, not around a settings file, so the old
    # lede is gone on purpose; the caveat it carried was for developers.

    def test_the_secret_with_nowhere_to_go_says_so_somewhere(self):
        """A secret the server manages must have a box on a page, or be named
        where somebody holding one would look. LINKEDIN_CLIENT_SECRET is the
        one registered secret with neither."""
        secret_keys = {k for k, v in settings_mod.MANAGED_KEYS.items()
                       if v.get("secret")}
        homeless = sorted(secret_keys - self.on_screen)
        if not homeless:
            self.skipTest("every registered secret has a field now")
        for key in homeless:
            with self.subTest(key=key):
                self.assertIn(
                    key, server_code("api_ledger.py") + server_code("api_abilities.py"),
                    f"{key} is a secret the server manages with no field and no "
                    f"mention anywhere on the page. Somebody holding one has "
                    f"nowhere to put it and nothing telling them so")


class TestTheCommentsDoNotMiscountTheRegistry(unittest.TestCase):
    # NC-236: the two comments that miscounted the registry ("this is four
    # buttons" on the Remove button, and the note that there is no disclosure
    # left) were in the old Settings page's code and are gone with it. What
    # remains is that the new page does not open by describing itself as a
    # settings file.

    def test_the_page_does_not_open_by_describing_a_settings_file(self):
        head = source()[:source().index("import ")]
        self.assertNotIn("there is no disclosure left to describe", head)
        self.assertNotIn("Do not edit that file by hand", head)


if __name__ == "__main__":
    unittest.main()
