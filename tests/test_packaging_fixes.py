"""The copy of the app that gets handed out: what it needs, how it starts, what
it carries, and how it learns of a new version.

Each class names the issue number it pins. Where the property is behaviour (a file's
permissions, what a second build writes) it runs the real thing: shell functions
cut out of packaging/appify.sh and Python from crm/, against stand-in programs in
a temporary folder. Where the property lives in the app's own program, which is
Swift and is not compiled or started by this suite (native/Sources/Engine.swift,
App.swift, RootView.swift), it reads that source for the one line or the one
ordering that carries it. Nothing here needs the network, a built bundle, or a window.

NC-330 (2026-10-17): until that day the app was a bash launcher script
(packaging/src/launcher.sh.tmpl) and a second, web-page window program, and many
classes here cut the launcher's functions out and ran them. The launcher and that
window program are gone; the native app starts its own engine. What survived moved:
the places are named in each class, and the tests that checked launcher-only behaviour
(finding a Python on the Mac, a setup marker file, a launcher restart) were deleted
with it.
"""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, up_to, updates_mod,
)

import python_env  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPIFY = os.path.join(REPO, "packaging", "appify.sh")
DMG = os.path.join(REPO, "packaging", "make_dmg.sh")
NATIVE = os.path.join(REPO, "native")
COMPILE = os.path.join(NATIVE, "compile.sh")
BUILD = os.path.join(NATIVE, "build.sh")
ENGINE_SWIFT = os.path.join(NATIVE, "Sources", "Engine.swift")
ROOT_VIEW_SWIFT = os.path.join(NATIVE, "Sources", "RootView.swift")


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _function(src, name):
    """A shell function exactly as the script has it: from `name() {` to the
    first line that is a lone closing brace."""
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


def _swift_function(src, signature):
    """One Swift function or property, from its signature to the closing brace that
    sits at the same indent as the signature."""
    start = src.index(signature)
    indent = src[src.rfind("\n", 0, start) + 1:start]
    return src[start:src.index("\n" + indent + "}\n", start)]


def _bash(script, env=None, cwd=None, timeout=60):
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=timeout)


def _stub(folder, name, body):
    """A stand-in program on a PATH of its own."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def _readme_block():
    """The Read Me the disk image carries, as written by make_dmg.sh."""
    return up_to(_read(DMG), "<<'READMEEOF'", "\nREADMEEOF")


class _TempDir(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)


# ── NC-310 ────────────────────────────────────────────────────────────────────

class TestNoCompiledCacheReachesTheBundle(_TempDir):
    """A compiled cache records the absolute folder it was made in. Caches were
    being written into the built app (the build's own checks, and a first launch
    before the launcher said not to), so a handed-out app named the folders of
    whoever built and opened it."""

    def test_the_app_tells_every_python_it_starts_to_write_no_caches(self):
        """Moved 2026-10-17 (NC-330) from the launcher's `export
        PYTHONDONTWRITEBYTECODE=1` at the top of the script. The app's program starts
        Python in two places, the engine and the setup program (crm/python_env.py),
        and each is told, after any PYTHON* variable the app itself inherited is
        dropped, so nothing a shell left behind can undo it."""
        swift = _read(ENGINE_SWIFT)
        env = _swift_function(swift, "func environment(code: URL)")
        self.assertIn('key.hasPrefix("PYTHON")', env)
        self.assertRegex(env, r'env\["PYTHONDONTWRITEBYTECODE"\]\s*=\s*"1"')
        self.assertLess(env.index('key.hasPrefix("PYTHON")'), env.index('env["PYTHONDONTWRITEBYTECODE"]'))
        run = _swift_function(swift, "nonisolated static func run(")
        self.assertIn('!$0.key.hasPrefix("PYTHON")', run)
        self.assertRegex(run, r'env\["PYTHONDONTWRITEBYTECODE"\]\s*=\s*"1"')

    def test_the_build_says_no_caches_too(self):
        src = _read(APPIFY)
        m = re.search(r"^export PYTHONDONTWRITEBYTECODE=1$", src, re.M)
        self.assertIsNotNone(m)
        self.assertLess(m.start(), src.index("prepare_python() {"))

    def test_the_build_sweeps_the_whole_bundle_as_its_last_step(self):
        src = _read(APPIFY)
        app = os.path.join(self.d, "Network CRM.app")
        caches = ("Contents/Resources/python/lib/python3.13/__pycache__/os.cpython-313.pyc",
                  "Contents/Resources/app/crm/__pycache__/db.cpython-313.pyc",
                  "Contents/Resources/python/lib/stray.pyc",
                  "Contents/Resources/.DS_Store")
        kept = ("Contents/Resources/python/lib/python3.13/os.py", "Contents/MacOS/NetworkCRM")
        for rel in caches + kept:
            os.makedirs(os.path.dirname(os.path.join(app, rel)), exist_ok=True)
            open(os.path.join(app, rel), "w").close()
        out = _bash(_function(src, "strip_caches") + f'\nstrip_caches "{app}" "{self.d}/missing"')
        self.assertEqual(out.returncode, 0, out.stderr)
        for rel in caches:
            self.assertFalse(os.path.exists(os.path.join(app, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(
            app, "Contents/Resources/python/lib/python3.13/__pycache__")))
        for rel in kept:
            self.assertTrue(os.path.exists(os.path.join(app, rel)), rel)
        # Last, before the seal is made: the sweep follows everything that could write a
        # cache, and the signature (and the smoke test) follow the sweep. 2026-10-17: the
        # window is compiled by native/compile.sh and there is no second bundle to sweep.
        call = src.index('strip_caches "$APP_DIR"\n')
        for earlier in ('"$REPO_ROOT/native/compile.sh"',
                        'embed_python "$APP_DIR/Contents/Resources/python"',
                        'compile_imessage_helper "$APP_DIR',
                        'cp -R "$REPO_ROOT/crm" "$APP_CODE_DIR/crm"',
                        '> "$APP_DIR/Contents/Resources/build_id.txt"'):
            self.assertLess(src.index(earlier), call, f"{earlier} runs after the sweep")
        self.assertLess(call, src.index('codesign --force --sign - --identifier "$BUNDLE_ID" "$APP_DIR"'))
        self.assertLess(call, src.index('"$APP_DIR/Contents/MacOS/$EXE_NAME" --engine-check'))
        self.assertLess(call, src.index('echo "appify.sh: done."'))


# ── NC-321 ────────────────────────────────────────────────────────────────────

class TestTheAppsFilesAreForThisAccountOnly(_TempDir):
    """A database created by a server the launcher starts used to be readable by
    every account on the Mac; on a source checkout in a home folder others can
    open, that is the whole CRM.

    NC-330 (2026-10-17): the launcher script and its `umask 077` are gone. The mask is
    now set by the engine itself (crm/app.py main, below) and by the setup program
    (crm/python_env.py main), and the folders the app's own program makes are made
    0700 on purpose rather than through a mask."""

    def _a_marker_the_setup_program_writes(self, args_before):
        """Run python_env.main the way the app does, with the Python that would make the
        environment replaced by one that fails, so the setup program writes the note it
        leaves in the data folder; returns that note's and its folder's file modes."""
        previous = os.umask(0o022)       # the usual mask: what a Mac user account has
        self.addCleanup(os.umask, previous)
        venv, home, data = (os.path.join(self.d, n) for n in ("venv", "app", "data"))
        os.makedirs(home)
        for name in ("requirements.txt", "requirements-lock.txt"):
            with open(os.path.join(home, name), "w") as fh:
                fh.write("alpha==1.0\n")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch.object(python_env.subprocess, "run", return_value=failed), \
                contextlib.redirect_stdout(io.StringIO()):     # it prints the Python to use
            python_env.main(args_before + ["--venv", venv, "--app-home", home, "--data", data])
        note = os.path.join(data, python_env.MARKER_NAME)
        return (oct(os.stat(note).st_mode & 0o777), oct(os.stat(data).st_mode & 0o777))

    def test_what_the_setup_program_writes_is_private(self):
        note, folder = self._a_marker_the_setup_program_writes(["prepare"])
        self.assertEqual((note, folder), ("0o600", "0o700"),
                         "the setup note or its folder is readable by other accounts")

    def test_the_folders_the_app_makes_are_made_private(self):
        """Moved from `umask 077` before `mkdir -p "$SUPPORT_DIR" "$LOG_DIR"`. The app's
        program names the permission on each folder it makes, and on the log it opens."""
        swift = _read(ENGINE_SWIFT)
        make = _swift_function(swift, "func makeFolders() throws")
        self.assertIn("for dir in [support, dataFolder, secretsFolder, logs]", make)
        self.assertIn("attributes: [.posixPermissions: 0o700]", make)
        self.assertIn("setAttributes([.posixPermissions: 0o700], ofItemAtPath: secretsFolder.path)", make)
        log = _swift_function(swift, "func openLog(note: String)")
        self.assertIn("attributes: [.posixPermissions: 0o700]", log)
        self.assertIn("attributes: [.posixPermissions: 0o600]", log)

    def test_the_engine_sets_it_too_before_it_touches_the_database(self):
        """start.command and `python3 crm/app.py` skip the app's own program (the
        launcher script, before NC-330); the source checkout, where data/ sits beside
        the code, is exactly where it matters."""
        src = _read(os.path.join(REPO, "crm", "app.py"))
        main = src[src.index("def main() -> None:"):]
        self.assertLess(main.index("os.umask(0o077)"), main.index("_init_db_or_recover()"))
        self.assertLess(main.index("os.umask(0o077)"), main.index("_open_lan_refusal("))
        start = _read(os.path.join(REPO, "start.command"))
        self.assertLess(re.search(r"^umask 077$", start, re.M).start(), start.index('"$PY" crm/app.py'))

    def test_databases_made_before_are_tightened_at_start(self):
        import config
        for name, mode in (("crm.db", 0o644), ("crm.db-wal", 0o644), ("crm.db-shm", 0o664),
                           ("archive.db", 0o640), ("notes.txt", 0o644), ("private.db", 0o600)):
            path = os.path.join(self.d, name)
            open(path, "w").close()
            os.chmod(path, mode)
        with mock.patch.object(config, "DATA_DIR", self.d):
            self.assertEqual(config.harden_databases(), 4)
            self.assertEqual(config.harden_databases(), 0, "a second start changes nothing")
        modes = {n: oct(os.stat(os.path.join(self.d, n)).st_mode & 0o777)
                 for n in os.listdir(self.d)}
        self.assertEqual(modes["crm.db"], "0o600")
        self.assertEqual(modes["crm.db-shm"], "0o600")
        self.assertEqual(modes["archive.db"], "0o600")
        self.assertEqual(modes["notes.txt"], "0o644", "only the databases are touched")
        self.assertIn("config.harden_databases()", _read(os.path.join(REPO, "crm", "app.py")))


# ── NC-312 ────────────────────────────────────────────────────────────────────

class TestItSaysWhichMacItNeeds(_TempDir):
    """Decided: a Mac with an Apple chip and macOS 26 or later. Info.plist said
    10.15 while the window needed whatever macOS built it, and on an Intel Mac
    nothing appeared at all."""

    @staticmethod
    def _version(text):
        """"26.0" and "26" are the same macOS: compared as numbers, trailing zeros off."""
        parts = [int(p) for p in text.split(".")]
        while parts and parts[-1] == 0:
            parts.pop()
        return tuple(parts)

    def test_the_plists_and_the_compilers_name_the_same_macos(self):
        """Four places state the minimum, and macOS itself ("this Mac is too old")
        only works while they agree: the property list in appify.sh (the app) and in
        native/build.sh (the developer copy), the compiler's target in appify.sh (kept
        for the copying helper's handout build) and in native/compile.sh (the window).

        Moved 2026-10-17 (NC-330): appify.sh used to compile the window itself, in a
        function (compile_native_window) with its own target; the one place that
        compiles it is native/compile.sh now, shared by both builds."""
        appify, build, compile_sh = _read(APPIFY), _read(BUILD), _read(COMPILE)
        plist = r"<key>LSMinimumSystemVersion</key>\s*<string>([\d.]+)</string>"
        in_appify = re.search(plist, appify).group(1)
        in_build = re.search(plist, build).group(1)
        swift_target = re.search(r'^SWIFT_TARGET="arm64-apple-macos([\d.]+)"$', appify, re.M).group(1)
        window_target = re.search(r"-target arm64-apple-macos([\d.]+)", compile_sh).group(1)
        self.assertEqual(in_appify, "26.0")
        self.assertEqual({self._version(v) for v in (in_appify, in_build, swift_target, window_target)},
                         {(26,)},
                         f"the minimum macOS disagrees: app {in_appify}, developer copy {in_build}, "
                         f"helper target {swift_target}, window target {window_target}")
        # The window is built by that one script from both builds, never by a second recipe.
        self.assertIn('"$REPO_ROOT/native/compile.sh" "$APP_DIR/Contents/MacOS/$EXE_NAME"', appify)
        self.assertIn('"$HERE/compile.sh" "$APP/Contents/MacOS/$EXE_NAME"', build)
        self.assertNotIn("swiftc", re.sub(r"(?m)^\s*#.*$", "", build),
                         "native/build.sh compiles on its own instead of through compile.sh")

    def test_the_app_is_built_for_the_apple_chip_only(self):
        """An Intel Mac is turned away by macOS, in macOS's own words, because the
        program has no Intel code and the property list says arm64 first. Moved
        2026-10-17 (NC-330) from the launcher's own dialog (runs_on_this_mac,
        APPLE_CHIP_MSG), which is gone with the launcher: a program built for the Apple
        chip only (arm64) needs no dialog of ours to say it cannot run on an Intel Mac."""
        for name, src in (("appify.sh", _read(APPIFY)), ("native/build.sh", _read(BUILD))):
            with self.subTest(file=name):
                self.assertRegex(src, r"<key>LSArchitecturePriority</key>\s*<array><string>arm64</string></array>")
        compile_sh = re.sub(r"(?m)^\s*#.*$", "", _read(COMPILE))
        self.assertNotIn("x86_64", compile_sh)
        self.assertNotIn("universal", compile_sh)

    def test_the_helper_is_compiled_for_macos_26_only_in_a_handout(self):
        # Its bytes decide whether an installed helper keeps its Full Disk Access
        # grant, so every other build compiles it exactly as before.
        helper = _function(_read(APPIFY), "compile_imessage_helper")
        self.assertRegex(helper, r'if \[ "\$HANDOUT" = 1 \]; then\s*\n\s*target_args=\(-target "\$SWIFT_TARGET"\)')
        self.assertIn('xcrun swiftc -O ${target_args[@]+"${target_args[@]}"} -o "$bin"', helper)

    def test_the_readme_and_the_read_me_name_the_requirement(self):
        readme = _read(os.path.join(REPO, "README.md"))
        flat = re.sub(r"\s+", " ", readme[:readme.index("## Quick start")])
        self.assertIn("Apple chip (M1 or later) and macOS 26 or later", flat)
        block = re.sub(r"\s+", " ", _readme_block())
        self.assertIn("Apple chip (M1 or later) running macOS 26", block)
        self.assertIn("does not run on a Mac with an Intel processor", block)
        self.assertNotIn("falls back to needing Python", block,
                         "the Read Me still says an Intel Mac works with its own Python")


# ── NC-313 ────────────────────────────────────────────────────────────────────

class TestTheFirstLaunchBlockIsExplained(unittest.TestCase):
    """macOS 15 and later do not let right-click > Open past the block on an app
    that is not signed. The way through is Open Anyway in System Settings, for
    about an hour after the blocked attempt, and the disk image's Read Me is the
    only help a recipient has."""

    def test_the_read_me_walks_through_open_anyway_in_order(self):
        block = re.sub(r"\s+", " ", _readme_block())
        done = block.index("click Done")
        settings = block.index("Privacy & Security")
        anyway = block.index("Open Anyway")
        self.assertLess(done, settings)
        self.assertLess(settings, anyway)
        self.assertIn("about an hour", block)
        self.assertIn("password", block)
        self.assertNotIn("right-click", block.lower())

    def test_the_paid_route_is_named_as_the_lasting_fix(self):
        header = _read(DMG)[:_read(DMG).index("set -euo pipefail")]
        self.assertIn("Developer ID", header)
        self.assertIn("99 US dollars a year", header)


# ── NC-315 ────────────────────────────────────────────────────────────────────

def _http_404(*_a, **_k):
    raise urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", {}, None)


class TestNewVersionsHaveAPlaceToComeFrom(Base):
    """A handed-out copy knows its release page; a repository with no release
    yet is a calm answer, asked about at most once a day, not an error."""

    NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

    def test_no_release_yet_is_a_calm_answer_remembered_for_the_day(self):
        calls = []

        def urlopen(*a, **k):
            calls.append(1)
            _http_404()

        with mock.patch.object(updates_mod.urllib.request, "urlopen", urlopen):
            out = updates_mod.check(self.conn, repo="owner/name", now=self.NOW)
            again = updates_mod.check(self.conn, repo="owner/name",
                                      now=self.NOW + timedelta(hours=2))
        self.assertTrue(out["configured"])
        self.assertIsNone(out["latest"])
        self.assertFalse(out["update_available"])
        self.assertFalse(out["stale"], "a 404 read as 'could not check'")
        self.assertIsNotNone(out["checked_at"])
        self.assertIn("No new version has been published yet", out["says"])
        self.assertIn("https://github.com/owner/name/releases", out["says"])
        self.assertEqual(len(calls), 1, "it asked GitHub again within the day")
        self.assertEqual(again["says"], out["says"])

    def test_other_failures_still_retry(self):
        def offline(*a, **k):
            raise urllib.error.URLError("offline")

        with mock.patch.object(updates_mod.urllib.request, "urlopen", offline):
            out = updates_mod.check(self.conn, repo="owner/name", now=self.NOW)
        self.assertTrue(out["stale"])
        self.assertIn("Could not reach GitHub", out["says"])

    def test_each_answer_has_its_sentence(self):
        newer = updates_mod.check(self.conn, repo="owner/name", now=self.NOW,
                                  fetcher=lambda: {"version": "9.9.9", "url": "https://example.com/r"})
        self.assertIn("Version 9.9.9 is out", newer["says"])
        self.assertIn("replace the app; your data stays", newer["says"])
        same = updates_mod.check(self.conn, repo="owner/name", force=True, now=self.NOW,
                                 fetcher=lambda: {"version": updates_mod.CURRENT_VERSION, "url": "u"})
        self.assertIn("This is the newest version", same["says"])
        self.assertEqual(updates_mod.check(self.conn, repo="")["says"],
                         "This copy does not check for updates.")

    def test_a_screen_can_say_where_versions_come_from_without_going_online(self):
        said = updates_mod.channel_says("owner/name")
        self.assertIn("https://github.com/owner/name/releases", said)
        self.assertIn("replace the app; your data stays", said)
        self.assertEqual(updates_mod.channel_says(""), "This copy does not check for updates.")

    def test_a_handout_build_writes_the_public_repository_into_the_app(self):
        src = _read(APPIFY)
        out = _bash(f'REPO_ROOT="{REPO}"\n' + _function(src, "public_repo") + "\npublic_repo")
        self.assertEqual(out.stdout.strip(), updates_mod.PUBLIC_REPO)
        self.assertRegex(updates_mod.PUBLIC_REPO, r"^[\w.-]+/[\w.-]+$")
        tail = src[src.index("UPDATE_REPO=\"${NC_UPDATE_REPO:-}\""):]
        self.assertIn('if [ "$HANDOUT" = 1 ] && [ -z "$UPDATE_REPO" ]; then', tail)
        self.assertIn('> "$APP_DIR/Contents/Resources/update_repo.txt"', tail)

    def test_the_app_hands_the_address_to_the_engine(self):
        """Moved 2026-10-17 (NC-330) from the launcher's block that read
        update_repo.txt (and ran against a stand-in file). The app's program does it
        in EngineLaunch.environment: the address is read from the file beside the
        bundle, only the characters a repository name has pass (so a file holding
        "owner/name; touch /tmp/x" cannot carry a command anywhere), an empty one is
        not passed at all, and an NC_UPDATE_REPO the app itself inherited from a shell
        is dropped first, so nothing but the file decides where the check looks."""
        env = _swift_function(_read(ENGINE_SWIFT), "func environment(code: URL)")
        self.assertIn('resources.appending(path: "update_repo.txt")', env)
        self.assertIn('CharacterSet.alphanumerics.contains($0) || "._/-".unicodeScalars.contains($0)', env)
        self.assertIn('if !repo.isEmpty { env["NC_UPDATE_REPO"] = repo }', env)
        self.assertLess(env.index('key.hasPrefix("NC_")'), env.index('env["NC_UPDATE_REPO"]'))

    def test_the_read_me_and_the_readme_say_how_to_update(self):
        self.assertIn("replace the app; your data stays", _readme_block())
        self.assertIn("__RELEASES__", _readme_block())
        self.assertIn('s|__RELEASES__|$RELEASES|', _read(DMG))
        readme = _read(os.path.join(REPO, "README.md"))
        self.assertIn(f"https://github.com/{updates_mod.PUBLIC_REPO}/releases", readme)
        self.assertIn("replace the app; your data stays", readme)


# ── NC-316 ────────────────────────────────────────────────────────────────────

PIN = re.compile(r"^([A-Za-z0-9._-]+)==([0-9][A-Za-z0-9.+!-]*)$")


def _pins(name):
    pins = {}
    for line in _read(os.path.join(REPO, name)).splitlines():
        line = line.strip()
        # NC-316: a fingerprint line belongs to the version line above it.
        if not line or line.startswith(("#", "--hash=")):
            continue
        line = line.removesuffix("\\").strip()
        m = PIN.match(line)
        assert m, f"{name}: {line!r} is not one exact version"
        key = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        assert key not in pins, f"{name} names {key} twice"
        pins[key] = m.group(2)
    return pins


class TestTheFirstLaunchInstallsExactVersions(_TempDir):
    """Each person got whatever was newest on the day they first opened the app;
    a package taken over by someone else would have run with access to their
    messages. Now every package is one exact version with its fingerprints (hashes)
    in requirements-lock.txt, and pip (the tool that installs Python packages) is told
    to refuse any file that does not match
    (tests/test_python_env.py checks both: a fingerprint for every package, and the
    flags pip is given)."""

    def test_every_package_is_one_exact_version_and_the_two_lists_agree(self):
        wanted = _pins("requirements.txt")
        locked = _pins("requirements-lock.txt")
        self.assertGreaterEqual(len(wanted), 3)
        for name, version in wanted.items():
            with self.subTest(package=name):
                self.assertEqual(locked.get(name), version,
                                 "requirements.txt and the lock disagree")
        self.assertGreater(len(locked), len(wanted),
                           "the lock lists only the packages asked for, not what they pull in")

    def test_the_build_ships_every_list_the_install_reads(self):
        """Moved 2026-10-17 (NC-330). The launcher's install ran `pip install -r
        requirements.txt -c requirements-lock.txt`, and this checked that line and the
        build copying the lock in. The install is crm/python_env.py's now, which reads
        the lists from beside the engine's code (tests/test_python_env.py holds what it
        does with them); what is left to check here is that the build puts all three
        lists there: the first start's two, and the meaning-based search's, which Settings
        installs later from the same folder (crm/api_instance.py)."""
        src = _read(APPIFY)
        for name in ("requirements.txt", "requirements-lock.txt", "requirements-semantic.txt"):
            with self.subTest(list=name):
                self.assertIn(f'cp "$REPO_ROOT/{name}" "$APP_CODE_DIR/{name}"', src)

    def test_a_changed_lock_installs_again(self):
        """Moved 2026-10-17 (NC-330) from the launcher's requirements_hash, run as shell,
        to python_env.requirements_hash. The first start installs once per package list
        and again when the list changes, so the stamp of what was installed has to
        change when only the lock does (a new version of the app with the same
        requirements.txt but a different pin)."""
        home = self.d

        def digest():
            return python_env.requirements_hash(home)

        self.assertEqual(digest(), "", "no list at all must read as nothing to install")
        with open(os.path.join(home, "requirements.txt"), "w") as fh:
            fh.write("alpha==1.0\n")
        alone = digest()
        with open(os.path.join(home, "requirements-lock.txt"), "w") as fh:
            fh.write("alpha==1.0\nbeta==2.0\n")
        with_lock = digest()
        with open(os.path.join(home, "requirements-lock.txt"), "w") as fh:
            fh.write("alpha==1.0\nbeta==2.1\n")
        self.assertEqual(len({alone, with_lock, digest()}), 3)
        self.assertRegex(alone, r"^[0-9a-f]{64}$")


class TestTheWindowOpensFirstOnAFirstLaunch(unittest.TestCase):
    """The first launch built the Python environment and installed packages, a
    minute or more, before any window appeared: only a bouncing Dock icon. Now
    the window opens at once and says it is setting up.

    NC-330 (2026-10-17): this ran the launcher end to end on a stand-in bundle (a
    stand-in Python whose `venv` (private environment) and `pip` steps were quick fakes,
    a stand-in server and a
    stand-in window that recorded what it saw), and handed the window a "setting up"
    marker file. The launcher and the marker are gone: the native program is the
    window, it is on screen while it starts the engine, and EngineController.state says
    which step it is on. The order that mattered (window first, the install next, the
    engine only after the install, a second open installing nothing) is read from
    Engine.swift below; what the install itself does is held by tests/test_python_env.py."""

    def setUp(self):
        self.swift = _read(ENGINE_SWIFT)
        self.start = _swift_function(self.swift, "private func startEngine(")

    def test_the_install_comes_before_the_engine_and_only_when_it_is_needed(self):
        start = self.start
        ready = start.index("launch.readyPython(base: basePython, code: code)")
        setting = start.index("state = .settingUp")
        setup = start.index("launch.setUpOptionalParts(base: basePython, code: code)")
        starting = start.index("state = .starting")
        run = start.index("try p.run()")
        self.assertLess(ready, setting, "the setup is announced before asking whether it is needed")
        self.assertLess(setting, setup, "the window says 'setting up' after the install began")
        self.assertLess(setup, starting)
        self.assertLess(starting, run, "the engine starts before its packages are installed")
        # A second open: the quick look says ready, so nothing is installed and the
        # window never says 'setting up'.
        self.assertRegex(
            start, r"if let ready = await launch\.readyPython\(base: basePython, code: code\) \{\s*"
                   r"python = ready\s*\} else \{\s*state = \.settingUp")
        self.assertIn("if launch.setsUpOptionalParts {", start)

    def test_a_failed_install_still_starts_the_engine(self):
        """python_env never fails the start (it leaves a note for Settings and the core
        runs on the bare Python); the app's side of that is falling back to the bare
        Python when the setup gave nothing back."""
        self.assertIn("python = await launch.setUpOptionalParts(base: basePython, code: code) ?? basePython",
                      self.start)

    def test_the_window_says_what_is_happening(self):
        """Was: the launcher's setting-up message, read from the old window program."""
        root = _read(ROOT_VIEW_SWIFT)
        self.assertIn("case .settingUp:\n            EngineWaitingView(sentence: EngineWaitingView.settingUp)", root)
        shown = up_to(root, "static let settingUp = ", "\n")
        for words in ("Setting up", "about a minute", "needs the internet"):
            self.assertIn(words, shown)


# ── NC-225 ────────────────────────────────────────────────────────────────────

class TestTheInterpreterIsPreparedOnce(_TempDir):
    """Every build unpacked, trimmed, stripped and signed the bundled Python and
    built a throwaway environment to check it: about 190 MB written per build.
    The prepared tree is kept and each later build clones it.

    The real functions, run twice against a stand-in tarball, with the tools
    that do the writing replaced by ones that log their calls."""

    def _run_embed(self, dest):
        src = _read(APPIFY)
        fns = src[src.index("prepared_key() {"):src.index("strip_caches() {")]
        bin_dir = os.path.join(self.d, "bin")
        calls = os.path.join(self.d, "calls.log")
        _stub(bin_dir, "uname", 'echo arm64')
        _stub(bin_dir, "strip", f'echo strip >> "{calls}"')
        _stub(bin_dir, "codesign", f'echo codesign >> "{calls}"')
        _stub(bin_dir, "tar", f'echo tar >> "{calls}"; exec /usr/bin/tar "$@"')
        script = (f'set -euo pipefail\nPATH="{bin_dir}:$PATH"\n'
                  f'PY_CACHE_DIR="{self.cache}"\nAPPIFY="{APPIFY}"\nREPO_ROOT="{REPO}"\n'
                  + fns + f'\nembed_python "{dest}"')
        out = _bash(script)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        try:
            with open(calls) as fh:
                logged = fh.read().split()
        except FileNotFoundError:
            logged = []
        if os.path.exists(calls):
            os.remove(calls)
        return out.stdout + out.stderr, logged

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.d, "cache")
        tree = os.path.join(self.d, "src", "python")
        _stub(os.path.join(tree, "bin"), "python3", """
case "$1" in
  -c) echo "3.13.0 3.46.0" ;;
  -m) if [ "$2" = venv ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python3"; fi ;;
  -) cat >/dev/null; echo "  CPython  3.13.0  PSF-2.0" ;;
esac
exit 0
""")
        os.makedirs(os.path.join(tree, "lib", "python3.13"))
        with open(os.path.join(tree, "lib", "python3.13", "os.py"), "w") as fh:
            fh.write("# a stand-in\n")
        os.makedirs(self.cache)
        tarball = os.path.join(self.cache, "cpython-3.13.0+20990101-aarch64-apple-darwin-install_only.tar.gz")
        subprocess.run(["/usr/bin/tar", "czf", tarball, "-C", os.path.join(self.d, "src"), "python"],
                       check=True)

    def test_a_second_build_clones_instead_of_preparing_again(self):
        first = os.path.join(self.d, "one", "python")
        said, logged = self._run_embed(first)
        self.assertIn("tar", logged)
        self.assertIn("codesign", logged)
        self.assertTrue(os.path.exists(os.path.join(first, "bin", "python3")))
        self.assertTrue(os.path.exists(os.path.join(first, "THIRD-PARTY-LICENSES.txt")))
        prepared = [n for n in os.listdir(self.cache) if n.startswith("prepared-")]
        self.assertEqual(len(prepared), 1, os.listdir(self.cache))
        self.assertFalse([n for n in os.listdir(self.cache) if n.startswith(".staging")])

        second = os.path.join(self.d, "two", "python")
        said, logged = self._run_embed(second)
        self.assertEqual(logged, [], "the second build unpacked or signed the interpreter again")
        self.assertIn("reusing the interpreter prepared earlier", said)
        self.assertIn("bundled CPython 3.13.0 3.46.0", said, "the line builders check for is gone")
        self.assertTrue(os.path.exists(os.path.join(second, "bin", "python3")))

    def test_a_new_tarball_prepares_again_and_the_oldest_goes(self):
        self._run_embed(os.path.join(self.d, "one", "python"))
        tarball = [n for n in os.listdir(self.cache) if n.endswith(".tar.gz")][0]
        path = os.path.join(self.cache, tarball)
        for step in (1, 2):
            later = os.stat(path).st_mtime + 60 * step      # same name, a new download
            os.utime(path, (later, later))
            said, logged = self._run_embed(os.path.join(self.d, f"again{step}", "python"))
            self.assertIn("tar", logged)
        prepared = [n for n in os.listdir(self.cache) if n.startswith("prepared-")]
        self.assertEqual(len(prepared), 2, "the two newest are kept, older ones go")

    def test_a_half_prepared_tree_is_never_reused(self):
        self._run_embed(os.path.join(self.d, "one", "python"))
        prepared = [n for n in os.listdir(self.cache) if n.startswith("prepared-")][0]
        os.remove(os.path.join(self.cache, prepared, ".ready"))
        said, logged = self._run_embed(os.path.join(self.d, "two", "python"))
        self.assertIn("tar", logged)


# ── NC-311 ────────────────────────────────────────────────────────────────────

class TestAHandoutCarriesItsOwnIdentity(_TempDir):
    """Every Mac that opened a handed-out app would have listed it, and its
    always-on helper, in Login Items and Full Disk Access under the identity of
    the checkout it was built from. A handout must carry one under io.github."""

    def test_the_identity_rule(self):
        fn = _function(_read(APPIFY), "handout_identity_ok")
        for ident, ok in (("io.github.someone.networkcrm", 0), ("com.example.networkcrm", 1),
                          ("", 1), ("io.githubx.networkcrm", 1)):
            with self.subTest(ident=ident):
                self.assertEqual(_bash(f'{fn}\nhandout_identity_ok "{ident}"').returncode, ok)

    def test_a_refused_handout_writes_nothing(self):
        src = re.sub(r'^BUNDLE_ID="[^"]*"$', 'BUNDLE_ID="com.example.networkcrm"',
                     _read(APPIFY), count=1, flags=re.M)
        self.assertIn('BUNDLE_ID="com.example.networkcrm"', src)
        here = os.path.join(self.d, "packaging")
        os.makedirs(here)
        with open(os.path.join(here, "appify.sh"), "w") as fh:
            fh.write(src)
        dist = os.path.join(self.d, "dist")
        env = dict(os.environ, NC_DIST_DIR=dist)
        out = subprocess.run(["/bin/bash", os.path.join(here, "appify.sh"), "--handout"],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 1)
        self.assertIn("io.github.", out.stderr)
        self.assertFalse(os.path.exists(dist), "a refused handout still started building")

    def test_the_checks_run_before_anything_is_removed_or_built(self):
        """The identity check comes before the staging folder is cleared and made, the
        window compiled or anything written. Moved 2026-10-17 (NC-330): the build used
        to clear and remake the finished app and the Quit app in place (`rm -rf
        "$APP_DIR" "$QUIT_APP_DIR"`); it builds in dist/.building now, so the check
        comes before that is touched, and there is one app and one identity, not two."""
        src = _read(APPIFY)
        guard = src.index('if [ "$HANDOUT" = 1 ] && ! handout_identity_ok "$BUNDLE_ID"; then')
        self.assertLess(guard, src.index('rm -rf "$STAGE_DIR"\nAPP_DIR='))
        self.assertLess(guard, src.index('mkdir -p "$APP_DIR/Contents/MacOS"'))
        self.assertLess(guard, src.index('"$REPO_ROOT/native/compile.sh"'))
        self.assertIn('write_plist "$APP_DIR" "Network CRM" "$BUNDLE_ID" "$EXE_NAME"', src)
        self.assertEqual(src.count('write_plist "$'), 1,
                         "a second bundle was added; each would need the identity rule too")

    def test_a_handout_missing_a_part_is_refused_and_nothing_is_replaced(self):
        """Was: a handout missing a part was removed, not left to be zipped. Now the
        build is made in dist/.building and the finished app is swapped in only at
        the very end, so a refusal leaves the previous app exactly as it was and the
        half-made one is removed by the build's exit handler (2026-10-17, NC-330)."""
        src = _read(APPIFY)
        block = up_to(src, "# NC-311: a handout is all or nothing.", '    exit 1\n  fi\nfi\n')
        for part in ('"$PY_SHIPPED" = 1', '"$IMSG_HELPER_SHIPPED" = 1', '-n "$UPDATE_REPO"'):
            self.assertIn(part, block)
        self.assertIn("Nothing was replaced.", block)
        # The swap is the last thing the build does: after the refusal, the signature
        # and the smoke test.
        swap = src.index('mv "$APP_DIR" "$FINAL_APP_DIR"')
        for earlier in ("# NC-311: a handout is all or nothing.", "codesign --verify --strict",
                        '--engine-check'):
            self.assertLess(src.index(earlier), swap, f"{earlier} comes after the swap")
        # Run it, beside a previous app and a half-made new one, with the build's own
        # exit handler: a build that lacks its own Python is refused, the previous app
        # is untouched and the half-made one is gone.
        exit_handler = re.search(r"^trap '[^\n]*' EXIT$", src, re.M).group(0)
        dist = os.path.join(self.d, "dist")
        previous = os.path.join(dist, "Network CRM.app")
        os.makedirs(previous)
        script = (f'DIST_DIR="{dist}"\nSTAGE_DIR="$DIST_DIR/.building"\n'
                  'APP_DIR="$STAGE_DIR/Network CRM.app"\nmkdir -p "$APP_DIR"\n'
                  + exit_handler + '\n'
                  'HANDOUT=1\nPY_SHIPPED=0\nIMSG_HELPER_SHIPPED=1\n'
                  f'UPDATE_REPO=owner/name\nPY_CACHE_DIR=/nowhere\n' + block)
        out = _bash(script)
        self.assertEqual(out.returncode, 1)
        self.assertIn("its own Python", out.stderr)
        self.assertTrue(os.path.isdir(previous), "the previous app was removed by a refused build")
        self.assertFalse(os.path.exists(os.path.join(dist, ".building")),
                         "the half-made app was left behind, to be zipped by mistake")


# ── NC-331, NC-332 ────────────────────────────────────────────────────────────
# TestTheWindowSavesFilesAndOpensPhotos was deleted on 2026-10-17 (NC-330), three tests.
# A web view saves nothing and shows no file panel unless the window program wires up
# the delegates that do (WKDownloadDelegate for an export, runOpenPanelWith for
# "Choose a photo"); they checked those methods were there. The window is native
# now: its export buttons and "Choose a photo" open NSSavePanel and NSOpenPanel
# directly (native/Sources/DataExport.swift, ListsExport.swift, PersonHeader.swift),
# and there is no web view to forget to wire up.


# ── NC-330 ────────────────────────────────────────────────────────────────────

class TestTheBuildMakesOneNativeApp(_TempDir):
    """One program, its own Python, built in a staging folder and swapped in at the
    end. What the build refuses, and what it no longer carries."""

    def _appify_copy(self, args):
        """appify.sh as a copy in a folder of its own, run with `args` and a build folder
        of its own. Only for options it refuses before it does anything."""
        here = os.path.join(self.d, "packaging")
        os.makedirs(here)
        with open(os.path.join(here, "appify.sh"), "w") as fh:
            fh.write(_read(APPIFY))
        dist = os.path.join(self.d, "dist")
        out = subprocess.run(["/bin/bash", os.path.join(here, "appify.sh")] + args,
                             capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, NC_DIST_DIR=dist))
        return out, dist

    def test_a_build_without_its_own_python_is_refused_in_words(self):
        """Was: --no-python made a lean bundle that hunted for a Python on the Mac.
        Since NC-330 the app starts its engine on its own Python or not at all, so
        the option says so and stops, before it builds anything."""
        out, dist = self._appify_copy(["--no-python"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("--no-python is gone", out.stderr)
        self.assertFalse(os.path.exists(dist), "a refused option still started building")

    def test_the_older_spelling_still_means_the_same_build(self):
        src = _read(APPIFY)
        self.assertIn("--embed) EMBED=1 ;;", src)
        self.assertIn("--with-python) EMBED=1 ;;", src)
        self.assertIn("--handout) EMBED=1; HANDOUT=1 ;;", src)

    def test_an_embedded_build_stops_where_the_archive_is_missing(self):
        """The app cannot start its engine without its Python, so a build that would
        make one without says where to put the archive and replaces nothing."""
        src = _read(APPIFY)
        empty = os.path.join(self.d, "cache")
        os.makedirs(empty)
        script = (f'PY_CACHE_DIR="{empty}"\n'
                  + "uname() { echo arm64; }\n"
                  + _function(src, "embed_python") + f'\nembed_python "{self.d}/into"')
        out = _bash(script)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no cached CPython tarball", out.stderr)
        # And the build treats that as the end of it.
        caller = up_to(src, 'if embed_python "$APP_DIR/Contents/Resources/python"; then', "\n  fi\n")
        self.assertIn("exit 1", caller.split("else", 1)[1])
        self.assertIn("nothing was replaced", caller)

    def test_none_of_the_old_app_is_built_any_more(self):
        """No launcher script, no window program, no web page folder, no second
        'Quit' bundle; and the one an earlier build left in dist/ is removed."""
        code = re.sub(r"(?m)^\s*#.*$", "", _read(APPIFY))
        for gone in ("launcher.sh.tmpl", "NetworkCRMWindow", "__MODE__", 'REPO_ROOT/static',
                     "LSUIElement", "compile_native_window"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, code)
        self.assertIn('[ -d "$OLD_QUIT_APP_DIR" ] && rm -rf "$OLD_QUIT_APP_DIR"', code)
        self.assertRegex(code, r'EXE_NAME="NetworkCRM"')


# ── NC-290 item 9, NC-324 ─────────────────────────────────────────────────────

class TestTheReadmeSaysHowToReportAndWhatProtectsTheData(unittest.TestCase):

    def setUp(self):
        self.readme = _read(os.path.join(REPO, "README.md"))

    def test_how_to_report_a_problem_or_a_security_issue(self):
        section = up_to(self.readme, "## Reporting a problem", "\n---")
        self.assertIn(f"https://github.com/{updates_mod.PUBLIC_REPO}/issues", section)
        self.assertIn("private vulnerability reporting", section)
        self.assertIn("Report a vulnerability", section)
        self.assertIn("do not open a public issue", section)

    def test_it_says_the_copies_are_not_encrypted(self):
        flat = re.sub(r"\s+", " ", self.readme)
        self.assertIn("stored unencrypted in its data folder", flat)
        self.assertIn("any program you run yourself can read it", flat)
        self.assertIn("FileVault", flat)


# ── NC-249 ────────────────────────────────────────────────────────────────────

class TestEveryBundledPartHasItsLicenceText(unittest.TestCase):
    """The built app named its Python's parts and their licences without the
    texts, which most of those licences require in a program handed out built."""

    TEXT_FOR = {"SQLite": "sqlite", "OpenSSL": "openssl-3", "zlib": "zlib",
                "libexpat": "expat", "libmpdec": "mpdecimal", "libffi": "libffi",
                "liblzma / xz": "liblzma", "bzip2": "bzip2", "ncurses": "ncurses",
                "libedit (NetBSD editline)": "libedit", "libuuid": "libuuid"}

    def test_every_listed_part_has_a_text_in_the_repository(self):
        src = _read(APPIFY)
        body = _function(src, "write_python_licenses")
        listed = set(re.findall(r'rows\.append\(\("([^"]+)"', body)) | {"libedit (NetBSD editline)"}
        listed.discard("CPython")   # its own licence travels inside the tree
        self.assertEqual(listed, set(self.TEXT_FOR), "a part was listed or dropped without its text")
        folder = os.path.join(REPO, "packaging", "licenses")
        for part, name in self.TEXT_FOR.items():
            with self.subTest(part=part):
                path = os.path.join(folder, f"LICENSE.{name}.txt")
                self.assertTrue(os.path.isfile(path), f"no text for {part}")
                self.assertGreater(os.path.getsize(path), 300)

    def test_the_build_appends_every_text(self):
        body = _function(_read(APPIFY), "write_python_licenses")
        self.assertIn('"$REPO_ROOT"/packaging/licenses/LICENSE.*.txt', body)
        self.assertIn('cat "$f" >> "$out"', body)
        self.assertLess(body.index("LICEOF\n  # NC-249"), body.index('cat "$f" >> "$out"'),
                        "the texts must follow the list, not overwrite it")


# ── NC-378 ────────────────────────────────────────────────────────────────────

class TestSpotlightListsOnlyTheRealApp(unittest.TestCase):
    """Spotlight listed three "Network CRM" apps: the real one, the developer copy and
    the loose app the hand-out build leaves beside its disk image. Opening the wrong
    one starts a second app on the same data. macOS's Spotlight skips any folder
    whose name ends in .noindex, so the developer copy builds into one; the hand-out
    folder is checked where it is defined, in the public-copy tests."""

    def test_the_developer_copy_builds_where_spotlight_does_not_look(self):
        src = _read(os.path.join(NATIVE, "build.sh"))
        uses = re.findall(r'"\$HERE/(dist[^/"]*)/', src)
        self.assertTrue(uses, "build.sh no longer names its output folder")
        self.assertEqual(set(uses), {"dist.noindex"}, "a build.sh path still writes to native/dist/")
        with open(os.path.join(NATIVE, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn("dist.noindex/", fh.read().split())

    def test_the_real_app_stays_where_spotlight_finds_it(self):
        src = _read(APPIFY)
        self.assertIn('DIST_DIR="${NC_DIST_DIR:-$SCRIPT_DIR/dist}"', src)


if __name__ == "__main__":
    unittest.main()
