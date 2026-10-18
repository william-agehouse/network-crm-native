"""NC-246. README claims that drift on their own when the code moves.

Most README fixes are one-off corrections, but two kinds of claim are numbers
that change without anyone touching the README: the helper's size ("~500 lines"
for a file of 1,218) and the count of deployment shapes ("three", with two
listed after one was deleted).
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9}


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestTheReadmeMatchesTheCode(unittest.TestCase):

    def test_the_helper_is_as_long_as_the_readme_says(self):
        readme = re.sub(r"\s+", " ", _read("README.md"))
        m = re.search(r"compiled helper of about ([\d,]+) lines", readme)
        self.assertIsNotNone(m, "README no longer says how long the helper is")
        claimed = int(m.group(1).replace(",", ""))
        actual = len(_read("packaging", "src", "imessage_sync.swift").splitlines())
        self.assertLess(abs(actual - claimed) / actual, 0.25,
                        f"README says about {claimed} lines; the helper is {actual}")

    def test_the_number_of_shapes_is_the_number_listed(self):
        readme = _read("README.md")
        section = readme[readme.index("## Where to run it"):]
        section = section[:section.index("\n## ", 4)]
        rows = re.findall(r"^\| \*\*[A-Z]\. ", section, re.M)
        heading = re.search(r"## Where to run it — (\w+) shapes", readme).group(1)
        said = re.search(r"the (\w+) deployment\s+shapes", readme).group(1)
        self.assertEqual(WORDS[heading], len(rows))
        self.assertEqual(WORDS[said], len(rows),
                         f"README says {said} deployment shapes and lists {len(rows)}")

    def test_the_docs_name_every_source_the_helper_copies(self):
        # NC-272. README said the helper copies "those two databases plus your
        # address book", and that there would never be an Apple Calendar option,
        # while the helper also copied every calendar on the Mac. Someone
        # deciding whether to grant it Full Disk Access was told less than is true.
        swift = _read("packaging", "src", "imessage_sync.swift")
        labels = re.findall(r'^\s+label: "(\w+)",$', swift, re.M)
        self.assertEqual(labels, ["iMessage", "WhatsApp", "Contacts", "Calendar"])
        header = re.search(r"Disk Access\. (\w+) sources", swift).group(1).lower()
        self.assertEqual(WORDS[header], len(labels))
        said = {"iMessage": "iMessage", "WhatsApp": "WhatsApp",
                "Contacts": "address book", "Calendar": "Calendar"}
        readme = re.sub(r"\s+", " ", _read("README.md"))
        para = readme[readme.index("**opt-in** way to skip the copying"):]
        para = para[:para.index("IMESSAGE_SYNC.md")]
        guide = re.sub(r"\s+", " ", _read("packaging", "IMESSAGE_SYNC.md"))
        guide = guide[guide.index("**2. This helper"):guide.index("### Do it from inside")]
        for label in labels:
            with self.subTest(source=label):
                self.assertIn(said[label], para, "README's helper paragraph")
                self.assertIn(said[label].lower(), guide.lower(), "IMESSAGE_SYNC.md")
        self.assertNotIn("there will not be one", readme)

    def test_the_search_download_is_given_at_its_real_size(self):
        # NC-275. Both docs called it the 90 MB model; the packages before it are
        # about 2 GB, as the app's own Settings row and requirements.txt say.
        for rel in ("README.md", "ARCHITECTURE.md"):
            for para in re.split(r"\n\s*\n", _read(rel)):
                if "90 MB" in para:
                    with self.subTest(doc=rel):
                        self.assertIn("2 GB", para)

    def test_the_readme_names_the_data_pages_real_parts(self):
        # NC-276. README sent readers to "Found on this Mac" and an Enrich button
        # after the Data page was rebuilt around Needs you and Your sources.
        readme = _read("README.md")
        for gone in ("Found on this Mac", "Network view", "**Enrich**"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, readme)
        data_js = _read("static", "app", "data.js")
        for section in ("Needs you", "Your sources"):
            with self.subTest(section=section):
                self.assertIn(f"**{section}**", readme)
                self.assertIn(section, data_js)
        ledger = _read("crm", "api_ledger.py")
        for row in ("Worked out on this Mac", "LinkedIn profile facts", "Facts written by AI"):
            with self.subTest(row=row):
                self.assertIn(f"- **{row}**", readme)
                self.assertIn(f'"label": "{row}"', ledger)

    def test_the_linkedin_row_says_what_the_api_route_needs(self):
        # NC-277. It said "Nothing — one click via LinkedIn's API", a route that
        # needs a LinkedIn developer app of your own, which most readers do not have.
        readme = _read("README.md")
        row = next(line for line in readme.splitlines() if line.startswith("| **LinkedIn** |"))
        self.assertNotIn("one click", row)
        self.assertLess(row.index("Connections.csv"), row.index("Member Data Portability"),
                        "the route that works for anyone has to come first")
        self.assertIn("r_dma_portability_self_serve", row)
        self.assertIn("r_dma_portability_self_serve", _read("crm", "importers.py"))
        self.assertIn('"LinkedIn messages"', _read("crm", "api_ledger.py"))
        self.assertIn("**LinkedIn messages** row", row)

    def test_the_mac_app_section_carries_its_own_build_command(self):
        # NC-278. Quick start sent readers to "the steps in that section", which
        # only linked elsewhere, and the section read like change-log notes.
        readme = _read("README.md")
        section = readme[readme.index("### Install as a Mac app"):]
        section = section[:section.index("\n## ")]
        self.assertIn("```bash\nxcode-select --install", section)
        self.assertIn("packaging/appify.sh --embed\n```", section)
        for phrase in ("used to be missing", "and that has not changed"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, readme)

    def test_the_build_guide_matches_the_build(self):
        # NC-281. packaging/README.md said the bundle no longer carried a Python
        # (it does, since NC-021) and that nothing read deps_status.json (the
        # app does), and left the notices file out of what the build copies. The
        # launcher still named the bots.
        guide = re.sub(r"\s+", " ", _read("packaging", "README.md"))
        for phrase in ("no longer carries an embedded CPython", "nothing in the app reads"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, guide)
        # NC-330: static/ is no longer copied (the window is native), and the
        # launcher script that once named the bots is gone.
        lists = re.findall(r"copies `crm/`,.*? into", guide)
        self.assertEqual(len(lists), 2, lists)
        for listed in lists:
            self.assertIn("THIRD_PARTY_NOTICES.md", listed)
            self.assertNotIn("static/", listed)
        self.assertIn('cp "$REPO_ROOT/THIRD_PARTY_NOTICES.md"', _read("packaging", "appify.sh"))
        self.assertFalse(os.path.exists(os.path.join(REPO, "packaging", "src", "launcher.sh.tmpl")))

    def test_the_changelog_does_not_contradict_the_app(self):
        # NC-283. It said the bundled Python was gone for good, described a boot
        # picker and a Settings section that were removed, and miscounted
        # Settings.
        log = re.sub(r"\s+", " ", _read("CHANGELOG.md"))
        for phrase in ("Settings → Essentials", "shipped to everyone"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, log)
        self.assertIn("Reversed in NC-021", log)
        self.assertIn("NC-062", log)
        said = re.search(r"Settings holds (\w+) things", log).group(1)
        rows = re.findall(r'^    \("(\w+)", "', _read("crm", "api_abilities.py"), re.M)
        self.assertEqual(WORDS.get(said, said), len(rows), rows)

    def test_the_tabs_in_the_readme_are_the_tabs_in_the_app(self):
        # NC-263. README sold a Today view after NC-009 deleted it.
        tabs = re.search(r"const TABS = \[(.*?)\];", _read("static", "app", "main.js"), re.S)
        app_tabs = re.findall(r'\["\w+", "([^"]+)"\]', tabs.group(1))
        self.assertGreaterEqual(len(app_tabs), 3, app_tabs)
        readme = _read("README.md")
        section = readme[readme.index("## The app — "):]
        section = section[:section.index("\n## ", 4)]
        listed = re.findall(r"^\| \*\*([A-Z][a-z]+)\*\* \|", section, re.M)
        self.assertEqual(listed, app_tabs)
        heading = re.search(r"## The app — (\w+) tabs", readme).group(1)
        self.assertEqual(WORDS[heading], len(app_tabs))

    def test_the_layout_names_only_front_end_modules_that_exist(self):
        line = next(line for line in _read("README.md").splitlines()
                    if line.strip().startswith("app/") and "Preact ES modules" in line)
        names = re.search(r"— ([a-z/]+)", line).group(1).split("/")
        self.assertGreaterEqual(len(names), 5, names)
        for name in names:
            with self.subTest(module=name):
                self.assertTrue(os.path.exists(os.path.join(REPO, "static", "app", name + ".js")),
                                f"README's layout lists static/app/{name}.js, which does not exist")

    def test_the_readme_says_it_is_for_macos(self):
        # NC-261. A Linux or Windows visitor should learn that before the install steps.
        readme = _read("README.md")
        self.assertLess(readme.index("Built for macOS"), readme.index("## Quick start"))

    def test_no_doc_sends_the_reader_to_the_today_view(self):
        for rel in ("README.md", "ARCHITECTURE.md"):
            text = re.sub(r"\s+", " ", _read(rel))
            for sentence in re.split(r"(?<=[.!?])\s", text):
                if "Today" in sentence:
                    with self.subTest(page=rel, sentence=sentence[:70]):
                        self.assertRegex(sentence, r"deleted|went with|took over|NC-009",
                                         "the Today view is gone; say so or drop it")

    def test_the_architecture_lists_the_same_shapes(self):
        arch = _read("ARCHITECTURE.md")
        section = arch[arch.index("## 7. Deployment shapes"):]
        section = section[:section.index("\n## ", 4)]
        rows = re.findall(r"^\| \*\*[A-Z]\. ", section, re.M)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("Railway", section, "a deleted hosting shape is back")


if __name__ == "__main__":
    unittest.main()
