#!/usr/bin/env python3
"""
Merge LinkedIn-scrape enrichment from the scraper machine back into this Mac's
master crm.db.

The scraper can run on a separate scraper Mac against a COPY of crm.db (so the
automated browsing never touches your main machine). This pulls the facts it produced
— home_city / home_country / home_metro / topics / role_type / blurb — back into
the master here, matching contacts by id+name and NEVER overwriting a 'manual'
edit you made locally.

ONLY NEEDED WHEN THE SCRAPER IS A SEPARATE MACHINE (NC-012). This whole script —
and import_scraped_photos.py, enrich_li_extras.py, pull_and_merge.sh, and the
liscraper LaunchAgent that ties them together — exists to bridge two computers:
the isolated scraper (crm.db copy) and this Mac (the real crm.db). If
scrape_linkedin_profiles.py runs on THIS SAME Mac instead, none of that bridge is
needed: db.get_db() with no path argument opens this Mac's own db.DB_PATH, so the
scraper is already writing facts_source='linkedin_scrape' straight into the exact
database the app reads — the merge already happened, at write time, for free.
Reach for --source/--pull only once the scraper and the app are on different
machines and a copy genuinely has to travel between them.

Usage:
  # one command — snapshot the scraper Mac's db, copy it over ssh, then merge (what
  # the app's "LinkedIn profile facts" card runs. IMAC=user@host is REQUIRED and
  # has no default — see the constant below; KEY=~/.ssh/... if the key is not at
  # the default key path):
  python crm/merge_scraped_facts.py --pull
  python crm/merge_scraped_facts.py --pull --dry-run

  # or in two steps, from an already-pulled copy. Take the snapshot on the scraper Mac
  # first — scp'ing crm.db straight out misses everything still sitting in its
  # -wal sidecar, which is the whole reason remote_snapshot() exists. The commands
  # below are one line each, and quoted so you can paste them as they appear:
  ssh -i "$KEY" -o IdentitiesOnly=yes "$IMAC" 'sqlite3 network-crm/data/crm.db ".backup /tmp/snap.db"'
  scp -i "$KEY" -o IdentitiesOnly=yes "$IMAC":/tmp/snap.db /tmp/scraper_crm.db
  python crm/merge_scraped_facts.py --source /tmp/scraper_crm.db
"""

import argparse
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402,F401 — imported for its side effect: resolves paths / loads .env
import db  # noqa: E402

# The columns the scraper produces (facts_source/facts_updated_at handled separately).
FACTS_COLS = ("home_city", "home_country", "home_metro", "topics", "role_type", "blurb")
SCRAPE_SOURCE = "linkedin_scrape"

# Where the scraper machine lives (see LINKEDIN_SCRAPE_HANDOFF.md §6). A
# name that resolves from any network (a VPN or mesh-network name) is the
# recommended form.
#
# NO DEFAULT, deliberately (NC-006). A default would be a machine name that
# resolves on one person's machines and nowhere else, so on every other install
# the Data page's "LinkedIn profile facts" button would be live and its error
# would name a computer the user had never heard of. With a default, nothing can
# tell "configured" from "never touched", so the row cannot know to show itself
# as not set up.
#
# Both are settings now (settings.MANAGED_KEYS), so changing scraper machines is a
# field rather than an edit to this file.
IMAC = os.environ.get("IMAC", "").strip()
IMAC_KEY = os.environ.get("KEY", "").strip() or os.path.expanduser("~/.ssh/imac_scraper")
IMAC_DB = "network-crm/data/crm.db"


SSH_OPTS = ["-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]

# Where the scraper Mac stages the consistent snapshot we actually copy. Its /tmp: the
# same volume as the db, so if the db fits then so does a copy of it, and macOS
# sweeps /tmp periodically, so a snapshot orphaned by a killed run cleans itself up.
REMOTE_SNAPSHOT = "/tmp/ncrm-pull-snapshot.db"

# Named constants rather than literals, matching import_scraped_photos.PULL_TIMEOUT_S.
# The backup itself is instant on a db this size; the ceiling exists so a host that
# accepts the connection and then wedges fails instead of hanging the import card.
SNAPSHOT_TIMEOUT_S = 120
COPY_TIMEOUT_S = 120
CLEANUP_TIMEOUT_S = 30

SSH_CONNECT_FAILED = 255   # ssh's own exit code for "I never reached the remote"
SOURCE_MISSING = 3         # our own: the scraper Mac has no crm.db to snapshot


def _unreachable(detail: str) -> SystemExit:
    """The one diagnosis for every way the scraper machine can be absent."""
    return SystemExit(
        f"Could not reach the scraper machine ({IMAC}).\n"
        f"{detail}\n"
        "Is the scraper Mac on and reachable over the network?"
    )


def _ssh(remote_cmd: str, timeout: int) -> "subprocess.CompletedProcess | None":
    """Run one shell command on the scraper Mac. None means ssh could not be run at all."""
    try:
        # NC-149. The "--" is load-bearing: without it a host that begins with
        # "-" is read by ssh as an OPTION, and `-oProxyCommand=...` runs through
        # /bin/sh: `ssh -oProxyCommand='touch /tmp/x' true` creates the file;
        # with "--" ssh answers "hostname contains invalid characters" and does
        # nothing. IMAC is an ordinary Settings field, so this path
        # turns "can change Settings" into "can run commands as whoever runs the app".
        return subprocess.run(["ssh", "-i", IMAC_KEY, *SSH_OPTS, "--", IMAC, remote_cmd],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def remote_snapshot() -> str:
    """Have the scraper Mac write a consistent snapshot of crm.db; return its remote path.

    Returns "" to mean "no snapshot — copy the live db and hope", which is what
    this script did before and is still better than not syncing at all.

    WHY A SNAPSHOT. The scraper's db runs in WAL mode, so its recent commits live
    in a crm.db-wal sidecar until SQLite checkpoints them, and we only ever copied
    crm.db. So a plain copy returns the last *checkpointed* state — the newest
    scraped profiles simply aren't in the file we fetch. `.backup` reads through a
    connection, so it sees the WAL and writes one self-contained file.

    WHY NOT JUST CHECKPOINT THE WAL, which is the obvious cheaper fix: it is worse
    than doing nothing. With a reader holding an older snapshot — an active
    scrape, i.e. exactly when you need this — `PRAGMA wal_checkpoint(TRUNCATE)`
    copies the frames it can, leaves the rest in the WAL, and reports busy=1
    through an exit code of *zero*, so it looks like it worked. The half-updated crm.db it leaves behind then copies as
    "database disk image is malformed": a stale pull turned into a corrupt one, with
    no warning. `.backup` under the same contention returns every committed row,
    and reports real failures with exit 1. So the snapshot fixes the staleness and
    removes the tear that merge() already has an error message for.

    umask 077, because this file is a complete copy of the CRM database and must no
    more sit world-readable in the scraper Mac's /tmp than the local copy may linger in
    this Mac's (see main()). sqlite3 would otherwise create it 0644.

    -readonly and the `test -s` on the SOURCE are both there for one trap:
    the sqlite3 CLI opens its database argument with O_CREAT. So if the scraper
    Mac's crm.db were missing — moved repo, dangling data/ symlink, a freshly set-up
    scraper, a typo in IMAC_DB — a plain `.backup` CREATES a 0-byte db at that path
    and cheerfully backs it up to a structurally valid, 4096-byte, EMPTY snapshot.
    Exit 0, and `test -s` on the snapshot passes because 4096 > 0, so the pull would
    report success, copy an empty database, and merge() would blame a torn snapshot
    and tell you to try again in a minute — advice that can never work, for a cause
    that isn't the real one. Worse, it makes the pull a WRITER on the scraper
    machine, which a plain scp never was. -readonly exits 1 and creates nothing;
    the source check names the actual problem. -readonly still returns every
    uncheckpointed WAL row when no -shm exists (the ordinary case: the scraper has
    finished and exited)."""
    snap = shlex.quote(REMOTE_SNAPSHOT)
    src = shlex.quote(IMAC_DB)
    # `test -s` on the snapshot goes last: it proves a file arrived, independently
    # of how a given sqlite3 build reports errors. It cannot prove CONTENT, which
    # is what the source check above it is for.
    remote_cmd = (
        f"test -s {src} || {{ echo '{IMAC_DB} is missing or empty' >&2; "
        f"exit {SOURCE_MISSING}; }}; "
        f"rm -f {snap} && "
        f"(umask 077 && sqlite3 -readonly {src} \".backup '{REMOTE_SNAPSHOT}'\") && "
        f"test -s {snap}"
    )
    r = _ssh(remote_cmd, SNAPSHOT_TIMEOUT_S)
    if r is None:
        # Not fatal by itself: scp is the step that has to work, and it will give
        # the real diagnosis a moment from now.
        print("  (could not ask the scraper Mac for a snapshot — copying the db live, "
              "so the newest scrapes may lag)")
        return ""
    if r.returncode == 0:
        return REMOTE_SNAPSHOT
    if r.returncode == SSH_CONNECT_FAILED:
        # Host down, key rejected, host key changed. scp is about to fail the same
        # way, so stop now rather than make the user wait out a second connection
        # timeout for the same news.
        raise _unreachable((r.stderr or "").strip() or "ssh could not connect.")
    if r.returncode == SOURCE_MISSING:
        # Fatal, not degraded: falling back would scp a path we just proved isn't
        # there. And NOT _unreachable() — the scraper Mac answered perfectly well.
        raise SystemExit(
            f"The scraper Mac has no database to pull: {IMAC_DB} is missing or empty "
            f"on {IMAC}.\n"
            "Has the scraper been set up and given its copy of crm.db yet? "
            "(See LINKEDIN_SCRAPE_HANDOFF.md §1.)"
        )
    detail = (r.stderr or r.stdout or "").strip() or "no detail given"
    print(f"  (the scraper Mac could not make a snapshot — {detail}; copying the db live, "
          "so the newest scrapes may lag)")
    return ""


def _discard_remote_snapshot() -> None:
    """Best effort, and deliberately silent: a full copy of the CRM database must
    not be left on the scraper Mac, but the pull has already reported success or failure
    and a cleanup complaint stacked on top would only bury it."""
    _ssh(f"rm -f {shlex.quote(REMOTE_SNAPSHOT)}", CLEANUP_TIMEOUT_S)


def pull(dest: str) -> None:
    """Copy the scraper Mac's crm.db to `dest`, via a snapshot taken there.

    BatchMode makes an unreachable host fail in seconds with a real error instead
    of hanging on a password prompt. See remote_snapshot for why we never copy the
    live file when we can avoid it."""
    print(f"Pulling crm.db from {IMAC} …")
    remote_path = remote_snapshot()
    staged = bool(remote_path)
    # "--" for the same reason as in _ssh: the source is one f-string starting
    # with IMAC, so a host beginning with "-" makes the whole token an option.
    cmd = ["scp", "-i", IMAC_KEY, *SSH_OPTS, "-q", "--",
           f"{IMAC}:{remote_path or IMAC_DB}", dest]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=COPY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # ConnectTimeout only covers connection setup — a transfer that stalls
        # mid-copy (Wi-Fi drop, host sleeping) lands here instead.
        raise _unreachable(f"The transfer stalled and timed out after {COPY_TIMEOUT_S}s.")
    except OSError as exc:  # scp binary missing/unrunnable — never a traceback
        raise _unreachable(f"Could not run scp: {exc}")
    finally:
        # In the finally, not after the returncode check: a failed transfer leaves
        # the snapshot behind just as surely as a successful one.
        if staged:
            _discard_remote_snapshot()
    if r.returncode != 0:
        raise _unreachable((r.stderr or r.stdout or "").strip())


def _empty(value: str) -> bool:
    """'' and the JSON-empty-list topics default both count as no data."""
    return not value or value.strip() in ("", "[]")


def merge(source_path: str, dest_path: str, dry_run: bool) -> dict:
    """Copy scraped facts from source→dest. Returns a stats dict. Matches on
    id AND name (the scraper Mac's db is a snapshot copy, so ids line up); skips any dest
    contact that carries a 'manual' facts_source so hand edits are never lost.

    Column-by-column, never wholesale: the scraper's LLM often extracts only
    SOME fields for a profile (city but no blurb, topics but no role), and the
    master row may already hold values from other passes (offline / message_scan
    / llm). A blank snapshot column must never erase a filled master column —
    each column only ever changes when the snapshot actually has data for it."""
    src = sqlite3.connect(source_path)
    src.row_factory = sqlite3.Row
    db.init_db(dest_path)                 # make sure the facts columns exist
    dst = db.get_db(dest_path)

    try:
        rows = src.execute(
            "SELECT * FROM contacts WHERE facts_source = ?", (SCRAPE_SOURCE,)
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        src.close()
        dst.close()
        raise SystemExit(
            f"The pulled snapshot isn't readable ({exc}).\n"
            "If the pull warned that it had to copy the db live, that copy raced an "
            "active scrape session — try again in a minute. Otherwise the file we "
            "fetched is not the scraper's crm.db, and retrying will not change that."
        )

    stats = {"scraped_in_source": len(rows), "merged": 0,
             "skipped_manual": 0, "id_name_mismatch": 0, "unchanged": 0}

    for r in rows:
        d = dst.execute(
            f"SELECT id, name, facts_source, manual_fields, {', '.join(FACTS_COLS)} "
            "FROM contacts WHERE id = ?", (r["id"],)
        ).fetchone()
        if not d or (d["name"] or "").strip() != (r["name"] or "").strip():
            stats["id_name_mismatch"] += 1
            continue
        # Per-field manual provenance: the scrape refreshes every field
        # EXCEPT the ones the user hand-edited. A legacy row-wide 'manual'
        # facts_source expands to all fact fields, so those rows stay fully
        # protected exactly as before.
        d_manual = db.manual_field_set(d)
        # By default a hand-edited field always wins over the scrape. Set
        # SCRAPE_OVERRIDES_MANUAL=1 (Settings) to let a newer scraped value win.
        if os.environ.get("SCRAPE_OVERRIDES_MANUAL", "0").strip() == "1":
            d_manual = set()
        if d_manual.issuperset(FACTS_COLS):
            stats["skipped_manual"] += 1
            continue

        changes = {c: r[c] for c in FACTS_COLS
                   if c not in d_manual and not _empty(r[c]) and (r[c] or "") != (d[c] or "")}
        if not changes:
            stats["unchanged"] += 1
            continue
        if dry_run:
            stats["merged"] += 1
            continue
        set_clause = ", ".join(f"{c} = ?" for c in changes)
        dst.execute(
            f"UPDATE contacts SET {set_clause}, facts_source = ?, facts_updated_at = ?, "
            f"updated_at = datetime('now') WHERE id = ?",
            (*changes.values(), SCRAPE_SOURCE, r["facts_updated_at"], r["id"]),
        )
        stats["merged"] += 1

    if not dry_run:
        dst.commit()
    src.close()
    dst.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source",
                    help="path to an already-pulled copy of the scraper Mac's crm.db")
    ap.add_argument("--pull", action="store_true",
                    help="pull the scraper Mac's crm.db over ssh, then merge it")
    ap.add_argument("--dest", default=db.DB_PATH,
                    help="master crm.db to merge into (default: this Mac's)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()

    if bool(args.source) == args.pull:  # exactly one of the two
        ap.error("pass either --pull or --source /path/to/scraper_crm.db")

    # --source is a file you already have, so it needs no scraper machine at all;
    # only --pull reaches over the network. Checking here rather than at import
    # keeps the merge half usable on any Mac (NC-006).
    if args.pull and not IMAC:
        raise SystemExit(
            "No scraper machine is set, so there is nothing to pull from.\n"
            "\n"
            "Set The other Mac, as user@name, in Settings to the machine running the LinkedIn scraper, "
            "as user@host — a name that resolves from any network works best, for example "
            "someone@scraper-mac. If its SSH key is not at ~/.ssh/imac_scraper, set "
            "KEY to the key's path as well.\n"
            "\n"
            "Already have a copy of that machine's crm.db? Skip the pull:\n"
            "  python merge_scraped_facts.py --source /path/to/their_crm.db")

    tmp_path = ""
    source = args.source
    try:
        # The whole pull lives inside the try: a failed scp (host off, stalled
        # transfer) must still clean up — this tempfile is a full copy of the
        # CRM database and must never linger in the shared temp dir.
        if args.pull:
            fd, tmp_path = tempfile.mkstemp(prefix="scraper_crm_", suffix=".db")
            os.close(fd)
            pull(tmp_path)
            source = tmp_path

        if not os.path.exists(source) or os.path.getsize(source) == 0:
            print(f"source db not found or empty: {source}", file=sys.stderr)
            sys.exit(1)
        if os.path.abspath(source) == os.path.abspath(args.dest):
            print("source and dest are the same file — nothing to do.", file=sys.stderr)
            sys.exit(1)

        stats = merge(source, args.dest, args.dry_run)
    finally:
        if tmp_path:
            # opening the snapshot spawns -wal/-shm sidecars — sweep those too
            for p in (tmp_path, tmp_path + "-wal", tmp_path + "-shm"):
                try:
                    os.remove(p)
                except OSError:
                    pass

    tag = "[dry-run] would merge" if args.dry_run else "Merged"
    print(f"{tag} {stats['merged']} of {stats['scraped_in_source']} scraped contacts.")
    print(f"  already up to date: {stats['unchanged']}")
    print(f"  skipped (manual edit kept): {stats['skipped_manual']}")
    print(f"  id/name mismatch (not on this master): {stats['id_name_mismatch']}")
    if not args.dry_run and stats["merged"]:
        print("Profile facts (city, topics, role, blurb) are live — "
              "check any scraped contact's card.")


if __name__ == "__main__":
    main()
