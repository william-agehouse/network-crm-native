"""
NO LONGER A ROW ON THE DATA PAGE — command line only (NC-015).

Messenger and Instagram both had rows and neither had ever imported anything:
nothing ever filled in `msgr_count` or `ig_count`. Messenger is the
reason all three Meta/Telegram rows went rather than one being finished — default
end-to-end encryption keeps recent chats out of the standard DYI export
altogether, and the ones that remain need a separate PIN-protected flow this file
does not know. A row that can only ever return a fraction of your history is
worse than no row.

The script itself is untouched and still works:

    python meta_import.py <export folder> --platform messenger

Everything it writes still counts — the archive channels and the
`msgr_count`/`ig_count` columns were kept for exactly that reason.

Import your Meta DMs (Facebook Messenger / Instagram) into crm.db — runs entirely
on your Mac.

You request a "Download Your Information" (DYI) export from Meta (Accounts Center
→ Your information and permissions → Download your information, format **JSON**).
That produces per-thread folders, each holding one or more message_<n>.json files:

  • Messenger:   .../messages/inbox/<thread_slug>/message_1.json
  • Instagram:   .../your_instagram_activity/messages/inbox/<thread_slug>/message_1.json

Point this script at the export root, at a `messages/inbox` directory, or at a
single thread folder — it walks the tree and finds every message_*.json. Nothing
needs special macOS permission — it's just a folder you chose to download.

Like the iMessage, WhatsApp, email and Telegram imports, this follows the
"only people you actually know, two-way only, no noise" philosophy:

  • It enriches EXISTING contacts (your LinkedIn connections etc.) with per-platform
    message stats — it does not create a contact from every group member. A thread
    matches a contact when the other participant's name normalizes to an existing
    contact's name. Pass --create-new to also create a contact from a real,
    unmatched participant name.
  • One-to-one threads only (exactly two participants). Group threads (>2
    participants) are ignored.
  • Two-way only: a thread counts only if there's at least one message FROM you and
    at least one FROM the other person. One-sided threads don't count.
  • People in exclude_list.txt (anyone you chose never to index) are skipped.

It reads only message metadata (who sent it and when) — message text is never
summarized or sent anywhere.

Usage:
    python meta_import.py <path>                                  # apply (auto-detect platform)
    python meta_import.py <path> --platform instagram             # force the platform
    python meta_import.py <path> --dry-run                        # preview, write nothing
    python meta_import.py <path> --create-new                     # also create from real names
"""

import os
import re
import sys
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import config  # noqa: F401 — loads .env
import db
import archive as arc

HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))
SELF_NAME = os.environ.get("SELF_NAME", "")  # your own name, so you're excluded from counts

VALID_PLATFORMS = ("messenger", "instagram")
# A 1:1 thread has exactly two participants (you + the other person).
ONE_TO_ONE_PARTICIPANTS = 2


# ── Meta mojibake fix ──────────────────────────────────────────────────────────
def fix_mojibake(s: Optional[str]) -> str:
    """Undo Meta's broken encoding: it serializes UTF-8 bytes escaped as if they
    were Latin-1, so 'José' arrives as 'JosÃ©'. Re-encode as Latin-1 and
    decode as UTF-8 to recover the real text. If that round-trip fails (the string
    was already clean, or holds bytes that aren't valid UTF-8) leave it untouched.
    """
    if not s:
        return ""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def has_alpha(s: str) -> bool:
    return any(c.isalpha() for c in (s or ""))


def ms_to_iso(timestamp_ms) -> str:
    """A `timestamp_ms` (epoch milliseconds) → 'YYYY-MM-DD HH:MM:SS' in UTC.
    '' if missing or unparseable."""
    if timestamp_ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# ── Platform detection ──────────────────────────────────────────────────────────
def detect_platform(path: str, override: Optional[str]) -> str:
    """Resolve the platform. An explicit --platform wins; otherwise a path that
    mentions instagram → instagram, else messenger (Facebook's product name)."""
    if override:
        if override not in VALID_PLATFORMS:
            raise SystemExit(f"--platform must be one of {VALID_PLATFORMS}, got {override!r}")
        return override
    return "instagram" if "instagram" in path.lower() else "messenger"


# ── Export discovery & parsing ──────────────────────────────────────────────────
def find_message_files(path: str) -> List[str]:
    """Return every message_*.json under `path`, sorted. Accepts an export root, a
    `messages/inbox` directory, or a single thread folder. Raises SystemExit if the
    path doesn't exist or holds no message files."""
    if not os.path.exists(path):
        raise SystemExit(f"Meta export path not found: {path}")
    files: List[str] = []
    if os.path.isfile(path):
        files = [path] if re.match(r"message_\d+\.json$", os.path.basename(path)) else []
    else:
        for root, _dirs, names in os.walk(path):
            for name in names:
                if re.match(r"message_\d+\.json$", name):
                    files.append(os.path.join(root, name))
    if not files:
        raise SystemExit(
            f"No message_*.json files found under {path}.\n"
            "Point this script at a Meta 'Download Your Information' export "
            "(its root, a messages/inbox folder, or a single thread folder)."
        )
    return sorted(files)


def group_files_by_thread(files: List[str]) -> Dict[str, List[str]]:
    """Group message files by their containing thread folder, so a thread split
    across message_1.json, message_2.json, … is aggregated as one conversation."""
    by_thread: Dict[str, List[str]] = {}
    for fpath in files:
        by_thread.setdefault(os.path.dirname(fpath), []).append(fpath)
    return by_thread


class ThreadAgg:
    """Per one-to-one thread running tally collected from the export."""

    __slots__ = ("name", "from_self", "from_them", "first", "last")

    def __init__(self, name: str) -> None:
        self.name = name
        self.from_self = 0     # messages you sent
        self.from_them = 0     # messages the other person sent
        self.first = ""
        self.last = ""

    @property
    def count(self) -> int:
        return self.from_self + self.from_them

    @property
    def two_way(self) -> bool:
        return self.from_self > 0 and self.from_them > 0

    def touch(self, date: str) -> None:
        if date:
            if not self.first or date < self.first:
                self.first = date
            if date > self.last:
                self.last = date


def _load_thread(files: List[str]) -> Tuple[List[str], str, List[dict]]:
    """Read every file for one thread, returning (participant names, title,
    messages). Names/title are mojibake-fixed; messages keep their raw dicts.
    Files that don't parse are skipped cleanly."""
    participants: List[str] = []
    title = ""
    messages: List[dict] = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not participants:
            participants = [fix_mojibake((p or {}).get("name")) for p in
                            (data.get("participants") or [])]
        if not title:
            title = fix_mojibake(data.get("title"))
        messages.extend(data.get("messages") or [])
    return participants, title, messages


def parse_threads(by_thread: Dict[str, List[str]]) -> Tuple[List[Tuple[List[str], str, List[dict]]], int]:
    """Read each thread folder into (participants, title, messages). Threads that
    aren't 1:1 (participant count != 2) are counted toward group_count and dropped.
    Returns (one_to_one_threads, group_count)."""
    one_to_one: List[Tuple[List[str], str, List[dict]]] = []
    group_count = 0
    for files in by_thread.values():
        participants, title, messages = _load_thread(files)
        if len(participants) != ONE_TO_ONE_PARTICIPANTS:
            group_count += 1
            continue
        one_to_one.append((participants, title, messages))
    return one_to_one, group_count


# ── Self detection ──────────────────────────────────────────────────────────────
def detect_self(threads: List[Tuple[List[str], str, List[dict]]]) -> str:
    """Decide which participant is "you". SELF_NAME (mojibake-fixed) wins; else
    auto-detect — across all 1:1 threads, you are the participant who appears in
    the MOST threads (you're in every one). Returns the normalized self name."""
    self_norm = db.normalize_name(fix_mojibake(SELF_NAME))
    if self_norm:
        return self_norm
    appearances: Dict[str, int] = {}
    label: Dict[str, str] = {}
    for participants, _title, _messages in threads:
        for name in set(db.normalize_name(p) for p in participants if p):
            appearances[name] = appearances.get(name, 0) + 1
        for p in participants:
            label.setdefault(db.normalize_name(p), p)
    if not appearances:
        return ""
    self_norm = max(appearances, key=lambda k: (appearances[k], k))
    print(f"  (auto-detected you as the most frequent participant: "
          f"'{label.get(self_norm, self_norm)}'; set SELF_NAME in .env to override)")
    return self_norm


# ── Aggregation ─────────────────────────────────────────────────────────────────
def aggregate_threads(threads: List[Tuple[List[str], str, List[dict]]],
                      self_norm: str) -> List[ThreadAgg]:
    """Turn parsed 1:1 threads into ThreadAgg tallies. The contact is the
    participant who isn't you (falling back to the thread title); each message is
    attributed to you or them by its mojibake-fixed sender_name."""
    out: List[ThreadAgg] = []
    for participants, title, messages in threads:
        others = [p for p in participants if db.normalize_name(p) != self_norm]
        # the other person; if both normalize to self (shouldn't happen) use title
        name = others[0] if others else title
        agg = ThreadAgg((name or "").strip())
        for message in messages:
            sender_norm = db.normalize_name(fix_mojibake(message.get("sender_name")))
            date = ms_to_iso(message.get("timestamp_ms"))
            if sender_norm == self_norm:
                agg.from_self += 1
            else:
                agg.from_them += 1
            agg.touch(date)
        out.append(agg)
    return out


# ── Matching to existing contacts ─────────────────────────────────────────────
def match_contact(conn, name: str) -> Optional[int]:
    """Resolve a thread's other-participant name to an EXISTING contact id, or None."""
    if not has_alpha(name):
        return None
    existing = db.find_exact(conn, name)
    return existing[0]["id"] if existing else None


# ── Apply to crm.db ───────────────────────────────────────────────────────────
def apply(threads: List[ThreadAgg], group_count: int, platform: str, *,
          create_new: bool, dry_run: bool) -> Tuple[dict, Dict[str, int]]:
    conn = db.get_db()
    db.ensure_columns(conn)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    stats = {
        "matched": 0, "created": 0, "skipped_group": group_count,
        "skipped_one_sided": 0, "skipped_exclude": 0, "unmatched": 0,
    }

    # Aggregate per contact BEFORE writing so the same person appearing in more
    # than one thread (e.g. split files, or a re-created chat) gets a single bump.
    per_contact: Dict[int, Dict[str, object]] = {}
    name_to_cid: Dict[str, int] = {}

    for thread in threads:
        if not thread.two_way:
            stats["skipped_one_sided"] += 1
            continue
        if not has_alpha(thread.name):
            stats["unmatched"] += 1
            continue
        if db.normalize_name(thread.name) in exclude_norms:
            stats["skipped_exclude"] += 1
            continue

        cid = match_contact(conn, thread.name)
        if cid is not None:
            stats["matched"] += 1
        elif create_new:
            cid = None if dry_run else db.create_contact(conn, thread.name, source=platform)
            stats["created"] += 1
        else:
            stats["unmatched"] += 1
            continue

        if cid is None:   # dry-run create — nothing to aggregate
            continue
        name_to_cid[thread.name] = cid
        bucket = per_contact.setdefault(cid, {"count": 0, "last": ""})
        bucket["count"] += thread.count                       # type: ignore[operator]
        bucket["last"] = max(bucket["last"], thread.last)     # type: ignore[type-var]

    if not dry_run:
        for cid, bucket in per_contact.items():
            db.set_channel(conn, cid, platform, bucket["count"], bucket["last"])
        conn.commit()
    conn.close()

    stats["contacts_touched"] = len(per_contact)
    return stats, name_to_cid


def archive_messages(threads_raw: List[Tuple], name_to_cid: Dict[str, int],
                     platform: str, self_norm: str,
                     archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Read message text from the parsed Meta threads and write to archive.db.

    `threads_raw` is the same List[Tuple[participants, title, messages]] used by
    aggregate_threads(). Only contacts in name_to_cid are archived. Returns
    {"new": int, "skipped": int}.
    """
    if not name_to_cid:
        return {"new": 0, "skipped": 0}

    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)

    new = skipped = 0
    for participants, title, messages in threads_raw:
        others = [p for p in participants if db.normalize_name(fix_mojibake(p)) != self_norm]
        contact_name = fix_mojibake(others[0]).strip() if others else fix_mojibake(title).strip()
        cid = name_to_cid.get(contact_name)
        if cid is None:
            continue
        thread_key = db.normalize_name(contact_name)[:20]
        for msg in messages:
            body = fix_mojibake(msg.get("content") or "").strip()
            if not body:
                continue
            ts_ms = msg.get("timestamp_ms")
            if ts_ms is None:
                continue
            ts = ms_to_iso(ts_ms)
            sender_norm = db.normalize_name(fix_mojibake(msg.get("sender_name") or ""))
            from_me = int(sender_norm == self_norm)
            source_key = f"{thread_key}_{ts_ms}"
            inserted = arc.insert_message(
                arc_conn,
                contact_id=cid,
                channel=platform,
                direction=from_me,
                ts=ts,
                body=body,
                source_key=source_key,
            )
            if inserted:
                new += 1
            else:
                skipped += 1

    arc_conn.commit()
    arc_conn.close()
    return {"new": new, "skipped": skipped}


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args(argv: List[str]) -> Tuple[Optional[str], Optional[str], bool, bool]:
    path: Optional[str] = None
    platform: Optional[str] = None
    create_new = "--create-new" in argv
    dry_run = "--dry-run" in argv
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--platform":
            platform = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if not arg.startswith("-") and path is None:
            path = arg
        i += 1
    return path, platform, create_new, dry_run


def main() -> None:
    path, platform_arg, create_new, dry_run = _parse_args(sys.argv)

    if not path:
        raise SystemExit("Usage: python meta_import.py <path> "
                         "[--platform messenger|instagram] [--create-new] [--dry-run]")

    platform = detect_platform(path, platform_arg)
    files = find_message_files(path)
    by_thread = group_files_by_thread(files)

    db.init_db()
    print(f"Reading Meta export ({platform}): {path}")
    parsed, group_count = parse_threads(by_thread)
    print(f"  {len(parsed)} one-to-one threads ({group_count} groups ignored)")

    self_norm = detect_self(parsed)
    threads = aggregate_threads(parsed, self_norm)

    stats, name_to_cid = apply(threads, group_count, platform, create_new=create_new,
                                dry_run=dry_run)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"\n{tag}Platform:                    {platform}\n"
        f"{tag}Matched to existing contacts: {stats['matched']}\n"
        f"{tag}New contacts created:        {stats['created']}\n"
        f"{tag}Skipped (group):             {stats['skipped_group']}\n"
        f"{tag}Skipped (one-sided):         {stats['skipped_one_sided']}\n"
        f"{tag}Skipped (exclude_list):      {stats['skipped_exclude']}\n"
        f"{tag}Unmatched (ignored):         {stats['unmatched']}\n"
        f"{tag}Contacts updated:            {stats['contacts_touched']}"
    )
    if not dry_run and name_to_cid:
        print("\nArchiving message content…")
        arc_stats = archive_messages(parsed, name_to_cid, platform, self_norm)
        print(f"  {arc_stats['new']:,} new messages archived  "
              f"({arc_stats['skipped']:,} already stored)")
        if arc_stats["new"]:
            arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
            n_chunks = sum(arc.build_chunks(arc_conn, contact_id=cid)
                          for cid in set(name_to_cid.values()))
            arc_conn.close()
            print(f"  {n_chunks:,} conversation chunks ready for search")


if __name__ == "__main__":
    main()
