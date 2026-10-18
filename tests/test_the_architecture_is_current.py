"""NC-282. ARCHITECTURE.md's figures and tool list match the code, within reason.

It said Ask had three tools (it has eleven), app.py was ~660 lines (1,073), the
logic lived in 15 modules (24), and the suite was ~310 tests in 21 files taking
ten seconds (about 2,700 in 115, forty seconds), and it named tests that no
longer exist. Nothing checked it. Figures are rounded on purpose, so this
fails only past DRIFT.
"""

import glob
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import chat_mod as chat  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIFT = 0.25
WORDS = {"no": 0, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14}


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def _lines(rel):
    return sum(1 for _ in open(os.path.join(REPO, rel), encoding="utf-8"))


def _section(text, number):
    start = text.index(f"\n## {number}. ")
    end = text.find("\n## ", start + 5)
    return text[start:end if end > 0 else len(text)]


class TestTheArchitectureIsCurrent(unittest.TestCase):

    def setUp(self):
        self.text = _read("ARCHITECTURE.md")

    def near(self, claimed, actual, what):
        self.assertLessEqual(abs(claimed - actual) / actual, DRIFT,
                             f"ARCHITECTURE.md says {what} is ~{claimed}; it is {actual}")

    def test_the_module_table_lists_every_api_module_at_its_size(self):
        rows = dict(re.findall(r"^\| `(api_\w+\.py)` \| ([\d,]+) \|", _section(self.text, 1), re.M))
        real = {os.path.basename(p) for p in glob.glob(os.path.join(REPO, "crm", "api_*.py"))}
        self.assertEqual(set(rows), real, "the §1 table and crm/api_*.py disagree")
        for name, figure in rows.items():
            with self.subTest(module=name):
                self.near(int(figure.replace(",", "")), _lines(os.path.join("crm", name)), name)
        said = re.search(r"lives in (\d+) flat sibling modules", self.text).group(1)
        self.assertEqual(int(said), len(real))

    def test_the_two_big_files_are_given_at_their_size(self):
        for rel, pattern in (("crm/app.py", r"\*\*transport only\*\* \(~([\d,]+) lines\)"),
                             ("crm/db.py", r"even though at ~([\d,]+) lines it is")):
            with self.subTest(file=rel):
                claimed = int(re.search(pattern, self.text).group(1).replace(",", ""))
                self.near(claimed, _lines(rel), rel)

    def test_it_lists_every_tool_ask_has(self):
        section = _section(self.text, 5)
        word = re.search(r"\*\*`TOOLS`\*\* — (\w+) tools", section).group(1)
        self.assertEqual(WORDS[word], len(chat.TOOLS))
        for tool in chat.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertIn(f"`{tool['name']}`", section)

    def test_the_suite_is_given_at_its_size_and_names_only_real_files(self):
        section = _section(self.text, 9)
        m = re.search(r"~([\d,]+) tests across ~([\d,]+) files", section)
        files = glob.glob(os.path.join(REPO, "tests", "test_*.py"))
        tests = sum(len(re.findall(r"^\s+def test_", _read("tests", os.path.basename(f)), re.M))
                    for f in files)
        self.near(int(m.group(1).replace(",", "")), tests, "the number of tests")
        self.near(int(m.group(2)), len(files), "the number of test files")
        for name in set(re.findall(r"`(test_\w+)`", section)):
            with self.subTest(test=name):
                self.assertTrue(os.path.exists(os.path.join(REPO, "tests", name + ".py")),
                                f"§9 names tests/{name}.py, which does not exist")

    def test_the_intake_table_is_at_its_size(self):
        section = _section(self.text, 10)
        rows = re.findall(r"^\| `([^`]+)` \| ([\d,]+) \|", section, re.M)
        self.assertGreaterEqual(len(rows), 4, rows)
        for rel, figure in rows:
            with self.subTest(file=rel):
                self.near(int(figure.replace(",", "")), _lines(rel), rel)

    def test_the_known_bug_count_is_right(self):
        word = re.search(r"`TestKnownBugs` holds (\w+) `expectedFailure`", self.text).group(1)
        src = _read("tests", "test_api_intake.py")
        body = src[src.index("class TestKnownBugs"):]
        nxt = body.find("\nclass ", 5)
        body = body if nxt < 0 else body[:nxt]
        decorators = re.findall(r"^\s*@unittest\.expectedFailure\s*$", body, flags=re.M)
        self.assertEqual(WORDS[word], len(decorators))


if __name__ == "__main__":
    unittest.main()
