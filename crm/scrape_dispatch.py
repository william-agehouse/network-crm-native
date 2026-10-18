#!/usr/bin/env python3
"""
Paced dispatcher for the LinkedIn scraper.

A LaunchAgent wakes this at a fixed interval (see deploy/com.networkcrm.liscraper.plist
for the shipped one). Each wake it decides whether to run one
small scraping session *right now*, so that over a day you get a handful of
naturally-spaced sessions, a capped (and day-to-day varied) daily total, timing
that moves around, and an automatic multi-hour cooldown if LinkedIn ever pushes
back. Pacing is the strongest ban protection, so all the knobs live here and are
easy to change (env vars override, for testing):

  SCRAPE_ACTIVE_START / _END   only run between these local hours     (9–21)
  SCRAPE_DAILY_MIN / _MAX       profiles/day, re-picked each day        (40–55)
  SCRAPE_SESSION_MIN / _MAX     profiles per session                    (8–15)
  SCRAPE_GAP_MIN / _MAX         minutes to wait between sessions        (75–210)
  SCRAPE_COOLDOWN_MIN / _MAX    hours to pause everything after a block (24–48)

State (today's target, last session, cooldown) lives in data/scrape_state.json.

Usage (normally invoked by the LaunchAgent):
  python crm/scrape_dispatch.py            # tick: maybe run a session
  python crm/scrape_dispatch.py --status   # print the decision, never scrape
"""

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import db  # noqa: E402


def _envint(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# ── Tunable pacing settings (env overrides win) ───────────────────────────────
ACTIVE_HOUR_START = _envint("SCRAPE_ACTIVE_START", 9)
ACTIVE_HOUR_END = _envint("SCRAPE_ACTIVE_END", 21)
DAILY_TARGET_MIN = _envint("SCRAPE_DAILY_MIN", 40)
DAILY_TARGET_MAX = _envint("SCRAPE_DAILY_MAX", 55)
SESSION_MIN = _envint("SCRAPE_SESSION_MIN", 8)
SESSION_MAX = _envint("SCRAPE_SESSION_MAX", 15)
GAP_MIN = _envint("SCRAPE_GAP_MIN", 75)
GAP_MAX = _envint("SCRAPE_GAP_MAX", 210)
COOLDOWN_HOURS_MIN = _envint("SCRAPE_COOLDOWN_MIN", 24)
COOLDOWN_HOURS_MAX = _envint("SCRAPE_COOLDOWN_MAX", 48)

# Exit code the scraper returns on a genuine LinkedIn checkpoint (vs any other
# non-zero, which is a technical error). Keep in sync with scrape_linkedin_profiles.
EXIT_CHECKPOINT = 3

STATE_PATH = config.data_path("scrape_state.json")
SCRAPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "scrape_linkedin_profiles.py")
VENV_PYTHON = os.path.join(config.ROOT, ".venv", "bin", "python")


# ── Time / state helpers ──────────────────────────────────────────────────────
def now_local() -> datetime:
    return datetime.now().astimezone()


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def scraped_today_count() -> int:
    """How many profiles have been scraped since local midnight (matches the
    facts_updated_at UTC timestamps the scraper writes)."""
    midnight_local = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = _utc_iso(midnight_local)
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE facts_source = 'linkedin_scrape' "
            "AND facts_updated_at >= ?", (cutoff,)).fetchone()
        return (row[0] if row else 0) or 0
    finally:
        conn.close()


# ── Decision ──────────────────────────────────────────────────────────────────
def decide(state: dict):
    """Return (action, reason, ctx). action is 'run' or 'skip'. May update `state`
    (today's target/day) as a side effect — caller persists it."""
    nl = now_local()
    now_iso = _utc_iso(nl)

    cd = state.get("cooldown_until", "")
    if cd and now_iso < cd:
        return "skip", f"cooling down until {cd}", {}

    if not (ACTIVE_HOUR_START <= nl.hour < ACTIVE_HOUR_END):
        return "skip", (f"outside active hours {ACTIVE_HOUR_START}-{ACTIVE_HOUR_END} "
                        f"(local hour {nl.hour})"), {}

    today = nl.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["target"] = random.randint(DAILY_TARGET_MIN, DAILY_TARGET_MAX)
        state["next_ok"] = ""     # first session of the day may go immediately
    target = state.get("target") or random.randint(DAILY_TARGET_MIN, DAILY_TARGET_MAX)

    done = scraped_today_count()
    if done >= target:
        return "skip", f"daily target met ({done}/{target})", {"done": done, "target": target}

    nxt = state.get("next_ok", "")
    if nxt and now_iso < nxt:
        return "skip", (f"spacing sessions until {nxt} ({done}/{target} today)"), \
            {"done": done, "target": target}

    size = min(random.randint(SESSION_MIN, SESSION_MAX), target - done)
    return "run", f"session of {size} ({done}/{target} done today)", \
        {"size": size, "done": done, "target": target}


def run_session(size: int) -> int:
    cmd = [VENV_PYTHON, SCRAPER, "--limit", str(size)]
    print(f"  running: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=config.ROOT).returncode


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true",
                    help="print the current decision and state, but never scrape")
    args = ap.parse_args()

    state = load_state()
    action, reason, ctx = decide(state)
    stamp = now_local().strftime("%Y-%m-%d %H:%M %Z")
    print(f"[{stamp}] {action}: {reason}", flush=True)

    if args.status:
        print(f"  state: {json.dumps(state)}")
        return

    if action != "run":
        save_state(state)         # persist any day/target bookkeeping
        return

    rc = run_session(ctx["size"])
    if rc == EXIT_CHECKPOINT:
        # a REAL LinkedIn block — back off for hours
        hrs = random.randint(COOLDOWN_HOURS_MIN, COOLDOWN_HOURS_MAX)
        until = _utc_iso(now_local() + timedelta(hours=hrs))
        state["cooldown_until"] = until
        print(f"  LinkedIn checkpoint detected → COOLDOWN until {until}",
              file=sys.stderr, flush=True)
    elif rc != 0:
        # technical hiccup (e.g. Firefox hung on launch) — NOT a block. Retry
        # soon; never burn a multi-hour cooldown on a local glitch.
        mins = random.randint(20, 40)
        state["next_ok"] = _utc_iso(now_local() + timedelta(minutes=mins))
        print(f"  session exited {rc} (technical error, not a block) → retry in ~{mins} min",
              file=sys.stderr, flush=True)
    else:
        state["cooldown_until"] = ""
        gap = random.randint(GAP_MIN, GAP_MAX)
        state["next_ok"] = _utc_iso(now_local() + timedelta(minutes=gap))
        print(f"  session ok; next session no earlier than +{gap} min", flush=True)
    state["last_session"] = _utc_iso(now_local())
    save_state(state)


if __name__ == "__main__":
    main()
