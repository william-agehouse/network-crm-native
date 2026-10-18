#!/usr/bin/env bash
# Pull the scraper Mac's output and bring it into this Mac's master crm.db:
# first the LinkedIn enrichment facts, then the scraped profile photos, then the
# re-parsed "LinkedIn extras" (shared history / mutuals / recent activity). Thin
# wrapper — the actual work lives in crm/merge_scraped_facts.py --pull,
# crm/import_scraped_photos.py --pull and crm/enrich_li_extras.py --pull (the
# facts merge is also what the app's "LinkedIn profile facts" card runs).
#
#   ./deploy/pull_and_merge.sh              # merge facts, pull photos, enrich extras
#   ./deploy/pull_and_merge.sh --dry-run    # preview only
#
# Override target host/key via env: IMAC=... KEY=... ./deploy/pull_and_merge.sh
#
# ONLY NEEDED WHEN THE SCRAPER IS A SEPARATE MACHINE (NC-012). If
# scrape_linkedin_profiles.py runs on this same Mac, it already writes straight
# into this Mac's own crm.db — this whole script, and the three --pull calls
# below, exist only to bridge a scraper on a DIFFERENT machine back to this
# one. See crm/merge_scraped_facts.py's docstring. On one machine, skip this
# script and pass --source-dir data/li_raw/ to import_scraped_photos.py /
# enrich_li_extras.py directly if you ever do want to re-run just those two.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"        # …/network-crm

# The extras step (enrich_li_extras.py) needs the `anthropic` package, which the
# system python3 does not have. Use a venv for all three steps (the first two are
# stdlib-only, so it's fine for them too): the repo's own .venv first, which is
# the one README says to make, then one in the parent folder (if you keep one
# there), then python3 (the same order as start.command).
if [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
elif [ -x "$HERE/../.venv/bin/python" ]; then
  PY="$HERE/../.venv/bin/python"
else
  PY="python3"
fi

"$PY" "$HERE/crm/merge_scraped_facts.py" --pull "$@"
"$PY" "$HERE/crm/import_scraped_photos.py" --pull "$@"
"$PY" "$HERE/crm/enrich_li_extras.py" --pull "$@"
