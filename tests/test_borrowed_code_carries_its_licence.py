"""NC-244. What other people wrote travels with its licence.

The vendored Preact + htm file was one minified line with no notice at all, and
the Fraunces font came with a two-line note naming its licence but not its text.
MIT, Apache-2.0 and the SIL Open Font License each ask for their notice in every
copy, so publishing the repository or handing out the app without them breaks
all three. Cheap to fix, easy to lose again the next time something is vendored.
"""

import glob
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# SHA-256 of htm@3.1.1/preact/standalone.module.js as published on npm (unpkg).
# The vendored file is this plus one comment line at the top.
HTM_3_1_1_STANDALONE_SHA256 = "72284e8e9079c87817145df1110f74e8a2aa040b2fc384922e18dfcb46fc1fd7"
NOTICES = os.path.join(REPO, "THIRD_PARTY_NOTICES.md")
BUNDLE_APP = os.path.join(REPO, "packaging", "dist", "Network CRM.app",
                          "Contents", "Resources", "app")

# Each borrowed thing: its copyright line, and a sentence from deep inside its
# licence, so a notice that keeps the heading but loses the text still fails.
BORROWED = {
    "Preact": ("Copyright (c) 2015-present Jason Miller",
               "The above copyright notice and this permission notice shall be included"),
    "htm": ("Copyright 2018 Google Inc.",
            "9. Accepting Warranty or Additional Liability."),
    "Fraunces": ("Copyright 2020 The Fraunces Project Authors",
                 "5) The Font Software, modified or unmodified, in part or in whole,"),
    # NC-280. Seven icons are drawn after Feather's; the gear, phone and link are
    # its coordinates scaled to the 16-pixel grid.
    "Feather Icons": ("Copyright (c) 2013-2017 Cole Bemis",
                      "The above copyright notice and this permission notice shall be included"),
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _flat(text):
    return re.sub(r"\s+", " ", text)


class TestTheNoticesAreComplete(unittest.TestCase):

    def test_each_borrowed_thing_has_its_copyright_and_full_licence(self):
        text = _flat(_read(NOTICES))
        for name, (copyright_line, deep_line) in BORROWED.items():
            with self.subTest(name=name):
                self.assertIn(copyright_line, text)
                self.assertIn(_flat(deep_line), text,
                              f"{name}'s licence text is cut short")

    def test_every_vendored_file_is_named_in_the_notices(self):
        # A second library dropped into static/vendor/ must come with a notice.
        text = _read(NOTICES)
        for path in glob.glob(os.path.join(REPO, "static", "vendor", "*")):
            rel = os.path.relpath(path, REPO).replace(os.sep, "/")
            with self.subTest(file=rel):
                self.assertIn(rel, text, f"{rel} is vendored but has no notice")

    def test_every_font_family_is_named_in_the_notices(self):
        text = _read(NOTICES).lower()
        for path in glob.glob(os.path.join(REPO, "static", "fonts", "*.woff2")):
            family = os.path.basename(path).split("-", 1)[0]
            with self.subTest(font=family):
                self.assertIn(family, text, f"the {family} font has no notice")


    def test_each_notice_carries_its_own_licence_text(self):
        # Preact's and Feather's are both MIT, so a check over the whole file
        # would pass with only one copy of the text. Each section needs its own.
        text = _read(NOTICES)
        for name, (copyright_line, deep_line) in BORROWED.items():
            with self.subTest(name=name):
                section = text[text.index(f"## {name} —"):]
                end = section.find("\n## ", 4)
                section = _flat(section if end < 0 else section[:end])
                self.assertIn(copyright_line, section)
                self.assertIn(_flat(deep_line), section)

    def test_the_icons_that_are_feathers_say_so(self):
        head = _read(os.path.join(REPO, "static", "app", "icons.js"))[:2500]
        self.assertIn("Feather", head)
        self.assertIn("THIRD_PARTY_NOTICES.md", head)


class TestTheNoticeSitsWithTheCopy(unittest.TestCase):

    def test_the_vendored_file_points_at_the_notices(self):
        first = _read(os.path.join(REPO, "static", "vendor",
                                   "preact-htm.module.js")).splitlines()[0]
        self.assertTrue(first.startswith("/*") and "THIRD_PARTY_NOTICES.md" in first,
                        "the minified file lost its pointer to its licences")

    def test_the_vendored_file_is_the_release_the_notices_name(self):
        # NC-262. The notices say "unchanged apart from a one-line comment". This
        # holds them to it: past that line, the bytes are htm 3.1.1's
        # preact/standalone.module.js as published on npm.
        import hashlib
        with open(os.path.join(REPO, "static", "vendor", "preact-htm.module.js"), "rb") as fh:
            body = fh.read().split(b"\n", 1)[1]
        self.assertEqual(hashlib.sha256(body).hexdigest(), HTM_3_1_1_STANDALONE_SHA256,
                         "the vendored file was edited, or replaced by another release: "
                         "update THIRD_PARTY_NOTICES.md to say what it is now")
        self.assertIn("htm 3.1.1", _read(os.path.join(REPO, "THIRD_PARTY_NOTICES.md")))

    def test_the_font_licence_is_next_to_the_fonts(self):
        text = _flat(_read(os.path.join(REPO, "static", "fonts", "OFL.txt")))
        self.assertIn(BORROWED["Fraunces"][0], text)
        self.assertIn(_flat(BORROWED["Fraunces"][1]), text)

    def test_the_build_copies_the_notices_into_the_app(self):
        src = _read(os.path.join(REPO, "packaging", "appify.sh"))
        self.assertRegex(src, r'cp "\$REPO_ROOT/THIRD_PARTY_NOTICES\.md" '
                              r'"\$APP_CODE_DIR/THIRD_PARTY_NOTICES\.md"')

    def test_the_built_app_carries_them(self):
        """The notices file travels inside the app. Until NC-330 (2026-10-17) the app
        also carried the web page and its fonts, so this checked the font licence
        (static/fonts/OFL.txt) inside the bundle too. The window is native now and the
        bundle no longer holds static/ at all (tests/test_dev_parity.py asserts that),
        so the font licence has nothing beside it to travel with; it stays in the
        repository, next to the fonts the browser page still uses."""
        if not os.path.isdir(BUNDLE_APP):
            self.skipTest("no built bundle (run packaging/appify.sh --embed)")
        self.assertEqual(_read(os.path.join(BUNDLE_APP, "THIRD_PARTY_NOTICES.md")),
                         _read(NOTICES), "the app carries an old or missing notices file")


if __name__ == "__main__":
    unittest.main()
