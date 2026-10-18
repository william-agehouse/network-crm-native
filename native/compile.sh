#!/bin/bash
# Compiles native/Sources/*.swift into the app's one program. Used by packaging/appify.sh
# (Network CRM.app) and native/build.sh (the quick developer copy), so the two can never
# build the window differently.
#
#   native/compile.sh <output program>
#
# Which toolkit (NC-375). The macOS 26.5 one under the Command Line Tools comes first: in
# the 27 toolkit SwiftUI's @State became a macro whose build plugin ships only with full
# Xcode, which needs its licence accepted first, and 26.5 compiles @State, @Observable and
# the rest with what the Command Line Tools install. When 26.5 is missing, the toolkit
# `xcrun` finds is used instead (full Xcode, or newer or older tools), and this says which,
# because a compile error from a different toolkit reads very differently.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:?usage: native/compile.sh <output program>}"

CLT=/Library/Developer/CommandLineTools
PREFERRED="$CLT/SDKs/MacOSX26.5.sdk"
if [ -d "$PREFERRED" ]; then
  export DEVELOPER_DIR="$CLT"
  SDK="$PREFERRED"
  echo "compile.sh: using the macOS 26.5 toolkit from the Command Line Tools ($SDK)"
else
  if ! SDK="$(xcrun --sdk macosx --show-sdk-path 2>/dev/null)" || [ -z "$SDK" ]; then
    echo "compile.sh: no macOS toolkit found. Install the Command Line Tools (xcode-select --install) and build again." >&2
    exit 1
  fi
  echo "compile.sh: the macOS 26.5 toolkit is not at $PREFERRED, so using the one xcrun found: $SDK ($(xcrun --sdk macosx --show-sdk-version 2>/dev/null || echo "unknown version"), from $(xcode-select -p 2>/dev/null || echo "unknown"))"
fi

shopt -s nullglob
SOURCES=("$HERE"/Sources/*.swift)
[ ${#SOURCES[@]} -gt 0 ] || { echo "compile.sh: no sources in $HERE/Sources" >&2; exit 1; }
mkdir -p "$(dirname "$OUT")"
# -target pins the oldest macOS the app starts on (NC-312: Apple chip, macOS 26 or later);
# LSMinimumSystemVersion in each Info.plist says the same, and a test keeps them equal.
xcrun swiftc -sdk "$SDK" -target arm64-apple-macos26 -swift-version 6 -parse-as-library -O \
  "${SOURCES[@]}" -o "$OUT"
echo "compile.sh: compiled ${#SOURCES[@]} files -> $OUT"
