# How to remove Network CRM completely

This is a checklist in the style of an app remover. It lists everything Network
CRM puts on your Mac, says what each thing is in plain words, gives the order to
take it out in, and ends with one block you can paste into Terminal if you would
rather not click through it. It covers the Mac app, **Network CRM**, and the two
companions older versions came with: **Quit Network CRM** and **Network CRM
Native**. (CRM is short for *customer relationship management*: software that
remembers the people you deal with. Here it is for your own network.)

> **Removing the data cannot be undone.** The app's folder holds every person,
> note, message and photo you gave it, and your saved keys. Once you empty the
> Trash, or run the Terminal block, they are gone. Back them up first if there is
> any chance you will want them ([step 4](#step-4-back-up-your-data-if-you-might-want-it)).

**The button that does most of it.** Network CRM's **Settings** page ends with
**Remove everything**. It does steps 1 and 6 for you, in that order: it turns the
copying helper off, and only if that worked does it delete the support folder and
the log folder. It shows the list of what it will delete and asks you to type the
word *remove* first, then stops the engine. It does not delete the apps themselves
(step 5) or the small leftovers (step 7), and no app can do steps 3 and 8: macOS
does not let a program edit its own Full Disk Access entry, and access you gave
Google or LinkedIn can only be taken back on their side. Use step 10 afterwards to
check what is left.

(Corrected: an earlier version of this page said the button "may do some of the
deleting in steps 5 to 7". It does steps 1 and 6, and never deletes the apps.)

**On this page**

- [What is on your Mac](#what-is-on-your-mac)
- [The order to remove things in](#the-order-to-remove-things-in), ten steps
- [The shortcut: one block for Terminal](#the-shortcut-one-block-for-terminal)
- [If you ran it from the source code](#if-you-ran-it-from-the-source-code)
- [If you set up the LinkedIn profile reader](#if-you-set-up-the-linkedin-profile-reader)
- [Putting it back later](#putting-it-back-later)

---

## What is on your Mac

Three words used below:

- **The engine** is the part of the app that does the work. It is a small Python
  program the app starts when it opens and stops when it quits. The window shows
  you what it knows.
- **The support folder** is `~/Library/Application Support/NetworkCRM`. The `~`
  means your own home folder. It is where nearly everything lives.
- **The copying helper** is an optional small program named
  `networkcrm-imessage-sync`. If you set up automatic updates on the Data page,
  it copies your Messages, WhatsApp, Contacts and Calendar into the support
  folder every two minutes. It is the one thing that keeps running when the app
  is closed.

### The programs

| What | Where it usually is | What it is |
|---|---|---|
| **Network CRM.app** | `/Applications` | The app you open. It carries the engine's code and its own copy of Python inside it, so removing the app removes those too. |
| **Quit Network CRM.app** | next to it, from an older version | A tiny app that stopped the engine if the app was force-quit. Versions since 17 October 2026 stop their engine themselves and no longer come with it. It holds nothing. |
| **Network CRM Native.app** | `/Applications`, from an older version | A trial of the window Network CRM now has. It kept **no people or messages of its own**. |
| A disk image, **NetworkCRM.dmg** | wherever you downloaded it | The file the app came in. Not needed once the app is in Applications. |

### What they keep in the support folder

Everything below is inside `~/Library/Application Support/NetworkCRM`.

| What | What it is | Removed in step |
|---|---|---|
| `data/` | **Your information.** See the list under this table. | 6 |
| `secrets/` | **Your keys and sign-ins.** See the list under this table. | 6 |
| `venv/` | The app's own private set of add-on Python packages (a *virtual environment*): the library for Ask, Google's libraries for Gmail, and similar. If you turned on meaning-based search it also holds PyTorch (a large machine-learning toolkit) and its companions, about 2 gigabytes (GB). Harmless to delete; the app rebuilds it if you ever reinstall. | 6 |
| `bin/` | The copying helper itself, and a small note of which version of it is installed. | 1 and 6 |
| `imessage/`, `whatsapp/`, `addressbook/`, `calendar/` | The copying helper's copies of Messages, WhatsApp Desktop, Contacts and Calendar. They are **complete copies of other apps' data**, kept without the protection macOS gives the originals. | 6 |
| `sync-status.json`, `.sync-cursor.json` | The helper's report of its last run, and its place-marker. | 6 |
| `run/` | While the app is open, the private file the window and the engine talk through. Only your account can open it, and it goes when the app quits. | 6 |
| `networkcrm.pid` | Older versions only: a note of the engine's process number, so Quit could find it. | 6 |

**Inside `data/`** (you may see only some of these, depending on what you used):

| File or folder | What it is |
|---|---|
| `crm.db` | Your address book: every person, your notes, your lists, closeness scores, saved Ask chats and the app's own switches. This is the part you typed. Short-lived companion files with `-wal` and `-shm` on the end belong to it. |
| `archive.db` | The text of your messages from iMessage, WhatsApp, LinkedIn and email, plus a search index. The biggest and most private file, and some of it cannot be fetched again: LinkedIn shows the app only its most recent messages. |
| `photos/` | Profile pictures. |
| `crm_vectors.npz`, `chunk_vectors.npz` | The index for meaning-based search, only if you turned that on. |
| `exclude_list.txt` | Names you told the app to skip. |
| `calendar/` | Calendar files fetched from a link you pasted. |
| `imessage_unmatched.csv` | Only if you ran the iMessage importer from Terminal with its logging option: the phone numbers and addresses it could not match to anyone you have. |
| `linkedin-token.json` | A small note of how long your LinkedIn token has left (not the token itself). It is there even if you never used LinkedIn. |
| `scrape_state.json`, `li_raw/`, `linkedin_messages.db` | Only if you used the LinkedIn profile reader: its progress, the pages it saved, and an older copy of LinkedIn messages. |
| `deps_status.json` | A note left when the optional add-ons could not finish installing. |

**Inside `secrets/`:**

| File or folder | What it is |
|---|---|
| `.env` | The settings and keys you typed into the Settings page: your Anthropic key for Ask, your LinkedIn token and its client details, your Gmail addresses, your name and email, and any **secret calendar links** (see step 8: anyone holding such a link can read that whole calendar). |
| `gmail_credentials_*.json` | The sign-in file you made at Google Cloud Console so the app can ask Google for your mail. |
| `gmail_token_*.json`, `google_photos_token_*.json` | The permission Google gave the app after you pressed Allow. Deleting the file does **not** take the permission back at Google (step 8). |

### Elsewhere in your Library

| What | Where | What it is | Removed in step |
|---|---|---|---|
| Logs | `~/Library/Logs/NetworkCRM/` | `server.log` (what the engine said), `imessage-sync.log` (the helper's), and from older versions `native-engine.log` (Native's). They can name people the app had trouble with. | 6 |
| The helper's schedule file (a *launch agent*) | `~/Library/LaunchAgents/io.github.william-agehouse.networkcrm.imessagesync.plist` | A small file that tells your Mac to run the copying helper at login and every two minutes after. This is what keeps it running with the app closed. | 1 and 7 |
| Settings files, caches and saved window state | `~/Library/Preferences`, `~/Library/Caches`, `~/Library/WebKit`, `~/Library/HTTPStorages`, `~/Library/Saved Application State` | Each app leaves small files named after its identity (see the next table): where its window was and what you chose in it. Versions before 17 October 2026 showed a web page in their window and also kept a cache of it. Present only if the app got far enough to write them. | 7 |
| Crash reports | `~/Library/Logs/DiagnosticReports` | Only if an app ever crashed. | 7 |

**The identities.** Every Mac app has an identity, a short name that macOS files
its leftovers under (its *bundle identifier*). Here they are:

| App | Its identity |
|---|---|
| Network CRM | `io.github.william-agehouse.networkcrm` |
| Quit Network CRM (older versions) | `io.github.william-agehouse.networkcrm.quit` |
| Network CRM Native (older versions) | `io.github.william-agehouse.networkcrm.native` |
| The copying helper's launch agent | `io.github.william-agehouse.networkcrm.imessagesync` |

Every identity the apps have used contains the word `networkcrm`. That is why
step 7 and the Terminal block look for that word instead of a fixed list of
names: it also catches files left by an older build whose identity is not in the
table.

### Outside your Mac

These are not files on your disk. They are permissions you gave other services,
and only they can take them back (step 8):

- **Google**, if you connected Gmail or imported contact photos. The permission is
  read-only: the app can read mail and contacts, not send or delete.
- **LinkedIn**, if you pasted a token for LinkedIn messages, or ran the profile
  reader (which stays signed in to LinkedIn).
- **Anthropic**, if you added a key for Ask. The key keeps working, and keeps
  costing you if anyone else gets it, until you delete it at Anthropic.
- **Calendar links**, if you pasted a secret Google or iCloud calendar address.

### What Network CRM does not do

Worth knowing, because a remover would otherwise check for it. In the app's code
there is nothing that stores anything in your Keychain (the Mac's password
store) or adds a login item (a program that opens when you log in), and nothing that
asks for an administrator password. Apart from the app
files themselves, everything goes in your own home folder. You never need `sudo`
to remove it, and you should not use it here.

---

## The order to remove things in

The order matters for two reasons. The copying helper recreates the support folder
every two minutes for as long as it is installed, so deleting the folder first
just brings it back. And the engine holds the databases open, so copying or
deleting them while it runs gives you a half-written copy.

So: **turn the helper off, quit, take its Full Disk Access entry out, back up,
then delete.**

Finder hides your Library folder. To open any path below, choose **Go > Go to
Folder…** (or press Shift-Command-G), paste the path and press Return.

### Step 1. Turn the copying helper off

Skip this step if you never set up automatic updates.

**In the app (easiest).** Open Network CRM, go to **Data**, find the **Full Disk
Access** panel, and press **Turn off automatic copying**. The app asks you to
confirm first. This stops the helper and deletes its launch agent and its program.

**Without the app.** If you have already deleted the apps, or they will not open,
use [the Terminal block](#the-shortcut-one-block-for-terminal) now. It turns the
helper off by itself before it deletes anything. Then come back for steps 3, 8
and 9, which it cannot do.

### Step 2. Quit the app

Quit **Network CRM** with Command-Q. That stops its engine and everything the
engine started; even a force-quit leaves nothing running behind it.

An older version (one that came with **Quit Network CRM**) could leave its engine
running after a force-quit. If you had one, open **Quit Network CRM** once, or
paste this line into Terminal, which does the same job:

```bash
pkill -f crm/app.py
```

### Step 3. Take the helper's Full Disk Access entry out

Skip this step if you never set up automatic updates.

*Full Disk Access* is a switch in System Settings that lets one program read
everything on your disk, including Messages. Network CRM never held it. The
copying helper did, and macOS does not let a script remove that entry for you.

1. Open **System Settings > Privacy & Security > Full Disk Access**.
2. Select **networkcrm-imessage-sync**.
3. Press the **−** button under the list and confirm with your password or Touch ID.

Once the helper's program is deleted the entry does nothing, but it will sit in
the list looking like a live permission until you remove it. If you also see
**Network CRM** or **Terminal** in that list and you added them for this app,
remove them too. The app never asks for either.

### Step 4. Back up your data, if you might want it

Skip this step if you are sure you do not.

The folder to keep is `data` inside the support folder. It holds everything you
typed and everything the app learned. **Do not rely on the app's Export:** it
writes your people and notes as readable files but leaves out the messages and the
photos.

In Finder: go to `~/Library/Application Support/NetworkCRM`, then drag the `data`
folder to somewhere outside it while holding the Option key (which copies instead
of moves). Check its size first with **File > Get Info**; with years of messages
it can run to several gigabytes.

In Terminal, the same thing in one line (it makes `~/NetworkCRM-data-backup`):

```bash
ditto "$HOME/Library/Application Support/NetworkCRM/data" "$HOME/NetworkCRM-data-backup"
```

Two cautions. The copy holds your whole message history as plain files, so keep it
somewhere you would keep the originals, not in a shared or synced folder by
accident. And leave out `secrets/` unless you really want your keys: it is easier
and safer to make new ones later.

The helper's copies (`imessage/`, `whatsapp/`, `addressbook/`, `calendar/`) are
copies of data that still exists in Messages, WhatsApp, Contacts and Calendar, so
they need no backup.

### Step 5. Delete the apps

In Finder, open **Applications** and drag **Network CRM** to the Trash, and
**Quit Network CRM** and **Network CRM Native** too if an older version left them
there. Then:

- If you opened the app from a disk image, eject the **Network CRM** disk in the
  Finder sidebar and delete the `.dmg` file you downloaded.
- If you built the apps yourself, the originals are still in the `packaging/dist`
  and `native/dist.noindex` folders of your copy of the code. Deleting the folder (see
  [If you ran it from the source code](#if-you-ran-it-from-the-source-code))
  removes them.

### Step 6. Delete the data, secrets and logs

This is the step that cannot be undone. In Finder:

1. Go to `~/Library/Application Support`, and drag the **NetworkCRM** folder to
   the Trash. That one folder holds `data`, `secrets`, `venv`, `bin` and all the
   helper's copies.
2. Go to `~/Library/Logs`, and drag the **NetworkCRM** folder to the Trash.
3. Choose **Finder > Empty Trash**. Until you do, nothing is really gone.

### Step 7. Delete the small leftovers

Open each of these folders (Go to Folder…) and look for items whose name contains
`networkcrm`. Drag every one to the Trash, then empty it.

- `~/Library/LaunchAgents`: `…networkcrm.imessagesync.plist`, if step 1 did not
  already remove it.
- `~/Library/Preferences`: files ending `.plist`, one per app.
- `~/Library/Caches`, `~/Library/WebKit` and `~/Library/HTTPStorages`: folders
  named after each app's identity. Versions that showed a web page in their
  window kept its small stored settings and a cache of its files in `WebKit` and
  `Caches`.
- `~/Library/Saved Application State`: folders ending `.savedState`.
- `~/Library/Logs/DiagnosticReports`: crash reports, only if there are any.

Tip: in the Finder window for each folder, type `networkcrm` in the search box and
pick the button that carries the folder's name (not **This Mac**) under it, so you
see only the matches.

If you turned on meaning-based search, it also downloaded a small language model
(about 90 megabytes, MB) that other programs may share:
`~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2`. Delete
that one folder if you want it gone. Leave the rest of `~/.cache/huggingface`
alone.

### Step 8. Take back the access you gave other services

Deleting a file here does not cancel a permission you gave elsewhere. Menus on
these sites move around, so look for the idea (third-party apps, permitted
services, keys) rather than the exact wording.

- **Google (Gmail, contact photos).** Sign in at `myaccount.google.com/permissions`
  (Google may redirect it to a page called *third-party apps and services* under
  Security). Remove the entry named after the project you made at Google Cloud
  Console. If you want to go further, delete that project there too, which also
  deletes the sign-in file you made.
- **LinkedIn.** The messages token expires by itself, but you can end it now: in
  LinkedIn, open **Settings & Privacy > Data privacy** and look for **Permitted
  services**, and remove the app you made. You can also delete the app at
  `linkedin.com/developers`. If you ran the profile reader, sign its session out
  from LinkedIn's list of places you are signed in.
- **Anthropic.** Delete the key you added for Ask in the developer console where
  you made it. Deleting it there is what makes it stop working. Anything Ask
  already sent (your question and the excerpts it found) cannot be recalled.
- **Calendar links.** If you pasted a secret Google calendar address, open that
  calendar's settings in Google Calendar and reset its secret address. If you
  pasted an iCloud link, stop sharing that calendar publicly in the Calendar app.

### Step 9. Delete the files you made yourself

Nothing here can know about files you saved by hand for the app. Search Finder
for these and delete the ones you do not need, since they can hold the same
information as the app did:

- exports you made for it: LinkedIn's data archive and its `Connections.csv`, a
  Contacts export (a `.vcf` file), a calendar export (a `.ics` file), an email
  export (a `.mbox` file), your own copy of Messages' `chat.db` or WhatsApp's
  `ChatStorage.sqlite`, and anything you used the app's Export to make;
- the sign-in file you downloaded from Google Cloud Console, usually named
  `client_secret_….json`, which stays in Downloads after the app copies it;
- your backup from step 4, once you no longer want it.

Time Machine, or any other backup you keep, may also hold older copies of the
support folder. This guide does not touch those.

### Step 10. Check that nothing is left

- Open **System Settings > General > Login Items & Extensions**. Under **Allow in
  the Background** there should be nothing for Network CRM or the helper.
- Open **Full Disk Access** again: no `networkcrm-imessage-sync`.
- In Terminal, this should print nothing:

  ```bash
  ls -d "$HOME/Library/Application Support/NetworkCRM" "$HOME/Library/Logs/NetworkCRM" 2>/dev/null
  ```

---

## The shortcut: one block for Terminal

> ### WARNING: THIS DELETES EVERYTHING AND CANNOT BE UNDONE
>
> The block below **permanently deletes every person, note, message, photo and
> saved key** that Network CRM holds, along with the app and any companions an
> older version left. It does not use the
> Trash, so there is no way to get anything back. **If you want any of it, do
> step 4 (the backup) first and check that the backup opened.**
>
> It asks you to type the word `DELETE` before it removes anything.

What it does, in order: refuses to run while the app is open; asks you to
confirm; stops an engine an older version left running; turns off and deletes any launch agent with
`networkcrm` in its name (that includes the helper's); deletes the apps from
`/Applications` and `~/Applications` (the app, and the two companions of older
versions); deletes the support folder and the logs
folder; deletes the settings files, web caches and saved state that contain
`networkcrm`; and finally lists anything it could not remove.

What it does **not** do, because no script can: take the helper's entry out of
Full Disk Access (step 3), take back access you gave Google, LinkedIn or
Anthropic (step 8), or delete files you saved yourself (step 9). It also does
not make a backup.

To use it: quit the app (Command-Q), open **Terminal** (in Applications >
Utilities, or press Command-Space and type Terminal), paste the whole block, and
press Return. Do not put `sudo` in front of it; it refuses to run as an
administrator.

```bash
/bin/bash <<'REMOVE_NETWORK_CRM'
set -u

# --- Checks before anything is touched -------------------------------------
if [ "$(id -u)" = "0" ]; then
  echo "Do not run this with sudo or as the administrator. It only touches your own account." >&2
  exit 1
fi
_canon() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
REAL_HOME="$(eval echo "~$(id -un)")"
if [ "$(_canon "$HOME")" != "$(_canon "$REAL_HOME")" ]; then
  echo "HOME is $HOME, but this account's home is $REAL_HOME. Nothing was changed." >&2
  exit 1
fi
# The app's program, and those of older versions (NetworkCRMWindow, NetworkCRMNative).
if pgrep -f 'Contents/MacOS/NetworkCRM' >/dev/null 2>&1; then
  echo "Network CRM is still open. Quit it with Command-Q, then paste this block again." >&2
  exit 1
fi

SUPPORT="$HOME/Library/Application Support/NetworkCRM"
LOGS="$HOME/Library/Logs/NetworkCRM"

# --- The loud question ------------------------------------------------------
echo
echo "THIS PERMANENTLY DELETES NETWORK CRM AND EVERYTHING IT HOLDS:"
echo "every person, note, message, photo and saved key. IT CANNOT BE UNDONE."
if [ -d "$SUPPORT/data" ]; then
  echo "Your data folder is $SUPPORT/data ($(du -sh "$SUPPORT/data" 2>/dev/null | cut -f1))."
fi
printf 'If you are sure, type DELETE and press Return: '
read -r answer < /dev/tty || answer=""
if [ "$answer" != "DELETE" ]; then
  echo "Nothing was removed."
  exit 1
fi
echo

# --- A. Stop an engine an older version left (only one really crm/app.py) ---
# The app's own engine stops when the app does, so with the app closed there is
# none. Older versions kept a process-number note and listened on port 8765.
pids=""
[ -f "$SUPPORT/networkcrm.pid" ] && pids="$(cat "$SUPPORT/networkcrm.pid" 2>/dev/null)"
pids="$pids $(lsof -ti tcp:8765 -sTCP:LISTEN 2>/dev/null)"
for pid in $pids; do
  case "$pid" in ''|*[!0-9]*) continue ;; esac
  if ps -ww -p "$pid" -o command= 2>/dev/null | grep -q 'crm/app.py'; then
    kill -TERM "$pid" 2>/dev/null && echo "Stopped the engine (process $pid)."
  fi
done
sleep 2

# --- B. Turn off and delete background jobs named after the app -------------
# This is the copying helper's launch agent. It would also remove the LinkedIn
# profile reader's launch agent if that ran on this Mac.
find "$HOME/Library/LaunchAgents" -maxdepth 1 -iname '*networkcrm*.plist' 2>/dev/null |
while IFS= read -r plist; do
  label="$(plutil -extract Label raw -o - -- "$plist" 2>/dev/null)" || label=""
  [ -n "$label" ] || label="$(basename "$plist" .plist)"
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1
  launchctl unload "$plist" >/dev/null 2>&1
  rm -f "$plist" && echo "Turned off and removed the background job $label"
done

# --- C. The apps ------------------------------------------------------------
for dir in /Applications "$HOME/Applications"; do
  for app in "Network CRM.app" "Quit Network CRM.app" "Network CRM Native.app"; do
    [ -e "$dir/$app" ] || continue
    if rm -rf "$dir/$app"; then
      echo "Removed $dir/$app"
    else
      echo "Could not remove $dir/$app. Drag it to the Trash in Finder instead." >&2
    fi
  done
done

# --- D. The data, secrets, helper copies, private Python packages and logs --
for path in "$SUPPORT" "$LOGS"; do
  [ -e "$path" ] || [ -L "$path" ] || continue
  if rm -rf "$path"; then
    echo "Removed $path"
  else
    echo "Could not remove $path" >&2
  fi
done

# --- E. Settings files, web caches, saved window state, crash reports -------
for sub in Preferences Caches HTTPStorages WebKit "Saved Application State" "Logs/DiagnosticReports"; do
  [ -d "$HOME/Library/$sub" ] || continue
  find "$HOME/Library/$sub" -maxdepth 1 -iname '*networkcrm*' 2>/dev/null |
  while IFS= read -r item; do
    rm -rf "$item" && echo "Removed $item"
  done
done

# --- What is left -----------------------------------------------------------
left="$(
  for path in "$SUPPORT" "$LOGS"; do
    [ -e "$path" ] && echo "$path"
  done
  for dir in /Applications "$HOME/Applications"; do
    for app in "Network CRM.app" "Quit Network CRM.app" "Network CRM Native.app"; do
      [ -e "$dir/$app" ] && echo "$dir/$app"
    done
  done
  for sub in LaunchAgents Preferences Caches HTTPStorages WebKit "Saved Application State"; do
    find "$HOME/Library/$sub" -maxdepth 1 -iname '*networkcrm*' 2>/dev/null
  done
)"
echo
if [ -z "$left" ]; then
  echo "Done. Nothing this block looks for is left."
else
  echo "Done, except these could not be removed (try the Trash in Finder):"
  echo "$left"
fi
echo
echo "Three things no script can do. Do them yourself:"
echo "  1. System Settings > Privacy & Security > Full Disk Access:"
echo "     remove 'networkcrm-imessage-sync' with the minus button."
echo "  2. Take back access you gave Google, LinkedIn and Anthropic, and any"
echo "     secret calendar links (step 8 of the guide)."
echo "  3. Delete files you saved yourself for the app: exports, your backup,"
echo "     and the sign-in file from Google Cloud Console (step 9)."
REMOVE_NETWORK_CRM
```

If a line says **Operation not permitted** about one of the apps, macOS has
refused to let Terminal remove it. Drag that app to the Trash in Finder instead,
and do not give Terminal extra permissions just for this.

---

## If you ran it from the source code

If you started Network CRM from a downloaded or cloned folder of its code (with
`start.command` or `python3 crm/app.py`), its data and keys are not in the support
folder. They sit inside that folder instead (the support folder exists only if you
also used one of the apps or the copying helper):

- `data/` and `secrets/`, the same contents as listed above.
- `.venv/`, the private Python packages you made for it.
- `packaging/dist/` and `native/dist.noindex/`, if you built the apps there.

Deleting the whole folder removes all of it. If that folder's `data` is a shortcut
(a *symbolic link*) pointing into the support folder, deleting the folder removes
only the shortcut, so delete the support folder as in step 6 as well.

If you built Network CRM or its copying helper from source, the helper's launch
agent and Full Disk Access entry are exactly the same as above: steps 1 and 3
still apply.

## If you set up the LinkedIn profile reader

The profile reader is optional and advanced, and it often runs on a second Mac.
Wherever you ran it, that Mac holds more than the lists above:

- A launch agent named `com.networkcrm.liscraper.plist` in that Mac's
  `~/Library/LaunchAgents` (the Terminal block above removes it if it is on the Mac
  you run the block on). Otherwise turn it off with
  `launchctl bootout gui/$(id -u)/com.networkcrm.liscraper`, then delete the file.
- A copy of the code with its own `secrets/ff_profile` (a Firefox profile that is
  signed in to your LinkedIn) and `data/li_raw/` (the pages it saved). Delete both
  or the whole folder.
- Any key you made so the two Macs could log in to each other (an *SSH key*, kept
  in `~/.ssh`).

Then end the LinkedIn session from LinkedIn's list of places you are signed in.
[`LINKEDIN_SCRAPE_HANDOFF.md`](../LINKEDIN_SCRAPE_HANDOFF.md) describes what the
reader keeps.

## Putting it back later

Installing again starts from nothing. If you kept the backup from step 4, quit
the app, put the `data` folder back at
`~/Library/Application Support/NetworkCRM/data` before you open it for the first
time, and the app will use what is in it. You will need to turn automatic
updates, Gmail and the other connections on again, and grant Full Disk Access
again if you want the helper.

See also [`packaging/IMESSAGE_SYNC.md`](../packaging/IMESSAGE_SYNC.md), which
explains the copying helper and its permission in full, and
[`README.md`](../README.md#privacy--whats-shareable) for what to back up. If you
would rather change the program than remove it, see
[How to change Network CRM](EDITING.md).
