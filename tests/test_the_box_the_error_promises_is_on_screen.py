"""NC-178. Both ways the file picker can fail ended by pointing at a box.

`POST /api/import/pick-file` returns "This Mac has no osascript, so the file
picker cannot open. Type or paste the path into the field instead." and "The
file picker could not open… You can paste the path into the field instead."

There was no field. The chosen path was rendered as a read-only chip — and only
once a path existed, which means even the chip was absent in the state the
message is written for. So the only advice given to somebody whose picker could
not open named a control that was not on screen, in exactly the two cases where
they have no other way in: a Mac without `osascript`, and a dialog that would
not launch.

Two halves are gated here.

**The box exists, and it does not depend on the thing it supplies.** The old
chip was `${path && html`<span…>`}` — visible only after the picker had already
worked. A gate that reads the source cannot tell that apart from a box that is
always there; both are one line with the right words in it. So every guard that
decides whether the box renders is sliced out of the real file and EXECUTED
under node, once with a path and once without, and the two answers must match.

**A pasted path is a path.** The box is new, and what people paste into it is
not what the native picker returned: a trailing newline, quotes around it,
backslash-escaped spaces from dragging a file onto Terminal, a leading `~`.
`api_import.human_path` accepts all four, and refusing them would have made the
box a worse picker rather than a second way in.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db, js_code_of  # noqa: E402,F401
import api_import  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")

# Where the box has to be, and why each one matters: three boxes in two views.
VIEWS = {
    "data.js": "the Data page's import control",
    "onboarding.js": "both first-run steps",
}
EXPECTED_BOXES = {"data.js": 1, "onboarding.js": 2}


def path_box_guards(filename):
    """Every `${<guard> && html`<${PathBox}` guard in a view, as source text.

    Taken from the real file with comments blanked, so the gate reads what runs.
    """
    js = js_code_of(os.path.join(APP, filename))
    return [m.group(1).strip() for m in
            re.finditer(r"\$\{([^`$]*?)&&\s*html`\s*\n?\s*<\$\{PathBox\}", js)]


class TestTheBoxDoesNotWaitForAPath(unittest.TestCase):
    """Executed, not read. The bug was a boolean, so the boolean has to run."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed")

    def evaluate(self, guard, scope):
        # NC-236: the Data page decides on `act.needs_path`, the first-run steps
        # on `needsFile`; give every guard both shapes of the same fact.
        scope = {**scope, "act": {"needs_path": scope["needsFile"]}}
        script = ("".join(f"const {k} = {json.dumps(v)};\n" for k, v in scope.items())
                  + "process.stdout.write(JSON.stringify(!!(" + guard + ")));")
        out = subprocess.run([self.node, "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, f"{guard!r}\n{out.stderr}")
        return json.loads(out.stdout)

    def test_every_view_that_needs_a_box_has_one(self):
        for name, where in VIEWS.items():
            with self.subTest(view=name):
                self.assertEqual(len(path_box_guards(name)), EXPECTED_BOXES[name],
                                 f"{where} lost its path box")

    def test_the_box_renders_whether_or_not_a_path_is_known(self):
        """The whole rule, in one assertion. A box that appears only after the
        picker worked is no help to somebody the picker failed."""
        for name in VIEWS:
            for guard in path_box_guards(name):
                with self.subTest(view=name, guard=guard):
                    spec = {"kind": "file", "pick_kind": "vcf", "key": "contacts"}
                    empty = self.evaluate(guard, {"spec": spec, "needsFile": True,
                                                  "path": ""})
                    filled = self.evaluate(guard, {"spec": spec, "needsFile": True,
                                                   "path": "/tmp/x.vcf"})
                    self.assertTrue(empty,
                                    "the box is hidden until a path exists — "
                                    "which is the bug, not the fix")
                    self.assertEqual(empty, filled)

    def test_a_source_that_needs_no_file_still_gets_no_box(self):
        """The control. Without it, `true` passes everything above and Gmail —
        which has no file to choose — grows a path box."""
        for name in VIEWS:
            for guard in path_box_guards(name):
                with self.subTest(view=name, guard=guard):
                    spec = {"kind": "none", "pick_kind": None, "key": "gmail"}
                    self.assertFalse(
                        self.evaluate(guard, {"spec": spec, "needsFile": False,
                                              "path": ""}),
                        "a source with nothing to pick is offering a path box")


class TestTheMessagesAndTheBoxAgree(unittest.TestCase):
    """The messages and the box must stay true together, or the advice points at
    a control that is not on screen again."""

    def test_the_picker_still_promises_a_box(self):
        """If these stop naming a field, the box may go — but then this test is
        what tells you the box may go, instead of the box being deleted with the
        messages left behind."""
        with open(os.path.join(REPO, "crm", "api_import.py"), encoding="utf-8") as fh:
            py = fh.read()
        at = py.index("def handle_import_pick_file")
        body = py[at:py.index("\n# ", at)]
        promises = [ln for ln in body.splitlines() if "into the field" in ln]
        self.assertEqual(len(promises), 2,
                         "both picker failures used to point at a field; "
                         f"found {len(promises)}")

    def test_and_a_box_exists_for_them_to_point_at(self):
        ui = js_code_of(os.path.join(APP, "ui.js"))
        self.assertIn("export function PathBox", ui)
        self.assertIn('class="src-path-box"', ui)
        with open(os.path.join(REPO, "static", "css", "ui.css"), encoding="utf-8") as fh:
            css = fh.read()
        self.assertIn(".src-path-box", css, "the box has no styling at all")

    def test_the_refusal_no_longer_names_only_the_picker(self):
        """"Select the file first." was the other half of the same lie: it is
        the message you get for pressing Run with nothing chosen, and it named
        the one route that had just failed."""
        js = js_code_of(os.path.join(APP, "data.js"))
        self.assertNotIn("Select the file first.", js)
        self.assertNotIn("Select the folder first.", js)
        self.assertIn("paste its path into the box", js)


class TestAPastedPathIsAPath(unittest.TestCase):
    """api_import.human_path — the four shapes a person's paste really arrives in."""

    def test_a_plain_path_is_untouched(self):
        self.assertEqual(api_import.human_path("/tmp/contacts.vcf"),
                         "/tmp/contacts.vcf")

    def test_whitespace_and_a_trailing_newline_go(self):
        self.assertEqual(api_import.human_path("  /tmp/contacts.vcf\n"),
                         "/tmp/contacts.vcf")

    def test_quotes_round_the_path_go(self):
        for quoted in ('"/tmp/contacts.vcf"', "'/tmp/contacts.vcf'",
                       '  "/tmp/contacts.vcf" '):
            self.assertEqual(api_import.human_path(quoted), "/tmp/contacts.vcf", quoted)

    def test_an_unmatched_quote_is_left_alone(self):
        """A filename really can start with a quote. Only a matching pair is a
        wrapper."""
        self.assertEqual(api_import.human_path('"/tmp/odd.vcf'), '"/tmp/odd.vcf')

    def test_a_home_shortcut_is_expanded(self):
        out = api_import.human_path("~/Downloads/contacts.vcf")
        self.assertTrue(out.startswith(os.path.expanduser("~")), out)
        self.assertNotIn("~", out)

    def test_a_path_dragged_from_terminal_finds_its_file(self):
        """Dragging a file onto Terminal escapes the spaces. It is the most
        likely thing to be in the clipboard of somebody who has just been told
        to paste a path."""
        d = tempfile.mkdtemp(prefix="nc178_")
        real = os.path.join(d, "My Export.vcf")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("x")
        dragged = real.replace(" ", "\\ ")
        self.assertNotEqual(dragged, real)
        self.assertEqual(api_import.human_path(dragged), real)

    def test_a_real_backslash_survives(self):
        """The unescape is a check, not a guess: it only happens when the
        literal path does not exist and the unescaped one does. A file whose
        name really contains a backslash keeps it."""
        d = tempfile.mkdtemp(prefix="nc178_")
        real = os.path.join(d, "keep\\ me.vcf")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.assertEqual(api_import.human_path(real), real)
        self.assertTrue(os.path.exists(api_import.human_path(real)))

    def test_a_typo_is_reported_as_the_person_typed_it(self):
        """The unescape needs BOTH halves of its check, and this is the half the
        outer guard does not cover: when neither form exists — a typo — the path
        comes back exactly as typed, so the importer's "no file at …" names what
        is in their clipboard rather than a string the app invented."""
        d = tempfile.mkdtemp(prefix="nc178_")
        typed = os.path.join(d, "Not\\ Here.vcf")
        self.assertFalse(os.path.exists(typed))
        self.assertFalse(os.path.exists(typed.replace("\\ ", " ")))
        self.assertEqual(api_import.human_path(typed), typed)

    def test_nothing_stays_nothing(self):
        for empty in ("", "   ", None):
            self.assertEqual(api_import.human_path(empty), "")


class TestTheTypedPathReachesTheImporter(Base):
    """human_path existing is not the claim — the claim is that what a person
    typed is what runs."""

    def test_the_command_carries_the_tidied_path(self):
        cmd = api_import._build_command("contacts", {"path": '  "~/Downloads/a.vcf" \n'})
        joined = " ".join(str(a) for a in cmd)
        self.assertIn(os.path.expanduser("~/Downloads/a.vcf"), joined)
        self.assertNotIn("~", joined)
        self.assertNotIn('"', joined)

    def test_the_background_loop_is_not_touched_by_any_of_this(self):
        """Every path the timer imports was written by this app, not typed by a
        person. It builds its argv straight from the registry and never comes
        through _build_command, so tidying cannot reach it — and a path that
        legitimately contains a backslash or a tilde is safe there."""
        import importers
        odd = "/tmp/a path/with\\ backslash.db"
        cmd = importers.REGISTRY["imessage"].build_cmd({"path": odd})
        self.assertIn(odd, [str(a) for a in cmd])


if __name__ == "__main__":
    unittest.main()
