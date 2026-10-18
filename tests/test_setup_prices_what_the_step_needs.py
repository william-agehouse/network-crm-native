"""NC-186. No setup step may read as cheaper than what doing it actually takes.

The runbook at the top of the Data page priced Apple Contacts and Calendar at
"one tap". Both taps only work once the sync helper is installed and holds Full
Disk Access, and that grant was priced two rows later, at iMessage. So on the
first screen a new install sees, the two rows that looked cheapest were the two
hiding what they cost.

The price now comes from the helper's own report (via api_fda, which owns it):

* the helper already copies the source  → the plain price ("one tap") is true;
* it does not, and the source needs Full Disk Access → the FIRST such step not yet
  done carries the grant and the "needs a permission" tag, the later ones say
  "same grant as …" instead of charging for it again;
* this build ships no helper → the way in that needs no permission is the price;
* WhatsApp never needed Full Disk Access, so it never says it does.

And every step priced at the grant names the way in that needs no permission,
because invariant 6 says the help describes both routes.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db, importers  # noqa: E402

import api_setup  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRANT_STEPS = ("contacts", "calendar", "imessage")


def report(**outcomes):
    """A helper report as the Swift helper writes it: one row per source label."""
    return {"ran_at": 0, "sources": [{"label": label, "outcome": outcome}
                                     for label, outcome in outcomes.items()]}


class _Setup(Base):
    installer = "/x/install_imessage_sync.sh"   # this build ships a helper
    installed = False
    helper_report = {}                            # ...that has never reported

    def progress(self):
        with mock.patch.object(importers, "imsg_helper_installer",
                               lambda: self.installer), \
             mock.patch.object(importers, "imsg_helper_installed",
                               lambda: self.installed), \
             mock.patch.object(importers, "imsg_helper_report",
                               lambda: self.helper_report):
            out = api_setup.handle_setup_progress(self.conn)
        return {s["key"]: s for s in out["steps"]}


class TestAFreshInstallWithNoHelper(_Setup):
    """A fresh install: nothing imported, helper not set up."""

    def test_no_helper_step_claims_one_tap(self):
        steps = self.progress()
        for key in GRANT_STEPS + ("whatsapp",):
            with self.subTest(step=key):
                self.assertNotEqual(steps[key]["cost"], "one tap",
                                    f"{key} says one tap, but nothing has copied it "
                                    f"yet, so the tap has nothing to import")

    def test_the_grant_is_priced_at_the_first_step_that_needs_it(self):
        steps = self.progress()
        self.assertEqual(steps["contacts"]["cost"], api_setup.GRANT_PRICE)
        self.assertTrue(steps["contacts"]["heavy"],
                        "the step carrying the grant must carry the permission tag")

    def test_the_grant_is_charged_once_not_three_times(self):
        steps = self.progress()
        self.assertEqual([k for k in GRANT_STEPS if steps[k]["heavy"]], ["contacts"])
        for key in ("calendar", "imessage"):
            with self.subTest(step=key):
                self.assertEqual(steps[key]["cost"], "same grant as Apple Contacts")

    def test_whatsapp_no_longer_claims_to_need_the_grant(self):
        steps = self.progress()
        self.assertNotIn("grant", steps["whatsapp"]["cost"])
        self.assertIn("no Full Disk Access", steps["whatsapp"]["cost"])
        self.assertFalse(steps["whatsapp"]["heavy"])

    def test_whatsapp_names_the_dialog_it_costs_without_the_helper(self):
        """NC-192: "at no extra permission" was true of Full Disk Access and
        false of the macOS dialog that reading WhatsApp in place raises."""
        steps = self.progress()
        self.assertNotIn("no extra permission", steps["whatsapp"]["gives"])
        self.assertIn("macOS asks", steps["whatsapp"]["how"])
        self.assertIn("sync helper", steps["whatsapp"]["how"])

    def test_every_grant_step_names_the_way_in_that_needs_no_permission(self):
        """Invariant 6: both routes, not only the simple one."""
        steps = self.progress()
        for key in GRANT_STEPS:
            with self.subTest(step=key):
                how = steps[key]["how"]
                self.assertIn("sync helper", how)
                self.assertIn("never to this app", how)
                self.assertIn("with no permission at all", how)
        self.assertIn("Export vCard", steps["contacts"]["how"])
        self.assertIn("chat.db", steps["imessage"]["how"])

    def test_the_price_fits_the_row_it_is_drawn_on(self):
        """The title row does not wrap (`.sp-step-n { white-space: nowrap }`) and
        shares its width with the label and two tags, in a sidebar as narrow as
        340px. The long explanation belongs in `how`."""
        for key, s in self.progress().items():
            with self.subTest(step=key):
                self.assertLessEqual(len(s["cost"]), 30, s["cost"])


class TestWhenTheHelperIsCopying(_Setup):
    installed = True
    helper_report = report(**{importers.HELPER_CONTACTS: "copied",
                              importers.HELPER_CALENDAR: "current",
                              importers.HELPER_IMESSAGE: "current",
                              importers.HELPER_WHATSAPP: "copied"})

    def test_one_tap_is_true_and_says_so(self):
        steps = self.progress()
        for key in GRANT_STEPS + ("whatsapp",):
            with self.subTest(step=key):
                self.assertEqual(steps[key]["cost"], "one tap")
                self.assertFalse(steps[key]["heavy"])
                self.assertEqual(steps[key]["how"], "")


class TestWhenMacOSRefusesTheHelper(_Setup):
    """Installed but never granted — the most common state right after installing,
    because nothing prompts for Full Disk Access."""
    installed = True
    helper_report = report(**{importers.HELPER_CONTACTS: "blocked",
                              importers.HELPER_CALENDAR: "blocked",
                              importers.HELPER_IMESSAGE: "blocked",
                              importers.HELPER_WHATSAPP: "copied"})

    def test_the_blocked_steps_are_priced_at_the_grant(self):
        steps = self.progress()
        self.assertEqual(steps["contacts"]["cost"], api_setup.GRANT_PRICE)
        self.assertTrue(steps["contacts"]["heavy"])
        self.assertEqual(steps["whatsapp"]["cost"], "one tap",
                         "WhatsApp copies without the grant, so it really is one tap")


class TestContactsDoneByHand(_Setup):
    """Contacts imported from an exported card file, helper still not set up: the
    grant has NOT been paid, so the next step that needs it has to carry it —
    "same grant as Apple Contacts" would claim a payment that never happened."""

    def test_calendar_carries_the_grant_once_contacts_is_done(self):
        db.create_contact(self.conn, "Ada Lovelace")
        steps = self.progress()
        self.assertTrue(steps["contacts"]["done"])
        self.assertFalse(steps["contacts"]["heavy"])
        self.assertEqual(steps["calendar"]["cost"], api_setup.GRANT_PRICE)
        self.assertTrue(steps["calendar"]["heavy"])
        self.assertEqual(steps["imessage"]["cost"], "same grant as Calendar")


class TestABuildThatShipsNoHelper(_Setup):
    installer = ""   # a bundle whose helper did not compile or sign (appify.sh warns
    #                  and ships it anyway, except as a handout, NC-311)

    def test_the_way_in_that_needs_no_permission_is_the_price(self):
        steps = self.progress()
        for key in GRANT_STEPS:
            with self.subTest(step=key):
                self.assertNotIn("Full Disk Access", steps[key]["cost"])
                self.assertNotIn("grant", steps[key]["cost"])
                self.assertFalse(steps[key]["heavy"])
                self.assertNotIn("sync helper", steps[key]["how"],
                                 "a build with no helper must not offer one")
        self.assertEqual(steps["contacts"]["cost"], "export by hand")
        self.assertIn("macOS asks", steps["whatsapp"]["how"])
        self.assertNotIn("sync helper", steps["whatsapp"]["how"])
        self.assertTrue(steps["contacts"]["how"].startswith("Export a card file"))


class TestAFailedProbeOverstatesRatherThanUnderstates(_Setup):
    def test_a_broken_helper_probe_prices_the_grant(self):
        import api_fda

        def boom():
            raise RuntimeError("probe failed")
        with mock.patch.object(api_fda, "handle_fda_status", boom):
            out = api_setup.handle_setup_progress(self.conn)
        steps = {s["key"]: s for s in out["steps"]}
        self.assertEqual(steps["contacts"]["cost"], api_setup.GRANT_PRICE)


# NC-236: the setup panel (setup-progress.js) that drew these prices went with
# the old Data page. The prices above are still the server's own (api_setup), and
# the Data page draws its rows' "how" and "the other way in" text from the
# ledger; the two checks below keep the same rules on the new page.
class TestTheDataPageDrawsBothRoutes(unittest.TestCase):
    def _js(self, name):
        with open(os.path.join(REPO, "static", "app", name), encoding="utf-8") as fh:
            return fh.read()

    def test_a_row_shows_how_and_the_way_in_that_needs_no_permission(self):
        js = self._js("data.js")
        self.assertIn("how=${d.help && html`<${Help} text=${d.help} />`}", js,
                      "the server sends help for each row and the page never draws it")
        self.assertIn("other=${d.manual && html`<p>${d.manual}</p>`}", js,
                      "the way in that needs no permission is not drawn")

    def test_no_price_is_hardcoded_in_the_page(self):
        """The price is the server's decision from the helper's report. A literal
        in the page is how "one tap" would come back without anyone noticing."""
        for name in ("data.js", "ledger.js", "settings.js"):
            js = self._js(name)
            for literal in ("one tap", "Full Disk Access, once", "same grant"):
                with self.subTest(file=name, literal=literal):
                    self.assertNotIn(literal, js)


if __name__ == "__main__":
    unittest.main()
