"""
Seed crm.db from LinkedIn's standard data export (the portable path — anyone can
use this).

Get the file: LinkedIn → Settings → Data Privacy → "Get a copy of your data" →
pick "Connections" → you'll be emailed a Connections.csv with columns:
First Name, Last Name, URL, Email Address, Company, Position, Connected On.

If you request the FULL archive ("All data") the CSV sometimes also includes a
"Location" or "Geo Location" column — this script reads that too when present.

Usage:
    python seed_from_csv.py Connections.csv
    python seed_from_csv.py ~/Downloads/Connections.csv
"""

import os
import sys
import csv

import cli_guard
import config  # noqa: F401 — loads .env
import db
from enrich_facts import _metro_for_city, _match_role_type

HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))


def _make_reader(path):
    """LinkedIn prepends a few 'Notes:' lines before the real header — skip to it."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("first name")), 0)
    return csv.DictReader(lines[start:])


def _apply_location(conn, contact_id: int, raw_location: str) -> None:
    """Write home_city/home_metro from a LinkedIn location string like 'Stockholm, Sweden'."""
    if not raw_location:
        return
    row = db.get_contact(conn, contact_id)
    if not row or row["facts_source"] in ("manual", "llm"):
        return  # don't overwrite higher-quality facts
    city = raw_location.split(",")[0].strip()
    metro = _metro_for_city(city)
    conn.execute(
        "UPDATE contacts SET home_city=?, home_metro=?, facts_source='linkedin_csv', "
        "facts_updated_at=datetime('now') WHERE id=? AND (home_city='' OR home_city IS NULL)",
        (city, metro, contact_id),
    )


USAGE = """Usage: seed_from_csv.py <Connections.csv>

Adds everyone in LinkedIn's Connections.csv export to the CRM and fills empty
fields for people already in it. It writes on every run; there is no --dry-run."""


def main():
    cli_guard.check(sys.argv[1:], USAGE)
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python seed_from_csv.py <Connections.csv>")
    path = sys.argv[1]
    if not os.path.exists(path):
        raise SystemExit(f"File not found: {path}")

    db.init_db()
    conn = db.get_db()
    reader = _make_reader(path)

    added = backfilled = skipped = located = 0
    for row in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = " ".join(x for x in (r.get("first name", ""), r.get("last name", "")) if x).strip()
        if not name:
            skipped += 1
            continue
        before = conn.execute("SELECT COUNT(*) FROM contacts WHERE name_norm = ?",
                              (db.normalize_name(name),)).fetchone()[0]
        cid = db.upsert_contact_from_linkedin(
            conn, name=name,
            position=r.get("position", ""), company=r.get("company", ""),
            connected_on=r.get("connected on", ""),
            linkedin_url=r.get("url", ""), email=r.get("email address", ""),
        )
        added += (before == 0)
        backfilled += (before > 0)
        # location — LinkedIn "full archive" export sometimes includes this column
        loc = r.get("location", "") or r.get("geo location", "") or r.get("geo-location", "")
        if loc and cid:
            _apply_location(conn, cid, loc)
            located += 1
        # role_type from title if not already set
        if cid:
            contact = db.get_contact(conn, cid)
            if contact and not contact["role_type"]:
                title = " ".join(filter(None, [r.get("position", ""), r.get("company", "")]))
                rt = _match_role_type(title)
                if rt:
                    conn.execute("UPDATE contacts SET role_type=? WHERE id=?", (rt, cid))
    conn.commit()

    excluded = db.apply_exclude_list(conn, db.load_exclude_list(EXCLUDE_FILE))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    conn.close()
    print(f"Seed complete: +{added} new, {backfilled} existing backfilled, {skipped} skipped.")
    if located:
        print(f"Location data found for {located} contacts (LinkedIn 'full archive' export).")
    if excluded:
        print(f"Excluded {excluded} contact(s) from exclude_list.txt.")
    print(f"crm.db now holds {total} contacts.")


if __name__ == "__main__":
    main()
