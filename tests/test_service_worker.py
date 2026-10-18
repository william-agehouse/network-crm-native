"""Guards on static/sw.js's SHELL_ASSETS list — NC-053.

SHELL_ASSETS is a hand-maintained array, and a hand-kept list of a directory's
contents drifts by construction: before NC-053 it was nine files behind
(app/menu.js and app/setup-progress.js among them). It stays an explicit array
rather than generated code (simplest, most robust for a no-build-step app, no
install-time fetch that could make `cache.addAll()` fragile), and a test fails
the moment the array disagrees with the real import graph, in either direction:

  - forward: every stylesheet static/index.html links, and every JS module
    reachable by import from its entry script, must be in SHELL_ASSETS —
    otherwise the offline/cold-start case silently serves nothing for it.
  - reverse: every /app/ or /css/ entry in SHELL_ASSETS must exist on disk —
    a stale or mistyped entry makes the *whole* `cache.addAll()` reject,
    which fails SW install entirely, so a typo is worse than an omission.

Deliberate exclusion, named rather than silently allowed: static/vendor/ and
static/fonts/ are never in SHELL_ASSETS, on purpose. sw.js's own `isShell`
check only matches "/", "/manifest.webmanifest", "/css/*" and "/app/*" — vendor
and font requests fall through to RUNTIME_CACHE (networkFirstWithFallback),
and get their own long-lived HTTP Cache-Control instead (crm/app.py's
_serve_static_asset). So
vendor/preact-htm.module.js is walked as part of the import graph (every app
module imports it) but is excluded from the required set below, with this
comment as the reason a reviewer should not "fix" that gap.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, js_code  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")
SW_JS = os.path.join(STATIC, "sw.js")
INDEX_HTML = os.path.join(STATIC, "index.html")

# The one intentional gap between "reachable from index.html" and "must be in
# SHELL_ASSETS" — see the module docstring. Stored as the exact module-graph
# key (a "/"-rooted static path) so the forward check can subtract it by
# identity, not by any string match on the entry.
ALLOWED_EXCEPTIONS = {"/vendor/preact-htm.module.js"}


def _parse_shell_assets():
    """Return SHELL_ASSETS from sw.js as an exact list of string literals."""
    with open(SW_JS, encoding="utf-8") as fh:
        src = js_code(fh.read())
    m = re.search(r"const\s+SHELL_ASSETS\s*=\s*\[(.*?)\]", src, re.DOTALL)
    if not m:
        raise AssertionError("sw.js no longer declares a SHELL_ASSETS array literal")
    body = m.group(1)
    return re.findall(r'"([^"]+)"', body)


def _parse_index_html():
    """Return (stylesheet_hrefs, entry_module_src) from static/index.html, each
    normalised to a leading-slash static-relative path."""
    with open(INDEX_HTML, encoding="utf-8") as fh:
        html = fh.read()
    stylesheets = [
        _normalize(href)
        for href in re.findall(
            r'<link\s+rel="stylesheet"\s+href="([^"]+)"', html
        )
    ]
    entries = re.findall(r'<script\s+type="module"\s+src="([^"]+)"', html)
    if len(entries) != 1:
        raise AssertionError(
            f"expected exactly one module entry script in index.html, found {len(entries)}"
        )
    return stylesheets, _normalize(entries[0])


def _normalize(path):
    """index.html writes hrefs without a leading slash ('css/tokens.css',
    'app/main.js'); sw.js writes them with one ('/css/tokens.css'). Compare in
    sw.js's own form throughout."""
    return path if path.startswith("/") else "/" + path


def _resolve_import(importer_static_path, spec):
    """Resolve an import specifier ('./x.js', '../vendor/y.js') relative to the
    importing module's own directory, back into a leading-slash static path."""
    importer_dir = os.path.dirname(importer_static_path)
    joined = os.path.normpath(os.path.join(importer_dir, spec))
    return "/" + joined.replace(os.sep, "/").lstrip("/")


IMPORT_RE = re.compile(r'\bfrom\s+["\']([^"\']+)["\']')


def _module_closure(entry_static_path):
    """Walk the `import ... from "..."` graph from the entry module, following
    every local (./ and ../) specifier transitively. Returns the set of every
    reachable module as a leading-slash static path, including the entry
    itself and vendor modules (callers decide what to exclude)."""
    seen = set()
    stack = [entry_static_path]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        fs_path = os.path.join(STATIC, current.lstrip("/"))
        if not os.path.isfile(fs_path):
            raise AssertionError(f"module closure hit a non-existent file: {current}")
        with open(fs_path, encoding="utf-8") as fh:
            src = js_code(fh.read())
        for spec in IMPORT_RE.findall(src):
            if not spec.startswith("."):
                continue  # not a relative local import (shouldn't occur here)
            stack.append(_resolve_import(current, spec))
    return seen


class TestShellAssetsCoverEveryStylesheet(unittest.TestCase):
    """Forward direction: nothing index.html links, and nothing the module
    graph imports, is missing from SHELL_ASSETS."""

    def setUp(self):
        self.shell_assets = set(_parse_shell_assets())
        self.stylesheets, self.entry = _parse_index_html()
        self.closure = _module_closure(self.entry)
        self.required_modules = self.closure - ALLOWED_EXCEPTIONS

    def test_every_stylesheet_link_is_precached(self):
        missing = [h for h in self.stylesheets if h not in self.shell_assets]
        self.assertEqual(
            missing, [], f"index.html links a stylesheet sw.js never precaches: {missing}"
        )

    def test_every_reachable_module_is_precached(self):
        missing = sorted(m for m in self.required_modules if m not in self.shell_assets)
        self.assertEqual(
            missing,
            [],
            f"reachable from {self.entry} via import but absent from SHELL_ASSETS: {missing}",
        )

    def test_the_previously_missing_files_are_now_present(self):
        # The files NC-053 added to the list; they must stay in it. app/menu.js
        # (and the since-removed app/setup-progress.js) were missing from it.
        previously_missing = {
            "/app/intake.js",
            "/app/icons.js",
            "/app/dnd.js",
            "/app/select.js",
            "/app/fda.js",
            "/app/menu.js",
            # "/app/setup-progress.js" was the ninth. It went with the old Data
            # page (NC-236), and the reverse check below makes sure the list
            # no longer names it.
            "/css/view-intake.css",
            "/css/view-setup.css",
        }
        still_missing = previously_missing - self.shell_assets
        self.assertEqual(still_missing, set())

    def test_the_vendor_exception_is_real_and_reachable(self):
        # Guards the exception list itself: if preact-htm.module.js stopped
        # being imported (or ALLOWED_EXCEPTIONS drifted from what sw.js's own
        # isShell regex actually excludes), this fails loudly instead of the
        # exception silently covering for something else.
        self.assertIn("/vendor/preact-htm.module.js", self.closure)
        for exc in ALLOWED_EXCEPTIONS:
            self.assertTrue(
                os.path.isfile(os.path.join(STATIC, exc.lstrip("/"))),
                f"allowed exception {exc} does not exist on disk",
            )
            self.assertFalse(
                exc.startswith("/app/") or exc.startswith("/css/"),
                f"allowed exception {exc} is under /app/ or /css/, which isShell() "
                "already routes to the precache — it should not need an exception",
            )


class TestShellAssetsPointAtRealFiles(unittest.TestCase):
    """Reverse direction: every entry sw.js will try to cache.addAll() must
    resolve to a real file, or the whole SW install rejects."""

    def setUp(self):
        self.shell_assets = _parse_shell_assets()

    def test_no_duplicate_entries(self):
        # A duplicate is harmless to caches.addAll() itself but is exactly the
        # kind of hand-edit slip this list is prone to — assert it stays clean.
        self.assertEqual(
            len(self.shell_assets),
            len(set(self.shell_assets)),
            "SHELL_ASSETS has a duplicate entry",
        )

    def test_every_file_entry_exists_on_disk(self):
        bad = []
        for entry in self.shell_assets:
            if entry in ("/", "/manifest.webmanifest"):
                continue
            fs_path = os.path.join(STATIC, entry.lstrip("/"))
            if not os.path.isfile(fs_path):
                bad.append(entry)
        self.assertEqual(bad, [], f"SHELL_ASSETS names files that do not exist: {bad}")

    def test_manifest_entry_resolves(self):
        self.assertTrue(os.path.isfile(os.path.join(STATIC, "manifest.webmanifest")))


if __name__ == "__main__":
    unittest.main()
