/* ── Context menu ─────────────────────────────────────────────────────────────
 * Right-click a person, get who they are and what you can do about them.
 *
 * The menu opens AT THE POINTER and scales out of it. That is the origin-aware
 * rule doing real work: a panel that grows from the spot you clicked reads as a
 * consequence of the click, while the same panel scaling from its own centre
 * reads as arriving from nowhere. transform-origin is set per-open from the
 * corner the menu actually flipped to, so a menu that opens upward grows
 * upward.
 *
 * It is also a keyboard surface. Right-click is a mouse gesture, but the menu it
 * opens must not be — arrow keys move, Enter runs, Escape closes and returns
 * focus to where it came from. Shift+F10 and the Menu key open it, which is how
 * a keyboard user reaches a context menu on every other platform.
 */
import { html, useState, useRef, useEffect, useCallback } from "../vendor/preact-htm.module.js";
import { Icon } from "./icons.js";

const MENU_W = 232;
const EDGE = 8;      // never let the panel touch the window edge

/**
 * Controller hook. Owns "is a menu open and where", so a table can share one
 * menu across every row instead of mounting one per row — a few thousand hidden
 * menus would be a few thousand subtrees preact has to diff on every keystroke in
 * the search box.
 */
export function useContextMenu() {
  const [menu, setMenu] = useState(null);   // {x, y, payload}
  const restoreRef = useRef(null);

  const open = useCallback((e, payload) => {
    e.preventDefault();
    e.stopPropagation();
    restoreRef.current = document.activeElement;
    // A keyboard-opened menu (Shift+F10) has no pointer coordinates — anchor it
    // to the element instead, or it opens in the top-left corner of the screen.
    let x = e.clientX, y = e.clientY;
    if (!x && !y) {
      const r = e.currentTarget.getBoundingClientRect();
      x = r.left + 24; y = r.bottom - 8;
    }
    setMenu({ x, y, payload });
  }, []);

  const close = useCallback(() => {
    setMenu(null);
    // Focus has to come back, or closing the menu drops the user at the top of
    // the document and their next Tab starts over.
    restoreRef.current?.focus?.();
    restoreRef.current = null;
  }, []);

  return { menu, openMenu: open, closeMenu: close };
}

/**
 * @param items  [{key, label, icon, run, danger, disabled}] — or {sep:true}
 * @param head   optional node rendered above the items (the person peek)
 */
export function ContextMenu({ x, y, items, head, onClose }) {
  const ref = useRef(null);
  const [pos, setPos] = useState({ left: x, top: y, origin: "top left" });
  const [active, setActive] = useState(-1);
  const runnable = items.filter((i) => !i.sep && !i.disabled);

  // Flip against the window edges before the first paint, so the menu never
  // appears in the wrong place and then jumps. useLayoutEffect would be the
  // textbook call; measuring in a plain effect works here because the panel
  // starts at opacity 0 and the transition begins on the next frame anyway.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const h = el.offsetHeight;
    const flipX = x + MENU_W + EDGE > window.innerWidth;
    const flipY = y + h + EDGE > window.innerHeight;
    setPos({
      left: flipX ? Math.max(EDGE, x - MENU_W) : x,
      top: flipY ? Math.max(EDGE, y - h) : y,
      // Grow from the corner nearest the click — the corner that is pinned.
      origin: `${flipY ? "bottom" : "top"} ${flipX ? "right" : "left"}`,
    });
    el.focus();
  }, [x, y]);

  useEffect(() => {
    const onDown = (e) => { if (!ref.current?.contains(e.target)) onClose(); };
    // `true` = capture: close before the click lands on whatever is underneath,
    // so dismissing the menu never also triggers the thing behind it.
    document.addEventListener("pointerdown", onDown, true);
    // Any scroll detaches the menu from the row it belongs to, so it closes
    // rather than floating over an unrelated part of the table.
    window.addEventListener("scroll", onClose, true);
    window.addEventListener("resize", onClose);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("scroll", onClose, true);
      window.removeEventListener("resize", onClose);
    };
  }, [onClose]);

  const onKeyDown = (e) => {
    if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => {
        const n = runnable.length;
        return n ? (i + step + n) % n : -1;   // wraps, like a native menu
      });
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const it = runnable[active];
      if (it) { onClose(); it.run?.(); }
    }
  };

  let runIndex = -1;
  return html`
    <div class="ctx-menu" ref=${ref} role="menu" tabindex="-1" onKeyDown=${onKeyDown}
      style=${`left:${pos.left}px; top:${pos.top}px; width:${MENU_W}px; transform-origin:${pos.origin}`}>
      ${head && html`<div class="ctx-head">${head}</div>`}
      ${items.map((it, i) => {
        if (it.sep) return html`<div key=${"s" + i} class="ctx-sep" role="separator"></div>`;
        if (!it.disabled) runIndex += 1;
        const mine = runIndex;
        return html`
          <button key=${it.key} role="menuitem" disabled=${it.disabled}
            class=${"ctx-item" + (it.danger ? " danger" : "") + (!it.disabled && mine === active ? " on" : "")}
            onMouseEnter=${() => !it.disabled && setActive(mine)}
            onClick=${() => { onClose(); it.run?.(); }}>
            <${Icon} name=${it.icon} />
            <span class="grow">${it.label}</span>
            ${it.hint && html`<span class="ctx-hint">${it.hint}</span>`}
          </button>`;
      })}
    </div>`;
}
