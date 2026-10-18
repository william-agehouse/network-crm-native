"""NC-363 and NC-373: what a person reads when something fails, and the ring they see.

Both are the same kind of defect: the app was right underneath and drew it so
that a person could not use it. They share this file because both are checked
by computing what reaches the eye, not by reading source for the right words.

NC-363. A failed request used to reach the screen as the developer's text.
A stopped engine said "Load failed", a timeout named its route ("/api/chats did
not answer within 30s"), and a crash said "500 Internal Server Error" or a
Python exception. About 55 places hand `e.message` to a toast, so the one place
that makes the Error is `api()` in static/app/api.js, and it is fixed there.
These tests run the REAL api.js under node with `fetch` replaced, once per kind
of failure, and read the sentence that comes out:

  * the engine REFUSED (400-499 with a sentence): word for word, untouched;
  * the engine says why something OUTSIDE it failed (502, 504): shown too,
    unless it carries developer text;
  * everything else (500 and up, an answer that is not JSON, no answer, no
    sentence): one plain sentence, with the route and the status kept in
    console.error and never on screen.

The native app has the same rule in `EngineError`; Swift cannot run in this
suite, so a small source check below stops its sentences growing a route or a
code again, and `--selftest` (refusalReads) checks the real behaviour.

NC-373. `--ring-faint` was #d7d9dd on white, 1.41 to 1. WCAG asks 3 to 1 of a
graphic that carries meaning, and this is the colour every ring falls back to.
The ratio is computed from tokens.css, for every surface a ring is drawn on, in
both themes.
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
from tests.helpers import js_code_of  # noqa: E402,F401

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_JS = os.path.join(REPO, "static", "app", "api.js")
TOKENS = os.path.join(REPO, "static", "css", "tokens.css")
ENGINE_CLIENT = os.path.join(REPO, "native", "Sources", "EngineClient.swift")

WENT_WRONG = ("Something went wrong inside Network CRM. Try again, and if it keeps "
              "happening, quit and reopen the app.")
UNREACHABLE = "Network CRM is not answering. Quit and reopen the app, then try again."

# The driver imports the real api.js (copied to .mjs, since the file has no
# package.json to say it is a module) with the few globals it touches stubbed.
DRIVER = r"""
import { pathToFileURL } from "node:url";
globalThis.location = { hostname: "127.0.0.1", host: "127.0.0.1:8765", protocol: "http:",
                        pathname: "/", search: "", reload() {} };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const realSetTimeout = globalThis.setTimeout;
// A timeout of three minutes must not take three minutes to test.
globalThis.setTimeout = (fn, ms) => realSetTimeout(fn, 0);
const logged = [];
console.error = (...a) => logged.push(a.join(" "));

const mod = await import(pathToFileURL(process.argv[2]).href);
const reply = (status, statusText, body, readable = true) => async () => ({
  ok: status >= 200 && status < 300, status, statusText,
  json: async () => { if (!readable) throw new SyntaxError("Unexpected token '<'"); return body; },
});
const neverAnswers = (path, o) => new Promise((_, reject) => {
  o.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
});

const scenarios = JSON.parse(process.argv[3]);
const fetches = {
  stopped_safari: async () => { throw new TypeError("Load failed"); },
  stopped_chrome: async () => { throw new TypeError("Failed to fetch"); },
  stopped_refused: async () => { throw new TypeError("NetworkError when attempting to fetch resource."); },
  never_answers: neverAnswers,
  crash_python: reply(500, "Internal Server Error", { error: "KeyError: 'name'" }),
  crash_with_route: reply(500, "Internal Server Error", { error: "/api/contacts raised ValueError: x" }),
  crash_html: reply(500, "Internal Server Error", null, false),
  crash_null_body: reply(500, "Internal Server Error", null),
  bad_gateway_html: reply(502, "Bad Gateway", null, false),
  unavailable: reply(503, "Service Unavailable", { error: "Your saved messages could not be read just now: database is locked" }),
  missing_route_html: reply(404, "Not Found", null, false),
  refusal_without_sentence: reply(400, "Bad Request", { error: "" }),
  refusal_400: reply(400, "Bad Request", { error: "Some of these people are not in your network." }),
  refusal_404: reply(404, "Not Found", { error: "That note no longer exists." }),
  refusal_409_detail: reply(409, "Conflict", { detail: "A list called Dinner already exists." }),
  refusal_loopback_401: reply(401, "Unauthorized", { error: "missing or invalid token" }),
  outside_502: reply(502, "Bad Gateway", { error: "Could not reach calendar.example within 20s (timed out). Nothing was downloaded." }),
  outside_504: reply(504, "Gateway Timeout", { error: "The helper did not answer. Open it again from System Settings." }),
  outside_502_technical: reply(502, "Bad Gateway", { error: "Draft failed: Error code: 401 - {'type': 'error'}" }),
  not_json_success: reply(200, "OK", null, false),
  fine: reply(200, "OK", { ok: true, items: [1, 2] }),
};
const out = {};
for (const s of scenarios) {
  logged.length = 0;
  globalThis.fetch = fetches[s.fetch];
  try {
    const data = await mod.api(s.path || "/api/chats", s.opts || {});
    out[s.name] = { ok: true, data, logged: [...logged] };
  } catch (e) {
    out[s.name] = { ok: false, message: e.message, kind: e.kind, status: e.status, logged: [...logged] };
  }
}
process.stdout.write(JSON.stringify(out));
"""

TOAST_DRIVER = r"""
import { pathToFileURL } from "node:url";
globalThis.location = { hostname: "127.0.0.1", search: "", pathname: "/", reload() {} };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const delays = [];
globalThis.setTimeout = (fn, ms) => { delays.push(ms); return 0; };
globalThis.clearTimeout = () => {};
const node = () => ({ setAttribute() {}, remove() {}, isConnected: true, className: "", textContent: "" });
globalThis.document = { querySelectorAll: () => [], createElement: node, body: { appendChild() {} } };
const mod = await import(pathToFileURL(process.argv[2]).href);
const out = {};
for (const [name, text] of Object.entries(JSON.parse(process.argv[3]))) {
  delays.length = 0;
  mod.toast(text);
  out[name] = delays[delays.length - 1];
}
out.waited = [20000, 30000, 180000, 315000, 1000].map(ms => mod.waitedFor(ms));
process.stdout.write(JSON.stringify(out));
"""

# What no person should ever read in a failure the app wrote itself.
DEVELOPER_TEXT = ("/api/", "HTTP", "Internal Server Error", "KeyError", "ValueError",
                  "Load failed", "Failed to fetch", "NetworkError", "Traceback")
A_STATUS_NUMBER = re.compile(r"\b[1-5]\d\d\b")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _node():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    return node


class _RunsTheRealApiJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = _node()
        cls.dir = tempfile.mkdtemp(prefix="nc-error-text-")
        cls.addClassCleanup(shutil.rmtree, cls.dir, True)
        cls.api = os.path.join(cls.dir, "api.mjs")
        shutil.copy(API_JS, cls.api)

    @classmethod
    def run_driver(cls, source, payload):
        driver = os.path.join(cls.dir, "driver-%d.mjs" % abs(hash(source)))
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(source)
        out = subprocess.run([cls.node, driver, cls.api, json.dumps(payload)],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError("node failed:\n" + out.stderr)
        return json.loads(out.stdout)


class TestAFailedRequestSaysOnePlainSentence(_RunsTheRealApiJs):
    """NC-363, the web page. Every case is one run of the real api()."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cases = [
            ("stopped_safari", {}), ("stopped_chrome", {}), ("stopped_refused", {}),
            ("never_answers", {"opts": {"timeoutMs": 180000}}),
            ("crash_python", {}), ("crash_with_route", {}), ("crash_html", {}),
            ("crash_null_body", {}), ("bad_gateway_html", {}), ("unavailable", {}),
            ("missing_route_html", {}), ("refusal_without_sentence", {}),
            ("refusal_400", {}), ("refusal_404", {}), ("refusal_409_detail", {}),
            ("refusal_loopback_401", {}), ("outside_502", {}), ("outside_504", {}),
            ("outside_502_technical", {}), ("not_json_success", {}), ("fine", {}),
        ]
        cls.out = cls.run_driver(DRIVER, [
            dict(name=name, fetch=name, path="/api/chats", **extra) for name, extra in cases])

    def said(self, name):
        got = self.out[name]
        self.assertFalse(got["ok"], f"{name}: expected a failure, got {got}")
        return got["message"]

    def assert_plain(self, name):
        said = self.said(name)
        for bad in DEVELOPER_TEXT:
            self.assertNotIn(bad, said, f"{name} shows developer text: {said!r}")
        self.assertIsNone(A_STATUS_NUMBER.search(said), f"{name} shows a status number: {said!r}")
        return said

    # ── no answer at all ────────────────────────────────────────────────────
    def test_a_stopped_engine_says_it_is_not_answering_in_every_browser(self):
        for name in ("stopped_safari", "stopped_chrome", "stopped_refused"):
            with self.subTest(name):
                self.assertEqual(self.assert_plain(name), UNREACHABLE)
                self.assertEqual(self.out[name]["kind"], "unreachable")

    def test_a_timeout_says_how_long_it_waited_and_names_no_route(self):
        said = self.assert_plain("never_answers")
        self.assertEqual(said, "Network CRM did not answer within 3 minutes. Try again.")
        self.assertEqual(self.out["never_answers"]["kind"], "timeout")

    def test_a_timeout_still_matches_what_ask_and_data_listen_for(self):
        """ask.js (TIMED_OUT_RE) and data.js tell a request that gave up from a
        request that failed by this phrase. Rewording it silently turns every
        Ask timeout into a fault, so it is pinned until they read `.kind`."""
        said = self.said("never_answers")
        self.assertRegex(said, r"(?i)did not answer within")
        self.assertRegex(said, r"did not answer")

    # ── the engine answered, and it was not a sentence for people ───────────
    def test_a_crash_never_shows_the_python_exception_or_the_status(self):
        for name in ("crash_python", "crash_with_route", "crash_html", "crash_null_body"):
            with self.subTest(name):
                self.assertEqual(self.assert_plain(name), WENT_WRONG)
                self.assertEqual(self.out[name]["kind"], "server")
                self.assertEqual(self.out[name]["status"], 500)

    def test_an_answer_that_is_not_the_engines_json_is_one_plain_sentence(self):
        for name in ("bad_gateway_html", "missing_route_html", "not_json_success"):
            with self.subTest(name):
                self.assertEqual(self.assert_plain(name), WENT_WRONG)

    def test_a_server_failure_with_a_sentence_of_its_own_is_still_replaced(self):
        """503 and the rest of 500 and up: the engine's text there is a database
        error, not something it worded for a person."""
        self.assertEqual(self.assert_plain("unavailable"), WENT_WRONG)

    def test_a_refusal_with_no_sentence_is_one_plain_sentence(self):
        self.assertEqual(self.assert_plain("refusal_without_sentence"), WENT_WRONG)

    # ── the engine's own words go through untouched ─────────────────────────
    def test_a_refusal_reaches_the_screen_word_for_word(self):
        for name, sentence in (
                ("refusal_400", "Some of these people are not in your network."),
                ("refusal_404", "That note no longer exists."),
                ("refusal_409_detail", "A list called Dinner already exists."),
                ("refusal_loopback_401", "missing or invalid token")):
            with self.subTest(name):
                self.assertEqual(self.said(name), sentence)
                self.assertEqual(self.out[name]["kind"], "refused")

    def test_a_502_or_504_that_says_why_something_outside_failed_is_shown(self):
        """A calendar address that stopped working, a helper that timed out: the
        engine worded these for the person, and "Something went wrong" would
        throw away the one useful fact (what to fix)."""
        self.assertEqual(self.said("outside_502"),
                         "Could not reach calendar.example within 20s (timed out). "
                         "Nothing was downloaded.")
        self.assertEqual(self.said("outside_504"),
                         "The helper did not answer. Open it again from System Settings.")

    def test_a_502_that_carries_developer_text_is_replaced(self):
        """The AI call's own exception text rides on its 502."""
        self.assertEqual(self.assert_plain("outside_502_technical"), WENT_WRONG)

    def test_a_good_answer_is_returned_untouched(self):
        self.assertEqual(self.out["fine"]["data"], {"ok": True, "items": [1, 2]})
        self.assertEqual(self.out["fine"]["logged"], [])

    # ── the detail is kept, in the console ──────────────────────────────────
    def test_the_route_and_status_go_to_the_console_not_the_screen(self):
        for name in ("crash_python", "crash_html", "bad_gateway_html", "unavailable"):
            with self.subTest(name):
                logged = " ".join(self.out[name]["logged"])
                self.assertIn("/api/chats", logged, "the route is lost, so a bug report has nothing in it")
                self.assertIn(str(self.out[name]["status"]), logged)
        self.assertIn("KeyError", " ".join(self.out["crash_python"]["logged"]),
                      "what the engine said is the one thing a fix needs")
        self.assertIn("/api/chats", " ".join(self.out["never_answers"]["logged"]))
        self.assertIn("/api/chats", " ".join(self.out["stopped_safari"]["logged"]))

    def test_a_refusal_is_not_logged_as_an_error(self):
        for name in ("refusal_400", "refusal_404", "outside_502"):
            with self.subTest(name):
                self.assertEqual(self.out[name]["logged"], [],
                                 "the engine's sentence is the whole story; logging it "
                                 "as an error would bury the real ones")


class TestALongSentenceStaysLongEnoughToRead(_RunsTheRealApiJs):
    """The plain sentences run to a hundred characters. A toast that left after
    2.6 seconds would be gone before it could be read."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out = cls.run_driver(TOAST_DRIVER, {
            "saved": "Saved", "merged": "Merged Ada Lovelace into Ada King",
            "went_wrong": WENT_WRONG, "huge": "x" * 400})

    def test_a_confirmation_keeps_its_old_2_6_seconds(self):
        self.assertEqual(self.out["saved"], 2600)
        self.assertEqual(self.out["merged"], 2600)

    def test_the_failure_sentence_stays_long_enough_to_read(self):
        self.assertGreaterEqual(self.out["went_wrong"], 5000)
        self.assertLessEqual(self.out["went_wrong"], 9000)

    def test_nothing_stays_longer_than_nine_seconds(self):
        self.assertEqual(self.out["huge"], 9000)

    def test_a_wait_is_said_in_seconds_or_minutes(self):
        self.assertEqual(self.out["waited"],
                         ["20 seconds", "30 seconds", "3 minutes", "5 minutes", "1 second"])


class TestTheNativeAppUsesTheSameSentences(unittest.TestCase):
    """Swift cannot run here, so this only keeps the words from growing a route, a
    status or a port again. `--selftest` (SelfTest+Refusals.swift) checks the
    behaviour: every screen's helper, every failure, the engine's refusal
    unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.swift = _read(ENGINE_CLIENT)
        start = cls.swift.index("var problem: String")
        # `problem` and `advice` are the sentences themselves; what follows them
        # (the check for developer text in a 502) legitimately names "/api/".
        cls.words = cls.swift[start:cls.swift.index("var errorDescription", start)]

    def test_what_a_person_is_shown_has_no_route_status_or_port_in_it(self):
        for bad in ("HTTP", "port \\(", "(port", "\\(route", "\\(path", "\\(status",
                    "/api/", "shape this app did not expect"):
            self.assertNotIn(bad, self.words, f"EngineError's sentences show {bad!r} again")

    def test_it_says_the_same_things_as_the_web_page(self):
        api = _read(API_JS)
        for sentence in ("Network CRM is not answering.",
                         "Network CRM did not answer within",
                         "Something went wrong inside Network CRM.",
                         "Quit and reopen the app, then try again.",
                         "Try again, and if it keeps happening, quit and reopen the app."):
            self.assertIn(sentence, self.swift, f"the native app lost {sentence!r}")
            self.assertTrue(sentence in api or sentence.rstrip(".") in api,
                            f"the web page does not say {sentence!r}")

    def test_the_detail_goes_to_the_log(self):
        self.assertIn("import os", self.swift)
        self.assertIn("Logger(subsystem:", self.swift)
        self.assertIn("technicalDetail", self.swift)


# ── NC-373 ───────────────────────────────────────────────────────────────────

def _channel(v):
    v /= 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a, b):
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(theme):
    css = re.sub(r"/\*.*?\*/", "", _read(TOKENS), flags=re.S)
    opener = r":root" if theme == "light" else r'\[data-theme="dark"\]'
    block = re.search(opener + r"\s*\{(.*?)\n\}", css, re.S).group(1)
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


class TestTheFaintRingCanBeSeen(unittest.TestCase):
    """NC-373. Where a ring is drawn: on a table row (--panel, or --hover when the
    row is hovered or open), in the omnibox chip (--panel-2) and on the shell
    (--bg). A ring has to reach 3 to 1 on the worst of them, in both themes."""

    SURFACES = ("--panel", "--panel-2", "--bg", "--hover")

    def test_it_reaches_three_to_one_on_every_surface_in_both_themes(self):
        for theme in ("light", "dark"):
            tokens = _tokens(theme)
            faint = tokens["--ring-faint"]
            for surface in self.SURFACES:
                with self.subTest(theme=theme, surface=surface):
                    ratio = contrast(faint, tokens[surface])
                    self.assertGreaterEqual(
                        ratio, 3.0,
                        f"{theme}: --ring-faint {faint} on {surface} {tokens[surface]} is "
                        f"{ratio:.2f} to 1; a graphic that carries meaning needs 3 to 1")

    def test_it_is_still_the_quietest_tier(self):
        """Raising the faint ring must not make it louder than the tiers above
        it, or the ring stops reading as closeness. Faint stays the lowest ratio
        on --panel in both themes."""
        for theme in ("light", "dark"):
            tokens = _tokens(theme)
            panel = tokens["--panel"]
            faint = contrast(tokens["--ring-faint"], panel)
            for tier in ("--ring-close", "--ring-active", "--ring-loose"):
                with self.subTest(theme=theme, tier=tier):
                    self.assertLessEqual(faint, contrast(tokens[tier], panel) + 0.01)

    def test_it_is_not_the_old_invisible_grey(self):
        self.assertNotEqual(_tokens("light")["--ring-faint"].lower(), "#d7d9dd")
        self.assertNotEqual(_tokens("dark")["--ring-faint"].lower(), "#3d3e4a")

    def test_the_numbers_the_comment_in_tokens_css_states_are_the_real_ones(self):
        css = _read(TOKENS)
        light, dark = _tokens("light"), _tokens("dark")
        for stated, a, b in (
                ("3.59", light["--ring-faint"], light["--panel"]),
                ("3.21", light["--ring-faint"], light["--hover"]),
                ("3.89", dark["--ring-faint"], dark["--panel"]),
                ("3.47", dark["--ring-faint"], dark["--hover"])):
            self.assertIn(stated, css)
            self.assertEqual(f"{contrast(a, b):.2f}", stated,
                             "tokens.css says a ratio that is no longer true")


if __name__ == "__main__":
    unittest.main()
