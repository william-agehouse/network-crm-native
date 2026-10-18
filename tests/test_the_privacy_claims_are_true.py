"""The front page's privacy promise has to match what the code sends (NC-241).

README opened with "Nothing is uploaded anywhere ... raw messages ... stay on this
Mac", and the landing page said raw messages are "never sent anywhere". Meanwhile
Ask's `search_messages` tool returned the text of matching messages, and the ones
either side, to Anthropic whenever a key was set. ARCHITECTURE.md had the true
version; the two pages a stranger reads first did not. For a privacy-first app
that is the worst claim to get wrong, so the wording is pinned here to the code.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Absolute promises the app cannot keep once an Anthropic key is set.
BROKEN_PROMISES = (
    "nothing is uploaded anywhere",
    "never sent anywhere",
    "messages never leave",
    "the only thing that ever leaves",
    "go nowhere",
    "goes nowhere",
    "phoning home",
)

# Claims that only Ask sends anything, or that no key means nothing leaves
# (a calendar feed needs no key). Checked in README and ARCHITECTURE.md (NC-253).
ONLY_ASK = (
    "ask calls only",
    "except ask calls",
    "with no keys set nothing leaves",
    "nothing leaves your home network",
)

# The pages that make privacy claims, of those this checkout has.
PAGES = tuple(p for p in ("README.md", os.path.join("landing", "index.html"))
              if os.path.exists(os.path.join(REPO, p)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _plain(text):
    """Lower-case text with markup and emphasis removed and spaces collapsed,
    so a promise split over two lines or wrapped in ** still matches."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("*", "").replace(">", " ")
    return re.sub(r"\s+", " ", text).lower()


def _ask_sends_message_text():
    """True while Ask has a tool that hands message text to the model."""
    src = _read(os.path.join("crm", "chat.py"))
    return '"name": "search_messages"' in src


class TestThePrivacyClaimsAreTrue(unittest.TestCase):

    def test_readme_is_always_checked(self):
        self.assertIn("README.md", PAGES)

    def test_no_page_makes_a_promise_the_code_breaks(self):
        for rel in PAGES:
            text = _plain(_read(rel))
            for promise in BROKEN_PROMISES:
                with self.subTest(page=rel, promise=promise):
                    self.assertNotIn(
                        promise, text,
                        f"{rel} promises {promise!r}, but with an Anthropic key "
                        "Ask sends message excerpts out (NC-241)")

    def test_each_page_says_what_ask_sends_while_it_sends_messages(self):
        if not _ask_sends_message_text():
            self.skipTest("Ask no longer sends message text; the wording may relax")
        for rel in PAGES:
            text = _plain(_read(rel))
            with self.subTest(page=rel):
                self.assertIn("message excerpts", text,
                              f"{rel} does not say Ask sends message excerpts")
                self.assertIn("anthropic", text,
                              f"{rel} does not say who receives them")

    def test_the_readme_says_it_before_anything_else(self):
        # The privacy note is the first thing README says after its feature list.
        # The correction has to be there, not buried under "Optional AI features".
        readme = _plain(_read("README.md"))
        first = readme.index("privacy first")
        self.assertIn("message excerpts", readme[first:first + 700])

    def test_no_doc_says_only_ask_sends_anything(self):
        # NC-253. ARCHITECTURE.md said nothing leaves "except Ask calls", while
        # the AI summary, the draft, the paste reader and three passes send too.
        for rel in ("README.md", "ARCHITECTURE.md"):
            text = _plain(_read(rel))
            for claim in ONLY_ASK:
                with self.subTest(page=rel, claim=claim):
                    self.assertNotIn(claim, text)

    def test_both_lists_name_the_update_check_while_it_exists(self):
        # NC-271. The lists said four things send data out; the daily release
        # check was a fifth, and a release build turns it on.
        if "api.github.com" not in _read(os.path.join("crm", "updates.py")):
            self.skipTest("the update check no longer asks GitHub")
        readme = _plain(_read("README.md"))
        first = readme.index("privacy first")
        privacy = readme[first:readme.index("quick start")]
        # Since 2026-10-17 the list says the app never checks
        # by itself, which is true only while nothing on either screen asks
        # /api/update. If a screen starts asking, the list must name the check again.
        callers = [f for d in ("static/app", "native/Sources")
                   for f in sorted(os.listdir(os.path.join(REPO, d)))
                   if "/api/update" in _read(os.path.join(d, f))]
        if callers:
            self.assertIn("the update check", privacy,
                          f"{callers} ask /api/update, so the list must name the check")
        else:
            self.assertIn("never checks for new versions by itself", privacy)
        arch = _read("ARCHITECTURE.md")
        section = _plain(arch[arch.index("## 7."):arch.index("## 8.")])
        self.assertIn("the update check (`updates.py`)".replace("`", ""), section.replace("`", ""))

    def test_every_module_that_calls_anthropic_is_named_in_section_7(self):
        # A new AI feature cannot be added without saying what it sends.
        arch = _read("ARCHITECTURE.md")
        section = arch[arch.index("## 7."):arch.index("## 8.")]
        crm = os.path.join(REPO, "crm")
        callers = sorted(f for f in os.listdir(crm) if f.endswith(".py")
                         and "messages.create(" in _read(os.path.join("crm", f)))
        self.assertGreaterEqual(len(callers), 5, callers)
        for name in callers:
            with self.subTest(module=name):
                self.assertIn(f"`{name}", section,
                              f"crm/{name} calls Anthropic but ARCHITECTURE.md §7 "
                              "does not say what it sends")

    def test_the_check_would_notice_the_tool(self):
        # If the tool is renamed, the skip above would quietly switch the
        # wording test off. Pin the name to something that exists.
        self.assertTrue(_ask_sends_message_text(),
                        "search_messages is gone from crm/chat.py: update "
                        "_ask_sends_message_text to the tool that replaced it")


if __name__ == "__main__":
    unittest.main()
