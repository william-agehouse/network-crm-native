#!/bin/bash
# Builds native/dist.noindex/Network CRM Native.app: the developer copy of the app's window, compiled
# from native/Sources/*.swift with the Command Line Tools alone (no Xcode project). It carries
# no engine of its own: opened from Finder it runs this checkout's crm/ on the checkout's .venv
# (Resources/repo_path.txt), against the real data folder, so quit Network CRM first. The app
# people use is built by packaging/appify.sh --embed, from the same sources (NC-330).
# native/compile.sh picks the toolkit (NC-375). The folder's name ends in .noindex so Spotlight
# never lists this copy beside the real app (NC-378).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
FINAL="$HERE/dist.noindex/Network CRM Native.app"
# Built beside the finished app and swapped in by rename at the end, so a failed build
# leaves the last good one, and an open copy's program file is never rewritten in place
# (macOS kills a running program whose signed code changes under it; a renamed or
# removed file it already has open keeps working until it quits).
STAGE="$HERE/dist.noindex/.building"
APP="$STAGE/Network CRM Native.app"
EXE_NAME=NetworkCRMNative
VERSION=0.1.0

rm -rf "$STAGE"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "build.sh: compiling…"
"$HERE/compile.sh" "$APP/Contents/MacOS/$EXE_NAME"
# Where the engine's code is: this checkout (EngineLaunch.forThisApp reads it).
echo "$REPO" > "$APP/Contents/Resources/repo_path.txt"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Network CRM Native</string>
  <key>CFBundleDisplayName</key><string>Network CRM Native</string>
  <key>CFBundleIdentifier</key><string>io.github.william-agehouse.networkcrm.native</string>
  <key>CFBundleExecutable</key><string>$EXE_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleIconFile</key><string>appicon</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Runs entirely on your Mac.</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null

# The icon: reuse the current app's when it has been built, else draw it from the same SVG
# the way packaging/appify.sh does (qlmanage → sips → iconutil, all built into macOS).
ICON_OUT="$APP/Contents/Resources/appicon.icns"
CURRENT_ICON="$REPO/packaging/dist/Network CRM.app/Contents/Resources/appicon.icns"
if [ -f "$CURRENT_ICON" ]; then
  cp "$CURRENT_ICON" "$ICON_OUT"
  echo "build.sh: icon copied from the current app"
else
  SVG="$REPO/packaging/src/appicon.svg"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  # appify.sh's guard: a malformed SVG makes qlmanage rasterise its own error page.
  python3 -c "import sys, xml.dom.minidom as m; m.parse(sys.argv[1])" "$SVG" \
    || { echo "build.sh: $SVG is not well-formed XML; not drawing an icon from it" >&2; exit 1; }
  qlmanage -t -s 1024 -o "$work" "$SVG" >/dev/null 2>&1
  master="$work/appicon.svg.png"
  [ -f "$master" ] || { echo "build.sh: qlmanage produced no PNG for the icon" >&2; exit 1; }
  iconset="$work/appicon.iconset"
  mkdir -p "$iconset"
  for sz in 16 32 128 256 512; do
    sips -z "$sz" "$sz" "$master" --out "$iconset/icon_${sz}x${sz}.png" >/dev/null
    sz2=$((sz * 2))
    sips -z "$sz2" "$sz2" "$master" --out "$iconset/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$ICON_OUT"
  echo "build.sh: icon drawn from appicon.svg"
fi

HEAD="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$HEAD" > "$APP/Contents/Resources/build_id.txt"

# Ad-hoc signed as one piece, so macOS sees one coherent bundle.
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"

OLD="$HERE/dist.noindex/.replaced-$$"
[ -d "$FINAL" ] && mv "$FINAL" "$OLD"
mv "$APP" "$FINAL"
rm -rf "$STAGE" "$OLD"
echo "build.sh: built $FINAL"
