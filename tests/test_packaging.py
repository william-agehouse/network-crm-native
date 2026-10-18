"""Guards on what the packaged .app actually ships.

The bug these exist for: `anthropic` sat in a commented-out "# Optional" block in
requirements.txt. The app's first start installs that list into its own private Python
environment (crm/python_env.py; before NC-330, on 2026-10-17, the launcher script did
it), so the package was never installed in any copy of the app.
The Ask view caught the ImportError, fell back to its rule-based engine, and
answered questions badly while looking like it worked. Every unit test passed.

Nothing here needs the network, a venv, or a built bundle. Where a check is about the
app's own program (native/Sources, which is Swift and is not compiled or run by this
suite) it reads that source for the one line or ordering that carries the property.
"""

import ast
import re
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402,F401 — config is the data-dir bootstrap
    config, up_to,
)

import api_fda  # noqa: E402 — the Python half of the helper's report contract

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")
NATIVE_SOURCES = os.path.join(REPO, "native", "Sources")
ENGINE_SWIFT = os.path.join(NATIVE_SOURCES, "Engine.swift")


def _native(name):
    """A source file of the app's own program (native/Sources). It is Swift and this
    suite does not compile or run it; the checks below read it for the one line or the
    one ordering that carries a property."""
    with open(os.path.join(NATIVE_SOURCES, name), encoding="utf-8") as fh:
        return fh.read()


def _swift_function(src, signature):
    """One Swift function or property, from its signature to the closing brace that
    sits at the same indent as the signature."""
    start = src.index(signature)
    indent = src[src.rfind("\n", 0, start) + 1:start]
    return src[start:src.index("\n" + indent + "}\n", start)]


def _swift_code(src):
    """Swift with its `//` comments taken out, for checks that must not match the
    explanation sitting beside the code they guard."""
    return re.sub(r"//[^\n]*", "", src)


# import name -> the distribution that provides it
DISTRIBUTION = {
    "anthropic": "anthropic",
    "telegram": "python-telegram-bot",
    "apscheduler": "APScheduler",
    "google": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "googleapiclient": "google-api-python-client",
    "sentence_transformers": "sentence-transformers",
    "numpy": "numpy",
    "selenium": "selenium",
    "openai": "openai",
    "requests": "requests",
}

# Third-party imports that must NOT be in requirements.txt, each with the reason.
# Anything here is a deliberate exclusion, not an oversight.
DELIBERATELY_NOT_SHIPPED = {
    # ~2GB of PyTorch. Opt-in from the Settings view; search_cli.embeddings_available()
    # is the runtime check and keyword/FTS search works fully without it.
    "sentence-transformers": "opt-in: pulls PyTorch (~2GB)",
    "numpy": "opt-in: only needed alongside sentence-transformers",
    # Developer-machine only.
    "selenium": "dev-only: the LinkedIn scraper runs outside the packaged app",
}


def _requirements():
    """Distribution names requirements.txt actually installs (comments excluded)."""
    names = set()
    with open(os.path.join(REPO, "requirements.txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in (">=", "==", "<=", "~=", ">", "<", "["):
                if sep in line:
                    line = line.split(sep, 1)[0]
            names.add(line.strip().lower())
    return names


def _first_party():
    # The package's own name counts: a `from crm.db import get_db` fallback branch
    # is a self-reference, not a dependency. os.path.basename(CRM) states it
    # directly, not as a side effect of some module's name.
    names = {os.path.basename(CRM)}
    names.update(os.path.splitext(f)[0] for f in os.listdir(CRM) if f.endswith(".py"))
    return names


def _imported_names():
    """Every module imported anywhere under crm/ — including imports nested inside
    functions, which is how the optional dependencies are written (that is the
    graceful-degradation pattern, and it is also what hides them from a naive scan).
    """
    found = {}
    for fname in sorted(os.listdir(CRM)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(CRM, fname)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.setdefault(a.name.split(".")[0], set()).add(fname)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    found.setdefault(node.module.split(".")[0], set()).add(fname)
    return found


class TestRequirementsCoverEveryImport(unittest.TestCase):
    def test_every_third_party_import_is_shipped_or_explicitly_excluded(self):
        stdlib = set(sys.stdlib_module_names)
        first_party = _first_party()
        shipped = _requirements()
        problems = []
        for mod, files in sorted(_imported_names().items()):
            if mod in stdlib or mod in first_party or mod.startswith("_"):
                continue
            dist = DISTRIBUTION.get(mod, mod).lower()
            if dist in shipped:
                continue
            if DISTRIBUTION.get(mod, mod) in DELIBERATELY_NOT_SHIPPED:
                continue
            problems.append(
                f"  {mod!r} (imported by {', '.join(sorted(files))}) -> "
                f"distribution {dist!r} is not in requirements.txt and is not "
                f"listed in DELIBERATELY_NOT_SHIPPED"
            )
        self.assertEqual(problems, [], "\n".join([
            "",
            "Third-party imports the packaged app would NOT have installed:",
            *problems,
            "",
            "The app's first start installs requirements.txt (at the versions in",
            "requirements-lock.txt) into its own Python environment, so an import",
            "missing from that file means the feature silently degrades",
            "on every install. Either add it to requirements.txt, or add it to",
            "DELIBERATELY_NOT_SHIPPED with the reason.",
        ]))

    def test_anthropic_specifically_is_shipped(self):
        """The regression. anthropic powers Ask and the brief/draft generation, both
        advertised in the UI, and the Settings view manages ANTHROPIC_API_KEY — so
        it must be installed, not left to a comment."""
        self.assertIn("anthropic", _requirements(),
                      "anthropic must be an installed requirement: without it the Ask "
                      "view silently answers from the rule-based engine instead")

    def test_opt_in_deps_are_not_installed_by_default(self):
        """The other direction: nobody 'helpfully' uncomments the 2GB stack."""
        shipped = _requirements()
        for dist, why in DELIBERATELY_NOT_SHIPPED.items():
            self.assertNotIn(dist.lower(), shipped, f"{dist} must stay opt-in — {why}")


class TestGracefulDegradation(unittest.TestCase):
    def test_optional_imports_are_lazy_not_module_level(self):
        """Optional dependencies must be imported INSIDE the function that needs
        them. A module-level `import anthropic` would make the whole module fail to
        import when the package is absent, taking the route table down with it
        instead of degrading to the fallback (rule 8 in docs/EDITING.md)."""
        for fname, mod in (("chat.py", "anthropic"), ("api_briefs.py", "anthropic")):
            path = os.path.join(CRM, fname)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in tree.body:          # top level only
                if isinstance(node, ast.Import):
                    self.assertNotIn(mod, [a.name.split(".")[0] for a in node.names],
                                     f"{fname}: {mod} must be imported lazily inside "
                                     f"the function that uses it, not at module level")


class TestFdaHelperExitContract(unittest.TestCase):
    """The helper's exit code is the installer's ONLY signal — guard the ordering.

    WHAT CAN BREAK. The helper has a second source (WhatsApp), and its exit code
    could be changed to "0 if anything copied". But install_imessage_sync.sh
    branches on the exit code alone, and only code 2 makes it print the Full Disk
    Access steps and open the System Settings pane. WhatsApp needs no FDA, so on
    any Mac with WhatsApp Desktop the default outcome would become: iMessage
    blocked, WhatsApp copied, exit 0 — and the installer would tell the user
    "Full Disk Access is already granted" while showing no instructions at all.

    So the blocked check must come BEFORE the success check. That ordering is the
    whole contract, it is one line either way, and nothing else in the suite can
    see it — the binary is Swift and only compiles on a Mac with the CLT.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.swift = open(os.path.join(here, "packaging", "src",
                                       "imessage_sync.swift"), encoding="utf-8").read()
        self.installer = open(os.path.join(here, "packaging",
                                           "install_imessage_sync.sh"), encoding="utf-8").read()

    def _code_only(self, src):
        """Comments stripped: the contract is explained at length right beside the
        code that implements it, so a raw search matches the prose either way."""
        return re.sub(r"//[^\n]*", "", src)

    def test_blocked_is_checked_before_success(self):
        code = self._code_only(self.swift)
        blocked_at = code.find("if !blocked.isEmpty")
        success_at = code.find("if copiedAny || currentAny {\n    reportAndExit(code: 0)")
        self.assertGreater(blocked_at, -1, "the blocked check is gone entirely")
        self.assertGreater(success_at, -1, "the success exit is gone entirely")
        self.assertLess(
            blocked_at, success_at,
            "a blocked source must decide the exit code BEFORE any success does, "
            "or a blocked iMessage beside a copied WhatsApp exits 0 and the "
            "installer never shows the Full Disk Access steps")

    def test_blocked_exits_two_specifically(self):
        """2 is not arbitrary — it is the value the installer matches on."""
        code = self._code_only(self.swift)
        tail = code[code.find("if !blocked.isEmpty"):]
        self.assertIn("code: 2", tail.split("if copiedAny")[0],
                      "the blocked branch must exit 2, the code the installer reads")

    def test_installer_still_branches_on_two_and_opens_the_pane(self):
        self.assertIn('elif [ "$RC" = "2" ]', self.installer,
                      "the installer's FDA branch is keyed to exit code 2")
        fda_branch = self.installer.split('elif [ "$RC" = "2" ]')[1]
        self.assertIn("Privacy_AllFiles", fda_branch,
                      "the FDA branch must open the Full Disk Access pane")
        self.assertIn("Full Disk Access", fda_branch)

    def test_success_branch_does_not_claim_fda_is_granted(self):
        """It cannot know that. A Mac with only WhatsApp reaches exit 0 having
        never needed FDA at all, and claiming otherwise is how the user concluded
        the grant existed."""
        success_branch = self.installer.split('if [ "$RC" = "0" ]')[1].split('elif')[0]
        self.assertNotIn("already granted", success_branch)


class TestFdaHelperReportContract(unittest.TestCase):
    """The helper's JSON report is the app's ONLY window onto this permission.

    WHY THIS EXISTS. The exit code above tells the *installer* what happened. It
    tells the app nothing: in normal operation the helper is launched by launchd,
    so the only process that sees the exit code is launchd, some time ago.
    The app cannot look at ~/Library/Messages to find out for itself — being
    unable to look is the entire reason the helper exists. So the helper writes
    what it did, and crm/api_fda.py reads that file. If the two ever disagree
    about the filename, the app silently reports "the helper has not reported yet"
    forever, which is precisely the flavour of quiet wrongness that leaves a
    person unsure whether the permission was ever granted.

    Four strings have to agree between a Swift file, a bash script and a Python
    module. Nothing but this class can see all three.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.swift = open(os.path.join(here, "packaging", "src",
                                       "imessage_sync.swift"), encoding="utf-8").read()
        self.installer = open(os.path.join(here, "packaging",
                                           "install_imessage_sync.sh"), encoding="utf-8").read()
        # importers' copies are redirected into a temp dir by tests/helpers.py, so
        # compare basenames and the trailing path shape, not absolute paths.
        self.report_name = os.path.basename(api_fda.importers.IMSG_HELPER_REPORT)
        self.bin_name = os.path.basename(api_fda.importers.IMSG_HELPER_BIN)

    def _sh_code(self):
        """The installer with its `#` comments removed.

        Not optional: both installer gates below would trip on the comment that
        documents the very rule they enforce — one names the python it forbids,
        the other quotes the `"$BIN" || RC=$?` it replaced. A gate that trips on its
        own explanation gets deleted rather than fixed."""
        return re.sub(r"(?m)^\s*#.*$", "", self.installer)

    def test_swift_writes_the_file_python_reads(self):
        self.assertIn(f'"{self.report_name}"', self.swift,
                      f"the helper must write {self.report_name}, the name "
                      "importers.IMSG_HELPER_REPORT reads")

    def test_every_terminal_path_writes_a_report(self):
        """Including the blocked one — that is the case the app most needs to see.

        Regression shape: a helper that wrote the report only on success would
        leave a blocked helper with no report at all, and the app would say "has
        not reported yet" rather than "macOS is blocking iMessage".
        """
        code = re.sub(r"//[^\n]*", "", self.swift)
        # From the first terminal branch onward, so the func definition itself is
        # not counted as one of its own call sites.
        tail = code[code.find("if !blocked.isEmpty"):]
        self.assertEqual(tail.count("reportAndExit("), 3,
                         "all three terminal branches (blocked, success, nothing "
                         "found) must report before exiting")
        self.assertNotIn("exit(0)", tail,
                         "a bare exit(0) here skips the report the app reads")

    def test_report_lands_beside_the_copies_not_somewhere_else(self):
        """Inside the CRM's own Application Support folder. Anywhere else and the
        app would need a permission to read its own status file — ~/Library/Logs
        is no alternative, for exactly that reason."""
        self.assertIn('let reportFile = supportDir.appendingPathComponent', self.swift)

    def test_installer_and_importers_agree_on_the_binary_and_the_label(self):
        self.assertIn(f'BIN="$BIN_DIR/{self.bin_name}"', self.installer,
                      "importers.IMSG_HELPER_BIN names a binary the installer "
                      "does not install")
        self.assertIn(f'LABEL="{api_fda.importers.IMSG_HELPER_LABEL}"', self.installer,
                      "the LaunchAgent label is what /api/fda/recheck kickstarts")

    def test_installer_and_importers_agree_on_the_log(self):
        """The panel offers the log path when it has nothing better to say, and a
        wrong one sends the user to an empty file."""
        self.assertIn(f'LOG="$LOG_DIR/{os.path.basename(api_fda.importers.IMSG_HELPER_LOG)}"',
                      self.installer)
        self.assertIn('LOG_DIR="$HOME/Library/Logs/NetworkCRM"', self.installer)

    def test_the_installer_verifies_through_launchd_not_as_its_own_child(self):
        """A shell-launched run of the helper reports a FALSE failure.

        macOS attributes a TCC check to the *responsible* process, and for a binary
        spawned from a shell that is the terminal, not the binary — so the grant on
        the helper does not apply and the read is refused. So `launchctl
        kickstart` can copy chat.db while `"$BIN"` run straight from bash says
        "still needs Full Disk Access".

        The installer used to close with `"$BIN" || RC=$?`, which meant its own
        final verdict told a user who had just granted the permission correctly that
        macOS was still blocking them. Same class of bug as the exit-code ordering
        above: a signal that does not answer the question being asked of it.
        """
        code = self._sh_code()
        self.assertNotIn('"$BIN" || RC=$?', code,
                         "the installer is verifying by running the binary itself "
                         "again — that reports blocked even when the grant is fine")
        self.assertIn("launchctl kickstart -k", code,
                      "verification must go through launchd, the parent the grant "
                      "was actually given for")
        self.assertIn("sync-status.json", code,
                      "the outcome has to come from the helper's report, since the "
                      "exit code now belongs to launchd")

    def test_the_installer_does_not_reintroduce_a_python_dependency(self):
        """The whole reason this script ships a prebuilt binary is that a recipient
        has no Xcode Command Line Tools — and /usr/bin/python3 is a stub that
        prompts to install them. Reading the JSON report is done with plutil, which
        is in /usr/bin on every Mac and needs nothing."""
        code = self._sh_code()
        # `python` also appears legitimately in the printed hint about running the
        # importer; what must not appear is the installer *invoking* one.
        for bad in ("/usr/bin/python3 ", "$(python3", "`python3", "| python3"):
            self.assertNotIn(bad, code,
                             f"the installer invokes a python ({bad!r}) — that "
                             "breaks on a Mac with no developer tools")
        self.assertIn("plutil -extract", self.installer)

    def test_the_printed_fda_steps_do_not_tell_you_to_verify_the_broken_way(self):
        """Step 4 used to say `Re-run this to confirm: "$BIN"`, which would say
        "still blocked" forever no matter how correctly the permission was granted.
        Anyone following it would conclude the grant had failed."""
        fda_branch = self.installer.split('elif [ "$RC" = "2" ]')[1]
        self.assertNotIn('Re-run this to confirm', fda_branch)
        self.assertIn("launchctl kickstart", fda_branch,
                      "the confirm step must not be a bare run of the binary")

    def test_the_helper_still_has_no_network_and_no_subprocesses(self):
        """The report added a file write to a binary that holds Full Disk Access.
        That is the whole security budget of this thing — check the rest of the
        audit claim in its header is still true."""
        # Comments stripped: the header's audit claim NAMES the things it promises
        # not to do ("no URLSession, no sockets"), so a raw search matches the
        # promise as readily as a violation.
        code = re.sub(r"//[^\n]*", "", self.swift)
        for forbidden in ("URLSession", "Process(", "NSTask", "socket(", "system("):
            self.assertNotIn(forbidden, code,
                             f"{forbidden} in a binary with Full Disk Access")
        imports = re.findall(r"^import (\w+)", code, re.M)
        self.assertEqual(imports, ["Foundation"],
                         "the only import may be Foundation — the audit argument "
                         "in this file's header is that you can read all of it")


class TestTheHelperIsCheapEnoughToRunEveryTwoMinutes(unittest.TestCase):
    """The two changes that made a 2-minute cadence affordable, and the
    correctness rules they must not trade away.

    WHY THIS EXISTS. A helper that re-copied both message databases in full on
    every run would rewrite them completely each time. At a two-minute interval
    that is far too much disk writing, which is not a cadence you can ship. Two
    things fix it, and each is one line away from
    silently reverting to the expensive behaviour:

      * clonefile(2) makes the copy nearly free on APFS. Delete it and every
        run writes the whole databases again — with no test failure, no error,
        and no visible symptom except a hot laptop.
      * the cursor makes an unchanged source cost two stat() calls. The check it
        replaced compared the source against the DESTINATION, which the app used
        to rewrite itself (fold_wal checkpointed the copy), so it never matched
        and every run copied everything.

    Neither can be checked by running anything: this is Swift, it only compiles on
    a Mac with the CLT, and the thing being asserted is "no bytes were written",
    which is invisible from inside the program. Source gates are the only
    available instrument.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.swift = open(os.path.join(here, "packaging", "src",
                                       "imessage_sync.swift"), encoding="utf-8").read()
        self.installer = open(os.path.join(here, "packaging",
                                           "install_imessage_sync.sh"), encoding="utf-8").read()

    def _code(self):
        """Swift with `//` comments stripped. Mandatory here: the comment beside
        each of these gates names the very thing the gate forbids — "comparing
        against the destination is the obvious thing and it is wrong", "copying
        only the changed part … would break the snapshot". A gate that matches its
        own explanation is worse than no gate."""
        return re.sub(r"//[^\n]*", "", self.swift)

    def test_the_copy_tries_to_clone_before_it_copies(self):
        code = self._code()
        clone_at = code.find("clonefile(src.path, tmp.path")
        self.assertGreater(clone_at, -1,
                           "clonefile is gone — every run is back to writing "
                           "whole databases, which no other test can see")
        copy_at = code.find("fm.copyItem(at: src, to: tmp)")
        self.assertGreater(copy_at, clone_at,
                           "the real copy must be the fallback, not the first try")

    def test_a_refused_clone_falls_back_instead_of_failing(self):
        """clonefile is refused for reasons that are none of our business — EXDEV
        across volumes, ENOTSUP on HFS+ or a network mount. A helper that gave up
        there would stop syncing on someone else's Mac."""
        code = self._code()
        self.assertIn("if clonefile(src.path, tmp.path, 0) != 0 {\n            try fm.copyItem(at: src, to: tmp)",
                      code,
                      "a non-zero clonefile must fall through to a real copy")

    def test_the_clone_is_still_made_atomic_and_private(self):
        """The speedup must not cost the two properties the slow copy had: the
        importer never sees a half-written file, and the copy of every message you
        ever sent is not world-readable."""
        code = self._code()
        body = code[code.find("func copyOne("):code.find("let cursorFile")]
        self.assertIn("rename(tmp.path, dst.path)", body,
                      "the clone lands under a tmp name and is renamed into place")
        self.assertIn(".posixPermissions: 0o600", body)
        self.assertIn("fm.removeItem(at: tmp)", body,
                      "a failed clone must not leave a partial file behind")

    def test_freshness_is_judged_against_the_source_not_the_destination(self):
        """The regression this replaces, in one line. `fold_wal()` checkpointed the
        helper's copy — growing the main file, truncating the -wal — so a
        source-vs-destination comparison reported "everything changed" forever.
        fold_wal is gone now, but source-vs-cursor is still the right question."""
        code = self._code()
        # `var`, not `let`: a sweep merges its own files into
        # the same dictionary (NC-042/NC-043), which is what makes "one account
        # gained a file" the same kind of change as "the -wal grew".
        self.assertIn("var srcSig = signature(s.dir, s.parts)", code)
        self.assertIn("try sweepSignature(s.dir, sweep)", code,
                      "the sweep's files must join the same signature, or a new "
                      "per-account store never triggers a copy")
        self.assertIn("let lastSig = cursor[s.label] ?? [:]", code)
        self.assertIn("srcSig == lastSig", code,
                      "the skip must compare the source to the cursor")
        self.assertNotIn("signature(dest", code,
                         "signing the DESTINATION is the bug this replaced: the "
                         "app rewrites the copy, so it never matches again and "
                         "every run re-copies both databases")

    def test_the_skip_still_requires_the_copy_to_exist(self):
        """Otherwise deleting the copy by hand would be unrecoverable — the cursor
        would keep insisting there was nothing to do."""
        code = self._code()
        self.assertIn("destMainExists", code)
        skip = code[code.find("if !srcSig.isEmpty"):]
        self.assertIn("destMainExists", skip.split("\n")[0],
                      "the skip condition must include the destination check")

    def test_an_empty_signature_never_counts_as_up_to_date(self):
        """signature() returns [:] when it cannot stat the source — which is what
        a blocked read looks like. Treating that as "matches the cursor" on a
        first run (cursor also empty) would make a blocked helper claim to be
        current and report nothing for the app to act on."""
        self.assertIn("if !srcSig.isEmpty, srcSig == lastSig", self._code())

    def test_every_part_is_copied_together_every_time(self):
        """A fresh -wal beside a main file from an earlier run is a WAL salt
        mismatch: SQLite silently discards the WAL, so the newest messages vanish
        from the copy until the next checkpoint. Per-part skipping is the obvious
        next optimisation and it is a correctness bug — and with clonefile there
        is nothing left to buy."""
        code = self._code()
        loop = code[code.find("for name in s.parts"):code.find("cursor[s.label] = srcSig")]
        self.assertIn("copyOne(name, from: s.dir, to: dest)", loop)
        self.assertNotIn("signature", loop,
                         "no per-part freshness check inside the copy loop — all "
                         "parts of a snapshot must be taken together")

    def test_the_cursor_is_recorded_only_after_the_whole_copy_succeeded(self):
        """Recorded earlier, a failure halfway through would convince the next run
        there was nothing to do — and the missing messages would never arrive."""
        code = self._code()
        loop_at = code.find("for name in s.parts")
        record_at = code.find("cursor[s.label] = srcSig")
        self.assertGreater(record_at, loop_at,
                           "the cursor is written before the parts are copied")
        # And nothing between the loop and the record may return early.
        between = code[loop_at:record_at]
        self.assertNotIn("return .copied", between)

    def test_the_cursor_is_persisted_on_every_exit_path(self):
        """reportAndExit is the only way out (three terminal branches, all of them
        go through it), so saving there covers all of them. A cursor that is only
        written on success would be lost every time WhatsApp was absent."""
        code = self._code()
        tail = code[code.find("func reportAndExit("):]
        save_at = tail.find("saveCursor()")
        exit_at = tail.find("exit(code)")
        self.assertGreater(save_at, -1, "reportAndExit no longer saves the cursor")
        self.assertLess(save_at, exit_at, "saveCursor() runs before exit(code)")

    def test_the_cursor_lives_beside_the_copies_and_losing_it_is_harmless(self):
        """In the CRM's own Application Support dir, for the same reason as the
        report: anywhere else and reading our own bookkeeping would need a
        permission. And it must be loaded with `try?` — a corrupt cursor means
        "copy once more", never a crash-loop that stops syncing."""
        code = self._code()
        # `var` only so --self-test can point it at a temp directory; normal
        # operation never reassigns it.
        self.assertIn("var cursorFile = supportDir.appendingPathComponent", code)
        loader = code[code.find("func loadCursor()"):code.find("var cursor =")]
        self.assertIn("try? Data(contentsOf: cursorFile)", loader)
        self.assertIn("return [:]", loader,
                      "an unreadable cursor must degrade to 'copy once', not throw")

    def test_the_launch_agent_interval_matches_what_the_app_expects(self):
        """Freshness is the sum of two intervals — the helper noticing a new
        message and the app noticing the fresher copy. Turning down only one moves
        the floor and leaves the ceiling where it was, which is exactly the
        confusion the 6h/15min pairing produced."""
        self.assertIn('INTERVAL="${NC_IMESSAGE_SYNC_INTERVAL:-120}"', self.installer,
                      "the LaunchAgent interval is not 120s any more — check the "
                      "app's NC_AUTOSYNC_MINUTES agrees before changing it")
        import api_import
        self.assertEqual(api_import.AUTOSYNC_MINUTES_DEFAULT, 2,
                         "the app's check is slower than the helper's copy, so the "
                         "copy's freshness is wasted")

    def test_the_interval_is_still_a_documented_install_time_knob(self):
        """Two minutes is a choice, not a constant. It has to stay overridable
        without editing the script — someone on a spinning disk, or an HFS+ volume
        where clonefile is refused and every run is a real full copy, needs to
        turn it down."""
        self.assertIn("NC_IMESSAGE_SYNC_INTERVAL", self.installer)
        with open(os.path.join(REPO, "packaging", "IMESSAGE_SYNC.md"),
                  encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("NC_IMESSAGE_SYNC_INTERVAL", doc)
        self.assertNotIn("every 21600 seconds", doc,
                         "the doc still states the old 6-hour cadence as current")


class TestTheAppIconIsAnIconAndNotAnErrorMessage(unittest.TestCase):
    """packaging/src/appicon.svg must be well-formed XML.

    WHY THIS EXISTS — a malformed icon can ship while every build reports success.

    appify.sh rasterises the icon with `qlmanage -t`, which renders through WebKit.
    WebKit's response to malformed SVG is not an error, it is a PAGE: the pink
    "This page contains the following errors:" screen. qlmanage thumbnails that
    page and EXITS 0, so appify.sh's `if ! qlmanage ...` guard passes, sips and
    iconutil build a structurally perfect ten-size .icns out of it, and the app
    goes out with a rendered XML parse error as its Dock icon.

    The actual defect was one character class: the header comment described the
    accent colour by its CSS custom-property name, and a double hyphen is illegal
    inside an XML comment. Nothing in the build, the test suite, or a visual check
    of the .icns file's structure can see it — the only way to notice is to
    decode a PNG out of the icns and look at it.

    Two guards now: xmllint in appify.sh fails the build, and this test fails the
    suite. Keep both — the build guard only helps someone who runs appify.sh, and
    this one catches the edit itself.
    """

    def setUp(self):
        self.svg = os.path.join(REPO, "packaging", "src", "appicon.svg")

    def test_the_icon_svg_is_well_formed_xml(self):
        import xml.etree.ElementTree as ET
        try:
            ET.parse(self.svg)
        except ET.ParseError as exc:
            self.fail(
                f"packaging/src/appicon.svg is not well-formed XML ({exc}).\n"
                "WebKit would rasterize its own error page into the app icon and "
                "every build would still report success. Fix the SVG.")

    def test_no_double_hyphen_inside_an_xml_comment(self):
        """The specific mistake, named — because it is a natural thing to type.

        Writing a CSS custom property in prose (two hyphens then a word) inside the
        header comment is how this happens, and it is exactly what someone
        documenting a colour token would write.
        """
        with open(self.svg, encoding="utf-8") as fh:
            src = fh.read()
        for comment in re.findall(r"<!--(.*?)-->", src, re.S):
            self.assertNotIn(
                "--", comment,
                "a '--' inside an XML comment makes the whole SVG malformed; "
                "write CSS custom properties without their leading dashes here")

    def test_appify_refuses_to_build_a_malformed_icon(self):
        """The build-time half of the guard, so a contributor who never runs the
        suite still cannot ship an error page as the icon."""
        with open(os.path.join(REPO, "packaging", "appify.sh"), encoding="utf-8") as fh:
            sh = re.sub(r"(?m)^\s*#.*$", "", fh.read())
        self.assertIn("xmllint --noout", sh,
                      "appify.sh no longer validates the icon SVG before rasterizing it")
        # The guard block echoes the error and re-runs xmllint to print it, so look
        # at a window wide enough to contain the whole `if`, not just its head.
        block = sh.split("xmllint --noout", 1)[1][:900]
        self.assertIn("exit 1", block,
                      "a malformed icon SVG must fail the build, not warn — a wrong "
                      "icon is not a degraded build")


class TestTheVersionIsOneNumberInTwoPlaces(unittest.TestCase):
    """NC-001. The app reports `updates.CURRENT_VERSION`; macOS reports
    `CFBundleShortVersionString` from packaging/appify.sh. A comment in updates.py
    says to "keep in lockstep with ... appify.sh on release", which is exactly the
    kind of instruction that gets followed until it doesn't — and then the About
    line and Finder's Get Info disagree about which build this is.

    Also asserts the changelog names the version, so a release cannot be cut
    without a line saying what is in it. That is the whole point of NC-001:
    finished work was invisible because nothing anywhere declared it done.
    """

    def _repo(self, *parts):
        import os
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parts)

    def _read(self, *parts):
        import io as _io
        with _io.open(self._repo(*parts), encoding="utf-8") as fh:
            return fh.read()

    def test_the_python_and_the_plist_agree(self):
        import re
        py = re.search(r'CURRENT_VERSION\s*=\s*"([^"]+)"', self._read("crm", "updates.py"))
        self.assertIsNotNone(py, "updates.CURRENT_VERSION is gone")
        sh = re.search(r'<key>CFBundleShortVersionString</key>\s*\n\s*<string>([^<]+)</string>',
                       self._read("packaging", "appify.sh"))
        self.assertIsNotNone(sh, "appify.sh no longer sets CFBundleShortVersionString")
        self.assertEqual(py.group(1), sh.group(1),
                         "the version the app reports and the version macOS reports "
                         "disagree — bump both or neither")

    def test_the_changelog_names_the_current_version(self):
        import re
        version = re.search(r'CURRENT_VERSION\s*=\s*"([^"]+)"',
                            self._read("crm", "updates.py")).group(1)
        changelog = self._read("CHANGELOG.md")
        self.assertIn(version, changelog,
                      f"CHANGELOG.md never mentions {version}, so this build has no "
                      f"record of what is in it")


class TestTheInstallDocsAreTrue(unittest.TestCase):
    """NC-007. README's install section sold the `.app` as needing no terminal and
    said nothing about Python. Both halves were wrong in the same direction — it
    needed Python 3.10+ on the target Mac, and when the launcher found none it had
    to name a route that actually produced one.

    NC-056 corrected what that route is. These docs used to send people to
    `xcode-select --install`; Apple's developer tools install Python 3.9.6, which
    this app's own version gate rejects, so that advice was a loop. The gates below
    assert the RULE — name python.org, and mention xcode-select only to warn
    against it — because pinning the old literal is exactly how the wrong advice
    ships.

    NC-330 (2026-10-17) moved the requirement. The app starts its engine on the Python
    it carries or not at all, so the install section no longer asks anyone for one:
    Python 3.10+ is what RUNNING FROM SOURCE needs, and the quick start says so with
    the same python.org-and-warn-off-xcode rule. The checks follow it there.

    The pattern worth gating is not the specific sentence but WHERE the answer
    lives: every honest account of Gatekeeper and of the Python requirement was in
    `packaging/`, which someone who receives a `.app` never opens. So the DMG has
    to carry it.

    Three other README claims are gated too, each compared with the code: "No
    Full Disk Access required" (an opt-in helper now asks for exactly
    that), a default model of `claude-opus-4-8` (it is `claude-opus-5` in four
    places), and source rows for Telegram / Messenger / Instagram / WhatsApp photos
    that no longer exist (NC-015, NC-030)."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.here = here
        for name, path in (("readme", ["README.md"]),
                           ("dmg", ["packaging", "make_dmg.sh"])):
            with open(os.path.join(here, *path), encoding="utf-8") as fh:
                setattr(self, name, fh.read())

    def install_section(self):
        """The `### Install as a Mac app` section, bounded by the `---` that
        actually ends it.

        Not a fixed character window such as `self.readme[i:i + 2200]`: adding
        one bullet about the build-time interpreter cache pushes the Gatekeeper
        paragraphs past the window, and the tests fail while every sentence they
        check is still there. A section ends where the document says it ends.
        """
        i = self.readme.index("### Install as a Mac app")
        return self.readme[i:self.readme.index("\n---", i)]

    def assert_points_at_a_python_that_works(self, text, where):
        """The rule, not a literal: docs must offer python.org, and may name
        xcode-select ONLY inside a warning.

        Asserting the mere ABSENCE of "xcode-select --install" would be wrong —
        warning someone off it requires naming it. Asserting its PRESENCE (what
        these tests used to do) is satisfied by a warning just as well as by a
        recommendation, so it proves nothing either way.

        Proximity rather than a phrase list. A fixed string such as "not a fix
        for this" breaks when the README is reworded to "not a fix" while the
        docs stay correct. What the rule actually says is "there is a negation
        near the mention", so that is what gets checked.
        """
        import re
        low = re.sub(r"\s+", " ", text.lower())
        self.assertIn("python.org", low,
                      f"{where} names no route to a Python 3.10+ interpreter")
        NEG = (r"do not|don't|not a fix|too old|older than|below what|"
               r"end-of-life|end of life|will not run|won't run|no longer")
        for m in re.finditer(r"xcode-select", low):
            window = low[max(0, m.start() - 120):m.end() + 260]
            self.assertRegex(
                window, NEG,
                f"{where} mentions xcode-select at offset {m.start()} with no "
                "negation nearby, so it reads as the recommended fix — and "
                "Apple's tools install 3.9.6, which this app rejects")

    def assert_says_the_app_brings_its_own_python(self, text, where):
        """NC-021. The arm64 bundle carries its own interpreter, so docs that
        still front "you need Python 3.10+" as the headline requirement are
        now understating the app."""
        import re
        low = re.sub(r"\s+", " ", text.lower())
        self.assertRegex(
            low, r"carries its own python|own python inside|nothing to install|"
                 r"needs nothing installed",
            f"{where} does not say the app ships its own interpreter, so a "
            "recipient still believes they must install Python first")
        self.assertRegex(
            low, r"intel|apple silicon|arm64",
            f"{where} does not say which Macs the bundled interpreter covers — "
            "it is arm64-only, and an Intel recipient needs to know that")

    def test_the_install_section_says_the_app_brings_its_own_python(self):
        """Was test_the_install_section_admits_the_python_requirement (NC-007): the
        section had to say Python 3.10+ was needed. It is not, for the app (NC-330), so
        the section has to say the opposite, and say which Macs that covers. The
        Python 3.10+ requirement now belongs to running from source, where the same
        python.org rule applies to the whole README."""
        section = self.install_section()
        self.assert_says_the_app_brings_its_own_python(
            section, "README's install section")
        self.assert_points_at_a_python_that_works(
            self.readme, "README's Python requirement")
        self.assertNotIn("no Terminal window", section)

    def test_it_no_longer_claims_no_terminal_is_ever_needed(self):
        i = self.readme.index("### Install as a Mac app")
        heading = self.readme[i:self.readme.index("\n", i)]
        self.assertNotIn("no terminal", heading.lower(),
                         "the heading itself makes the claim the body then retracts")

    def test_both_gatekeeper_outcomes_are_named_not_just_the_friendly_one(self):
        """Open Anyway clears one of them and NOT the other. A doc that mentions
        only the first leaves the "damaged" case looking like a corrupt download,
        which is exactly how a recipient concludes the app is broken.

        NC-313: right-click → Open was the fix this used to name. Since macOS 15
        it no longer gets past the block; System Settings → Privacy & Security →
        Open Anyway does, and the app needs macOS 26."""
        section = self.install_section()
        self.assertIn("open anyway", section.lower())
        self.assertIn("privacy & security", section.lower())
        self.assertNotIn("right-click → **open** clears it", section.lower(),
                         "README still names right-click → Open as the fix")
        self.assertIn("damaged", section.lower(),
                      "the 'damaged' dialog — the one right-click does not fix — "
                      "is still only documented where a .app user cannot see it")
        self.assertIn("xattr", section.lower())

    def test_the_dmg_carries_the_answer_beside_the_app(self):
        self.assertIn('Read Me First.txt', self.dmg,
                      "the DMG ships the app with no explanation of the first-launch "
                      "block, and packaging/README.md is not in the DMG")
        # The heredoc that writes the file, bounded by its own terminator
        # (NC-170). A fixed character count is wrong in both directions: too few
        # once the Read Me grows, and enough to reach whatever comes after it in
        # make_dmg.sh.
        #
        # Case-insensitive: the Read Me shouts its headings, so "PYTHON 3.10 OR
        # NEWER" is the real text and a literal match on "Python 3.10" would fail
        # against a file that says exactly the right thing.
        block = up_to(self.dmg, "<<'READMEEOF'", "\nREADMEEOF").lower()
        for needed in ("xattr -dr com.apple.quarantine", "damaged",
                       "open anyway", "privacy & security"):
            with self.subTest(needed=needed):
                self.assertIn(needed, block,
                              f"the DMG's Read Me does not cover {needed!r}")
        # The Read Me inside the DMG is the copy a recipient actually opens, so
        # it is the one that matters most; fixing README.md alone would leave it
        # carrying the old advice.
        self.assert_says_the_app_brings_its_own_python(block, "the DMG's Read Me")
        # NC-330: there is no build without its own Python and no Python for the
        # recipient to install, so the Read Me must not describe either (it used to
        # tell them to fetch 3.10 from python.org and not to use xcode-select).
        self.assertNotIn("without its own python", block,
                         "the Read Me still describes a copy built without its own Python")

    def test_the_dmg_does_not_point_at_a_quit_app(self):
        """There is no 'Quit Network CRM' app since NC-330 (2026-10-17): quitting the
        app stops its engine. A Read Me that sends someone to an app that is not in the
        disk image, or a script that goes looking for one, is wrong in a way nobody
        would notice until they needed it. (The sentence "Quit Network CRM, drag the new
        version into Applications" is about quitting the app and is fine.)"""
        self.assertIsNone(re.search(r'Quit Network CRM\.app|"Quit Network CRM"|QUIT_APP', self.dmg),
                          "make_dmg.sh still copies or names the 'Quit Network CRM' app")

    def test_the_full_disk_access_claim_matches_the_helper_that_exists(self):
        self.assertNotIn("**No Full Disk Access required.**", self.readme,
                         "the README still claims no Full Disk Access is involved, "
                         "while packaging/ ships a helper whose whole job needs it")
        # The distinction that keeps it honest: the APP never holds it.
        self.assertIn("The app itself never gets Full Disk Access", self.readme)
        self.assertIn("opt-in", self.readme)

    def test_the_documented_model_is_the_one_the_code_uses(self):
        import re
        m = re.search(r"`ANTHROPIC_MODEL` \(default `([^`]+)`\)", self.readme)
        self.assertIsNotNone(m, "the model default is no longer documented")
        with open(os.path.join(self.here, "crm", "settings.py"), encoding="utf-8") as fh:
            settings_src = fh.read()
        real = re.search(r'"ANTHROPIC_MODEL":\s*\{[^}]*"default":\s*"([^"]+)"',
                         settings_src)
        self.assertIsNotNone(real)
        self.assertEqual(m.group(1), real.group(1),
                         "README documents a different default model than "
                         "settings.MANAGED_KEYS actually holds")

    def test_it_does_not_pre_authorise_sharing_the_repo(self):
        """NC-169. The README must not say the repo is "safe to fork and share
        publicly — it contains only code and docs".

        The reasoning is the defect, not the phrasing. Personal data does not
        only arrive through the gitignored data directory; it can arrive through
        test fixtures, comments, examples and commit messages, which ARE code
        and docs. A sentence that grants permission on the strength of a
        gitignore rule tells the reader the check is already done.

        Matched on the CLAIM, not one sentence: any "safe to ... share/fork/
        publish" phrasing fails, so rewording it back is not a way around."""
        import re
        low = re.sub(r"\s+", " ", self.readme.lower())
        claim = re.compile(r"safe to [a-z ]{0,20}(share|fork|publish|distribute|"
                           r"open.?source)|nothing (personal|private|sensitive) "
                           r"(is |ever )?(in|reaches) (the |this )?repo")
        m = claim.search(low)
        self.assertIsNone(
            m, "README pre-authorises sharing the repo "
               f"({low[max(0, (m.start() if m else 0) - 60):(m.end() if m else 0) + 60]!r}) — "
               "personal data gets in through fixtures, comments, examples and "
               "commit messages, which are code and docs too. See NC-169.")
        # And it has to say the opposite, not merely go quiet.
        i = self.readme.index("## Privacy & what's shareable")
        section = self.readme[i:self.readme.index("\n---", i)]
        self.assertIn("git log", section,
                      "the privacy section never mentions commit messages, which "
                      "is where a leak cannot be quietly fixed")

    def test_the_source_table_lists_only_sources_that_exist(self):
        i = self.readme.index("| Source | You provide |")
        table = self.readme[i:self.readme.index("\n\n", i)]
        for gone in ("**Telegram**", "**Messenger / Instagram**",
                     "**WhatsApp profile photos**"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, table,
                                 f"the table still offers {gone}, which has no row")

    def test_no_documented_command_calls_an_interpreter_a_mac_lacks(self):
        """NC-135. `python` and `pip` do not exist on a Mac — Apple removed both.
        A quick start that opens with two commands that fail on their first
        line for anyone on a fresh Mac, and only works where a Python version
        manager puts shims on the PATH, is broken.

        Three things the check has to get right, each of which a naive version
        would miss while staying green:

        1. **Fenced blocks are not the only place.** A bullet can say run
           `python crm/app.py` in an inline code span. A rule that only reads
           ```bash fences misses that line, so inline spans are scanned too.
        2. **words[0] is not the command.** `APP_HOST=0.0.0.0
           APP_TOKEN=$(openssl rand -hex 16) python crm/app.py` splits on
           whitespace into a first word of `APP_TOKEN=$(openssl`, so stripping
           leading assignments leaves `rand` as the apparent command and the
           `python` sails through. Every token is checked, not the first.
        3. **The inline rule is "no code span may BEGIN with python or pip".**
           A narrower filter — spans that also carry a script path — lets
           `pip install sentence-transformers numpy` through, because it names
           no `.py` file. Anything a reader could copy out of a code span and
           paste is a command, whatever its arguments look like, so the README
           explains the externally-managed problem in plain words instead of
           putting `pip install` in a span to make an exception for.

        The lookarounds let `.venv/bin/python3`, `python3`, `python.org` and
        `-m pip` past; a bare `python` or `pip` is what fails."""
        import re
        # `-m pip` is the CORRECT form — it runs whichever interpreter was
        # named, so it is excluded rather than flagged.
        BARE = re.compile(r"(?<![\w./-])(?<!-m )(python|pip)(?![\w.-])")
        candidates = []
        for block in re.findall(r"```(?:bash|sh|shell|console)\n(.*?)```",
                                self.readme, re.S):
            for line in block.splitlines():
                if line.strip() and not line.strip().startswith("#"):
                    candidates.append(("shell block", line.strip()))
        self.assertTrue(candidates, "no shell blocks found — the extractor is wrong")
        # Inline spans, e.g. `python crm/app.py` in a bullet.
        # Every span is scanned; a span that BEGINS with python/pip is a command
        # by definition, and the BARE rule below decides the rest.
        inline = 0
        for span in re.findall(r"`([^`\n]+)`", self.readme):
            if " " in span or re.match(r"(python|pip)(?![\w.-])", span):
                candidates.append(("inline span", span.strip()))
                inline += 1
        self.assertTrue(inline, "no inline commands found — the extractor is wrong, "
                                "and the README's quick start has some")
        for where, line in candidates:
            for m in BARE.finditer(line):
                with self.subTest(where=where, line=line):
                    self.fail(
                        f"README's {where} tells the reader to run {m.group(1)!r} "
                        f"({line!r}) — no Mac has had it since Apple removed it, "
                        "so the documented step fails on its own first line")

    def test_the_quick_start_names_where_to_get_a_python_that_works(self):
        """The pointer existed, but only inside the `.app` section — which
        someone handed the SOURCE has no reason to open. The requirement and
        the route to satisfying it have to sit together."""
        i = self.readme.index("## Quick start")
        section = self.readme[i:self.readme.index("### Install as a Mac app")]
        self.assertIn("Python 3.10", section)
        self.assert_points_at_a_python_that_works(section, "the quick start")
        self.assertIn("3.9.6", section,
                      "the quick start does not say what the Mac's own python3 "
                      "actually is, so /usr/bin/python3 looks like a valid answer")

    def test_the_quick_start_and_start_command_agree_on_where_the_venv_is(self):
        """The README tells the reader to create `.venv` inside the repo and
        claims start.command finds it. That claim is only true because of one
        line in start.command; if the path there changes, the README silently
        starts lying."""
        i = self.readme.index("## Quick start")
        section = self.readme[i:self.readme.index("### Install as a Mac app")]
        self.assertIn("python3 -m venv .venv", section)
        with open(os.path.join(self.here, "start.command"), encoding="utf-8") as fh:
            launcher = fh.read()
        # Anchored on the repo-local branch specifically. `'.venv/bin/python'
        # in launcher` passes on the sibling-directory branch above it
        # ("../.venv/bin/python" contains it), so deleting the branch the README
        # actually depends on would leave this test green.
        self.assertRegex(
            launcher, r'(?<!\.\./)\.venv/bin/python"',
            "start.command no longer looks for a repo-local .venv — only for one "
            "in the folder above, which only suits someone who keeps a "
            "virtualenv there — so the README's 'it finds .venv on its own' is "
            "now false for a recipient")

    def test_the_launcher_tries_its_own_venv_before_the_folder_above(self):
        """NC-247. Both branches existed, but the folder above came first, so
        any unrelated .venv one level up beat the one README says to make, and
        the optional features quietly failed. ("The launcher" here is start.command
        and the scripts beside it, which start the app from a source checkout; the
        packaged app's launcher script is gone, NC-330.)"""
        # NC-287: deploy/pull_and_merge.sh looked only one folder up.
        for rel in ("start.command", os.path.join("packaging", "preflight.sh"),
                    os.path.join("deploy", "pull_and_merge.sh")):
            with open(os.path.join(self.here, rel), encoding="utf-8") as fh:
                src = fh.read()
            own = re.search(r'-x "(\$REPO/|\$HERE/)?\.venv/bin/python3?"', src)
            above = re.search(r'-x "(\$REPO/|\$HERE/)?\.\./\.venv/bin/python3?"', src)
            with self.subTest(file=rel):
                self.assertIsNotNone(own, f"{rel} no longer checks its own .venv")
                self.assertIsNotNone(above)
                self.assertLess(own.start(), above.start(),
                                f"{rel} tries the folder above before its own .venv")

    def test_the_developer_copy_of_the_app_tries_its_own_venv_first_too(self):
        """NC-247 again, for the program that replaced the launcher script (NC-330,
        2026-10-17). A developer copy of the app (native/build.sh) runs this checkout's
        engine on a Python it picks from two candidates, and the checkout's own .venv
        must come before one in the folder above, for the same reason as above."""
        launch = _swift_function(_native("Engine.swift"), "static func forThisApp()")
        own = launch.index('repo.appending(path: ".venv/bin/python3")')
        above = launch.index('repo.deletingLastPathComponent().appending(path: ".venv/bin/python3")')
        self.assertLess(own, above, "the folder above is tried before the checkout's own .venv")



# NC-048, TestTheWindowNeverFlashesWhiteBeforeTheAppPaints, was deleted on 2026-10-17
# (NC-330), three tests. A web view paints its own opaque white until the page's first
# paint, so the old window had to be told the app's dark ground, and the two colour
# values (one in Swift, one in the page's tokens.css) had to be kept equal by a test. The
# window is native now: there is no web view to flash white and no second copy of the
# colour to drift (the app's colours are in native/Sources/Theme.swift alone).


class TestTheNativeMenuAgreesWithTheAppsOwnTabs(unittest.TestCase):
    """NC-051. The packaged app's Go menu hardcoded tab titles and bound each to a
    digit it sent to the web page. `static/app/main.js`'s `TABS` array decided what
    those digits actually DID.

    They drifted. The Swift array still started with "Today" after that view was
    deleted (NC-009), so every item was labelled one tab off from where it went:
    "Today" opened People, "People" opened Ask, and "Data" opened nothing. Five
    wrong menu items in the shipped app — and invisible from a browser, because a
    browser has no menu bar, which is why it survived. This was the cross-file gate
    that had to exist.

    NC-330 (2026-10-17): the window is native, and its Go menu is built from the same
    list of sections that draws the sidebar (AppModel.Section), so the two cannot
    drift and the comparison with main.js's TABS is gone. What is left to hold is that
    it stays built that way, that the deleted view stays deleted, and that pasting a
    list of people is still reachable by name without taking a digit."""

    def setUp(self):
        self.app = _native("App.swift")
        self.model = _native("AppModel.swift")
        self.root = _native("RootView.swift")
        menu = self.app[self.app.index('CommandMenu("Go") {'):]
        self.go_menu = menu[:menu.index("\n            }\n")]

    def test_the_go_menu_is_the_same_list_the_sidebar_draws(self):
        self.assertIn("ForEach(Array(AppModel.Section.allCases.enumerated()), id: \\.element)",
                      self.go_menu)
        self.assertIn("Button(section.title)", self.go_menu)
        self.assertIn(".keyboardShortcut(KeyEquivalent(Character(String(index + 1))), modifiers: .command)",
                      self.go_menu)
        self.assertNotRegex(self.go_menu, r'Button\("(People|Ask|Lists|Data|Settings|Today)"\)',
                            "a tab title is typed into the menu by hand again; it will drift")
        self.assertNotIn("for (index, title) in [", self.app)
        self.assertIn("AppModel.Section.allCases", self.root,
                      "the sidebar no longer draws from the list the menu uses")

    def test_a_deleted_view_cannot_linger_in_the_menu(self):
        """Named explicitly, because this is the one that shipped. Today was
        deleted in NC-009 and the menu kept offering it."""
        cases = re.search(r"enum Section: [^{]*\{\s*case ([^\n]+)\n", self.model).group(1)
        self.assertNotIn("today", cases.lower(), "a deleted view is a section of the app again")
        self.assertNotIn('"Today"', self.go_menu)

    def test_intake_is_reachable_by_name_and_takes_no_digit(self):
        """Pasting a list of people is not one of the tabs, so it must not be one of
        the digit-bound sections (that would re-point a key), but it must still be
        reachable, or the surface is keyboard-only. It is a File menu item."""
        self.assertIn('Button("Paste a List of People…") { app.setupFlow = .paste }', self.app)
        cases = re.search(r"enum Section: [^{]*\{\s*case ([^\n]+)\n", self.model).group(1)
        self.assertNotIn("paste", cases.lower())
        self.assertNotIn("intake", cases.lower())


class TestTheAppNeverHuntsForAPythonOnTheMac(unittest.TestCase):
    """NC-021, NC-056: state the requirement and fail honestly, rather than embedding a
    CPython nobody could verify. That made the failure path the deliverable, so it had to
    be exercised: the launcher hunted for a Python 3.10+ on the Mac, checked each
    candidate with a bash function (is_py310plus), skipped Apple's /usr/bin/python3
    stand-in unless the developer tools were installed (is_apple_shim, have_devtools:
    executing that stand-in raises Apple's own "install the command line tools" dialog,
    unattributed, over an app the user just opened), and showed a dialog naming python.org
    when it found nothing.

    NC-330 (2026-10-17) removed the hunt, so the tests that ran those bash functions went
    with it (is_py310plus: three tests; the missing-Python dialog: three; the shim guard:
    two). The app now starts its engine on the Python inside it, or says in the window that
    this copy has none. What survives of the old rules is that the app must never go
    looking: the one thing in this story that was a real hazard (Apple's stand-in raising
    its dialog) can only come back if some code names that file or searches the Mac."""

    def setUp(self):
        self.swift = _native("Engine.swift")
        self.code = _swift_code(self.swift)

    def test_the_app_never_runs_apples_python_stand_in_or_any_python_it_finds(self):
        for named in ("/usr/bin/python", "/opt/homebrew", "/usr/local/bin", ".pyenv",
                      "xcode-select", "xcrun", "/Library/Frameworks/Python.framework"):
            with self.subTest(named=named):
                self.assertNotIn(named, self.code,
                                 f"the app's program names {named!r}: a Python on this Mac is "
                                 "being looked for again, and Apple's stand-in raises its own "
                                 "dialog when run on a Mac without the developer tools")

    def test_a_copy_with_no_python_says_so_in_the_window_and_what_to_do(self):
        """Was: the dialog had to name python.org and 3.10. There is nothing for the
        person to install now, so the sentence is that this copy is incomplete and the
        remedy is to download it again (their people are kept, outside the app)."""
        start = _swift_function(self.swift, "private func startEngine(")
        branch = start[start.index("guard let basePython = launch.python else {"):]
        branch = branch[:branch.index("return\n        }")]
        self.assertIn("has no Python inside it", branch)
        self.assertIn("state = .failed(", branch)
        self.assertIn("Download Network CRM again", branch)
        self.assertIn("Your people are kept", branch)


class TestEverySyncSourceHasItsDestinationFlag(unittest.TestCase):
    """NC-042. The helper keeps three parallel lists — `sources`, the
    `destFlags` table, and the positional `overrides` array the run loop indexes
    into — and nothing but a runtime `precondition` held them in step.

    That precondition works: a source added without its `--<name>-dest` flag
    traps the binary at global init with exit 133 and not one line of output,
    which is about as loud as a failure gets. But it only fires when the binary
    RUNS. A source added and shipped without its flag would take the helper out
    entirely on the user's Mac — every launchd run trapping before it copies
    anything — and the suite would be green the whole way. The helper's own
    comment predicts this exact mistake and calls the precondition "the
    backstop that actually catches it"; this is the same check moved early
    enough to be free.
    """

    SWIFT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "packaging", "src", "imessage_sync.swift")

    def _src(self):
        with open(self.SWIFT, encoding="utf-8") as fh:
            return fh.read()

    def _labels(self):
        src = self._src()
        block = src[src.index("let sources: [Source] = ["):]
        block = block[:block.index("\n]")]
        # Comments stripped first — the file explains its sources at length, and
        # a label quoted in prose is not a source. A gate that matches the code's
        # own commentary is a recurring trap in this repo.
        code = "\n".join(ln for ln in block.splitlines()
                         if not ln.lstrip().startswith("//"))
        return re.findall(r'label:\s*"([^"]+)"', code)

    def test_the_source_list_is_what_the_test_thinks_it_is(self):
        labels = self._labels()
        self.assertEqual(labels, ["iMessage", "WhatsApp", "Contacts", "Calendar"],
                         "the helper's sources changed; every list below must "
                         "move with it")

    def test_overrides_has_exactly_one_slot_per_source(self):
        """The array the run loop indexes by source position. Short and the last
        source traps; long and the precondition trips."""
        src = self._src()
        # Line-based, not bracket-matching: matching brackets truncates at the
        # wrong "]", either the one inside the TYPE (`[URL?]`) or the one inside
        # an ENTRY (`destFlags["--whatsapp-dest"]`). A Swift literal is full of
        # brackets; the only reliable delimiter is the closing line.
        lines = src.splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("let overrides: [URL?] = ["))
        end = next(i for i in range(start + 1, len(lines))
                   if lines[i].rstrip() == "]")
        slots = [ln.strip().rstrip(",") for ln in lines[start + 1:end]
                 if ln.strip() and not ln.lstrip().startswith("//")]
        self.assertEqual(len(slots), len(self._labels()),
                         f"{len(self._labels())} sources but {len(slots)} "
                         f"destination overrides: {slots}")

    def test_every_source_after_the_first_has_a_named_dest_flag(self):
        """The first source takes the positional argument; the rest need a
        --<name>-dest flag, or there is no way to redirect them and the
        overrides slot above can never be anything but nil."""
        src = self._src()
        lines = src.splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("var destFlags: [String: URL?] = ["))
        end = next(i for i in range(start + 1, len(lines))
                   if lines[i].rstrip() == "]")
        flags = set(re.findall(r'"(--[a-z-]+)"',
                               "\n".join(lines[start + 1:end])))
        for label in self._labels()[1:]:
            want = f"--{label.lower()}-dest"
            self.assertIn(want, flags,
                          f"source {label!r} has no {want} flag — the binary "
                          f"traps at startup with no output at all")

    def test_the_runtime_precondition_is_still_there(self):
        """Belt and braces. This test reads three lists and could itself drift;
        the precondition is what catches it if it does."""
        self.assertIn("precondition(overrides.count == sources.count", self._src())


class TestOneBlockedSourceCostsOnlyItself(unittest.TestCase):
    """NC-042. A source macOS blocks on must not take the others with it.

    Three steps give this design its shape.

    First the helper can hang: macOS BLOCKS rather than refuses a read of a
    folder it has never asked permission for, so the process stays alive at 0%
    CPU, waiting on a dialog nobody sees. A whole-run watchdog fixes the hang and
    not the cost — the sources run in order and the watchdog kills the run, so
    with WhatsApp second of four, Contacts and Calendar are never reached. Every
    run would log iMessage, stall, exit. Four sources, none syncing, and the
    app's Data page showing the last good report as though it were current.

    Then the obvious fix, re-exec per source and kill the child on timeout, is
    rejected by `test_the_helper_still_has_no_network_and_no_subprocesses`.
    Correctly: this binary holds Full Disk Access and its audit argument is that
    you can read the whole thing and see it spawns nothing. `Process(` would
    spend that argument to buy what a thread gives free.

    So: one thread per source, abandoned on timeout. With that, all four sources
    report an outcome and the whole run stays short.

    The prompts come back after every rebuild, not once: a binary that is ad-hoc
    signed gets a new code hash on every compile, and TCC keys app-data grants on
    that hash. Every rebuild orphans WhatsApp's and Calendar's grants and the next
    read blocks on a fresh dialog.
    """

    SWIFT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "packaging", "src", "imessage_sync.swift")

    def _src(self):
        with open(self.SWIFT, encoding="utf-8") as fh:
            return fh.read()

    def _fn(self, name):
        src = self._src()
        fn = src[src.index(f"func {name}("):]
        return fn[:fn.index("\n}\n")]

    def test_the_run_loop_goes_through_the_abandonable_wrapper(self):
        """The loop must not call sync() directly again. That is the whole
        regression: one in-process call and a blocked source is back to taking
        every source after it with it."""
        src = self._src()
        body = src[src.index("for (n, s) in sources.enumerated() {"):]
        body = body[:body.index("\n}") + 2]
        self.assertIn("syncAbandonable(", body,
                      f"the run loop no longer isolates each source: {body!r}")
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("//"))
        self.assertNotIn(" sync(s,", code,
                         "the run loop calls sync() on its own thread, so one "
                         "blocked source stalls the ones after it again")

    def test_the_per_source_ceiling_is_below_the_whole_run_watchdog(self):
        """The invariant that makes isolation worth anything. If the per-source
        timeout were the larger of the two, the watchdog would always fire first
        and kill the run — the exact behaviour isolation exists to replace, with
        extra machinery on top and no change in outcome."""
        src = self._src()

        def default_of(name):
            frag = src[src.index(f"let {name}"):]
            frag = frag[:frag.index("\n\n")]
            return float(re.search(r"\?\?\s*([0-9.]+)", frag).group(1))

        per_source = default_of("perSourceSeconds")
        whole_run = default_of("watchdogSeconds")
        self.assertLess(
            per_source * 2, whole_run,
            f"per-source ceiling {per_source}s vs whole-run watchdog "
            f"{whole_run}s: at least two sources must be able to time out "
            f"before the backstop fires, or the backstop IS the behaviour")

    def test_the_wait_is_bounded_and_a_timeout_means_blocked(self):
        """A wait with no deadline is the original hang wearing a new function
        name, and it would read as correct at a glance."""
        fn = self._fn("syncAbandonable")
        self.assertIn(".now() + timeout", fn,
                      f"the wait is not bounded by the timeout: {fn!r}")
        self.assertIn("== .timedOut", fn)
        self.assertIn("return .blocked", fn)

    def test_the_result_is_only_read_after_the_worker_signals(self):
        """The abandoned thread keeps running. Reading its slot on the timeout
        path would be a data race whose symptom is an outcome from the previous
        source appearing under this one's name."""
        fn = self._fn("syncAbandonable")
        # Just the if-block's body. Slicing to the end of the function would
        # sweep up the SUCCESS path's `return slot.value`, which is exactly where
        # that read belongs, and fail against correct code.
        branch = fn[fn.index("== .timedOut"):]
        branch = branch[:branch.index("\n    }")]
        self.assertIn("return .blocked", branch,
                      "the timeout path does not return early, so it falls "
                      "through to the worker's slot")
        self.assertNotIn("slot.value", branch,
                         "the timeout path reads the worker's slot while the "
                         "worker may still be writing it")
        # And the success path must read it, or the outcome is discarded.
        self.assertIn("return slot.value", fn)

    def test_no_subprocess_crept_in_with_the_isolation(self):
        """The reason this design is threads. Duplicated from the header audit
        test on purpose: that one guards the whole file, this one records that
        the per-source isolation specifically was written to stay inside it."""
        code = re.sub(r"//[^\n]*", "", self._src())
        for forbidden in ("Process(", "NSTask", "posix_spawn", "execv"):
            self.assertNotIn(forbidden, code,
                             f"{forbidden} in a binary with Full Disk Access — "
                             f"a thread does this job without it")

    def test_every_shared_cursor_access_is_locked(self):
        """`cursor` is now touched from worker threads as well as the main one.
        An abandoned worker is blocked before its write, so this guards a race
        that should not be reachable — but the failure it would cause is a
        corrupt write to the file whose whole job is telling the app what is
        true, which is the wrong thing to be relaxed about."""
        src = self._src()
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("//"))
        self.assertIn("let cursorLock = NSLock()", code)
        lines = code.splitlines()
        for i, ln in enumerate(lines):
            if "cursor[s.label]" not in ln:
                continue
            window = "\n".join(lines[max(0, i - 2):i + 3])
            self.assertIn("cursorLock", window,
                          f"unlocked shared cursor access: {ln.strip()!r}")


class TestTheBundledInterpreterIsTheOneThatRuns(unittest.TestCase):
    """NC-021. The app carries its own CPython so a recipient needs nothing
    installed: it carries its own runtime, as many Mac apps do.

    A bundled interpreter is only useful if the app reaches it. If it is the
    LAST candidate tried, and the first one found is kept, then on any Mac that already
    has a Python the bundled copy is unreachable BY CONSTRUCTION, however well it was
    built. That ordering is what these tests guarded when the launcher script tried a
    list of candidates.

    NC-330 (2026-10-17): there is no list now. The app's program (EngineLaunch in
    native/Sources/Engine.swift) takes the bundled Python for a bundle that carries its
    own engine code, and nothing else; the checkout's .venv is only for a developer copy,
    which has no engine code inside it. The ordering test became a test that nothing but
    those two exist. Which Python a private environment belongs to (its version and
    whether it came from the bundle) is crm/python_env.py's, and
    tests/test_python_env.py holds it (test_one_built_by_another_python_is_replaced,
    test_the_bundled_python_counts_as_a_different_one), so the bash-extracting tests of
    that are gone."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    APPIFY = os.path.join(ROOT, "packaging", "appify.sh")

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _for_this_app(self):
        return _swift_function(_native("Engine.swift"), "static func forThisApp()")

    def test_a_bundle_with_its_own_engine_code_runs_on_the_bundled_python(self):
        fn = self._for_this_app()
        embedded = fn.index('if fm.fileExists(atPath: embedded.appending(path: "crm/app.py").path) {')
        bundled = fn.index('resources.appending(path: "python/bin/python3")')
        developer = fn.index('else if let raw = try? String(contentsOf: resources.appending(path: "repo_path.txt")')
        self.assertLess(embedded, bundled,
                        "the bundled interpreter is chosen for something other than a bundle "
                        "that carries its own engine code")
        self.assertLess(bundled, developer,
                        "the developer copy's checkout is consulted before the bundled "
                        "interpreter, which would make a real bundle run on a .venv")
        taken = fn[bundled:developer]
        self.assertIn("python = bundled", taken)
        self.assertIn("setsUp = true", taken,
                      "the private environment for the optional parts is no longer made "
                      "from the bundled Python")

    def test_the_engine_is_started_on_that_python_and_never_a_found_one(self):
        """The Python that runs `crm/app.py` is the one the setup returned, which is the
        bundled one's environment, or the bundled one itself, and nothing else."""
        start = _swift_function(_native("Engine.swift"), "private func startEngine(")
        self.assertIn("guard let basePython = launch.python else {", start)
        self.assertIn("var python = basePython", start)
        self.assertIn("p.executableURL = python", start)

    def test_the_engine_does_not_inherit_a_python_environment(self):
        """An inherited PYTHONHOME points the bundled interpreter at another
        installation's standard library, which fails in a way that reads as a corrupt
        bundle; PYTHONPATH, VIRTUAL_ENV and a user's site-packages do the same kind of
        harm more quietly. Was `unset PYTHONHOME PYTHONPATH` in the launcher."""
        env = _swift_function(_native("Engine.swift"), "func environment(code: URL)")
        self.assertIn('key.hasPrefix("PYTHON")', env)
        self.assertIn('key == "VIRTUAL_ENV"', env)
        self.assertRegex(env, r'env\["PYTHONNOUSERSITE"\]\s*=\s*"1"')

    def test_the_build_still_makes_no_network_fetch(self):
        """appify.sh's header has promised an offline build since it was
        written, and adding a 25MB interpreter is the obvious way to break that
        promise by accident."""
        src = self._read(self.APPIFY)
        for fetcher in ("curl ", "wget ", "git clone", "pip install",
                        "nscurl", "ftp "):
            with self.subTest(fetcher=fetcher.strip()):
                self.assertNotIn(
                    fetcher, src,
                    f"appify.sh now runs {fetcher.strip()!r} — the build is "
                    "supposed to be entirely offline, and the interpreter is "
                    "meant to come from packaging/.cache/")

    def test_the_interpreter_is_never_signed_with_the_hardened_runtime(self):
        """Under the Hardened Runtime, Library Validation rejects sibling
        ad-hoc-signed dylibs: they carry no Team ID to match against, and
        re-signing them ad-hoc cannot produce one without a paid Developer ID.
        Ad-hoc with no hardened runtime loads fine; hardened plus ad-hoc does
        not. This is a one-flag mistake with a confusing failure."""
        # Comments stripped first. The function carries a deliberate "NEVER add
        # --options runtime here" warning, so a whole-file search matches the
        # warning and fails on a correct build. The same trap is why NC-056's
        # dead-end advice could survive a doc fix: a search matches prose that
        # states a rule as readily as one that breaks it.
        code = "\n".join(
            ln for ln in self._read(self.APPIFY).splitlines()
            if not ln.lstrip().startswith("#"))
        for flag in ("--options runtime", "--options=runtime",
                     "--options runtime,library"):
            with self.subTest(flag=flag):
                self.assertNotIn(
                    flag, code,
                    "appify.sh enables the hardened runtime, which will stop "
                    "the bundled interpreter's extension modules from loading "
                    "at all — ad-hoc siblings have no Team ID to validate")

    def test_intel_macs_are_refused_rather_than_shipped_something_broken(self):
        """Deliberately out of scope for now. What matters is
        that an x86_64 build does not silently ship an arm64 interpreter, which
        would fail as the very "no Python" error the embed exists to remove."""
        src = self._read(self.APPIFY)
        fn = src[src.index("embed_python() {"):src.index("write_python_licenses() {")]
        self.assertIn("arm64", fn)
        self.assertIn("uname -m", fn,
                      "embed_python no longer checks the host architecture")

    def test_the_licence_file_names_every_bundled_component(self):
        """The repo is MIT licensed and the bundle redistributes third-party
        code. The install_only tarball carries CPython's own LICENSE.txt and
        pip's vendored ones, but NOT the licences of the statically linked C
        libraries."""
        src = self._read(self.APPIFY)
        fn = src[src.index("write_python_licenses() {"):]
        for lib in ("OpenSSL", "SQLite", "zlib", "libexpat", "libffi",
                    "liblzma", "bzip2", "ncurses", "libmpdec"):
            with self.subTest(lib=lib):
                self.assertIn(lib, fn,
                              f"{lib} ships in the bundle but is not listed in "
                              "THIRD-PARTY-LICENSES.txt")


# Where the venv check remembers the build it last passed for. Next to the bundle,
# so it is gitignored with it and goes when dist/ is cleaned; not in the run's
# temp folder, which is deleted after every run.
VENV_STAMP = os.path.join(REPO, "packaging", "dist", ".venv-check-passed")


def bundle_build_id(bundle):
    """The id appify.sh stamps into every build, or '' when there is none."""
    try:
        with open(os.path.join(bundle, "Contents", "Resources",
                               "build_id.txt")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def venv_check_passed_for(stamp, build):
    """True only when this exact build already passed. A bundle with no id
    never counts, so an old or hand-made bundle is always checked."""
    if not build:
        return False
    try:
        with open(stamp) as fh:
            return fh.read().strip() == build
    except OSError:
        return False


def remember_venv_check(stamp, build):
    if build:
        with open(stamp, "w") as fh:
            fh.write(build + "\n")


class TestTheVenvCheckRunsOncePerBuild(unittest.TestCase):
    """NC-226. The skip has to be exact: a new build must always be checked,
    or a broken trim ships while the suite reports a pass from an older one."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.stamp = os.path.join(self.d, "stamp")
        self.bundle = os.path.join(self.d, "App.app")
        os.makedirs(os.path.join(self.bundle, "Contents", "Resources"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _stamp_build(self, build):
        with open(os.path.join(self.bundle, "Contents", "Resources",
                               "build_id.txt"), "w") as fh:
            fh.write(build + "\n")

    def test_the_first_run_on_a_build_is_not_skipped(self):
        self._stamp_build("20250101T120000Z-abc1234")
        self.assertFalse(venv_check_passed_for(
            self.stamp, bundle_build_id(self.bundle)))

    def test_a_second_run_on_the_same_build_is_skipped(self):
        self._stamp_build("20250101T120000Z-abc1234")
        build = bundle_build_id(self.bundle)
        remember_venv_check(self.stamp, build)
        self.assertTrue(venv_check_passed_for(self.stamp, build))

    def test_a_new_build_is_checked_again(self):
        remember_venv_check(self.stamp, "20250101T120000Z-abc1234")
        self._stamp_build("20250101T130000Z-abc1234")
        self.assertFalse(venv_check_passed_for(
            self.stamp, bundle_build_id(self.bundle)))

    def test_a_bundle_without_an_id_is_always_checked(self):
        self.assertEqual(bundle_build_id(self.bundle), "")
        remember_venv_check(self.stamp, "")
        self.assertFalse(os.path.exists(self.stamp))
        self.assertFalse(venv_check_passed_for(self.stamp, ""))
        # An empty stamp (a write cut short) must not match an empty id either.
        open(self.stamp, "w").close()
        self.assertFalse(venv_check_passed_for(self.stamp, ""))

    def test_the_stamp_lives_in_ignored_build_output(self):
        # Not the run's temp folder (deleted every run, so it would never skip)
        # and nowhere git would pick up.
        self.assertEqual(os.path.dirname(VENV_STAMP),
                         os.path.join(REPO, "packaging", "dist"))
        with open(os.path.join(REPO, "packaging", ".gitignore")) as fh:
            self.assertIn("dist/", fh.read().split())


class TestTheShippedBundleActuallyCarriesAWorkingPython(unittest.TestCase):
    """The build's own claims, checked against the artifact it produced.

    Everything above reads source. This class refuses to, because reading source
    cannot notice an embedded interpreter that never runs. Skipped when no
    bundle has been built, so a fresh checkout's suite still passes. (Since NC-330 an
    embedded bundle always carries its Python; tests/test_dev_parity.py fails one that
    does not. A developer copy has none and is skipped here.)
    """

    BUNDLE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "packaging", "dist", "Network CRM.app")
    PY = os.path.join(BUNDLE, "Contents", "Resources", "python", "bin", "python3")

    def setUp(self):
        if not os.path.exists(self.BUNDLE):
            self.skipTest("no built bundle (run packaging/appify.sh --embed)")
        if not os.path.exists(self.PY):
            self.skipTest("bundle has no embedded interpreter (a developer build)")

    # NC-310. Running the bundle's own interpreter imports its standard library,
    # and without this every check below wrote __pycache__ folders into the
    # built bundle, each naming the folder it was built in.
    ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def _run(self, code):
        import subprocess
        out = subprocess.run([self.PY, "-c", code], capture_output=True,
                             text=True, timeout=60, env=self.ENV)
        self.assertEqual(out.returncode, 0,
                         f"the bundled interpreter failed: {out.stderr[:400]}")
        return out.stdout.strip()

    def test_it_runs_and_is_new_enough_for_this_app(self):
        v = self._run("import sys; print('%d.%d' % sys.version_info[:2])")
        major, minor = (int(x) for x in v.split("."))
        self.assertGreaterEqual(
            (major, minor), (3, 10),
            "the bundled interpreter is older than the app's own floor, so "
            "crm/app.py (MIN_PYTHON) refuses to start on it and the app opens to an "
            "engine that stops — shipping the interpreter for nothing")

    def test_it_has_the_modules_the_app_cannot_start_without(self):
        # sqlite3 and ssl are the two that come from C libraries and so are the
        # two a trim can plausibly break. The rest are cheap to confirm.
        got = self._run(
            "import sqlite3, ssl, json, hashlib, zlib, lzma, bz2, ctypes, "
            "sqlite3 as s, ssl as t; "
            "print(s.sqlite_version, t.OPENSSL_VERSION.split()[1])")
        self.assertRegex(got, r"^\d+\.\d+", f"unexpected probe output: {got!r}")

    def test_it_can_build_the_venv_the_app_depends_on(self):
        """(Was test_it_can_build_the_venv_the_launcher_depends_on, NC-226's name.)
        crm/python_env.py (the launcher's ensure_embedded_venv before NC-330) calls
        `-m venv` and then pip. If the trim broke either, the app degrades to the
        stdlib core silently — the optional features just never work, with nothing
        pointing at why.

        It writes about 13 MB (871 files) to the disk, and its answer can only
        change when the bundle does, so it runs once per build (NC-226)."""
        import subprocess
        import tempfile
        build = bundle_build_id(self.BUNDLE)
        if venv_check_passed_for(VENV_STAMP, build):
            self.skipTest(f"the venv check already passed for build {build}")
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "v")
            mk = subprocess.run([self.PY, "-m", "venv", v],
                                capture_output=True, text=True, timeout=180, env=self.ENV)
            self.assertEqual(mk.returncode, 0,
                             f"`-m venv` failed: {mk.stderr[:400]}")
            pip = subprocess.run([os.path.join(v, "bin", "python3"),
                                  "-m", "pip", "--version"],
                                 capture_output=True, text=True, timeout=120, env=self.ENV)
            self.assertEqual(pip.returncode, 0,
                             f"the venv has no working pip: {pip.stderr[:400]}")
        remember_venv_check(VENV_STAMP, build)

    def test_the_gui_toolkit_really_was_trimmed(self):
        """tkinter is the single biggest thing removed and this app never uses
        it — it serves a web UI. If it comes back the bundle grows ~12MB for
        nothing, and the licence file would then also be wrong (Tcl/Tk carries
        its own licence, which is not listed)."""
        import subprocess
        out = subprocess.run([self.PY, "-c", "import tkinter"],
                             capture_output=True, text=True, timeout=60, env=self.ENV)
        self.assertNotEqual(out.returncode, 0,
                            "tkinter is present in the shipped interpreter, so "
                            "the trim did not happen and THIRD-PARTY-LICENSES "
                            "does not mention Tcl/Tk")

    def test_no_component_in_the_bundle_is_copyleft(self):
        """The one licensing question that could actually block distribution.
        GNU readline is GPL-3; libedit is BSD-3. python-build-standalone links
        libedit on macOS, and this asserts it rather than trusting it — an
        MIT-licensed app shipping GPL-3 in its bundle is a real problem, and it
        would be invisible until someone looked."""
        backend = self._run(
            "import readline; print(getattr(readline, 'backend', '') or "
            "getattr(readline, '_READLINE_LIBRARY_VERSION', ''))")
        self.assertNotIn("GNU", backend.upper().replace("GNU READLINE", "GNU"),
                         f"readline backend is {backend!r} — if that is GNU "
                         "readline it is GPL-3 and cannot ship inside an "
                         "MIT-licensed bundle without settling the copyleft "
                         "question")
        licences = os.path.join(self.BUNDLE, "Contents", "Resources", "python",
                                "THIRD-PARTY-LICENSES.txt")
        self.assertTrue(os.path.exists(licences),
                        "the bundle ships third-party code with no licence file")
        with open(licences, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("REVIEW BEFORE SHIPPING", text,
                         "the generated licence file flagged a component for "
                         "review — read it before distributing this bundle")
        self.assertIn("libedit", text,
                      "the licence file does not record which readline "
                      "implementation shipped")


class TestTheGrantRouteIsSpelledOutWhereTheUserIs(unittest.TestCase):
    """NC-058. The sync helper never appears in the Full Disk Access list by
    itself, so the ONLY way to grant it is to press '+' in System Settings and
    browse to the binary. That is how macOS behaves: the toggle never shows up
    in the list by itself, but opening the helper in Finder from the app's grant
    button works.

    Both surfaces explain that, and without this gate nothing protects the
    explanation. A refactor could delete the '+' step and every test would still
    pass, leaving a recipient told to grant a permission to a program that is
    not in the list. Since the row does not
    appear on its own, this copy is not a nicety; it is the whole mechanism.

    Asserting the RULE (a '+' instruction and a path, on each surface) rather
    than the sentences, so rewording stays free.

    NC-330 (2026-10-17): the app's own window is native, so "the app itself" is now
    its Full Disk Access sheet (native/Sources/FullDiskAccessSheet.swift); the web
    page's explainer (static/app/fda.js) is what a source checkout shows in a browser.
    Both are held to the rule.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_the_app_itself_says_to_press_plus_and_where_to_browse(self):
        src = self._read("static", "app", "fda.js")
        # The <ol> itself, to its closing tag (NC-170). A character window is
        # wrong in both directions at once: `src-steps` first occurs inside the
        # COMMENT above the element, so much of the window is prose, and it can
        # stop short of the element's end — checking less than it says it does.
        steps = up_to(src, '<ol class="src-steps">', "</ol>")
        self.assertRegex(
            steps, r"<b>\+</b>|'\+'|\"\+\"",
            "the in-app instructions no longer tell the user to press '+'. The "
            "helper never appears in the Full Disk Access list on its own, so "
            "without that step there is no route to granting it at all")
        self.assertIn("${path}", steps,
                      "the in-app instructions no longer show WHICH file to "
                      "browse to, so '+' leads to a file dialog and a guess")
        # The two buttons that make the step doable rather than merely stated.
        self.assertIn("Show the helper in Finder", src)
        self.assertIn("Copy path", src)

    def test_the_apps_own_window_says_the_same_thing(self):
        """The sheet the app's window opens. Moved here 2026-10-17 (NC-330): this is
        the surface a person using the packaged app sees, and the page above is not."""
        sheet = _native("FullDiskAccessSheet.swift")
        grant = sheet[sheet.index("Click **+**"):]
        grant = grant[:grant.index("Make sure its toggle is")]
        self.assertIn("Click **+**", grant,
                      "the window no longer tells the user to press '+'. The helper never "
                      "appears in the Full Disk Access list on its own")
        self.assertIn("Text(path)", grant,
                      "the window no longer shows WHICH file to browse to")
        self.assertIn('Button("Copy path")', grant)
        self.assertIn("Show the helper in Finder", sheet)
        self.assertIn("not to Terminal", sheet,
                      "the window no longer warns against granting the permission to "
                      "Terminal instead of the helper")

    def test_the_installer_says_the_same_thing_in_a_terminal(self):
        """A recipient who runs the installer directly never sees the app's
        panel, so the instruction has to exist in both places."""
        src = self._read("packaging", "install_imessage_sync.sh")
        block = src[src.index("Full Disk Access has to be granted by hand"):]
        block = block[:2000]
        self.assertIn("'+'", block,
                      "the installer no longer names the '+' button")
        self.assertIn("$BIN_DIR", block,
                      "the installer no longer prints the folder to browse to")
        self.assertRegex(
            block, r"never prompted|by hand",
            "the installer no longer says the permission is never prompted "
            "for, which is the fact that makes the manual route necessary")

    def test_both_surfaces_scope_the_grant_to_the_helper_alone(self):
        """The permission is deliberately narrow — invariant 6 says the grant goes
        to the helper binary and never to the app itself. Granting it to
        Terminal or Network CRM.app instead adds no simplicity — it is the same
        one grant — while quietly widening the blast radius of the whole
        design."""
        for where, parts in (("the app", ("static", "app", "fda.js")),
                             ("the installer",
                              ("packaging", "install_imessage_sync.sh"))):
            with self.subTest(surface=where):
                low = self._read(*parts).lower()
                self.assertIn("not to terminal", low.replace("—", " "),
                              f"{where} no longer warns against granting the "
                              "permission to Terminal instead of the helper")


class TestItRefusesToStartOnAPythonItCannotWorkOn(unittest.TestCase):
    """NC-136. Apple ships Python 3.9.6 and calls it `python3`.

    On it, `crm/app.py` imports fine, the server boots and the whole interface
    loads — and then Apple Contacts, iMessage and WhatsApp import every raise
    `TypeError: unsupported operand type(s) for |` the moment they are imported,
    from `int | None` annotations evaluated at runtime. Those three modules fail
    under /usr/bin/python3 and db.py, config.py and enrich_facts.py do not,
    because those three carry `from __future__ import annotations`, which turns
    annotations into strings nothing ever evaluates.

    So the pre-NC-136 failure was the worst shape available: an app that looks
    like it works and silently cannot do the thing it exists for. It now refuses
    to start.
    """

    APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "crm", "app.py")

    def setUp(self):
        with open(self.APP, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_it_exits_instead_of_serving(self):
        """The requirement, made interpreter-independent.

        Running the real /usr/bin/python3 would only prove this while that stays
        3.9.6 — a future macOS could ship something newer and quietly turn this
        into a test of nothing. Faking `sys.version_info` to a plain tuple
        exercises the same branch on any interpreter, because the gate only ever
        compares it and slices it.
        """
        import subprocess
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.version_info = (3, 9, 6); import app"],
            cwd=os.path.dirname(self.APP), env=env,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1,
                         "app.py did not refuse to start on a 3.9 interpreter; "
                         f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        out = proc.stderr.lower()
        # Both halves of "which Python did it find": the version AND the path.
        # Asserting the version alone would pass while the opening sentence
        # dropped it, because the xcode-select paragraph names the same number
        # further down — and the path is the part that tells someone with
        # several Pythons installed WHICH one just ran.
        self.assertIn("3.9.6", out, "the message does not say which Python it found")
        # NC-295. Started as `../.venv/bin/python3`, sys.executable keeps the `..`
        # while the message prints the tidied path; either spelling is the name.
        spellings = {sys.executable, os.path.abspath(sys.executable),
                     os.path.realpath(sys.executable)}
        self.assertTrue(any(p.lower() in out for p in spellings),
                        "the message does not name the interpreter that ran, so "
                        "someone with several Pythons cannot tell which one this is")
        self.assertIn("3.10", out, "the message does not say which Python is needed")
        self.assertIn("python.org", out, "the message offers no route to a newer one")
        self.assertIn("xcode-select", out,
                      "the message does not warn off Apple's developer tools, "
                      "which install this same 3.9.6")
        self.assertRegex(out, r"do not use|don't use|not a fix",
                         "xcode-select is named with no negation, so it reads "
                         "as the recommended fix")

    def test_the_gate_runs_before_anything_that_could_fail_first(self):
        """The whole value of the check is that it happens first. An import
        added above it that itself blows up on 3.9 turns a clear sentence back
        into a traceback, and no behavioural test would notice, because the
        faked-version test above would still see returncode 1."""
        import re
        gate = self.src.index("if sys.version_info < MIN_PYTHON:")
        before = self.src[:gate]
        imports = re.findall(r"^\s*(?:import|from)\s+(\S+)", before, re.M)
        self.assertEqual(
            imports, ["sys"],
            f"app.py imports {imports} before the version gate — only `sys` is "
            "safe there; anything else can fail on the interpreter the gate "
            "exists to reject, and the user gets a traceback instead")

    def test_every_surface_agrees_on_the_minimum(self):
        """The requirement is stated in the code that enforces it and in the README
        (it was four places before NC-330: the launcher's own check and the disk image's
        Read Me are gone). They can drift (the README can say 3.10 while nothing
        enforces it), so they are checked against each other rather than against a
        literal in one file."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertIn("MIN_PYTHON = (3, 10)", self.src)
        # The README is checked on what it says, and it is the one document left that
        # states the minimum: since NC-330 (2026-10-17) the app brings its own Python, so
        # the launcher's own version check (which had to be compared, not read, because
        # lowering it to (3, 9) left every "3.10+" sentence intact) is gone, and the Read
        # Me in the disk image no longer asks the recipient for one. The Python the app
        # carries is held to the floor by test_it_runs_and_is_new_enough_for_this_app.
        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertRegex(
            text, r"3\s*,\s*10|3\.10",
            "README.md no longer states the 3.10 minimum that crm/app.py enforces "
            "for running from source")

    def test_the_launcher_window_does_not_call_the_refusal_a_crash(self):
        """start.command (the source checkout's launcher; the packaged app's is gone
        since NC-330) keeps its window open on a non-zero exit so the message can be
        read — and used to head it "The app exited unexpectedly", which reframes a
        deliberate, explained refusal as a bug and sends the reader looking for the
        wrong thing."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "start.command"), encoding="utf-8") as fh:
            launcher = fh.read()
        self.assertNotIn("exited unexpectedly", launcher)
        self.assertIn("The message above explains why", launcher,
                      "start.command no longer points at the message, so a "
                      "refusal scrolls past as a bare exit code")


class TestTheDocsAgreeOnWhoCarriesThePython(unittest.TestCase):
    """NC-137. README.md promised the `.app` "needs nothing installed". True of
    a build made with the tarball cached and of no other: `appify.sh` bundles a CPython
    only if an `install_only` aarch64 tarball is already sitting in
    `packaging/.cache/`, which is gitignored and which nothing in the repo
    fetches — no curl, no wget, no pin, no checksum anywhere in the script.

    This is not silent: appify.sh prints four lines naming the directory, saying
    it makes no network fetch by design, and linking the upstream releases page.
    The defect was five documents' promises, not the build's behaviour.

    A document that describes the state between the removal of `--with-python`
    (as an interpreter that never ran) and NC-021 (when it came back, reordered
    so it runs first) is a whole revision out of date. A document such as
    packaging/.gitignore that explains the ignore rule by describing a fetch and
    a checksum that do not exist lets a fresh clone produce a smaller bundle than
    the README promises, with nobody wrong on purpose.

    NC-330 (2026-10-17): a build without the tarball no longer exists (appify.sh stops
    and says where to put it), so what was a condition on the promise became a
    precondition of the build, and the launcher script that told the story four times
    is gone from the documents checked here.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DOCS = ("README.md", "packaging/README.md",
            "packaging/.gitignore", "packaging/appify.sh")

    def read(self, rel):
        with open(os.path.join(self.ROOT, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def test_the_build_really_does_not_fetch_the_interpreter(self):
        """The premise every doc below is checked against. If someone ever adds
        the download, these docs become wrong in the other direction — so this
        asserts the premise rather than assuming it."""
        import re
        src = self.read("packaging/appify.sh")
        for fetcher in (r"\bcurl\b", r"\bwget\b", r"\bshasum\b", r"\bsha256sum\b"):
            with self.subTest(fetcher=fetcher):
                self.assertIsNone(
                    re.search(fetcher, src),
                    f"appify.sh now contains {fetcher} — if it fetches or verifies "
                    "the interpreter, the docs saying you download it by hand "
                    "(README.md, packaging/README.md, packaging/.gitignore) "
                    "are the ones that are now wrong")

    # What tells the removal of the embedded Python, near the words that name
    # THIS subject (the bundled interpreter, or the flag that shipped it).
    REMOVAL = re.compile(r"\bremov(?:ed|al)\b|went away|dropped|deleted")
    SUBJECT = re.compile(r"--with-python|67 ?mb|embedded python|embedded cpython|"
                         r"embedded interpreter|bundled (?:cpython|interpreter|python)")
    CORRECTION = re.compile(r"nc-021|came back|brought back|it ships today|"
                            r"candidate #1|candidate 1")
    WINDOW = 1200

    def tellings_of_the_removal(self, text):
        """(every telling of the removal, those with no correction near it), as
        offsets into the whitespace-normalised, lower-cased text.

        PROXIMITY, not presence anywhere in the file: a file-wide search passes
        while a stale paragraph is reverted, because these files mention NC-021
        somewhere else — launcher.sh.tmpl names it four times. Each telling of
        the removal has to correct itself where it is told. Symmetric: appify.sh's
        header puts the heading "WHY IT CAME BACK (NC-021)" ABOVE the paragraph
        that tells the removal, and embed_python refers to the earlier failure
        inside a function whose whole subject is shipping the interpreter. The
        rule is "the correction sits near the telling", not "after it".
        """
        low = re.sub(r"\s+", " ", text.lower())
        told, uncorrected = [], []
        for m in self.REMOVAL.finditer(low):
            # Only a TELLING of the removal triggers the rule, and the subject
            # words are what narrow it: "removed" alone also describes the
            # Tcl/Tk files trimmed from the bundle, and a note about another
            # removed module can sit three lines from the words "the Python one".
            if not self.SUBJECT.search(low[max(0, m.start() - 150):m.end() + 150]):
                continue
            told.append(m.start())
            near = low[max(0, m.start() - self.WINDOW):m.end() + self.WINDOW]
            if not self.CORRECTION.search(near):
                uncorrected.append(m.start())
        return told, uncorrected

    def test_no_document_still_says_the_embedded_python_was_removed(self):
        """The mechanical half of the rule, and the one that catches staleness
        without needing to parse a claim: a document may tell the removal
        as history, but only if it also says it came back. Any file that
        mentions the removal and never mentions NC-021 is describing a state the
        code left behind."""
        total = 0
        for rel in self.DOCS:
            told, uncorrected = self.tellings_of_the_removal(self.read(rel))
            total += len(told)
            for at in uncorrected:
                with self.subTest(doc=rel, at=at):
                    self.fail(
                        f"{rel} tells the removal of the embedded "
                        f"Python at offset {at} without saying, within "
                        f"{self.WINDOW} characters, that it came back — a reader "
                        "who stops there believes the bundle has no interpreter")
        self.assertGreater(total, 0, "no document tells the removal at all, so "
                                     "this check is matching nothing")

    def test_the_removal_check_flags_a_stale_paragraph_and_passes_a_corrected_one(self):
        """The check has to fail when its rule is broken, not merely pass."""
        stale = "The embedded CPython was removed as 67MB that never ran."
        told, uncorrected = self.tellings_of_the_removal(stale)
        self.assertEqual((len(told), len(uncorrected)), (1, 1))
        fixed = stale + " It came back in NC-021, reordered so it runs first."
        told, uncorrected = self.tellings_of_the_removal(fixed)
        self.assertEqual((len(told), len(uncorrected)), (1, 0))
        unrelated = "Everything removed here is a GUI toolkit this app never uses."
        self.assertEqual(self.tellings_of_the_removal(unrelated), ([], []))

    def test_the_cache_precondition_is_stated_where_a_builder_reads(self):
        """The requirement. Someone building the bundle reads one of three things;
        all three have to carry the precondition, because a fresh clone's build
        succeeds either way and only the output line tells them which bundle
        they got."""
        for rel in ("README.md", "packaging/README.md", "packaging/appify.sh"):
            with self.subTest(doc=rel):
                text = self.read(rel)
                self.assertIn("packaging/.cache", text.replace(".cache/", ".cache"),
                              f"{rel} never names the directory the interpreter "
                              "has to be in, so a fresh clone silently builds the "
                              "smaller bundle")
                # Whitespace-normalised: these docs wrap at 80 columns, so
                # "never touches the network" is routinely split across a line
                # break and a raw search misses a sentence that is right there.
                import re as _re
                flat = _re.sub(r"\s+", " ", text.lower())
                self.assertTrue(
                    _re.search(r"no network fetch|never touches the network|"
                               r"not fetch|never downloads|is not downloaded|"
                               r"download .{0,40}by hand", flat),
                    f"{rel} does not say the build makes no network fetch, so "
                    "the empty cache reads as a bug rather than the design")

    def test_no_document_claims_a_fetch_or_a_checksum_that_does_not_exist(self):
        """packaging/.gitignore explained its own rule by saying the tarballs
        were "fetched by `appify.sh --with-python`" and "reproducible from the
        pinned release + checksum in appify.sh". None of that exists. That
        sentence is the direct cause of NC-137: it is the note a maintainer
        reads when wondering why the cache is ignored, and it told them the
        build would get the file itself.

        The mirror of test_the_build_really_does_not_fetch_the_interpreter —
        that one asserts the code makes no fetch, this one asserts no document
        says it does."""
        import re
        CLAIM = re.compile(
            r"(fetch|download|downloads|downloaded)(ed)? by [`']?appify|"
            # The junk class matters: a sentence like
            # "`appify.sh --embed` downloads the interpreter" has the flag
            # and a backtick between the two halves.
            r"appify\.sh[a-z0-9 `'\-–—]{0,24}(fetches|downloads|will download)|"
            r"checksum[- ]verif|"
            r"(pinned release|checksum) in appify")
        for rel in self.DOCS:
            flat = re.sub(r"\s+", " ", self.read(rel).lower())
            # The history section legitimately describes what the REMOVED flag
            # used to do, so a past-tense telling inside it is allowed; what is
            # not allowed is a present-tense claim about the build today.
            flat = flat.replace("used to download, checksum-verify and unpack", "")
            # A negation inside the match is the whole point of some of these
            # sentences: "`appify.sh` never downloads that interpreter" is one
            # the pattern above would otherwise match. Same
            # lesson as the xcode-select rule further up this file: warning
            # someone off a thing requires naming it.
            NEG = re.compile(r"never|not |n't |no network|by hand|deliberately")
            m = next((m for m in CLAIM.finditer(flat)
                      if not NEG.search(flat[max(0, m.start() - 40):m.end() + 40])),
                     None)
            with self.subTest(doc=rel):
                self.assertIsNone(
                    m, f"{rel} says the build fetches or checksums the "
                       f"interpreter ({flat[max(0, (m.start() if m else 0) - 40):(m.end() if m else 0) + 60]!r}). "
                       "It does neither — you download the tarball by hand into "
                       "packaging/.cache/. This exact sentence is why NC-137 "
                       "existed.")

    def test_the_app_resolves_the_bundled_interpreter_where_the_build_puts_it(self):
        """Was test_the_documented_preference_order_matches_the_launcher. The order
        (bundled first, then pyenv and Homebrew) was the one fact in this story that was
        easiest to let drift, and any document that spelled it out had to include the
        bundle. There is no order since NC-330 (2026-10-17): the app uses the bundled
        Python and nothing else, so what has to agree is the path. The build puts it at
        Contents/Resources/python (embed_python's destination); the app looks for
        Resources/python/bin/python3. If they ever differ every bundle is one whose
        engine cannot start."""
        build = self.read("packaging/appify.sh")
        self.assertIn('embed_python "$APP_DIR/Contents/Resources/python"', build)
        swift = self.read("native/Sources/Engine.swift")
        self.assertIn('resources.appending(path: "python/bin/python3")', swift,
                      "the app no longer looks for a bundled interpreter where the "
                      "build puts it, so every doc describing one is now wrong")

    def test_the_needs_nothing_installed_promise_carries_its_conditions(self):
        """README.md's headline claim. It is true — for an arm64 `--embed` build
        made with the tarball cached — and the conditions are the whole content
        of the claim, so they have to sit in the same section as the promise."""
        readme = self.read("README.md")
        i = readme.index("### Install as a Mac app")
        section = readme[i:readme.index("\n---", i)]
        self.assertIn("nothing installed", section)
        # NC-330: "--no-python" is no longer a condition, the option is gone. What is left
        # to name is the one Mac it does not run on and the one thing a builder must have.
        for condition, why in (
                ("Intel", "an Intel Mac cannot open the app"),
                ("packaging/.cache", "a build with nothing cached cannot be made")):
            with self.subTest(condition=condition):
                self.assertIn(
                    condition, section,
                    f"the install section promises 'nothing installed' without "
                    f"naming the case where it is false: {why}")
