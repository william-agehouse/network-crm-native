#!/bin/bash
# ── Build NetworkCRM.dmg from packaging/dist ─────────────────────────────────
# Uses only hdiutil (no create-dmg / third-party dependency). Run
# packaging/appify.sh --handout (or --embed) first — this script refuses to run
# against a repo-linked build since that .app is useless once copied off this
# machine. NC_DIST_DIR names another build folder, as it does for appify.sh.
#
# NC-313. This disk image is the free way to hand the app out: the app is not
# signed by Apple, so macOS blocks its first launch, and the Read Me beside the
# app is how a recipient gets past that without anyone to ask. The lasting fix is
# an Apple Developer ID with notarization (99 US dollars a year), which removes
# the block for everyone; see packaging/README.md, "Signing and notarizing".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${NC_DIST_DIR:-$SCRIPT_DIR/dist}"
APP_DIR="$DIST_DIR/Network CRM.app"
DMG_PATH="$DIST_DIR/NetworkCRM.dmg"
VOL_NAME="Network CRM"

if ! command -v hdiutil >/dev/null 2>&1; then
  echo "make_dmg.sh: hdiutil not found — this only runs on macOS." >&2
  exit 1
fi

if [ ! -d "$APP_DIR" ]; then
  echo "make_dmg.sh: '$APP_DIR' not found. Run packaging/appify.sh --embed first." >&2
  exit 1
fi

if [ ! -d "$APP_DIR/Contents/Resources/app" ]; then
  echo "make_dmg.sh: warning — '$APP_DIR' looks repo-linked, not embedded (no Contents/Resources/app)." >&2
  echo "              A DMG built from this .app will not work once copied to another Mac." >&2
  read -r -p "Continue anyway? [y/N] " reply
  case "$reply" in
    [yY]*) ;;
    *) exit 1 ;;
  esac
fi

# Since NC-330 the app starts its engine on its own Python and on nothing else, so a copy
# without one cannot open at all.
if [ ! -x "$APP_DIR/Contents/Resources/python/bin/python3" ]; then
  echo "make_dmg.sh: '$APP_DIR' carries no Python of its own, so it cannot open. Build it with --handout." >&2
  exit 1
fi

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

cp -R "$APP_DIR" "$STAGE_DIR/"
ln -s /Applications "$STAGE_DIR/Applications"

# What someone who receives the app has to know, next to the app, because they
# will never open this repo (NC-007, NC-313). Written here rather than kept as a
# checked-in file so it cannot drift out of sync with the disk image that carries
# it, and .txt rather than .md so double-clicking it opens TextEdit instead of
# doing nothing.
#
# macOS 15 and later no longer let right-click > Open past the block for an app
# that is not signed; the way through is System Settings > Privacy & Security >
# Open Anyway, which is offered for about an hour after the blocked attempt
# (Apple's "Open a Mac app from an unknown developer" page). The app needs macOS
# 26, so that is the only route its readers have.
RELEASES="the project's releases page on GitHub"
if [ -f "$APP_DIR/Contents/Resources/update_repo.txt" ]; then
  RELEASES="github.com/$(tr -cd 'A-Za-z0-9._/-' < "$APP_DIR/Contents/Resources/update_repo.txt")/releases"
fi
cat > "$STAGE_DIR/Read Me First.txt" <<'READMEEOF'
Network CRM — read this before the first launch
===============================================

WHAT YOU NEED: a Mac with an Apple chip (M1 or later) running macOS 26 or
later. It does not run on a Mac with an Intel processor.

1. Drag "Network CRM" onto the Applications folder in this window.

2. THE FIRST LAUNCH WILL BE BLOCKED, once. The app is not signed by Apple, so
   macOS stops it the first time and says it could not check it for harmful
   software. That is expected. To let it through:

   a. Double-click Network CRM in Applications. macOS shows the warning:
      click Done (not Move to Trash).
   b. Open System Settings (the Apple menu > System Settings), click
      Privacy & Security in the sidebar, and scroll down to the Security
      section. It says "Network CRM" was blocked. Click Open Anyway.
      This button appears only for about an hour after step a. If it is
      not there, do step a again.
   c. Type your Mac's password (or use Touch ID), then click Open Anyway
      in the dialog that follows.

   After that it opens with a double-click like any other app. A new
   version you install may need these steps once more.

   If instead macOS says "Network CRM is damaged and can't be opened. You
   should move it to the Trash": it is NOT damaged. That is the message macOS
   shows for an app that is not signed and came by download, AirDrop or a
   messaging app. Open Terminal and paste this single line:

         xattr -dr com.apple.quarantine "/Applications/Network CRM.app"

   Then double-click as usual. xattr ships with every Mac; no admin password
   and no installation.

3. THE FIRST LAUNCH TAKES ABOUT A MINUTE AND NEEDS THE INTERNET. The window
   says "Setting up" while the app fetches the parts the AI features and the
   Google imports use. Later launches start straight away, and work offline.

4. NOTHING TO INSTALL. The app carries its own Python inside it, so there is
   nothing to set up beyond step 3.

UPDATING: new versions are published at __RELEASES__.
To update, replace the app; your data stays. Quit Network CRM, drag the new
version into Applications and choose Replace. Your contacts, notes and messages
live in your own Library folder, not inside the app, so replacing it keeps them.

Nothing about this app phones home. Everything below is optional and off until
you turn it on: an Anthropic API key for the AI features, a calendar link, and a
separate small helper if you want your messages to keep themselves up to date.

To stop the app, quit it (Cmd-Q). That stops everything it started; even a
force-quit leaves nothing running behind it.

REMOVING IT: putting the app in the Trash leaves your data in your Library
folder, and the copying helper (if you set it up) keeps running. In the app,
open Settings and press Remove everything at the bottom: it turns the helper
off, then deletes the app's data and logs. Then drag the app to the Trash, and
remove the helper's entry in System Settings > Privacy & Security > Full Disk
Access. The full checklist is docs/UNINSTALL.md on the project's GitHub page.
READMEEOF
# The release address is only known at build time; the Read Me is written with
# a placeholder so the text above can stay a quoted, literal block.
sed -i '' "s|__RELEASES__|$RELEASES|" "$STAGE_DIR/Read Me First.txt"

rm -f "$DMG_PATH"
hdiutil create -volname "$VOL_NAME" -srcfolder "$STAGE_DIR" -ov -format UDZO "$DMG_PATH"

echo "make_dmg.sh: staged 'Read Me First.txt' beside the app (requirements, Open Anyway, first-launch setup, updating, removing)."
echo "make_dmg.sh: done -> $DMG_PATH"
echo "make_dmg.sh: this DMG is unsigned; its Read Me walks the recipient through Open Anyway. The paid alternative (Developer ID + notarization) is in packaging/README.md."
