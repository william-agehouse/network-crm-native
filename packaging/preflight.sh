#!/bin/bash
# ── Network CRM shipping preflight ────────────────────────────────────────────
# Run this before believing "it works". It answers one question: would the
# PRODUCT behave the way dev just verified? It compares the two interpreters,
# checks the built bundle is fresh, confirms the shipped venv can import
# everything requirements.txt promises, and prints PASS / FAIL.
#
# Strictly read-only: it never installs, builds, or edits anything.
#
#   ./packaging/preflight.sh
#
# Exit code is 0 only if every check that could run passed.
set -u

# ── Locations ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # .../network-crm/packaging
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"                            # .../network-crm
REQ="$REPO/requirements.txt"
CRM="$REPO/crm"

BUNDLE="$REPO/packaging/dist/Network CRM.app"
BUNDLE_APP="$BUNDLE/Contents/Resources/app"
BUNDLE_CRM="$BUNDLE_APP/crm"
EMBEDDED_PY="$BUNDLE/Contents/Resources/python/bin/python3"
APP_SUPPORT="$HOME/Library/Application Support/NetworkCRM"
APP_VENV_PY="$APP_SUPPORT/venv/bin/python3"

# Interpreter dev verifies on: the checkout's own venv, then a .venv one level up
# (if you keep one there), else this shell's python3. Own first, as in
# start.command (NC-247).
if [ -x "$REPO/.venv/bin/python3" ]; then
  DEV_PY="$REPO/.venv/bin/python3"
elif [ -x "$REPO/../.venv/bin/python3" ]; then
  DEV_PY="$(cd "$REPO/.." && pwd)/.venv/bin/python3"
else
  DEV_PY="$(command -v python3 || true)"
fi

# The interpreter the app RUNS on = the venv crm/python_env.py builds under
# ~/Library/Application Support and installs requirements.txt into. Any embedded
# CPython under Resources/python is only the BASE the venv is built from (so a Mac
# with no Python can run the app); it is deliberately bare and is NOT the runtime.
if [ -x "$APP_VENV_PY" ]; then
  SHIP_PY="$APP_VENV_PY"; SHIP_LABEL="app-support venv (runtime)"
else
  SHIP_PY=""; SHIP_LABEL="(app never launched)"
fi

FAIL=0
say()  { printf '%s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; }
warn() { printf '  SKIP  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAIL=1; }

# import names requirements.txt maps to (must mirror test_dev_parity.DIST_IMPORT).
import_name() {
  case "$1" in
    python-telegram-bot) echo telegram ;;
    apscheduler)         echo apscheduler ;;
    google-auth-oauthlib) echo google_auth_oauthlib ;;
    google-api-python-client) echo googleapiclient ;;
    sentence-transformers) echo sentence_transformers ;;
    *) echo "${1//-/_}" ;;
  esac
}

# Distribution names requirements.txt installs (comments stripped, lower-cased).
reqs() {
  sed -e 's/#.*//' "$REQ" \
    | sed -E 's/[[:space:]]//g; s/(>=|==|<=|~=|>|<|\[).*//' \
    | tr 'A-Z' 'a-z' | grep -v '^$'
}

say "Network CRM — shipping preflight"
say "  repo:              $REPO"
say "  dev interpreter:   ${DEV_PY:-<none>}"
say "  shipped ($SHIP_LABEL): ${SHIP_PY:-<none>}"
[ -x "$EMBEDDED_PY" ] && say "  embedded base:     $EMBEDDED_PY (bootstrap only — deliberately bare)"
say ""

# ── 1. Shipped interpreter imports every requirement ───────────────────────────
say "1. Shipped interpreter imports everything requirements.txt promises"
if [ -z "$SHIP_PY" ]; then
  warn "no packaged interpreter installed yet (fresh machine) — nothing to check"
else
  for dist in $(reqs); do
    mod="$(import_name "$dist")"
    if "$SHIP_PY" -c "import $mod" >/dev/null 2>&1; then
      pass "$dist ($mod)"
    else
      bad "$dist ($mod) — NOT importable in the shipped venv; the feature that needs it degrades on every install"
    fi
  done
fi
say ""

# ── 2. Dev vs shipped: what the two environments differ on ──────────────────────
say "2. Dev vs shipped interpreter (informational diff of installed distributions)"
if [ -n "$DEV_PY" ] && [ -n "$SHIP_PY" ]; then
  D="$(mktemp)"; S="$(mktemp)"
  "$DEV_PY"  -m pip freeze 2>/dev/null | tr 'A-Z' 'a-z' | sort > "$D"
  "$SHIP_PY" -m pip freeze 2>/dev/null | tr 'A-Z' 'a-z' | sort > "$S"
  only_dev="$(comm -23 "$D" "$S" | sed 's/^/    only in dev:     /')"
  only_ship="$(comm -13 "$D" "$S" | sed 's/^/    only in shipped: /')"
  [ -n "$only_dev" ]  && say "$only_dev"
  [ -n "$only_ship" ] && say "$only_ship"
  say "  (informational — differences are expected; opt-in stacks like torch live only in dev)"
  rm -f "$D" "$S"
else
  warn "need both interpreters to diff — skipped"
fi
say ""

# ── 3. Bundle freshness: crm/*.py + requirements.txt byte-match the repo ────────
say "3. Built bundle ships the repo's current code and dependency list"
if [ ! -d "$BUNDLE_APP" ]; then
  warn "no built bundle at $BUNDLE — nothing shipped yet"
else
  stale=0
  for f in "$CRM"/*.py; do
    b="$BUNDLE_CRM/$(basename "$f")"
    if [ ! -f "$b" ]; then bad "crm/$(basename "$f") missing from bundle"; stale=1
    elif ! cmp -s "$f" "$b"; then bad "crm/$(basename "$f") differs from repo (stale)"; stale=1; fi
  done
  for b in "$BUNDLE_CRM"/*.py; do
    f="$CRM/$(basename "$b")"
    [ -f "$f" ] || { bad "crm/$(basename "$b") in bundle but not in repo"; stale=1; }
  done
  if [ -f "$BUNDLE_APP/requirements.txt" ]; then
    cmp -s "$REQ" "$BUNDLE_APP/requirements.txt" \
      && pass "requirements.txt matches repo" \
      || { bad "bundle requirements.txt differs from repo (a fresh install would get the wrong dependencies, such as a missing anthropic)"; stale=1; }
  else
    bad "bundle ships no requirements.txt"
  fi
  [ "$stale" = 0 ] && pass "crm/*.py byte-identical to repo"
fi
say ""

# ── 4. No compiled cruft in the bundle's APP CODE ───────────────────────────────
# Scoped to Contents/Resources/app: an embedded CPython under Resources/python
# legitimately ships its stdlib .pyc. The risk is first-party source cache
# (crm/__pycache__) that can execute after the source changed.
say "4. Bundle app code carries no stale __pycache__ / *.pyc"
if [ ! -d "$BUNDLE_APP" ]; then
  warn "no built bundle — nothing to check"
else
  cruft="$(find "$BUNDLE_APP" \( -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' \) 2>/dev/null)"
  if [ -z "$cruft" ]; then
    pass "no __pycache__ / *.pyc in the app tree"
  else
    while IFS= read -r line; do bad "cruft: ${line#"$BUNDLE"/}"; done <<< "$cruft"
  fi
fi
say ""

# ── Verdict ─────────────────────────────────────────────────────────────────────
if [ "$FAIL" = 0 ]; then
  say "RESULT: PASS — dev and the shipped product agree."
  exit 0
else
  say "RESULT: FAIL — the product would NOT behave the way dev verified. Fix the FAILs above."
  exit 1
fi
