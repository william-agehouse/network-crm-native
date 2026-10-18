"""Doc gate for LINKEDIN_SCRAPE_HANDOFF.md (NC-013).

WHY THIS FILE EXISTS
---------------------
Before NC-013, the handoff doc described the data flow from an *already
running* scraper and never said "pip install", never mentioned Firefox, and
never mentioned --login. All of that setup information lived only in
crm/scrape_linkedin_profiles.py's module docstring, which the doc didn't even
point at — so a reader (human or AI agent) handed just the doc had no way to
actually set the scraper up. These gates assert the pieces that were missing
stay present, and — the highest-value one — that every CLI flag the doc tells
a reader to type still exists in the script's own argparse, so the doc cannot
silently drift out of sync with the code the way it did before.

Scope: the flag-drift gate only checks ```bash fenced code blocks — the doc's
actual copy-pasteable commands — not prose or the reference table at the
bottom, which name flags descriptively rather than telling anyone to type them.

THE TRAP THIS FILE IS WRITTEN TO AVOID
---------------------------------------
A gate that greps raw text can match the doc's own explanatory prose instead
of its real content, for example a comment that says "NO setPointerCapture
here". Two defenses used throughout:
  * every phrase gate is checked against a whitespace-normalized, markdown-
    bold-stripped copy of the text, because this doc hard-wraps prose at ~80
    columns — a phrase can legitimately split across a source line, and a raw
    multi-line string literal would go stale the moment the wrapping changes;
  * where a word (Firefox, Selenium) legitimately appears twice for different
    reasons — once in the actual install step, once in the pre-existing
    pipeline description in section 3 — the gate is scoped to the Setup
    section's own slice of text, not "appears anywhere in the file", so
    deleting the install step still fails the test even though section 3
    still mentions the same word.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATH = os.path.join(REPO, "LINKEDIN_SCRAPE_HANDOFF.md")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _doc_text():
    return _read(DOC_PATH)


def _normalize(text):
    """Collapse whitespace (undoing this doc's hard line-wraps) and strip
    markdown bold markers, lowercased — so a phrase check survives rewrapping
    or a bold/plain toggle without survivng an actual content deletion."""
    text = text.replace("*", "")
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _section(text, start_heading, end_heading):
    """The raw slice of `text` from `start_heading` (inclusive) up to
    `end_heading` (exclusive). Both are matched literally. Raises if either
    heading is missing, so a renamed heading fails loudly instead of silently
    matching the whole document."""
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


# ── Flag-drift gate: read valid flags straight from each script's argparse ──
ADD_ARGUMENT_RE = re.compile(r'add_argument\(\s*["\'](--[a-zA-Z][a-zA-Z0-9-]*)["\']')
FLAG_TOKEN_RE = re.compile(r'(--[a-zA-Z][a-zA-Z0-9-]*)')
BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)```", re.S)

# Every script this doc tells a reader to invoke by name, and (for the shell
# wrapper) which underlying scripts' flags apply to it. Registering a script
# here is deliberate: a line with a "--flag" that names none of these keys
# fails the gate instead of being silently skipped, so a future doc edit that
# starts invoking a new script has to teach this file about it too.
SCRIPT_FLAG_TARGETS = {
    "crm/scrape_linkedin_profiles.py": ["crm/scrape_linkedin_profiles.py"],
    "crm/merge_scraped_facts.py": ["crm/merge_scraped_facts.py"],
    "crm/import_scraped_photos.py": ["crm/import_scraped_photos.py"],
    "crm/enrich_li_extras.py": ["crm/enrich_li_extras.py"],
    "crm/scrape_dispatch.py": ["crm/scrape_dispatch.py"],
    # Thin wrapper — forwards its own argv unchanged to all three pull scripts.
    "deploy/pull_and_merge.sh": [
        "crm/merge_scraped_facts.py",
        "crm/import_scraped_photos.py",
        "crm/enrich_li_extras.py",
    ],
}


def _script_flags(rel_path):
    """The set of --flags `rel_path` registers via argparse, read live from
    source so this gate tracks the script rather than a hardcoded snapshot."""
    src = _read(os.path.join(REPO, rel_path))
    return set(ADD_ARGUMENT_RE.findall(src))


def _known_flags_for_line(line):
    """Union of argparse flags for every script named on this line, or None
    if the line names none of the scripts this gate knows about."""
    matched = [key for key in SCRIPT_FLAG_TARGETS if key in line]
    if not matched:
        return None
    flags = {"--help"}  # argparse always provides this; no script opts out
    for key in matched:
        for target in SCRIPT_FLAG_TARGETS[key]:
            flags |= _script_flags(target)
    return flags


class TestTosWarningPrecedesFirstCommand(unittest.TestCase):
    """The ToS/ban-risk warning comes before any instruction."""

    def test_warning_text_present_and_first(self):
        norm = _normalize(_doc_text())
        tos_idx = norm.find("restricted or permanently banned linkedin account")
        self.assertNotEqual(tos_idx, -1, "ToS/ban-risk warning sentence is missing")

        first_command_idx = norm.find("```bash")
        self.assertNotEqual(first_command_idx, -1, "doc has no runnable command at all")

        self.assertLess(
            tos_idx, first_command_idx,
            "the ToS warning must appear before the first copy-pasteable command",
        )

    def test_warning_names_the_realistic_consequence(self):
        norm = _normalize(_doc_text())
        self.assertIn("terms of service", norm)
        self.assertIn("linkedin's systems", norm)


class TestSetupNamesFirefoxSeleniumAndAnthropicSdk(unittest.TestCase):
    """The doc says to install Firefox, and to install Selenium + Anthropic SDK.

    Scoped to the Setup section's own text — section 3 (the pre-existing
    pipeline description) also says "Firefox" and "Selenium", so a gate that
    checked the whole document would keep passing even if the actual install
    steps were deleted.
    """

    def _setup_slice(self):
        text = _doc_text()
        section = _section(
            text,
            "## Setup — do this once, in order, on the machine that will run the scraper",
            "## TL;DR",
        )
        return _normalize(section)

    def test_firefox_install_step(self):
        self.assertIn("firefox", self._setup_slice())

    def test_selenium_install_step(self):
        setup = self._setup_slice()
        self.assertIn("selenium", setup)
        self.assertIn("pip install selenium anthropic", setup)

    def test_anthropic_sdk_install_step(self):
        setup = self._setup_slice()
        self.assertIn("anthropic sdk", setup)
        self.assertIn("pip install selenium anthropic", setup)


class TestLoginRequiresAHumanFor2fa(unittest.TestCase):
    """The doc names --login, and says that a human must clear 2FA themselves —
    explicitly enough that an AI agent handed this doc won't try it."""

    def _setup_slice(self):
        text = _doc_text()
        section = _section(
            text,
            "## Setup — do this once, in order, on the machine that will run the scraper",
            "## TL;DR",
        )
        return _normalize(section)

    def test_login_flag_documented(self):
        self.assertIn("--login", self._setup_slice())

    def test_human_must_clear_2fa_sentence(self):
        setup = self._setup_slice()
        self.assertIn(
            "a human must type the linkedin credentials and clear whatever "
            "checkpoint or 2fa challenge",
            setup,
        )
        self.assertIn("no instruction in this file", setup)

    def test_agent_is_told_it_cannot_do_this_step(self):
        setup = self._setup_slice()
        self.assertIn("if you are an ai agent: you cannot complete this step", setup)


class TestCheckpointInstructionPresent(unittest.TestCase):
    """What to do at a checkpoint: stop, wait 24-48h, don't retry."""

    def test_checkpoint_banner_quoted(self):
        norm = _normalize(_doc_text())
        self.assertIn("checkpoint / security page detected", norm)

    def test_do_not_retry_instruction(self):
        norm = _normalize(_doc_text())
        self.assertIn(
            "single most important operational rule in this whole document: "
            "do not re-run the scraper after this",
            norm,
        )

    def test_cooldown_window_matches_scrape_dispatch_constants(self):
        norm = _normalize(_doc_text())
        self.assertIn("24-48 hours", norm)
        # cross-check against the actual source constants, not just a number
        # this file happens to also use somewhere else.
        dispatch_src = _read(os.path.join(REPO, "crm/scrape_dispatch.py"))
        self.assertIn('_envint("SCRAPE_COOLDOWN_MIN", 24)', dispatch_src)
        self.assertIn('_envint("SCRAPE_COOLDOWN_MAX", 48)', dispatch_src)


class TestOneMachineIsTheDefaultPath(unittest.TestCase):
    """One machine is the default, two machines the variant, and the doc must not
    invent a flag name — --source-dir must be real."""

    def test_one_machine_presented_before_two_machine(self):
        norm = _normalize(_doc_text())
        one_idx = norm.find("one machine (the default")
        two_idx = norm.find("two machines (")
        self.assertNotEqual(one_idx, -1, "one-machine path is missing")
        self.assertNotEqual(two_idx, -1, "two-machine variant is missing")
        self.assertLess(one_idx, two_idx, "one-machine must be presented as the default, first")

    def test_source_dir_flag_used_is_real(self):
        text = _doc_text()
        one_idx = text.lower().find("one machine (the default")
        two_idx = text.lower().find("two machines (")
        one_machine_slice = text[one_idx:two_idx]
        self.assertIn("--source-dir", one_machine_slice)
        # and it must actually exist on both scripts it's demonstrated against
        self.assertIn("--source-dir", _script_flags("crm/import_scraped_photos.py"))
        self.assertIn("--source-dir", _script_flags("crm/enrich_li_extras.py"))

    def test_facts_need_no_merge_step_on_one_machine(self):
        norm = _normalize(_doc_text())
        self.assertIn("no merge step, no ssh", norm)


class TestLaunchdPlistGenerateNotCopy(unittest.TestCase):
    """The shipped plist cannot be loaded as it stands, and the doc has to say so
    and say why.

    The rule is what is checked, not a literal sentence: the doc must say the
    file will not work unsubstituted, and must name the token as the thing to
    substitute. Asserting an exact sentence would risk holding a wrong one in
    place — a gate keeping a document stale rather than true.
    """

    def test_it_says_the_shipped_file_will_not_work_as_it_stands(self):
        norm = _normalize(_doc_text())
        self.assertRegex(
            norm, r"will not load until you substitute|do not copy it in as-is|"
                  r"never copy in as-is|not a path",
            "the doc no longer warns that the shipped plist cannot be used "
            "unchanged, so a reader will copy it in and get a LaunchAgent "
            "pointing at directories that do not exist")

    def test_it_names_the_token_to_substitute(self):
        norm = _normalize(_doc_text())
        self.assertIn("__network_crm_root__", norm,
                      "the doc does not name the placeholder token, so 'adapt "
                      "your own' gives the reader nothing to search for")
        with open(os.path.join(REPO, "deploy",
                               "com.networkcrm.liscraper.plist"),
                  encoding="utf-8") as fh:
            plist = fh.read()
        self.assertIn("__NETWORK_CRM_ROOT__", plist,
                      "the plist no longer uses the token the doc tells the "
                      "reader to substitute")
        # Whether a REAL home directory has crept into the plist is checked by
        # tests/test_no_one_elses_machine.py, which knows that
        # `/Users/you/path/to/network-crm` in the sed example is a placeholder
        # and a blanket "/Users/" search is not. One rule, one owner.


class TestEveryCliFlagExistsInArgparse(unittest.TestCase):
    """The drift gate: every --flag this doc tells a reader to type, inside a
    ```bash block, must be a real argparse flag on the script named on that
    line. This is what stops the doc silently rotting the way it did before
    NC-013 (docs describing a running scraper nobody could actually set up)."""

    def test_bash_blocks_exist(self):
        blocks = BASH_BLOCK_RE.findall(_doc_text())
        self.assertGreater(len(blocks), 5, "expected several runnable command blocks")

    def test_every_flag_resolves_against_a_real_script(self):
        blocks = BASH_BLOCK_RE.findall(_doc_text())
        checked = 0
        for block in blocks:
            for line in block.splitlines():
                flags_in_line = FLAG_TOKEN_RE.findall(line)
                if not flags_in_line:
                    continue
                known = _known_flags_for_line(line)
                self.assertIsNotNone(
                    known,
                    "line uses a flag but names no script this gate can check "
                    f"against argparse — register it in SCRIPT_FLAG_TARGETS: {line!r}",
                )
                for flag in flags_in_line:
                    self.assertIn(
                        flag, known,
                        f"doc tells the reader to pass {flag!r} but that is not "
                        f"a registered argparse flag for the script(s) named on "
                        f"this line: {line!r}",
                    )
                    checked += 1
        self.assertGreater(checked, 0, "no flags were actually exercised by this gate")


if __name__ == "__main__":
    unittest.main()
