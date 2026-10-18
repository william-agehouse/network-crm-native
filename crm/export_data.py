#!/usr/bin/env python3
"""
Export / backup the whole Network CRM to a portable, human-readable file.

Three formats, all built from the same local SQLite database (crm.db):

  * Markdown — a clean personal record you can read in any editor.
  * CSV      — one row per contact, opens straight in a spreadsheet.
  * JSON     — full fidelity: every field, plus nested notes and identifiers.

The point of this file is that it is the whole relationship memory in one
place: copy it to a USB stick, a cloud drive, or a new machine and you have not
lost anything. Standard-library only — no installs.

CLI:
    python export_data.py [--format md|csv|json] [--out PATH] [--include-excluded]

By default it skips contacts you have marked excluded (people you chose never
to index) and writes  network-crm-export-YYYY-MM-DD.<ext>  in the current
directory.
"""

import argparse
import csv
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import config  # noqa: F401 — loads .env (e.g. CRM_DB_PATH) before db reads env
import db
import figures  # NC-087: the one spelling of a contact-scope filter

logger = logging.getLogger(__name__)

# Column order for the CSV export — one row per contact, spreadsheet-friendly.
CSV_COLUMNS = [
    "name",
    "company",
    "position",
    "category",
    "status",
    "excluded",
    "linkedin_msgs",
    "imessages",
    "whatsapp",
    "email",
    "telegram",
    "messenger",
    "instagram",
    "last_contact",
    "notes_count",
    "how_we_met",
]


# ── shared data assembly ──────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _last_seen(row: Any) -> str:
    """Most recent touchpoint across channels (LinkedIn / iMessage / WhatsApp /
    Email / Telegram / Messenger / Instagram)."""
    candidates = [
        (row["last_contact"] or ""),
        (row["imsg_last"] or ""),
        (row["wa_last"] or ""),
        (row["em_last"] or ""),
        (row["tg_last"] or ""),
        (row["msgr_last"] or ""),
        (row["ig_last"] or ""),
    ]
    nonempty = [c for c in candidates if c]
    return max(nonempty) if nonempty else ""


def _fetch_contacts(conn: sqlite3.Connection, include_excluded: bool) -> List[sqlite3.Row]:
    """All contacts ordered by name; excluded ones dropped unless asked for."""
    sql = "SELECT * FROM contacts"
    if not include_excluded:
        sql += f" WHERE {figures.population_where('visible')}"
    sql += " ORDER BY name COLLATE NOCASE ASC, id ASC"
    return conn.execute(sql).fetchall()


def _notes_oldest_first(conn: sqlite3.Connection, contact_id: int) -> List[sqlite3.Row]:
    """db.get_notes returns newest-first; reverse for a readable timeline."""
    return list(reversed(db.get_notes(conn, contact_id)))


# ── Markdown ──────────────────────────────────────────────────────────────────
def _markdown_contact(conn: sqlite3.Connection, row: sqlite3.Row) -> List[str]:
    """Render one contact as a Markdown section (heading + facts + timeline)."""
    lines: List[str] = [f"## {row['name']}"]

    role = " · ".join(x for x in (row["position"] or "", row["company"] or "") if x)
    facts = []
    if role:
        facts.append(role)
    if row["category"]:
        facts.append(row["category"])
    if facts:
        lines.append("_" + " — ".join(facts) + "_")

    channels = []
    if row["msg_count"]:
        channels.append(f"{row['msg_count']} LinkedIn")
    if row["imsg_count"]:
        channels.append(f"{row['imsg_count']} iMessages")
    if row["wa_count"]:
        channels.append(f"{row['wa_count']} WhatsApp")
    if row["em_count"]:
        channels.append(f"{row['em_count']} Email")
    if row["tg_count"]:
        channels.append(f"{row['tg_count']} Telegram")
    if row["msgr_count"]:
        channels.append(f"{row['msgr_count']} Messenger")
    if row["ig_count"]:
        channels.append(f"{row['ig_count']} Instagram")
    last = _last_seen(row)
    stat_bits = []
    if channels:
        stat_bits.append(" · ".join(channels))
    if last:
        stat_bits.append(f"last contact {last}")
    if stat_bits:
        lines.append("")
        lines.append("- " + "  |  ".join(stat_bits))

    if row["how_we_met"]:
        lines.append("")
        lines.append(f"**How we met:** {row['how_we_met']}")

    notes = _notes_oldest_first(conn, row["id"])
    if notes:
        lines.append("")
        lines.append("**Notes**")
        for n in notes:
            when = (n["created_at"] or "")[:10]
            lines.append(f"- _{when}_ — {n['body']}")

    lines.append("")
    return lines


def export_markdown(conn: sqlite3.Connection, include_excluded: bool = False) -> str:
    """A clean personal record: title + date, then one section per contact."""
    rows = _fetch_contacts(conn, include_excluded)
    out: List[str] = [
        "# Network CRM export",
        "",
        f"Generated {_now_iso()} · {len(rows)} contacts",
        "",
    ]
    for row in rows:
        out.extend(_markdown_contact(conn, row))
    return "\n".join(out).rstrip() + "\n"


# ── CSV ───────────────────────────────────────────────────────────────────────
def _csv_safe(v: Any) -> str:
    """Prefix a leading = + - @ (or control char) with an apostrophe so a
    scraped field can't execute as a spreadsheet formula on open."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "name": _csv_safe(row["name"] or ""),
        "company": _csv_safe(row["company"] or ""),
        "position": _csv_safe(row["position"] or ""),
        "category": row["category"] or "",
        "status": row["status"] or "",
        "excluded": 1 if row["excluded"] else 0,
        "linkedin_msgs": row["msg_count"] or 0,
        "imessages": row["imsg_count"] or 0,
        "whatsapp": row["wa_count"] or 0,
        "email": row["em_count"] or 0,
        "telegram": row["tg_count"] or 0,
        "messenger": row["msgr_count"] or 0,
        "instagram": row["ig_count"] or 0,
        "last_contact": _last_seen(row),
        "notes_count": len(db.get_notes(conn, row["id"])),
        "how_we_met": _csv_safe(row["how_we_met"] or ""),
    }


def export_csv(conn: sqlite3.Connection, include_excluded: bool = False) -> str:
    """One row per contact with a header line. Proper quoting via the csv module."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _fetch_contacts(conn, include_excluded):
        writer.writerow(_csv_row(conn, row))
    return buf.getvalue()


# ── JSON ──────────────────────────────────────────────────────────────────────
def _json_contact(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    contact = {key: row[key] for key in row.keys()}
    contact["excluded"] = bool(row["excluded"])
    contact["notes"] = [
        {
            "body": n["body"],
            "kind": n["kind"],
            "created_at": n["created_at"],
        }
        for n in _notes_oldest_first(conn, row["id"])
    ]
    contact["identifiers"] = [
        {"kind": i["kind"], "value": i["value"]}
        for i in db.get_identifiers(conn, row["id"])
    ]
    return contact


def export_json(conn: sqlite3.Connection, include_excluded: bool = False) -> str:
    """Full-fidelity JSON: every contact field plus nested notes + identifiers."""
    rows = _fetch_contacts(conn, include_excluded)
    payload = {
        "exported_at": _now_iso(),
        "contacts": [_json_contact(conn, row) for row in rows],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ── CLI ───────────────────────────────────────────────────────────────────────
EXPORTERS = {
    "md": (export_markdown, "md"),
    "csv": (export_csv, "csv"),
    "json": (export_json, "json"),
}


def _default_out(fmt: str) -> str:
    ext = EXPORTERS[fmt][1]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"network-crm-export-{date}.{ext}"


def build_export(conn: sqlite3.Connection, fmt: str, include_excluded: bool = False) -> str:
    """Dispatch to the right exporter. Raises ValueError on an unknown format."""
    if fmt not in EXPORTERS:
        raise ValueError(f"unknown format: {fmt}")
    return EXPORTERS[fmt][0](conn, include_excluded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export / back up the Network CRM.")
    parser.add_argument("--format", choices=sorted(EXPORTERS), default="md",
                        help="output format (default: md)")
    parser.add_argument("--out", default=None,
                        help="output path (default: network-crm-export-YYYY-MM-DD.<ext>)")
    parser.add_argument("--include-excluded", action="store_true",
                        help="include contacts marked excluded (people you chose never to index)")
    args = parser.parse_args()

    out_path = args.out or _default_out(args.format)
    conn = db.get_db()
    try:
        content = build_export(conn, args.format, include_excluded=args.include_excluded)
    finally:
        conn.close()

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Wrote {args.format} export to {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
