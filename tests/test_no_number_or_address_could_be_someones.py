"""NC-293. No test number or address can belong to a real person.

This code is read by strangers, and a made-up number that rings someone's
phone is that person's number. A Swedish mobile number made of repeated digits can
fall in the range handed to real subscribers, and an address on a real domain
reaches a real inbox. Sweden's telecoms regulator (PTS) sets aside
070-174 06 05 to 070-174 06 99 for fiction, and
example.com, example.org, example.net and anything ending in .example, .test or
.invalid are reserved for exactly this, so those are the only ones allowed.

Written on the shape, never the value, like tests/test_no_one_elses_machine.py:
forbidding one number by name would mean writing it here, and would miss the next.
"""

import os
import re
import subprocess
import unittest

import tests.helpers  # noqa: F401 — import order contract; see tests/helpers.py

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# A Swedish mobile in any spelling: +46, 0046 or 0 in front, or the bare digits a
# WhatsApp id carries. The nine digits after the prefix are what is checked.
SWEDISH_MOBILE = re.compile(r"(?<![\d])(?:\+46[ -]?|0046[ -]?|0|46)(7\d(?:[ -]?\d){7})(?!\d)")
FICTION = range(701740605, 701740700)

ADDRESS = re.compile(r"[a-z0-9._%+-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)
RESERVED = re.compile(r"(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(example|test|invalid))$")
# Not a person's inbox: WhatsApp's own id suffixes (always with an allowed number,
# checked above), GitHub's relay, and domains that cannot exist.
NOT_AN_INBOX = {"s.whatsapp.net", "g.us", "users.noreply.github.com"}
# Gmail needs at least six characters before the @, so this one cannot be anyone's.
GMAIL_PLACEHOLDER = "you@gmail.com"
# NC-249. Other projects' licence texts, copied in word for word because their
# licences require the notice to travel unchanged. The addresses in them are the
# authors' own published contact lines, not made-up ones a test could have invented.
VERBATIM_LICENCES = "packaging/licenses/LICENSE."


def tracked_text():
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout
    paths = [rel for rel in out.split("\0") if rel]
    skip = tests.helpers.not_exported(paths, REPO)    # never published
    for rel in paths:
        if rel in skip:
            continue
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                yield rel, fh.read()
        except UnicodeDecodeError:
            continue


class TestNoNumberOrAddressCouldBeSomeones(unittest.TestCase):

    def test_every_swedish_mobile_is_in_the_fiction_block(self):
        found = []
        for rel, text in tracked_text():
            # A package's fingerprint (NC-316) is 64 hex digits, and a run of them
            # can read as a phone number. It is a checksum, never a number to call.
            text = re.sub(r"--hash=sha256:[0-9a-f]{64}", "", text)
            for m in SWEDISH_MOBILE.finditer(text):
                digits = int(re.sub(r"\D", "", m.group(1)))
                if digits in FICTION or set(str(digits)[1:]) == {"0"}:
                    continue
                found.append(f"{rel}: {m.group(0)}")
        self.assertEqual(found, [], "use 070-174 06 05 to 070-174 06 99, reserved for fiction")

    def test_every_address_is_on_a_reserved_domain(self):
        found = []
        for rel, text in tracked_text():
            if rel.startswith(VERBATIM_LICENCES):
                continue
            for m in ADDRESS.finditer(text):
                domain = m.group(1).lower()
                if (RESERVED.search(domain) or domain in NOT_AN_INBOX
                        or m.group(0).lower() == GMAIL_PLACEHOLDER
                        or re.fullmatch(r"[\d.]+", domain)            # a version: htm@3.1.1
                        or re.search(r"\.[a-z]$", domain)):          # a one-letter ending: a@b.c
                    continue
                found.append(f"{rel}: {m.group(0)}")
        self.assertEqual(found, [], "use example.com, or a name ending in .example")

    def test_the_check_sees_what_it_is_meant_to(self):
        # A regex that matched nothing would pass forever. Built from pieces, so
        # this file does not itself hold a number or address the checks forbid.
        ring, bare = "70 111 11 11", "701111111"
        text = f"+46 {ring}, 0{bare}, 46{bare}@s.whatsapp.net, ada@{'x'}.com"
        self.assertEqual(len(SWEDISH_MOBILE.findall(text)), 3)
        self.assertFalse(RESERVED.search(ADDRESS.search(text).group(1)))
        self.assertTrue(RESERVED.search("bigco.example"))
        self.assertIn(701740605, FICTION)
        self.assertIn(701740699, FICTION)


if __name__ == "__main__":
    unittest.main()
