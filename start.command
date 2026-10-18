#!/bin/bash
# ── Network CRM launcher ──────────────────────────────────────────────────────
# Double-click this file in Finder to start the app. It runs entirely on your Mac;
# your data never leaves it. A browser tab opens automatically; close this Terminal
# window (or press Ctrl+C) to stop the server.

cd "$(dirname "$0")" || exit 1
# NC-321: files the app makes are readable by this account only, as in the
# packaged app's launcher. The engine sets this itself too.
umask 077

# Prefer the project's virtualenv so optional features (e.g. AI email summaries,
# which need the `anthropic` package) work. Fall back to system python3.
# The one inside this folder comes first: it is the one README tells you to make.
# A .venv one level up is checked only after it, so an unrelated environment
# there can never win over the project's own (NC-247).
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "../.venv/bin/python" ]; then
  PY="../.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "Python 3 not found. Install it from https://www.python.org/downloads/ and try again."
  read -r -p "Press Return to close." _
  exit 1
fi

echo "Starting Network CRM with: $PY"
"$PY" crm/app.py
status=$?

# If the server exits with an error, keep the window open so the message is readable.
# Not always a crash: app.py refuses to start on a Python older than 3.10 and
# prints why (NC-136), which is the most likely non-zero exit a user sees.
if [ $status -ne 0 ]; then
  echo ""
  echo "Network CRM stopped (code $status). The message above explains why."
  read -r -p "Press Return to close." _
fi
