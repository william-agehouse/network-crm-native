"""
Import WhatsApp profile photos into the CRM — from files you copy yourself.

WhatsApp Desktop caches every profile picture it has seen as plain JPEGs in
    ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/Media/Profile/
with filenames like `<jid-digits>-<pictureid>.jpg` (plus smaller `.thumb` files).
Same no-privileged-access pattern as the message importer: copy that folder (or
select it directly) and point this script at your copy.

Matching a file to a person, safest signal first:
  1. The digits prefix is looked up in ChatStorage.sqlite's ZWACHATSESSION
     (individual chats only) → the chat's saved partner name → matched to an
     existing CRM contact by normalized name. This also resolves WhatsApp's
     anonymized `@lid` numbers, which are NOT phone numbers.
  2. If there's no session (or the partner name is just a phone placeholder),
     a prefix that plausibly IS a phone number (8–13 digits) is matched against
     stored phone identifiers via db.find_by_handle (last-9-digits).
  3. Anything else — group avatars, unmatched lids — is skipped. A wrong face on
     the wrong person is worse than no face.

Only people already in your CRM get photos; exclude_list.txt is honored; photos
never overwrite a manually-set or address-book photo (see photos.SOURCE_RANK).
Re-running refreshes changed avatars, and only those: a photo whose file has the
same name, size and date as when it was last saved is left alone (NC-224).
Supports --dry-run.

Usage:
    python whatsapp_photos.py --dir ~/Desktop/Profile [--db ~/Desktop/ChatStorage.sqlite] [--dry-run]

If --db is omitted, ChatStorage.sqlite is looked for next to the folder (its
parent directories included), matching how the container is laid out.
"""

import argparse
import json
import os
import re
import sqlite3
from typing import Dict, Optional, Tuple

import config  # noqa: F401 — loads .env
import db
import photos

EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))

# JID-digit prefixes longer than this are WhatsApp anonymized ids (@lid), not phones.
MAX_PHONE_DIGITS = 13
MIN_PHONE_DIGITS = 8

# NC-224. What a saved photo's file looked like at the time, one row per person
# in the state table: "wa_photo_seen_<digits>" -> [file name, size, last written
# (nanoseconds), person id]. Saving a photo writes a temporary copy, converts it
# and compares it against every other person's photo, so a run in which one
# avatar changed used to redo that for everyone. A row is only written when a
# photo is actually saved, so a run with nothing new writes nothing.
SEEN_KEY = "wa_photo_seen_"


def find_chatstorage(profile_dir: str) -> Optional[str]:
    """ChatStorage.sqlite in / next to / two levels above the Profile folder —
    covers both a straight container copy and a copied-out folder pair."""
    candidates = [
        os.path.join(profile_dir, "ChatStorage.sqlite"),
        os.path.join(os.path.dirname(profile_dir), "ChatStorage.sqlite"),
        os.path.join(os.path.dirname(os.path.dirname(profile_dir)), "ChatStorage.sqlite"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_session_names(chatstorage_path: str) -> Dict[str, str]:
    """{jid digits: saved partner name} for individual chats (ZSESSIONTYPE=0)."""
    uri = f"file:{chatstorage_path}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT ZCONTACTJID AS jid, ZPARTNERNAME AS name
               FROM ZWACHATSESSION
               WHERE ZSESSIONTYPE = 0 AND ZCONTACTJID IS NOT NULL"""
        ).fetchall()
    finally:
        conn.close()
    out: Dict[str, str] = {}
    for r in rows:
        digits = re.sub(r"\D", "", (r["jid"] or "").split("@", 1)[0])
        if digits:
            out[digits] = (r["name"] or "").strip()
    return out


def is_phone_placeholder(name: str) -> bool:
    """True when a WhatsApp partner name is just the number ('+46 70 174 06 05')."""
    return bool(name) and not re.search(r"[^\d\s()+\-.]", name)


def best_files_by_prefix(profile_dir: str) -> Dict[str, str]:
    """Pick one file per jid prefix: real .jpg beats .thumb, then largest wins."""
    best: Dict[str, Tuple[int, int, str]] = {}  # prefix -> (is_jpg, size, path)
    for fname in os.listdir(profile_dir):
        if not fname.endswith((".jpg", ".thumb")):
            continue
        prefix = fname.split("-", 1)[0]
        if not prefix.isdigit():
            continue
        path = os.path.join(profile_dir, fname)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        key = (1 if fname.endswith(".jpg") else 0, size, path)
        if prefix not in best or key > best[prefix]:
            best[prefix] = key
    return {prefix: path for prefix, (_j, _s, path) in best.items()}


def file_mark(path: str) -> Optional[list]:
    """[name, size, last written in nanoseconds] for a photo file, or None when
    it cannot be looked at."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [os.path.basename(path), st.st_size, st.st_mtime_ns]


def unchanged_since_saved(conn, prefix: str, cid: int, mark: Optional[list]) -> bool:
    """True when this very file, for this very person, already produced the photo
    that is still in place — so saving it again could only repeat the work.

    All of it has to hold: the file looks as it did when saved, it is still
    matched to the same person, and that person still has a WhatsApp photo on
    disk. A photo the owner removed, replaced by hand or let another source take
    over (a tie in rank is allowed to refresh it) is saved or refused afresh, as
    before.
    """
    if mark is None:
        return False
    try:
        saved = json.loads(db.state_get(conn, SEEN_KEY + prefix, "") or "null")
    except ValueError:
        return False
    if saved != mark + [cid]:
        return False
    row = conn.execute("SELECT photo, photo_source FROM contacts WHERE id = ?",
                       (int(cid),)).fetchone()
    return bool(row and row["photo"] and row["photo_source"] == "whatsapp"
                and os.path.exists(photos.photo_path(cid)))


def resolve_contact(conn, prefix: str, session_names: Dict[str, str],
                    exclude_norms: set) -> Tuple[Optional[int], str]:
    """(contact_id, how) for a jid-digit prefix, or (None, reason-skipped)."""
    name = session_names.get(prefix, "")
    if name and not is_phone_placeholder(name):
        if db.normalize_name(name) in exclude_norms:
            return None, "excluded"
        rows = db.find_exact(conn, name)
        if rows:
            return rows[0]["id"], "name"
    if MIN_PHONE_DIGITS <= len(prefix) <= MAX_PHONE_DIGITS:
        row = db.find_by_handle(conn, prefix)
        if row is not None:
            if db.normalize_name(row["name"]) in exclude_norms:
                return None, "excluded"
            return row["id"], "phone"
    return None, "unmatched"


def run(profile_dir: str, chatstorage_path: Optional[str], dry_run: bool) -> dict:
    conn = db.get_db()
    db.ensure_columns(conn)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)

    session_names: Dict[str, str] = {}
    if chatstorage_path:
        try:
            session_names = load_session_names(chatstorage_path)
        except sqlite3.Error as exc:
            print(f"  note: could not read ChatStorage.sqlite ({exc}); "
                  f"matching by phone number only")

    files = best_files_by_prefix(profile_dir)
    stats = {"files": len(files), "matched_name": 0, "matched_phone": 0,
             "saved": 0, "unchanged": 0, "kept_better": 0, "excluded": 0,
             "unmatched": 0, "unreadable": 0}

    for prefix, path in sorted(files.items()):
        cid, how = resolve_contact(conn, prefix, session_names, exclude_norms)
        if cid is None:
            stats[how if how in stats else "unmatched"] += 1
            continue
        stats["matched_name" if how == "name" else "matched_phone"] += 1
        if dry_run:
            continue
        mark = file_mark(path)
        if unchanged_since_saved(conn, prefix, cid, mark):
            stats["unchanged"] += 1
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            stats["unreadable"] += 1
            continue
        if photos.save_photo(conn, cid, data, "whatsapp"):
            stats["saved"] += 1
            if mark is not None:
                db.state_set(conn, SEEN_KEY + prefix, json.dumps(mark + [cid]))
        else:
            stats["kept_better"] += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Import WhatsApp profile photos")
    ap.add_argument("--dir", required=True, help="Media/Profile folder (or your copy of it)")
    ap.add_argument("--db", default=None, help="ChatStorage.sqlite path (auto-detected if omitted)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    profile_dir = os.path.expanduser(args.dir)
    if not os.path.isdir(profile_dir):
        raise SystemExit(f"Profile folder not found: {profile_dir}")
    chatstorage = os.path.expanduser(args.db) if args.db else find_chatstorage(profile_dir)

    db.init_db()
    print(f"Reading profile photos: {profile_dir}")
    print(f"Chat sessions for name matching: {chatstorage or '(none found — phone matching only)'}")

    stats = run(profile_dir, chatstorage, args.dry_run)
    tag = "[dry-run] " if args.dry_run else ""
    print(
        f"\n{tag}Photo files (best per person):  {stats['files']}\n"
        f"{tag}Matched by chat name:           {stats['matched_name']}\n"
        f"{tag}Matched by phone number:        {stats['matched_phone']}\n"
        f"{tag}Photos saved/refreshed:         {stats['saved']}\n"
        f"{tag}Unchanged since last time:      {stats['unchanged']}\n"
        f"{tag}Kept existing better photo:     {stats['kept_better']}\n"
        f"{tag}Skipped (excluded):             {stats['excluded']}\n"
        f"{tag}Skipped (groups/unknown ids):   {stats['unmatched']}"
    )
    if not args.dry_run and stats["saved"]:
        print("\nFaces will show up across the app immediately — no restart needed.")


if __name__ == "__main__":
    main()
