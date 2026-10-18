"""Shared test fixtures — and the data-dir bootstrap every test file depends on.

**Import this before any crm module, from every test file, without exception.**

Several crm modules capture paths under data/ into module-level constants at
import time (config.DATA_DIR, and search_cli's crm_vectors.npz /
chunk_vectors.npz). If a test file imports one of them before this module has
set CRM_DATA_DIR, the suite writes into the REAL data/ folder. That is a real
risk, not a hypothetical one: it would leave crm_vectors.npz holding a single
synthetic contact's embedding — which costs a full re-embed of every real
contact on the next semantic search. chunk_vectors.npz is large and is captured
exactly the same way. tests/test_bootstrap.py is the tripwire for a regression.

So every test file starts with the same three lines, and `from tests.helpers
import ...` must be the FIRST crm-touching import:

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tests.helpers import Base, db, ...

`from tests.helpers import ...` (not a bare `from helpers import ...`) is the
only form that works under every invocation: `unittest discover -s tests -t .`,
`unittest tests.test_x`, and pytest.
"""

import atexit
import io
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crm"))

# ── Keep the test run off the SSD as far as it can be ────────────────────────
# Every temporary folder a run makes goes inside ONE folder, removed when the
# run ends. Without this, nothing removes them: Base.setUp makes a folder per
# test, so leftover crm_test_ folders (and about 20 other kinds) would pile up in
# the temp folder run after run. TMPDIR too, so a program a test starts puts its
# files in the same place.
#
# A run that is killed (by a time limit or an interrupt) never reaches its
# atexit, so a run also clears away earlier runs' folders once they are old
# enough that no run can still be using them.
_SYSTEM_TMP = tempfile.gettempdir()
_STALE_RUN_SECONDS = 3 * 3600
for _name in os.listdir(_SYSTEM_TMP):
    if _name.startswith("ncrm-run-"):
        _old = os.path.join(_SYSTEM_TMP, _name)
        try:
            if time.time() - os.path.getmtime(_old) > _STALE_RUN_SECONDS:
                shutil.rmtree(_old, True)
        except OSError:
            pass
_RUN_DIR = tempfile.mkdtemp(prefix="ncrm-run-")
tempfile.tempdir = _RUN_DIR
os.environ["TMPDIR"] = _RUN_DIR
atexit.register(shutil.rmtree, _RUN_DIR, True)

# Test databases are thrown away seconds after they are made, so making SQLite
# wait for every commit to reach the disk buys nothing. With it waiting, one
# full run wrote over a gigabyte to the SSD; the two heaviest files wrote about
# 2.5 times less and ran about 2.5 times faster without it. This changes
# durability only, never what a query returns. The app itself is untouched: it
# keeps SQLite's default.
_real_sqlite_connect = sqlite3.connect


def _connect_without_waiting_for_the_disk(*args, **kwargs):
    conn = _real_sqlite_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA synchronous = OFF")
    except sqlite3.Error:
        pass
    return conn


sqlite3.connect = _connect_without_waiting_for_the_disk

# Point every crm module at a throwaway data dir BEFORE importing any of them.
# setdefault, so an explicit CRM_DATA_DIR (a scratch run against a copy of real
# data) still wins.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ncrm-tests-")
os.environ.setdefault("CRM_DATA_DIR", _TEST_DATA_DIR)
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

import db                              # noqa: E402,F401
import archive as arc                  # noqa: E402,F401
import imessage_import as im           # noqa: E402,F401
import whatsapp_import as wa           # noqa: E402,F401
import email_import as ei              # noqa: E402,F401
import telegram_import as tg           # noqa: E402,F401
import meta_import as meta             # noqa: E402,F401
import seed_from_csv as sc             # noqa: E402,F401
import contacts_import as ci           # noqa: E402,F401
import app                             # noqa: E402,F401
import chat as chat_mod                # noqa: E402,F401
import export_data as ex               # noqa: E402,F401
import feed                            # noqa: E402,F401
import importers                       # noqa: E402,F401
import search_cli                      # noqa: E402,F401
import config                          # noqa: E402,F401
import settings as settings_mod        # noqa: E402,F401
import updates as updates_mod          # noqa: E402,F401


# ── Keep the suite off the machine's protected folders ───────────────────────
# Same principle as the CRM_DATA_DIR redirect above, for a different failure
# mode. importers.detect_sources() probes what is actually on the machine, and
# five of those roots are ones macOS guards: ~/Library/Group Containers
# (WhatsApp), ~/Library/Application Support/NetworkCRM (the iMessage inbox —
# another app's folder as far as the OS is concerned), ~/Library/Messages, and
# ~/Desktop + ~/Downloads via SCAN_HOME.
#
# The failure is not a denied read. On current macOS, the app-data category of
# the privacy-permission system (TCC) makes os.access()/os.path.exists()/glob on
# those paths BLOCK on a consent dialog — so the suite does not fail, it HANGS,
# indefinitely, behind a prompt nobody answers. It stalls the whole run at
# test_api_import.TestImportDetect::test_detect_sources_shape, and the
# `except OSError` around the glob cannot help, because a blocked syscall raises
# nothing.
#
# Redirecting here rather than per-test is deliberate: several tests reach
# detect_sources() indirectly through app.handle_import_sources(), and one of
# them patched only IMSG_AUTO_DB while leaving the other roots pointing at the
# real machine. Individual tests still override any of these freely.
_TCC_SAFE = os.path.join(_TEST_DATA_DIR, "not-the-real-mac")
importers.SCAN_HOME = _TCC_SAFE
importers.WA_CONTAINER = os.path.join(_TCC_SAFE, "whatsapp-container")
importers.IMSG_SOURCE_DIR = os.path.join(_TCC_SAFE, "Messages")
importers.IMSG_AUTO_DB = os.path.join(_TCC_SAFE, "imessage", "chat.db")
importers.IMSG_INBOX = os.path.dirname(importers.IMSG_AUTO_DB)
# WhatsApp's own unprotected copy, added when the FDA helper started writing one.
# Redirected for the same reason as the rest: it lives under Application Support
# in real life, and the suite must never read the real one — a test that imported
# it would rewrite the actual database.
importers.WA_AUTO_DB = os.path.join(_TCC_SAFE, "whatsapp", "ChatStorage.sqlite")
importers.WA_INBOX = os.path.dirname(importers.WA_AUTO_DB)
# WA_PHOTOS_AUTO_DIR is DERIVED from WA_INBOX at import time (NC-047), so the two
# lines above do not move it and it needs its own redirect. This is NC-044's rule:
# add the new path here in the change that adds the source. The gate in
# tests/test_whatsapp_consent.py fails if any importers path constant is still
# pointing at the real home directory.
importers.WA_PHOTOS_AUTO_DIR = os.path.join(importers.WA_INBOX, "Media", "Profile")
# The Contacts copy (NC-029). Leaving it out fails silently, for the worst
# possible reason: while the real directory does not exist yet, the probe finds
# nothing and every test passes. The moment the sync helper first writes it,
# test_api_import's autosync tests would pick up the developer's own address book
# and queue a real import of it inside the test suite. A redirect that is only
# correct while the machine happens to be empty is not a redirect; add the new
# path here in the same change that adds the source.
importers.AB_AUTO_DB = os.path.join(_TCC_SAFE, "addressbook",
                                    "AddressBook-v22.abcddb")
importers.AB_INBOX = os.path.dirname(importers.AB_AUTO_DB)
# Apple Calendar's store (NC-042), redirected in the same change that added the
# path (NC-044's rule). A calendar file can hold thousands of events and
# participants, so a suite that found the real one would queue a real import of
# the developer's entire calendar inside the test run.
importers.CAL_AUTO_DB = os.path.join(_TCC_SAFE, "calendar", "Calendar.sqlitedb")
importers.CAL_INBOX = os.path.dirname(importers.CAL_AUTO_DB)
# OPEN_TARGETS captured two of the above at import time, so rebuild those entries
# or the "Open the Messages folder" tests would assert against the real paths.
importers.OPEN_TARGETS["imessage"]["path"] = importers.IMSG_SOURCE_DIR
importers.OPEN_TARGETS["imessage_inbox"]["path"] = importers.IMSG_INBOX

# The FDA helper's four facts. Redirected for determinism as much as for safety:
# whether the LaunchAgent is installed and what its last run reported are
# properties of the developer's own Mac, so a test asserting the status shape
# would pass here and fail on a machine that had never installed the helper (or
# the reverse). Every test that cares writes these files itself.
importers.IMSG_HELPER_PLIST = os.path.join(_TCC_SAFE, "LaunchAgents",
                                           f"{importers.IMSG_HELPER_LABEL}.plist")
importers.IMSG_HELPER_BIN = os.path.join(_TCC_SAFE, "bin", "networkcrm-imessage-sync")
importers.IMSG_HELPER_REPORT = os.path.join(_TCC_SAFE, "sync-status.json")
importers.IMSG_HELPER_LOG = os.path.join(_TCC_SAFE, "imessage-sync.log")
importers.OPEN_TARGETS["fda_binary"]["path"] = os.path.dirname(importers.IMSG_HELPER_BIN)


def code_of(path: str) -> str:
    """A Python file's CODE, with comments and docstrings blanked to spaces.

    For gates that count how many times something is spelled in a source file.
    Blanked in place rather than deleted, so line and column offsets still line
    up with the real file and a slice taken by index still means what it says.

    A gate that counts raw source trips on the comment that explains it: a
    count of `importers.IMSG_AUTO_DB` goes up when a comment quotes the removed
    guard (NC-103), and a CSS class matches inside the note recording its rename
    (NC-105). A gate that punishes its own explanation teaches people to delete
    the explanation.

    NC-109 moved the copies that had been inlined in test files onto this and
    js_code() below, so there is one of each to get right.
    """
    import tokenize
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    out = list(src)
    lines = src.split("\n")
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)

    def blank(tok):
        a = starts[tok.start[0] - 1] + tok.start[1]
        b = starts[tok.end[0] - 1] + tok.end[1]
        for i in range(a, min(b, len(out))):
            if out[i] != "\n":
                out[i] = " "

    with io.open(path, "rb") as fh:
        prev = tokenize.INDENT
        for tok in tokenize.tokenize(fh.readline):
            is_doc = tok.type == tokenize.STRING and prev in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE,
                tokenize.NL, tokenize.ENCODING)
            if tok.type == tokenize.COMMENT or is_doc:
                blank(tok)
            if tok.type not in (tokenize.NL, tokenize.COMMENT):
                prev = tok.type
    return "".join(out)


def js_code(src: str) -> str:
    """JavaScript source with its // and /* */ comments blanked to spaces.

    code_of()'s counterpart for the front end, for the same reason: a gate that
    reads raw source trips on the comment that explains it. A check that the
    browser never aborts its own fetch (NC-154) matches any comment beside it
    that uses the word "abort" to say why.

    Blanked in place, never deleted, so a slice taken by index still lines up
    with the real file. Strings, template literals and regular-expression
    literals are stepped over rather than read, so "https://…" in a string is
    not a comment. Inside a template literal's ${…} it is code again, which is
    where htm's `${/* note */ ""}` comments live.

    NC-109. Five copies of this had been inlined in test files, each a regex that
    DELETED comments and differed from the others: two kept `https://`, one did
    not, and none of them knew a string from a comment. They all use this now.
    """
    out = list(src)
    n = len(src)

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    def regex_may_start(at):
        """A `/` starts a regex literal after an operator or an opening bracket,
        and is division after a value — the usual rule, good enough here."""
        k = at - 1
        while k >= 0 and src[k] in " \t\n":
            k -= 1
        return k < 0 or src[k] in "(,=:[!&|?{};+-*%<>~^" or src[k - 5:k + 1] == "return"

    # Each entry is "`" while inside a template literal's text, or a brace depth
    # while inside one of its ${ } expressions.
    stack = []
    i = 0
    while i < n:
        c = src[i]
        if stack and stack[-1] == "`":
            if c == "\\":
                i += 2
            elif c == "`":
                stack.pop()
                i += 1
            elif src.startswith("${", i):
                stack.append(0)
                i += 2
            else:
                i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] not in (c, "\n"):
                j += 2 if src[j] == "\\" else 1
            i = j + 1
        elif c == "`":
            stack.append("`")
            i += 1
        elif c == "/" and regex_may_start(i):
            j, in_class = i + 1, False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
        else:
            if stack and c == "{":
                stack[-1] += 1
            elif stack and c == "}":
                if stack[-1] == 0:
                    stack.pop()          # back into the template literal's text
                else:
                    stack[-1] -= 1
            i += 1
    return "".join(out)


def js_code_of(path: str) -> str:
    """js_code() of a file."""
    with io.open(path, encoding="utf-8") as fh:
        return js_code(fh.read())


# ── Reading a BLOCK out of a file, instead of a number of characters ─────────
#
# NC-170. Ten gates in this suite sliced `text[i:i + 700]`, `+ 900`, `+ 2200`,
# `+ 3000` from an anchor and called the result "the section". It is not a
# section, and the number is wrong in both directions at once:
#
#   * TOO SHORT once the thing grows. NC-137 added one bullet to a README and
#     two gates started failing while every sentence they check was still
#     present, because the content had slid past character 2200.
#   * TOO LONG the rest of the time. A 900-character window from one entry in a
#     list reaches into the next one, so a gate on the Contacts source could be
#     satisfied by a line belonging to the Calendar source below it — which is
#     the failure nobody would ever notice, because it passes.
#
# Every document here already has a boundary of its own. These read it.
#
# Four separate versions of this had drifted into four test files before it was
# worth naming; that is the same accident NC-109 records for the
# comment-blanking helper above.


def braced_block(text: str, anchor: str, opener: str = "{",
                 closer: str = "}") -> str:
    """From `anchor`, the balanced `opener`…`closer` block that follows it.

    The block a CSS `@media` rule or a JS function actually occupies, from its
    first brace to the one that matches it. Nesting is counted, so an inner rule
    does not end the outer one.
    """
    at = text.index(anchor)
    start = text.index(opener, at)
    depth, i, n = 0, start, len(text)
    while i < n:
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[at:i + 1]
        i += 1
    raise AssertionError(
        f"unbalanced {opener}{closer} after {anchor!r} — the file is malformed, "
        "or this anchor is inside a string")


def call_block(text: str, anchor: str) -> str:
    """The `name(…)` call or literal that `anchor` sits inside.

    Backs up from the anchor to the `(` that opens it, then reads to the paren
    that closes it. Written for the Swift source table in imessage_sync.swift,
    where each entry is one `Source(...)` literal and the previous 700- and
    900-character windows spilled into the next entry.
    """
    at = text.index(anchor)
    start = text.rindex("(", 0, at)
    return braced_block(text[start:], "(", "(", ")")


def js_statement(text: str, anchor: str) -> str:
    """From `anchor` to the `;` that ends the statement it is part of.

    Depth-aware, so a `;` inside a nested function body or an arrow callback
    does not end it early — which is the whole reason a character count was
    reached for here in the first place.
    """
    at = text.index(anchor)
    depth, i, n = 0, at, len(text)
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return text[at:i]
        elif c == ";" and depth == 0:
            return text[at:i + 1]
        i += 1
    return text[at:]


def js_ternary_branch(text: str, anchor: str) -> str:
    """The TRUE branch of the ternary that `anchor` is the condition of.

    `cond ? <this> : <not this>`. The distinction matters: a window big enough
    to hold the true branch also holds the false one, so a gate asserting "the
    timeout message names Downloads" could be satisfied by the generic message
    underneath it. Depth-aware over brackets and template literals.
    """
    at = text.index(anchor)
    start = text.index("?", at)
    depth, i, n = 0, start + 1, len(text)
    tick = False
    while i < n:
        c = text[i]
        if c == "`" and text[i - 1] != "\\":
            tick = not tick
        elif tick:
            pass
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                break
        elif c == ":" and depth == 0:
            return text[start + 1:i]
        i += 1
    raise AssertionError(
        f"no ternary `:` found for the condition at {anchor!r} — it may not be "
        "a ternary any more, which is itself worth knowing")


def up_to(text: str, anchor: str, closer: str) -> str:
    """From `anchor` to the first `closer` after it, inclusive.

    For boundaries a document states outright rather than by nesting: a shell
    heredoc's terminator, an HTML element's closing tag, a fence. `closer` is
    asserted to exist, so a document that has lost its end marker fails loudly
    instead of silently returning the rest of the file.
    """
    at = text.index(anchor)
    end = text.find(closer, at + len(anchor))
    if end == -1:
        raise AssertionError(
            f"no {closer!r} after {anchor!r} — the block is unterminated, or the "
            "marker has been renamed")
    return text[at:end + len(closer)]


def py_block(text: str, anchor: str) -> str:
    """The Python suite that `anchor` opens — its line, plus every line indented
    under it.

    Indentation IS the boundary in Python, so there is no reason to guess a
    character count. Ends at the first non-blank line indented at or below the
    anchor's own level.
    """
    at = text.index(anchor)
    line_start = text.rfind("\n", 0, at) + 1
    indent = len(text[line_start:]) - len(text[line_start:].lstrip(" "))
    lines = text[line_start:].splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        if not line.strip():
            out.append(line)
            continue
        if len(line) - len(line.lstrip(" ")) <= indent:
            break
        out.append(line)
    return "".join(out)


def _apple_ns(dt):
    return int((dt - datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds() * 1e9)


def _coredata_secs(dt):
    return (dt - datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds()


MAY2026 = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)


# NC-211. Building a test's database runs the whole schema and every migration,
# about 143 KB written per test (measured with the disk waits above already
# off), in thousands of tests a run. The result is the same file every
# time (nothing in the schema or the migrations depends on the time or the
# machine), so it is built once per run, and each test gets a clone of the
# closed file: a copy the Mac's disk format (APFS) makes by sharing the
# original's blocks until one is changed, so it writes almost nothing. A plain
# copy would still write the whole 104 KB file; that is the fallback where a
# clone is not possible (another disk format, another volume).
_TEMPLATE_DB = None
try:
    import ctypes
    _clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    _clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
except (AttributeError, OSError):
    _clonefile = None


def _clone_or_copy(src, dst):
    if _clonefile is not None and _clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
        return
    shutil.copyfile(src, dst)


def fresh_crm_db(path):
    """Put an empty, fully migrated crm.db at `path`, as db.init_db would."""
    global _TEMPLATE_DB
    if _TEMPLATE_DB is None or not os.path.exists(_TEMPLATE_DB):
        template = os.path.join(_RUN_DIR, "template-crm.db")
        db.init_db(template)
        _TEMPLATE_DB = template
    _clone_or_copy(_TEMPLATE_DB, path)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_test_")
        self.path = os.path.join(self.dir, "crm.db")
        fresh_crm_db(self.path)
        self.conn = db.get_db(self.path)
        # The API-backed auto-sync half (Gmail, LinkedIn) is due the moment
        # nothing has run, so it turns up in every _autosync_pending() call and
        # would silently pad assertions about the file-backed half. Off by
        # default; TestApiAutosync switches it back on deliberately.
        self._api_env = {k: os.environ.get(k)
                         for s in app.API_SOURCES.values() for k in (s["switch"],)}
        for spec in app.API_SOURCES.values():
            os.environ[spec["switch"]] = "0"

    def tearDown(self):
        self.conn.close()
        for k, v in self._api_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def not_exported(paths, repo=None):
    """The paths git keeps out of an export (the `export-ignore` attribute):
    files a checkout holds but never publishes. Empty outside a git checkout."""
    import subprocess
    paths = [p for p in paths if p]
    if not paths:
        return set()
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "check-attr", "--stdin", "-z", "export-ignore"],
                             cwd=repo, input="\0".join(paths), capture_output=True,
                             text=True, check=True).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {out[i] for i in range(0, len(out) - 2, 3) if out[i + 2] == "set"}
