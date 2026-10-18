"""NC-119 gate: the one field whose label and whose storage disagreed.

`KEY` is read as a file path — `crm/enrich_li_extras.py`, `crm/import_scraped_photos.py`
both hand it to ssh and rsync as `-i <path>`. A label that names a credential
("Scraper SSH key") over a box that stores ordinary unmasked text invites a person
to paste the key itself and have it echoed back into a plain text input.

**Note:** the storage half of this is closed by NC-149, which makes these values
safe to hand to ssh. Its `_check_key_path` refuses anything that is not a path, so
key material cannot get into `secrets/.env` at all. What the box does with each
paste:

    the whole key, as copied   REFUSED — value must be a single line
    one line of the key body   REFUSED — There is no file at b3BlbnNzaC1rZXkt…
    the header line alone      REFUSED — a path starting with '-' is read by ssh…
    a real path                STORED

That leaves the label, and whether to mask the box. It is left unmasked, and the
reason is the validator: there is nothing secret in the box, and a masked path
cannot be checked at a glance — which is the whole job of that field, since a
wrong path is the likeliest reason the scraper row fails. That reasoning only
holds while KEY is validated as a path, which is why the last test here fails if
it stops being.
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, js_code_of  # noqa: E402,F401

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import config  # noqa: E402
import settings  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")

PRIVATE_KEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
               "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAA\n"
               "-----END OPENSSH PRIVATE KEY-----\n")


def key_field():
    """The KEY box, as the Data page's LinkedIn profile facts row declares it,
    plus the refusal the page shows for a pasted key. (NC-236: it used to be a
    field in the old Settings page; it is now a plain text box on that row.)"""
    with open(os.path.join(REPO, "crm", "api_ledger.py"), encoding="utf-8") as fh:
        m = re.search(r'_text\("KEY",\s*"((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)"\)', fh.read())
    assert m, "the KEY box is no longer declared with _text(...) in api_ledger.py"
    refusal = js_code_of(os.path.join(APP, "ledger.js"))
    at = refusal.index("export function textProblem(")
    body = refusal[at:refusal.index("\n}\n", at)]
    r = re.search(r'key === "KEY".*?return "((?:[^"\\]|\\.)*)"', body, re.S)
    assert r, "textProblem no longer refuses a pasted private key for KEY"
    return {"label": m.group(1), "placeholder": m.group(2), "refusal": r.group(1)}


class TestTheLabelNamesAPath(unittest.TestCase):

    def setUp(self):
        self.field = key_field()

    def test_the_label_says_where_before_it_says_key(self):
        label = (self.field["label"] or "").lower()
        self.assertTrue(label.startswith("where"),
                        "the label for a box that stores a file path does not "
                        "lead with where the file is: %r" % self.field["label"])
        self.assertLess(
            label.index("where"), label.index("key"),
            "the label leads with the credential and mentions the place "
            "second, which is the order that invites someone to paste the "
            "key: %r" % self.field["label"])
        self.assertIn("file", label)

    def test_the_refusal_says_the_key_itself_does_not_go_here(self):
        said = self.field["refusal"].lower()
        self.assertIn("that is the key itself", said,
                      "the page refuses a pasted key without saying what it "
                      "was: %r" % self.field["refusal"])
        self.assertIn("where the key file is", said,
                      "the refusal never says what to paste instead")

    def test_the_placeholder_still_shows_a_path(self):
        self.assertTrue((self.field["placeholder"] or "").startswith("~/"),
                        "the placeholder is what signals that this box takes a "
                        "path; it has to keep doing that")

    def test_a_pasted_key_is_refused_before_it_is_sent(self):
        js = js_code_of(os.path.join(APP, "ledger.js"))
        at = js.index("export function TextSetting(")
        self.assertIn("textProblem(act.key, value)", js[at:at + 1200])


class TestKeyMaterialCannotBeStored(Base):
    """NC-149's validator, held from NC-119's side: it is now load-bearing for
    the decision not to mask this field, so it is pinned from here too."""

    def write(self, value):
        with mock.patch.object(config, "SECRETS_DIR", self.dir):
            return settings.write_key("KEY", value)

    def test_the_whole_key_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.write(PRIVATE_KEY)
        self.assertIn("single line", str(caught.exception))

    def test_one_line_of_the_key_body_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.write(PRIVATE_KEY.splitlines()[1])
        self.assertIn("no file at", str(caught.exception),
                      "a single base64 line has no newline to catch it, so the "
                      "only thing standing between it and secrets/.env is the "
                      "check that the path exists")

    def test_the_header_line_alone_is_refused(self):
        with self.assertRaises(ValueError):
            self.write("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_a_real_path_is_stored_as_typed(self):
        path = os.path.join(self.dir, "a_key")
        open(path, "w").close()
        self.write(path)
        with open(os.path.join(self.dir, ".env"), encoding="utf-8") as fh:
            self.assertIn("KEY=" + path, fh.read())

    def test_nothing_that_was_refused_reached_the_file(self):
        for value in (PRIVATE_KEY, PRIVATE_KEY.splitlines()[1],
                      "-----BEGIN OPENSSH PRIVATE KEY-----"):
            with self.assertRaises(ValueError):
                self.write(value)
        env = os.path.join(self.dir, ".env")
        body = open(env, encoding="utf-8").read() if os.path.exists(env) else ""
        self.assertNotIn("PRIVATE KEY", body)
        self.assertNotIn("b3BlbnNzaC", body)


class TestTheDecisionNotToMaskStillHolds(unittest.TestCase):

    def test_the_field_is_not_masked(self):
        self.assertIs(settings.MANAGED_KEYS["KEY"]["secret"], False,
                      "masking hides the one thing this field exists to let you "
                      "check — a wrong path is the likeliest reason the scraper "
                      "row fails. If this is flipped on purpose, the reasoning "
                      "in the NC-119 comment beside it has to be rewritten too")

    def test_not_masking_it_rests_on_the_value_being_a_path(self):
        self.assertIn(
            "KEY", settings.VALIDATORS,
            "KEY is no longer validated, so nothing stops key material being "
            "stored in it — and leaving it unmasked was only defensible while "
            "something did. Re-take the masking decision")

    def test_the_value_comes_back_unmasked(self):
        with mock.patch.dict(os.environ, {"KEY": "~/.ssh/example_key"}):
            row = next(r for r in settings.read_all() if r["key"] == "KEY")
        self.assertEqual(row["value"], "~/.ssh/example_key")
        self.assertFalse(row["secret"])

    def test_the_settings_that_really_are_secret_are_still_masked(self):
        """The control: this only means something if masking works at all."""
        masked = [k for k, m in settings.MANAGED_KEYS.items() if m["secret"]]
        self.assertTrue(masked, "no setting is marked secret, so 'not masked' "
                                "says nothing about this one")
        with mock.patch.dict(os.environ, {masked[0]: "sk-ant-not-a-real-key"}):
            row = next(r for r in settings.read_all() if r["key"] == masked[0])
        self.assertNotIn("not-a-real-key", row["value"])


if __name__ == "__main__":
    unittest.main()
