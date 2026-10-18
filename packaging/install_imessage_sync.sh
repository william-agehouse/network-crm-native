#!/bin/bash
# ── Install the iMessage sync helper (opt-in) ────────────────────────────────
#
# Usage:
#   packaging/install_imessage_sync.sh                 # install / update
#   packaging/install_imessage_sync.sh --force-rebuild  # recompile even if the
#                                                       # source is unchanged
#
# Builds packaging/src/imessage_sync.swift into a small compiled binary at a
# STABLE path in Application Support, ad-hoc signs it, and registers a
# LaunchAgent that runs it every 2 minutes so the CRM always has a fresh,
# readable copy of chat.db AND of WhatsApp's ChatStorage.sqlite. Two minutes is
# affordable because the copy is an APFS clone and a run with no new message
# costs two stat() calls — see the INTERVAL comment below.
#
# The name still says "imessage" because the Full Disk Access grant is pinned to
# the binary's identifier and code hash: renaming it would orphan every grant
# already given, on every machine it is installed on, in exchange for a more
# accurate filename. WhatsApp comes through this binary too — reading its
# container live from Python raises a consent dialog every time the background
# loop runs — so the app needs no permission for either.
#
# You then grant Full Disk Access to that one binary — not to the CRM app, and
# not to bash. See packaging/IMESSAGE_SYNC.md for the full rationale.
#
# Never needs sudo, and refuses to run under it: everything lives in your own
# home directory, and a root-owned binary/plist in ~/Library would be both
# wrong and annoying to clean up.
#
# Safe to run repeatedly. By default it will NOT recompile a binary whose
# source hasn't changed, because recompiling changes the binary's code hash and
# macOS silently invalidates the Full Disk Access grant when that happens.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Two layouts, because this script now runs in two places.
#
#   Inside the app bundle (what a user has):
#       Contents/Resources/imessage-sync/{install_imessage_sync.sh,
#                                        networkcrm-imessage-sync}
#     appify.sh compiled and ad-hoc signed the binary at build time, so there is
#     nothing to compile here. That is the point: swiftc lives in the Xcode
#     Command Line Tools, which a user has no reason to have installed, and a
#     script that needed them would die on exactly that. It also keeps the Full Disk Access
#     grant stable — the grant is pinned to the binary's code hash, so one set of
#     bytes shipped to everyone beats a per-machine compile whose hash differs.
#
#   Inside the repo (a developer checkout):
#       packaging/{install_imessage_sync.sh,src/imessage_sync.swift}
#     No prebuilt binary, so compile from source exactly as before.
PREBUILT="$SCRIPT_DIR/networkcrm-imessage-sync"
SRC="$SCRIPT_DIR/src/imessage_sync.swift"
if [ -x "$PREBUILT" ]; then
  SRC=""
elif [ -f "$SRC" ]; then
  PREBUILT=""
else
  echo "error: found neither a prebuilt helper at $PREBUILT nor its source at $SRC." >&2
  echo "       Run this from inside Network CRM.app's Resources/imessage-sync/," >&2
  echo "       or from a repo checkout's packaging/ directory." >&2
  exit 1
fi

LABEL="io.github.william-agehouse.networkcrm.imessagesync"
SUPPORT_DIR="$HOME/Library/Application Support/NetworkCRM"
BIN_DIR="$SUPPORT_DIR/bin"
BIN="$BIN_DIR/networkcrm-imessage-sync"
SRC_HASH_FILE="$BIN_DIR/.networkcrm-imessage-sync.source.sha256"
DEST_DIR="$SUPPORT_DIR/imessage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/NetworkCRM"
LOG="$LOG_DIR/imessage-sync.log"

# How often the agent looks at the source databases, in seconds.
#
# 120 (2 minutes). Six hours would be the right number for a helper that
# re-copied both databases in full on every run (hundreds of MB): at 2 minutes
# that would be hundreds of GB of SSD writes a day. This helper does neither —
# clonefile(2) makes the copy copy-on-write (about a 20 KB write) and the cursor
# file means an unchanged source is not copied at all — so a run where nothing
# arrived costs two stat() calls. See the notes in src/imessage_sync.swift.
#
# The number to keep in mind is not this one but the sum: a message is visible in
# the app after (this interval) + (NC_AUTOSYNC_MINUTES, the app's own check,
# default 2). Turning only one of them down moves the floor, not the ceiling.
#
# Do not confuse the two pipelines: this interval is the helper's own, and the
# app's loop (NC_AUTOSYNC_MINUTES) defaults to minutes, not hours.
INTERVAL="${NC_IMESSAGE_SYNC_INTERVAL:-120}"

FORCE_REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --force-rebuild) FORCE_REBUILD=1 ;;
    # Print this file's header comment block (everything after the shebang up
    # to the first line of real code), so --help can't drift out of date.
    -h|--help) awk 'NR>1 && /^#/ {print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "install_imessage_sync.sh: unknown option '$arg' (try --help)" >&2; exit 1 ;;
  esac
done

die() { echo "install_imessage_sync.sh: $*" >&2; exit 1; }

# ── preflight ────────────────────────────────────────────────────────────────
[ "$(id -u)" != "0" ] || die "do not run this with sudo. It installs into your own home directory only."

# NC-197. Refuse to run with a borrowed HOME. launchd names the agent by LABEL,
# not by folder, and the helper writes to the account's real home whatever $HOME
# says — so from the app's fresh-install preview, or a test, with HOME pointed
# somewhere else, this would replace the helper the account already has, and a copy
# with no Full Disk Access would then report "blocked" into the real app.
# `~user` is looked up in the account database, never taken from $HOME.
_canon() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
REAL_HOME="$(eval echo "~$(id -un)")"
[ "$(_canon "$HOME")" = "$(_canon "$REAL_HOME")" ] || die "HOME is $HOME, but this account's home is $REAL_HOME. The helper and launchd always use the real one, so installing from here would replace the helper this Mac already has. Nothing was changed."
# Exactly one of the two is set by the layout check at the top of this file.
[ -n "$PREBUILT" ] || [ -f "$SRC" ] || die "cannot find the helper source at $SRC"

case "$INTERVAL" in
  ''|*[!0-9]*) die "NC_IMESSAGE_SYNC_INTERVAL must be a positive whole number of seconds, got '$INTERVAL'" ;;
esac
[ "$INTERVAL" -gt 0 ] || die "NC_IMESSAGE_SYNC_INTERVAL must be greater than 0"

# swiftc ships with the Xcode Command Line Tools. /usr/bin/swiftc exists as a
# stub even when they are not installed, so actually invoke it to check.
# Only needed on the compile-from-source path — a bundle install has its binary
# already, which is the whole reason a user without developer tools can run this
# at all.
SWIFTC=""
if [ -n "$SRC" ]; then
for candidate in "$(command -v swiftc 2>/dev/null || true)" "$(xcrun -f swiftc 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
    SWIFTC="$candidate"
    break
  fi
done
if [ -z "$SWIFTC" ]; then
  die "swiftc is not available, so the helper cannot be compiled.
  Install the Xcode Command Line Tools first:

      xcode-select --install

  (This helper MUST be a compiled binary — a shell script would make macOS
  attach Full Disk Access to /bin/bash instead, which would hand that access
  to every other program on the Mac. See packaging/IMESSAGE_SYNC.md.)"
fi
fi   # end: swiftc is only required when compiling from source

UID_NUM="$(id -u)"
mkdir -p "$BIN_DIR" "$LOG_DIR"
# Only tighten the directory this script owns. $SUPPORT_DIR is shared with the
# rest of the app, so re-permissioning it here would be a side effect that
# outlives an uninstall. (The destination directory for the copy gets 0700 from
# the helper itself, since that is where the message content actually lands.)
chmod 700 "$BIN_DIR" 2>/dev/null || true

# ── 1. put the binary in place (only when needed) ─────────────────────────────
# The sentinel hashes whatever this install builds FROM — the source in a repo
# checkout, the shipped binary in a bundle install. Either way an unchanged input
# means the installed binary is left exactly as it is, because replacing it
# changes its code hash and macOS then silently drops the Full Disk Access grant.
NEW_HASH="$(shasum -a 256 "${PREBUILT:-$SRC}" | awk '{print $1}')"
OLD_HASH=""
[ -f "$SRC_HASH_FILE" ] && OLD_HASH="$(cat "$SRC_HASH_FILE" 2>/dev/null || true)"

REBUILT=0
if [ "$FORCE_REBUILD" = "1" ] || [ ! -x "$BIN" ] || [ "$NEW_HASH" != "$OLD_HASH" ]; then
  if [ -x "$BIN" ]; then
    echo "==> Replacing the helper (input changed, or --force-rebuild)."
    echo "    NOTE: this changes the binary's code hash, so macOS will treat it"
    echo "    as a different program and DROP any existing Full Disk Access"
    echo "    grant. You will have to remove and re-add it (steps below)."
  elif [ -n "$PREBUILT" ]; then
    echo "==> Installing the helper that shipped with the app."
  else
    echo "==> Compiling the helper."
  fi
  # Stage next to the target and move it into place, so an interrupted install
  # can't leave a broken binary at the stable path.
  TMP_BIN="$BIN.new.$$"
  trap 'rm -f "$TMP_BIN"' EXIT
  if [ -n "$PREBUILT" ]; then
    # appify.sh already compiled AND ad-hoc signed this with the identifier used
    # below, so re-signing would change the code hash for no reason — and the
    # hash is exactly what the Full Disk Access grant is pinned to.
    cp "$PREBUILT" "$TMP_BIN" || die "could not copy $PREBUILT"
  else
    "$SWIFTC" -O "$SRC" -o "$TMP_BIN" || die "swiftc failed to compile $SRC"
    # Ad-hoc signature ("-"). That is not a stable signing identity, so macOS
    # pins the permission to this exact binary's code hash instead.
    #
    # -i sets the signing identifier explicitly. Without it codesign derives the
    # identifier from the *filename it is signing*, which here is the temp name
    # above — that would bake a PID into the signature and change it on every
    # install for no reason.
    codesign -s - --force -i networkcrm-imessage-sync "$TMP_BIN" >/dev/null 2>&1 \
      || die "codesign failed on $TMP_BIN"
  fi
  mv -f "$TMP_BIN" "$BIN"
  trap - EXIT
  chmod 755 "$BIN"
  printf '%s\n' "$NEW_HASH" > "$SRC_HASH_FILE"
  REBUILT=1
else
  echo "==> Helper already built from this exact source; leaving it untouched"
  echo "    so any existing Full Disk Access grant keeps working."
fi

echo "    Binary: $BIN"

# ── 2. LaunchAgent plist ─────────────────────────────────────────────────────
echo "==> Writing LaunchAgent: $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <!-- The compiled helper, at its stable path. No arguments: it uses its
       default destination, $DEST_DIR -->
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
  </array>

  <!-- Run once at login/load, then every $INTERVAL seconds. -->
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>$INTERVAL</integer>

  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST" >/dev/null || die "generated plist is malformed: $PLIST"

# ── 3. load it ───────────────────────────────────────────────────────────────
# Unload first so a re-run picks up plist changes instead of silently keeping
# the old definition. Both failures are expected when nothing is loaded yet.
launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
launchctl unload "$PLIST" >/dev/null 2>&1 || true

echo "==> Loading the agent."
if ! launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
  # Older launchctl syntax (pre-10.11) / edge cases.
  if ! launchctl load -w "$PLIST" 2>/dev/null; then
    if launchctl list "$LABEL" >/dev/null 2>&1; then
      echo "    (already loaded)"
    else
      die "could not load the agent. Try: launchctl bootstrap gui/$UID_NUM \"$PLIST\""
    fi
  fi
fi

if launchctl list "$LABEL" >/dev/null 2>&1; then
  echo "    Loaded, runs every $INTERVAL seconds."
else
  echo "    WARNING: the agent does not appear in 'launchctl list' — check $LOG" >&2
fi

# ── 4. run it once now, so the permission outcome is visible immediately ─────
# RunAtLoad above already triggered one background run, but that one only shows up
# in the log. Getting the answer NOW rather than at the next scheduled run is the
# point of this step.
#
# THROUGH LAUNCHD, NOT AS OUR OWN CHILD — read this before "simplifying" it back.
# Running `"$BIN" || RC=$?` here would report a FALSE FAILURE on a Mac
# where Full Disk Access IS granted. macOS attributes a TCC check to the
# *responsible* process, and for a binary spawned from a shell that is the
# terminal, not the binary. So the grant on the helper does not apply and the read
# is refused. So the same Mac gives both answers, depending on the path:
#
#     launchctl kickstart (the real scheduled path) -> copied chat.db
#     "$BIN" straight from bash                     -> "still needs Full Disk Access"
#
# Which means a closing verdict taken from the child run would tell a user who had
# just granted the permission correctly that macOS was still blocking them — and
# a step 4 that told them to confirm by re-running the binary would say the same
# thing forever. It is the same class of bug as reading the wrong exit code: a
# signal that does not answer the question being asked of it.
#
# launchd is the parent the grant was given for, so the outcome comes from the
# helper's own report file instead of from an exit code we are not entitled to.
echo
echo "==> Running the helper once now, through launchd, to see the real"
echo "    permission outcome. (Running it straight from this shell would be"
echo "    attributed to your terminal and report a false failure.)"

REPORT="$SUPPORT_DIR/sync-status.json"
STAMP_BEFORE=""
[ -f "$REPORT" ] && STAMP_BEFORE="$(stat -f %m "$REPORT" 2>/dev/null || true)"

RC=0
if launchctl kickstart -k "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  # Wait for a report NEWER than the one that was already there — reading the
  # previous run's answer back as this one's is the same mistake in miniature.
  WAITED=0
  while [ "$WAITED" -lt 60 ]; do
    STAMP_NOW=""
    [ -f "$REPORT" ] && STAMP_NOW="$(stat -f %m "$REPORT" 2>/dev/null || true)"
    if [ -n "$STAMP_NOW" ] && [ "$STAMP_NOW" != "$STAMP_BEFORE" ]; then
      # The helper writes its own exit code into the report. That is the value the
      # branches below have always keyed on.
      # plutil, not python: it reads JSON, it is in /usr/bin on every Mac, and it
      # needs no developer tools. Requiring a python here would undo the whole
      # reason this script ships a prebuilt binary (a user has no Xcode CLT,
      # and /usr/bin/python3 is a stub that prompts to install them).
      RC="$(plutil -extract exit raw -o - -- "$REPORT" 2>/dev/null || echo 0)"
      i=0
      while [ "$i" -lt 8 ]; do
        SRC_LABEL="$(plutil -extract "sources.$i.label" raw -o - -- "$REPORT" 2>/dev/null)" || break
        SRC_OUTCOME="$(plutil -extract "sources.$i.outcome" raw -o - -- "$REPORT" 2>/dev/null || echo '?')"
        printf '    %-9s %s\n' "$SRC_LABEL" "$SRC_OUTCOME"
        i=$((i + 1))
      done
      break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
  done
  if [ "$WAITED" -ge 60 ]; then
    echo "    The helper did not report back within 60s — check $LOG" >&2
  fi
else
  echo "    launchd would not start the agent — check $LOG" >&2
  RC=1
fi

echo
if [ "$RC" = "0" ]; then
  echo "──────────────────────────────────────────────────────────────────────"
  echo "Done — macOS is not blocking anything. See the per-source lines above"
  echo "for what was actually copied (a Mac with only one of Messages/WhatsApp"
  echo "is normal, and an absent source is skipped rather than granted)."
  echo
  echo "  Copy lives in : $DEST_DIR"
  echo "  Refreshed     : every $INTERVAL seconds, plus at login"
  echo "  Log           : $LOG"
  echo
  echo "Point the importer at the copy:"
  echo "  CHAT_DB=\"$DEST_DIR/chat.db\" python crm/imessage_import.py"
  echo "  (or: python crm/imessage_import.py --chatdb \"$DEST_DIR/chat.db\")"
  echo
  echo "WhatsApp, if you use WhatsApp Desktop, needs no flag at all — the app"
  echo "finds the helper's copy by itself and imports it on every sync pass."
elif [ "$RC" = "2" ]; then
  # Expected on a first install: FDA is never granted automatically and macOS
  # never prompts for it, so it has to be added by hand. The install itself
  # succeeded.
  echo "──────────────────────────────────────────────────────────────────────"
  echo "Install finished. macOS is still blocking AT LEAST ONE source (the"
  echo "line above names which). Full Disk Access has to be granted by hand,"
  echo "one time. It is never prompted for automatically."
  echo
  echo "This can be a PARTIAL block: WhatsApp needs no Full Disk Access, so it"
  echo "may have copied fine while iMessage did not. That combination used to"
  echo "exit 0 and print this section not at all — see the exit-code contract"
  echo "at the bottom of src/imessage_sync.swift."
  echo
  echo "  1. The Full Disk Access pane should now be open. If not:"
  echo "     System Settings > Privacy & Security > Full Disk Access"
  echo "  2. Click '+'. In the file dialog press Cmd+Shift+G and paste:"
  echo
  echo "       $BIN_DIR"
  echo
  echo "     then select:  networkcrm-imessage-sync"
  echo "     (You can also drag the binary into the list.)"
  echo "  3. Make sure its toggle is ON."
  echo "  4. Confirm it worked. Easiest: open Network CRM, go to Data, and press"
  echo "     'Check now' in the Full Disk Access panel."
  echo
  echo "     From a terminal, it has to go through launchd — running the binary"
  echo "     directly is attributed to your terminal, not to the binary, and will"
  echo "     say 'still blocked' even when the grant is correct:"
  echo
  echo "       launchctl kickstart -k gui/$UID_NUM/$LABEL"
  echo "       plutil -p \"$SUPPORT_DIR/sync-status.json\""
  echo
  echo "Grant it to THAT binary only — not to Terminal, not to the CRM app."
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" \
    >/dev/null 2>&1 || echo "  (Could not open System Settings automatically — open it manually.)"
else
  echo "──────────────────────────────────────────────────────────────────────"
  echo "The agent is installed, but the helper failed for a reason other than"
  echo "permissions (exit $RC). See the message above and $LOG." >&2
fi

echo
echo "Installed:"
echo "  $BIN"
echo "  $PLIST   (launchd label: $LABEL)"
if [ "$REBUILT" = "1" ]; then
  echo "  (binary compiled from packaging/src/imessage_sync.swift and ad-hoc signed)"
fi
echo
echo "To undo all of it:  packaging/uninstall_imessage_sync.sh"
