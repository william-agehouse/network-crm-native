/* ── Only the rows you can actually see exist in the page ─────────────────────
 *
 * The People table can hold thousands of people. Drawing all of them put tens of
 * thousands of elements in the page, and the browser then spent most of the 16.7
 * milliseconds a frame gets at 60 frames a second working out where they all go,
 * on every scroll step. That was the stutter: at 500 rows the same measurement is
 * a small fraction of that and feels fine. The cost grew in direct proportion to
 * the element count, which is the signature of laying out rows nobody can look
 * at. Pressing "Load all" froze the window for several seconds, building every
 * row from the fifteenth on. NC-084.
 *
 * The fix: draw the rows in view plus a margin either side, and keep the
 * scrollbar honest with two empty spacer elements — one above the drawn rows,
 * one below — each as tall as the rows it stands in for. Fourteen rows fit on
 * screen, so about thirty elements-worth of rows exist at any moment instead of
 * thousands.
 *
 * Spacers rather than absolutely-positioned rows, on purpose: the rows stay in
 * normal document flow, so the grid columns, the hairline dividers and the hover
 * fill all keep working untouched, and the scrolling element needs no
 * `position: relative`. It also keeps the spacers out of every
 * `[data-row-id]` query in select.js, which would otherwise mistake them for
 * rows.
 *
 * TWO ASSUMPTIONS, both load-bearing, both guarded by tests:
 *
 * 1. EVERY ROW IS THE SAME HEIGHT, and that height is --row-h (40px in
 *    tokens.css, no media-query override, and `compact` mode on this table only
 *    rings the avatar). Row n therefore begins at exactly n × 40px. That is what
 *    lets the marquee and the keyboard cursor work out where a row IS without
 *    asking the page where it is — and they have to, because a row that was
 *    never drawn cannot be measured.
 *
 * 2. THE SCROLLING ELEMENT CONTAINS NOTHING BUT ROWS AND THESE TWO SPACERS. A
 *    header or a banner inside it would push every row down by its height, and
 *    every row number worked out from a pixel offset would then be wrong. The
 *    People table's header is a SIBLING of the scroller, outside it, which is
 *    what makes the arithmetic safe. If you ever put something else in there,
 *    this breaks silently and selection starts hitting the wrong people.
 *
 * WHAT THIS DELIBERATELY DOES NOT TOUCH: the full list of ids. Only the map that
 * turns rows into elements may be windowed. Everything that reasons about the
 * SET — select-all, shift-click ranges, the person page's previous/next stepper,
 * "add these N to a list" — keeps reading the whole list, or it would silently shrink
 * to one screenful. See the note at people.js's `sorted`.
 */
import { useState, useEffect } from "../vendor/preact-htm.module.js";

/* How many rows to draw beyond each edge of the view. Eight is a bit over half a
 * screenful here, which means a wheel flick or a held arrow key lands on a row
 * that has already been drawn: the window only ever has to catch up, and is
 * never the thing being waited for. */
export const OVERSCAN = 8;

/* How many rows to draw on the very first paint, before anything has been
 * measured. Enough to fill a tall window, so the table never flashes empty for a
 * frame on the way in — and cheap: 40 rows is 760 elements against tens of thousands. */
export const FIRST_PAINT_ROWS = 40;

/* The value of --row-h in tokens.css. Only used when there is no laid-out
 * element to read it from — a detached node in a test, or a render before mount. */
export const ROW_H_FALLBACK = 40;

/** Row height in pixels, read from CSS rather than held as a constant here, so
 *  --row-h stays the single place it is defined. */
export function rowHeight(el) {
  if (!el || typeof getComputedStyle !== "function") return ROW_H_FALLBACK;
  const n = parseFloat(getComputedStyle(el).getPropertyValue("--row-h"));
  return n > 0 ? n : ROW_H_FALLBACK;
}

/** Which rows to draw, and how tall the spacers above and below them must be.
 *
 *  @param count    how many rows there are in total — ALL of them, not a page
 *  @param ref      a ref on the scrolling element
 *  @param overscan rows to draw past each edge; defaults to OVERSCAN
 *  @returns {{start, end, padTop, padBottom, rowH}} — draw rows[start..end)
 */
export function useRowWindow({ count, ref, overscan = OVERSCAN }) {
  const [win, setWin] = useState({
    start: 0, end: FIRST_PAINT_ROWS, rowH: ROW_H_FALLBACK,
  });

  useEffect(() => {
    const el = ref?.current;
    if (!el) return;
    let queued = false;

    const measure = () => {
      queued = false;
      const rowH = rowHeight(el);
      // Clamp the scroll offset here rather than trusting it. When a filter
      // shrinks the list the browser clamps its own scrollTop and fires a scroll
      // event — but a render from the smaller `count` can land first, and an
      // unclamped offset would put `start` past the end of the list and draw a
      // tall spacer and no rows at all: an empty table where rows plainly exist.
      const maxTop = Math.max(0, count * rowH - el.clientHeight);
      const top = Math.min(el.scrollTop, maxTop);
      const first = Math.floor(top / rowH);
      // +1 for the row that is only half on screen at the bottom.
      const fits = Math.ceil(el.clientHeight / rowH) + 1;
      const start = Math.max(0, first - overscan);
      const end = Math.min(count, first + fits + overscan);
      // Same window means the same object, so preact sees no change and does not
      // re-render. Without this every scroll event inside one row's worth of
      // travel would re-render the whole table — which is the cost this file
      // exists to remove.
      setWin(w => (w.start === start && w.end === end && w.rowH === rowH
        ? w : { start, end, rowH }));
    };

    // One measurement per frame at most. A trackpad flick fires `scroll` far
    // more often than the screen refreshes, and measuring per event would put
    // back a per-event layout read of exactly the kind we are removing.
    const onScroll = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(measure);
    };

    measure();
    el.addEventListener("scroll", onScroll, { passive: true });
    // Resizing the window changes how many rows fit; so does opening a person,
    // which narrows the table to a 324px rail. A ResizeObserver catches both,
    // where a window resize listener would miss the second.
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => {
      el.removeEventListener("scroll", onScroll);
      ro?.disconnect();
    };
  }, [count, overscan]);

  const start = Math.max(0, Math.min(win.start, count));
  const end = Math.max(start, Math.min(win.end, count));
  return {
    start, end, rowH: win.rowH,
    padTop: start * win.rowH,
    padBottom: (count - end) * win.rowH,
  };
}
