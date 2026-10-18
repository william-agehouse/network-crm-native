"""
NO LONGER A ROW ON THE DATA PAGE — command line only (NC-015).

Unlike Messenger there is nothing wrong with this importer — the export it reads
is complete and local. Its row went with the other two because the export is a
hand-made download, and the product's rule is that an import should not need a
manual step (NC-036), so a Telegram row was never going to become automatic.

The script itself is untouched and still works:

    python telegram_import.py <result.json>

Import your Telegram history into crm.db — runs entirely on your Mac.

You export your data from Telegram Desktop (Settings → Advanced → "Export
Telegram data", format **Machine-readable JSON**). That produces a folder
containing a `result.json`. Point this script at that file (or its folder).
Nothing needs special macOS permission — it's just a file you chose to export.

Like the iMessage, WhatsApp and email imports, this follows the "only people you
actually know, two-way only, no noise" philosophy:

  • It enriches EXISTING contacts (your LinkedIn connections etc.) with Telegram
    message stats — it does not create a contact from every group member or bot.
    A chat matches a contact when the chat's display name normalizes to an
    existing contact's name. Pass --create-new to also create a contact from a
    real, unmatched chat name.
  • Only one-to-one chats count (type == "personal_chat"). Groups, supergroups,
    channels, bots and "Saved Messages" are ignored.
  • Two-way only: a chat counts only if there's at least one message FROM you and
    at least one FROM the other person. One-sided chats (only you, or only them)
    don't count.
  • People in exclude_list.txt are skipped.

It reads only message metadata (who sent it and when) — message text is never
summarized or sent anywhere.

Usage:
    python telegram_import.py <result.json | export_dir>              # apply
    python telegram_import.py <result.json | export_dir> --dry-run     # preview
    python telegram_import.py <result.json | export_dir> --create-new  # also create
"""

import os
import sys
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import config  # noqa: F401 — loads .env
import db
import archive as arc

HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))

# Only a one-to-one conversation enriches a contact. Everything else is noise.
PERSONAL_CHAT_TYPE = "personal_chat"
# Telegram tags your own outgoing messages as "user<your numeric id>".
SELF_ID_PREFIX = "user"


# ── Date parsing ──────────────────────────────────────────────────────────────
def parse_message_date(message: dict) -> str:
    """A message's timestamp → 'YYYY-MM-DD HH:MM:SS' in UTC. '' if unparseable.

    Prefer `date_unixtime` (epoch seconds); fall back to the ISO-ish `date`
    field ('YYYY-MM-DDTHH:MM:SS', already in the export's local time)."""
    raw_unix = message.get("date_unixtime")
    if raw_unix is not None:
        try:
            dt = datetime.fromtimestamp(int(raw_unix), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    raw = message.get("date")
    if raw:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
    return ""


def has_alpha(s: str) -> bool:
    return any(c.isalpha() for c in (s or ""))


# ── Export parsing ────────────────────────────────────────────────────────────
def load_export(path: str) -> dict:
    """Read result.json from a file path or an export directory. Raises
    SystemExit with a helpful message if it can't be found or parsed."""
    if os.path.isdir(path):
        candidate = os.path.join(path, "result.json")
        if not os.path.exists(candidate):
            raise SystemExit(
                f"No result.json found in {path}.\n"
                "Point this script at the result.json from a Telegram Desktop "
                "export (Settings → Advanced → Export Telegram data, "
                "Machine-readable JSON)."
            )
        path = candidate
    if not os.path.exists(path):
        raise SystemExit(f"Telegram export not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read Telegram export {path}: {exc}")


def self_id(data: dict) -> str:
    """Your own from_id, e.g. 'user111111111'. '' if the export lacks it."""
    user_id = (data.get("personal_information") or {}).get("user_id")
    if user_id is None:
        return ""
    return f"{SELF_ID_PREFIX}{user_id}"


class ChatAgg:
    """Per personal-chat running tally collected from the export."""

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


def aggregate_chats(data: dict) -> Tuple[List[ChatAgg], int]:
    """Scan the export and return (one-to-one chat tallies, group_count).

    Only `personal_chat` entries produce a ChatAgg; every other chat type
    (groups, supergroups, channels, bots, saved messages) is counted toward
    group_count and otherwise ignored."""
    me = self_id(data)
    chats = (data.get("chats") or {}).get("list") or []
    out: List[ChatAgg] = []
    group_count = 0

    for chat in chats:
        if chat.get("type") != PERSONAL_CHAT_TYPE:
            group_count += 1
            continue
        agg = ChatAgg((chat.get("name") or "").strip())
        for message in chat.get("messages") or []:
            if message.get("type") != "message":   # skip "service" messages
                continue
            date = parse_message_date(message)
            if str(message.get("from_id") or "") == me:
                agg.from_self += 1
            else:
                agg.from_them += 1
            agg.touch(date)
        out.append(agg)
    return out, group_count


# ── Matching to existing contacts ─────────────────────────────────────────────
def match_contact(conn, name: str) -> Optional[int]:
    """Resolve a chat display name to an EXISTING contact id, or None."""
    if not has_alpha(name):
        return None
    existing = db.find_exact(conn, name)
    return existing[0]["id"] if existing else None


# ── Apply to crm.db ───────────────────────────────────────────────────────────
def apply(chats: List[ChatAgg], group_count: int, *, create_new: bool,
          dry_run: bool) -> Tuple[dict, Dict[str, int]]:
    conn = db.get_db()
    db.ensure_columns(conn)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    stats = {
        "matched": 0, "created": 0, "skipped_group": group_count,
        "skipped_one_sided": 0, "skipped_exclude": 0, "unmatched": 0,
    }

    # Aggregate per contact BEFORE writing so the same person appearing in more
    # than one personal_chat gets a single bump.
    per_contact: Dict[int, Dict[str, object]] = {}
    name_to_cid: Dict[str, int] = {}

    for chat in chats:
        if not chat.two_way:
            stats["skipped_one_sided"] += 1
            continue
        if not has_alpha(chat.name):
            stats["unmatched"] += 1
            continue
        if db.normalize_name(chat.name) in exclude_norms:
            stats["skipped_exclude"] += 1
            continue

        cid = match_contact(conn, chat.name)
        if cid is not None:
            stats["matched"] += 1
        elif create_new:
            cid = None if dry_run else db.create_contact(conn, chat.name, source="telegram")
            stats["created"] += 1
        else:
            stats["unmatched"] += 1
            continue

        if cid is None:   # dry-run create — nothing to aggregate
            continue
        name_to_cid[chat.name] = cid
        bucket = per_contact.setdefault(cid, {"count": 0, "last": ""})
        bucket["count"] += chat.count                        # type: ignore[operator]
        bucket["last"] = max(bucket["last"], chat.last)      # type: ignore[type-var]

    if not dry_run:
        for cid, bucket in per_contact.items():
            db.set_channel(conn, cid, "telegram", bucket["count"], bucket["last"])
        conn.commit()
    conn.close()

    stats["contacts_touched"] = len(per_contact)
    return stats, name_to_cid


def _tg_text(msg: dict) -> str:
    """Extract plain text from a Telegram message dict.

    The `text` field is either a plain string or a list of entities;
    in the list form each entity is a dict with a `text` key, or a bare string."""
    raw = msg.get("text") or ""
    if isinstance(raw, list):
        return "".join(e.get("text", "") if isinstance(e, dict) else e for e in raw)
    return str(raw)


def archive_messages(data: dict, name_to_cid: Dict[str, int],
                     archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Read message text from the Telegram export and write to archive.db.

    Only contacts present in name_to_cid (matched/created by apply()) are
    archived. Returns {"new": int, "skipped": int}.
    """
    if not name_to_cid:
        return {"new": 0, "skipped": 0}

    me = self_id(data)
    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)

    new = skipped = 0
    for chat in (data.get("chats") or {}).get("list") or []:
        if chat.get("type") != PERSONAL_CHAT_TYPE:
            continue
        chat_name = (chat.get("name") or "").strip()
        cid = name_to_cid.get(chat_name)
        if cid is None:
            continue
        chat_key = chat_name[:32]  # prefix to keep source_key bounded
        for msg in chat.get("messages") or []:
            if msg.get("type") != "message":
                continue
            body = _tg_text(msg).strip()
            if not body:
                continue
            ts = parse_message_date(msg)
            from_me = str(msg.get("from_id") or "") == me
            msg_id = str(msg.get("id") or "")
            source_key = f"{chat_key}_{msg_id}"
            inserted = arc.insert_message(
                arc_conn,
                contact_id=cid,
                channel="telegram",
                direction=int(from_me),
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
def _parse_args(argv: List[str]) -> Tuple[Optional[str], bool, bool]:
    path: Optional[str] = None
    create_new = "--create-new" in argv
    dry_run = "--dry-run" in argv
    for arg in argv[1:]:
        if not arg.startswith("-"):
            path = arg
            break
    return path, create_new, dry_run


def main() -> None:
    path, create_new, dry_run = _parse_args(sys.argv)

    if not path:
        raise SystemExit("Usage: python telegram_import.py <result.json | export_dir> "
                         "[--create-new] [--dry-run]")

    data = load_export(path)
    db.init_db()
    print(f"Reading Telegram export: {path}")
    chats, group_count = aggregate_chats(data)
    print(f"  {len(chats)} one-to-one chats ({group_count} groups/other ignored)")

    stats, name_to_cid = apply(chats, group_count, create_new=create_new, dry_run=dry_run)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"\n{tag}Matched to existing contacts: {stats['matched']}\n"
        f"{tag}New contacts created:        {stats['created']}\n"
        f"{tag}Skipped (group/other):       {stats['skipped_group']}\n"
        f"{tag}Skipped (one-sided):         {stats['skipped_one_sided']}\n"
        f"{tag}Skipped (exclude_list):      {stats['skipped_exclude']}\n"
        f"{tag}Unmatched (ignored):         {stats['unmatched']}\n"
        f"{tag}Contacts updated:            {stats['contacts_touched']}"
    )
    if not dry_run and name_to_cid:
        print("\nArchiving message content…")
        arc_stats = archive_messages(data, name_to_cid)
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
