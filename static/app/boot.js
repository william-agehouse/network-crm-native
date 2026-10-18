// NC-049 — the boot layer's lifecycle. Its own module rather than a block in
// main.js because the lifecycle is worth reading on its own: a splash that can
// outlive the app is the failure that matters here, and the failsafe guarding
// against it lives next to the code it guards.
//
// NC-062 removed `previewBoot` from this module along with the Settings picker
// it existed for. With one animation there is nothing to compare, so there is
// nothing to preview.

/** How long this variant insists on being visible, in ms.
 *
 *  Read from `--boot-min` in boot.css rather than duplicated here, so the price
 *  of a variant lives next to the animation that charges it. A variant that
 *  sets none parses to NaN and falls to 0 — the correct default: get off the
 *  screen the moment the app is ready. `prefers-reduced-motion` zeroes it in
 *  the stylesheet, so nothing is held back for someone who asked for less. */
function bootMin(root = document.documentElement) {
  return parseInt(getComputedStyle(root).getPropertyValue("--boot-min"), 10) || 0;
}

/** Hand the screen over from the boot layer to the mounted app.
 *
 *  Called straight after render(), NOT on DOMContentLoaded: main.js is a module
 *  and modules defer, so DOMContentLoaded fires after the app has already
 *  mounted. Waiting for it would hold the splash open on every launch for no
 *  reason at all. */
export function dismissBoot(el = document.getElementById("boot")) {
  if (!el) return;   // nothing to dismiss (the failsafe already fired)
  // Elapsed time counts AGAINST the minimum rather than adding to it, so a slow
  // first paint spends the budget instead of extending the wait.
  const started = Number(document.documentElement.dataset.bootAt || 0);
  const owed = Math.max(0, bootMin() - (performance.now() - started));
  setTimeout(() => finish(el), owed);
}

function finish(el) {
  el.classList.add("boot-done");
  // Removed, not merely faded. It sits at z-index 100 across the whole app, and
  // a transparent sheet that still exists swallows every click underneath it.
  el.addEventListener("transitionend", () => el.remove(), { once: true });
  setTimeout(() => el.remove(), 400);   // if the transition never fires
}

