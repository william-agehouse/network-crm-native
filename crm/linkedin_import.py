"""
Import your LinkedIn connections and message history via the Member Snapshot API.

Requires LINKEDIN_TOKEN in .env with r_dma_portability_self_serve scope.
To regenerate: linkedin.com/developers/tools/oauth/token-generator → pick the app
with Member Data Portability → tick r_dma_portability_self_serve → Request token.

Usage:
    python linkedin_import.py                   # fetch connections + messages
    python linkedin_import.py --connections     # connections only
    python linkedin_import.py --messages        # messages only
    python linkedin_import.py --dry-run         # preview, no writes
"""

import hashlib
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

import cli_guard
import config  # noqa: F401 — loads .env
import archive as arc
import db
import engagement  # NC-107: the one writer of msg_count

HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))

SNAPSHOT_URL = "https://api.linkedin.com/rest/memberSnapshotData"
API_VERSION = "202312"


# ── API paging ────────────────────────────────────────────────────────────────

def _fetch_all(token: str, domain: str) -> List[dict]:
    """Fetch every row for a snapshot domain, following paging links."""
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": API_VERSION,
    }
    params: Optional[dict] = {"q": "criteria", "domain": domain}
    url: Optional[str] = SNAPSHOT_URL
    rows: List[dict] = []

    while url:
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code == 401:
            raise SystemExit(
                "401 Invalid or expired token — generate a fresh one at "
                "linkedin.com/developers/tools/oauth/token-generator and paste it into\n"
                "the LinkedIn panel on the Data page."
            )
        if r.status_code == 403:
            raise SystemExit(
                f"403 Forbidden — token lacks access to {domain}. "
                "Ensure r_dma_portability_self_serve scope is ticked."
            )
        if r.status_code == 404:
            break  # no more pages
        r.raise_for_status()
        data = r.json()
        batch = []
        for el in data.get("elements", []):
            batch.extend(el.get("snapshotData", []))
        if not batch:
            break  # empty page = done
        rows.extend(batch)
        next_href = next(
            (l["href"] for l in data.get("paging", {}).get("links", [])
             if l.get("rel") == "next"),
            None,
        )
        if next_href:
            url = ("https://api.linkedin.com" + next_href
                   if next_href.startswith("/") else next_href)
            params = None
        else:
            url = None

    return rows


# ── CONNECTIONS → crm.db ──────────────────────────────────────────────────────

def _parse_connected_on(s: str) -> str:
    """'01 Mar 2020' → '2020-03-01'. Returns '' on failure."""
    for fmt in ("%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass
    return s.strip()


def apply_connections(rows: List[dict], *, dry_run: bool) -> Tuple[dict, Dict[str, int]]:
    """Upsert connection rows into crm.db. Returns (stats, url_to_cid)."""
    conn = db.get_db()
    db.ensure_columns(conn)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)

    stats = {"added": 0, "backfilled": 0, "skipped_exclude": 0, "skipped_empty": 0}
    url_to_cid: Dict[str, int] = {}

    for row in rows:
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        name = f"{first} {last}".strip()
        if not name:
            stats["skipped_empty"] += 1
            continue
        if db.normalize_name(name) in exclude_norms:
            stats["skipped_exclude"] += 1
            continue

        url = (row.get("URL") or "").strip()
        company = (row.get("Company") or "").strip()
        position = (row.get("Position") or "").strip()
        connected_on = _parse_connected_on(row.get("Connected On") or "")
        email = (row.get("Email Address") or "").strip()

        before = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE name_norm = ?",
            (db.normalize_name(name),),
        ).fetchone()[0]

        if not dry_run:
            cid = db.upsert_contact_from_linkedin(
                conn,
                name=name,
                position=position,
                company=company,
                connected_on=connected_on,
                linkedin_url=url,
                email=email,
            )
            if url:
                url_to_cid[url] = cid
        else:
            cid = None

        if before == 0:
            stats["added"] += 1
        else:
            stats["backfilled"] += 1

    if not dry_run:
        excluded = db.apply_exclude_list(conn, exclude_norms)
        conn.commit()
        if excluded:
            stats["excluded_applied"] = excluded
        # also build url_to_cid for contacts already in db but not in this export
        for row_db in conn.execute(
            "SELECT id, linkedin_url FROM contacts WHERE linkedin_url != ''"
        ).fetchall():
            url_to_cid.setdefault(row_db["linkedin_url"], row_db["id"])

    conn.close()
    return stats, url_to_cid


# ── INBOX → archive.db ────────────────────────────────────────────────────────

def _msg_source_key(row: dict) -> str:
    convo = row.get("CONVERSATION ID", "")
    date = row.get("DATE", "")
    sender = row.get("FROM", "")
    body = row.get("CONTENT", "")
    return hashlib.sha1(f"{convo}|{date}|{sender}|{body}".encode()).hexdigest()


def _parse_ts(s: str) -> str:
    """'2020-01-01 12:00:00 UTC' → '2020-01-01 12:00:00'"""
    return (s or "").replace(" UTC", "").strip()


def archive_messages(
    rows: List[dict],
    url_to_cid: Dict[str, int],
    archive_path: str = arc.ARCHIVE_DB_PATH,
) -> dict:
    """Write LinkedIn message rows to archive.db.

    Returns {"new", "skipped", "unmatched", "cids": the people given a new message}.
    """
    if not url_to_cid or not rows:
        return {"new": 0, "skipped": 0, "cids": set()}

    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)

    new = skipped = unmatched = 0
    touched: set = set()
    for row in rows:
        if (row.get("IS MESSAGE DRAFT") or "").strip().lower() == "yes":
            continue
        body = (row.get("CONTENT") or "").strip()
        if not body:
            continue

        sender_url = (row.get("SENDER PROFILE URL") or "").strip()
        recip_urls = [u.strip() for u in (row.get("RECIPIENT PROFILE URLS") or "").split(",") if u.strip()]

        # Determine direction and contact
        if sender_url in url_to_cid:
            # inbound — sender is a known contact
            direction = 0
            cid = url_to_cid[sender_url]
        else:
            # outbound — I am the sender; match recipient
            direction = 1
            cid = next((url_to_cid[u] for u in recip_urls if u in url_to_cid), None)

        if cid is None:
            unmatched += 1
            continue

        source_key = _msg_source_key(row)
        ts = _parse_ts(row.get("DATE", ""))
        inserted = arc.insert_message(
            arc_conn,
            contact_id=cid,
            channel="linkedin",
            direction=direction,
            ts=ts,
            body=body,
            source_key=source_key,
        )
        if inserted:
            new += 1
            touched.add(cid)
        else:
            skipped += 1

    arc_conn.commit()
    arc_conn.close()

    # NC-107. No message count is written here any more. `msg_count` and
    # `last_contact` are the messages the app can show, counted from archive.db
    # by engagement.recount_channels for everyone. This used to recount the
    # people matched on this run and then refuse to go LOWER than the stored
    # number, so that LinkedIn's short download window could not shrink history
    # an old scraper had counted. That rule kept placeholders the scraper counted
    # and never saved: some cards claimed messages their timelines did not hold.
    # The history it protected is safe without it,
    # because an import only adds saved messages. The one thing that removes
    # them is permanently deleting the person, and their card goes with them.

    return {"new": new, "skipped": skipped, "unmatched": unmatched, "cids": touched}


def archive_and_chunk(rows: List[dict], url_to_cid: Dict[str, int],
                      archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Archive the rows, then rebuild the search pieces of the people given a
    new message, in one commit. NC-266: this used to rebuild everyone matched
    from the connections list whenever any message was new, as the iMessage and
    WhatsApp imports did before NC-222. Returns archive_messages' stats plus
    {"chunks"}."""
    out = archive_messages(rows, url_to_cid, archive_path=archive_path)
    out["chunks"] = 0
    if out["new"]:
        arc_conn = arc.get_archive_db(archive_path)
        try:
            out["chunks"] = arc.build_chunks(arc_conn, contact_ids=out["cids"])
        finally:
            arc_conn.close()
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

USAGE = """Usage: linkedin_import.py [--connections] [--messages] [--dry-run]

Imports your LinkedIn connections and recent messages through LinkedIn's API,
using the token saved on the Data page. With neither option it does both.
  --connections  only the connections
  --messages     only the messages
  --dry-run      count what would change and write nothing"""


def main() -> None:
    cli_guard.check(sys.argv[1:], USAGE, flags=("--connections", "--messages", "--dry-run"))
    args = set(sys.argv[1:])
    do_connections = "--connections" in args or "--messages" not in args
    do_messages = "--messages" in args or "--connections" not in args
    dry_run = "--dry-run" in args

    token = os.environ.get("LINKEDIN_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "No LinkedIn token yet — add one under Settings first "
            "(the LinkedIn card explains how to generate it)."
        )

    db.init_db()
    url_to_cid: Dict[str, int] = {}

    if do_connections:
        print("Fetching LinkedIn connections…")
        conn_rows = _fetch_all(token, "CONNECTIONS")
        print(f"  {len(conn_rows)} connection records from API")
        stats, url_to_cid = apply_connections(conn_rows, dry_run=dry_run)
        tag = "[dry-run] " if dry_run else ""
        print(
            f"{tag}New contacts added:    {stats['added']}\n"
            f"{tag}Existing backfilled:   {stats['backfilled']}\n"
            f"{tag}Skipped (exclude):     {stats['skipped_exclude']}"
        )

    if do_messages:
        if not do_connections:
            # build url_to_cid from existing crm.db
            conn = db.get_db()
            for row in conn.execute(
                "SELECT id, linkedin_url FROM contacts WHERE linkedin_url != ''"
            ).fetchall():
                url_to_cid[row["linkedin_url"]] = row["id"]
            conn.close()

        print("\nFetching LinkedIn messages…")
        msg_rows = _fetch_all(token, "INBOX")
        print(f"  {len(msg_rows)} message records from API")

        if not dry_run:
            arc_stats = archive_and_chunk(msg_rows, url_to_cid)
            print(
                f"  {arc_stats['new']:,} new messages archived  "
                f"({arc_stats['skipped']:,} already stored, "
                f"{arc_stats.get('unmatched', 0):,} unmatched contacts)"
            )
            if arc_stats["new"]:
                print(f"  {arc_stats['chunks']:,} conversation chunks ready for search")
            # NC-107. The app recounts after every import it runs; this is for an
            # import run by hand, so its counts are right before the app starts.
            conn = db.get_db()
            try:
                engagement.refresh(conn)
            finally:
                conn.close()
        else:
            print("[dry-run] Skipping archive write.")


if __name__ == "__main__":
    main()
