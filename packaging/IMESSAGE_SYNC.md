# Automatic iMessage sync (opt-in)

`crm/imessage_import.py` reads your iMessage history out of a copy of
`chat.db`. Getting that copy is the annoying part, because macOS protects the
original. There are two ways to do it, and **the manual one remains the
default** — nothing here is installed unless you run the installer yourself.

## The two options

**1. Manual (default, no permissions granted).** Quit Messages, Cmd+Shift+G to
`~/Library/Messages/`, copy `chat.db` to your Desktop, and pick it in the app's
Import panel. Nothing on your Mac is granted any special access. The cost is
that you do it by hand every time you want fresh data, so the numbers go stale
between imports — and the Finder copy grabs only `chat.db`, which silently
drops your most recent messages (see "The WAL detail" below).

**2. This helper (opt-in, one permission granted to one small binary).** A
background job keeps a readable copy up to date — every 2 minutes by default —
so imports are always current. It copies four things: iMessage, WhatsApp Desktop, your
address book (every account's), and Calendar's store, which holds every calendar on this
Mac because macOS offers nothing narrower. The cost is that you
grant Full Disk Access — once, by hand, to a single compiled binary that does
nothing but copy those databases.

### Do it from inside the app

**This is the normal route and the only one most people need.** The Data page
carries the whole flow, and so does the wizard's Messages step:

- **Set up automatic updates** installs the helper. No Terminal.
- **Open Full Disk Access settings** opens the exact System Settings pane, and
  **Show the helper in Finder** opens the folder with the binary already selected,
  so granting it is one drag.
- **Check now** runs the helper through launchd and reports what actually
  happened, per source — "iMessage blocked by macOS, WhatsApp already up to
  date". macOS offers no way to *ask* whether a grant exists, so trying the read
  and saying what happened is the only honest answer available.

Why this exists: the people the app is for use the packaged `.app` and do not
open a Terminal, so anything that lived only in this script's terminal output
would be invisible from inside the product. The
UI is `static/app/fda.js` over `crm/api_fda.py`; the status it renders comes from
the helper's own report (`sync-status.json`, see "The report" below).

### Or run the installer directly

Still supported, and what the app runs for you. Where it lives depends on what
you have:

```bash
# If you have Network CRM.app — the helper ships inside it, already
# compiled, so you need no developer tools.
"/Applications/Network CRM.app/Contents/Resources/imessage-sync/install_imessage_sync.sh"

# If you have a repo checkout, it compiles from source instead.
packaging/install_imessage_sync.sh
```

Shipping it prebuilt is deliberate, and not only for convenience: the Full Disk
Access grant is pinned to the binary's **code hash**, so a binary compiled
separately on every Mac would have a different hash on every Mac, and any
rebuild would silently drop the grant. One set of bytes, built once, means the
permission survives app updates until the helper's source genuinely changes.

## The report

After every run the helper writes what it did to

```
~/Library/Application Support/NetworkCRM/sync-status.json
```

```json
{"version": 1, "ran_at": 1700000000.0, "exit": 2,
 "binary": "…/bin/networkcrm-imessage-sync",
 "sources": [{"label": "iMessage", "outcome": "blocked"},
             {"label": "WhatsApp", "outcome": "copied", "bytes": 1048576}]}
```

`outcome` is one of `copied`, `current`, `absent`, `blocked`. This file is the
only channel by which the app learns that the permission is missing, and each of
the alternatives was rejected for a concrete reason:

- **the exit code** is seen only by whoever launched the helper, which in normal
  operation is launchd, and the run is over by the time anyone asks;
- **the log** (`~/Library/Logs/NetworkCRM/imessage-sync.log`) is prose, and
  having the app screen-scrape it would make every reworded line a silent UI
  regression;
- **looking at the protected folders** is exactly what the app must never do —
  that is the whole reason this binary exists.

It lands inside the CRM's own Application Support folder, which the app already
reads for the database copies, so reporting needs no permission the app does not
already have. `tests/test_packaging.py::TestFdaHelperReportContract` holds the
filename, the LaunchAgent label and the binary path in agreement across the
Swift file, this installer and `crm/importers.py`.

## Verifying the grant: through launchd, never from a shell

**Running the helper directly from a terminal reports a false failure, even when
Full Disk Access is granted correctly.** macOS attributes a TCC check to the
*responsible* process, and for a binary spawned from a shell that is the terminal
— not the binary — so the helper's own grant does not apply and the read is
refused. The same Mac can give both answers, depending on how the helper is started:

```
launchctl kickstart (the real scheduled path) ->  copied chat.db
"$BIN" straight from bash                     ->  "still needs Full Disk Access"
```

So the installer must not close by executing the binary itself, nor tell the user
to "re-run this to confirm": a user who had just granted the permission correctly
would be told macOS was still blocking them, and the suggested confirmation would
say the same thing forever.

So verify one of these two ways:

- **In the app.** Data → the Full Disk Access panel → **Check now**. It kickstarts
  the LaunchAgent and reports what the run actually did, per source.
- **From a terminal, through launchd:**

  ```bash
  launchctl kickstart -k gui/$(id -u)/io.github.william-agehouse.networkcrm.imessagesync
  plutil -p ~/Library/Application\ Support/NetworkCRM/sync-status.json
  ```

`tests/test_packaging.py::TestFdaHelperReportContract` holds the installer to
this: it fails if the script goes back to executing the binary itself, or if the
printed steps tell a user to.

## What you are actually granting, and to what

`~/Library/Messages/chat.db` is readable **only** by a process holding Full
Disk Access (FDA). There is no narrower permission for it — no "Messages only"
toggle exists — and a file-picker does not help either: macOS's mandatory
access control overrides your own selection for that path, so even a file you
personally chose in a dialog stays unreadable.

So something has to hold FDA. The only question worth arguing about is *how
big that something is*. Here it is:

```
~/Library/Application Support/NetworkCRM/bin/networkcrm-imessage-sync
```

That is the exact and only thing you add to the FDA list. **Not** Network
CRM.app, **not** Terminal, **not** Python. The CRM itself never gets FDA and
never reads the protected path — it only ever opens the ordinary copy.

The binary is built from `packaging/src/imessage_sync.swift`. Read it before you
grant anything; that is the whole point of it being this short. It imports only
`Foundation`, has no networking of any kind (no URLSession, no sockets, no
subprocesses), touches no files other than the three `chat.db` files and its
destination directory, and accepts no arguments beyond an optional destination
path.

Be clear-eyed about the residual risk: a process with FDA *could* read anything
on the disk. Nothing macOS offers can narrow that. The mitigation is that this
particular process is small enough to audit in a couple of minutes, and you can
re-audit it whenever you like.

There is a second risk, and it is the one that should decide this for you: the
helper takes a destination directory as its argument, so **any** program running
as you can invoke it and have your entire message history written somewhere that
program can read — without holding Full Disk Access itself. Preventing exactly
that is what TCC is for, so installing this trades "only privileged code can read
Messages" for "anything on this Mac can, through here". Auditing the binary does
not address it; the binary behaves correctly in both cases.

Hardcoding the destination would remove the redirect but not the exposure, since
the copy still lands at a predictable path that anything running as you can read
(the section below is honest about that). If this risk is the deciding factor,
the manual copy/paste flow in the app grants nothing at all and is the better
trade — see the iMessage card under Data.

## Why a compiled binary and not a shell script

This matters more than it looks. macOS attaches the FDA grant to the process
that performs the read. For a shell script, that process is **the interpreter** —
`/bin/bash` — not your script. So "give Full Disk Access to my little sync
script" actually means "give Full Disk Access to bash", and from then on *any*
unsandboxed program on the machine can shell out to bash and read your entire
disk with it. It is one of the worst permissions you can hand out on macOS.

A compiled Mach-O binary gets its own grant, which nothing else inherits. That
is the only reason this is Swift instead of a two-line `cp`.

## The recompile caveat (read this one)

The installer ad-hoc signs the binary (`codesign -s -`), which gives it no stable
signing identity, so macOS pins the FDA grant to that binary's exact
**code hash**.

**Recompiling changes the hash, and macOS silently drops the grant.** It does
not warn you, and the helper stays sitting in the Full Disk Access list looking
healthy while the sync quietly fails. The only fix is to remove it and add it
again.

Two things reduce how often you hit this:

- The binary installs to a **stable path** in Application Support, never a build
  directory, so the path in your FDA list never goes stale.
- Re-running the installer does **not** recompile when
  `imessage_sync.swift` is unchanged — it compares a stored hash of the source
  first. So a routine re-run is safe and your grant survives it. It only
  rebuilds when the source genuinely changed or you pass `--force-rebuild`, and
  in that case it warns you on stdout that you must re-add the permission.

If sync stops working after you pull an update that touched the helper, this is
why. Symptom: `imessage-sync.log` shows the Full Disk Access message again.

## The WAL detail

`chat.db` is a SQLite database in **WAL mode**. Recent messages can live
entirely in the write-ahead log, `chat.db-wal`, and not yet be in `chat.db`
itself. Copying only `chat.db` therefore silently loses your newest history —
which is exactly the history you care about most in a CRM. On a normal Mac the
WAL routinely holds a couple of MB of messages.

The helper copies all three files together:

```
chat.db   chat.db-wal   chat.db-shm
```

Copying all three is necessary but **not sufficient**, which is easy to get
wrong: SQLite's `immutable=1` flag ignores a `-wal` entirely rather than replay
it, so a three-file copy read under `immutable=1` still returns only the
checkpointed rows. Measured on a 50-row WAL with all three files present:
`immutable=1` sees 1 row, `mode=ro` sees 51.

`imessage_import.snapshot_uri()` closes the gap by picking the flag per copy —
`mode=ro` when a non-empty `-wal` is beside the file, `immutable=1` when there
isn't (`mode=ro` cannot open a WAL-mode database with no log next to it, which is
exactly what a one-file Finder copy is). The original is never touched, and
neither is the copy: reading the log costs no writes at all.

> **Note.** This used to be done by `importers.fold_wal()`, which checkpointed the
> copy with `wal_checkpoint(TRUNCATE)` before importing. It read the right
> messages, but writing scattered pages *into an APFS clone* breaks copy-on-write
> block by block: a few MB of log cost several times that in real disk writes,
> on every sync cycle — gigabytes a day at a 2-minute cadence. Reading the log
> instead of folding it is what makes the fast cadence affordable (NC-028).

(The sidecars only exist between checkpoints; when one is genuinely absent, any
previous copy of it is deleted rather than left behind, since a stale `-wal`
next to a fresh `chat.db` is worse than no `-wal` at all.)

Two related details, for the record:

- Each file is copied to a temp name in the destination and then `rename(2)`d
  into place, so a reader never sees a half-written file — it gets either the
  previous copy or the new one.
- The three renames are not atomic *as a set*. If Messages checkpoints midway,
  the WAL you end up with can disagree with the `chat.db` you copied. SQLite
  detects that via the WAL header salt and ignores the mismatched WAL, so the
  worst case is a snapshot missing the last few minutes — never a corrupt one —
  and the next run picks them up.

Also worth knowing: `~/Library/Messages` cannot be *listed* without FDA even
though individual files inside it can be `stat`ed, so the helper looks the
sidecars up by exact name instead of globbing the directory. A shell glob like
`chat.db*` returns nothing there and would look like "no sidecars exist".

## Where things go

| What | Where |
| --- | --- |
| The binary | `~/Library/Application Support/NetworkCRM/bin/networkcrm-imessage-sync` |
| The copies | `~/Library/Application Support/NetworkCRM/` — `imessage/`, `whatsapp/`, `addressbook/`, `calendar/` |
| LaunchAgent | `~/Library/LaunchAgents/io.github.william-agehouse.networkcrm.imessagesync.plist` |
| Log | `~/Library/Logs/NetworkCRM/imessage-sync.log` |

The copies are complete: every message you have ever sent or received, every
contact and every calendar event. So the helper writes them `0600` in `0700`
directories. They are not encrypted: they are as sensitive as the originals,
and they now sit somewhere macOS does not guard. That is the actual trade you
are making by choosing convenience here.

## Cadence, and why it can be this fast

The agent runs at login and then **every 120 seconds (2 minutes)**. Override at
install time:

```bash
NC_IMESSAGE_SYNC_INTERVAL=600 packaging/install_imessage_sync.sh   # 10 minutes
```

A slow cadence (21600 seconds, 6 hours) would be the cautious choice if every run
re-copied both databases in full: on a large archive that is a lot of data per run,
and at 2-minute intervals it would add up to a very large amount of SSD writes a
day. Two things remove that cost, both in `src/imessage_sync.swift`:

- **`clonefile(2)`.** Source and destination are on the same APFS volume, so the
  copy is copy-on-write and nothing is written. On a large `chat.db`,
  `FileManager.copyItem` writes the whole file while `clonefile` writes only a
  few KB of metadata. The result is byte-identical and valid as a SQLite
  database. It falls back to a real copy if clonefile is refused (EXDEV,
  ENOTSUP).
- **`.sync-cursor.json`.** The helper records the size:mtime of each source part
  it copied FROM, and skips entirely when the source has not moved. It used to
  compare the source against the *destination*, and that was wrong even in
  principle — the question is "has the source moved", and only the source can
  answer it. Historically it was also wrong in practice, because the app rewrote
  the copy: `fold_wal()` checkpointed it, growing the main file and truncating
  the `-wal`, so every run saw "everything changed" — consecutive runs with a
  byte-identical main file each copied the whole thing. Nothing rewrites the copy
  any more, but the cursor stays: it is the correct comparison regardless.

Together, a run where no message arrived costs two `stat()` calls.

**Freshness is the sum of two intervals**, and turning down only one moves the
floor rather than the ceiling:

| | knob | default |
|---|---|---|
| helper notices a new message | `NC_IMESSAGE_SYNC_INTERVAL` (install time) | 120 s |
| app notices the fresher copy | `NC_AUTOSYNC_MINUTES` (Settings → Automatic imports) | 2 min |

Worst case ≈ 4 minutes.

Speeding up the app's check does **not** speed up Gmail or LinkedIn: each API
source is floored at one hour by `_api_hours()` and stamps its attempt before the
call, both against a persisted timestamp rather than the loop's sleep. Those two
properties are the only thing keeping a personal OAuth token off a 2-minute
timer — do not remove them.

Then point the importer at the copy:

```bash
CHAT_DB="$HOME/Library/Application Support/NetworkCRM/imessage/chat.db" \
  python crm/imessage_import.py
# or
python crm/imessage_import.py --chatdb "$HOME/Library/Application Support/NetworkCRM/imessage/chat.db"
```

## Granting the permission

macOS **never prompts** for Full Disk Access — it cannot be requested, only
added by hand. That is why the installer runs the helper once in the foreground
and, if it is blocked, opens the right System Settings pane for you. Expect the
first install to report a permission denial; that is the correct outcome, not a
failure.

1. System Settings > Privacy & Security > Full Disk Access
2. Click `+`, press Cmd+Shift+G in the dialog, paste
   `~/Library/Application Support/NetworkCRM/bin`, and select
   `networkcrm-imessage-sync`. (Dragging the binary into the list works too.)
3. Make sure its toggle is on.
4. Confirm, without reinstalling anything. Easiest: open Network CRM, go to
   **Data**, and press **Check now** in the Full Disk Access panel.

   From a terminal it has to go through launchd — see "Verifying the grant"
   above, and do NOT just run the binary, which reports a false failure no
   matter how correctly you granted the permission:
   ```bash
   launchctl kickstart -k "gui/$(id -u)/io.github.william-agehouse.networkcrm.imessagesync"
   plutil -p "$HOME/Library/Application Support/NetworkCRM/sync-status.json"
   ```
   Success is `"outcome" => "copied"` (or `"current"`) for both sources.

Exit codes: `0` copied, `2` blocked by Full Disk Access, `1` anything else.

## Checking on it

```bash
launchctl list | grep imessagesync          # 2nd column is the last exit status
tail -5 ~/Library/Logs/NetworkCRM/imessage-sync.log
```

Every log line is timestamped, because launchd appends to that file on every run
and adds no timestamps of its own. Runs where nothing changed say "source
unchanged since the last copy", which is what most of them will say.

## Uninstalling

```bash
packaging/uninstall_imessage_sync.sh
```

Unloads and deletes the LaunchAgent and deletes the binary. It **leaves the
copied data alone** — deleting your message history is not a
script's decision — and prints the path so you can `rm -rf` it yourself.

One step it cannot do for you: removing the FDA entry. macOS does not let
scripts edit that list (which is precisely why you had to add it by hand). The
dead entry is harmless once the binary is gone — it points at a file that no
longer exists — but it will sit in the list looking like a live permission until
you remove it with `-`.

Neither script needs `sudo`, and both refuse to run under it: everything lives
in your own home directory, and root-owned files in `~/Library` would just be a
cleanup problem later.
