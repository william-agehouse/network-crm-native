"""NC-138. The repository carries no machine's layout.

A hostname, a login, a private-network address and an absolute home directory
grant no access, but they are a map of somebody's private network, and they are
useless to every reader who is not that person. They also break things: hooks
pinned to one home directory fail on every other machine.

Every gate here is written on the SHAPE of the thing, never on the value. A gate
that says `assertNotIn("<a hostname>", ...)` has to write that hostname into
the repo in order to forbid it, and it still misses every other real host.
"""

import os
import re
import unittest

import tests.helpers  # noqa: F401 — import order contract; see tests/helpers.py

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This file has to write "100.64.0.0/10" in order to explain which addresses it
# rejects, so it would flag itself. Same shape as the xcode-select rule in
# test_packaging.py: warning about a thing means naming it. Files git never
# exports are skipped too: they are not published.
SKIP_FILES = {os.path.join("tests", "test_no_one_elses_machine.py")}

# Placeholders a reader is meant to substitute. A home path is fine when it is
# visibly not a real one.
PLACEHOLDER_HOME = re.compile(r"^/Users/(you|your|x|xxx|user|username|me|"
                              r"someone|example|\.\.\.|<[^>]*>|\{)", re.I)


def tracked_files():
    """Every file git tracks OR is about to, read as text where possible.

    NC-205. Files not yet added to git must be read too: a file that carries a
    made-up "/Users/<name>/" folder would pass a check of tracked files and fail
    only once it was committed. New files that .gitignore does not exclude are
    what the next commit will carry, so they are checked now."""
    import subprocess
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    paths = [rel for rel in out.split("\0") if rel]
    private = tests.helpers.not_exported(paths, REPO)
    for rel in paths:
        if rel in SKIP_FILES or rel in private:
            continue
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                yield rel, fh.read()
        except (UnicodeDecodeError, OSError):
            continue


class TestANewFileIsCheckedBeforeItIsCommitted(unittest.TestCase):
    def test_an_untracked_file_is_read(self):
        """NC-205. The premise of every check below: a file nobody has run
        `git add` on yet is still read."""
        name = f"_nc205_probe_{os.getpid()}.txt"
        path = os.path.join(REPO, "tests", name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("probe\n")
        try:
            self.assertIn(os.path.join("tests", name), {rel for rel, _ in tracked_files()})
        finally:
            os.remove(path)

    def test_an_ignored_file_is_not(self):
        """And what .gitignore keeps out is not read: that is where secrets/
        and the database files live."""
        import subprocess
        name = f"_nc205_probe_{os.getpid()}.db"             # *.db is ignored
        path = os.path.join(REPO, "tests", name)
        rel = os.path.join("tests", name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("probe\n")
        try:
            ignored = subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO)
            self.assertEqual(ignored.returncode, 0, "the premise: *.db is ignored")
            self.assertNotIn(rel, {r for r, _ in tracked_files()})
        finally:
            os.remove(path)


class TestNoRealHomeDirectoryIsCommitted(unittest.TestCase):
    def test_no_absolute_path_into_someones_home(self):
        """`/Users/<name>/...` in a committed file is wrong on every machine but
        one, and a path like that in `.claude/settings.json` would make the hooks
        run for nobody else: hooks resolve through the project directory."""
        for rel, text in tracked_files():
            for m in re.finditer(r"/Users/[A-Za-z0-9._-]+", text):
                with self.subTest(file=rel, path=m.group(0)):
                    self.assertRegex(
                        m.group(0), PLACEHOLDER_HOME,
                        f"{rel} contains {m.group(0)!r}, an absolute path into a "
                        "real home directory. It is wrong on every other machine, "
                        "and it tells a stranger the account name of whoever wrote "
                        "it. Use a path relative to the repo, or "
                        "$CLAUDE_PROJECT_DIR.")


class TestNoMachineCoordinatesAreCommitted(unittest.TestCase):
    """A host, a login and an address, anywhere in the repository."""

    # 100.64.0.0/10 is the carrier-grade NAT range Tailscale assigns from, so
    # any address in it is a real node on somebody's private network.
    TAILSCALE = re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\."
                           r"\d{1,3}\.\d{1,3}\b")

    def test_no_tailscale_address(self):
        for rel, text in tracked_files():
            m = self.TAILSCALE.search(text)
            with self.subTest(file=rel):
                self.assertIsNone(
                    m, f"{rel} carries what looks like a Tailscale address "
                       f"({m.group(0) if m else ''}) — a real machine on a real "
                       "private network")

    # A login at a machine: an account, then a bare machine name or a
    # ".local" one. An address with a real domain (x@example.com) is an email,
    # and the negative lookahead leaves it alone.
    LOGIN_AT_MACHINE = re.compile(
        r"(?<![A-Za-z0-9_.@/-])([A-Za-z][A-Za-z0-9_.-]*)@"
        r"([A-Za-z][A-Za-z0-9-]*(?:\.local)?)(?![A-Za-z0-9.-])")
    # Words that make one side visibly a stand-in for the reader's own value.
    STAND_IN = re.compile(r"^(you|your|someone|their|user|example|host|name|me|"
                          r"test|other|x)\b|^(you|your|their|other)-", re.I)

    def test_no_real_account_at_host_anywhere(self):
        """NC-245. Every file is checked, not a fixed list of likely ones: a
        fixed list passes while a help text or a test carries a real login."""
        for rel, text in tracked_files():
            for m in self.LOGIN_AT_MACHINE.finditer(text):
                account, host = m.group(1), m.group(2)
                if self.STAND_IN.search(account) or self.STAND_IN.search(host):
                    continue
                with self.subTest(file=rel, token=m.group(0)):
                    self.fail(
                        f"{rel} names {m.group(0)!r}, which reads as a real login "
                        "on a real machine. Say the shape, like you@your-mac, and "
                        "leave the values in the reader's ssh config.")


class TestNoRealWhatsAppIdIsCommitted(unittest.TestCase):
    """NC-242. A WhatsApp hidden id in a fixture has to be invented, because a
    real one identifies a real person.

    WhatsApp's ids are random, so a real one uses most of the ten digits. The
    invented ones here use at most three (100000000000001, 120000000000000001).
    Only the lengths WhatsApp's ids come in, 14 to 18 digits, are checked;
    ordinary phone fixtures already follow the suite's fake patterns."""

    LONG_ID = re.compile(r"(?<![0-9A-Za-z.])\d{14,18}(?![0-9A-Za-z])")

    def test_every_long_id_is_visibly_invented(self):
        for rel, text in tracked_files():
            for m in self.LONG_ID.finditer(text):
                digits = m.group(0)
                with self.subTest(file=rel, starts=digits[:4]):
                    self.assertLessEqual(
                        len(set(digits)), 3,
                        f"{rel} has a {len(digits)}-digit number that looks "
                        "random, which is what a real WhatsApp id looks like. "
                        "Use one made of zeros and ones, like 100000000000001.")



class TestTheExampleEnvAsksOnlyForKeysTheCodeReads(unittest.TestCase):
    """NC-139. `env.example` is the first file a new user is told to copy, so
    every key in it must be one the code reads. Checked by asking the code
    rather than by keeping a list.

    NC-299: it was `.env.example`. The leading dot hid it in Finder, so a Mac
    user could not see the file README tells them to copy, and tools that skip
    every `.env` file skipped it too. Without the dot it is visible, and
    readable by the checks.
    """

    def _env_example(self):
        """The working copy, plain `open()`.

        Not `git show HEAD:env.example`: that asserts against the last
        commit, so it fails for anyone who has edited the file and not yet
        committed, which is everyone who is fixing it. A test process reading
        a committed template is ordinary file I/O.
        """
        with open(os.path.join(REPO, "env.example"), encoding="utf-8") as fh:
            return fh.read()

    def _keys_in(self, text):
        return {m.group(1) for m in
                re.finditer(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", text, re.M)}

    def _keys_the_code_reads(self):
        import glob
        names = set()
        for path in glob.glob(os.path.join(REPO, "crm", "*.py")):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            names |= {m.group(1) for m in re.finditer(
                r'os\.environ(?:\.get)?[\(\[]\s*["\']([A-Z][A-Z0-9_]*)["\']', src)}
        return names

    def test_every_key_it_asks_for_is_read_somewhere(self):
        offered = self._keys_in(self._env_example())
        self.assertTrue(offered, "no keys found — the extractor is wrong")
        live = self._keys_the_code_reads()
        for key in sorted(offered):
            with self.subTest(key=key):
                self.assertIn(
                    key, live,
                    f"{key} is offered in env.example and read by no file in "
                    "crm/. Filling it in does nothing, and a template full of "
                    "dead keys is how someone concludes the app needs accounts "
                    "it does not need.")

    def test_it_asks_for_the_one_key_the_app_actually_uses(self):
        text = self._env_example()
        self.assertIn("ANTHROPIC_API_KEY", self._keys_in(text))
        # Uncommented: this is the one a reader is meant to fill in, and a
        # commented-out line reads as an optional extra rather than the point.
        self.assertRegex(text, r"(?m)^ANTHROPIC_API_KEY=",
                         "ANTHROPIC_API_KEY is only mentioned as a comment, so "
                         "the file still does not ask for it")

    SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASS\b|CLIENT_ID|AUTH|COOKIE|SESSION")
    PLACEHOLDER = re.compile(r"^$|your|<[^>]*>|\.\.\.|example|x{3,}|changeme|replace", re.I)

    def _secret_keys(self, text):
        """(checked, filled): every key named like a secret, and those whose
        value is not blank or an obvious placeholder. Values never leave here."""
        checked, filled = [], []
        for m in re.finditer(r"^#?[ \t]*([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*([^\n]*)$", text, re.M):
            key, value = m.group(1), m.group(2).strip().strip("\"'")
            if self.SECRETISH.search(key):
                checked.append(key)
                if not self.PLACEHOLDER.search(value):
                    filled.append(key)
        return checked, filled

    def test_no_secret_in_it_has_a_real_value(self):
        """NC-265. It is published with the code, so every key named like a secret
        is blank or an obvious placeholder. A failure names the key and never
        prints the value."""
        checked, filled = self._secret_keys(self._env_example())
        self.assertIn("ANTHROPIC_API_KEY", checked, "the check found no secret keys to check")
        self.assertEqual(filled, [], "these keys in env.example have a value that is not "
                                     "a placeholder (values not shown)")

    def test_it_is_short_dateless_and_visible_in_finder(self):
        """NC-299. The old one had grown big, carried a date, and was hidden."""
        text = self._env_example()
        self.assertLessEqual(len(text.splitlines()), 40, "the template has grown long again")
        self.assertIsNone(re.search(r"\b20\d\d-\d\d-\d\d\b", text), "it carries a date")
        self.assertIsNone(re.search(r"\bNC-\d+", text), "it names an internal issue number")
        self.assertFalse(os.path.exists(os.path.join(REPO, ".env" + ".example")),
                         "the hidden copy came back beside the visible one")

    def test_the_secret_check_would_notice_a_real_key(self):
        text = ("ANTHROPIC_API_KEY=sk-ant-api03-abcDEF123\n"
                "APP_TOKEN=\n# GMAIL_CLIENT_SECRET=<your client secret>\nAPP_PORT=8765\n")
        checked, filled = self._secret_keys(text)
        self.assertEqual(checked, ["ANTHROPIC_API_KEY", "APP_TOKEN", "GMAIL_CLIENT_SECRET"])
        self.assertEqual(filled, ["ANTHROPIC_API_KEY"])


class TestSecretFilesAreIgnored(unittest.TestCase):
    """NC-288. Google hands out its sign-in file as client_secret_<id>.json. The
    guide says to save it in secrets/, which is ignored; left in the project
    folder instead, it would be one `git add` from being published."""

    def test_git_ignores_every_kind_of_secret_file(self):
        import subprocess
        for name in ("client_secret_1234-abcd.apps.googleusercontent.com.json",
                     "crm/client_secret_x.json", "gmail_credentials_you.json",
                     "gmail_token_you.json", "secrets/.env", "stray-copy.db"):
            with self.subTest(file=name):
                try:
                    proc = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO)
                except OSError:
                    self.skipTest("git is not available")
                if proc.returncode == 128:
                    self.skipTest("not a git checkout")
                self.assertEqual(proc.returncode, 0, f"git would commit {name}")


if __name__ == "__main__":
    unittest.main()
