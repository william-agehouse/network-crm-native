"""NC-149 gate: a Settings field must not be able to become an ssh option.

`IMAC` and `KEY` are ordinary text fields on the Settings page. They reach three
command lines — `ssh`, `scp` and `rsync -e` — and as bare arguments they would be
read as options. ssh reads an argument beginning with "-" as an OPTION, and
`-oProxyCommand=…` runs its value through /bin/sh. So "can change Settings" would
mean "can run commands as the user running the app", over an HTTP API that binds
127.0.0.1 with no auth by default.

It is not theoretical. With a real ssh client:

    $ ssh -o BatchMode=yes -o ConnectTimeout=1 -oProxyCommand=touch\\ /tmp/x true
    Connection closed by UNKNOWN port 65535
    $ ls /tmp/x
    /tmp/x

    $ ssh -o BatchMode=yes -o ConnectTimeout=1 -- -oProxyCommand=touch\\ /tmp/x true
    hostname contains invalid characters

Two walls, and the order matters. `--` at the call site is the real fix: it
holds for a value that never came through the Settings form at all — one typed
straight into secrets/.env, or inherited from the environment. The validator is
the second wall, and its job is to give a person a sentence instead of a
mystery.

The boring half is the one that will actually happen: a key path with a space in
it, which rsync's -e splitter turns into extra ssh options, so the photo pull
fails and blames an unreachable host. `~/Library/Application Support/…` is an
ordinary place to keep a file on this platform, so the fix quotes the path
rather than forbidding the space.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, code_of, py_block  # noqa: E402,F401

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import settings  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")


class TestTheHostFieldRefusesAnOption(unittest.TestCase):
    def test_a_proxycommand_host_is_refused(self):
        with self.assertRaises(ValueError) as e:
            settings._check_host("-oProxyCommand=touch /tmp/x")
        self.assertIn("ssh would read it as an option", str(e.exception))

    def test_an_option_with_no_space_in_it_is_refused_too(self):
        """The leading dash is the rule, not the space.

        `-oProxyCommand=touch /tmp/x` is also refused by a regex as loose as
        `^\\S+$` — for the space, not for the dash. `-oProxyCommand=id` has no
        space in it and separates the two.
        """
        for value in ("-oProxyCommand=id", "-F/tmp/evil_config", "--"):
            with self.assertRaises(ValueError, msg=f"{value!r} was accepted"):
                settings._check_host(value)

    def test_an_ordinary_host_is_accepted(self):
        settings._check_host("you@your-mac")
        settings._check_host("your-mac")

    def test_a_host_with_a_space_is_refused(self):
        """Nothing legitimate needs one, and a space is how a single field
        becomes two arguments."""
        with self.assertRaises(ValueError):
            settings._check_host("host -oProxyCommand=x")

    def test_the_validator_is_wired_into_the_write_path(self):
        self.assertIn("IMAC", settings.VALIDATORS)
        self.assertIn("KEY", settings.VALIDATORS)
        write = py_block(code_of(os.path.join(CRM, "settings.py")), "def write_key(")
        self.assertIn("VALIDATORS", write,
                      "write_key no longer runs the per-setting validators, so "
                      "the Settings form accepts an ssh option again")


class TestTheKeyFieldRefusesWhatOnlyLooksLikeAPath(unittest.TestCase):
    def test_a_key_path_starting_with_a_dash_is_refused(self):
        with self.assertRaises(ValueError):
            settings._check_key_path("-oProxyCommand=x")

    def test_a_key_path_with_a_quote_in_it_is_refused(self):
        """The call site quotes this path into rsync's -e string. A path with a
        quote of its own would close that quoting."""
        with self.assertRaises(ValueError) as e:
            settings._check_key_path("/tmp/a'b")
        self.assertIn("rsync and ssh read as syntax", str(e.exception))

    def test_a_key_path_with_a_space_is_allowed(self):
        """A key path with a space in it must be accepted (NC-149).
        `~/Library/Application Support/…` has a space in it, and refusing it
        would refuse the commonest real path on this platform."""
        import tempfile
        d = tempfile.mkdtemp(prefix="crm key ")
        p = os.path.join(d, "scraper key")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("not a real key")
        settings._check_key_path(p)   # must not raise

    def test_a_key_that_is_not_there_is_refused_by_name(self):
        with self.assertRaises(ValueError) as e:
            settings._check_key_path("/tmp/definitely-not-a-key-" + "x" * 12)
        self.assertIn("There is no file at", str(e.exception))


class TestEveryCallSiteStopsOptionParsing(unittest.TestCase):
    """The wall that holds for a value the Settings form never saw."""

    def test_ssh_passes_two_dashes_before_the_host(self):
        src = code_of(os.path.join(CRM, "merge_scraped_facts.py"))
        block = py_block(src, "def _ssh(")
        self.assertIn('"--", IMAC', block,
                      "ssh is called with IMAC as a bare argument again, so a "
                      "host beginning with '-' is an option and "
                      "-oProxyCommand runs a shell")

    def test_scp_passes_two_dashes_before_its_source(self):
        src = code_of(os.path.join(CRM, "merge_scraped_facts.py"))
        i = src.index('"scp"')
        line_end = src.index("]", i)
        self.assertIn('"--"', src[i:line_end],
                      "the scp source is one f-string starting with IMAC, so "
                      "without '--' the whole token is read as an option")

    def test_rsync_stops_option_parsing_twice(self):
        """Once for rsync's own source argument, and once inside -e for the ssh
        rsync spawns — rsync appends the host to that command, so the '--' has
        to be the last thing in it."""
        src = code_of(os.path.join(CRM, "import_scraped_photos.py"))
        block = py_block(src, "def pull_photos(")
        self.assertRegex(block, r'ConnectTimeout=15 --"',
                         "the -e command no longer ends with '--', so the host "
                         "rsync appends to it is parsed as an ssh option")
        self.assertRegex(block, r'"--exclude=\*", "--",',
                         "rsync's own source argument is unguarded again")

    def test_the_rsync_key_path_is_quoted(self):
        src = code_of(os.path.join(CRM, "import_scraped_photos.py"))
        block = py_block(src, "def pull_photos(")
        self.assertIn("shlex.quote(IMAC_KEY)", block,
                      "the key path goes into rsync's -e string unquoted again, "
                      "so a path with a space in it becomes extra ssh options "
                      "and the pull fails blaming an unreachable host")
        self.assertNotIn("-i {IMAC_KEY}", block,
                         "the unquoted interpolation is back")


if __name__ == "__main__":
    unittest.main()
