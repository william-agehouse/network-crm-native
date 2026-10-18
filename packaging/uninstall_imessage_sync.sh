#!/bin/bash
# ── Remove the iMessage sync helper ──────────────────────────────────────────
#
# Usage:
#   packaging/uninstall_imessage_sync.sh
#
# Reverses packaging/install_imessage_sync.sh completely: unloads and deletes
# the LaunchAgent, deletes the compiled helper binary.
#
# It deliberately does NOT delete the copied chat.db — that is your data, and
# deleting a large message history on your behalf is not this
# script's call. The path is printed at the end so you can remove it yourself.
#
# The one thing no script can do for you is remove the Full Disk Access entry;
# macOS only allows that from System Settings. Instructions are printed.
#
# Safe to run repeatedly, and when nothing is installed.
set -euo pipefail

LABEL="io.github.william-agehouse.networkcrm.imessagesync"
SUPPORT_DIR="$HOME/Library/Application Support/NetworkCRM"
BIN_DIR="$SUPPORT_DIR/bin"
BIN="$BIN_DIR/networkcrm-imessage-sync"
SRC_HASH_FILE="$BIN_DIR/.networkcrm-imessage-sync.source.sha256"
DEST_DIR="$SUPPORT_DIR/imessage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/NetworkCRM/imessage-sync.log"

for arg in "$@"; do
  case "$arg" in
    # See the same trick in install_imessage_sync.sh: print the header comment
    # block rather than hard-coded line numbers that go stale.
    -h|--help) awk 'NR>1 && /^#/ {print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "uninstall_imessage_sync.sh: unknown option '$arg' (try --help)" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" = "0" ]; then
  echo "uninstall_imessage_sync.sh: do not run this with sudo — it only touches your own home directory." >&2
  exit 1
fi

# NC-197. Refuse to run with a borrowed HOME. launchd names the agent by LABEL,
# not by folder, and the helper writes to the account's real home whatever $HOME
# says — so from the app's fresh-install preview, or a test, with HOME pointed
# somewhere else, this would stop the helper the account already has.
# `~user` is looked up in the account database, never taken from $HOME.
_canon() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
REAL_HOME="$(eval echo "~$(id -un)")"
if [ "$(_canon "$HOME")" != "$(_canon "$REAL_HOME")" ]; then
  echo "uninstall_imessage_sync.sh: HOME is $HOME, but this account's home is $REAL_HOME. launchd always uses the real one, so this would stop the helper this Mac already has. Nothing was changed." >&2
  exit 1
fi

UID_NUM="$(id -u)"
REMOVED_ANY=0

# ── 1. unload the agent ──────────────────────────────────────────────────────
# Try both launchctl generations; "not loaded" is a success case here, so all
# failures are tolerated and the real check is `launchctl list` afterwards.
echo "==> Unloading the agent (if loaded)."
launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
launchctl unload "$PLIST" >/dev/null 2>&1 || true

if launchctl list "$LABEL" >/dev/null 2>&1; then
  echo "    WARNING: '$LABEL' is still listed by launchctl. Try manually:" >&2
  echo "      launchctl bootout gui/$UID_NUM/$LABEL" >&2
else
  echo "    Not loaded."
fi

# ── 2. remove the plist ──────────────────────────────────────────────────────
if [ -e "$PLIST" ]; then
  rm -f "$PLIST"
  echo "==> Removed $PLIST"
  REMOVED_ANY=1
else
  echo "==> No plist at $PLIST (nothing to remove)."
fi

# ── 3. remove the binary ─────────────────────────────────────────────────────
if [ -e "$BIN" ]; then
  rm -f "$BIN"
  echo "==> Removed $BIN"
  REMOVED_ANY=1
else
  echo "==> No binary at $BIN (nothing to remove)."
fi

rm -f "$SRC_HASH_FILE"
# Clean up leftover temp files from an interrupted install, then drop the bin
# directory only if it is now empty (rmdir refuses otherwise, which is the
# behavior we want — never delete someone else's files).
rm -f "$BIN".new.* 2>/dev/null || true
[ -d "$BIN_DIR" ] && rmdir "$BIN_DIR" 2>/dev/null || true

# ── 4. what is left, and the one manual step ─────────────────────────────────
echo
echo "──────────────────────────────────────────────────────────────────────"
if [ "$REMOVED_ANY" = "1" ]; then
  echo "Uninstalled. The automatic sync will not run again."
else
  echo "Nothing was installed — nothing to do."
fi
echo
echo "ONE MANUAL STEP — remove the now-dead Full Disk Access entry:"
echo "  System Settings > Privacy & Security > Full Disk Access"
echo "  Select 'networkcrm-imessage-sync' and click '-'."
echo
echo "  It is harmless once the binary is gone (it grants access to a file that"
echo "  no longer exists), but it will sit in the list looking like a live"
echo "  permission until you remove it. macOS does not allow scripts to edit"
echo "  that list, which is exactly why the entry had to be added by hand too."
echo
echo "LEFT IN PLACE ON PURPOSE (your data — delete it yourself if you want):"
if [ -d "$DEST_DIR" ]; then
  echo "  $DEST_DIR"
  echo "    $(du -sh "$DEST_DIR" 2>/dev/null | awk '{print $1}') of copied message history"
  echo "    Remove with:  rm -rf \"$DEST_DIR\""
else
  echo "  $DEST_DIR  (does not exist — nothing was ever copied)"
fi
if [ -f "$LOG" ]; then
  echo "  $LOG"
fi
echo
echo "Anything already imported into crm.db / archive.db is untouched."
