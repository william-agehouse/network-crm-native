"""NC-010 — the declared half of who the owner is, and where it reaches.

What it is for: a page where you describe your own goals.

The measured half is crm/style_profile.py, which reads the owner's sent messages
and derives how they write. Nothing else held what the owner is trying to *do*,
and `chat.py`'s SYSTEM_PROMPT carried no free-text block about them at all — so
there was no code path that would pick such text up even if a page existed to
type it into. These tests pin the substrate: one state row, three prompts, and
an install that has been told nothing producing exactly the request it did before.
"""

import subprocess
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

import app  # noqa: E402
import chat as chat_mod  # noqa: E402
import owner  # noqa: E402



def _author_first_name():
    """No shipped prompt may carry a real person's name. The name checked for is
    the first name of whoever made the latest commit, read from git rather than
    written out here; "" when this is not a git checkout."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        name = subprocess.run(["git", "log", "-1", "--format=%an"], cwd=here,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return name.split()[0] if name else ""


_AUTHOR_FIRST = _author_first_name()


class TestTheOwnerBlockIsBuiltFromWhatWasActuallySaid(Base):
    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ("SELF_NAME", "SELF_EMAIL")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_an_install_that_was_told_nothing_adds_nothing(self):
        """The important one. Every caller appends this conditionally, so "" has to
        mean "produce the request you produced before this module existed". A block
        reading "the user has not told you anything" would spend tokens to add
        nothing and invite the model to remark on the absence."""
        self.assertEqual(owner.context_block(self.conn), "")
        self.assertEqual(chat_mod._system_blocks("", ""),
                         chat_mod._system_blocks(""))

    def test_a_name_alone_is_enough_to_produce_a_block(self):
        os.environ["SELF_NAME"] = "Ada Lovelace"
        block = owner.context_block(self.conn)
        self.assertIn("Ada Lovelace", block)
        self.assertNotIn('"""', block, "no goals were set, so there is no quoted text")

    def test_the_goals_text_is_quoted_and_marked_as_being_about_the_owner(self):
        """A brief and a draft are generated PER CONTACT. Without the last
        paragraph, a model reading "wants to learn to bake bread" in the system
        prompt attributes it to whichever contact it is describing."""
        owner.save_goals(self.conn, "Learning to bake bread. Want intros to local bakers.")
        block = owner.context_block(self.conn)
        self.assertIn("Learning to bake bread", block)
        self.assertIn('"""', block)
        self.assertIn("never a contact", block,
                      "nothing stops the model attributing the owner's goals to "
                      "the contact it is writing about")
        # And the instruction has to name WHO not to attribute it to. "do not
        # attribute any of it to anyone" would pass the assertion above while
        # telling the model to ignore the block entirely.
        self.assertIn("the person you are writing about", block,
                      "the instruction does not say who the goals must not be "
                      "attributed to, so it reads as 'ignore this block'")

    def test_goals_longer_than_the_cap_are_trimmed_not_rejected(self):
        stored = owner.save_goals(self.conn, "x" * (owner.GOALS_MAX_CHARS + 500))
        self.assertEqual(len(stored), owner.GOALS_MAX_CHARS)
        self.assertEqual(db.state_get(self.conn, owner.GOALS_STATE_KEY), stored,
                         "save_goals returned something other than what it stored, "
                         "so the UI would keep showing text the database does not have")

    def test_the_display_name_never_guesses_at_a_person(self):
        """A prompt that names the wrong human writes confidently in that
        person's voice, which is worse than naming nobody, so with no name set
        the display name is "the user", never a guess."""
        self.assertEqual(owner.display_name(self.conn), "the user")
        os.environ["SELF_NAME"] = "Ada Lovelace"
        self.assertEqual(owner.display_name(self.conn), "Ada Lovelace")


class TestTheCacheMarkerStaysOnTheLastStableBlock(Base):
    """The cost decision in chat._system_blocks. A cache breakpoint caches the
    prefix up to AND INCLUDING its own block, so the marker has to sit on the last
    block that does not change between questions. Get it wrong in one direction and
    every question re-sends the owner block forever; get it wrong in the other and
    every Brief/Full switch writes a new ~950-token cache entry."""

    def test_with_no_owner_block_the_marker_is_on_the_system_prompt(self):
        blocks = chat_mod._system_blocks("SHAPE")
        self.assertEqual(blocks[0]["text"], chat_mod.SYSTEM_PROMPT)
        self.assertIn("cache_control", blocks[0])
        self.assertNotIn("cache_control", blocks[-1],
                         "the per-question shape directive is inside the cached "
                         "prefix, so every Brief/Full switch re-pays it")

    def test_with_an_owner_block_the_marker_moves_onto_it(self):
        blocks = chat_mod._system_blocks("SHAPE", "ABOUT THE PERSON")
        # Three blocks, and the last one is the per-question tail. It carries
        # today's date as well as the directive (NC-081), for exactly this test's
        # reason: a date inside the cached prefix would rewrite the whole prefix
        # once a day. So the assertion is on what this class is actually about
        # (which block is where, and where the marker sits), and the tail's
        # contents are pinned by
        # test_chat_messages.TestTheModelIsToldWhatDayItIs.
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["text"], chat_mod.SYSTEM_PROMPT)
        self.assertEqual(blocks[1]["text"], "ABOUT THE PERSON")
        self.assertIn("SHAPE", blocks[2]["text"])
        self.assertNotIn("cache_control", blocks[0],
                         "two markers, or the wrong one: the cached prefix stops "
                         "before the owner block and it is re-sent every question")
        self.assertIn("cache_control", blocks[1])
        self.assertNotIn("cache_control", blocks[2])

    def test_exactly_one_marker_ever(self):
        for directive, owner_block in (("", ""), ("S", ""), ("", "O"), ("S", "O")):
            blocks = chat_mod._system_blocks(directive, owner_block)
            marked = [b for b in blocks if "cache_control" in b]
            self.assertEqual(len(marked), 1,
                             f"{len(marked)} markers for "
                             f"directive={directive!r} owner={owner_block!r}")

    def test_the_shape_directive_is_always_last_when_present(self):
        """Last, and outside the cached prefix. Everything that varies per
        question or per day shares that one trailing block; the rule is that
        nothing varying is above the marker, not that the directive is alone."""
        blocks = chat_mod._system_blocks("SHAPE", "OWNER")
        self.assertIn("SHAPE", blocks[-1]["text"])
        self.assertNotIn("cache_control", blocks[-1])
        for block in blocks[:-1]:
            self.assertNotIn("SHAPE", block["text"],
                             "the per-question directive leaked into a cached "
                             "block, so every Brief/Full switch re-pays the "
                             "whole prefix")


class TestTheAskPathActuallyPassesItIn(Base):
    """_system_blocks can be perfect and still never receive an owner block.

    chat.chat() needs ANTHROPIC_API_KEY and the anthropic package, so no test
    calls it — so deleting `owner.context_block(conn)` from its one call site
    would break nothing. This gates the wiring itself.
    """

    def test_the_one_call_site_passes_the_owner_block(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "crm", "chat.py"), encoding="utf-8") as fh:
            src = fh.read()
        calls = [ln.strip() for ln in src.splitlines()
                 if "_system_blocks(" in ln and not ln.lstrip().startswith(("#", "def ", '"'))]
        self.assertEqual(len(calls), 1,
                         f"expected exactly one call site, found {len(calls)}: {calls}")
        self.assertIn("owner.context_block(conn)", calls[0],
                      "Ask builds its system prompt without the owner block, so the "
                      "goals text reaches the briefs and not the thing the owner actually asks")

    def test_chat_imports_owner_without_shadowing_it(self):
        """The parameter is `owner_block`, not `owner`, precisely so the module
        stays reachable inside the function that uses it."""
        import chat
        self.assertIs(chat.owner, __import__("owner"))
        import inspect
        params = list(inspect.signature(chat._system_blocks).parameters)
        self.assertEqual(params, ["directive", "owner_block"])


class TestBothBriefPromptsCarryIt(Base):
    """One state row feeding three prompts is the reason owner.py is a module and
    not three inlined lines. A caller that forgets it is a silent divergence."""

    def setUp(self):
        super().setUp()
        self.cid = db.create_contact(self.conn, "Grace Hopper")
        self.conn.commit()
        os.environ["SELF_NAME"] = "Ada Lovelace"
        owner.save_goals(self.conn, "Building a greenhouse.")
        self.systems = []
        import api_briefs
        self.api_briefs = api_briefs
        self._orig = api_briefs._run_claude

        def spy(system, user_content, max_tokens, error_label):
            self.systems.append(system)
            return "text"
        api_briefs._run_claude = spy

    def tearDown(self):
        self.api_briefs._run_claude = self._orig
        os.environ.pop("SELF_NAME", None)
        super().tearDown()

    def test_the_brief_prompt_carries_the_owner_block(self):
        self.api_briefs.handle_make_brief(self.conn, self.cid)
        self.assertEqual(len(self.systems), 1)
        self.assertIn("Building a greenhouse", self.systems[0])
        self.assertIn("story so far", self.systems[0],
                      "the owner block replaced the job description instead of "
                      "being appended to it")

    def test_the_draft_prompt_uses_the_owner_name_and_not_a_hardcoded_one(self):
        self.api_briefs.handle_draft_message(self.conn, self.cid)
        self.assertEqual(len(self.systems), 1)
        self.assertIn("from Ada Lovelace", self.systems[0])
        if _AUTHOR_FIRST:
            self.assertNotIn(_AUTHOR_FIRST, self.systems[0],
                             "a personal name is hardcoded in a shipped prompt")
        self.assertIn("Building a greenhouse", self.systems[0])

    def test_no_shipped_prompt_string_names_a_real_person(self):
        """The gate, not just the fix. Anchored on the prompt strings themselves,
        since comments are never sent to the model."""
        if not _AUTHOR_FIRST:
            self.skipTest("not a git checkout, so there is no author to look for")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "crm", "api_briefs.py"), encoding="utf-8") as fh:
            src = fh.read()
        prompt_lines = [ln for ln in src.splitlines()
                        if '"' in ln and not ln.lstrip().startswith("#")]
        offenders = [ln.strip() for ln in prompt_lines if _AUTHOR_FIRST in ln]
        self.assertEqual(offenders, [],
                         f"a personal name is inside prompt text: {offenders}")


class TestTheTextHasSomewhereToComeFrom(Base):
    """NC-010's second half. A substrate with nowhere to type into it is dead
    code: three prompts would read a state row that no UI could ever write. These
    pin the endpoint pair."""

    def test_both_routes_are_declared_and_point_at_real_handlers(self):
        self.assertEqual(owner.ROUTES["GET"]["/api/owner"], "handle_owner")
        self.assertEqual(owner.ROUTES["POST"]["/api/owner/goals"],
                         "handle_owner_goals")
        for name in ("handle_owner", "handle_owner_goals"):
            self.assertTrue(callable(getattr(owner, name, None)), name)

    def test_the_module_is_actually_in_the_route_registry(self):
        # Declaring ROUTES is half of it. A module can be finished, tested and
        # unreachable because the OTHER half is missing, so assert the list
        # itself rather than trusting the declaration.
        self.assertIn(owner, app.ROUTE_MODULES)

    def test_the_get_hands_the_ui_the_ceiling_so_no_number_is_duplicated(self):
        got = owner.handle_owner(self.conn)
        self.assertEqual(got["max_chars"], owner.GOALS_MAX_CHARS)
        self.assertEqual(set(got), {"name", "email", "goals", "max_chars"})

    def test_a_save_round_trips_through_the_get(self):
        owner.handle_owner_goals(self.conn, {"goals": "  learning to bake bread  "})
        self.assertEqual(owner.handle_owner(self.conn)["goals"],
                         "learning to bake bread")

    def test_a_save_reports_truncation_because_the_client_cannot_tell(self):
        short = owner.handle_owner_goals(self.conn, {"goals": "  short  "})
        self.assertFalse(short["truncated"])
        self.assertEqual(short["goals"], "short")
        long = owner.handle_owner_goals(
            self.conn, {"goals": "x" * (owner.GOALS_MAX_CHARS + 50)})
        self.assertTrue(long["truncated"])
        self.assertEqual(len(long["goals"]), owner.GOALS_MAX_CHARS)

    def test_a_non_string_body_is_a_400_not_a_500(self):
        for bad in (42, ["a"], {"a": 1}, None):
            with self.assertRaises(app.ApiError) as cm:
                owner.handle_owner_goals(self.conn, {"goals": bad})
            self.assertEqual(int(cm.exception.status), 400, bad)
        # A missing key defaults to "" and is a legitimate clear, not an error.
        self.assertEqual(owner.handle_owner_goals(self.conn, {})["goals"], "")

    def test_the_write_survives_the_connection_that_made_it(self):
        """The one that a same-connection test cannot see.

        db.state_set does not commit and app.py's _with_conn closes without
        committing, so an uncommitted write is rolled back at the end of every
        request — while the handler still returns 200 with the text echoed back,
        and every other test in this class passes, because an uncommitted write
        is visible to reads on the same connection. Read through a SECOND
        connection, which is what the next HTTP request actually is.
        """
        owner.handle_owner_goals(self.conn, {"goals": "learning to bake bread"})
        other = db.get_db(self.path)
        try:
            self.assertEqual(other.execute(
                "SELECT value FROM state WHERE key = ?",
                (owner.GOALS_STATE_KEY,)).fetchone()["value"],
                "learning to bake bread")
        finally:
            other.close()

    def test_what_the_endpoint_stores_is_what_the_prompt_block_reads(self):
        # The one join that matters: if the endpoint wrote a different state key
        # than context_block reads, every test above would still pass and the
        # feature would do nothing.
        owner.handle_owner_goals(self.conn, {"goals": "hiring a shop manager"})
        self.assertIn("hiring a shop manager", owner.context_block(self.conn))


class TestSettingsActuallyRendersIt(Base):
    """Source gates. The endpoints above are exercised in Python; the textarea is
    not, and "backend done, nothing to type into" is the state NC-010 exists to
    prevent."""

    def setUp(self):
        super().setUp()
        here = os.path.dirname(os.path.abspath(__file__))
        self.js = open(os.path.join(here, "..", "static", "app",
                                    "settings.js")).read()
        self.css = open(os.path.join(here, "..", "static", "css",
                                     "view-settings.css")).read()

    def goals_block(self):
        block = self.js[self.js.index("export function GoalsField("):]
        return block[:block.index("/** Optional semantic")]

    def test_the_component_exists_and_is_mounted_not_merely_defined(self):
        self.assertIn("export function GoalsField(", self.js)
        self.assertIn("<${GoalsField} onSaved=${onChanged} />", self.js)

    def test_it_is_mounted_for_the_goals_row_and_the_server_offers_that_row(self):
        # NC-236: the old page mounted it inside its Essentials tier. The rebuilt
        # page draws whatever acts the server sends, so the two joins are: the
        # `goals` act renders the component, and the goals row sends that act.
        acts = self.js[self.js.index("function Acts("):]
        case = acts[acts.index('case "goals":'):acts.index('case "semantic":')]
        self.assertIn("<${GoalsField}", case)
        import api_abilities
        row = api_abilities._goals(self.conn)
        self.assertEqual(row["key"], "goals")
        self.assertIn({"kind": "goals"}, row["detail"]["acts"])
        self.assertIn("goals", [r[0] for r in api_abilities.ROWS])

    def test_it_talks_to_the_owner_endpoints_and_not_to_settings(self):
        block = self.goals_block()
        self.assertIn('api("/api/owner")', block)
        self.assertIn('"/api/owner/goals"', block)
        # Writing this through /api/settings would put prose into secrets/.env.
        self.assertNotIn("/api/settings", block)
        # And there is no .env key to hint at, so no KeyHint.
        self.assertNotIn("KeyHint", block)

    def test_the_ceiling_is_read_from_the_server_not_hardcoded_as_a_limit(self):
        block = self.goals_block()
        self.assertIn("state.max_chars", block)
        # A maxlength attribute would silently swallow keystrokes at the limit
        # instead of letting the counter warn and the save trim.
        self.assertNotIn("maxlength", block.lower())

    def test_enter_alone_does_not_save_a_textarea(self):
        block = self.goals_block()
        # The newline is the point. The accelerator must be modifier-gated.
        self.assertIn("e.metaKey || e.ctrlKey", block)
        self.assertIn('e.key === "Escape"', block)

    def test_the_textarea_has_css_of_its_own(self):
        # .settings-field-row's `.input { flex: 1 }` would squeeze a textarea in
        # beside the Save button. It renders on its own line, so it needs a rule.
        #
        # Anchored on the rule opening, not the bare class name: renaming the
        # selector to `.settings-goals-unused` would leave the substring intact
        # and the gate green. The class the JS applies must be a selector that
        # actually opens a block.
        self.assertRegex(self.css, r"\.settings-goals\s*\{")
        self.assertIn('class="input settings-goals"', self.js)

    def test_the_rule_can_actually_beat_the_shared_input_rule(self):
        # A cascade collision: ui.css has
        # `.input, textarea.input { min-height: var(--ctl-h) }`, so a bare
        # `.settings-goals` (0,1,0) loses to `textarea.input` (0,1,1) and the
        # box's min-height silently resolves to 28px. It LOOKS right because
        # rows="5" supplies the height — until you drag the resize handle.
        # The element qualifier is what makes the rule win, so pin it.
        self.assertRegex(self.css, r"textarea\.settings-goals\s*\{")
