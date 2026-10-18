#!/usr/bin/env python3
"""
Import scraped LinkedIn profile photos from the scraper machine into this Mac's
master crm.db photo store.

Companion to merge_scraped_facts.py: that script pulls the *facts* the scraper
extracted (city / topics / role / blurb); this one pulls the *profile photos* it
archived alongside them. The scraper writes one <contact_id>.jpg per profile into
its data/li_raw/ folder (next to the <id>.json / <id>.html.gz raw archive). Those
image files never come back with the facts merge (that only syncs the DB), so this
importer rsyncs them over SSH and registers each one through photos.save_photo.

Safety mirrors the facts merge:
  • matched by id AND name — the sidecar li_raw/<id>.json "name" (when present) must
    equal the master contact's name, so a stale/renamed id can't attach a wrong face;
  • never-clobber — photos.save_photo enforces source rank
    (manual > linkedin > contacts > whatsapp/google), so a photo you set by hand or
    curated from your address book is never overwritten by a scraped avatar.

Nothing is fetched from the network here beyond the rsync: photos.save_photo only
ever reads the bytes we hand it (pulled straight from the scraper machine's files).

ONE MACHINE, NO SSH (NC-012): if the scraper runs on this same Mac — or you
already have its data/li_raw/ folder some other way (a copied drive, a synced
folder) — --source-dir skips the network entirely and reads the photos straight
from disk. --pull exists for the two-machine setup (see
LINKEDIN_SCRAPE_HANDOFF.md §6); it is no longer the only path, just the one for
when the scraper and the app are different computers.

Usage:
  # two machines: pull the scraper machine's photos over SSH and register them
  # (IMAC=user@host KEY=~/.ssh/... to override, exactly like the facts merge):
  python crm/import_scraped_photos.py --pull
  python crm/import_scraped_photos.py --pull --dry-run

  # one machine: read straight from the scraper's own output directory, no SSH
  # configured anywhere:
  python crm/import_scraped_photos.py --source-dir data/li_raw/
  python crm/import_scraped_photos.py --source-dir data/li_raw/ --dry-run
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402,F401 — imported for its side effect: resolves paths / loads .env
import db  # noqa: E402
import photos  # noqa: E402

# Where the scraper machine lives (see LINKEDIN_SCRAPE_HANDOFF.md §6). Same
# host + key conventions as merge_scraped_facts.py; override via env.
# NO DEFAULT, deliberately (NC-006): a default is what stops anything telling
# "configured" from "never touched".
# Both are MANAGED_KEYS settings — see merge_scraped_facts.py for the full note.
IMAC = os.environ.get("IMAC", "").strip()
IMAC_KEY = os.environ.get("KEY", "").strip() or os.path.expanduser("~/.ssh/imac_scraper")
# The scraper's raw per-profile archive (holds <id>.jpg + <id>.json + <id>.html.gz).
IMAC_RAW = "network-crm/data/li_raw/"

# A first full backfill can be a few thousand small jpgs over SSH; give the
# transfer room but still bound it so an asleep/dropped host fails instead of hanging.
PULL_TIMEOUT_S = 600

PHOTO_SOURCE = "linkedin"


def pull_photos(dest_dir: str) -> None:
    """rsync the scraper machine's profile photos into `dest_dir`.

    Pulls the <id>.jpg images and their <id>.json sidecars (the json is used only
    for the id+name safety check below — same guard the facts merge applies). Like
    merge_scraped_facts.pull, BatchMode + a short ConnectTimeout make an unreachable
    host fail in seconds with a real error instead of hanging on a password prompt."""
    # No host, nothing to reach (NC-006). Raised here rather than at import so
    # every offline half of this module stays usable on any Mac.
    if not IMAC:
        raise SystemExit(
            "No scraper machine is set, so there is nothing to pull from.\n"
            "Set IMAC under Settings to the machine running the LinkedIn scraper, "
            "as user@host — a name that resolves from any network works. Set KEY too "
            "if its SSH key is not at ~/.ssh/imac_scraper.")
    # NC-149, two bugs in one f-string. rsync word-splits its -e argument, so an
    # unquoted key path containing a space became extra ssh options and the pull
    # failed blaming an unreachable host — the boring half, and the one that
    # happens first. shlex.quote fixes that; macOS's rsync hands the quoted path
    # to ssh as a single argument.
    #
    # The trailing "--" is the other half. rsync appends the host to this
    # command, so "--" lands immediately before it and ssh stops reading options
    # there — otherwise a host beginning with "-" is an ssh option, and
    # -oProxyCommand runs a shell. The second "--" below is rsync's own: the
    # source is one token starting with IMAC.
    ssh = (f"ssh -i {shlex.quote(IMAC_KEY)} -o IdentitiesOnly=yes "
           "-o BatchMode=yes -o ConnectTimeout=15 --")
    cmd = ["rsync", "-az", "-e", ssh,
           "--include=*.jpg", "--include=*.json", "--exclude=*", "--",
           f"{IMAC}:{IMAC_RAW}", dest_dir + os.sep]
    print(f"Pulling scraped photos from {IMAC} …")
    unreachable = (
        f"Could not reach the scraper machine ({IMAC}).\n"
        "%s\n"
        "Is that machine on and reachable from this one?"
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


def _scraped_name(pull_dir: str, contact_id: int) -> "str | None":
    """The scraped contact 'name' from li_raw/<id>.json, or None when the sidecar
    is absent/unreadable. None means the id+name guard can't run for this photo, so
    it proceeds on id alone (best-effort — 'if that json is available')."""
    try:
        with open(os.path.join(pull_dir, f"{contact_id}.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    name = (data.get("name") or "").strip()
    return name or None


def _decide(conn, contact_id: int, data: bytes, scraped_name: "str | None") -> str:
    """Classify one photo without side effects. Returns one of:
      'save'     — a storable image for a matching contact with no better photo
      'better'   — a higher-ranked photo is already set (leave it alone)
      'mismatch' — no such contact id, or the id+name guard failed
      'fail'     — the bytes aren't a storable image (not JPEG/PNG/HEIC, too small/large)
    Mirrors the checks photos.save_photo makes, so --dry-run predicts the real run."""
    row = conn.execute(
        "SELECT id, name, photo, photo_source FROM contacts WHERE id = ?", (contact_id,)
    ).fetchone()
    if row is None:
        return "mismatch"  # id not on this master (snapshot drift / stale file)
    if scraped_name is not None and (row["name"] or "").strip() != scraped_name:
        return "mismatch"
    if photos.sniff_content_type(data) is None or not (photos.MIN_BYTES <= len(data) <= photos.MAX_BYTES):
        return "fail"
    if row["photo"] and photos.source_rank(PHOTO_SOURCE) < photos.source_rank(row["photo_source"]):
        return "better"
    return "save"


def import_photos(pull_dir: str, dest_path: str, dry_run: bool) -> dict:
    """Register every <id>.jpg in `pull_dir` into the master crm.db photo store.

    Matches each file's id to a contact, confirms the sidecar json name (if any),
    and hands the bytes to photos.save_photo(..., 'linkedin'), which normalizes the
    image and refuses to clobber a higher-ranked photo. Returns a stats dict."""
    db.init_db(dest_path)                 # make sure the photo/photo_source columns exist
    conn = db.get_db(dest_path)
    stats = {"found": 0, "saved": 0, "skipped_better": 0,
             "skipped_mismatch": 0, "failed": 0}
    try:
        for fname in sorted(os.listdir(pull_dir)):
            if not fname.endswith(".jpg"):
                continue
            stem = fname[:-4]
            if not stem.isdigit():        # photos are keyed by contact id
                continue
            contact_id = int(stem)
            stats["found"] += 1
            try:
                with open(os.path.join(pull_dir, fname), "rb") as fh:
                    data = fh.read()
            except OSError:
                stats["failed"] += 1
                continue

            decision = _decide(conn, contact_id, data, _scraped_name(pull_dir, contact_id))
            if decision == "mismatch":
                stats["skipped_mismatch"] += 1
            elif decision == "better":
                stats["skipped_better"] += 1
            elif decision == "fail":
                stats["failed"] += 1
            elif dry_run:
                stats["saved"] += 1
            elif photos.save_photo(conn, contact_id, data, PHOTO_SOURCE):
                stats["saved"] += 1
            else:
                # decision said 'save' but the write refused — treat as failed so
                # the tally never silently loses a photo.
                stats["failed"] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pull", action="store_true",
                    help="rsync the scraper machine's photos yourself first (over SSH)")
    # Named --source-dir, not --source (merge_scraped_facts.py's flag), on
    # purpose: --source there names a FILE (a pulled copy of crm.db); this
    # script's local input is a DIRECTORY of files (<id>.jpg + <id>.json
    # sidecars), which is a different enough shape that reusing the same name
    # would silently invite `--source data/li_raw/` to work when it can't. Same
    # underlying idea — skip the network pull, read what's already local — so
    # "source" stays the shared root; "-dir" is the honest, minimal difference.
    ap.add_argument("--source-dir",
                    help="read scraped photos from a local directory instead of "
                         "pulling over SSH — e.g. data/li_raw/ when the scraper "
                         "runs on this same Mac")
    ap.add_argument("--dest", default=db.DB_PATH,
                    help="master crm.db to register photos into (default: this Mac's)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would import without writing any photo")
    args = ap.parse_args()

    if bool(args.source_dir) == args.pull:  # exactly one of the two
        ap.error("pass either --pull or --source-dir /path/to/li_raw")

    tmp_dir = ""
    try:
        if args.pull:
            # The whole pull lives inside the try so a failed rsync (host off,
            # stalled transfer) still cleans up the temp dir of pulled images.
            tmp_dir = tempfile.mkdtemp(prefix="scraper_photos_")
            pull_photos(tmp_dir)
            pull_dir = tmp_dir
        else:
            pull_dir = args.source_dir
            if not os.path.isdir(pull_dir):
                raise SystemExit(f"--source-dir {pull_dir!r} is not a directory")
        stats = import_photos(pull_dir, args.dest, args.dry_run)
    finally:
        # tmp_dir is only ever set in the --pull branch, above — a --source-dir
        # path is the caller's own directory (e.g. the live data/li_raw/) and
        # must never be deleted out from under them.
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    tag = "[dry-run] would import" if args.dry_run else "Imported"
    print(f"{tag} {stats['saved']} of {stats['found']} scraped photos.")
    print(f"  skipped (better photo already set): {stats['skipped_better']}")
    print(f"  skipped (id/name mismatch, not on this master): {stats['skipped_mismatch']}")
    print(f"  failed (unreadable / not an image): {stats['failed']}")
    if not args.dry_run and stats["saved"]:
        print("Profile photos are live — check any scraped contact's card.")


if __name__ == "__main__":
    main()
