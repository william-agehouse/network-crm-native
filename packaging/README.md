# Packaging Network CRM as a Mac app

Turns the repo into **Network CRM.app** — something a normal person can
double-click, with no Terminal and no git.

> **An `--embed` build needs nothing installed on the target Mac.** It carries
> its own CPython in `Contents/Resources/python`, and the app starts its engine on
> that one every time, so the path that ships is the path that runs here too. This
> came back in NC-021 after being removed once — the history, and the reason the
> first attempt never ran, are in
> ["Why the embedded Python left and came back"](#why-the-embedded-python-left-and-came-back) below.
>
> **What the app runs on (NC-312, decided):** a Mac with an Apple chip and macOS
> 26 or later. `Info.plist` says 26.0 and prefers the Apple chip, so macOS itself
> turns an older Mac away; the window is compiled for 26.0 (`-target` in
> `native/compile.sh`), so a build made on a newer macOS still starts there.
>
> **What the app is, since NC-330 (2026-10-17).** One program: the native window,
> compiled from `native/Sources`. It starts the engine (`crm/app.py`) as its own
> child, talks to it through a private file only this Mac account can open instead
> of a network address, and stops it when it quits. There is no launcher script, no
> web page in a window, and no separate "Quit Network CRM" app any more.

## Build

**The command to actually ship**, from the repository's folder:

```bash
packaging/appify.sh --embed
```

`--embed` copies `crm/`, `requirements.txt`, `requirements-lock.txt`,
`requirements-semantic.txt`, `LICENSE` and `THIRD_PARTY_NOTICES.md` into the
bundle, so the app is self-contained *code* and safe to move, rename or copy.
`static/` (the old web page) is not copied: the window is native, and nothing in
the app opens a page.
It needs no network — and that is a design decision with a precondition
attached, not just a nice property:

> **The interpreter is not downloaded. You put it there once.** `--embed`
> bundles a CPython only if it finds an `install_only`
> `aarch64-apple-darwin` tarball already sitting in `packaging/.cache/`, which
> is gitignored, so a fresh clone does not have one. Get one from
> [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases)
> and drop it in that directory.
>
> Without it the build **stops** (NC-330), names the directory it looked in, and
> replaces nothing: the app starts its engine on its own Python, so a copy without
> one could not open. (Before NC-330 the build went on and made a smaller bundle
> that needed Python 3.10+ on the target Mac; that bundle no longer exists, and nor
> does `--no-python`.) Check the `bundled CPython` line in the output, or that
> `Contents/Resources/python/bin/python3` exists.

`--with-python` is an accepted spelling of the same thing, kept because it is
what the older notes say. It builds exactly what `--embed` builds.

**The interpreter is prepared once (NC-225).** Unpacking, trimming, stripping
and signing it, then proving it can build a Python environment, wrote about
190 MB on every build. The finished tree is now kept in
`packaging/.cache/prepared-<tarball name, size, date>-<appify.sh size, date>/`
and copied into each later build as an APFS clone, which shares the blocks on
disk instead of writing them again. A new tarball, or any edit to `appify.sh`,
prepares a fresh one; the two newest are kept.

## Building the copy you hand out

```bash
packaging/appify.sh --handout
packaging/make_dmg.sh
```

`--handout` is `--embed` plus three things. It writes the address of the
project's releases page into the app (`PUBLIC_REPO` in `crm/updates.py`), so the
update check knows where new versions come from (NC-315). It compiles the
copying helper for macOS 26 as well as the window. And it refuses, removing what
it built, unless the app carries its own Python, its window, its helper and that
address, and unless its identity is under `io.github.`: an app named after a
private address would appear under that name in every recipient's Login Items
and Full Disk Access list (NC-311).

`make_dmg.sh` then puts the app in a disk image with **Read Me First.txt**,
which says what Mac it needs, walks through **Open Anyway** for the first
launch, explains the minute of setting up, and says how to update: replace the
app; your data stays (NC-313).

Two settings move the build elsewhere: `NC_DIST_DIR` (where the apps and the
disk image go) and `NC_PY_CACHE_DIR` (where the CPython tarball is).

**Hand out the exact copy that was built, without opening it first.** Every
build removes compiled Python caches from the whole bundle as its last step,
because a cache records the folder it was made in (NC-310), and the app starts
its engine with `PYTHONDONTWRITEBYTECODE=1` so it writes no new ones; not opening the copy keeps anything else from
being added to it either.

The lighter mode still exists:

```bash
packaging/appify.sh             # developer build (see below)
```

Every build needs `xcrun swiftc` (from the Xcode Command Line Tools —
`xcode-select --install` if you don't have them), because the window *is* the
app. Without a compiler the build stops and replaces nothing; there is no
browser fallback any more. `native/compile.sh` picks the toolkit: the macOS 26.5
one under the Command Line Tools first, otherwise whatever `xcrun` finds, and it
says which (NC-375).

## Why the embedded Python left and came back

**It ships today.** `--embed` bundles a CPython and the app runs its engine on it,
every time.
This section is the history, kept because the reason the first attempt failed is
the reason this one is built differently.

### It left once, as "67MB that never ran"

`--with-python` unpacked a
[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone)
CPython 3.13.14 into `Contents/Resources/python` so the app would run on a Mac
that had never had Python installed. It was removed because it never executed
once.

The launcher of the time built `~/Library/Application Support/NetworkCRM/venv` once and
rebuilt it only when its interpreter stopped working. So on any Mac that already
had a Python — every development machine — the venv was created from that Python
and the embedded interpreter was written to disk and never touched again. 67MB
of dead weight, and the one claim it existed to support ("runs on a Python-free
Mac") was never once exercised.

The lesson is worth more than the flag was: **an untested distribution path is
indistinguishable from a working one.** The bundle looked self-contained, the
build printed a reassuring line, and nothing was false except the part nobody
could see.

### It came back in NC-021, with the ordering changed

**Corrected:** "it never ran" was the symptom, not the cause, and the difference
decides whether a second attempt works. The bundled interpreter was the *last*
candidate the launcher tried. Since the launcher keeps whatever it finds first,
a Mac that already had any Python made the bundled copy unreachable by
construction — not merely untested. Removing it fixed the dead weight and left
the reach problem exactly where it was.

Two changes made that attempt different, both in the launcher script of the time:

- **The bundled interpreter became candidate #1**, ahead of pyenv and Homebrew.
- **The venv became keyed on the interpreter that built it**, so a stale venv
  could no longer pin the app to an interpreter that was no longer the one
  shipping.

**Since NC-330 there is no list of candidates at all.** The launcher script is
gone; the window starts the engine on `Contents/Resources/python` and on nothing
else, and `crm/python_env.py` builds the venv from that interpreter and rebuilds
it when the interpreter changes. A Mac's own Python is never looked for, so the
bundled one cannot be skipped by construction.

### What is still open

The claim "runs on a Mac that has never had Python" is still only *argued*, not
*measured* — the argument being that the app now uses no other interpreter. The
section "Proving a build works on a Mac that has never seen this app" below is
the right shape of test; it still needs a host without an interpreter. Until
someone runs it there, treat that sentence as the strongest available reasoning
rather than as a verified fact: a path that is argued is not the same as a path
that is exercised.

## Proving a build works on a Mac that has never seen this app

Opening the built app on your own Mac **cannot** prove this. It runs against your
real data folder, already full of contacts — so the "first launch by a stranger"
half is missing entirely.

**Every `--embed` build already runs one check on itself.** As its smoke test,
`appify.sh` runs `Contents/MacOS/NetworkCRM --engine-check`: the app starts its
engine from inside the bundle on a throwaway folder, and the build fails unless
the engine answers, has opened no network port, keeps its private file to this
account (permissions 600 in a 700 folder), stops by itself when the app vanishes,
and leaves nothing running after a quit. No window opens and the real data is
never touched. The output lines are printed with the build.

What that check does not cover is a stranger's first launch with real choices.
Simulate that by hand. This is not ceremony: it is the check that catches an app
auto-importing WhatsApp before the user has agreed to anything, and that import
then pushing the contact count past the onboarding grandfather threshold so the
setup wizard never appears at all.

```bash
A="packaging/dist/Network CRM.app/Contents/Resources"
S=/tmp/ncrm-cleanmac                      # anywhere disposable
rm -rf "$S" && mkdir -p "$S/data" "$S/secrets"
# The bundle's OWN CPython, exactly as the app uses it. This is still not "a Mac
# that has never had Python": only running this on a host with no interpreter
# proves that, and this has not been tested on one.
PY="$("$A/python/bin/python3" "$A/app/crm/python_env.py" prepare \
  --venv "$S/venv" --app-home "$A/app" --data "$S/data")"
NC_NO_BROWSER=1 APP_PORT=8768 PYTHONDONTWRITEBYTECODE=1 \
  CRM_DATA_DIR="$S/data" CRM_SECRETS_DIR="$S/secrets" \
  "$PY" "$A/app/crm/app.py"
```

This runs the engine on a network port (8768) on purpose, so `curl` can reach it;
the app itself never does. `PYTHONDONTWRITEBYTECODE=1` is not optional — the
source lives inside the .app, and a `__pycache__` written there fails
`tests/test_dev_parity.py`, breaks the app's seal, and breaks a read-only install.

Then check, in another shell:

```bash
curl -s -D - -o /dev/null http://127.0.0.1:8768/api/status | grep -i '^server:'   # names the live Python
curl -s localhost:8768/api/onboarding  # MUST be {"done": false} on a first launch
curl -s localhost:8768/api/import/detect | grep -o '"auto": *true' | wc -l   # must be 0
```

A first launch must leave `data/` holding an empty `crm.db` and **no
`archive.db` at all**. An `archive.db` means something imported message bodies
before anyone said yes.

## `--embed` vs the developer build

`appify.sh` has two modes (three counting `--handout`, which is `--embed` with
more checks, above):

- **`--embed`** (what you ship): copies `crm/`, `requirements.txt`,
  `requirements-lock.txt`, `requirements-semantic.txt`, `LICENSE` and
  `THIRD_PARTY_NOTICES.md` into `Network CRM.app/Contents/Resources/app/`, and
  the CPython beside it. The resulting `.app` is self-contained — move it,
  rename it, zip it, hand it to someone else, it keeps working, and it needs
  nothing installed. User data goes to
  `~/Library/Application Support/NetworkCRM/{data,secrets}` so it survives
  rebuilding or replacing the bundle. Trade-off: a rebuild is needed to pick up
  any code change.
- **no flag** (developer build): instead of copying code, the bundle records this
  checkout's absolute path in `Contents/Resources/repo_path.txt`, and the app
  starts the engine from the checkout's `crm/` on the checkout's `.venv` (or the
  one in the folder above it). It still uses the app's own data folder in
  Application Support, the same one an `--embed` build uses. Handy for trying a
  change in the window without a copy step. Trade-off: the bundle is worthless
  once separated from this exact checkout — never hand out a developer build.

The app clears every `NC_*`, `CRM_*`, `PYTHON*` and `NETWORK_CRM_HOME` setting
before it starts its engine, so nothing in the shell that opened it can point the
engine at other code, other data or another Python. (Before NC-330
`NETWORK_CRM_HOME` overrode both modes; it no longer does.) To run a window
against an engine you started yourself, start the program inside the developer
copy from `native/build.sh` with `NC_NATIVE_PORT=<port>` or
`NC_NATIVE_SOCKET=<socket file>` set; it then joins that engine instead of
starting one.

## Icon

`packaging/src/appicon.svg` — a persimmon (`#b5502a`) rounded square with a
lowercase serif "n", matching the browser favicon in `static/index.html`.
`appify.sh` builds the `.icns` using only tools that ship with macOS:

1. `qlmanage -t` (QuickLook) rasterizes the SVG to a 1024×1024 PNG.
2. `sips` resizes it down to the 10 sizes `iconutil` expects.
3. `iconutil -c icns` packs them into `appicon.icns`.

If `sips`/`iconutil` are missing, the build script exits with an error (they
ship with every Mac, so this should not happen in practice). If `qlmanage`
specifically fails or is missing, the build prints a warning and continues
without an icon rather than failing the whole build.

## What happens when the app opens

`Contents/MacOS/NetworkCRM` is the whole app: the native window, compiled from
`native/Sources` by `native/compile.sh`. When it opens (NC-330):

1. **It finds its engine's code and Python.** For an `--embed` build, that is
   `Contents/Resources/app` and `Contents/Resources/python`; for a developer
   build, the checkout named in `repo_path.txt` and its `.venv`. No other
   Python on the Mac is ever looked for.
2. **It makes its folders**, each opening for this Mac account only:
   `~/Library/Application Support/NetworkCRM/{data,secrets,run}` and
   `~/Library/Logs/NetworkCRM`.
3. **The first start of a new version sets up the optional parts.** It runs
   `crm/python_env.py check`; if that says setup is needed, the window says
   "Setting up the optional parts (Ask with AI, Gmail, Google photos)…" while
   `python_env.py prepare` builds `~/Library/Application Support/NetworkCRM/venv`
   from the bundled Python and installs the packages in `requirements.txt`, at
   the exact versions and file fingerprints in `requirements-lock.txt` (NC-316):
   pip refuses any file whose fingerprint differs, and installs only ready-made
   packages, never building one from source. That takes about a minute and needs
   the internet once. A failed install never stops the app — the core needs only
   the standard library — but it is not hidden either: see "Dependency status
   marker" below. The environment rebuilds itself when its Python stops working
   (a half-built one from a crash, or the app moved after first launch) or when a
   new version brings a different Python.
4. **It starts the engine as its own child**: `python crm/app.py --socket
   …/run/engine.sock --watch-stdin`, logging to
   `~/Library/Logs/NetworkCRM/server.log`. The engine opens **no network port**.
   It answers only on that socket file, a private connection with permissions
   600 in a 700 folder, so nothing else on the Mac — no browser, no web page, no
   other account — can reach it. The window talks to it over that file.
5. **When the app goes, the engine goes.** The engine watches the end of a pipe
   only the app holds, so even a force-quit app, which runs no shutdown code,
   takes its engine with it. On a normal quit (Cmd+Q, the app menu, Dock > Quit)
   the app also asks the engine to stop, and after three seconds stops it and
   everything it started. That is why there is no "Quit Network CRM" app any
   more: nothing can be left behind for it to stop.

The first version with this design also stops, once, an engine left running by
an older version: it reads the old process number from Application Support and
stops it only if that process's command line really is `crm/app.py`.

## Dependency status marker

When the first-start `pip install` (or making the environment itself) fails, the
app does **not** fail to open — the standard-library core still runs with zero
optional parts (the app must work with no API keys) — but `crm/python_env.py`
records the failure so it can't hide:

- The real error goes to `~/Library/Logs/NetworkCRM/server.log`.
- It writes a JSON marker at **`$CRM_DATA_DIR/deps_status.json`** — for a normal
  install that is
  **`~/Library/Application Support/NetworkCRM/data/deps_status.json`**.

The marker's presence means "something optional is missing"; on the next start,
if the install succeeds, it is **deleted**. Format:

```json
{
  "status": "error",
  "phase": "pip_install",         // or "venv_create"
  "message": "The optional parts (Ask with AI, Gmail, Google photos, LinkedIn messages) could not finish installing. The rest of the app works, and it tries again the next time it opens.",
  "timestamp": "2000-01-01T12:00:00Z",
  "requirements": "…/Contents/Resources/app/requirements.txt",
  "log_file": "…/Library/Logs/NetworkCRM/server.log",
  "retry": "Open Network CRM again while online and it tries once more by itself, or delete the folder …/venv to set it up from scratch."
}
```

So a machine never stays broken for good:

- **Offline first start, or a package that will not install** → marker written,
  and the install runs again at the next start (the "installed" stamp inside the
  environment is written only on success, keyed by the sha256 of
  `requirements.txt` and `requirements-lock.txt` together, so it also reinstalls
  when a new version changes either).
- **Half-built or moved environment** → detected and rebuilt from scratch.

The app reads that marker: `api_instance.deps_status()` puts it in the
diagnostics, and when an install has failed Settings says so and names the
install's log (`crm/api_abilities.py`).

Meaning-based search (sentence-transformers and PyTorch, about 2 GB) is never
installed at first start. Settings offers it, and installs it the same way: only
the versions and fingerprints in `requirements-lock.txt` and
`requirements-semantic.txt`, and only ready-made packages.

## First launch (the app is not signed with a Developer ID)

A build made by this script is sealed but **not signed by a known developer**:
`appify.sh` signs it *ad hoc*, as one piece, which lets macOS notice any later
change inside it but tells Gatekeeper nothing about who made it. Signing with a
Developer ID needs a paid Apple account and is a manual step — see "Signing and
notarizing" below. State of a fresh build:

```
$ codesign -dv "Network CRM.app"
…
Signature=adhoc
$ spctl -a -vvv "Network CRM.app"
rejected
```

So macOS Gatekeeper **will** get in the way of the *first* launch. This is one
extra step, not a dead end — and the disk image's **Read Me First.txt** tells
users up front so they don't hit a scary dialog cold.

**What the user will see, and how to open it (NC-313):**

1. Double-clicking gives a dialog saying macOS could not verify that "Network
   CRM" is free of malware, with **Done** and **Move to Trash**. Click **Done**.
2. Open **System Settings → Privacy & Security** and scroll to the Security
   section, which says "Network CRM" was blocked. Click **Open Anyway**, confirm
   with the Mac's password or Touch ID, and click **Open Anyway** once more. The
   button is offered for about an hour after the blocked attempt; after that,
   double-click once more to bring it back.

**Right-click → Open no longer does this.** It did until macOS 15; since then
the only way past the block is the Open Anyway button (Apple's "Open a Mac app
from an unknown developer" page). The app needs macOS 26, so every recipient is
on a macOS where this is the route.

**If instead it says *"Network CRM is damaged and can't be opened. You should
move it to the Trash":*** it isn't damaged — that message is what a *quarantined
unsigned app with embedded binaries* (this app bundles its own Python) often
shows instead of the Open dialog. Strip the quarantine flag from Terminal and it
opens normally:

```bash
# Remove just the quarantine attribute, recursively (the app is fine afterwards):
xattr -dr com.apple.quarantine "/Applications/Network CRM.app"
# (equivalently, clear ALL extended attributes: xattr -cr "/Applications/Network CRM.app")
```

Then double-click as usual. `xattr` ships with every Mac; no admin rights or
Developer account needed.

## Signing and notarizing (needs an Apple Developer ID)

**Not done by the build.** With an Apple Developer Program membership, signing +
notarization remove the Gatekeeper prompt entirely (no right-click, no `xattr`,
no "damaged" message) for every user. This is *not* automated by `appify.sh`; run it by hand after
`packaging/appify.sh --embed` + `packaging/make_dmg.sh`. The bundle carries an
embedded CPython and its `.so` extension modules (NC-021), so signing has to
cover every compiled file inside it, not only the app's own two; `--deep` reaches
them.

```bash
# 0. One-time: store notarization credentials in the keychain.
#    Create an app-specific password at appleid.apple.com, then:
xcrun notarytool store-credentials "network-crm-notary" \
  --apple-id "you@example.com" --team-id "TEAMID" --password "app-specific-pw"

# 1. Sign the app (and everything inside it) with your Developer ID, with the
#    hardened runtime (required for notarization).
codesign --deep --force --options runtime --timestamp \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  "packaging/dist/Network CRM.app"

# 2. Verify the signature and that Gatekeeper now ACCEPTS it (the two commands
#    that today report "not signed at all" / "rejected"):
codesign --verify --deep --strict --verbose=2 "packaging/dist/Network CRM.app"
codesign -dv "packaging/dist/Network CRM.app"          # -> shows the Authority chain
spctl -a -vvv "packaging/dist/Network CRM.app"         # -> "accepted, source=Developer ID"

# 3. Rebuild the DMG from the now-signed apps, then sign the DMG itself.
packaging/make_dmg.sh
codesign --force --timestamp \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  "packaging/dist/NetworkCRM.dmg"

# 4. Submit for notarization and wait for the ticket.
xcrun notarytool submit "packaging/dist/NetworkCRM.dmg" \
  --keychain-profile "network-crm-notary" --wait

# 5. Staple the ticket so Gatekeeper is satisfied even offline.
xcrun stapler staple "packaging/dist/NetworkCRM.dmg"
xcrun stapler validate "packaging/dist/NetworkCRM.dmg"
```

## Known limitations

- No automatic updates. A new build means a new DMG and a manual download:
  replace the app; your data stays. An update checker exists
  (`crm/updates.py`); a `--handout` build points it at the project's releases
  page, and any other build leaves it off. A repository with no release yet
  answers "no new version has been published yet", calmly.
- The developer build is for local testing only; never ship it.
- Apple chip and macOS 26 or later only (NC-312). There is no Intel build.
- The optional parts (Ask with AI, Gmail, Google photos) are installed into a
  private environment on the first start of each new version, which needs the
  internet **once**. An offline first start leaves the app running the
  standard-library core AND writes `deps_status.json` (see "Dependency status
  marker") instead of silently doing without; it tries again at the next start.
  Meaning-based search (sentence-transformers and PyTorch, about 2 GB) stays
  opt-in from Settings and is never installed by default.
- Sealed ad hoc but not signed with a Developer ID or notarized by this script
  (see "First launch" and "Signing and notarizing" above); that step is manual
  and needs a paid Apple Developer account.
