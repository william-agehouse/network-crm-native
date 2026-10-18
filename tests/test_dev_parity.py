"""Guards on the gap between the dev environment and the product that ships.

The gap these exist for: a change verified end-to-end against a developer's own
virtual environment — one with anthropic, numpy and sentence-transformers
installed — can have every test green and every endpoint exercised and still not
work. The shipped product runs on a DIFFERENT interpreter
(~/Library/Application Support/NetworkCRM/venv, built from the bundle's own Python),
and if that one lacks anthropic, the Ask view silently falls back to its
rule-based engine and answers badly, while every dev check says healthy.

test_packaging.py already guards the STATIC contract (requirements.txt covers every
crm import). This file guards the LIVE contract: that the environment and code the
product actually runs are the ones dev verified. Concretely it fails when

  a) the shipped interpreter can't import something requirements.txt promises,
  b) the built bundle ships crm/*.py or package lists that differ from the repo, or
     has a different shape than the build promises (one program, its own Python, no
     web page folder),
  c) the built bundle carries stale compiled cruft (__pycache__/*.pyc),
  d) the interpreter running THIS suite is itself missing a production dependency —
     so a green run here would not describe the product.

NC-330 (2026-10-17) changed what the bundle holds: the window is a native program
(Contents/MacOS/NetworkCRM), the engine's code is crm/ alone (the old web page, static/,
is no longer shipped), and the bundled Python is always there. The checks below describe
that layout. Against a bundle built before that day, (b) fails until the app is rebuilt.

Anything that needs a packaged interpreter or a built bundle skips cleanly when it
is absent, so this passes on a fresh clone and in CI.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import config  # noqa: E402,F401 — data-dir bootstrap (see helpers)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")
REQUIREMENTS = os.path.join(REPO, "requirements.txt")

# The interpreter the app actually RUNS on. On the first start of a new version the app
# runs crm/python_env.py (before NC-330, the launcher script's ensure_embedded_venv), which
# builds this venv under ~/Library/Application Support from the bundled Python and
# pip-installs requirements-lock.txt into it; the engine (crm/app.py) then runs on it.
# It lives OUTSIDE the repo — that separateness is the whole point, since it
# can differ from a development venv.
#
# NOTE: the bundle's own Python, at Contents/Resources/python, is only the BASE
# interpreter the venv is built FROM — deliberately bare, NOT the runtime once the venv
# exists, and it must not be checked for app dependencies.
BUNDLE = os.path.join(REPO, "packaging", "dist", "Network CRM.app")
BUNDLE_APP = os.path.join(BUNDLE, "Contents", "Resources", "app")
BUNDLE_CRM = os.path.join(BUNDLE_APP, "crm")
# The package lists the first start installs from, and the optional meaning-based
# search installs from later (NC-316). appify.sh copies all three beside crm/.
PACKAGE_LISTS = ("requirements.txt", "requirements-lock.txt", "requirements-semantic.txt")
APP_SUPPORT = os.path.expanduser("~/Library/Application Support/NetworkCRM")
APP_VENV_PY = os.path.join(APP_SUPPORT, "venv", "bin", "python3")

# distribution (as written in requirements.txt, lower-cased) -> a module whose
# successful import proves that distribution is installed. A distribution not
# listed here falls back to its normalised name, which is correct for the common
# case (foo-bar -> foo_bar) and wrong only for renamers like
# google-api-python-client, which is exactly why the tricky ones are pinned
# explicitly.
DIST_IMPORT = {
    "anthropic": "anthropic",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "google-api-python-client": "googleapiclient",
    "requests": "requests",
    "openai": "openai",
    "selenium": "selenium",
    "sentence-transformers": "sentence_transformers",
    "numpy": "numpy",
}


def _requirements():
    """Distribution names requirements.txt actually installs (comments excluded),
    lower-cased — same parsing as test_packaging.py so the two files agree."""
    names = set()
    with open(REQUIREMENTS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in (">=", "==", "<=", "~=", ">", "<", "["):
                if sep in line:
                    line = line.split(sep, 1)[0]
            names.add(line.strip().lower())
    return names


def _import_name(dist):
    return DIST_IMPORT.get(dist, dist.replace("-", "_"))


def _shipped_interpreter():
    """(path, human-label) of the interpreter the SHIPPED app runs on — the venv
    crm/python_env.py builds under ~/Library/Application Support and installs
    the package list into — or (None, None) if it does not exist yet (a fresh
    clone, CI, or an app that has never been launched)."""
    if os.path.exists(APP_VENV_PY):
        return APP_VENV_PY, "app-support venv (the app's own Python environment)"
    return None, None


def _imports_missing_in(interpreter, dists):
    """Run real imports in `interpreter` (a different process/venv) and return the
    list of [dist, module, error] it could not import. A real import, not
    find_spec, because the shipped venv is where a broken/absent install actually
    bites."""
    pairs = [(d, _import_name(d)) for d in sorted(dists)]
    prog = (
        "import importlib, json\n"
        f"pairs = {pairs!r}\n"
        "bad = []\n"
        "for dist, mod in pairs:\n"
        "    try:\n"
        "        importlib.import_module(mod)\n"
        "    except Exception as e:\n"
        "        bad.append([dist, mod, type(e).__name__ + ': ' + str(e)])\n"
        "print(json.dumps(bad))\n"
    )
    proc = subprocess.run([interpreter, "-c", prog], capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"could not probe {interpreter!r}: rc={proc.returncode} "
            f"stderr={proc.stderr.strip()!r}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestShippedInterpreterImportsEveryRequirement(unittest.TestCase):
    """(a) The environment the product runs on must be able to import everything
    requirements.txt promises. This is the check that catches a missing
    anthropic directly: with it absent from the app venv, Ask degrades."""

    def test_shipped_interpreter_imports_all_requirements(self):
        interpreter, label = _shipped_interpreter()
        if interpreter is None:
            self.skipTest(
                "app-support venv not present (fresh clone / CI / app never "
                f"launched); looked for {APP_VENV_PY!r}"
            )
        missing = _imports_missing_in(interpreter, _requirements())
        detail = "\n".join(f"    {d} (import {m!r}) -> {err}" for d, m, err in missing)
        self.assertEqual(missing, [], "\n".join([
            "",
            "The SHIPPED interpreter cannot import packages requirements.txt promises.",
            "The product would silently degrade (e.g. Ask -> rule-based engine) while",
            "every dev check stays green. The app installs the bundle's package",
            "list (requirements-lock.txt) into this venv, so a gap here ships to every install.",
            f"  shipped interpreter ({label}):",
            f"    {interpreter}",
            f"  this test's interpreter (what dev verifies on):",
            f"    {sys.executable}",
            "  importable in dev but MISSING in the shipped interpreter:",
            detail,
        ]))


class TestBundleShipsCurrentSource(unittest.TestCase):
    """(b) A built bundle must contain the repo's code and dependency list, not an
    older snapshot. A stale bundle is a lie: you verify one thing and ship another."""

    def _skip_without_bundle(self):
        if not os.path.isdir(BUNDLE_APP):
            self.skipTest(f"no built bundle at {BUNDLE!r} — nothing shipped yet")

    def test_bundle_crm_is_byte_identical_to_repo(self):
        self._skip_without_bundle()
        repo_py = {f for f in os.listdir(CRM) if f.endswith(".py")}
        bundle_py = {f for f in os.listdir(BUNDLE_CRM) if f.endswith(".py")}
        problems = [f"missing from bundle: {f}" for f in sorted(repo_py - bundle_py)]
        problems += [f"in bundle, not in repo: {f}" for f in sorted(bundle_py - repo_py)]
        for f in sorted(repo_py & bundle_py):
            with open(os.path.join(CRM, f), "rb") as a, \
                 open(os.path.join(BUNDLE_CRM, f), "rb") as b:
                if a.read() != b.read():
                    problems.append(f"differs from repo: {f}")
        self.assertEqual(problems, [], "\n".join([
            "",
            "The bundle's crm/ is not byte-identical to the repo's — it ships older",
            "code than the code you tested. Rebuild it (packaging/appify.sh) before",
            "trusting any verification done against the repo.",
            f"  repo:   {CRM}",
            f"  bundle: {BUNDLE_CRM}",
            *(f"    {p}" for p in problems),
        ]))

    def test_bundle_ships_no_static_folder(self):
        """The old web page, static/, is not inside the app any more (NC-330,
        2026-10-17), and this used to check it was byte-identical to the repo's. The
        window is native and nothing in the app serves or opens that folder, so a copy
        would be tens of megabytes of dead code: and, worse, one more thing a reader
        could take for the app's own front end. Where the front end came from was the
        trouble this file's old check existed for (a fixed view sitting in the repo and
        shipping as the old one). With no copy there is nothing to go stale, and what
        has to hold is that none comes back."""
        self._skip_without_bundle()
        self.assertFalse(
            os.path.exists(os.path.join(BUNDLE_APP, "static")),
            "the bundle carries a static/ folder: appify.sh copies only crm/ now. "
            "Rebuild it (packaging/appify.sh --embed).")

    def test_the_bundle_has_the_one_program_and_its_own_python(self):
        """The shape the build promises: one native program (before NC-330 there was a
        launcher script, a window program and a second 'Quit' app), named NetworkCRM
        in the property list, and the bundled Python the app cannot start without."""
        self._skip_without_bundle()
        macos = os.path.join(BUNDLE, "Contents", "MacOS")
        self.assertEqual(sorted(os.listdir(macos)), ["NetworkCRM"],
                         "the bundle holds programs other than the app's one native "
                         "program; it was built before NC-330 or by something else")
        with open(os.path.join(BUNDLE, "Contents", "Info.plist"), encoding="utf-8") as fh:
            plist = fh.read()
        self.assertRegex(plist, r"<key>CFBundleExecutable</key>\s*<string>NetworkCRM</string>")
        self.assertNotIn("LSUIElement", plist,
                         "the app is hidden from the Dock and the menu bar (a launcher-era setting)")
        python = os.path.join(BUNDLE, "Contents", "Resources", "python", "bin", "python3")
        self.assertTrue(os.path.exists(python),
                        "an embedded bundle without its own Python cannot start its "
                        "engine (appify.sh --embed refuses to build one since NC-330)")

    def test_no_quit_app_is_left_beside_it(self):
        """There is no 'Quit Network CRM' app any more (NC-330): quitting the app stops
        its engine. appify.sh removes an old one from dist/ when it builds."""
        self._skip_without_bundle()
        self.assertFalse(
            os.path.exists(os.path.join(os.path.dirname(BUNDLE), "Quit Network CRM.app")),
            "an old 'Quit Network CRM.app' sits beside the app; rebuild with appify.sh")

    def test_bundle_requirements_match_repo(self):
        self._skip_without_bundle()
        bundle_req = os.path.join(BUNDLE_APP, "requirements.txt")
        if not os.path.exists(bundle_req):
            self.fail(f"bundle ships no requirements.txt at {bundle_req!r} — the "
                      "app's first start would have nothing to install")
        repo_set = _requirements()
        # parse the bundle copy with the same rules
        bundle_set = set()
        with open(bundle_req, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                for sep in (">=", "==", "<=", "~=", ">", "<", "["):
                    if sep in line:
                        line = line.split(sep, 1)[0]
                bundle_set.add(line.strip().lower())
        missing = sorted(repo_set - bundle_set)
        extra = sorted(bundle_set - repo_set)
        self.assertEqual((missing, extra), ([], []), "\n".join([
            "",
            "The bundle's requirements.txt differs from the repo's. The app's first",
            "start installs the BUNDLE copy, so a NEW install gets this",
            "dependency set, not the repo's — regardless of what dev has installed.",
            f"  repo:   {REQUIREMENTS}",
            f"  bundle: {bundle_req}",
            f"  in repo, MISSING from the bundle: {missing or 'none'}",
            f"  in bundle, not in repo:           {extra or 'none'}",
            "  (missing 'anthropic' here means a fresh install's Ask view falls back to",
            "   the rule-based engine even though the repo is fixed.)",
        ]))

    def test_bundle_carries_every_package_list_unchanged(self):
        """All three lists, byte for byte. requirements-lock.txt is what the first
        start installs (with a fingerprint for every file pip may download, NC-316),
        and requirements-semantic.txt is what the opt-in meaning-based search installs
        later; a bundle missing the second turns that switch into an error on every
        install, and one with an older lock installs versions nobody checked."""
        self._skip_without_bundle()
        problems = []
        for name in PACKAGE_LISTS:
            ours, theirs = os.path.join(REPO, name), os.path.join(BUNDLE_APP, name)
            if not os.path.exists(theirs):
                problems.append(f"missing from bundle: {name}")
                continue
            with open(ours, "rb") as a, open(theirs, "rb") as b:
                if a.read() != b.read():
                    problems.append(f"differs from repo: {name}")
        self.assertEqual(problems, [], "\n".join([
            "",
            "The bundle's package lists are not the repo's. Rebuild it (packaging/appify.sh).",
            *(f"    {p}" for p in problems),
        ]))


class TestBundleHasNoCompiledCruft(unittest.TestCase):
    """(c) The bundle's own APP CODE must not carry __pycache__/*.pyc. Stale
    bytecode is how a bundle serves code that no longer matches its own source.

    Scoped to Contents/Resources/app: the risk this guard targets is first-party
    source cache, i.e. crm/__pycache__ inside the app. (Since NC-310 appify.sh sweeps
    the WHOLE bundle, the bundled Python's own folders included, as its last step before
    signing; tests/test_packaging_fixes.py holds that sweep to its order.)"""

    def test_no_pycache_or_pyc_in_app_code(self):
        if not os.path.isdir(BUNDLE_APP):
            self.skipTest(f"no built bundle at {BUNDLE!r} — nothing shipped yet")
        offenders = []
        for root, dirs, files in os.walk(BUNDLE_APP):
            for d in dirs:
                if d == "__pycache__":
                    offenders.append(os.path.relpath(os.path.join(root, d), BUNDLE))
            for fn in files:
                if fn.endswith((".pyc", ".pyo")):
                    offenders.append(os.path.relpath(os.path.join(root, fn), BUNDLE))
        self.assertEqual(sorted(offenders), [], "\n".join([
            "",
            "The bundle's app code ships compiled Python cruft. A .pyc can execute",
            "even after its source changed, so the bundle can serve code that no",
            "longer matches what you see. Exclude __pycache__/*.pyc from the app tree",
            "when building (packaging/appify.sh).",
            f"  app tree: {BUNDLE_APP}",
            *(f"    {o}" for o in sorted(offenders)),
        ]))


class TestTestInterpreterMatchesProduction(unittest.TestCase):
    """(d) The interpreter running THIS suite must itself hold every production
    dependency. If it does not, a green run here is meaningless — it describes an
    environment the product never uses. The message names both interpreters so a
    failure says WHICH environment is wrong."""

    def test_this_interpreter_can_import_every_requirement(self):
        missing = [(d, _import_name(d)) for d in sorted(_requirements())
                   if importlib.util.find_spec(_import_name(d)) is None]
        shipped, label = _shipped_interpreter()
        shipped_line = (f"  shipped interpreter ({label}): {shipped}"
                        if shipped else "  shipped interpreter: not installed here")
        detail = "\n".join(f"    {d} (import {m!r})" for d, m in missing)
        self.assertEqual(missing, [], "\n".join([
            "",
            "This suite is running on an interpreter that is MISSING production",
            "dependencies, so passing tests here do not describe the shipped product.",
            f"  test interpreter: {sys.executable}",
            shipped_line,
            "  requirements this interpreter cannot import:",
            detail,
            "  Run the suite on the same environment the app ships with, or install",
            "  the missing packages before trusting a green run.",
        ]))


class TestTheEmptyStateTesterDescribesThisCode(unittest.TestCase):
    """(e) NC-072. An optional sibling clone with an empty database (when one
    is kept beside this repo, named after it plus "-tester") is the only place
    the onboarding flow can be checked honestly — `onboarding_done()`
    grandfathers any install that already has contacts, so a data directory with
    contacts can never test a first run.

    That makes it the same class of bug as the rest of this file: a verification
    environment that can quietly stop describing the thing being verified. A
    clone parked on an old branch lacks the newer modules that a page's module
    imports at the top, and that page renders as a blank white screen there.

    Branch equality rather than commit equality on purpose: the tester being a
    few commits behind is normal and harmless, and a gate that failed on it would
    be turned off within a week. Being on a DIFFERENT branch is the thing that
    silently stops it tracking, and it is a crisp condition with an obvious fix.
    """

    TESTER = os.path.join(os.path.dirname(REPO), os.path.basename(REPO) + "-tester")

    def _branch(self, repo):
        out = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    def test_the_tester_follows_the_same_branch_as_this_repo(self):
        if not os.path.isdir(os.path.join(self.TESTER, ".git")):
            self.skipTest(f"no tester clone at {self.TESTER!r}")
        mine = self._branch(REPO)
        theirs = self._branch(self.TESTER)
        if not mine or not theirs:
            self.skipTest("could not read a branch name from git")
        self.assertEqual(theirs, mine, "\n".join([
            "",
            "The empty-database tester is on a different branch from this repo, so",
            "any onboarding check run there describes other code.",
            f"  this repo:   {mine}",
            f"  the tester:  {theirs}",
            f"  fix: git -C '{self.TESTER}' checkout -B {mine} origin/{mine}",
            "  Its data/ is gitignored, so switching branches keeps the empty DB.",
        ]))

    def test_every_module_the_tester_imports_exists_there(self):
        """The symptom, guarded directly: a missing module is not an error page,
        it is a blank one. A page's module imports several siblings and any single
        404 takes the whole view down."""
        app_dir = os.path.join(self.TESTER, "static", "app")
        if not os.path.isdir(app_dir):
            self.skipTest(f"no tester clone at {self.TESTER!r}")
        missing = []
        for name in sorted(os.listdir(app_dir)):
            if not name.endswith(".js"):
                continue
            with open(os.path.join(app_dir, name), encoding="utf-8") as fh:
                body = fh.read()
            for target in re.findall(r'from\s+"\./([A-Za-z0-9_.-]+\.js)"', body):
                if not os.path.exists(os.path.join(app_dir, target)):
                    missing.append(f"{name} imports ./{target}, which is not there")
        self.assertEqual(missing, [], "\n".join([
            "", "The tester's app modules do not resolve, so its pages render blank:",
            *[f"  {m}" for m in missing],
            "  This is what a half-synced clone looks like — see NC-072.",
        ]))


if __name__ == "__main__":
    unittest.main()
