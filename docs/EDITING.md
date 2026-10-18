# How to change Network CRM

This guide is for someone who wants to edit the program, whether by hand or with
an AI coding assistant, and who may not build this kind of software for a
living. It says what the program is made of, how to run your edited copy and see
a change, how to run the tests that tell you whether you broke something, where
to look for what, which rules must not be broken (and why), how to build the Mac
app, and how to check a change against what people actually run.

Words you will meet are explained the first time. If one is not, that is a gap in
this guide, not in you.

**On this page**

- [What it is made of](#what-it-is-made-of), and why the older web page has no build step
- [Run it from the source and see a change](#run-it-from-the-source-and-see-a-change)
- [The tests](#the-tests)
- [Where to change what](#where-to-change-what)
- [The rules that must not break](#the-rules-that-must-not-break)
- [Build the Mac app](#build-the-mac-app)
- [Check a change against what actually ships](#check-a-change-against-what-actually-ships)
- [How the app checks itself](#how-the-app-checks-itself)
- [Where to read more](#where-to-read-more)

---

## What it is made of

Network CRM is a private address book for everyone you know. (CRM is short for
*customer relationship management*: software that remembers the people you deal
with. Here it is for your own network.) It pulls in your contacts and message
history, scores how close you are to each person, and lets you search and ask
questions about all of it, with the help of an AI (artificial intelligence)
language model if you add a key. It runs only on your Mac.

There are two pieces, and two small extras.

```
        ┌─────────────────────────── your Mac ──────────────────────────┐
        │                                                               │
        │  Network CRM.app — a window drawn with Apple's own toolkit    │
        │         │                                                     │
        │         │  starts the engine when it opens, stops it on quit, │
        │         ▼  and talks to it through a private file only you    │
        │            can open (no network address)                      │
        │   THE ENGINE (Python)                                         │
        │   crm/app.py + crm/api_*.py                                   │
        │   answers every question                                      │
        │         ▼                                                     │
        │   crm.db  your people and notes     archive.db  message text  │
        └───────────────────────────────────────────────────────────────┘
```

| Piece | What it is made of | Where it lives | What it does |
|---|---|---|---|
| **The engine** | Python, using only what comes with Python itself (its *standard library*), plus two database files in the SQLite format | `crm/` | Holds the data, runs the imports, answers every question. Inside the app it opens no network address at all: it answers only through a private file (a *socket*) in the app's own folder, which only your Mac account can open. |
| **Network CRM.app** | Apple's own toolkit for drawing Mac windows (SwiftUI), in Swift, Apple's programming language | `native/` (the screens), `packaging/` (the build) | People, Ask, Lists, Data, Settings. It starts the engine when it opens, on the copy of Python it carries, and stops it when it quits. |
| The **copying helper** (extra) | One small compiled program, written in Swift | `packaging/src/imessage_sync.swift` | The only part that is ever given Full Disk Access, so that it can copy Messages, WhatsApp, Contacts and Calendar into the app's own folder. |
| The **older web page** (extra) | One small library that draws the page from data (Preact) and another that lets you write the page's layout inside the code (htm), loaded as plain JavaScript modules | `static/` | What the app showed in its window before 17 October 2026 (NC-330). It is no longer part of the app; it still works in a browser when you run the engine from source, which is handy for trying an engine change. |

Your data lives outside the code. For the packaged apps it is in
`~/Library/Application Support/NetworkCRM` (the `~` means your home folder). When
you run from source it is in a `data/` folder inside your copy of the code, unless
you point it somewhere else (next section).

### Why the older web page has no build step

Most modern web pages are *built*: you write source files, then a tool (usually
one installed with npm, a package manager for JavaScript) turns them into the
different files a browser actually reads. Here there is no such step. **The files
in `static/` are the files the browser reads.**

That works because browsers can load JavaScript modules directly (one file saying
`import … from "./other.js"` is enough), and the two small libraries are a single
file of about 13 kilobytes (KB), kept inside the repository in `static/vendor/`.
The result:

- You edit a file, save, and reload the page. That is the whole loop.
- There is nothing to install and nothing that breaks when a tool updates.
- What ships is exactly the text you can read in the folder.

The price is that nothing checks types for you (whether a number is passed where
text is expected). The syntax check and the tests carry that weight, which is one
reason to run them.

---

## Run it from the source and see a change

### What you need

- A Mac with **Python 3.10 or newer**. Not the Python that comes with macOS or
  with Apple's developer tools: that is 3.9, too old, and the engine refuses to
  start on it. The macOS installer from python.org is the simple route.
- The code, from [the README's quick start](../README.md#quick-start) (copy it with
  git, or download it as a compressed folder, a ZIP file).
- A private folder of add-on packages for the code (a *virtual environment*, so
  they stay out of the rest of your Mac), made once from the code's folder:

  ```bash
  python3 -m venv .venv
  .venv/bin/python3 -m pip install -r requirements.txt -c requirements-lock.txt
  ```

Node (a program that runs JavaScript outside a browser) is optional. You only need
it for the older web page's syntax check below.

### Start your own copy

Run from source, the engine listens on a network address on your own Mac (a
*port*; the default is 8765), so a browser or a test can reach it. The app never
does that. Give yours a spare port, and point it at a throwaway folder so that
anything you try cannot touch real data:

```bash
NC_NO_BROWSER=1 NC_AUTOSYNC=0 APP_PORT=8900 \
  CRM_DATA_DIR="$HOME/ncrm-dev/data" CRM_SECRETS_DIR="$HOME/ncrm-dev/secrets" \
  .venv/bin/python3 crm/app.py
```

What each setting does:

- `NC_NO_BROWSER=1` stops it opening a browser tab by itself.
- `NC_AUTOSYNC=0` turns off its automatic background imports, which otherwise
  check for new messages every couple of minutes.
- `APP_PORT=8900` is the door number it listens on.
- `CRM_DATA_DIR` and `CRM_SECRETS_DIR` say where it keeps its data and its keys.
  Leave both out and a run from source uses `data/` and `secrets/` inside your
  copy of the code. Those folders are ignored by git, so they are never committed,
  but they are real folders: if you ever made `data/` a shortcut to the packaged
  app's folder, you are working on real data.

It prints `Network CRM is running LOCALLY at http://127.0.0.1:8900`. Open that
address in a browser to use the older web page, or point a developer copy of the
window at it (see [Network CRM Native.app](#network-crm-nativeapp-the-developer-copy-of-the-window)).
Press Control-C in Terminal to stop it.

With an empty folder, add a few made-up people by hand (the first-run screen walks
you through it). The Data page looks in Downloads and Desktop for exports, so macOS
may ask permission the first time you open it. Saying no is fine.

### See a change

| You changed | To see it |
|---|---|
| A file in `static/` (the older web page) | Save it and reload the page. The engine reads these files from disk on every request. If you ever see the old version, do a hard reload (hold Shift while you press reload). |
| A file in `crm/` (the engine) | Stop it with Control-C and start it again. It does not reload itself. |
| Anything in `packaging/` or `native/` | Build the app again ([Build the Mac app](#build-the-mac-app)). |
| The installed Network CRM.app | It carries its own copy of the code. Editing your folder changes nothing in it until you rebuild. |

---

## The tests

The tests are small programs that check one claim each: "importing the same
messages twice never doubles anything", "a photo you set by hand is never replaced".
A change that breaks a claim makes a test fail, which is how you find out in under
a minute rather than in someone's address book.

There are roughly 2,800 of them, in about 120 files, one file per area. They use
only what comes with Python.

```bash
.venv/bin/python3 -m unittest discover -s tests -t .                  # all of them
.venv/bin/python3 -m unittest tests.test_db_core                       # one file
.venv/bin/python3 -m unittest tests.test_api_instance.TestUpdates -v   # one group
.venv/bin/python3 -m pytest tests/ -q                                  # also works, if installed
```

A full run takes about 45 seconds on a recent Apple Silicon Mac. A few tests skip
themselves when something optional is missing (the meaning-based search model, or
a built app). *Skipped* is not *failed*. One test checks that the packages in
`requirements.txt` are installed, which is why you installed them above.

When something fails, the test's file name says which area, and the note at the
top of the file says which claim it defends. Read that before you decide the test
is wrong.

### Writing a new test

Every test file starts with the same three lines, in this order:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db   # must be the first import that touches crm/
```

The reason is a trap worth knowing. Several engine modules decide where the data
folder is the moment they are first loaded. `tests/helpers.py` points them at a
throwaway folder *before* they load. A test file that loads an engine module first
makes the whole run read and write the real data folder, and everything still
passes while it happens. `tests/test_bootstrap.py` exists to catch that.

`Base` gives each test a fresh, empty database in a temporary folder
(`self.conn`). Use made-up people (Ada, Grace, Alan), email addresses on
`example.com`, and phone numbers only from the range Sweden's telephone regulator
sets aside for fiction, 070-174 06 05 to 070-174 06 99. A test checks the
repository for anything else.

### The two quick checks

Python mistakes (a misspelled name, a broken line) are found by a checker called
ruff. It is not in `requirements.txt`, so install it first:

```bash
.venv/bin/python3 -m pip install ruff
.venv/bin/ruff check crm/ tests/
```

`ruff.toml` limits it to real errors on purpose. Do not run it with `--fix`: a few
`import config` lines look unused but exist for their side effect (loading the
settings file), and removing them breaks the app.

The web page's JavaScript can be checked for syntax with Node, but **not** with
the obvious `node --check file.js`. For files that use `import` (every file in
`static/app/`), that command answers "fine" whatever the file contains. Feed the
file in on standard input and say what kind it is:

```bash
node --input-type=module --check < static/app/people.js

# every page file at once, naming any that are broken:
for f in static/app/*.js; do node --input-type=module --check < "$f" >/dev/null 2>&1 || echo "BROKEN: $f"; done
```

### When the test run seems frozen

On recent macOS, merely looking at certain folders (Desktop, Downloads, another
app's data) can make macOS open a permission dialog, and the program waits for
your answer. In a test run that shows up as a hang, not a failure. The tests point
every such folder at a temporary one for this reason. Never "fix" a test by
pointing it back at your real folders.

---

## Where to change what

Start from what you want to change on screen, not from the file names. Comments in
the code carry tags like `NC-028`; they refer to a list of past decisions that is
not in this repository. The comment next to a tag says everything you need.

| You want to change | Look in |
|---|---|
| What a person's card or page contains | `crm/api_serialize.py` (what is sent), `static/app/people.js` (how it is drawn) |
| How close you are to someone (the 0 to 100 score) | `relationship_strength` in `crm/db.py`; the counting in `crm/engagement.py` |
| The People table, filters, the person page | `crm/api_contacts.py`, `static/app/people.js`, `static/app/windowing.js` |
| **Ask** (questions about your network) | `crm/chat.py` (the loop that talks to the AI), `crm/api_ask.py` (both the AI path and the path that works with no key), `static/app/ask.js` |
| The **Data** page and each source's row | `crm/api_ledger.py`, `crm/api_sources.py`, `crm/importers.py`; drawn by `static/app/data.js` and `static/app/ledger.js` |
| The **Settings** page, and which settings exist | `crm/api_abilities.py`, `crm/settings.py`, `static/app/settings.js` |
| Adding an import source (a new place to bring people or messages from) | The recipe in [`ARCHITECTURE.md`](../ARCHITECTURE.md#8-recipe-add-a-new-import-source): a `crm/<name>_import.py`, one entry in `crm/importers.py`, tests. The Data page draws it with no page edit. |
| An existing importer | `crm/<source>_import.py`, for example `imessage_import.py` (the one to copy from) |
| Gmail, LinkedIn | `crm/gmail_import.py`, `crm/api_gmail.py`; `crm/linkedin_import.py`, `crm/api_linkedin.py`, `crm/linkedin_token.py` |
| Contact photos | `crm/photos.py` |
| Search | `crm/search_cli.py` (keyword and meaning-based), `crm/archive.py` (message text) |
| A new database column | `ensure_columns` in `crm/db.py`, and nowhere else (see the rules) |
| A new endpoint (a new address the page can ask the engine for) | A new `crm/api_<topic>.py` that declares a `ROUTES` table, added once to `ROUTE_MODULES` in `crm/app.py`. A handler whose first parameter is called `conn` is given a database connection. Addresses with an id inside them (like a person's number) keep their hand-written branches in `app.py`. |
| Colours, sizes, fonts | `static/css/tokens.css`. Everything else in the stylesheets points at variables declared there. `docs/DESIGN_DIRECTION.md` is the design brief. |
| A new page file | Add it where `static/app/main.js` imports it, **and** to the list of cached files in `static/sw.js`; a test fails if the two disagree |
| Wording shown to people | The strings in `static/app/*.js`; each source's help text in `crm/importers.py` |
| How the app starts and stops its engine | `native/Sources/Engine.swift` (finding the code and Python, starting, stopping), `native/Sources/PrivateConnection.swift` (talking over the private file), `crm/python_env.py` (setting up the optional parts), the socket half of `crm/app.py` |
| Building the app | `packaging/appify.sh`, `native/compile.sh` |
| The copying helper and its installer | `packaging/src/imessage_sync.swift`, `packaging/install_imessage_sync.sh`, `crm/api_fda.py`, `static/app/fda.js` |
| The app's screens | `native/Sources/` (the files are named for their screen: `People…`, `Person…`, `Ask…`, `Lists…`, `Data…`, `Settings…`, `Onboarding…`). The exact answers the engine gives are written down in [`native/API_CONTRACT.md`](../native/API_CONTRACT.md). |
| The version number | `CURRENT_VERSION` in `crm/updates.py` **and** `CFBundleShortVersionString` in `packaging/appify.sh`. They must match; a test checks. |

---

## The rules that must not break

Each one protects people's data or the app's trust, and several have already cost a
real bug. Breaking one is a bug even if every test passes. Code comments refer to
them by number ("rule 6").

### About people's data and trust

1. **Importers only add to people you already have.** An import enriches someone
   already in the address book (matched by name, by the last nine digits of a phone
   number, or by email). It creates new people only when you pass `--create-new`.
2. **Count only real two-way conversations.** Skip the names on the exclude list.
   Archive group chats only when they are small, and only for people you already
   have. Every importer must support `--dry-run`, which counts what would change and
   writes nothing.
3. **Importing twice never doubles anything.** Messages are stored once, with a key
   that catches repeats. A person's message count and last date are worked out from
   the messages the app can actually show, never added up from import to import. An
   import only adds messages; the one thing that removes them is deleting a person
   for good.
4. **A better source of a fact always wins.** In order, best first: typed by you
   (`manual`), then written by the AI or by the LinkedIn profile reader, then read
   from messages or from LinkedIn's export, then guessed by rules from phone
   numbers, email addresses and job titles. A later guess never overwrites a better
   source. Location fields are written only through `db.set_field`.
5. **A better source of a photo always wins.** A picture you set yourself, then
   LinkedIn, then your address book, then WhatsApp or Google. An import never
   replaces a photo you set by hand.
6. **There are two ways in to what macOS protects, and the simple one leads.** iMessage,
   WhatsApp, Apple Contacts and Apple Calendar can each be brought in two ways:

   - **With the sync helper**, the simple route the Data page leads with: one small
     program holds Full Disk Access and copies the files into the app's own folder
     every couple of minutes. Install it once from the Data page, grant it once in
     System Settings, then there is nothing more to do.
   - **Without it**, the route that needs no permission: you copy the file yourself
     in Finder (or export a `.vcf` or `.ics`) and the app reads your copy. It must
     keep working, and each source's row on the Data page must describe it.

   **The switch is the helper itself.** On: install it and grant it Full Disk
   Access. Off: **Turn off automatic copying** on the Data page, which runs
   `packaging/uninstall_imessage_sync.sh`, or switch its access off in System
   Settings. Nothing else needs setting: the app reads, from the helper's own
   report, which route produced each copy.

   The permission is only ever given to the small helper program, **never to
   Network CRM.app and never to Terminal**, and the helper must stay small enough to
   read in full: Foundation only, no network, no other programs started
   (`tests/test_packaging.py::test_the_helper_still_has_no_network_and_no_subprocesses`).
7. **`archive.db` never leaves the Mac as a file.** It holds the text of your
   messages. (Ask's message search does send short excerpts to Anthropic when you
   have added a key; the README says so.)
8. **The app must work with no keys at all.** With no Anthropic key, Ask falls back
   to a built-in search and answers in the same shape. Both halves live in
   `crm/api_ask.py` so the fallback stays in one place.
9. **Database changes go in `ensure_columns` and nowhere else.** It runs every time
   the app starts and only adds what is missing. People must never be asked to run a
   migration step.
10. **Never commit `data/` or `secrets/`.** Both are ignored by git, with extra
    patterns for `*.db`, `*.npz` and exported `Connections*.csv` as a second net.

### About how the code is arranged

- **The core uses only what comes with Python.** Adding a runtime dependency is a
  real decision, not a detail. Anything a shipped feature imports must be listed in
  `requirements.txt`, because the packaged app builds its own packages from that
  list and a missing line makes a feature quietly stop working
  (`tests/test_packaging.py` and `tests/test_dev_parity.py` check).
- **No build step for the page, and nothing loaded from elsewhere.** No npm, and no
  content delivery network (a CDN: files fetched from someone else's servers). One
  module per screen, plus the one library kept inside the repository.
- **Engine modules are flat siblings in `crm/`.** No `crm/api/` sub-folder, and
  modules import each other as `import db`, never `from crm import db`. The tests
  and the packaged app both start the engine in a way that depends on this.
- **When a name moves out of `crm/app.py`, re-export it there.** The route tables
  and many tests reach handlers through `app`. Tests must **patch the module that
  owns a thing**, for example `mock.patch.object(app.api_contacts, "EXCLUDE_FILE", p)`,
  never assign `app.EXCLUDE_FILE = …`: that changes only `app`'s own copy, and the
  test can end up making a real, billed AI call while still passing.
- **Do not turn `crm/db.py` into a package.** About twenty tests swap its database
  path while running; behind a package the swap silently misses and the suite writes
  invented people into the real database.
- **`crm/api_views.py` stays disposable.** It only combines answers from other
  modules; no queries live in it.
- **Every test file imports `tests.helpers` first** (see [The tests](#the-tests)).
- **The page's style and script files are served with a fingerprint** (an `ETag`),
  so a shipped update is never hidden by an old cached copy. Do not remove it.
- **The engine listens on this Mac only.** To reach it from another device you set
  `APP_HOST`, and then it refuses to start without `APP_TOKEN`, a password for
  every request. Do not weaken that.
- **Every colour, size and font in the stylesheets comes from `tokens.css`.**
  Change the look in one reversible file, not across many.
- **The packaged app builds its code without writing into itself.** It sets
  `PYTHONDONTWRITEBYTECODE=1` so it never scatters cache files through its own
  bundle, which would invalidate a signature and make a read-only install fail.

---

## Build the Mac app

It is built with Apple's command line tools (a free download that gives you the
Swift compiler, without the full Xcode). Install them once with
`xcode-select --install`. The build never needs the network.

### Network CRM.app

```bash
packaging/appify.sh --embed
```

What it does: compiles the window program from `native/Sources/`, compiles and
signs the copying helper, copies `crm/` and the three `requirements` files into
the app, puts a copy of Python inside it, seals the whole app, and then runs the
app's own check on it (`--engine-check`, below). The app lands in
`packaging/dist/`, which git ignores. Drag **Network CRM.app** to Applications.

Things worth knowing:

- **Put a copy of Python in place first.** The build never downloads anything.
  Download an `install_only` build named
  `…-aarch64-apple-darwin-install_only.tar.gz` from the
  [python-build-standalone releases](https://github.com/astral-sh/python-build-standalone/releases)
  and drop it in `packaging/.cache/` before you build. Without it the build stops
  and says so, because the app starts its engine on that Python and nothing else.
- **Without the command line tools the build stops too**, since the window is
  the app. `native/compile.sh` picks the toolkit: the macOS 26.5 one under the
  command line tools first, because the newer one needs full Xcode, with its
  licence accepted, before SwiftUI's `@State` compiles; otherwise whatever it
  finds, and it says which.
- **The app is not signed by a known developer.** macOS blocks the first launch.
  Click **Done**, then open **System Settings → Privacy & Security**, click **Open
  Anyway** beside the line about Network CRM, and confirm. (Right-clicking and
  choosing Open stopped working in macOS 15.) If it says the app is "damaged",
  clear the quarantine mark with the `xattr` line in
  [`packaging/README.md`](../packaging/README.md#first-launch-the-app-is-not-signed-with-a-developer-id).
  Signing with your own Apple developer certificate is described there too.
- **A rebuild is needed to see a code change in the installed app.** Quit it and
  open the new build; the engine goes and comes back with the app.
- `packaging/appify.sh` with **no** flag makes a build that runs the engine's code
  straight out of your folder, on your folder's `.venv`. That is handy for trying
  a change, but it stops working when the folder moves, and it still uses the real
  data folder. Never hand one to someone else.
- `packaging/make_dmg.sh` puts the built app in a disk image
  (`packaging/dist/NetworkCRM.dmg`) with a "Read Me First" note beside it. Build
  with `--handout` first.

[`packaging/README.md`](../packaging/README.md) has every option and the history of
why it is built this way.

### Network CRM Native.app, the developer copy of the window

```bash
native/build.sh
```

The same window, from the same `native/Sources/`, built quickly into
`native/dist.noindex/Network CRM Native.app` without copying code or Python in. Opened
from Finder it starts the engine from your folder's `crm/` on your folder's
`.venv`, against the real data folder, so quit Network CRM first. Started from
Terminal with `NC_NATIVE_PORT=<port>` (an engine you started from source) or
`NC_NATIVE_SOCKET=<file>`, it joins that engine instead and stops nothing when it
quits. That is how the checks below use it.

- It needs an Apple Silicon Mac running macOS 26 or later, and the command line
  tools.
- It is built next to the finished app and swapped in by renaming, so a failed build
  leaves the last good app, and rebuilding while the app is open does not crash it.
- `native/dist.noindex/` is ignored by git. Its name ends in `.noindex` so
  Spotlight never lists this copy beside the real app; the disk image to hand out
  sits in `packaging/dist/handout.noindex/` for the same reason.

---

## Check a change against what actually ships

**The app people run is not the copy you test in.** Network CRM.app uses its own
Python and its own private packages, in `~/Library/Application Support/NetworkCRM/venv`.
Your development folder has a different set. A change can pass every test and every
click in your copy and still fail in the app. The clearest example: if the library
Ask needs is missing from the app's packages, Ask silently answers from its
built-in search while everything you see in your copy looks healthy.

So for anything that matters:

1. **Run the tests**, as above.
2. **Build the app**: `packaging/appify.sh --embed`, and read the output for the
   `bundled CPython` line.
3. **Run `packaging/preflight.sh`.** It only reads; it changes nothing. It compares
   your development Python with the app's, checks the built app carries exactly the
   code in your folder (so it is not stale), checks the app's packages can import
   everything `requirements.txt` promises, and prints PASS or FAIL.
4. **See it as a stranger would.** Opening the built app on your own Mac proves
   little if you already use it: it runs on your own data, already full of people,
   so the first-run experience never appears. The section
   [Proving a build works on a Mac that has never seen this app](../packaging/README.md#proving-a-build-works-on-a-mac-that-has-never-seen-this-app)
   has a short recipe that runs the app's own Python on the app's own code, with a
   throwaway data folder and a spare port (on purpose, so `curl` can reach it), then
   lists three checks to run against it.
   It is built so that an empty first run must stay empty until you say yes.
5. **Ask which Python answered.** The engine's reply carries a `Server:` header that
   includes the Python version:

   ```bash
   curl -s -D - -o /dev/null http://127.0.0.1:8900/api/status | grep -i '^server:'
   ```

   It prints something like `Server: NetworkCRM/1.0 Python/3.12.9`.

   If it is not the interpreter you meant to test, you have tested the wrong thing.

Be careful when experimenting with the built app. It always uses the real data
folder, however it is opened: it clears every setting from the Terminal that
started it before it starts its engine. Use the throwaway recipe above for
anything that writes.

---

## How the app checks itself

The app can test itself without showing a window, a Dock icon, or asking for
permission to record your screen. You run its program from Terminal with a flag.
The first three checks below refuse to run unless you say which engine to use,
with `NC_NATIVE_PORT` (a port) or `NC_NATIVE_SOCKET` (a private file):

```bash
APP="native/dist.noindex/Network CRM Native.app/Contents/MacOS/NetworkCRMNative"
NC_NATIVE_PORT=8900 "$APP" --selftest
```

| Flag | What it does | Why it is safe |
|---|---|---|
| `--selftest` | Asks the engine for every read-only answer the app uses and checks each one decodes exactly as expected. Prints `PASS` or `FAIL` and a count for each. | It only reads, and it prints counts and the names of the parts that failed, never a person's details. Routes where merely asking would write something are left out on purpose. |
| `--snapshot <folder>` | Draws every screen offscreen, in light and dark, and saves pictures (`people-light.png` and so on) to the folder. `NC_SNAPSHOT_ONLY=people,person` limits it to the scenes you name. | It never opens a window, and it refuses to run without `NC_NATIVE_PORT`, so it only ever touches an engine you named. |
| `--selftest-writes` | Tries every action that changes something: add a note, make a list, edit a person. | It refuses to run unless the engine is on **port 8768**, which should be an empty throwaway engine like the one below, or on a private file whose engine says its data is in a temporary folder. It also refuses to send a question to Ask unless that engine reports no AI key, so it can never spend money. |
| `--engine-check` | Proves the way the app starts its engine: it starts one from inside the app on a throwaway folder, with automatic imports off, and checks the engine answers, opened no network port, keeps its private file to your account, stops by itself when the app vanishes, and leaves nothing running after a quit. Every `--embed` build runs it as its last step. | It never touches the real data folder. Run it on the built app: `"packaging/dist/Network CRM.app/Contents/MacOS/NetworkCRM" --engine-check`. |

For the checks that write, start an empty throwaway engine on the port the check
insists on:

```bash
NC_NO_BROWSER=1 NC_AUTOSYNC=0 APP_PORT=8768 \
  CRM_DATA_DIR="$HOME/ncrm-dev/empty-data" CRM_SECRETS_DIR="$HOME/ncrm-dev/empty-secrets" \
  .venv/bin/python3 crm/app.py
# then, in another Terminal window:
NC_NATIVE_PORT=8768 "$APP" --selftest-writes
```

A few checks need situations a real engine cannot stage on demand: an Ask answer that
takes twenty seconds so Stop can be pressed, or an engine that goes quiet and comes
back. `native/tools/standin.py` is a stand-in engine with made-up answers for those.
Start it with `python3 native/tools/standin.py 8869`, and run `--selftest` against
port 8869, which the self-test recognises.

---

## Where to read more

- [`ARCHITECTURE.md`](../ARCHITECTURE.md): the data model, the importer rules, how
  Ask works, what leaves your Mac and when, and the recipe for a new import source.
- [`README.md`](../README.md): setup and what each part of the app is for.
- [`packaging/README.md`](../packaging/README.md): building, signing and proving the
  Mac app, and what happens when it opens. [`packaging/IMESSAGE_SYNC.md`](../packaging/IMESSAGE_SYNC.md): the copying
  helper and the permission it needs.
- [`native/API_CONTRACT.md`](../native/API_CONTRACT.md): the engine answers the
  app relies on.
- [`docs/DESIGN_DIRECTION.md`](DESIGN_DIRECTION.md): how the app should look.
- [`CHANGELOG.md`](../CHANGELOG.md): what has changed.
- [How to remove Network CRM completely](UNINSTALL.md): every place the app puts
  something on a Mac, and the order to take it out in.
