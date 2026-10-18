#!/usr/bin/env python3
"""
Re-parse the LinkedIn profile text we already archived into "networking extras" —
the little relationship-warming details worth glancing at before a message:
shared history, named mutual connections, what they've posted about lately, and
their current company/title/headline.

NO new scraping, NO new LinkedIn visits. This ONLY re-reads the <id>.json
sidecars the scraper already saved on the scraper machine (each holds the rendered
"profile_text", a "mutuals" line, "headline", "name"). It rsyncs those *.json over
SSH — exactly like import_scraped_photos.py — sends each profile_text to
Claude Haiku, and stores the extracted extras as ONE JSON blob in a new
contacts.li_extra column. Storage only; the display of these lives elsewhere.

Safety mirrors the facts merge / photo import:
  • matched by id AND name — the sidecar li_raw/<id>.json "name" must equal the
    master contact's name, so a stale/renamed id can't attach the wrong extras;
  • idempotent — a contact whose li_extra is already populated is skipped
    (pass --force to re-extract every matched contact);
  • never crashes the batch — any LLM/parse error just skips that one contact.

ONE MACHINE, NO SSH (NC-012): if the scraper runs on this same Mac, --source-dir
reads the *.json sidecars straight from disk and skips the network entirely.
--pull is for the optional two-machine setup (LINKEDIN_SCRAPE_HANDOFF.md §6).

THE BUTTON (NC-372): the Data page's LinkedIn profile facts row runs this as
`--pull` through the ordinary import runner (importers._cmd_linkedin_facts with
the act's `extras` flag), never with --force. One press sends at most the spend
cap's number of profiles (settings.spend_cap(), the box on the Settings page's
AI row, the same cap Facts written by AI obeys); the rest wait for the next
press. most_per_profile_usd() is the ceiling the row's cost line is worked out
from. --limit overrides the cap when run by hand.

Usage:
  # two machines: pull the scraper machine's *.json sidecars over SSH and
  # enrich (what deploy/pull_and_merge.sh runs; IMAC=user@host KEY=~/.ssh/... to
  # override, exactly like the facts merge / photo import):
  python crm/enrich_li_extras.py --pull
  python crm/enrich_li_extras.py --pull --dry-run
  python crm/enrich_li_extras.py --pull --force

  # one machine: read the sidecars straight from the scraper's own output
  # directory, no SSH configured anywhere:
  python crm/enrich_li_extras.py --source-dir data/li_raw/
  python crm/enrich_li_extras.py --source-dir data/li_raw/ --dry-run
"""

from __future__ import annotations  # allow `str | None` hints on older Pythons (3.8)

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402,F401 — imported for its side effect: resolves paths / loads .env
import db  # noqa: E402
import settings  # noqa: E402

# Where the scraper machine lives (see LINKEDIN_SCRAPE_HANDOFF.md §6). Same
# host + key conventions as merge_scraped_facts.py / import_scraped_photos.py;
# override via env for a different setup.
# NO DEFAULT, deliberately (NC-006): a default would stop anything telling
# "configured" from "never touched".
# Both are MANAGED_KEYS settings — see merge_scraped_facts.py for the full note.
IMAC = os.environ.get("IMAC", "").strip()
IMAC_KEY = os.environ.get("KEY", "").strip() or os.path.expanduser("~/.ssh/imac_scraper")
# The scraper's raw per-profile archive (holds <id>.json + <id>.html.gz + <id>.jpg).
IMAC_RAW = "network-crm/data/li_raw/"

# The sidecars are small text files, but bound the pull so an asleep/dropped
# host fails instead of hanging.
PULL_TIMEOUT_S = 600

# Haiku is cheap and fast; cap the profile text we send so a huge page can't blow
# the budget (matches the scraper's own PROFILE_TEXT_CAP).
PROFILE_TEXT_CAP = 6000
MUTUALS_CAP = 200      # characters of the "mutual connections" line sent
HEADLINE_CAP = 200     # characters of the headline sent
LLM_MODEL = "claude-haiku-4-5-20251001"
LLM_MAX_TOKENS = 400

LLM_SYSTEM = """You extract LinkedIn "networking extras" — small relationship
details worth a glance before reaching out. You are given the rendered text of ONE
person's LinkedIn profile (their top card, About, any "Highlights" such as "You
both worked at X" / "You both studied at Y", recent activity), plus a line of
mutual connections and their headline.

Respond with ONLY valid JSON (no prose, no markdown fences) in EXACTLY this shape:
{
  "shared_history": [],   // up to 5 short strings, e.g. "You both worked at Acme (2021-2023)", "You both studied at State University". ONLY things shared with the viewer (the "You both …" highlights); [] if none.
  "mutual_names": [],     // the NAMED mutual connections shown, e.g. ["Ada", "Alan"]. Names only, drop the "and 12 others" tail; [] if none.
  "recent_activity": "",  // one short phrase on what they've posted/commented about lately, or "" if none shown
  "current_company": "",  // their current employer, or ""
  "current_title": "",    // their current role/title, or ""
  "headline": ""          // their profile headline verbatim, or ""
}

All fields are optional: use "" for unknown strings and [] for unknown lists. Never
invent details — extract only what the text actually shows."""

# NC-372. What one profile can cost at most, worked out from this file's own
# caps and LLM_MODEL's list price, so the Data page's cost line is a ceiling
# rather than a guess. Haiku 4.5's list price is 1 US dollar per million tokens
# read and 5 per million written (Anthropic's pricing table, checked
# 2026-10-17); it is not the Settings page's two prices, which are for the
# model Ask uses. Three characters per token is fewer than English text
# averages, so the estimate of what is read errs high.
PRICE_IN_PER_MTOK = 1.0
PRICE_OUT_PER_MTOK = 5.0
CHARS_PER_TOKEN_LOW = 3


def most_per_profile_usd() -> float:
    """The most one profile can cost, in US dollars: the instructions plus the
    longest profile text, mutuals line and headline this file ever sends, and
    the longest reply it allows."""
    chars = len(LLM_SYSTEM) + PROFILE_TEXT_CAP + MUTUALS_CAP + HEADLINE_CAP + 64
    tokens_in = chars / CHARS_PER_TOKEN_LOW
    return (tokens_in * PRICE_IN_PER_MTOK + LLM_MAX_TOKENS * PRICE_OUT_PER_MTOK) / 1_000_000


def pull_sidecars(dest_dir: str) -> None:
    """rsync the scraper machine's <id>.json profile sidecars into `dest_dir`.

    Pulls ONLY the *.json (the archived profile text we re-parse) — not the bulky
    .html.gz / .jpg. Like merge_scraped_facts.pull / import_scraped_photos.pull_photos,
    BatchMode + a short ConnectTimeout make an unreachable host fail in seconds with a
    real error instead of hanging on a password prompt."""
    # No host, nothing to reach (NC-006). Raised here rather than at import so
    # every offline half of this module stays usable on any Mac.
    if not IMAC:
        raise SystemExit(
            "No other Mac is set, so there are no saved profiles to read.\n"
            "Type its sign-in, as user@name, in the LinkedIn profile facts row on "
            "the Data page, and where its key file is if it is not at "
            "~/.ssh/imac_scraper.")
    ssh = (f"ssh -i {IMAC_KEY} -o IdentitiesOnly=yes -o BatchMode=yes "
           "-o ConnectTimeout=15")
    cmd = ["rsync", "-az", "-e", ssh,
           "--include=*.json", "--exclude=*",
           f"{IMAC}:{IMAC_RAW}", dest_dir + os.sep]
    print(f"Copying the saved profiles from {IMAC} …")
    unreachable = (
        f"Could not reach the other Mac ({IMAC}).\n"
        "%s\n"
        "Check that it is switched on, awake and on a network this Mac can reach, "
        "then try again."
    )
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PULL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # ConnectTimeout only covers connection setup — a transfer that stalls
        # mid-copy (Wi-Fi drop, host sleeping) lands here instead.
        raise SystemExit(unreachable % f"The transfer stalled and timed out after {PULL_TIMEOUT_S}s.")
    except OSError as exc:  # rsync binary missing/unrunnable — never a traceback
        raise SystemExit(unreachable % f"Could not run rsync: {exc}")
    if r.returncode != 0:
        raise SystemExit(unreachable % (r.stderr or r.stdout or "").strip())


def _is_populated(value: "str | None") -> bool:
    """True when li_extra already holds a real blob (so we skip it unless --force)."""
    return bool(value and value.strip() not in ("", "{}", "[]", "null"))


def _clean_str(value, cap: int) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return value.strip()[:cap]


def _clean_list(value, cap_items: int, cap_len: int) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        s = _clean_str(item, cap_len)
        if s:
            out.append(s)
        if len(out) >= cap_items:
            break
    return out


def normalize_extras(data: dict) -> dict:
    """Coerce the LLM's JSON into the exact, display-ready li_extra shape. Missing
    or wrong-typed fields fall back to '' / []. Kept tidy so the (later) UI can
    render it without defensive checks."""
    return {
        "shared_history": _clean_list(data.get("shared_history"), 5, 140),
        "mutual_names": _clean_list(data.get("mutual_names"), 12, 60),
        "recent_activity": _clean_str(data.get("recent_activity"), 200),
        "current_company": _clean_str(data.get("current_company"), 120),
        "current_title": _clean_str(data.get("current_title"), 120),
        "headline": _clean_str(data.get("headline"), 220),
    }


def extract_extras(client, profile_text: str, mutuals: str, headline: str) -> dict:
    """Call Haiku for one profile. Returns the normalized extras dict, or raises on
    any LLM/parse error (the caller turns that into a per-contact skip)."""
    parts = [profile_text[:PROFILE_TEXT_CAP].strip()]
    if mutuals:
        parts.append(f"\nMutual connections: {mutuals[:MUTUALS_CAP]}")
    if headline:
        parts.append(f"\nHeadline: {headline[:HEADLINE_CAP]}")
    content = "\n".join(p for p in parts if p)

    msg = client.messages.create(
        model=LLM_MODEL,
        max_tokens=LLM_MAX_TOKENS,
        system=LLM_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    raw = msg.content[0].text.strip()
    # strip markdown fences if present (mirror enrich_facts Pass C parsing)
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    return normalize_extras(json.loads(raw))


def _load_sidecar(path: str) -> "dict | None":
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def enrich(pull_dir: str, dest_path: str, force: bool, dry_run: bool, client,
           limit: "int | None" = None) -> dict:
    """Walk the pulled <id>.json sidecars and store networking extras for each
    matched contact. Matches on id (filename stem) AND the sidecar's name; skips
    contacts already carrying li_extra unless --force. Returns a stats dict.

    NC-372: at most `limit` profiles are sent to the model in one run (None
    sends every one). Every send counts, a failed one too, because a reply that
    could not be used was still billed. The rest are counted as `left`, for the
    next run.

    In --dry-run we still pull and predict every skip, but make NO LLM calls and NO
    writes — the tally shows what a real run would extract."""
    db.init_db(dest_path)                 # make sure the li_extra columns exist
    conn = db.get_db(dest_path)
    stats = {"found": 0, "extracted": 0, "skipped_existing": 0,
             "id_name_mismatch": 0, "errors": 0, "sent": 0, "left": 0}
    try:
        for fname in sorted(os.listdir(pull_dir)):
            if not fname.endswith(".json"):
                continue
            stem = fname[:-5]
            if not stem.isdigit():        # sidecars are keyed by contact id
                continue
            contact_id = int(stem)
            stats["found"] += 1

            sidecar = _load_sidecar(os.path.join(pull_dir, fname))
            scraped_name = (sidecar.get("name") or "").strip() if sidecar else ""

            row = conn.execute(
                "SELECT id, name, li_extra FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            # id+name guard: no such contact, or the sidecar name disagrees.
            if row is None or (row["name"] or "").strip() != scraped_name or not scraped_name:
                stats["id_name_mismatch"] += 1
                print(f"  Profile {contact_id}: saved under a name nobody here has, so skipped")
                continue
            if not force and _is_populated(row["li_extra"]):
                stats["skipped_existing"] += 1
                continue

            profile_text = (sidecar.get("profile_text") or "").strip() if sidecar else ""
            if not profile_text:
                stats["errors"] += 1
                print(f"  {row['name']}: the saved profile has no text, so skipped")
                continue

            if limit is not None and stats["sent"] >= limit:
                stats["left"] += 1        # NC-372: over this run's cap
                continue
            stats["sent"] += 1

            if dry_run:
                stats["extracted"] += 1
                print(f"  {row['name']}: would extract")
                continue

            try:
                extras = extract_extras(
                    client, profile_text,
                    (sidecar.get("mutuals") or ""), (sidecar.get("headline") or ""))
            except Exception as exc:  # LLM/parse error — never crash the batch
                stats["errors"] += 1
                print(f"  {row['name']}: Anthropic's reply could not be used ({exc}), so skipped",
                      file=sys.stderr)
                continue

            conn.execute(
                "UPDATE contacts SET li_extra = ?, li_extra_updated_at = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (json.dumps(extras, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 contact_id),
            )
            conn.commit()
            stats["extracted"] += 1
            n_hist = len(extras["shared_history"])
            n_mut = len(extras["mutual_names"])
            print(f"  {row['name']}: {n_hist} shared, {n_mut} mutual connections"
                  + (", recent posts" if extras["recent_activity"] else ""))
    finally:
        conn.close()
    return stats


def _make_client() -> "object | None":
    """Anthropic client for the real run, or None if unavailable (message printed)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("No key from Anthropic is set, so the saved profiles cannot be read. "
              "Add one in Settings, then try again.", file=sys.stderr)
        return None
    try:
        import anthropic
    except ImportError:
        print("The part of the app that talks to Anthropic is not installed. Quit and "
              "reopen the app while connected to the internet to install it.",
              file=sys.stderr)
        return None
    return anthropic.Anthropic(api_key=api_key)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pull", action="store_true",
                    help="rsync the scraper machine's *.json profile sidecars "
                         "first (over SSH)")
    # See import_scraped_photos.py's --source-dir comment for why this is
    # "-dir" and not merge_scraped_facts.py's bare --source: that flag names a
    # FILE (a pulled crm.db copy), this one a DIRECTORY of *.json sidecars.
    ap.add_argument("--source-dir",
                    help="read *.json profile sidecars from a local directory "
                         "instead of pulling over SSH — e.g. data/li_raw/ when "
                         "the scraper runs on this same Mac")
    ap.add_argument("--dest", default=db.DB_PATH,
                    help="master crm.db to store extras into (default: this Mac's)")
    ap.add_argument("--force", action="store_true",
                    help="re-extract every matched contact, even ones already populated")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be extracted without any LLM call or write")
    # NC-372: one run sends at most this many profiles. The default is the spend
    # cap on the Settings page's AI row, read now, so a saved change applies to
    # the next press without a restart.
    cap = settings.spend_cap()
    ap.add_argument("--limit", type=int, default=cap,
                    help=f"most profiles to send to the model in one run "
                         f"(default {cap}, the spend cap set in Settings)")
    args = ap.parse_args()
    limit = max(1, args.limit)

    if bool(args.source_dir) == args.pull:  # exactly one of the two
        ap.error("pass either --pull or --source-dir /path/to/li_raw")

    # Fail fast on a real run with no usable LLM client — before the network pull.
    client = None
    if not args.dry_run:
        client = _make_client()
        if client is None:
            sys.exit(1)

    tmp_dir = ""
    try:
        if args.pull:
            # The whole pull lives inside the try so a failed rsync (host off,
            # stalled transfer) still cleans up the temp dir of pulled sidecars.
            tmp_dir = tempfile.mkdtemp(prefix="scraper_li_extras_")
            pull_sidecars(tmp_dir)
            pull_dir = tmp_dir
        else:
            pull_dir = args.source_dir
            if not os.path.isdir(pull_dir):
                raise SystemExit(f"--source-dir {pull_dir!r} is not a directory")
        stats = enrich(pull_dir, args.dest, args.force, args.dry_run, client, limit)
    finally:
        # tmp_dir is only set in the --pull branch — a --source-dir path is the
        # caller's own directory (e.g. the live data/li_raw/) and must survive.
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if args.dry_run:
        print(f"[dry-run] would extract extras for {stats['extracted']} of "
              f"{stats['found']} saved profiles. Nothing was sent or saved.")
    else:
        # "Enriched N contacts" is the line importers.RECEIPT_PATTERNS turns into
        # the result's chip on the Data page.
        print(f"Enriched {stats['extracted']} contacts with shared history and mutual "
              f"connections, from {stats['found']} saved LinkedIn profiles.")
    if stats["left"]:
        print(f"  {stats['left']:,} more are waiting. One run sends at most {limit:,}, "
              "the limit set in Settings, so run it again for the next ones.")
    print(f"  Already read before, so skipped: {stats['skipped_existing']}")
    print(f"  Saved under a name nobody here has, so skipped: {stats['id_name_mismatch']}")
    print(f"  Could not be read (no text saved, or a reply that could not be used): "
          f"{stats['errors']}")


if __name__ == "__main__":
    main()
