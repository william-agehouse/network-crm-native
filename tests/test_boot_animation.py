"""NC-049 / NC-062 — the boot animation.

The app shows an animation while it starts. NC-049 compared five variants
through a picker; the constellation was kept, the choice itself went and the
constellation was made smaller — so there is one animation, no picker, no
NC_BOOT_ANIMATION key and no `data-boot` attribute.

That changes what is worth testing. The variant-plumbing failures (a selector
that never binds, an unvalidated value reaching an attribute selector) cannot
happen any more because the mechanism is gone, and this file asserts that it is
really gone rather than half-removed. What remains are the two failures that
still bite: a splash that outlives the app behind it, and a splash that quietly
costs a fast launch more time than it admits to.

What it deliberately does NOT assert is whether the animation looks good. That
is a matter of taste.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, settings_mod  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "css", "boot.css")
INDEX = os.path.join(ROOT, "static", "index.html")
BOOTJS = os.path.join(ROOT, "static", "app", "boot.js")
MAINJS = os.path.join(ROOT, "static", "app", "main.js")
SETTINGSJS = os.path.join(ROOT, "static", "app", "settings.js")
APPPY = os.path.join(ROOT, "crm", "app.py")


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ── The trap these two exist for ─────────────────────────────────────────────
# A source-reading gate that matches the code's own PROSE rather than its code
# goes red against code that is right. Two ways it happens in this file:
#   · a handover gate that searches main.js for "DOMContentLoaded" finds it in
#     the comment explaining why the handover does NOT use DOMContentLoaded;
#   · a load-order gate that searches index.html for "app/main.js" finds it in
#     the comment saying boot.css must load ahead of app/main.js — three lines
#     ABOVE the boot.css link, so correct code fails.
# A gate that is red against right code teaches you to delete the gate. Strip
# the prose first, always.
def code_only_js(src):
    """JS with // line comments and /* */ blocks removed."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln.split("//")[0] if ln.lstrip().startswith("//") else ln
                      for ln in src.splitlines())


def code_only_py(src):
    """Python with # comments and docstrings removed. Same trap as the two
    above: crm/app.py now carries a comment explaining that data-boot is NOT
    stamped any more, and a plain search finds that sentence."""
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            node.value.value = ""
    return ast.unparse(tree)


def code_only_html(src):
    """HTML with <!-- --> comments removed."""
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


class TestTheChoiceIsGoneAndNotHalfGone(Base):
    """A setting removed in four places out of six is worse than one left
    alone: the UI stops offering it while the server still reads it, so the
    behaviour becomes unreachable rather than absent. Every limb of the old
    five-variant mechanism is checked here by name."""

    def test_the_setting_is_no_longer_offered_or_stored(self):
        self.assertNotIn(
            "NC_BOOT_ANIMATION", settings_mod.MANAGED_KEYS,
            "the boot animation key is still registered, so it is still "
            "writable to secrets/.env by anything that posts to /api/settings")
        for gone in ("BOOT_ANIMATIONS", "BOOT_ANIMATION_DEFAULT", "boot_animation"):
            with self.subTest(name=gone):
                self.assertFalse(
                    hasattr(settings_mod, gone),
                    f"settings.{gone} survived the removal")

    def test_no_variant_attribute_is_stamped_on_the_document(self):
        """The attribute was interpolated into the served HTML from a value in
        .env. With one animation there is nothing to stamp — and nothing to
        validate, which is the point: the injection surface went with it."""
        code = code_only_py(read(APPPY))
        self.assertNotIn("data-boot", code,
                         "app.py still stamps a data-boot attribute")
        self.assertNotIn("boot_animation()", code,
                         "app.py still calls the removed selector")

    def test_the_picker_and_its_preview_are_gone_from_the_view(self):
        code = code_only_js(read(SETTINGSJS))
        for gone in ("BOOT_OPTIONS", "BootPreview", "previewBoot",
                     "NC_BOOT_ANIMATION"):
            with self.subTest(name=gone):
                self.assertNotIn(gone, code,
                                 f"settings.js still references {gone}")

    def test_the_stylesheet_selects_on_nothing_that_no_longer_exists(self):
        code = code_only_js(read(CSS))   # /* */ blocks, same syntax as JS
        self.assertNotIn('[data-boot', code,
                         "boot.css still has attribute selectors for a "
                         "mechanism that no longer sets the attribute, so "
                         "those rules can never bind")


class TestTheSplashCannotOutliveTheApp(Base):
    """The failure that would be worst and least obvious: #boot sits at z-index
    100 across the whole window. If it is ever left behind, every control under
    it is dead and the app looks frozen rather than broken."""

    def test_main_dismisses_it_immediately_after_render_not_on_a_dom_event(self):
        """DOMContentLoaded is the trap. main.js is a module, modules defer, so
        that event fires AFTER the app has mounted — waiting for it would hold
        the splash open on every single launch for no reason."""
        js = code_only_js(read(MAINJS))
        self.assertIn("dismissBoot()", js, "main.js never dismisses the splash")
        call = js.index("dismissBoot()")
        render = js.index("render(html`<${App} />`")
        self.assertGreater(call, render, "dismissBoot runs before the app mounts")
        tail = js[render:]
        self.assertNotIn("DOMContentLoaded", tail,
                         "the handover waits on DOMContentLoaded, which a "
                         "deferred module has already missed")

    def test_it_is_removed_from_the_dom_and_not_merely_faded(self):
        js = read(BOOTJS)
        self.assertIn(".remove()", js,
                      "the splash is only faded — a transparent sheet at "
                      "z-index 100 still swallows every click beneath it")

    def test_the_stylesheet_removes_it_even_if_the_module_never_runs(self):
        """A syntax error in any module, or an offline first load against a cold
        service worker, means dismissBoot() is never called. Without this the
        app is a permanently blank sheet — the exact failure the splash was
        added to prevent."""
        css = read(CSS)
        self.assertIn("boot-failsafe", css, "no CSS failsafe")
        self.assertIn("visibility: hidden", css,
                      "the failsafe fades but leaves the layer hit-testable")


class TestTheSplashCostsWhatItSaysAndNoMore(Base):
    """The measurement the whole feature rests on. The app mounts almost at once
    off a local http.server, so every millisecond of --boot-min is time a fast
    app spends looking slow — paid on every launch, by someone who did not ask
    to watch anything.

    The hold is short on purpose, and the ceiling below is what stops it
    drifting back to a long splash: not a pinned number, which any edit can
    update without thought, but a bound with a reason attached.
    """

    CEILING_MS = 500

    def _boot_min(self, css):
        m = re.search(r"#boot\s*\{[^}]*?--boot-min:\s*(\d+)", css, re.S)
        self.assertIsNotNone(m, "#boot declares no --boot-min at all")
        return int(m.group(1))

    def test_the_hold_stays_under_half_a_second(self):
        held = self._boot_min(read(CSS))
        self.assertLessEqual(
            held, self.CEILING_MS,
            f"the splash holds the window back {held} ms on every launch. It "
            f"was cut from 920 ms to well under {self.CEILING_MS} ms on "
            "purpose; raising it again needs a better reason than the "
            "animation feeling unfinished, because the app is already up.")

    def test_nothing_animates_for_longer_than_the_hold(self):
        """A keyframe still running when the splash is dismissed is cut off
        mid-way, which reads as a glitch rather than as a fast launch.

        A shell fade-in declared at 280+240 against a 420 hold, for example,
        would have the chrome chopped on every launch.

        Both halves are needed. The `animation:` shorthand carries the inline
        delays, but the two longest chains in the layer are staggered through
        `animation-delay: calc(... var(--i) * Nms)` — the nodes and the edges —
        and a shorthand-only check cannot see those at all."""
        css = read(CSS)
        held = self._boot_min(css)
        overruns = []

        # 1. inline `animation: <name> <dur>ms <easing…> <delay>ms both`
        for decl in re.findall(r"animation:\s*([^;]+);", css):
            if "boot-failsafe" in decl:
                continue                      # the 4s belt-and-braces, by design
            times = [int(x) for x in re.findall(r"(\d+)ms", decl)]
            if not times:
                continue
            total = sum(times[:2]) if len(times) > 1 else times[0]
            if total > held:
                overruns.append((decl.strip(), total))

        # 2. the staggered ones. Worst case is the highest --i in the markup,
        #    read from index.html rather than assumed, so adding a node to the
        #    constellation re-checks the budget automatically.
        html = read(INDEX)
        svg = html[html.index('<svg class="boot-net"'):html.index("</svg>")]
        max_i = max(int(m) for m in re.findall(r"--i:\s*(\d+)", svg))
        # NB: capture to the SEMICOLON, not to the first ")". `calc(60ms +
        # var(--i) * 5ms)` contains a nested paren, so a `[^)]+` capture stops
        # inside `var(--i` — the step never matches, every stagger computes as
        # zero, and the check silently passes whatever the values are — a
        # stagger widened to 40ms (960ms of chain against a 420ms hold) would go
        # uncaught.
        staggered = re.findall(
            r"animation:\s*boot-(\w+)\s+(\d+)ms[^;]*;\s*"
            r"animation-delay:\s*([^;]+);", css)
        self.assertTrue(staggered,
                        "no staggered animations found — the parser below has "
                        "stopped matching the stylesheet it is meant to check")
        for name, dur, expr in staggered:
            # e.g. "60ms + var(--i) * 5ms"  ->  base 60, step 5
            self.assertIn("var(--i)", expr,
                          f"boot-{name} has a plain animation-delay; this "
                          "parser assumes the staggered form")
            step_m = re.search(r"var\(--i\)\s*\*\s*(\d+)ms", expr)
            step = int(step_m.group(1)) if step_m else 0
            without_step = expr.replace(step_m.group(0), "") if step_m else expr
            base = sum(int(x) for x in re.findall(r"(\d+)ms", without_step))
            total = base + max_i * step + int(dur)
            if total > held:
                overruns.append((f"boot-{name} staggered to --i:{max_i}", total))

        self.assertEqual(
            overruns, [],
            f"these finish after the {held} ms hold, so they are cut off "
            f"mid-animation on a fast launch: {overruns}")

    def test_reduced_motion_stops_the_hold_entirely(self):
        """Resolved, not merely suppressed. Someone who asked for less motion
        should not still be waiting to be shown less motion."""
        css = read(CSS)
        block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertIn("--boot-min: 0", block,
                      "reduced motion still pays the full minimum")

    def test_every_edge_can_actually_draw_itself(self):
        """The edges animate with stroke-dashoffset, which does nothing at all
        unless the line declares pathLength — the dasharray would otherwise be
        one unit of a line hundreds of units long. It fails silently: the lines
        simply appear instead of drawing, and no test of the CSS alone notices."""
        html = read(INDEX)
        i = html.index('<svg class="boot-net"')
        svg = html[i:html.index("</svg>", i)]
        lines = re.findall(r"<line\b[^>]*>", svg)
        self.assertTrue(lines, "the constellation has no edges")
        missing = [l for l in lines if 'pathLength="1"' not in l]
        self.assertEqual(
            len(missing), 0,
            f"{len(missing)} of {len(lines)} edges have no pathLength, so "
            "their draw animation silently does nothing")
        self.assertIn("stroke-dashoffset", read(CSS),
                      "the edges no longer draw at all")


class TestTheShellStillPaintsBeforeTheModuleLoads(Base):
    def test_the_stylesheet_is_linked_ahead_of_the_module_script(self):
        html = code_only_html(read(INDEX))
        self.assertLess(html.index("css/boot.css"), html.index("app/main.js"),
                        "boot.css loads after main.js, so the splash paints "
                        "unstyled on a cold cache")

    def test_the_boot_layer_precedes_the_app_root(self):
        html = code_only_html(read(INDEX))
        self.assertLess(html.index('id="boot"'), html.index('id="root"'),
                        "the splash is after #root, so it paints late")

    def test_the_first_paint_clock_is_inline_and_not_deferred(self):
        """dismissBoot()'s "how much of the minimum is still owed" is measured
        against this. Recorded from a deferred module it would read ~0 elapsed
        and every paid variant would charge its full price on top of the mount
        rather than overlapping it."""
        html = code_only_html(read(INDEX))
        self.assertIn("dataset.bootAt", html)
        stamp = html.index("dataset.bootAt")
        tag = html.rindex("<script", 0, stamp)
        opening = html[tag:html.index(">", tag)]
        self.assertNotIn("type=\"module\"", opening,
                         "the first-paint clock is inside a module, so it is "
                         "deferred and records the wrong zero")
        self.assertNotIn("defer", opening)

    def test_boot_assets_are_cached_by_the_service_worker(self):
        """NC-053's rule, applied in the change that adds the files rather than
        afterwards: an asset missing from SHELL_ASSETS is an offline launch with
        no splash and, for boot.js, no dismissal either."""
        sw = read(os.path.join(ROOT, "static", "sw.js"))
        for asset in ("/css/boot.css", "/app/boot.js"):
            self.assertIn(f'"{asset}"', sw, f"{asset} is not cached offline")
