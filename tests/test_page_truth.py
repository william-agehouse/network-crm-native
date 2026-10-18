"""NC-076 gate: the Data page may not assert what its gate does not prove.

WHY THIS FILE EXISTS, AND WHY IT IS NOT FOUR ASSERTIONS

The Data page made four false claims at once — "All sources connected",
"Every source is connected, so that is the real answer rather than missing
data", "No source needs you now.", and a scraper progress bar under a headline
about messages — while a panel between them said LinkedIn messages were not
being collected. Every number on that page was correct. Every sentence about
those numbers was wrong.

These are instances of one defect class in this codebase, a true number under
a sentence that claims more:

    NC-035  `imsg_helper_installed()`  proved a file exists   -> "sync is working"
    NC-061  a file's mtime             proved a write happened-> "this is up to date"
    NC-067  "No file needed"           proved nothing         -> "no setup required"
    NC-068  `token_present`            proved a string is set -> "LinkedIn is up to date"
    NC-070  a never-shrink guard       preserved a COUNT      -> "you have N messages"
    NC-076  `have > 0`                 proved it ever ran     -> "connected", present tense

A recurring class is a missing gate, not a run of unlucky bugs. So most of what
follows tests the SHAPE that stops the next instance, not the four sentences that
were wrong:

  * TestEveryPanelReportsWhetherItIsAsking is the structural one. The page prints
    a page-wide verdict, so every self-contained panel on it must report whether
    it is asking for something. Add another panel that fetches its own status
    and forgets to, and this fails — which is the case the four hand-written
    assertions below would sail straight past.

  * TestNothingCertifiesCompletenessWithoutCheckingIt guards the specific move
    that made the headline the worst line on the page: not merely claiming
    everything was connected, but using that claim to certify a DIFFERENT number
    as trustworthy.

A gate scoped to one file misses the same defect in another file. Hence: these
read every module of the page, not one.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, js_code_of, py_block, up_to  # noqa: E402,F401 — Base bootstraps CRM_DATA_DIR

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")
CRM = os.path.join(REPO, "crm")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def py_code(text, start, end=None):
    """The CODE of a Python span — docstrings and `#` comments removed.

    The same rule as strip_comments below: a `configured()` gate that reads
    docstrings fires on the very docstring explaining WHY configured() must not
    be used. A gate that fires on prose is the source-reading-its-own-words trap
    NC-056 hit, and it teaches you to delete the explanation rather than read the
    gate.
    """
    body = text[text.index(start):]
    body = body[:body.index(end)] if end else body
    body = re.sub(r'"""[\s\S]*?"""', " ", body)
    return re.sub(r"(?m)#.*$", " ", body)


def assignment(js, name):
    """The whole `const <name> = ...;` statement, terminator included.

    A character window lets a mutation walk straight through it: `const asks =
    false;` still has the real field name in the next 300 characters of unrelated
    code, so the gate passes while the value it guards has been hardcoded. Scope
    the read to the statement or the gate is reading the neighbourhood, not the
    expression.
    """
    i = js.index(f"const {name} =")
    return js[i:js.index(";", i) + 1]


def strip_comments(js):
    """Block and line comments out, so a gate can never be satisfied — or
    tripped — by prose. Every string below is checked against CODE.

    Deliberately crude: it does not understand comment-like text inside string
    literals. That direction is safe here, because it can only make a gate
    stricter (a needed string vanishing fails loudly), never quietly looser.
    """
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", js)


# NC-236: the "every panel reports whether it is asking" checks went with the old
# Data page. That page worked out its own verdict in the browser from panels that
# each fetched their own status, so each panel had to report upward. The page that
# replaced it decides nothing: the server works out what needs you (crm/api_ledger.py
# _needs) and the page draws it. So the same rule is pinned where it now lives,
# below: a source in use that cannot collect is a need, and the quiet line is only
# drawn when there are no needs and the scan has answered.
class TestThePageVerdictIsTheServers(unittest.TestCase):
    def setUp(self):
        self.ledger = read(CRM, "api_ledger.py")
        self.needs = py_code(self.ledger, "def _needs(", "# 3. Your name and email")
        self.page = strip_comments(read(APP, "data.js"))

    def test_a_source_in_use_that_cannot_collect_is_a_need(self):
        self.assertRegex(
            self.needs, r'r\["in_use"\] and r\["state"\]\["tone"\] in \("blocked", "behind", "ready"\)',
            "a source that is in use and stopped, behind or waiting for a yes is "
            "no longer listed under Needs you, so the page can say nothing needs "
            "you while a LinkedIn token is dead or macOS is refusing a folder")

    def test_the_quiet_line_is_only_drawn_when_there_are_no_needs(self):
        self.assertRegex(
            self.page, r"needs\.length > 0\s*\?[\s\S]*?:\s*html`<\$\{AllClear\}",
            "the page's quiet line is no longer behind the list of needs being empty")

    def test_the_quiet_line_waits_for_the_scan(self):
        body = self.page[self.page.index("function AllClear"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("!data.scanned", body)
        self.assertLess(body.index("!data.scanned"), body.index("all working"),
                        "the page says every source works before the check of this "
                        "Mac for files has answered")


class TestEveryPanelThatCanAskDeclaresIt(unittest.TestCase):
    """A panel reporting `onAttention` is only half of it — the value it reports
    has to be a real answer. Both panels decide it from an explicit per-state
    table, never from the tone or the icon, because the two come apart: LinkedIn's
    `cooldown` is a warning whose own body says "Nothing here needs doing"."""

    # NC-236: "LinkedIn declares asks for every state" went with the old LinkedIn
    # panel, whose states each carried an `asks` flag for the page to read. The
    # server now gives each row one tone, and Needs you lists only blocked,
    # behind and ready ones.
    def test_the_cooldown_state_is_not_treated_as_a_demand(self):
        """The one state whose copy says to do nothing. LinkedIn pausing the
        account is a "paused" row, and "paused" is deliberately not one of the
        tones that put a row under Needs you: if it were, the page would nag
        during a LinkedIn block, which is how an account gets poked while it is
        rate-limited."""
        ledger = read(CRM, "api_ledger.py")
        i = ledger.index('if (scraper.get("state") or {}).get("in_cooldown"):')
        self.assertIn('"tone": "paused"', ledger[i:i + 200],
                      "a paused LinkedIn account is no longer a paused row")
        needs = py_code(ledger, "def _needs(", "# 2. Automatic importing")
        self.assertNotIn('"paused"', needs,
                         "a paused row now counts as something that needs you")

    def test_each_reported_answer_is_derived_from_real_status(self):
        """Reporting `onAttention` is not enough — the VALUE has to come from the
        component's own data. Hardcoding a panel's answer to `false` restores the
        original bug in full while every other assertion here stays green. So each
        module's `asks` expression must reference the field that actually decides
        it."""
        # NC-236: the setup-progress and linkedin panels are gone; fda is the
        # one panel that still reports upward.
        for mod, field in (("fda", "status.state"),):
            with self.subTest(module=mod):
                js = strip_comments(read(APP, mod + ".js"))
                self.assertIn("const asks =", js,
                              f"{mod}.js no longer computes an `asks` value")
                expr = assignment(js, "asks")
                self.assertIn(
                    field, expr,
                    f"{mod}.js reports whether it is asking the user for something "
                    f"without reading {field} — a constant answer would satisfy "
                    f"every other gate in this file while the page went back to "
                    f"contradicting itself")

    def test_fda_decides_from_states_not_from_a_tone(self):
        js = strip_comments(read(APP, "fda.js"))
        self.assertIn("const asks =", js, "FdaPanel no longer computes `asks`")
        expr = assignment(js, "asks")
        for state in ("blocked", "outdated", "quiet"):
            self.assertIn(state, expr,
                          f"FdaPanel's `asks` no longer accounts for the "
                          f"'{state}' state")


# NC-236: the "nothing certifies completeness" checks (the "Every source is
# connected" sentence, the N of M sources counter, the bar that could only be
# full, "keeps running") went with the old Setup progress panel. The new page has
# no sources-connected counter, no bar and no such sentence; its only reassuring
# line, "all working", is pinned above (TestThePageVerdictIsTheServers) to come
# after the scan and only when nothing is under Needs you.


class TestTheServerCanSayConnectedButNotCollecting(unittest.TestCase):
    """`done` means "ever produced a row" and must keep meaning that; `blocked`
    is the second fact the UI needed and did not have. Both endpoints that
    describe a source carry it, because SourceRow is rendered from six places
    and a fact threaded as a prop reaches five of them."""

    def test_setup_progress_reports_blocked_per_step_and_in_total(self):
        py = read(CRM, "api_setup.py")
        self.assertIn('"blocked"', py, "steps no longer carry a blocked reason")
        self.assertIn('"blocked_count"', py,
                      "the response no longer totals blocked sources, so the "
                      "headline cannot know whether it may certify anything")
        self.assertIn('"done": have > 0', py,
                      "`done` has changed meaning. It is allowed to mean 'ever "
                      "produced a row' — that is what `blocked` exists to "
                      "complement — but if it now means something else, the "
                      "sentences downstream of it all need re-reading")

    def test_the_sources_list_carries_blocked_too(self):
        """Two claims, deliberately separate, and neither is the whole line.

        Asserting the expression verbatim — `s["blocked"] = blocked.get` — would
        fail on an improvement: a waiting Google sign-in is a blocker too
        (NC-177), and it takes precedence over the probe. A gate that fails on
        an improvement gets deleted rather than obeyed, so what is pinned here is
        what has to stay true: the row carries a `blocked` key, and the probes
        are still what fills it."""
        py = read(CRM, "api_import.py")
        self.assertIn('s["blocked"] = ', py,
                      "/api/import/sources no longer marks blocked sources, so "
                      "every SourceRow is back to reading a success timestamp as "
                      "proof the source still works")
        self.assertIn('blocked.get(s["key"]', py,
                      "the row no longer reads _source_blockers(), so a lane a "
                      "probe knows is dead still looks alive on the Data page")

    def test_blockers_come_from_the_module_that_owns_the_decision(self):
        """Two places deciding the same thing is how they come to disagree. Both
        callers ask the owner; the owner is the only one with the rule."""
        for mod in ("api_setup.py", "api_import.py"):
            with self.subTest(module=mod):
                py = read(CRM, mod)
                self.assertIn("api_linkedin.collection_blocked()", py,
                              f"{mod} no longer asks api_linkedin whether its "
                              f"lane can run — check it has not reimplemented "
                              f"the token rule locally")
                self.assertIn("api_fda.collection_blocked()", py,
                              f"{mod} no longer asks api_fda")

    def test_a_blocker_is_only_reported_when_something_proves_it(self):
        """The fix must not become the bug pointing the other way. `not
        installed` is NOT `blocked`: invariant 6 keeps the route that needs no
        permission — the user copying the file themselves — fully supported beside
        the helper, so plenty of installs import iMessage with no helper at all."""
        body = py_code(read(CRM, "api_fda.py"), "def collection_blocked()", "def _tail")
        self.assertIn('!= "blocked"', body,
                      "api_fda.collection_blocked no longer filters to the one "
                      "outcome that proves macOS is refusing")
        self.assertNotIn('"not_installed"', body,
                         "a missing helper is being reported as a blocked source, "
                         "which would flag every install that imports by hand")

    def test_linkedin_does_not_use_configured_which_needs_a_client_secret(self):
        """`linkedin_token.configured()` is True only with a client id AND secret
        AND token. Those two were taken off the Settings screen in NC-073 and are
        optional — a token alone collects messages. Using configured() here would
        report every install without them as blocked."""
        body = py_code(read(CRM, "api_linkedin.py"),
                       "def collection_blocked()", "def _utc_now")
        self.assertNotIn("configured()", body,
                         "collection_blocked uses configured(), which also "
                         "demands a client id and secret — installs that have "
                         "only a token would be reported as not collecting")
        self.assertIn("LINKEDIN_TOKEN", body,
                      "collection_blocked no longer checks for the one value "
                      "that actually decides whether the lane can run")


class TestAFigureNamesThePopulationItCounted(unittest.TestCase):
    """A number under a label that describes something else. The reader attributes
    the number to the label, and the number is not wrong, so nothing ever catches
    it."""

    # NC-236: the scraper bar's caption ("not messages") and the "nothing since"
    # recency line went with the old LinkedIn panel; the new page has neither
    # the coverage bar nor a newest-message line (the row only mentions the age
    # when the message lane has gone silent).
    def test_the_two_linkedin_populations_both_say_which_they_are(self):
        """Two figures, the contacts whose source is the export and the people
        you messaged, render inches apart on one page. Both are true. Each figure
        names its own population in the server's answer."""
        ledger = read(CRM, "api_ledger.py")
        self.assertIn('_fig(n, "people", "from the export")', ledger,
                      "the download lane is back to a bare 'N people', which sits "
                      "on the same page as a different, larger, also-bare count")
        self.assertIn('_fig(n_people, "people", "you messaged")', ledger,
                      "the message lane no longer says which people it counts")

    def test_the_data_page_row_keeps_saying_with_history(self):
        """The row's "N with history" for LinkedIn is correct, and pinned so it
        is not "fixed" back.

        It is tempting to say it is wrong, on the grounds that the coverage map
        counts linkedin_url. It does not.
        The Data page's coverage comes from api_contacts._channel_coverage(),
        which is SUM(msg_count > 0) — real message history, a smaller group than
        the people with a link.

        So "with history" is correct for the WhatsApp and iMessage rows and a
        per-source noun table would make it wrong. The two coverage maps share
        key names and mean different things."""
        self.assertIn(
            "SUM(msg_count  > 0) AS linkedin",
            read(CRM, "api_contacts.py"),
            "_channel_coverage no longer counts LinkedIn by message volume. If it "
            "now counts linkedin_url instead, a row saying 'with history' has "
            "become false and this whole test needs rewriting")
        ledger = read(CRM, "api_ledger.py")
        self.assertIn("api_contacts._channel_coverage(conn)", ledger,
                      "the ledger no longer takes its per-channel people from "
                      "_channel_coverage")
        self.assertEqual(ledger.count('_fig(n_people, "people", "with history")'), 2,
                         "the WhatsApp and iMessage rows no longer both say "
                         "'with history'")

    def test_the_calendar_step_counts_calendar_things(self):
        py = read(CRM, "db.py")
        i = py.index('out["meetings"]')
        sql = py[i:i + 400]
        self.assertIn("kind = 'meeting'", sql,
                      "the Calendar step counts every row in contact_dates again, "
                      "so a hand-typed birthday marks Calendar as a connected "
                      "source")
        self.assertIn("source = 'calendar'", sql,
                      "the Calendar step counts dates the address book wrote, not "
                      "only the ones the calendar wrote")

    def test_no_unit_claims_a_freshness_the_query_never_checks(self):
        """"current role" was a freshness claim over `position <> '' OR company
        <> ''`, which has no reference to any updated-at column."""
        py = read(CRM, "api_setup.py")
        self.assertNotIn("people with a current role", py,
                         "the enrichment unit claims the role is current, but the "
                         "query only proves a role was ever recorded")


class TestHidingIsNotDescribedAsADelete(unittest.TestCase):
    """NC-152. Two-sided, and it has to be.

    The Data page opens with "No data leaves it." Under it now sits a caveat
    saying that hiding someone removes them from every view, search and AI
    answer but not from the files under data/ — because the search index is
    built over everyone on purpose, so the app and the command line rank the
    same way, and hidden people are dropped from the RESULTS, not the cache.

    That sentence is true only while the code keeps working that way. So this
    pins both halves against each other: remove the sentence and the page goes
    back to implying hidden means gone; make the cache exclude hidden people and
    the sentence becomes a lie the page tells about itself. Either edit fails
    here, and the failure names the other half.

    The old tooltip — "Names hidden from the app everywhere" — is the specific
    wording that caused this. It was accurate about the app, and read as gone.
    """

    def test_the_data_page_says_hiding_is_a_filter_not_a_delete(self):
        js = js_code_of(os.path.join(APP, "data.js"))
        self.assertIn("filter, not a delete", js,
                      "the Data page no longer qualifies 'No data leaves it'. "
                      "Without that sentence the page implies hiding removes "
                      "someone from data/, which crm/search_cli.py does not do")

    def test_the_caveat_sits_with_the_figure_it_qualifies(self):
        """A caveat three screens below the claim is not a caveat. The old page
        put it under "No data leaves it"; the new one puts it directly under the
        People figure, which is the number hiding changes."""
        js = js_code_of(os.path.join(APP, "data.js"))
        block = up_to(js, 'label="People"', 'label="Messages"')
        self.assertIn("filter, not a delete", block,
                      "the hiding caveat has drifted away from the People figure "
                      "in This install")

    def test_the_caveat_names_the_folder_and_the_index(self):
        js = js_code_of(os.path.join(APP, "data.js"))
        block = up_to(js, 'label="People"', 'label="Messages"')
        self.assertIn("data folder", block,
                      "the caveat no longer says WHERE the rows stay, so a reader "
                      "cannot tell what a backup of the data folder would carry")
        self.assertIn("search index", block,
                      "the caveat no longer names the search index, which is the "
                      "one file people assume a hide must have rebuilt")

    def test_the_hidden_tooltip_does_not_promise_a_delete(self):
        js = js_code_of(os.path.join(APP, "people.js"))
        self.assertNotIn("hidden from the app everywhere", js.lower(),
                         "the Hidden control is back to 'hidden from the app "
                         "everywhere'. True of every read path and heard as "
                         "'gone' — NC-152")
        self.assertIn("filter, not a delete", js,
                      "the Hidden control's tooltip no longer says what hiding "
                      "actually is, so the Data page's caveat is the only place "
                      "it is stated and the control itself still over-promises")

    def test_the_index_really_is_built_over_everyone(self):
        """The half that makes the sentence true. If this ever changes, the page
        copy above is the thing to rewrite — not this assertion.

        Anchored on `semantic_rank_many`, which is where the ranking actually
        happens since NC-129 gave semantic_search a list of phrasings.
        `semantic_rank` is now a one-item wrapper around it, so a gate pointing
        there would be reading a two-line function and proving nothing.
        """
        py = py_block(read(CRM, "search_cli.py"), "def semantic_rank_many(")
        self.assertIn("SELECT id FROM contacts ORDER BY id", py,
                      "the vectors are no longer built over every contact. If "
                      "the cache now excludes hidden people, the Data page's "
                      "'built over everyone' sentence has become false")
        ids_at = py.index("SELECT id FROM contacts")
        excl_at = py.index("ids_outside")
        self.assertLess(ids_at, excl_at,
                        "hidden people are now filtered before the vectors are "
                        "loaded rather than out of the results — same rewrite "
                        "needed on the Data page")


if __name__ == "__main__":
    unittest.main()
