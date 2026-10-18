"""Gates on the Intake SURFACE — the front end for crm/api_intake.py (NC-016).

WHY THIS FILE EXISTS, AND WHAT IT IS ANCHORED TO
------------------------------------------------
`crm/intake.py` and `crm/api_intake.py` can be complete, correct and
**unreachable**: every test in `tests/test_api_intake.py` passes and eight
routes answer over HTTP, yet with no button, no omnibox command and nothing in
onboarding the feature does not exist for the person the app is for — the same shape of failure
as `TestFdaPanelIsActuallyReachable` in tests/test_frontend_gates.py (a finished
permission flow that only ever printed to a Terminal) and as [[NC-015]] (three
import sources that had never imported anything while the page presented them as
working).

The gate against it is the first class below: **every route in
`api_intake.ROUTES` is called by the client**, with the route list derived from
the module at test time rather than copied here. A copied list is a second source
of truth that goes stale exactly when it matters — when a route is added.

Same house rules as tests/test_frontend_gates.py, and for the same reasons:

  · `js_code` (tests/helpers.py) first, ALWAYS. This repo's recurring trap is a
    source-grepping gate that matches its own explanation: intake.js documents
    all eight routes in its header comment, so a raw-source search for
    "/api/intake/commit" passes with every call site deleted.
  · assert COUNTS and SET EQUALITY, not "the string appears somewhere". Both
    directions matter: a route nobody calls is dead code, and a call to a
    path that is not a route is a typo that 404s silently.
  · every gate here is tied to a failure mode that can really happen. No preferences.

`TestTheStylesheetIsRegistered.test_the_intake_stylesheet_is_linked` is the only
gate that notices an unstyled page: static/index.html must link view-intake.css,
and weakening the test to make a missing link pass would delete it.
tests/test_frontend_gates.py's `test_every_view_stylesheet_is_linked` checks the
same line for every view.
"""

import os, sys  # noqa: E401 — the bootstrap shim below must run before any crm import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, js_code  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

import re  # noqa: E402
import unittest  # noqa: E402

import api_intake  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def _braced(src: str, start: int) -> str:
    """The {...} block beginning at or after `start`, by brace matching.

    Raises rather than returning "" so a renamed thing fails loudly instead of
    making the gate silently vacuous — the main way a source-level test rots.
    """
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"unbalanced braces at offset {start}")


def _api_calls(src: str):
    """Every `api("<literal path>", {...})` in the source, as (method, path, opts).

    Literal first argument only, and that is a constraint on the client as much
    as on this helper: `api("/api/intake/" + which, …)` would make the route a
    runtime string that no source-level gate can see, which is precisely how a
    route stops being called without anything noticing. A missing options object
    means GET, the same as api() itself.
    """
    out = []
    for m in re.finditer(r'api\(\s*"(/api/[^"]+)"\s*(,?)', src):
        path, comma = m.group(1), m.group(2)
        opts = _braced(src, m.end()) if comma else ""
        method = "GET"
        mm = re.search(r'method:\s*"([A-Z]+)"', opts)
        if mm:
            method = mm.group(1)
        out.append((method, path, opts))
    return out


class TestEveryIntakeRouteIsCalledByTheFrontEnd(unittest.TestCase):
    """The gate that keeps the intake routes reachable. Without it, finished
    server code could sit behind URLs that nothing in the page ever calls.

    The expected list is read from `api_intake.ROUTES` at test time, so adding a
    new endpoint fails this file until the client calls it — which is the whole
    point. Both directions are asserted: a route with no caller is dead code, and
    a caller naming a path that is not a route is a silent 404.
    """

    def setUp(self):
        self.js = js_code(_read("app", "intake.js"))
        self.calls = [(m, p) for m, p, _ in _api_calls(self.js)
                      if p.startswith("/api/intake/")]

    def test_the_module_still_declares_the_routes_this_gate_reads(self):
        """If ROUTES ever stops being a {method: {path: handler}} map, every
        assertion below would pass against nothing at all."""
        self.assertTrue(api_intake.ROUTES, "api_intake.ROUTES is empty")
        for method, table in api_intake.ROUTES.items():
            self.assertIn(method, ("GET", "POST", "DELETE", "PUT", "PATCH"))
            self.assertTrue(table, f"api_intake.ROUTES[{method!r}] is empty")

    def test_every_route_is_called_with_its_own_method(self):
        want = {(method, path)
                for method, table in api_intake.ROUTES.items() for path in table}
        have = set(self.calls)
        missing = sorted(want - have)
        self.assertEqual(missing, [], "\n".join([
            "",
            "These routes exist in api_intake.ROUTES and static/app/intake.js never",
            "calls them. That is NC-016 exactly: finished, tested, routed, invisible.",
            *(f"  {m} {p}" for m, p in missing),
        ]))

    def test_the_client_calls_no_intake_path_that_is_not_a_route(self):
        want = {(method, path)
                for method, table in api_intake.ROUTES.items() for path in table}
        extra = sorted(set(self.calls) - want)
        self.assertEqual(extra, [], f"paths that would 404: {extra}")

    def test_there_is_exactly_one_writer(self):
        """`/api/intake/commit` is the only endpoint that touches crm.db, so a
        second call site is a second way to write — and only one of them would be
        behind the review step."""
        writes = [c for c in self.calls if c[1].endswith("/commit")]
        self.assertEqual(len(writes), 1,
                         f"expected one commit call site, found {len(writes)}")

    def test_the_commit_sends_the_json_boolean_true(self):
        """`commit_batch`'s gate is `bool(body.get("confirm"))`, so the STRING
        "false" confirms the batch (test_api_intake.TestKnownBugs
        .test_any_truthy_confirm_value_writes_the_batch). The client half of that
        is to send the boolean and never a variable or a stringified checkbox."""
        opts = [o for m, p, o in _api_calls(self.js) if p.endswith("/intake/commit")]
        self.assertEqual(len(opts), 1)
        self.assertRegex(opts[0], r"confirm:\s*true\b",
                         "the commit body does not send `confirm: true` as a "
                         "literal boolean")
        self.assertNotRegex(opts[0], r'confirm:\s*"',
                            "confirm is being sent as a string, which is truthy "
                            "for every value including \"false\"")

    def test_the_lookup_sends_the_paste_and_not_an_allowlist(self):
        """NC-148 reversed this test, and the reversal is the point.

        It used to require the client to send `allowed_urls`, because an empty
        one meant "allow everything" server-side and the browser sending the
        batch's own list was the only containment there was. That is a rule
        enforced with a value supplied by the thing being contained. The server
        now re-derives the allowlist from the paste, so the client's job is to
        send the paste and nothing else — and sending a list again would be
        sending a claim nobody reads.
        """
        opts = [o for m, p, o in _api_calls(self.js) if p.endswith("/intake/lookup")]
        self.assertEqual(len(opts), 1, "expected one lookup call site")
        self.assertRegex(opts[0], r"\btext\b",
                         "the lookup no longer sends the pasted text, so the "
                         "server has nothing to derive the allowlist from and "
                         "every fetch is refused")
        self.assertNotIn("allowed_urls", opts[0],
                         "the lookup is sending an allowlist again. The server "
                         "ignores it; a body key that reads like a permission "
                         "and grants nothing is worse than no key at all")

    def test_the_client_sends_no_contact_id_verdict_or_ref(self):
        """`commit_batch` throws all three away, so sending them reads as though
        the client expects them honoured. `itemFor` is the one place a batch item
        is built; it may name only intake.COMMIT_KEYS."""
        import intake
        body = _braced(self.js, self.js.index("function itemFor("))
        keys = set(re.findall(r"^\s{4}([a-z_]+):", body, re.M))
        self.assertTrue(keys, "itemFor no longer builds an object literal")
        self.assertEqual(sorted(keys - set(intake.COMMIT_KEYS)), [],
                         "itemFor sends keys commit_batch does not read")


class TestTheSurfaceIsReachable(unittest.TestCase):
    """A view module nobody imports, or a hash the router does not know, is the
    same invisibility with a different cause. Four ways in are asserted, because
    three of them are one-line deletions."""

    def setUp(self):
        self.main = js_code(_read("app", "main.js"))

    def test_main_imports_and_renders_the_view(self):
        self.assertIn('from "./intake.js"', self.main,
                      "main.js does not import the Intake view")
        self.assertIn("<${Intake}", self.main,
                      "main.js imports Intake but never renders it")

    def test_the_router_knows_the_hash(self):
        body = _braced(self.main, self.main.index("function parseHash("))
        self.assertIn('"intake"', body,
                      "#intake is not in parseHash's route names, so the hash "
                      "falls back to People and the surface has no address")

    def test_the_view_map_resolves_it(self):
        body = _braced(self.main, self.main.index("const view = "))
        self.assertIn('"intake":', body,
                      "the view map has no intake entry, so parseHash returns a "
                      "name that resolves to undefined and the app renders nothing")

    def test_the_omnibox_offers_it_without_needing_a_keyword(self):
        """⌘K is how this app is driven. `typedOnly` would keep the command out of
        the bare list, i.e. reachable only by already knowing it exists — which is
        the same defect (a feature nobody can reach), one step smaller."""
        i = self.main.index('hash: "intake"')
        start = self.main.rindex("{", 0, i)
        entry = _braced(self.main, start)
        self.assertIn("keys:", entry, "the intake command has no keywords")
        self.assertNotIn("typedOnly", entry,
                         "the intake command is typedOnly, so an empty ⌘K box "
                         "never shows it")
        self.assertEqual(self.main.count('hash: "intake"'), 1,
                         "more than one omnibox command points at #intake")

    def test_the_section_label_names_it(self):
        """The omnibox context pill reads SECTION_LABEL. A missing entry falls
        back to "People", so the box would claim you are somewhere you are not."""
        i = self.main.index("const SECTION_LABEL")
        self.assertIn("intake:", _braced(self.main, i))


class TestTheStylesheetIsRegistered(unittest.TestCase):
    """A view-*.css nobody links is invisible and looks exactly like a CSS bug.

    `test_the_intake_stylesheet_is_linked` requires the line

        <link rel="stylesheet" href="css/view-intake.css" />

    in static/index.html. A gate weakened to make a missing link pass is worse
    than no gate at all.
    """

    def test_the_stylesheet_exists(self):
        self.assertTrue(os.path.exists(os.path.join(STATIC, "css", "view-intake.css")))

    def test_the_intake_stylesheet_is_linked(self):
        self.assertIn('href="css/view-intake.css"', _read("index.html"),
                      "static/index.html does not link css/view-intake.css, so "
                      "the Intake view renders unstyled")

    def test_the_view_uses_no_colour_or_size_that_is_not_a_token(self):
        """DESIGN_DIRECTION §2: tokens.css is the contract. A hex literal or a px
        font-size here breaks the one-reversible-file property."""
        css = re.sub(r"/\*.*?\*/", "", _read("css", "view-intake.css"), flags=re.S)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", css), [],
                         "a colour is hardcoded instead of using a token")
        self.assertEqual(re.findall(r"font-size:\s*\d", css), [],
                         "a font-size is hardcoded instead of using --fs-*")

    def test_the_js_carries_no_style_block(self):
        """A stylesheet that is not linked is not a licence to inline it."""
        js = _read("app", "intake.js")
        self.assertNotIn("<style", js)
        self.assertNotIn("document.createElement(\"style\")", js)


class TestArchitectureNamesTheSurfaceAndItsRoutes(unittest.TestCase):
    """NC-016. Whoever reads ARCHITECTURE.md must find this feature there whether
    or not anybody remembers the commit that built it — so the doc names the
    module, the hash and every route, and the route list is derived from the code
    rather than trusted."""

    def setUp(self):
        with open(os.path.join(REPO, "ARCHITECTURE.md"), encoding="utf-8") as fh:
            self.doc = fh.read()

    def test_it_names_the_client_and_the_hash(self):
        for token in ("static/app/intake.js", "crm/api_intake.py",
                      "static/css/view-intake.css", "#intake"):
            with self.subTest(token=token):
                self.assertIn(token, self.doc,
                              f"ARCHITECTURE.md does not mention {token}")

    def test_it_names_every_route(self):
        """METHOD and path together, not the path alone. Three of the eight
        routes share the path /api/intake/batch, so a gate on paths passes with
        two of those three rows deleted from the doc. The pair is the unit of
        documentation."""
        missing = [f"{method} {path}"
                   for method, table in api_intake.ROUTES.items()
                   for path in table if f"{method} {path}" not in self.doc]
        self.assertEqual(missing, [], f"routes absent from ARCHITECTURE.md: {missing}")


if __name__ == "__main__":
    unittest.main()
