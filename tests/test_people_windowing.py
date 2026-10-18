"""Gates on the windowed People table — NC-084.

WHY THIS FILE EXISTS
--------------------
The People table used to draw every row, however many there were. A large list
put tens of thousands of elements in the page and made every scroll step costly
to lay out — which showed as stutter. Drawing only the rows in view removes that
cost.

The trade is that the table now depends on ARITHMETIC where it used to depend on
measuring elements: row n is at n x --row-h, because a row that was never drawn
has no position to measure. Three separate things break silently if that
arithmetic stops holding, and none of them raises an error — the selection simply
covers the wrong people, or the keyboard cursor walks off screen. So the
assumptions are gated here in source, since there is no way to execute the front
end from this suite.

Each gate below is tied to a specific way this can break, named in its docstring.
A gate that merely encodes a preference does not belong here.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def _no_comments(css):
    """CSS with /* ... */ stripped, so a gate cannot be satisfied by a comment
    that merely mentions the thing it is looking for."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


class TestOnlyTheDrawnRowsAreWindowed(unittest.TestCase):
    """The one rule that keeps windowing from quietly shrinking the app.

    `sorted` is the whole list. Only the map that turns rows into elements may be
    sliced. Every OTHER reader — the id list handed to the selection hook, the
    order shared with the person page's previous/next stepper, and the bulk
    "add these N to a list" footer — has to keep seeing all of them, or
    select-all, shift-click ranges, the stepper and bulk list-adding all narrow
    to one screenful with no error anywhere. Select-all must report every row,
    and the footer must say "Add these N to a list" for all N, however few rows
    are drawn.
    """

    def setUp(self):
        self.js = _read("app", "people.js")

    def test_the_row_map_is_the_only_thing_sliced(self):
        self.assertIn("sorted.slice(win.start, win.end).map", self.js,
                      "the rows are no longer drawn from a window")

    def test_the_id_list_for_selection_reads_every_row(self):
        self.assertIn("const ids = useMemo(() => sorted.map(c => c.id), [idsKey]);", self.js,
                      "the selection hook must be handed every id, not the drawn ones — "
                      "otherwise select-all and shift-click ranges stop at one screenful")

    def test_the_order_shared_with_the_person_page_reads_every_row(self):
        self.assertIn("const idsKey = sorted.map(c => c.id).join(\",\");", self.js)
        self.assertIn("onVisible?.(sorted.map(c => c.id))", self.js,
                      "the previous/next stepper on a person's page would only "
                      "step through the rows that happened to be drawn")

    def test_the_bulk_footer_offers_every_matching_person(self):
        self.assertIn("contactIds=${sorted.map(c => c.id)}", self.js,
                      '"add these N to a list" must mean all N, not the drawn rows')

    def test_no_other_slice_of_sorted_crept_in(self):
        slices = re.findall(r"sorted\.slice\([^)]*\)", self.js)
        self.assertEqual(slices, ["sorted.slice(win.start, win.end)"],
                         "a second slice of `sorted` is almost certainly a set-wide "
                         "reader that has been narrowed to the drawn rows: %r" % (slices,))


class TestTheRowHeightIsOneFixedNumber(unittest.TestCase):
    """Assumption 1 of windowing.js, and the one most likely to be broken by
    accident: every row is exactly --row-h tall.

    Row n is placed at n x --row-h. A media query that made rows shorter on a
    narrow window, or a `.nx-row` variant with its own height, would put every
    computed row number out by a growing amount — so a marquee drag would mark
    people other than the ones under the band, and the keyboard cursor would
    scroll to the wrong place. Neither fails loudly.
    """

    def test_row_h_is_defined_exactly_once_in_the_whole_stylesheet_set(self):
        defs = []
        for name in sorted(os.listdir(os.path.join(STATIC, "css"))):
            if not name.endswith(".css"):
                continue
            css = _no_comments(_read("css", name))
            for m in re.finditer(r"(^|[;{\s])--row-h\s*:", css):
                defs.append(name)
        self.assertEqual(defs, ["tokens.css"],
                         "--row-h is set in more than one place (%r). Windowing places "
                         "row n at n x --row-h, so a second definition — especially "
                         "inside a media query — silently misplaces every row." % (defs,))

    def test_the_people_row_takes_its_height_from_that_token_and_nothing_else(self):
        # .nx-row is written as two rules in this file (one for the grid, one for
        # the button reset), so check every one of them rather than the first.
        css = _no_comments(_read("css", "view-network.css"))
        rules = re.findall(r"(?<![\w.-])\.nx-row\s*\{([^}]*)\}", css)
        self.assertTrue(rules, ".nx-row rule not found")
        heights = [r for r in rules if re.search(r"(^|[;\s])height\s*:", r)]
        self.assertEqual(len(heights), 1,
                         "expected exactly one .nx-row rule to set a height, found %d"
                         % len(heights))
        self.assertIn("height: var(--row-h)", heights[0])
        # A min-height would let a row grow past the number the arithmetic uses.
        for r in rules:
            self.assertNotIn("min-height", r,
                             "a min-height lets a row exceed --row-h, and the row "
                             "numbers computed from the scroll offset then drift")

    def test_no_media_query_touches_the_people_row_height(self):
        css = _no_comments(_read("css", "view-network.css"))
        for block in re.findall(r"@media[^{]*\{(.*?\n\})", css, flags=re.S):
            self.assertNotRegex(
                block, r"\.nx-row[^{]*\{[^}]*height",
                "a media query changes the People row height; windowing cannot "
                "survive that (see windowing.js, assumption 1)")


class TestSelectionNoLongerMeasuresElements(unittest.TestCase):
    """The marquee used to hit-test by asking the page for every row element and
    comparing rectangles. Rows outside the window are not in the page, so that
    version selected only the ~30 that happened to be drawn while a drag
    auto-scrolled past hundreds. A drag across many rows must select all of
    them, including the rows that are not drawn.
    """

    def setUp(self):
        self.js = _read("app", "select.js")

    def test_nothing_asks_the_page_for_the_list_of_rows(self):
        self.assertNotIn('querySelectorAll("[data-row-id]")', self.js,
                         "the marquee is measuring elements again, so it can only "
                         "reach the rows that are currently drawn")

    def test_the_band_is_compared_in_the_scrolled_contents_own_coordinates(self):
        self.assertIn("function contentY(body, clientY)", self.js)
        self.assertIn("body.scrollTop", self.js)
        self.assertIn("startTop = contentY(body, e.clientY)", self.js,
                      "the drag origin must be recorded in content coordinates, or "
                      "it goes stale the moment the list auto-scrolls under it")

    def test_the_rows_under_the_band_come_from_the_id_list(self):
        self.assertRegex(self.js, r"for \(let i = first; i <= last; i\+\+\) hit\.add\(ids\[i\]\)",
                         "the band's rows should come from the full id list by index")


class TestTheKeyboardCursorCannotGetStuck(unittest.TestCase):
    """reveal() used to look the row up in the page and return silently when it
    was not there. Once only the rows in view are drawn, that froze the view on a
    jump to the far end of the list while the cursor carried on moving invisibly.

    It now scrolls by arithmetic first, and the row takes the focus itself when
    it mounts — no timer to be throttled and no animation frame to be starved,
    both of which stop exactly where this needs to work. End must put the scroll
    offset at the true maximum with focus on the last row and on screen; Home
    must return it to 0 with focus on the first; a long burst of ArrowDowns must
    leave the cursor drawn, focused and on screen.
    """

    def setUp(self):
        self.sel = _read("app", "select.js")
        self.people = _read("app", "people.js")

    def test_reveal_scrolls_from_the_row_number_not_from_the_element(self):
        m = re.search(r"function reveal\(body, id, index\) \{(.*?)\n\}", self.sel, flags=re.S)
        self.assertIsNotNone(m, "reveal no longer takes the container and the row number")
        body = m.group(1)
        self.assertIn("index * rowH", body, "the scroll offset must come from the row number")
        self.assertIn("body.scrollTop", body)
        self.assertNotIn("scrollIntoView", body,
                         "scrollIntoView needs the element, which is the thing that "
                         "may not exist")

    def test_both_call_sites_pass_the_row_number(self):
        calls = re.findall(r"reveal\([^)]*\)", self.sel)
        calls = [c for c in calls if c != "reveal(body, id, index)"]   # drop the definition
        self.assertEqual(len(calls), 2, "expected exactly two reveal() call sites: %r" % (calls,))
        for c in calls:
            self.assertRegex(c, r"reveal\(bodyRef\?\.current, id, \w+\)", c)

    def test_focus_is_claimed_when_the_row_mounts(self):
        self.assertIn("export function claimFocus(el, id)", self.sel)
        self.assertIn("let pendingFocus = null;", self.sel)
        # No clock anywhere in the focus path: a timer is throttled and an
        # animation frame is starved in a window that is not visible.
        m = re.search(r"function reveal\(body, id, index\) \{(.*?)\n\}", self.sel, flags=re.S)
        self.assertNotIn("setTimeout", m.group(1))
        self.assertNotIn("requestAnimationFrame", m.group(1))
        self.assertIn("claimFocus", self.people, "people.js must import the claim")
        self.assertIn("ref=${claim}", self.people,
                      "the row has to be wired to the claim, or focus never arrives "
                      "for a row that was not drawn when the key was pressed")
        self.assertIn("useCallback((el) => claimFocus(el, c.id), [c.id])", self.people,
                      "an inline ref would be detached and reattached on every render")


class TestTheKeyboardFocusesTheRowNotTheCheckbox(unittest.TestCase):
    """NC-086. The row wrapper holds two buttons and the marking checkbox comes
    FIRST in document order — it has to be a sibling, because a button inside a
    button is invalid and Safari makes the inner one unclickable. So "focus the
    first focusable thing in here" landed on the checkbox every time.

    It failed quietly in two ways at once, and the visible cursor styling hid
    both: `.nx-row:focus-visible` could never match, so the keyboard had no focus
    ring; and a screen reader read "Mark Ada Lovelace" instead of the row's own
    sentence, which people.js builds specifically so the empty cells get spoken.

    The check needs a real key press rather than a synthetic one — the
    browser's `:focus-visible` heuristic ignores untrusted events, so a synthetic
    ArrowDown proves nothing about the ring. After a real press,
    `.nx-row:focus-visible` must match exactly one element, with a 2px solid
    outline in --accent; the focused element must carry class `nx-row`; the
    checkbox must have no focus; and Space must still mark the row instead of
    opening the person.
    """

    def setUp(self):
        self.js = _read("app", "select.js")

    def test_the_marking_control_is_excluded_from_the_focus_search(self):
        m = re.search(r"function focusIn\(el\) \{(.*?)\n\}", self.js, flags=re.S)
        self.assertIsNotNone(m, "focusIn not found")
        body = m.group(1)
        self.assertIn("button:not(.nx-mark)", body,
                      "without excluding the marking control, the keyboard focuses "
                      "the checkbox: no focus ring, and the wrong thing read aloud")
        self.assertNotRegex(
            body, r'querySelector\("button, a, \[tabindex\]"\)',
            "the un-excluded search is back")

    def test_the_focus_ring_rule_can_actually_match(self):
        """A rule for a selector nothing ever matches is the shape this bug took.
        The row button must be the focus target AND carry the outline rule."""
        css = _no_comments(_read("css", "view-network.css"))
        self.assertRegex(css, r"\.nx-row:focus-visible[^{]*\{[^}]*outline",
                         "the focus ring rule for the row is gone")


class TestTheScrollbarStillDescribesTheWholeList(unittest.TestCase):
    """The spacers are what keep the scrollbar the length of the list: the
    scrolling element must measure exactly the row count times the row height,
    filtered or not.
    """

    def test_both_spacers_are_rendered_from_the_window(self):
        js = _read("app", "people.js")
        self.assertIn('class="nx-pad" style=${"height:" + win.padTop + "px"}', js)
        self.assertIn('class="nx-pad" style=${"height:" + win.padBottom + "px"}', js)

    def test_the_spacers_carry_no_border_or_background(self):
        """A spacer that inherited the row's hairline divider or hover fill would
        read as one giant empty row at the end of the table."""
        css = _no_comments(_read("css", "view-network.css"))
        rules = re.findall(r"\.nx-pad\s*\{([^}]*)\}", css)
        self.assertEqual(len(rules), 1, "expected exactly one .nx-pad rule")
        for prop in ("border", "background", "height"):
            self.assertNotIn(prop, rules[0],
                             ".nx-pad must not set %s — its height is set inline "
                             "per render and it must stay invisible" % prop)

    def test_scroll_anchoring_is_off_on_the_scrolling_element(self):
        """The browser tries to keep whatever you were looking at in place when
        content around it changes. Against a list that swaps rows in and out on
        every scroll, that fights the arithmetic and moves the offset."""
        css = _no_comments(_read("css", "view-network.css"))
        m = re.search(r"\.nx-body\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("overflow-anchor: none", m.group(1))


class TestTheWindowIsHonestAboutItsEdges(unittest.TestCase):
    """windowing.js's own arithmetic, read as source. The clamp is the part worth
    guarding: without it, shrinking the list with a filter could leave `start`
    past the end and draw a tall spacer and no rows — an empty table where rows
    plainly exist.
    """

    def setUp(self):
        self.js = _read("app", "windowing.js")

    def test_the_scroll_offset_is_clamped_to_the_content(self):
        self.assertIn("const maxTop = Math.max(0, count * rowH - el.clientHeight);", self.js)
        self.assertIn("const top = Math.min(el.scrollTop, maxTop);", self.js)

    def test_the_window_never_runs_past_the_end_of_the_list(self):
        self.assertIn("const end = Math.min(count, first + fits + overscan);", self.js)
        self.assertIn("const start = Math.max(0, first - overscan);", self.js)

    def test_an_unchanged_window_does_not_cause_a_render(self):
        """Every scroll event inside one row's worth of travel would otherwise
        re-render the whole table, putting back the cost this file removes."""
        self.assertRegex(self.js, r"setWin\(w => \(w\.start === start && w\.end === end")

    def test_the_row_height_comes_from_css_not_a_constant_in_the_logic(self):
        self.assertIn('getComputedStyle(el).getPropertyValue("--row-h")', self.js)
        # The fallback is allowed to be a number, but nothing else may be.
        body = self.js[self.js.index("export function useRowWindow"):]
        self.assertNotIn("40", body,
                         "a literal row height in the window arithmetic will drift "
                         "from --row-h the first time the token changes")

    def test_the_first_paint_draws_rows_before_anything_is_measured(self):
        """A window that starts empty flashes an empty table for a frame on the
        way in."""
        self.assertIn("start: 0, end: FIRST_PAINT_ROWS", self.js)
        self.assertGreaterEqual(
            int(re.search(r"FIRST_PAINT_ROWS = (\d+)", self.js).group(1)), 20)


if __name__ == "__main__":
    unittest.main()
