#!/bin/bash
# ── Build Network CRM.app ─────────────────────────────────────────────────────
#
# Usage:
#   packaging/appify.sh --embed             # THE SHIP COMMAND: the window, the engine's
#                                           # code (crm/) and its own Python, so the app
#                                           # needs nothing installed (Apple chip, and
#                                           # only with the Python archive cached; see
#                                           # below)
#   packaging/appify.sh                     # developer build: the window runs this
#                                           # checkout's crm/ on the checkout's .venv
#   packaging/appify.sh --handout           # the copy you give to other people:
#                                           # --embed, plus the release address for the
#                                           # update check, and it refuses to finish
#                                           # without its own Python and helper (NC-311,
#                                           # NC-315)
#
# Two settings, both optional, for building somewhere other than this folder:
#   NC_DIST_DIR       where the app is written (default: packaging/dist)
#   NC_PY_CACHE_DIR   where the CPython archive is looked for (default:
#                     packaging/.cache)
#
# WHAT THE APP IS (NC-330, 2026-10-17). Contents/MacOS/NetworkCRM is the native window,
# compiled from native/Sources (native/compile.sh). It starts the engine (crm/app.py) as
# its own child on the bundled Python, reaches it through a private socket file instead of
# a network port, and stops it when it quits. So there is no address a browser can open,
# no launcher script, no web page in a window and no separate "Quit Network CRM" app any
# more; all three were removed here that day. The first start of a new version also makes
# the private Python environment the optional parts run in (crm/python_env.py).
#
# THE BUNDLED INTERPRETER (NC-021). --embed ships a trimmed relocatable CPython in
# Contents/Resources/python, as many Mac apps carry their own runtime. Nothing is
# installed on the user's machine and no admin password is asked for. The app starts
# its engine on it every time, so the path that ships is the path that runs here too.
# Apple chip only (NC-312): embed_python refuses an Intel host rather than producing a
# bundle that fails in the worst way.
#
# THE INTERPRETER IS NOT DOWNLOADED. This script never touches the network, by design.
# It bundles an 'install_only' aarch64-apple-darwin CPython archive only if you have
# already put one in packaging/.cache/, which is gitignored, so a fresh clone has none.
# Since NC-330 the app cannot start its engine without it, so --embed stops and says
# where to put the archive instead of building an app that cannot open.
#
# Only macOS built-in tools are used: swiftc from the Command Line Tools, sips +
# iconutil for the .icns, qlmanage (QuickLook) to rasterize the source SVG.
set -euo pipefail

# NC-310. The build runs the bundled interpreter to check it, and an
# interpreter that imports its own standard library writes compiled caches
# (__pycache__/*.pyc) beside it. Those caches record the folder they were made
# in, so a bundle carrying them tells whoever receives it where it was built.
# Nothing this script runs may write them; strip_caches below removes any that
# arrive another way.
export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="${NC_DIST_DIR:-$SCRIPT_DIR/dist}"
APP_DIR="$DIST_DIR/Network CRM.app"
# Built by every version before NC-330; removed from dist/ when found.
OLD_QUIT_APP_DIR="$DIST_DIR/Quit Network CRM.app"
ICON_SVG="$SCRIPT_DIR/src/appicon.svg"
EXE_NAME="NetworkCRM"

# The name macOS files this app under: Login Items, Full Disk Access and the
# helper's background job all show it. A Full Disk Access grant hangs on it, so
# changing it on an installed copy means granting access once more.
BUNDLE_ID="io.github.william-agehouse.networkcrm"

# NC-312, decided: an Apple-chip Mac with macOS 26 or later. The same number is
# LSMinimumSystemVersion in write_plist and the compiler's target here and in
# native/compile.sh, so macOS itself says "this Mac is too old" instead of the app
# failing to open. A test keeps them equal.
SWIFT_TARGET="arm64-apple-macos26.0"

EMBED=0
HANDOUT=0
for arg in "$@"; do
  case "$arg" in
    --embed) EMBED=1 ;;
    # An older spelling of the same build, still accepted.
    --with-python) EMBED=1 ;;
    # The copy other people get (NC-311). --embed with everything in it.
    --handout) EMBED=1; HANDOUT=1 ;;
    --no-python)
      echo "appify.sh: --no-python is gone (NC-330): the app starts its engine on its own Python, so a copy without one cannot open." >&2
      exit 1 ;;
    *) echo "appify.sh: unknown option '$arg' (supported: --embed, --handout)" >&2; exit 1 ;;
  esac
done

# NC-311. A copy for other people must carry an identity of its own under
# io.github., never this checkout's when that is someone's private one: every
# Mac that opens it would otherwise list the app, and its always-on helper,
# under that name in Login Items and Full Disk Access. Checked before anything
# is written, so a refused build leaves the last one untouched.
handout_identity_ok() {
  case "$1" in
    io.github.*) return 0 ;;
    *) return 1 ;;
  esac
}
if [ "$HANDOUT" = 1 ] && ! handout_identity_ok "$BUNDLE_ID"; then
  echo "appify.sh --handout: this checkout names the app '$BUNDLE_ID'." >&2
  echo "  A copy for other people must carry an identity under io.github., so build it" >&2
  echo "  from the cleaned public copy, which has one, instead of from here." >&2
  exit 1
fi

for tool in sips iconutil; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "appify.sh: required tool '$tool' not found. This script only works on macOS with the standard developer command line tools present." >&2
    exit 1
  fi
done
# The window is the app now (NC-330): without a Swift compiler there is nothing to build.
if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "appify.sh: 'xcrun swiftc' not found. Install the Command Line Tools with: xcode-select --install" >&2
  exit 1
fi

write_plist() {
  # write_plist <bundle_dir> <bundle_name> <bundle_id> <executable>
  local dir="$1" name="$2" ident="$3" exe="$4"
  cat > "$dir/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>$name</string>
  <key>CFBundleDisplayName</key>
  <string>$name</string>
  <key>CFBundleIdentifier</key>
  <string>$ident</string>
  <key>CFBundleExecutable</key>
  <string>$exe</string>
  <key>CFBundleIconFile</key>
  <string>appicon</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>26.0</string>
  <key>LSArchitecturePriority</key>
  <array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSHumanReadableCopyright</key>
  <string>Runs entirely on your Mac.</string>
</dict>
</plist>
PLIST
  plutil -lint "$dir/Contents/Info.plist" >/dev/null
}

build_icon() {
  # build_icon <output.icns>
  local out="$1"
  local work
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' RETURN

  if ! command -v qlmanage >/dev/null 2>&1; then
    echo "appify.sh: warning — qlmanage not found, skipping app icon (bundle will use the generic icon)." >&2
    return 0
  fi

  # The SVG must be well-formed XML before anything rasterizes it.
  #
  # WHY THIS GUARD EXISTS. qlmanage renders through WebKit, and WebKit's answer to
  # a malformed SVG is not an error — it is a PAGE: the pink "This page contains
  # the following errors" screen. It thumbnails that page happily and EXITS 0, so
  # the check below passes, sips and iconutil build a perfectly valid 10-size
  # .icns out of it, and the app ships with an XML parse error as its Dock icon.
  # That is easy to do: a header comment that spells a CSS custom property in
  # full contains a double hyphen, which is illegal inside an XML comment, and
  # the icon becomes a screenshot of the resulting error message. Nobody notices,
  # because every build succeeds and every file is valid.
  #
  # xmllint is in /usr/bin on every Mac. This is a hard failure, not a warning:
  # a broken icon is not a degraded build, it is a wrong one.
  if command -v xmllint >/dev/null 2>&1; then
    if ! xmllint --noout "$ICON_SVG" 2>/dev/null; then
      echo "appify.sh: ERROR — $ICON_SVG is not well-formed XML. WebKit would" >&2
      echo "  rasterize its own error page into the app icon. Fix the SVG first:" >&2
      xmllint --noout "$ICON_SVG" || true
      exit 1
    fi
  fi

  # QuickLook renders the SVG to a PNG thumbnail — this is the only step that
  # needs anything beyond sips/iconutil, and qlmanage ships with every Mac.
  if ! qlmanage -t -s 1024 -o "$work" "$ICON_SVG" >/dev/null 2>&1; then
    echo "appify.sh: warning — qlmanage failed to rasterize the icon SVG, skipping app icon." >&2
    return 0
  fi
  local master="$work/appicon.svg.png"
  if [ ! -f "$master" ]; then
    echo "appify.sh: warning — icon rasterization produced no PNG, skipping app icon." >&2
    return 0
  fi

  local iconset="$work/appicon.iconset"
  mkdir -p "$iconset"
  # Standard macOS iconset sizes (iconutil requires exactly these names).
  local sizes=(16 32 128 256 512)
  for sz in "${sizes[@]}"; do
    sips -z "$sz" "$sz" "$master" --out "$iconset/icon_${sz}x${sz}.png" >/dev/null
    local sz2=$((sz * 2))
    sips -z "$sz2" "$sz2" "$master" --out "$iconset/icon_${sz}x${sz}@2x.png" >/dev/null
  done

  if ! iconutil -c icns "$iconset" -o "$out" >/dev/null 2>&1; then
    echo "appify.sh: warning — iconutil failed, skipping app icon." >&2
    return 0
  fi
  echo "appify.sh: icon generated -> $out"
}

compile_imessage_helper() {
  # compile_imessage_helper <output_dir>
  #
  # Builds the opt-in Full Disk Access helper (packaging/src/imessage_sync.swift)
  # and lays it out beside its own installer inside the bundle.
  #
  # This exists because a helper that is documented but not in the bundle reaches
  # nobody: appify.sh embeds only crm/, so packaging/ — installer,
  # uninstaller and source alike — would never ship. The consequence is specific:
  # macOS has NO permission narrower than Full Disk Access for ~/Library/Messages
  # (there is no "Messages only" toggle, and a file picker does not help because
  # mandatory access control overrides the user's own selection for that path),
  # so the helper is the ONLY route to iMessage data that stays fresh by itself.
  # Without it in the bundle a user has the paste-it-by-hand route and nothing
  # else.
  #
  # Prebuilt rather than compiled at install time, which is what
  # install_imessage_sync.sh has always done, for two reasons:
  #   · A user's Mac usually has no Xcode Command Line Tools, so `swiftc` is absent
  #     and the installer could only ever die on that machine.
  #   · The FDA grant is pinned to the binary's code hash (ad-hoc signature), so
  #     compiling per machine means a per-machine hash, and any rebuild silently
  #     drops the grant. One set of bytes, built once, keeps the grant alive
  #     across app updates until the helper's source genuinely changes.
  #
  # Nothing here grants anything: it only makes the option reachable. The manual
  # copy route is untouched and remains the default.
  local out_dir="$1"
  local bin="$out_dir/networkcrm-imessage-sync"
  if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "appify.sh: warning — 'xcrun swiftc' not found, so the opt-in iMessage FDA helper is NOT in this bundle. Users get the manual copy route only." >&2
    return 1
  fi
  mkdir -p "$out_dir"
  # The oldest macOS it starts on is pinned only for a handout (NC-312). On every
  # other build the compile command stays exactly as it was, because a different
  # command makes different bytes, and different bytes lose the Full Disk Access
  # grant an installed helper already holds (see above).
  local target_args=()
  if [ "$HANDOUT" = 1 ]; then
    target_args=(-target "$SWIFT_TARGET")
  fi
  if ! xcrun swiftc -O ${target_args[@]+"${target_args[@]}"} -o "$bin" "$REPO_ROOT/packaging/src/imessage_sync.swift" 2>&1; then
    echo "appify.sh: warning — swiftc failed on imessage_sync.swift; helper not shipped." >&2
    rm -rf "$out_dir"
    return 1
  fi
  # The identifier the installer also uses, so a bundle-built binary and a
  # locally-compiled one are the same program as far as codesign is concerned.
  if ! codesign -s - --force -i networkcrm-imessage-sync "$bin" >/dev/null 2>&1; then
    echo "appify.sh: warning — codesign failed on the iMessage helper; not shipping it." >&2
    rm -rf "$out_dir"
    return 1
  fi
  chmod 755 "$bin"
  # Smoke test. --help reads nothing, needs no permission and touches no message,
  # so it proves the binary executes on this architecture without being a test
  # that quietly copies a database. This is only "does it run".
  if ! "$bin" --help >/dev/null 2>&1; then
    echo "appify.sh: ERROR — the iMessage helper binary does not run; not shipping it." >&2
    rm -rf "$out_dir"
    return 1
  fi
  cp "$REPO_ROOT/packaging/install_imessage_sync.sh" "$out_dir/"
  cp "$REPO_ROOT/packaging/uninstall_imessage_sync.sh" "$out_dir/"
  cp "$REPO_ROOT/packaging/IMESSAGE_SYNC.md" "$out_dir/"
  chmod 755 "$out_dir/install_imessage_sync.sh" "$out_dir/uninstall_imessage_sync.sh"
  echo "appify.sh: embedded the opt-in iMessage FDA helper -> $out_dir"
  return 0
}

# ── Bundle a relocatable CPython into the .app ────────────────────────────────
# Why this exists at all: the app is Python, and macOS has shipped no usable
# system python3 since 12.3. Without this, a user with no Homebrew, pyenv
# or Xcode tools cannot run the app — and Apple's developer tools are NOT a fix,
# because they install 3.9.6, which this app rejects (see NC-056).
#
# This is the same thing many other Mac apps do: they carry their own runtime
# inside the bundle. Nothing is installed on the user's
# machine, no admin password is asked for, and drag-to-Applications keeps working.
# A .pkg installer that put a system-wide Python on someone's Mac would need a
# paid Developer ID Installer certificate AND an admin password AND would mutate
# a machine we cannot see. Wrong mechanism for the same goal.
#
# NO NETWORK FETCH. The whole build is offline by design (see the header), so the
# interpreter comes from a tarball cached under packaging/.cache/. If it is not
# there the build says so and produces the lean bundle instead of silently
# shipping something that cannot run.
PY_CACHE_DIR="${NC_PY_CACHE_DIR:-$SCRIPT_DIR/.cache}"
APPIFY="$SCRIPT_DIR/appify.sh"

# ── The prepared interpreter is kept, and cloned into each build (NC-225) ─────
# Unpacking, trimming, stripping and signing the interpreter, then building a
# throwaway environment to prove it works, writes about 190 MB, and the app is
# rebuilt many times a day. None of it changes unless the tarball or this script
# does. So the finished tree is kept in packaging/.cache/ under a name made from
# the tarball's name, size and date and this script's size and date, and each
# later build copies it in as an APFS clone: the copy shares the same blocks on
# disk instead of writing them again. Edit this script (the trim list, say) and
# the next build prepares a fresh one.
prepared_key() {
  # prepared_key <tarball>
  printf '%s-%s-%s' "$(basename "$1" .tar.gz)" \
    "$(stat -f '%z.%m' "$1" 2>/dev/null || echo 0)" \
    "$(stat -f '%z.%m' "$APPIFY" 2>/dev/null || echo 0)"
}

clone_tree() {
  # clone_tree <from> <to>: a clone where the disk supports one (APFS does), a
  # plain copy where it does not.
  cp -cR "$1" "$2" 2>/dev/null || { rm -rf "$2"; cp -R "$1" "$2"; }
}

probe_python() {
  # probe_python <tree>: prints "<python version> <sqlite version>", or fails.
  "$1/bin/python3" -c 'import sys, sqlite3, ssl, json, hashlib, sqlite3 as s; print(sys.version.split()[0], s.sqlite_version)' 2>&1
}

forget_older_prepared() {
  # forget_older_prepared <keep>: keeps the one just made and the newest other
  # one (a private build and a handout build can alternate between two), and
  # removes the rest, so the cache folder does not grow with every edit.
  local keep="$1" d n=0
  while IFS= read -r d; do
    [ "$d" = "$keep" ] && continue
    n=$((n + 1))
    [ "$n" -gt 1 ] && rm -rf "$d"
  done < <(ls -1dt "$PY_CACHE_DIR"/prepared-* 2>/dev/null)
  return 0
}

prepare_python() {
  # prepare_python <tarball> <out_dir>  -> 0 with <out_dir>/python ready to ship,
  # 1 if not (reason printed). Works inside PY_CACHE_DIR so the finished tree is
  # moved into place, not copied.
  local tarball="$1" out="$2" name staging
  name="$(basename "$tarball")"
  if ! staging="$(mktemp -d "$PY_CACHE_DIR/.staging.XXXXXX" 2>/dev/null)"; then
    echo "appify.sh: cannot write in $PY_CACHE_DIR — skipping the bundled interpreter." >&2
    return 1
  fi
  if ! tar xzf "$tarball" -C "$staging" 2>/dev/null; then
    echo "appify.sh: could not extract $name — skipping the bundled interpreter." >&2
    rm -rf "$staging"
    return 1
  fi
  # python-build-standalone unpacks to a single "python/" directory.
  local tree="$staging/python"
  if [ ! -x "$tree/bin/python3" ]; then
    echo "appify.sh: $name has no bin/python3 where expected — skipping." >&2
    rm -rf "$staging"
    return 1
  fi

  # Trim. Everything removed here is either a GUI toolkit this app never touches
  # (its window is native Swift) or build-time material. Measured: 66MB
  # extracted -> 54MB after this plus the strip below. Nothing here is imported
  # by crm/ — asserted by tests/test_packaging.py.
  local libdir
  libdir="$(cd "$tree/lib" && pwd)"
  rm -rf "$tree"/lib/tcl* "$tree"/lib/tk* "$tree"/lib/itcl* "$tree"/lib/thread* \
         "$tree"/lib/libtcl* "$tree"/lib/libtk* 2>/dev/null || true
  rm -rf "$libdir"/python3.*/tkinter "$libdir"/python3.*/turtledemo \
         "$libdir"/python3.*/idlelib "$libdir"/python3.*/lib2to3 \
         "$libdir"/python3.*/config-*-darwin 2>/dev/null || true
  rm -f  "$libdir"/python3.*/lib-dynload/_tkinter*.so 2>/dev/null || true
  rm -rf "$tree/include" "$tree/share" 2>/dev/null || true
  # The bundled test suite is ~20% of the tree and is never run from the app.
  rm -rf "$libdir"/python3.*/test "$libdir"/python3.*/*/tests 2>/dev/null || true

  # Debug symbols. -x keeps the exported symbols the extension modules resolve
  # against; a full strip breaks dynamic loading.
  strip -x "$tree"/bin/python3.* 2>/dev/null || true
  strip -x "$tree"/lib/libpython3.*.dylib 2>/dev/null || true

  # Re-sign ad-hoc, shallowly. Every Mach-O we touched needs its signature to
  # match its new contents or dyld refuses to load it.
  #
  # NEVER add --options runtime here. Under the Hardened Runtime, Library
  # Validation rejects sibling ad-hoc-signed dylibs because they carry no Team
  # ID to match against, and re-signing them ad-hoc does not fix it — there is
  # no Team ID to produce without a paid Developer ID. Ad-hoc with no hardened
  # runtime loads fine; hardened + ad-hoc does not.
  local m
  while IFS= read -r m; do
    codesign --force --sign - "$m" >/dev/null 2>&1 || true
  done < <(find "$tree" -type f \( -name '*.dylib' -o -name '*.so' -o -perm -u+x \) 2>/dev/null | head -4000)

  # Prove it still works BEFORE it goes in the bundle. A silently broken
  # interpreter here is the earlier failure all over again.
  local probe
  if ! probe="$(probe_python "$tree")"; then
    echo "appify.sh: the trimmed interpreter does not run — NOT shipping it." >&2
    echo "appify.sh:   $probe" >&2
    rm -rf "$staging"
    return 1
  fi
  # crm/python_env.py, which the app runs on this interpreter, depends on venv+pip
  # working, so that is part of the contract and gets checked too, not assumed. Once per prepared
  # tree, not once per build: its answer cannot change while the tree does not.
  local vtmp
  vtmp="$(mktemp -d)"
  if ! "$tree/bin/python3" -m venv "$vtmp/v" >/dev/null 2>&1 \
       || ! "$vtmp/v/bin/python3" -m pip --version >/dev/null 2>&1; then
    echo "appify.sh: the trimmed interpreter cannot build a venv with pip — NOT shipping it." >&2
    rm -rf "$staging" "$vtmp"
    return 1
  fi
  rm -rf "$vtmp"

  rm -rf "$out"
  mkdir -p "$out"
  mv "$tree" "$out/python"
  rm -rf "$staging"
  # Written last, so a run cut short leaves no tree that looks finished.
  : > "$out/.ready"
  return 0
}

embed_python() {
  # embed_python <dest_dir>  -> 0 on success, 1 if skipped (reason printed)
  local dest="$1"
  local host_arch tarball name
  host_arch="$(uname -m)"

  # arm64 only for now, deliberately. An x86_64 tarball exists upstream and a
  # universal bundle is a later job; shipping an arm64 interpreter to an Intel
  # Mac would fail in the worst way, which is silently falling back to the
  # very "no Python" error this whole function exists to remove.
  if [ "$host_arch" != "arm64" ]; then
    echo "appify.sh: host is $host_arch, not arm64 — skipping the bundled interpreter." >&2
    echo "appify.sh:   (Intel support is deliberately not done yet; this bundle will need a Python 3.10+ on the target Mac.)" >&2
    return 1
  fi

  tarball=""
  for cand in "$PY_CACHE_DIR"/cpython-*-aarch64-apple-darwin-install_only.tar.gz; do
    [ -f "$cand" ] && tarball="$cand"
  done
  if [ -z "$tarball" ]; then
    echo "appify.sh: no cached CPython tarball in $PY_CACHE_DIR — skipping the bundled interpreter." >&2
    echo "appify.sh:   This build makes no network fetch by design. To enable it, download an" >&2
    echo "appify.sh:   'install_only' aarch64-apple-darwin build from" >&2
    echo "appify.sh:   https://github.com/astral-sh/python-build-standalone/releases into that directory." >&2
    return 1
  fi
  name="$(basename "$tarball")"

  local prepared probe
  prepared="$PY_CACHE_DIR/prepared-$(prepared_key "$tarball")"
  if [ -f "$prepared/.ready" ] && probe="$(probe_python "$prepared/python")"; then
    echo "appify.sh: reusing the interpreter prepared earlier from $name (NC-225)"
  else
    rm -rf "$prepared"
    prepare_python "$tarball" "$prepared" || return 1
    forget_older_prepared "$prepared"
    probe="$(probe_python "$prepared/python")" || return 1
  fi

  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  clone_tree "$prepared/python" "$dest"

  # Licence texts. CPython's own LICENSE.txt travels inside the tree, but the
  # statically linked C libraries' do not, so they are written out here.
  # Versions are read off the interpreter rather than hardcoded so the file
  # cannot drift from what actually shipped.
  write_python_licenses "$dest" "$name"

  local shipped
  shipped="$(du -sh "$dest" 2>/dev/null | cut -f1 | tr -d ' ')"
  echo "appify.sh: bundled CPython $probe -> $dest ($shipped, from $name)"
  return 0
}

write_python_licenses() {
  local tree="$1" tarball_name="$2"
  local out="$tree/THIRD-PARTY-LICENSES.txt"
  # Ask the interpreter what it contains. readline is the one that matters for
  # an MIT project: GNU readline is GPL-3, libedit is BSD-3. python-build-
  # standalone links libedit on macOS, and this prints which one it really is
  # rather than trusting that.
  local inv
  inv="$("$tree/bin/python3" - <<'PYINV' 2>/dev/null || true
def v(fn, default="bundled"):
    try:
        return fn()
    except Exception:
        return default
import sys
rows = [("CPython", sys.version.split()[0], "PSF-2.0 (Python-2.0, CNRI, BeOpen)")]
rows.append(("SQLite", v(lambda: __import__("sqlite3").sqlite_version), "Public domain"))
rows.append(("OpenSSL", v(lambda: __import__("ssl").OPENSSL_VERSION.split()[1]), "Apache-2.0"))
rows.append(("zlib", v(lambda: __import__("zlib").ZLIB_RUNTIME_VERSION), "zlib licence"))
rows.append(("libexpat", v(lambda: ".".join(map(str, __import__("xml.parsers.expat", fromlist=["x"]).version_info))), "MIT"))
rows.append(("libmpdec", v(lambda: __import__("_decimal").__libmpdec_version__), "BSD-2-Clause"))
rows.append(("libffi", "bundled", "MIT"))
rows.append(("liblzma / xz", "bundled", "0BSD"))
rows.append(("bzip2", "bundled", "bzip2 licence (BSD-like)"))
rows.append(("ncurses", "bundled", "MIT-style (ncurses licence)"))
rows.append(("libuuid", v(lambda: __import__("_uuid") and "bundled"), "BSD-3-Clause"))
def rl():
    import readline
    b = getattr(readline, "backend", "")
    lv = getattr(readline, "_READLINE_LIBRARY_VERSION", "")
    if b == "editline" or "EditLine" in str(lv):
        return ("libedit (NetBSD editline)", str(lv or "bundled"), "BSD-3-Clause")
    return ("GNU readline", str(lv or "bundled"), "GPL-3.0  << COPYLEFT: REVIEW BEFORE SHIPPING")
rows.append(v(rl, ("readline", "unknown", "UNDETERMINED - REVIEW")))
w = max(len(r[0]) for r in rows)
for n, ver, lic in rows:
    print(f"  {n.ljust(w)}  {str(ver):<12}  {lic}")
PYINV
)"
  cat > "$out" <<LICEOF
Third-party components bundled inside Network CRM.app
=====================================================

Network CRM itself is MIT licensed; see LICENSE in Contents/Resources/app/.

Contents/Resources/python/ is an unmodified-then-trimmed redistribution of a
python-build-standalone CPython build:

    $tarball_name
    https://github.com/astral-sh/python-build-standalone

Files removed from that build before shipping: the Tcl/Tk libraries and the
tkinter extension, turtledemo, idlelib, lib2to3, the C headers (include/),
share/, the platform config directory, and the CPython test suite. Nothing was
added or patched. Binaries were stripped of debug symbols (strip -x) and
re-signed ad-hoc.

CPython's own licence travels with the tree at:
    Contents/Resources/python/lib/python3.*/LICENSE.txt
pip's vendored dependencies carry their licences at:
    Contents/Resources/python/lib/python3.*/site-packages/pip-*.dist-info/licenses/

Components and their licences, as reported by the interpreter that actually
shipped in this bundle:

$inv

Notes
-----
* The readline line above is generated by asking the module which backend it
  is. On macOS python-build-standalone links libedit (BSD-3-Clause), not GNU
  readline (GPL-3.0). If that line ever says GNU readline, the copyleft
  question must be settled before distributing this bundle.
* SQLite is public domain and requires no attribution; it is listed for
  completeness.
* No component here is copyleft, so bundling them does not affect Network
  CRM's own MIT licence.
* The full text of each component's licence follows, after CPython's (which
  is in the tree, above).

Generated by packaging/appify.sh at build time.
LICEOF
  # NC-249. The texts themselves: most of the licences above require theirs to
  # travel with the program when it is handed out built, and the archive the
  # build uses carries only CPython's and pip's. These were taken once from the
  # same build's "full" archive (its python/licenses/ folder) and are kept in
  # packaging/licenses/, so a build needs no download. That folder's README
  # says which archive and why some texts were left out.
  local f
  for f in "$REPO_ROOT"/packaging/licenses/LICENSE.*.txt; do
    [ -f "$f" ] || continue
    printf '\n\n%s\n%s\n\n' "$(basename "$f" .txt | sed 's/^LICENSE\.//')" "----------------------------------------" >> "$out"
    cat "$f" >> "$out"
  done
}

strip_caches() {
  # strip_caches <bundle>...  NC-310. Every compiled Python cache and Finder
  # file, anywhere in the bundle, the bundled interpreter's own folders
  # included: a cache records the absolute folder it was compiled in, so one
  # written while a build sat in someone's home folder names that home folder.
  # Run as the build's last step, after everything that could have written one.
  local b
  for b in "$@"; do
    [ -d "$b" ] || continue
    find "$b" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$b" \( -name '*.pyc' -o -name '.DS_Store' \) -type f -delete 2>/dev/null || true
  done
}

public_repo() {
  # NC-315. The repository whose releases a handed-out copy asks about, read
  # from the one place it is written down (PUBLIC_REPO in crm/updates.py).
  sed -n 's/^PUBLIC_REPO = "\([^"]*\)".*/\1/p' "$REPO_ROOT/crm/updates.py" | head -1
}


echo "appify.sh: building into $DIST_DIR (mode: $([ "$EMBED" = 1 ] && echo embedded || echo developer))"
# Built beside the finished app and swapped in by rename at the end, so a failed build
# leaves the last good one, and an open copy's program file is never rewritten in place
# (macOS stops a running program whose signed code changes under it; one that was
# renamed away keeps working until it quits).
FINAL_APP_DIR="$APP_DIR"
STAGE_DIR="$DIST_DIR/.building"
rm -rf "$STAGE_DIR"
APP_DIR="$STAGE_DIR/Network CRM.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
trap 'rm -rf "$STAGE_DIR"' EXIT

# The window: the app's one program (NC-330).
if ! "$REPO_ROOT/native/compile.sh" "$APP_DIR/Contents/MacOS/$EXE_NAME"; then
  echo "appify.sh: the window did not compile; nothing was replaced." >&2
  exit 1
fi
write_plist "$APP_DIR" "Network CRM" "$BUNDLE_ID" "$EXE_NAME"
build_icon "$APP_DIR/Contents/Resources/appicon.icns"

PY_SHIPPED=0
IMSG_HELPER_SHIPPED=0
if [ "$EMBED" = 1 ]; then
  APP_CODE_DIR="$APP_DIR/Contents/Resources/app"
  mkdir -p "$APP_CODE_DIR"
  # The engine's code. static/ (the old web page) is not shipped: the window is native,
  # and nothing in the app opens a page (NC-330).
  cp -R "$REPO_ROOT/crm" "$APP_CODE_DIR/crm"
  # Outside Resources/app on purpose: that tree is the Python the app runs and
  # tests/test_dev_parity.py asserts it stays byte-identical to the repo. The
  # helper is a sibling payload the user may install, not code the app imports.
  if compile_imessage_helper "$APP_DIR/Contents/Resources/imessage-sync"; then
    IMSG_HELPER_SHIPPED=1
  fi
  # Never ship compiled bytecode or Finder droppings. A stale .pyc inside a
  # distributed app is a genuine hazard: it can shadow the .py it was built from,
  # so a user runs code that no longer exists in the bundle.
  find "$APP_CODE_DIR/crm" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$APP_CODE_DIR/crm" \( -name '*.pyc' -o -name '.DS_Store' \) -delete 2>/dev/null || true
  cp "$REPO_ROOT/requirements.txt" "$APP_CODE_DIR/requirements.txt"
  # NC-316: the exact version and fingerprints of every package the first start
  # installs, and of the opt-in meaning-based search.
  cp "$REPO_ROOT/requirements-lock.txt" "$APP_CODE_DIR/requirements-lock.txt"
  cp "$REPO_ROOT/requirements-semantic.txt" "$APP_CODE_DIR/requirements-semantic.txt"
  cp "$REPO_ROOT/LICENSE" "$APP_CODE_DIR/LICENSE"
  cp "$REPO_ROOT/THIRD_PARTY_NOTICES.md" "$APP_CODE_DIR/THIRD_PARTY_NOTICES.md"
  echo "appify.sh: embedded a copy of crm/, requirements.txt, requirements-lock.txt, requirements-semantic.txt, LICENSE, THIRD_PARTY_NOTICES.md into $APP_CODE_DIR"

  # The interpreter. Outside Resources/app for the same reason as the helper.
  if embed_python "$APP_DIR/Contents/Resources/python"; then
    PY_SHIPPED=1
  else
    echo "appify.sh: the app starts its engine on its own Python, so without one it cannot open (NC-330)." >&2
    echo "appify.sh: put the CPython archive in $PY_CACHE_DIR and build again; nothing was replaced." >&2
    exit 1
  fi
else
  # Developer build: the engine's code is this checkout, run on its .venv.
  echo "$REPO_ROOT" > "$APP_DIR/Contents/Resources/repo_path.txt"
  echo "appify.sh: recorded repo path -> $REPO_ROOT"
fi

# NC-203: which build this bundle is. The app hands it to the engine it starts, which
# reports it on /api/status, and Settings shows it.
BUILD_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
echo "$BUILD_ID" > "$APP_DIR/Contents/Resources/build_id.txt"
echo "appify.sh: build $BUILD_ID"

# NC-315: where new versions come from. Written for a handout, or for any build
# made with NC_UPDATE_REPO set; the app hands it to the engine, and the update
# check is otherwise off (crm/updates.py says why).
UPDATE_REPO="${NC_UPDATE_REPO:-}"
if [ "$HANDOUT" = 1 ] && [ -z "$UPDATE_REPO" ]; then
  UPDATE_REPO="$(public_repo)"
fi
if [ -n "$UPDATE_REPO" ]; then
  echo "$UPDATE_REPO" > "$APP_DIR/Contents/Resources/update_repo.txt"
  echo "appify.sh: new versions will be looked for at github.com/$UPDATE_REPO/releases"
fi

strip_caches "$APP_DIR"

# NC-311: a handout is all or nothing. Without its own Python the people it is
# for would need Python 3.10 installed, which none of them has; without the
# helper there is no automatic copying; without a release address it can never
# learn of a new version.
if [ "$HANDOUT" = 1 ]; then
  missing=""
  [ "$PY_SHIPPED" = 1 ] || missing="$missing its own Python (put the CPython archive in $PY_CACHE_DIR),"
  [ "$IMSG_HELPER_SHIPPED" = 1 ] || missing="$missing the copying helper (needs xcrun swiftc),"
  [ -n "$UPDATE_REPO" ] || missing="$missing the release address (PUBLIC_REPO in crm/updates.py),"
  if [ -n "$missing" ]; then
    echo "appify.sh --handout: refusing; it lacks${missing%,}. Nothing was replaced." >&2
    exit 1
  fi
fi

# Signed as one piece, ad hoc: macOS sees one coherent bundle, and any later change
# inside it shows as a broken seal. Not --deep: the copying helper inside keeps the
# signature it was given above, because its Full Disk Access grant is tied to it.
HELPER_BIN="$APP_DIR/Contents/Resources/imessage-sync/networkcrm-imessage-sync"
helper_before=""
[ -f "$HELPER_BIN" ] && helper_before="$(codesign -dvvv "$HELPER_BIN" 2>&1 | grep '^CDHash=' || true)"
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP_DIR"
codesign --verify --strict "$APP_DIR"
if [ -n "$helper_before" ] && [ "$(codesign -dvvv "$HELPER_BIN" 2>&1 | grep '^CDHash=' || true)" != "$helper_before" ]; then
  echo "appify.sh: signing the app changed the helper's signature, which would drop its Full Disk Access grant; not shipping this build." >&2
  exit 1
fi
echo "appify.sh: signed Network CRM.app as one piece (ad hoc)"

# Smoke test, on the bundle as built: the window's own --engine-check starts the
# engine from inside it on a throwaway folder, checks it opened no network port and
# answers only through its private file, that it stops when the app vanishes, and that
# quitting leaves nothing running. No window opens; the real data is never touched.
if [ "$EMBED" = 1 ]; then
  if ! check_out="$("$APP_DIR/Contents/MacOS/$EXE_NAME" --engine-check 2>&1)"; then
    echo "$check_out" >&2
    echo "appify.sh: smoke test FAILED; nothing was replaced." >&2
    exit 1
  fi
  echo "$check_out" | sed 's/^/appify.sh:   /'
  # The check ran the bundle's own code: it must have written nothing inside it.
  if ! codesign --verify --strict "$APP_DIR" 2>/dev/null; then
    echo "appify.sh: the smoke test wrote inside the app (its seal broke); not shipping this build." >&2
    exit 1
  fi
  echo "appify.sh: smoke test passed"
fi

# Swap in. The old copy is renamed away first, so a running one keeps working.
mkdir -p "$DIST_DIR"
OLD="$DIST_DIR/.replaced-$$"
[ -d "$FINAL_APP_DIR" ] && mv "$FINAL_APP_DIR" "$OLD"
mv "$APP_DIR" "$FINAL_APP_DIR"
rm -rf "$OLD" "$STAGE_DIR"
APP_DIR="$FINAL_APP_DIR"
# Every version before NC-330 also built a "Quit Network CRM" app; it has nothing left
# to stop.
[ -d "$OLD_QUIT_APP_DIR" ] && rm -rf "$OLD_QUIT_APP_DIR" && echo "appify.sh: removed the old Quit Network CRM.app (no longer needed)"

echo "appify.sh: done."
echo "  -> $APP_DIR"
if [ "$EMBED" = 1 ]; then
  echo "  -> Network CRM.app carries its own Python. Nothing needs installing on the target Mac (Apple chip)."
else
  echo "  -> Developer build: it runs this checkout's crm/ on the checkout's .venv."
fi
echo "  -> Opening it starts its engine; quitting it stops everything. No browser, no network address."
if [ "$HANDOUT" = 1 ]; then
  echo "  -> A copy to hand out. Hand out this exact copy without opening it first (NC-310)."
  echo "     Next: packaging/make_dmg.sh puts it in a disk image with its Read Me."
fi
