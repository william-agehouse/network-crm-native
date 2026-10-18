"""NC-123. Two ways in to what macOS protects, and the simple one leads.

Invariant 6 used to read **"No Full Disk Access, ever."** It shaped the whole
product: iMessage opened by telling the reader "this one never asks for the
permission that would", `crm/ics_import.py` called the Mac's own calendar
"permanently out of bounds" — and meanwhile a sync helper holding exactly that
permission copied iMessage, Contacts and Calendar, and the Apple Calendar row
read the calendar through it. The app argued both ways about one permission.

The decision (NC-123): simplicity matters more than avoiding Full Disk Access,
but an option that does not require it always stays, and the two routes are
chosen with one switch.

So the invariant is now two routes and a switch, and this file gates the three
things that make that true on screen rather than only in docs/EDITING.md:

1. **Every source that needs the permission describes both routes on its own
   row** — the helper, and the way in that needs nothing — with the simple one
   first. A row that offers only one is how the old rule hid the helper, and how a
   future tidy-up would hide the fallback.
2. **iMessage says how to switch.** A switch is only a switch if the reader
   knows where it is.
3. **Nothing still states the old rule as fact.** Quoting it is fine when the
   quote is marked as history; asserting it is how the old rule creeps back in.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import importers, not_exported  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The sources whose data macOS protects, so the helper needs Full Disk Access to
# copy them. WhatsApp is deliberately absent: it never needed Full Disk Access.
# Reading it in place costs a macOS dialog instead, which its row has to name
# (NC-192, TestWhatsAppNamesTheDialog below).
NEEDS_THE_PERMISSION = {
    "imessage": "iMessage",
    "contacts": "Apple Contacts",
    "calendar_local": "Apple Calendar",
}

# How a row introduces its no-permission route. One spelling on purpose: it is
# the heading a reader scans for, and three rows spelling it three ways is three
# things to recognise instead of one.
FALLBACK = "**Without the helper**"


def help_text(key):
    return importers.REGISTRY[key].export_help


class TestEveryProtectedSourceOffersBothRoutes(unittest.TestCase):

    def test_the_helper_covers_exactly_these_protected_sources(self):
        """The premise. If the helper gains a source that macOS protects, it has
        to be added above — or its row can offer the helper and nothing else."""
        covered = set(importers.HELPER_SOURCE_LABELS)
        self.assertEqual(covered, {"iMessage", "WhatsApp", "Contacts", "Calendar"},
                         "the helper copies a different set of sources now; "
                         "check which need Full Disk Access and list them here")

    def test_each_row_describes_the_helper_route(self):
        for key, label in NEEDS_THE_PERMISSION.items():
            with self.subTest(source=label):
                self.assertIn("helper", help_text(key).lower(),
                              f"the {label} row never mentions the sync helper, "
                              f"which is the simple route invariant 6 says leads")

    def test_each_row_keeps_the_route_that_needs_no_permission(self):
        """The condition on relaxing the rule: an option that does not require
        Full Disk Access stays."""
        for key, label in NEEDS_THE_PERMISSION.items():
            with self.subTest(source=label):
                self.assertIn(
                    FALLBACK, help_text(key),
                    f"the {label} row has no {FALLBACK} section, so a person who "
                    f"does not want to grant the permission is not told there is "
                    f"another way")

    def test_the_simple_route_comes_first(self):
        """Invariant 6: "the simple one leads". The old iMessage row led with the
        copy-it-yourself steps and never reached the helper at all."""
        for key, label in NEEDS_THE_PERMISSION.items():
            with self.subTest(source=label):
                text = help_text(key)
                first_helper = text.lower().index("helper")
                self.assertLess(
                    first_helper, text.index(FALLBACK),
                    f"the {label} row explains the no-permission route before the "
                    f"simple one")

    def test_the_helper_route_names_its_grant(self):
        """NC-191. Apple Contacts said the helper only had to be "installed", and
        never mentioned Full Disk Access — which is a separate grant nothing
        prompts for, so "installed" was the state in which Contacts quietly never
        arrived. The price of the simple route belongs in the simple route."""
        for key, label in NEEDS_THE_PERMISSION.items():
            with self.subTest(source=label):
                text = help_text(key)
                helper_route = text[:text.index(FALLBACK)]
                self.assertIn("Full Disk Access", helper_route,
                              f"the {label} row offers the helper without saying "
                              f"it needs Full Disk Access")


class TestWhatsAppNamesTheDialog(unittest.TestCase):
    """NC-192. WhatsApp's row promised "no special permissions". It needs no Full
    Disk Access — but since macOS 15 reading it in place raises a consent dialog
    that does not durably stick (importers.helper_covers_whatsapp), which is the
    exact surprise the wizard's soft-ask rule exists to prevent."""

    def setUp(self):
        self.text = help_text("whatsapp")

    def test_it_no_longer_promises_no_permissions(self):
        for claim in ("no special permissions", "readable in place, nothing"):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, self.text)

    def test_the_route_without_the_helper_names_the_dialog(self):
        self.assertIn(FALLBACK, self.text)
        after = self.text[self.text.index(FALLBACK):]
        self.assertIn("macOS dialog", after)
        self.assertIn("asks again", after,
                      "the answer does not stick, and the row has to say so")

    def test_the_helper_route_comes_first(self):
        self.assertLess(self.text.lower().index("helper"), self.text.index(FALLBACK))


class TestIMessageSaysWhereTheSwitchIs(unittest.TestCase):
    """The two routes are chosen with one switch — the helper's access in System
    Settings — and the app notices on its own which route produced the copy
    (importers.auto_copy_label). None of that helps a reader who is not told."""

    def setUp(self):
        self.text = help_text("imessage")

    def test_it_says_the_two_routes_are_switchable(self):
        self.assertIn("switch", self.text.lower())
        self.assertIn("System Settings", self.text)

    def test_the_manual_steps_are_still_all_there(self):
        """Relaxing the rule must not cost the fallback its instructions."""
        after = self.text[self.text.index(FALLBACK):]
        for step in ("Open the Messages folder", "⌘C", "Open the import folder",
                     "write-ahead log"):
            with self.subTest(step=step):
                self.assertIn(step, after,
                              f"the no-permission route lost a step: {step!r}")

    def test_it_no_longer_calls_not_asking_a_virtue(self):
        self.assertNotIn("never asks for the permission that would", self.text)

    def test_the_grant_is_still_scoped_to_the_helper(self):
        """What survived the reversal: the permission goes to the small helper,
        never to the app. Same one grant; much less exposed."""
        self.assertIn("never to this app", self.text)


class TestNothingStillStatesTheOldRuleAsFact(unittest.TestCase):
    """Quoting the old rule is how a correction explains itself. Stating it is how
    the old rule creeps back in. The difference this checks is whether the quote
    is tagged: an occurrence is allowed only with NC-123 within a few lines, which
    every history note about the reversal carries."""

    # NC-190 added two. The welcome screen's "never asks macOS for access to your
    # whole disk" is a fourth wording of the same rule, on the one screen every
    # new user reads, and the first three phrases do not match it. And
    # api_fda.py quotes the rule in lower case, which a case-sensitive match
    # cannot see.
    OLD_RULE = (
        "No Full Disk Access, ever",
        "no Full Disk Access, ever",
        "never asks for the permission that would",
        "permanently out of bounds",
        # The claim, not the words around it: packaging/src/imessage_sync.swift
        # rightly says the grant gives the helper "read access to your whole disk".
        "never asks macOS for access",
    )
    ROOTS = ("crm", "static", "tests", os.path.join("packaging", "src"), "docs")
    FILES = ("README.md",
             os.path.join("packaging", "install_imessage_sync.sh"),
             os.path.join("packaging", "IMESSAGE_SYNC.md"))

    def candidates(self):
        found = []
        for root in self.ROOTS:
            for dirpath, _dirs, names in os.walk(os.path.join(REPO, root)):
                for name in names:
                    if name.endswith((".py", ".js", ".swift", ".sh", ".md", ".css")):
                        found.append(os.path.relpath(os.path.join(dirpath, name), REPO))
        found += [rel for rel in self.FILES if os.path.exists(os.path.join(REPO, rel))]
        # A file the checkout keeps but never publishes may record the old rule
        # as it stood; only what is published has to say the current one.
        skip = not_exported(found, REPO)
        return [os.path.join(REPO, rel) for rel in found if rel not in skip]

    def test_the_old_rule_survives_only_as_tagged_history(self):
        me = os.path.abspath(__file__)
        offenders = []
        for path in self.candidates():
            if os.path.abspath(path) == me:
                continue            # this file has to name the phrases it bans
            lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
            for i, line in enumerate(lines):
                if not any(p in line for p in self.OLD_RULE):
                    continue
                near = "\n".join(lines[max(0, i - 4):i + 5])
                if "NC-123" in near:
                    continue
                offenders.append(f"{os.path.relpath(path, REPO)}:{i + 1}  "
                                 f"{line.strip()[:90]}")
        self.assertEqual(offenders, [], "\n".join([
            "", "These still state the no-Full-Disk-Access rule that was reversed",
            "(NC-123). Either rewrite them, or tag the quote with NC-123 so it reads",
            "as history:", *offenders]))

    # NC-290 item 7: the rules are in docs/EDITING.md, which numbers them the
    # same way.
    def rule_six(self):
        with open(os.path.join(REPO, "docs", "EDITING.md"), encoding="utf-8") as fh:
            guide = fh.read()
        m = re.search(r"^6\. (.*?)^7\. ", guide, re.S | re.M)
        self.assertIsNotNone(m, "rule 6 has moved")
        return m.group(1)

    def test_invariant_six_names_both_routes_and_the_switch(self):
        six = " ".join(self.rule_six().split())
        for needed in ("With the sync helper", "Without it",
                       "never to Network CRM.app", "uninstall_imessage_sync.sh"):
            with self.subTest(needed=needed):
                self.assertIn(needed, six)

    def test_invariant_six_says_what_the_switch_is_and_both_its_positions(self):
        """A switch is only a switch if both positions are written down. Checking
        for the word "switch" would not do, since the invariant uses it twice —
        deleting the sentence that says WHAT the switch is would change nothing,
        and the mutation would escape."""
        six = self.rule_six()
        m = re.search(r"\*\*The switch is (.*?)\*\*(.*?)(?:\n\n|\Z)", six, re.S)
        self.assertIsNotNone(m, "invariant 6 no longer says what the switch is")
        body = " ".join(m.group(2).split())
        self.assertIn("On:", body, "the switch's on position is not written down")
        self.assertIn("Off:", body, "the switch's off position is not written down")
        self.assertIn("System Settings", body,
                      "the off position does not say where to find it")


class TestTheWelcomeScreenNamesBothRoutes(unittest.TestCase):
    """NC-190. The first screen of a fresh install is where the soft-ask rule
    (static/app/onboarding.js's header) says the app states what it will need —
    so it is where both ways in have to be named, the simple one first."""

    def setUp(self):
        with open(os.path.join(REPO, "static", "app", "onboarding.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        start = js.index("function StepHello(")
        self.hello = js[start:js.index("\nfunction ", start + 1)]
        m = re.search(r'label="What it needs\.">(.*?)</\$\{Fact\}>', self.hello, re.S)
        self.assertIsNotNone(m, "the welcome screen no longer has a 'What it needs.' line")
        self.needs = " ".join(m.group(1).split())

    def test_it_names_the_helper_and_its_grant(self):
        self.assertIn("helper", self.needs)
        self.assertIn("Full Disk Access", self.needs)

    def test_it_says_the_app_itself_never_holds_the_grant(self):
        self.assertIn("this app itself never gets it", self.needs)

    def test_it_keeps_the_route_that_needs_no_permission(self):
        self.assertIn("copy or export yourself", self.needs)

    def test_the_simple_route_comes_first(self):
        self.assertLess(self.needs.index("helper"),
                        self.needs.index("copy or export yourself"))


class TestTheWizardContactsStepNamesTheHelpersCopy(unittest.TestCase):
    """NC-191. The wizard's Contacts step opened with "This reads only the vCard
    file you pick" — written before the helper copied Contacts, and contradicted
    by the "Found on this Mac" row under it offering the helper's copy."""

    def setUp(self):
        with open(os.path.join(REPO, "static", "app", "onboarding.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        start = js.index("function StepContacts(")
        step = js[start:js.index("\nfunction ", start + 1)]
        m = re.search(r'<p class="onb-privacy">(.*?)</p>', step, re.S)
        self.assertIsNotNone(m, "the Contacts step no longer says what it reads")
        self.reads = " ".join(m.group(1).split())

    def test_it_names_the_helpers_copy_first(self):
        self.assertIn("sync helper", self.reads)
        self.assertLess(self.reads.index("sync helper"),
                        self.reads.index("file you pick"))

    def test_the_picked_file_is_not_the_only_thing_it_reads(self):
        self.assertNotIn("reads only the", self.reads)


class TestTheWizardsIMessageStepLeadsWithTheHelper(unittest.TestCase):
    """NC-193. iMessage's help text says the helper is set up from "the panel at
    the top of this page". The wizard shows that same text, and had the panel at
    the bottom, under the copy-it-yourself steps — so the pointer led nowhere and
    the simple route came second, on the screen that introduces it."""

    def setUp(self):
        with open(os.path.join(REPO, "static", "app", "onboarding.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        start = js.index("function StepMessages(")
        self.step = js[start:js.index("\nfunction ", start + 1)]

    def test_the_premise_the_help_still_points_at_the_top(self):
        self.assertIn("the panel at the top of this page", help_text("imessage"),
                      "the help no longer says where the panel is; if it moved, "
                      "check this test still guards the right thing")

    def test_the_panel_comes_before_the_manual_steps(self):
        body = self.step[self.step.index("return html`"):]
        self.assertLess(body.index("<${FdaPanel}"), body.index("spec.export_help"),
                        "the helper panel is below the help that says it is at "
                        "the top")


if __name__ == "__main__":
    unittest.main()
