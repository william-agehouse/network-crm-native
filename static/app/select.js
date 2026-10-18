/* ── Row selection — a mark per row, the usual range and toggle keys ──────────
 * One hook, shared by any list of rows that wants marking. The contract the app
 * settled on:
 *
 *   click            open the row (unchanged — this is the app's primary act)
 *   hover            a mark control appears in place of the avatar
 *   ⇧ click          extend the range from the anchor
 *   ⌘ / ctrl click   toggle one row, and move the anchor there
 *   ↑ ↓              move a roving focus (⇧ extends while moving)
 *   space            mark / unmark the focused row
 *   ⏎                open the focused row
 *   ⌘A               mark everything on screen
 *   esc              clear
 *   drag on empty    marquee — a rubber band that marks what it crosses
 *
 * Two rules that matter more than they look:
 *
 * 1. Plain click still OPENS. Finder marks on a single click, but opening a
 *    person is what this app is for and it is done constantly;
 *    retraining it to a double-click to buy a marking gesture would be a bad
 *    trade. Marking gets the modifiers and the checkbox instead.
 *
 * 2. Rows are found by ARITHMETIC, not by measuring elements. The People table
 *    only draws the rows in view (see windowing.js), so a row twenty screens
 *    down is not in the page at all and has no rectangle to compare against.
 *    Both the marquee and the keyboard cursor therefore work out where row n is
 *    from `n x --row-h` and the scroll offset. This assumes every row is the
 *    same height and that the scrolling element holds nothing but rows and the
 *    two windowing spacers — which is true of the one table that uses this hook,
 *    and is spelled out in full at the top of windowing.js. Measuring elements
 *    instead selected only the rows that happened to be drawn.
 *
 * 3. The anchor is a separate thing from the focus. ⇧ click extends from the
 *    last row you deliberately touched, not from wherever the keyboard happens
 *    to be — which is what makes a click, then a ⇧ click twenty rows down,
 *    select the twenty rows between them rather than something surprising.
 */
import { html, useState, useRef, useCallback, useEffect } from "../vendor/preact-htm.module.js";
import { rowHeight } from "./windowing.js";

/* Below this many pixels a pointer-down is a click, not a drag. Without it every
 * slightly unsteady click on the table background would paint a 2px marquee and
 * wipe the selection. 10px is the same hysteresis a native list uses. */
const DRAG_THRESHOLD = 10;

const isMod = (e) => e.metaKey || e.ctrlKey;

/**
 * @param ids        every row id currently on screen, in visual order
 * @param onOpen     called with an id when a row should open
 * @param bodyRef    the scroll container; marquee drags start on its background
 */
export function useRowSelection({ ids, onOpen, bodyRef }) {
  const [selected, setSelected] = useState(() => new Set());
  const [focusId, setFocusIdState] = useState(null);
  const [marquee, setMarquee] = useState(null);   // {x, y, w, h} in client px
  const anchorRef = useRef(null);

  /* The cursor lives in a ref as well as in state, and the ref is what the key
     handler reads.
     Holding ↓ fires keydown on the OS repeat interval, which is faster than
     preact re-renders: several handlers then run against the same rendered
     closure, all read the same stale focusId, and all compute the same "one
     below that" — so the cursor moves a single row for a whole press. Writing
     the ref synchronously means each repeat sees where the previous one landed.
     The state copy still exists because rendering needs it. */
  const focusRef = useRef(null);
  const setFocusId = useCallback((id) => {
    focusRef.current = id;
    setFocusIdState(id);
  }, []);

  // Rows come and go (search, filter, "load all"). Anything no longer on screen
  // is dropped from the selection rather than kept invisibly: an action bar
  // reading "12 selected" when four of them are filtered out is a lie, and
  // "Add to list" would silently act on rows the user cannot see.
  useEffect(() => {
    setSelected((prev) => {
      if (prev.size === 0) return prev;
      const live = new Set(ids);
      const next = new Set([...prev].filter((id) => live.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [ids]);

  const clear = useCallback(() => {
    setSelected((prev) => (prev.size ? new Set() : prev));
  }, []);

  const selectAll = useCallback(() => setSelected(new Set(ids)), [ids]);

  const toggle = useCallback((id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    anchorRef.current = id;
    setFocusId(id);
  }, []);

  /* Range from the anchor to `id`, inclusive. `additive` keeps whatever was
     already marked, which is what ⌘⇧ click does in a native list. */
  const extendTo = useCallback((id, additive) => {
    const from = ids.indexOf(anchorRef.current);
    const to = ids.indexOf(id);
    if (to < 0) return;
    // No anchor yet (⇧ click as the very first act): treat it as marking one.
    const lo = from < 0 ? to : Math.min(from, to);
    const hi = from < 0 ? to : Math.max(from, to);
    const range = ids.slice(lo, hi + 1);
    setSelected((prev) => (additive ? new Set([...prev, ...range]) : new Set(range)));
    setFocusId(id);
  }, [ids]);

  /* The row's own pointer handler. Returns true when it consumed the event, so
     the caller knows not to also open the row. */
  const onRowPointerDown = useCallback((e, id) => {
    if (e.button === 2) return false;               // right-click is the menu's
    if (e.shiftKey) {
      // A ⇧-drag over text would otherwise paint the page blue behind the range.
      e.preventDefault();
      extendTo(id, isMod(e));
      return true;
    }
    if (isMod(e)) {
      e.preventDefault();
      toggle(id);
      return true;
    }
    return false;
  }, [extendTo, toggle]);

  const onKeyDown = useCallback((e) => {
    // Never steal keys from a field — the table lives under a search box.
    const t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (!ids.length) return;

    const focus = focusRef.current;
    const i = focus == null ? -1 : ids.indexOf(focus);

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      // From nothing, ↓ starts at the top and ↑ starts at the bottom.
      const next = i < 0 ? (step === 1 ? 0 : ids.length - 1)
                         : Math.min(ids.length - 1, Math.max(0, i + step));
      const id = ids[next];
      setFocusId(id);
      if (e.shiftKey) {
        if (anchorRef.current == null) anchorRef.current = focus ?? id;
        extendTo(id, false);
      }
      reveal(bodyRef?.current, id, next);
      return;
    }
    if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      const at = e.key === "Home" ? 0 : ids.length - 1;
      const id = ids[at];
      setFocusId(id);
      reveal(bodyRef?.current, id, at);
      return;
    }
    if (e.key === " " && focus != null) { e.preventDefault(); toggle(focus); return; }
    if (e.key === "Enter" && focus != null) { e.preventDefault(); onOpen?.(focus); return; }
    if (isMod(e) && (e.key === "a" || e.key === "A")) { e.preventDefault(); selectAll(); return; }
    if (e.key === "Escape" && (selectedRef.current.size || focus != null)) {
      // Only swallow Escape when there is something to clear, so it still
      // closes a modal or the omnibox when the table has no selection.
      e.preventDefault();
      clear();
      setFocusId(null);
    }
  }, [ids, bodyRef, toggle, extendTo, selectAll, clear, onOpen, setFocusId]);

  // The marquee effect below reads the live selection without re-subscribing on
  // every change — re-running that effect per keystroke would tear down and
  // rebuild the pointer listeners, potentially mid-drag.
  const selectedRef = useRef(selected);
  useEffect(() => { selectedRef.current = selected; }, [selected]);

  /* ── Marquee ──────────────────────────────────────────────────────────────
     A pointer-down anywhere in the table MAY become a rubber band; only crossing
     DRAG_THRESHOLD makes it one, and under the threshold it stays a click that
     opens the row. Window-level listeners keep a band tracking when the pointer
     leaves the table; pointer capture is taken only once the band commits, never
     on the press itself. The row rectangles are measured ONCE at drag start: re-reading
     them every pointermove would force a full layout on each frame over a few
     thousand rows, which is the difference between a smooth band and a stuttering one. */
  useEffect(() => {
    const body = bodyRef?.current;
    if (!body) return;

    // startX/startY are the drag origin on SCREEN, for drawing the band.
    // startTop is the same origin in the scrolled CONTENT's own coordinates,
    // which is what decides the rows: unlike a screen position it does not go
    // stale when the list auto-scrolls under the pointer, so nothing has to be
    // re-measured per move.
    let startX = 0, startY = 0, live = false, baseSel = null, pid = null;
    let startTop = 0, rowH = 0;

    const onMove = (e) => {
      if (!live) {
        if (Math.abs(e.clientX - startX) < DRAG_THRESHOLD
          && Math.abs(e.clientY - startY) < DRAG_THRESHOLD) return;
        live = true;
        // Capture HERE, not on pointerdown — this is the first moment we know the
        // gesture is a band and not a click. Capturing earlier broke opening a
        // person entirely: capture retargets pointerup/mouseup to the capturing
        // element, and `click` is dispatched on the nearest common ancestor of the
        // down and up targets, so a press that began on .nx-row ended up firing
        // click on .nx-body. The row's own onClick never ran and nothing opened.
        //
        // It also silently broke the "clear on background press" rule below, which
        // asks whether pointerup landed on a row — under capture the answer was
        // always "no", so a plain press on a row wiped the selection.
        //
        // Nothing is lost by waiting: the pointermove/pointerup/pointercancel
        // listeners are on `window`, which is what actually keeps a band tracking
        // outside the table. Capture is belt-and-braces for the drag.
        if (pid != null) {
          try { body.setPointerCapture(pid); } catch { /* window listeners suffice */ }
        }
        // Fix the origin and the row height now, once — neither can change
        // mid-drag. Also snapshot the selection so a modifier-drag adds to what
        // was already marked instead of replacing it.
        rowH = rowHeight(body);
        startTop = contentY(body, e.clientY);
        baseSel = (e.shiftKey || isMod(e)) ? new Set(baseSel) : new Set();
      }
      if (scrollBy) {
        // The drawn band's top edge scrolls with the content, or it would stay
        // pinned to the screen while the rows slid out from under it. Only the
        // drawing needs this — startTop is already in content coordinates.
        startY -= scrollBy;
      }
      autoScroll(e);
      const x = Math.min(startX, e.clientX), y = Math.min(startY, e.clientY);
      const w = Math.abs(e.clientX - startX), h = Math.abs(e.clientY - startY);
      setMarquee({ x, y, w, h });
      // Which rows the band covers. Rows are full-width, so vertical overlap
      // alone decides — testing the horizontal axis too would mean a band drawn
      // down the empty right-hand gutter selected nothing, which reads as broken.
      // floor() on each end gives exactly that overlap: a band lying entirely
      // inside one row still covers that row, and one straddling a divider
      // covers both.
      const hit = new Set(baseSel);
      if (rowH > 0) {
        const curTop = contentY(body, e.clientY);
        const first = Math.max(0, Math.floor(Math.min(startTop, curTop) / rowH));
        const last = Math.min(ids.length - 1, Math.floor(Math.max(startTop, curTop) / rowH));
        for (let i = first; i <= last; i++) hit.add(ids[i]);
      }
      setSelected(hit);
    };

    const end = (e) => {
      if (pid != null) { try { body.releasePointerCapture(pid); } catch { /* already gone */ } }
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      stopAutoScroll();
      if (live) {
        // The pointerup that ends a band is followed by a click on whatever the
        // press began on. Without this, banding from a row opens that person the
        // instant you release — the selection you just made replaced by their
        // page. Capture phase and once, so exactly one click is eaten.
        window.addEventListener("click", (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
        }, { capture: true, once: true });
      } else if (e.type === "pointerup" && !e.shiftKey && !isMod(e)
        && !e.target.closest("[data-row-id]")) {
        // A plain press on the EMPTY part of the list clears it, the way
        // clicking the background of a folder does. A press on a row is that
        // row's click and must not clear anything.
        clear();
      }
      live = false; pid = null; rowH = 0;
      setMarquee(null);
    };

    /* A band that stops at the edge of the viewport can only ever reach one
       screenful. Nudge the container while the pointer sits near an edge, at a
       speed that rises with how far past the edge it is — the same feel as
       dragging a selection past the bottom of a Finder window. */
    let scrollTimer = null, scrollBy = 0, lastMove = null;
    const stopAutoScroll = () => {
      if (scrollTimer) { clearInterval(scrollTimer); scrollTimer = null; }
      scrollBy = 0;
    };
    const autoScroll = (e) => {
      const r = body.getBoundingClientRect();
      const EDGE = 40;
      const over = e.clientY > r.bottom - EDGE ? e.clientY - (r.bottom - EDGE)
        : e.clientY < r.top + EDGE ? e.clientY - (r.top + EDGE) : 0;
      scrollBy = Math.max(-24, Math.min(24, over * 0.5));
      lastMove = e;
      if (scrollBy && !scrollTimer) {
        scrollTimer = setInterval(() => {
          body.scrollTop += scrollBy;
          // Rows moved under the pointer, so the hit test has to be redone
          // against their new positions.
          if (lastMove) onMove(lastMove);
        }, 16);
      } else if (!scrollBy) stopAutoScroll();
    };

    const onDown = (e) => {
      if (e.button !== 0) return;
      // A band may start ON a row, not only on empty space. Finder can demand
      // empty space because its icons leave plenty; a full-width table has
      // almost none — below the last row and nowhere else. Restricting the
      // gesture to that strip would make it undiscoverable, so a press anywhere
      // in the table may become a band, and only PASSING THE THRESHOLD turns it
      // into one. Under 10px it stays a click and the row opens as before.
      //
      // The mark button and any real control still own their own presses.
      if (e.target.closest(".nx-mark, a, input, textarea, select")) return;
      if (e.shiftKey || isMod(e)) return;   // those are the row's own gestures
      startX = e.clientX; startY = e.clientY; live = false;
      baseSel = new Set(selectedRef.current);
      pid = e.pointerId;
      // NO setPointerCapture here. See the note at `live = true` in onMove: taking
      // capture before the drag threshold is crossed retargets the click away from
      // the row and makes the whole list unopenable by mouse.
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", end);
      window.addEventListener("pointercancel", end);
    };

    body.addEventListener("pointerdown", onDown);
    return () => {
      body.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
    };
  }, [bodyRef, ids, clear]);

  return {
    selected, focusId, marquee,
    count: selected.size,
    isSelected: useCallback((id) => selected.has(id), [selected]),
    toggle, extendTo, clear, selectAll, setFocusId,
    onRowPointerDown, onKeyDown,
  };
}

/* A screen position turned into the scrolled content's own coordinates, where
 * row n occupies [n x rowH, (n+1) x rowH). One rectangle read per pointer move,
 * against one per row, which is what the old hit-test needed. */
function contentY(body, clientY) {
  return clientY - body.getBoundingClientRect().top + body.scrollTop;
}

/* Bring the roving cursor's row into view AND give it real DOM focus.
 *
 * Focus matters as much as the scroll: the rows are <button>s, so focusing the
 * one under the cursor is what makes :focus-visible follow the keyboard and what
 * makes a screen reader read the row you just moved to. The handlers above
 * preventDefault on Space and Enter, which is what stops the focused button
 * ALSO firing its own click and opening the person on a keypress meant to mark
 * them.
 *
 * Scroll by arithmetic BEFORE looking for the element. This used to ask the page
 * for the row and give up silently when it was not there — which, once the table
 * only drew the rows in view, meant a jump to the far end of the list froze the
 * view while the cursor carried on moving invisibly. NC-084.
 *
 * Only move if the row is off an edge, so a held arrow key walks the list one
 * row at a time instead of yanking each new row to the middle of the screen —
 * what scrollIntoView's "nearest" used to give us. */
function reveal(body, id, index) {
  if (body && index >= 0) {
    const rowH = rowHeight(body);
    const top = index * rowH;
    if (top < body.scrollTop) body.scrollTop = top;
    else if (top + rowH > body.scrollTop + body.clientHeight) {
      body.scrollTop = top + rowH - body.clientHeight;
    }
  }
  const el = document.querySelector(`[data-row-id="${CSS.escape(String(id))}"]`);
  if (el) { pendingFocus = null; focusIn(el); return; }
  // Not drawn yet — the row will take the focus when it mounts.
  pendingFocus = id;
}

/* The row the keyboard is trying to land on, when it was not in the page at the
 * moment we asked for it.
 *
 * A jump to the far end of a windowed list scrolls now and draws the rows for
 * that offset when preact next renders, so there is a gap in which the target row
 * does not exist. This is a standing INTENT rather than a retry loop, and that
 * choice is the point: the row claims the focus itself the instant it mounts (see
 * claimFocus, wired to the row's ref in people.js), so focus arrives exactly when
 * the row appears — no timer to be throttled, no animation frame to be starved,
 * and nothing to guess about how long a render takes. A retry-based version would
 * depend on timers and animation frames, and both stop in a hidden window. NC-084.
 *
 * Overwritten by the next reveal, and cleared as soon as it is claimed, so at most
 * one row is ever waiting. */
let pendingFocus = null;

/** Give a row's own control the focus. The row wrapper carries data-row-id; the
 *  thing that can actually hold focus is a button inside it. */
function focusIn(el) {
  if (el.matches("button, a, [tabindex]")) { el.focus(); return; }
  // `:not(.nx-mark)` is the whole fix for NC-086. The marking control is a
  // SIBLING of the row button and comes FIRST in document order (it has to be a
  // sibling: a button inside a button is invalid HTML and Safari makes the inner
  // one unclickable — the reason is written up on Row in people.js). So a plain
  // "first focusable thing in here" search landed on the checkbox every time,
  // with two consequences that both looked like something else:
  //   · `.nx-row:focus-visible` in view-network.css could never match, so the
  //     keyboard had no focus ring at all — only the .cursor styling, which is a
  //     different thing and hid the problem.
  //   · a screen reader read "Mark Ada Lovelace" instead of the row's own full
  //     sentence — name, role, place, last contact, closeness — which people.js
  //     builds precisely so the empty cells get spoken.
  // Note this is a selector LIST, not an order of preference: querySelector
  // returns the first match in document order whichever branch matches it, which
  // is exactly what we want once the checkbox is excluded.
  el.querySelector("button:not(.nx-mark), a, [tabindex]")?.focus();
}

/** Wired to each row's ref. Takes the focus if this is the row the keyboard was
 *  waiting for — which is the moment it becomes possible, and not before. */
export function claimFocus(el, id) {
  if (el && pendingFocus !== null && String(pendingFocus) === String(id)) {
    pendingFocus = null;
    focusIn(el);
  }
}

/** The rubber band itself. Fixed-position, so it is unaffected by the scroll
 *  offset of whatever it is drawn over. Pure geometry, no motion: this is
 *  direct manipulation, and a band that eased toward the pointer would lag the
 *  finger and feel broken rather than smooth. */
export function Marquee({ rect }) {
  if (!rect || (rect.w < 2 && rect.h < 2)) return null;
  return html`
    <div class="marquee" aria-hidden="true"
      style=${`left:${rect.x}px;top:${rect.y}px;width:${rect.w}px;height:${rect.h}px`}></div>`;
}
