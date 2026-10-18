"""The web page's own fixes (NC-333, 342, 347, 348, 349, 352, 354, 356, 305, 336).

WHY THIS FILE EXISTS

Each of these was a place where the page said one thing and the engine did
another, and a page cannot be tested by looking at it from the engine's side.
The engine already answered correctly in every case: it refused to blank a
filled field because it was not told to, it answered `ok: false` to a button
that then showed nothing, it sent a message count under a name the picker never
read. So the checks here run the page's own code.

HOW THE PAGE IS TESTED HERE

The repo has no browser in its test suite, so there are two honest options.
Where a fix is a function, the function is sliced out of the real source file
(comments blanked, so a rule written in prose cannot satisfy a test) and run
under node with stand-ins for the few things it talks to (`api`, `toast`,
`localStorage`). That is the real code running, not a copy of it. Where a fix
is a line inside a component, the line is read, and the test says which fact it
reads and why that fact is enough.

Where the engine is the other half of the fix (an empty City reaching the
database, a ⌘K search ranking the closest first), the page's own request is
built and handed to the engine's real handler, so the two halves are checked
together and not each against a guess about the other.

Made-up people only: Ada, Grace, Alan.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, db, js_code_of, braced_block, js_statement,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


def page(name):
    """A page module with its comments blanked: the code that runs, nothing else."""
    return js_code_of(os.path.join(APP, name))


def fn(text, anchor):
    """The whole function or arrow function that begins at `anchor`, braces and all."""
    return braced_block(text, anchor)


def const(text, anchor):
    """One `const … ;` statement, with a leading `export` taken off so it runs bare."""
    return js_statement(text, anchor).replace("export ", "", 1)


def without_export(src):
    return src.replace("export ", "", 1)


class NodeCase(unittest.TestCase):
    """Runs a snippet of the page's own JavaScript under node."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed")

    def run_js(self, body):
        """`body` is an async function body that returns a JSON-able value."""
        script = ("(async () => {\n" + body + "\n})()"
                  ".then(v => process.stdout.write(JSON.stringify(v === undefined ? null : v)))"
                  ".catch(e => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });")
        out = subprocess.run([self.node, "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)


# ── NC-333 ───────────────────────────────────────────────────────────────────

class TestEmptyingAFieldIsSaved(NodeCase, Base):
    """Clear a person's city in the web page, reopen them, and it is still there:
    the page never sent `clear`, and showed Saved over the engine's `unchanged`."""

    def setUp(self):
        Base.setUp(self)
        self.save_field = fn(page("people.js"), "function saveField")

    def call(self, value, reply):
        """What saveField sends for `value`, and whether it resolved or rejected."""
        return self.run_js(self.save_field + """
          const sent = [];
          const api = async (path, opts) => { sent.push({ path, body: JSON.parse(opts.body) }); return %s; };
          let outcome = "resolved";
          try { await saveField(7, "home_city", %s); } catch (e) { outcome = "rejected: " + e.message; }
          return { sent, outcome };
        """ % (json.dumps(reply), json.dumps(value)))

    def test_an_emptied_box_is_sent_as_a_deliberate_clear(self):
        out = self.call("", {"ok": True})
        self.assertEqual(out["sent"][0]["path"], "/api/contact/7/field")
        self.assertEqual(out["sent"][0]["body"],
                         {"field": "home_city", "value": "", "clear": True})

    def test_typed_text_is_not_a_clear(self):
        """The control: `clear` on every save would defeat the engine's guard
        against a stray blur wiping a field."""
        out = self.call("Paris", {"ok": True})
        self.assertEqual(out["sent"][0]["body"]["value"], "Paris")
        self.assertFalse(out["sent"][0]["body"]["clear"])

    def test_an_unchanged_answer_is_never_reported_as_saved(self):
        out = self.call("", {"ok": True, "unchanged": True})
        self.assertTrue(out["outcome"].startswith("rejected"), out["outcome"])
        self.assertEqual(self.call("Paris", {"ok": True})["outcome"], "resolved")

    def test_the_engine_empties_the_city_when_it_gets_what_the_page_sends(self):
        """The whole fix, end to end: the body the page builds, handed to the
        engine, leaves the person with no city."""
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.set_field(self.conn, cid, "home_city", "Paris")
        self.conn.commit()
        body = self.call("", {"ok": True})["sent"][0]["body"]
        out = app.handle_set_field(self.conn, cid, body)
        self.assertNotIn("unchanged", out)
        self.assertEqual(db.get_contact(self.conn, cid)["home_city"], "")

    def test_without_clear_the_engine_still_keeps_the_text(self):
        """Why the flag matters: the old body, no `clear`, changes nothing."""
        cid = db.create_contact(self.conn, "Grace Hopper")
        db.set_field(self.conn, cid, "home_city", "Paris")
        self.conn.commit()
        out = app.handle_set_field(self.conn, cid, {"field": "home_city", "value": ""})
        self.assertTrue(out.get("unchanged"))
        self.assertEqual(db.get_contact(self.conn, cid)["home_city"], "Paris")


# ── NC-342 (the page's half) ─────────────────────────────────────────────────

class TestDismissingTheRecoveryNoticeIsAWrite(unittest.TestCase):
    """A read that changes data can be repeated by a prefetch or a second window
    without anyone pressing the button, so the page sends a POST."""

    def test_every_call_to_the_dismiss_route_is_a_post(self):
        calls = 0
        for name in sorted(os.listdir(APP)):
            if not name.endswith(".js"):
                continue
            code = page(name)
            at = 0
            while True:
                at = code.find("/api/db-recovery-notice/dismiss", at)
                if at < 0:
                    break
                calls += 1
                call = code[code.rindex("api(", 0, at):code.index(")", at)]
                self.assertIn('method: "POST"', call, f"{name}: a GET to the dismiss route")
                at += 1
        self.assertGreaterEqual(calls, 1, "the page no longer calls the dismiss route; "
                                          "if the notice moved, this test should follow it")


# ── NC-347 ───────────────────────────────────────────────────────────────────

class TestAFailedAskSaysItFailed(unittest.TestCase):
    """A request that failed has no keyword answer under it, so it may not say
    one was used."""

    def setUp(self):
        self.ask = page("ask.js")

    def test_the_sentence_says_it_failed_and_what_to_try(self):
        sentence = const(self.ask, "const ASK_FAILED")
        self.assertIn("could not answer", sentence)
        self.assertIn("Try it again", sentence)
        self.assertIn("details", sentence)
        self.assertNotIn("keyword", sentence.lower())

    def test_the_failed_turn_uses_it_and_no_other_sentence_for_that_case(self):
        failed = braced_block(self.ask, "${m.failed && html`")
        self.assertIn("? ASK_FAILED", failed)
        self.assertNotIn("used keyword search instead", self.ask)
        self.assertNotIn("hit a problem", self.ask)

    def test_the_real_fallback_still_says_keyword_search(self):
        """The control. When the AI call failed and the keyword engine DID
        answer, saying so is true, and those notices must keep saying it."""
        notice = fn(self.ask, "function aiDownNotice")
        self.assertGreaterEqual(notice.count("Ask used keyword search for this answer"), 4)


# ── NC-348 ───────────────────────────────────────────────────────────────────

class TestAnOpenButtonThatIsRefusedSaysSo(NodeCase):
    """`ok: false` is a normal answer, not an error, and the page ignored it."""

    def setUp(self):
        ui = page("ui.js")
        self.src = const(ui, "export const OPEN_REFUSED") + "\n" + without_export(
            fn(ui, "export async function openTarget"))

    def open(self, reply=None, error=None):
        return self.run_js(self.src + """
          const toasts = [];
          const toast = (m) => toasts.push(m);
          const api = async () => { if (%s) throw new Error(%s); return %s; };
          await openTarget("imessage");
          return { toasts, refused: OPEN_REFUSED };
        """ % (json.dumps(error is not None), json.dumps(error or ""), json.dumps(reply)))

    def test_a_refusal_shows_a_sentence(self):
        out = self.open({"ok": False})
        self.assertEqual(out["toasts"], [out["refused"]])
        self.assertIn("did not open", out["refused"])

    def test_a_reason_from_the_engine_is_used_when_there_is_one(self):
        out = self.open({"ok": False, "message": "macOS would not open the folder."})
        self.assertEqual(out["toasts"], ["macOS would not open the folder."])

    def test_success_says_nothing(self):
        self.assertEqual(self.open({"ok": True})["toasts"], [])

    def test_a_failed_request_still_shows_its_own_error(self):
        self.assertEqual(self.open(error="Server unreachable")["toasts"],
                         ["Server unreachable"])


# ── NC-349 ───────────────────────────────────────────────────────────────────

class TestTheMergePickerShowsEveryMessage(NodeCase, Base):
    """The picker added up four columns that a people row does not carry, so
    every person read as having no messages."""

    def person_line(self, row):
        ui = page("ui.js")
        return self.run_js(
            without_export(fn(ui, "export function messageTotal")) + "\n"
            + fn(ui, "function personLine") + "\n"
            + "const noun = (n, one, many) => (n === 1 ? one : many);\n"
            + "return personLine(%s);" % json.dumps(row))

    def test_a_real_row_from_the_engine_shows_its_total_across_channels(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.set_channel(self.conn, cid, "linkedin", 3, "2026-01-01 10:00:00")
        db.set_channel(self.conn, cid, "whatsapp", 4, "2026-01-02 10:00:00")
        db.set_channel(self.conn, cid, "imessage", 5, "2026-01-03 10:00:00")
        self.conn.commit()
        row = app.handle_list_contacts(self.conn, {"q": ["Ada"]})["contacts"][0]
        self.assertIn("12 messages", self.person_line(row))

    def test_one_message_is_singular_and_none_is_left_out(self):
        self.assertIn("1 message", self.person_line({"counts": {"email": 1}}))
        self.assertNotIn("message", self.person_line({"counts": {"email": 0}, "company": "Acme"}))
        self.assertEqual(self.person_line({"counts": {}}), "No details yet")

    def test_a_total_key_is_not_counted_twice(self):
        """If the engine ever adds a `total` beside the channels, the sum must
        not double it."""
        self.assertIn("9 messages",
                      self.person_line({"counts": {"linkedin": 4, "whatsapp": 5, "total": 9}}))


# ── NC-352 ───────────────────────────────────────────────────────────────────

class TestTheCommandBarListsTheClosestFirst(Base):
    """⌘K sent no sort, and the engine then ordered by one LinkedIn column."""

    def page_params(self):
        """The query the command bar's people search really builds."""
        code = page("main.js")
        call = js_statement(code, 'api("/api/contacts?" + new URLSearchParams({ q: query')
        self.assertIn('sort: "closeness"', call, "the command bar's people search sends no sort")
        return {"q": ["Ada"], "sort": ["closeness"]}

    def test_the_closest_ada_comes_first_not_the_most_recently_connected(self):
        close = db.create_contact(self.conn, "Ada Close")
        db.set_channel(self.conn, close, "whatsapp", 900, "2020-01-01 10:00:00")
        db.add_note(self.conn, close, "years of history")
        recent = db.create_contact(self.conn, "Ada Recent")   # newest LinkedIn touch, barely known
        db.set_channel(self.conn, recent, "linkedin", 1, "2026-07-24 10:00:00")
        self.conn.commit()

        names = lambda q: [c["name"] for c in app.handle_list_contacts(self.conn, q)["contacts"]]  # noqa: E731
        self.assertEqual(names(self.page_params())[0], "Ada Close")
        # The control: with no sort, the weakly known one wins, so the fixture
        # would not have noticed a page that forgot the parameter.
        self.assertEqual(names({"q": ["Ada"]})[0], "Ada Recent")


# ── NC-354 ───────────────────────────────────────────────────────────────────

class TestPasteAnotherListStartsWithAnEmptyBox(unittest.TestCase):
    """After a save the people are in the CRM; the old text read again proposes
    them as matches for themselves."""

    def setUp(self):
        self.intake = page("intake.js")

    def test_the_button_after_a_save_empties_the_text(self):
        done = braced_block(self.intake, "${phase === \"done\" && html`")
        self.assertIn("Paste another list", done)
        self.assertRegex(done, r"onClick=\$\{pasteAnother\}>\s*Paste another list")

    def test_pasteAnother_clears_the_box_as_well_as_the_review(self):
        body = js_statement(self.intake, "const pasteAnother")
        self.assertIn("reset()", body)
        self.assertIn('setText("")', body)

    def test_start_again_still_keeps_the_text(self):
        """The control. Start again is on the review screen, before anything is
        saved, where keeping the paste is what you want."""
        reset = fn(self.intake, "const reset = () =>")
        self.assertNotIn("setText", reset)
        self.assertRegex(self.intake, r"onClick=\$\{reset\}>\s*Start again")


# ── NC-356 ───────────────────────────────────────────────────────────────────

def store_dict(saved):
    """What localStorage holds when `saved` is the person's saved choices."""
    return {} if saved is None else {"nc_ask_shape": json.dumps(saved)}


class TestANewChatStartsFromTheSavedChoices(NodeCase):
    """New Chat posted the settings of the chat that was open, not the saved
    People, Detail and Search choices."""

    def new_chat(self, stored):
        ask = page("ask.js")
        return self.run_js(
            const(ask, "const SHAPE_KEY") + "\n"
            + const(ask, "const SHAPE_DEFAULTS") + "\n"
            + fn(ask, "function loadShape") + "\n"
            + """
            const store = %s;
            const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem() {} };
            // The OPEN chat's own settings, restored into the chips without being saved.
            let shape = { people: "10", detail: "brief", search: "quick" };
            const setShape = (s) => { shape = s; };
            const posted = [];
            const api = async (path, opts) => { posted.push(JSON.parse(opts.body)); return { conversation: { id: 7 } }; };
            const toast = () => {};
            const loadChats = async () => {};
            const go = () => {};
            const setReloadKey = () => {};
            const inputRef = { current: { focus() {} } };
            const convId = 3, msgs = [{ role: "user" }];
            """ % json.dumps(store_dict(stored))
            + fn(ask, "const newChat = async () =>") + """;
            await newChat();
            return { posted, chips: shape };
            """)

    def test_the_saved_choices_are_posted_not_the_open_chats(self):
        saved = {"people": "25", "detail": "full", "search": "thorough"}
        out = self.new_chat(saved)
        self.assertEqual(out["posted"], [{"shape": saved}])
        self.assertEqual(out["chips"], saved)

    def test_with_nothing_saved_the_defaults_are_posted(self):
        out = self.new_chat(None)
        self.assertEqual(out["posted"],
                         [{"shape": {"people": "all", "detail": "full", "search": "thorough"}}])


# ── NC-305 (the page's half) ─────────────────────────────────────────────────

class TestMostMessagesIsASort(NodeCase):
    """A fourth way to order People, asked of the server so it holds across the
    whole list, and shown in the order each row's own count gives."""

    def setUp(self):
        self.people = page("people.js")

    def test_the_picker_offers_it_under_the_key_the_engine_is_asked_for(self):
        sorts = self.run_js(const(self.people, "const SORTS") + "\nreturn SORTS;")
        self.assertIn(["messages", "Most messages"], sorts)

    def test_the_chosen_sort_is_sent_to_the_server_as_it_is(self):
        load = fn(self.people, "const load = useCallback(async (")
        self.assertIn("params.sort = effSortF", load)
        self.assertIn('const effSortF = sortF || "closeness"', load)

    def order(self, sort, rows):
        ui = page("ui.js")
        return self.run_js(
            without_export(fn(ui, "export function messageTotal")) + "\n"
            + fn(self.people, "function orderRows") + "\n"
            + "return orderRows(%s, %s).map(c => c.name);" % (json.dumps(rows), json.dumps(sort)))

    ROWS = [
        {"name": "Ada", "last": "2026-01-01", "counts": {"linkedin": 5}},
        {"name": "Grace", "last": "2025-01-01", "counts": {"whatsapp": 40, "imessage": 30, "email": 9}},
        {"name": "Alan", "last": "2026-03-01", "counts": {"linkedin": 30, "whatsapp": 30}},
        {"name": "Nobody", "last": "", "counts": {}},
    ]

    def test_the_most_messages_across_every_channel_comes_first(self):
        # Grace: 79 over three channels. Alan: 60. Ada: 5. A sort on one channel
        # would put Alan (30 + 30) and Grace (40 on one) in the wrong order.
        self.assertEqual(self.order("messages", self.ROWS),
                         ["Grace", "Alan", "Ada", "Nobody"])

    def test_equal_totals_keep_the_order_the_server_gave(self):
        rows = [{"name": n, "counts": {"email": 7}} for n in ("Ada", "Grace", "Alan")]
        self.assertEqual(self.order("messages", rows), ["Ada", "Grace", "Alan"])

    def test_the_other_sorts_are_unchanged(self):
        self.assertEqual(self.order("closeness", self.ROWS), ["Ada", "Grace", "Alan", "Nobody"])
        self.assertEqual(self.order("quiet", self.ROWS), ["Grace", "Ada", "Alan", "Nobody"])
        self.assertEqual(self.order("recent", self.ROWS), ["Alan", "Ada", "Grace", "Nobody"])

    def test_the_table_orders_its_rows_through_that_function(self):
        self.assertIn("const sorted = orderRows(visible, effSort)", self.people)


# ── NC-336 (the page's half) ─────────────────────────────────────────────────

class TestAPersonPutBackOnAListIsSaidToBeAdded(NodeCase):
    """The engine counts a removed member put back as added; the page must then
    say added, never "already in it"."""

    def say(self, name, sent, added):
        ui = page("ui.js")
        return self.run_js(without_export(fn(ui, "export function addedMessage"))
                           + "\nreturn addedMessage(%s, %s, %s);"
                           % (json.dumps(name), sent, json.dumps(added)))

    def test_one_person_restored_is_added(self):
        self.assertEqual(self.say("Team dinner", 1, 1), "Added 1 person to “Team dinner”")

    def test_only_nothing_added_says_already_in_it(self):
        self.assertEqual(self.say("Team dinner", 1, 0),
                         "“Team dinner” already holds everyone you selected")

    def test_a_mix_says_how_many_were_new(self):
        self.assertEqual(self.say("Team dinner", 5, 3),
                         "Added 3 people to “Team dinner” · 2 already in the list")

    def test_a_missing_count_trusts_what_was_sent(self):
        """A list that was just created answers without `added` for none of its
        people being old, so what was sent is what was added."""
        self.assertEqual(self.say("New list", 2, None), "Added 2 people to “New list”")

    def test_the_popover_says_what_the_engine_added(self):
        ui = page("ui.js")
        commit = fn(ui, "const commit = async (listId, listName) =>")
        self.assertIn("addedMessage(listName, ids.length, d?.added)", commit)

    def test_the_other_two_ways_to_add_read_the_count_too(self):
        """Dragging a person onto a list and the Add a person box used to ignore
        the answer and say "Added" over a list that already held them."""
        lists = page("lists.js")
        drop = fn(lists, "onDrop: async (listId, person) =>")
        self.assertIn("d.added === 0", drop)
        add = fn(lists, "const add = async (c) =>")
        self.assertIn("d.added === 0", add)


if __name__ == "__main__":
    unittest.main()
