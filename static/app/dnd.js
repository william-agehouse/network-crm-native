/* ── Dragging ─────────────────────────────────────────────────────────────────
 * A small pointer-events drag, used for "take this person and drop them on a
 * list". Deliberately NOT the HTML5 drag-and-drop API: that one cannot style its
 * own drag image without a canvas hack, fires no event while the pointer sits
 * still, and behaves differently in every browser. Pointer events give exact
 * control and work identically for mouse, trackpad and touch.
 *
 * The rules here come from Apple's *Designing Fluid Interfaces*, and each one
 * fixes a specific way a drag feels wrong:
 *
 *   Threshold before committing   A drag that starts on the first pixel makes
 *                                 every click feel unstable. 10px of hysteresis
 *                                 means a click stays a click.
 *   Respect the grab offset       The ghost keeps the point you actually grabbed
 *                                 under the cursor. Snapping its centre to the
 *                                 pointer is the single most common tell that a
 *                                 drag was implemented rather than designed.
 *   Pointer capture               Tracking continues when the pointer leaves the
 *                                 row, which is the entire point here: the drop
 *                                 target is in a different column.
 *   One finger only               Additional touches are ignored once a drag is
 *                                 live, or switching fingers teleports the item.
 *   Nothing moves the real row    The row stays put and a ghost follows the
 *                                 pointer. Moving the row itself would reflow
 *                                 the list under the cursor mid-drag.
 *
 * The ghost is positioned with a full `transform: translate3d(...)` string set
 * directly on the element — not through a CSS variable on a parent, which would
 * force a style recalc for every child on every frame.
 */
import { html, useState, useRef, useCallback, useEffect } from "../vendor/preact-htm.module.js";

const DRAG_THRESHOLD = 10;

/**
 * @param onDrop      (dropKey, payload) => void — dropKey comes from the
 *                    [data-drop] attribute of whatever is under the pointer
 * @param dropSelector  which elements count as drop targets
 */
export function useDrag({ onDrop, dropSelector = "[data-drop]" }) {
  // `drag` drives the ghost; `over` is the drop target currently under the
  // pointer, so a target can light up without every row re-rendering.
  const [drag, setDrag] = useState(null);   // {payload, label, count, x, y}
  const [over, setOver] = useState(null);
  const state = useRef({});

  const start = useCallback((e, payload, label, count = 1) => {
    if (e.button != null && e.button !== 0) return;   // left button / touch only
    if (state.current.live || state.current.pending) return;  // one finger only
    const el = e.currentTarget;
    const r = el.getBoundingClientRect();
    state.current = {
      pending: true, live: false,
      startX: e.clientX, startY: e.clientY,
      // Where inside the row the grab happened, so the ghost sits under the
      // same point rather than jumping its own centre to the cursor.
      grabX: e.clientX - r.left, grabY: e.clientY - r.top,
      width: r.width, payload, label, count, pid: e.pointerId, el,
    };

    const move = (ev) => {
      const s = state.current;
      if (!s.pending && !s.live) return;
      if (!s.live) {
        if (Math.abs(ev.clientX - s.startX) < DRAG_THRESHOLD
          && Math.abs(ev.clientY - s.startY) < DRAG_THRESHOLD) return;
        s.live = true;
        try { s.el.setPointerCapture(s.pid); } catch { /* window listeners cover us */ }
        // Kill the text selection the initial press may have begun, or the drag
        // paints a blue smear across the row it started on.
        window.getSelection?.()?.removeAllRanges();
        document.body.classList.add("is-dragging");
      }
      setDrag({
        payload: s.payload, label: s.label, count: s.count, width: s.width,
        x: ev.clientX - s.grabX, y: ev.clientY - s.grabY,
      });
      // elementFromPoint, not pointerover: the ghost sits under the pointer and
      // has pointer-events:none, so this reads straight through to the target.
      const el2 = document.elementFromPoint(ev.clientX, ev.clientY);
      const target = el2 && el2.closest(dropSelector);
      setOver(target ? target.getAttribute("data-drop") : null);
    };

    const end = (ev) => {
      const s = state.current;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", cancel);
      if (s.live) {
        try { s.el.releasePointerCapture(s.pid); } catch { /* already released */ }
        const el2 = document.elementFromPoint(ev.clientX, ev.clientY);
        const target = el2 && el2.closest(dropSelector);
        if (target) onDrop?.(target.getAttribute("data-drop"), s.payload);
      }
      document.body.classList.remove("is-dragging");
      state.current = {};
      setDrag(null);
      setOver(null);
    };

    // Escape, a lost pointer, or the window blurring all mean "forget it" — and
    // a drag with no way to bail is a trap.
    const cancel = () => {
      const s = state.current;
      if (s.live) { try { s.el.releasePointerCapture(s.pid); } catch { /* gone */ } }
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", cancel);
      document.body.classList.remove("is-dragging");
      state.current = {};
      setDrag(null);
      setOver(null);
    };

    state.current.cancel = cancel;
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", cancel);
  }, [onDrop, dropSelector]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") state.current.cancel?.(); };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      state.current.cancel?.();
    };
  }, []);

  return { drag, over, startDrag: start, dragging: !!drag };
}

/** The thing that follows the pointer. Fixed-position and inert, so it never
 *  intercepts the hit test that finds the drop target underneath it. */
export function DragGhost({ drag }) {
  if (!drag) return null;
  return html`
    <div class="drag-ghost" aria-hidden="true"
      style=${`width:${Math.min(drag.width || 240, 280)}px;`
        + `transform:translate3d(${drag.x}px, ${drag.y}px, 0)`}>
      <span class="drag-ghost-label">${drag.label}</span>
      ${drag.count > 1 && html`<span class="drag-ghost-count">${drag.count}</span>`}
    </div>`;
}

/** Announce a drag to screen readers, which see none of the above. Pointer
 *  dragging is mouse-shaped by nature, so every drop this enables must also be
 *  reachable another way — for adding to a list that is the "Add to list"
 *  button, which stays exactly where it was. */
export function DragLive({ drag, over, overLabel }) {
  const msg = !drag ? ""
    : over ? `${drag.label}, over ${overLabel || over}. Release to add.`
           : `${drag.label}, dragging.`;
  return html`<div class="sr-only" role="status" aria-live="polite">${msg}</div>`;
}
