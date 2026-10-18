"""NC-118 gate: a control that appears and then vanishes is a screen lying.

`canCheck` was `!info || info.configured !== false`, and `info` starts `null`.
So a disabled "Checking…" button rendered on every page load, then deleted
itself a moment later when the answer came back — because the answer is always
`configured: false`.

That "always" is by design ([[NC-020]]): the checker has no default repository,
so an install with nowhere to ask does not ask, and says so in the UI. A default
pointing at a repository with no releases would make every install fire one GET
a day at api.github.com that could only 404, forever, invisibly, from an app
whose whole pitch is that it works offline. `tests/test_api_instance.py` pins the
empty default, so giving NC_UPDATE_REPO a real default is not the fix.

The fix is the honest-state rule: nothing renders until its own state is known,
and every update control is gated on `configured === true` rather than on "not
yet known to be false".

Reading the file cannot tell `!info || x !== false` from `!!info && x === true` —
both are one expression with the right words in them. So the two lines are sliced
out of `InstallFooter` and executed under node, once per shape `info` can have.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, js_code_of  # noqa: E402,F401

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


# NC-236: the node-executed checks of InstallFooter's canCheck / verNote / avail
# and the gates on "Check now", "Get version" and UpdateBanner went with the old
# Settings page. The rebuilt page draws no update control at all (no controls
# appear until releases are published) and never shows a
# "Checking..." state; it prints one sentence the server decides
# (updates_says). The two classes below pin that instead.


class TestTheRebuiltPageOffersNoUpdateControl(unittest.TestCase):
    """The old bug was a control that appeared and then deleted itself. The
    rebuilt page cannot do that while it has no control to draw."""

    def test_the_page_draws_no_update_control_and_asks_no_update_endpoint(self):
        js = js_code_of(os.path.join(APP, "settings.js"))
        for word in ("Check now", "Get version", "UpdateBanner", "/api/update",
                     "Checking"):
            self.assertNotIn(word, js,
                             f"{word!r} is back on the Settings page: an update "
                             "control must not render until its answer is known "
                             "and says releases are published (NC-118)")

    def test_the_sentence_comes_from_the_server_not_from_a_pending_state(self):
        js = js_code_of(os.path.join(APP, "settings.js"))
        self.assertIn("inst.updates_says", js)


class TestTheServerSaysPlainlyThatThisCopyDoesNotCheck(Base):

    def test_an_unconfigured_build_says_it_does_not_check(self):
        # Patch the updates module api_abilities itself imported. `from crm
        # import updates` is a second copy of the file under another name, so
        # patching it changed nothing api_abilities reads, and this test passed
        # only because the default happens to be empty.
        from crm import api_abilities
        updates = api_abilities.updates
        saved = updates.GITHUB_REPO
        try:
            updates.GITHUB_REPO = ""
            out = api_abilities._install(self.conn)
        finally:
            updates.GITHUB_REPO = saved
        self.assertIn("does not check", out["updates_says"])

    def test_a_handed_out_build_says_where_new_versions_are(self):
        """NC-315. The handout build stamps the public repository in, and
        Settings said nothing about it. It must not claim a daily check either:
        nothing in either app asks GET /api/update."""
        from crm import api_abilities
        updates = api_abilities.updates
        saved = updates.GITHUB_REPO
        try:
            updates.GITHUB_REPO = "someone/some-app"
            out = api_abilities._install(self.conn)
        finally:
            updates.GITHUB_REPO = saved
        self.assertIn("https://github.com/someone/some-app/releases", out["updates_says"])
        self.assertIn("replace the app; your data stays", out["updates_says"])
        self.assertNotIn("once a day", out["updates_says"])


class TestTheStockBuildReallyHasNowhereToAsk(Base):
    """The premise under all of the above — NC-020: the checker has no default repository."""

    def test_the_api_reports_configured_false(self):
        out = app.handle_update_get(self.conn, {})
        self.assertIs(out["configured"], False,
                      "a repo has been given a default again, which NC-020 "
                      "removed on purpose: every install then fires a daily GET "
                      "at GitHub that can only 404")


if __name__ == "__main__":
    unittest.main()
