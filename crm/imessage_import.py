"""
Import your iMessage history into crm.db — runs entirely on your Mac.

iMessage stores everything in ~/Library/Messages/chat.db, but messages are keyed
by *handle* (a phone number or email), not by name. So this does two things:

  1. Counts your 1-to-1 messages per handle (sent + received) and the last date.
  2. Resolves each handle to a name using your macOS Contacts (AddressBook), then:
       • matches it to an existing contact (e.g. your LinkedIn connections) by name
         → adds the phone/email as an identifier + fills in iMessage stats, or
       • creates a new contact (source=imessage) if the name is new, or
       • if the handle isn't in your Contacts at all (numbers you never saved, one-off
         senders), it's ignored — these aren't important. Pass --log-unmatched
         to dump them to imessage_unmatched.csv, or --include-unknown to add them.

It only needs message metadata (handle + date) for the core import — it never has
to read message text, so nothing private is summarized or sent anywhere.

macOS protects ~/Library/Messages, so this script does NOT read chat.db there
directly (that would need Full Disk Access). Instead you point --chatdb at a copy
somewhere unprotected. This importer itself is never granted Full Disk Access.

Two ways to get that copy:
  • by hand — see COPY_HELP below. Nothing on the Mac gets Full Disk Access, but
    a Finder copy leaves the -wal sidecar behind, so the newest messages can be
    missing, and it goes stale until you redo it.
  • the opt-in helper (packaging/IMESSAGE_SYNC.md) — a tiny signed binary that
    holds Full Disk Access on its own and refreshes the copy (WAL included)
    every few hours. The CRM app still never holds that permission.

Usage:
    python imessage_import.py                   # import only people in your Contacts
    python imessage_import.py --dry-run          # show what would happen, write nothing
    python imessage_import.py --log-unmatched    # also dump not-in-Contacts handles to CSV
    python imessage_import.py --include-unknown  # also create Unknown(+46…) contacts
    python imessage_import.py --chatdb /path/to/chat.db
"""

import os
import re
import csv
import sys
import glob
import sqlite3
import datetime

import cli_guard
import config  # noqa: F401 — loads .env, and APP_SUPPORT_DIR for snapshot_uri
import db
import archive as arc
import engagement  # NC-204: the one writer of imsg_count

CHAT_DB = os.environ.get("CHAT_DB", os.path.expanduser("~/Library/Messages/chat.db"))
# Where macOS keeps the Contacts store. TWO layouts, and looking for only one of
# them is a bug: the single pattern Sources/*/AddressBook-v22.abcddb misses the
# flat layout (NC-029).
#
# On a Mac with the flat layout, two things can be true at once:
#   ~/Library/Application Support/AddressBook   is a folder
#   .../AddressBook/Sources                    is a folder, but listing it raises
#                                              PermissionError (Operation not permitted)
#   glob(Sources/*/AddressBook-v22.abcddb)     0 matches
#   .../AddressBook/AddressBook-v22.abcddb     is a file
# So the store sits there in the flat layout, and the Sources/ listing can be
# hard-denied by TCC (macOS's privacy permission system) — no consent dialog, no
# error, an empty glob. If load_addressbook() reported neither, every import would
# resolve names against an empty mapping while printing "0 phone/email -> name
# mappings" as if that were a normal result.
ADDRESSBOOK_DIR = os.path.expanduser("~/Library/Application Support/AddressBook")
ADDRESSBOOK_PATTERNS = ("AddressBook-v22.abcddb", "Sources/*/AddressBook-v22.abcddb")
SELF_NAME = os.environ.get("SELF_NAME", "")  # your own name, excluded from counts
HERE = os.path.dirname(os.path.abspath(__file__))
UNMATCHED_CSV = config.data_path("imessage_unmatched.csv")
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))

COPY_HELP = """
macOS protects ~/Library/Messages, so this app can't read chat.db there directly —
and you may not want to give it Full Disk Access. Instead, make your own
copy and point the import at the copy:

  1. Quit the Messages app.
  2. In Finder press Cmd+Shift+G and paste:  ~/Library/Messages/
     (If Finder itself asks for permission, that's Finder — not this app — and it's
      safe to allow. You can revoke it afterward in System Settings.)
  3. Copy  chat.db  to your Desktop.
  4. In the app's Import panel, pick the copy (~/Desktop/chat.db), or run:
        python imessage_import.py --chatdb ~/Desktop/chat.db
"""


def _diagnose(path: str, err: Exception) -> str:
    """Turn a sqlite open failure into a specific, actionable message — never the
    old 'grant Full Disk Access to this app' advice."""
    msg = str(err).lower()
    real = os.path.realpath(os.path.expanduser(path))
    in_protected = real.startswith(os.path.expanduser("~/Library/Messages"))
    if "authorization denied" in msg or in_protected:
        return ("That file is inside macOS's protected Messages folder, so it can't "
                "be read directly.\n" + COPY_HELP)
    if "unable to open database file" in msg:
        return (f"macOS is blocking access to '{path}' — Terminal doesn't have "
                "permission to read files in that folder (Desktop and Documents are "
                "protected separately from Full Disk Access).\n\n"
                "Fix: drag chat.db from the Desktop into your home folder in Finder "
                "(click your username in the sidebar), then run:\n"
                "    python imessage_import.py --chatdb ~/chat.db")
    if "no such table" in msg:
        return (f"Opened '{path}', but it has no Messages tables — that doesn't look "
                "like a chat.db. Make sure you copied the right file.\n" + COPY_HELP)
    if "not a database" in msg or "file is encrypted" in msg or "malformed" in msg:
        return (f"'{path}' isn't a readable SQLite database — the copy may be "
                "incomplete. Re-copy chat.db (let it finish) and try again.\n" + COPY_HELP)
    if "locked" in msg or "busy" in msg:
        return (f"'{path}' is locked — quit Messages (and anything using it), then "
                "re-copy chat.db and retry.\n" + COPY_HELP)
    return f"Couldn't open '{path}': {err}\n" + COPY_HELP


# Other applications' live data lives under here. Ours does too
# (config.APP_SUPPORT_DIR), so snapshot_uri() has to tell the two apart. Named
# rather than inlined so tests can repoint it at a temp tree.
LIBRARY_ROOT = os.path.expanduser("~/Library")


def snapshot_uri(db_path: str) -> str:
    """SQLite URI for reading one of *our own* copied chat.db snapshots.

    Two flags can read a database nobody else should be disturbed by, and the
    choice between them is not cosmetic — it decides whether the newest messages
    are visible and whether the SSD gets written to.

      * `immutable=1` promises SQLite the file cannot change, so it skips locking
        *and ignores `-wal` entirely*. On a snapshot whose newest messages are
        still only in the log, that silently returns stale data — none of the
        brand-new rows are visible.
      * `mode=ro` reads main file and `-wal` together (every new row is visible)
        and writes nothing to the main file. It needs the wal-index, so it creates a 32 KB
        `-shm` if the copy didn't include one — but the sync helper copies all
        three parts, so normally there is no write at all.

    `mode=ro` cannot open a WAL-mode database with no `-wal` beside it, which is
    exactly what a one-file Finder copy is. So: sidecar present → `mode=ro`;
    no sidecar → `immutable=1`, where there is no log to miss anyway.

    This replaced `importers.fold_wal()`, which got the newest messages by
    running `PRAGMA wal_checkpoint(TRUNCATE)` on the copy. That worked, but
    writing scattered pages *into an APFS clone* breaks copy-on-write block by
    block: a few MB of log became several times that in real writes, every sync
    cycle. At a 2-minute cadence that adds up to gigabytes a day. Reading the log
    instead of folding it costs nothing, which is what makes the fast cadence
    affordable.

    Never point this at ~/Library/Messages itself, or at WhatsApp's Group
    Container: those are other applications' live files, read in place, and
    `mode=ro` would create an `-shm` inside them. Copies only.
    """
    # Enforce "copies only" here rather than trusting every caller: the CLI's
    # own default --chatdb IS ~/Library/Messages/chat.db, so a plain has-wal test
    # would ask SQLite to create an -shm inside Messages' live folder. TCC blocks
    # that today, which makes it a silent trap rather than a safe one.
    #
    # ~/Library is where other applications keep live data — but it is also where
    # OUR OWN snapshots live (config.APP_SUPPORT_DIR, i.e. ~/Library/Application
    # Support/NetworkCRM/imessage/chat.db). So the test is "somebody else's
    # Library folder", not "Library". Getting this wrong is not loud: a blanket
    # ~/Library deny compiles, passes, and silently drops the newest messages
    # on the one path that matters.
    try:
        real = os.path.realpath(db_path)
    except OSError:
        real = db_path
    ours = os.path.realpath(config.APP_SUPPORT_DIR) + os.sep
    library = os.path.realpath(LIBRARY_ROOT) + os.sep
    if real.startswith(library) and not real.startswith(ours):
        return f"file:{db_path}?immutable=1"
    try:
        has_wal = os.path.getsize(db_path + "-wal") > 0
    except OSError:
        has_wal = False
    return f"file:{db_path}?mode=ro" if has_wal else f"file:{db_path}?immutable=1"


# ── Conversions ─────────────────────────────────────────────────────────────
def apple_date_to_iso(raw) -> str:
    """chat.db dates are nanoseconds since 2001-01-01 (older macOS: seconds)."""
    if not raw:
        return ""
    secs = raw / 1e9 if raw > 1e11 else raw
    dt = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=secs)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_phone(s: str) -> str:
    """Last 9 digits — robust to +46 / 0 prefixes and spacing within a country."""
    d = re.sub(r"\D", "", s or "")
    return d[-9:] if len(d) >= 9 else d


def is_email(handle: str) -> bool:
    return "@" in (handle or "")


# ── AddressBook (handle → name) ─────────────────────────────────────────────
def addressbook_paths(root: str = "") -> list:
    """Every Contacts store under `root`, both layouts, newest first.

    Kept separate from load_addressbook() so the reason for an empty result can be
    reported without opening anything — see addressbook_reason()."""
    base = root or ADDRESSBOOK_DIR
    hits = []
    for pat in ADDRESSBOOK_PATTERNS:
        hits += glob.glob(os.path.join(base, pat))
    return sorted(set(hits), key=lambda p: -os.path.getmtime(p)) if hits else []


_DENIED_HELP = ("Install the sync helper, which holds the permission, or import a "
                ".vcf export instead")


def addressbook_reason(root: str = "", found: int = -1, opened: int = -1) -> str:
    """Why the mapping is empty, in words — or "" when it is not empty.

    An empty mapping had exactly one presentation, the line "0 phone/email → name
    mappings", and it covered four completely different situations: no Contacts
    data on this Mac, a store macOS refuses to let us list, a store we can see but
    not open, and a store that opened and is genuinely empty. Two of them are
    fixable by the user; the "can see, cannot open" one is the one a TCC-protected
    Contacts folder produces.

    stat() on a TCC-protected directory is permitted while opendir() is denied —
    that asymmetry is the only reason these are distinguishable at all.

    `found`/`opened` come from load_addressbook_report(): how many stores were
    located and how many actually read. Pass them, or this cannot tell "no store"
    apart from "a store we were refused".
    """
    base = root or ADDRESSBOOK_DIR
    if found < 0:
        found = len(addressbook_paths(base))
    if found and opened == 0:
        return f"macOS blocked us from opening the Contacts store. {_DENIED_HELP}"
    if found:
        return "your macOS Contacts have no phone numbers or email addresses on them"
    if not os.path.isdir(base):
        return "no macOS Contacts data on this Mac"
    sources = os.path.join(base, "Sources")
    if os.path.isdir(sources):
        try:
            os.listdir(sources)
        except OSError as exc:
            return (f"macOS blocked us from reading the Contacts store "
                    f"({exc.strerror or exc}). {_DENIED_HELP}")
    return "the Contacts folder is there but holds no address book file"


def load_addressbook_report(root: str = "") -> tuple:
    """(mapping, reason) — the mapping, plus why it is empty when it is.

    load_addressbook() is the same thing without the reason, kept because two
    importers and their tests call it that way."""
    mapping: dict = {}
    paths = addressbook_paths(root)
    opened = 0
    for path in paths:
        try:
            c = sqlite3.connect(f"file:{path}?immutable=1&nolock=1", uri=True)
            names = {}
            for r in c.execute("SELECT Z_PK, ZFIRSTNAME, ZLASTNAME FROM ZABCDRECORD"):
                full = " ".join(x for x in (r[1], r[2]) if x).strip()
                if full:
                    names[r[0]] = full
            for r in c.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                if r[0] in names and r[1]:
                    mapping[normalize_phone(r[1])] = names[r[0]]
            for r in c.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if r[0] in names and r[1]:
                    mapping[r[1].strip().lower()] = names[r[0]]
            c.close()
            opened += 1
        except Exception as exc:
            print(f"  (AddressBook source skipped: {exc})")
    reason = "" if mapping else addressbook_reason(root, len(paths), opened)
    return mapping, reason


def load_addressbook(root: str = "") -> dict:
    """Build {normalized_phone|email: 'First Last'} from macOS Contacts. Optional —
    returns {} if Contacts isn't present/accessible.

    `root` overrides where to look, so a copy of the store (the one the sync
    helper makes) can be read instead of the protected original."""
    return load_addressbook_report(root)[0]


def resolve_name(handle: str, ab: dict) -> str:
    if is_email(handle):
        return ab.get(handle.strip().lower(), "")
    return ab.get(normalize_phone(handle), "")


# ── chat.db aggregation ─────────────────────────────────────────────────────
def aggregate_handles(chatdb_path: str) -> dict:
    """{handle_id: {'count', 'first', 'last'}} over 1-to-1 chats only.

    Tapbacks/reactions ("Liked", "Loved", …) are excluded so counts reflect real
    messages, not emoji reactions. Group chats are excluded too (1:1 only)."""
    conn = sqlite3.connect(snapshot_uri(chatdb_path), uri=True)
    rows = conn.execute(
        """
        SELECT h.id AS handle,
               COUNT(*) AS cnt,
               MIN(m.date) AS first_raw,
               MAX(m.date) AS last_raw
        FROM chat_handle_join chj
        JOIN (
            SELECT chat_id FROM chat_handle_join GROUP BY chat_id HAVING COUNT(*) = 1
        ) solo ON solo.chat_id = chj.chat_id
        JOIN chat_message_join cmj ON cmj.chat_id = chj.chat_id
        JOIN message m            ON m.ROWID = cmj.message_id
        JOIN handle h             ON h.ROWID = chj.handle_id
        WHERE COALESCE(m.associated_message_type, 0) = 0  -- exclude tapbacks/reactions
        GROUP BY h.id
        """
    ).fetchall()
    conn.close()
    out = {}
    for handle, cnt, first_raw, last_raw in rows:
        out[handle] = {
            "count": cnt,
            "first": apple_date_to_iso(first_raw),
            "last": apple_date_to_iso(last_raw),
        }
    return out


# ── Apply to crm.db ─────────────────────────────────────────────────────────
def apply(handles: dict, ab: dict, *, include_unknown: bool, dry_run: bool,
          log_unmatched: bool = False) -> tuple:
    conn = db.get_db()
    db.ensure_columns(conn)
    # NC-204. No message count is written here. `imsg_count` is the number of
    # messages the app can show, including the person's lines in group chats, so
    # engagement.recount_channels counts it from archive.db once the messages are
    # stored. This used to zero everyone and add Apple's one-to-one totals back,
    # which count photos and leave out groups: some cards disagreed with their own
    # timeline.
    self_norm = db.normalize_name(SELF_NAME)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    stats = {"matched": 0, "created": 0, "unknown": 0, "excluded": 0}
    unmatched = []
    handle_to_cid: dict = {}

    for handle, agg in handles.items():
        kind = "email" if is_email(handle) else "phone"
        cid = None

        # 1. Direct identifier match against CRM (works when Contacts have been
        #    imported via contacts_import.py — no macOS Contacts permission needed)
        direct = db.find_by_handle(conn, handle)
        if direct:
            name = direct["name"]
            if db.normalize_name(name) in exclude_norms:
                stats["excluded"] += 1
                continue
            cid = direct["id"]
            stats["matched"] += 1
        else:
            # 2. Fall back to AddressBook name resolution (requires macOS Contacts
            #    permission for Terminal — works if ab is non-empty)
            name = resolve_name(handle, ab)

            # skip people you've chosen never to index
            if name and db.normalize_name(name) in exclude_norms:
                stats["excluded"] += 1
                continue

            if name and db.normalize_name(name) != self_norm:
                existing = db.find_exact(conn, name)
                if existing:
                    cid = existing[0]["id"]
                    stats["matched"] += 1
                else:
                    cid = None if dry_run else db.create_contact(conn, name, source="imessage")
                    stats["created"] += 1
            elif include_unknown and not name:
                label = f"Unknown ({handle})"
                existing = db.find_exact(conn, label)
                cid = (existing[0]["id"] if existing
                       else (None if dry_run else db.create_contact(conn, label, source="imessage")))
                stats["unknown"] += 1
            else:
                unmatched.append((handle, agg["count"], agg["last"]))
                continue

        if cid and not dry_run:
            handle_to_cid[handle] = cid
            db.add_identifier(conn, cid, kind, handle, source="message_scan")

    if not dry_run:
        conn.commit()
    conn.close()

    # By default we don't even log people who aren't in your Contacts (numbers
    # you never saved, one-off senders) — they're skipped entirely. Pass --log-unmatched
    # if you ever want to review them.
    if unmatched and log_unmatched and not dry_run:
        with open(UNMATCHED_CSV, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["handle", "message_count", "last_contact"])
            w.writerows(sorted(unmatched, key=lambda x: -x[1]))
        stats["csv_written"] = True
    stats["not_in_contacts"] = len(unmatched)
    return stats, handle_to_cid


def group_max_members() -> int:
    """How big an iMessage group chat may be and still get imported (member
    count excluding you). Change it by setting IMSG_GROUP_MAX in secrets/.env
    (NC-185: it has had no field since NC-003); 0 disables."""
    try:
        return max(0, int(os.environ.get("IMSG_GROUP_MAX", "8") or 0))
    except ValueError:
        return 8


def match_group_handles(chatdb_path: str, handle_to_cid: dict, ab: dict,
                        max_members: int) -> dict:
    """Handles that appear ONLY in qualifying group chats (2..max_members other
    participants), matched against people already in the CRM.

    Match-only, on purpose: a group can contain anyone — matching by existing
    identifier or Contacts name is safe, but creating contacts from group
    membership would pollute the CRM with people you've never actually talked
    to. Returns {handle: contact_id} for the new matches (existing 1:1 matches
    are never touched)."""
    if max_members < 2:
        return {}
    conn_im = sqlite3.connect(snapshot_uri(chatdb_path), uri=True)
    rows = conn_im.execute(
        """
        SELECT DISTINCT h.id AS handle
        FROM chat_handle_join chj
        JOIN (
            SELECT chat_id FROM chat_handle_join
            GROUP BY chat_id HAVING COUNT(*) BETWEEN 2 AND ?
        ) grp ON grp.chat_id = chj.chat_id
        JOIN handle h ON h.ROWID = chj.handle_id
        """,
        (max_members,),
    ).fetchall()
    conn_im.close()

    conn = db.get_db()
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    self_norm = db.normalize_name(SELF_NAME)
    out: dict = {}
    for (handle,) in rows:
        if handle in handle_to_cid:
            continue
        direct = db.find_by_handle(conn, handle)
        if direct:
            if db.normalize_name(direct["name"]) in exclude_norms:
                continue
            out[handle] = direct["id"]
            continue
        name = resolve_name(handle, ab)
        if not name or db.normalize_name(name) == self_norm:
            continue
        if db.normalize_name(name) in exclude_norms:
            continue
        existing = db.find_exact(conn, name)
        if existing:
            out[handle] = existing[0]["id"]
            kind = "email" if is_email(handle) else "phone"
            db.add_identifier(conn, existing[0]["id"], kind, handle, source="message_scan")
    conn.commit()
    conn.close()
    return out


def _decode_typedstream_string(data: bytes) -> str:
    """Pull the message text out of a macOS `streamtyped` (NSArchiver) blob.

    macOS 13+ stores attributedBody as an NSAttributedString serialized with the
    old typedstream format (magic `\\x04\\x0bstreamtyped`), which plistlib can't
    read. The plain text is the first C-string that follows the `NSString` class
    marker: after `NSString` comes a `+` (0x2b) type byte, then a length prefix
    (one byte, or 0x81 + uint16-LE, or 0x82 + uint32-LE), then the UTF-8 bytes.
    """
    marker = data.find(b"NSString")
    if marker == -1:
        return ""
    plus = data.find(b"\x2b", marker)  # '+' begins the char-array payload
    if plus == -1:
        return ""
    i = plus + 1
    if i >= len(data):
        return ""
    prefix = data[i]
    if prefix == 0x81:
        length = int.from_bytes(data[i + 1:i + 3], "little"); i += 3
    elif prefix == 0x82:
        length = int.from_bytes(data[i + 1:i + 5], "little"); i += 5
    else:
        length = prefix; i += 1
    if length <= 0 or i + length > len(data):
        return ""
    return data[i:i + length].decode("utf-8", errors="replace")


def decode_attributed_body(blob) -> str:
    """Extract plain text from an iMessage attributedBody binary blob.

    macOS stores message text in the `attributedBody` column (instead of, or in
    addition to, the plain `text` column) in one of two serializations depending
    on the OS version:
      • bplist / NSKeyedArchiver (older macOS) — parsed with plistlib below.
      • typedstream / NSArchiver (macOS 13+, `streamtyped` magic) — plistlib
        can't read these, so we fall through to _decode_typedstream_string.
    Returns '' if neither yields text.
    """
    import plistlib
    data = bytes(blob)
    try:
        parsed = plistlib.loads(data)
        objs = parsed.get("$objects", []) if isinstance(parsed, dict) else []
        # Most bplist versions wrap the string in a dict with 'NS.string'
        for obj in objs:
            if isinstance(obj, dict) and "NS.string" in obj:
                s = obj["NS.string"]
                if isinstance(s, str):
                    return s
        # Fallback within bplist: longest non-class-name string in $objects
        best = ""
        skip = {"$null"}
        ns_prefixes = ("NS", "UI", "__", "$")
        for obj in objs:
            if isinstance(obj, str) and obj not in skip:
                if not any(obj.startswith(p) for p in ns_prefixes):
                    if len(obj) > len(best):
                        best = obj
        if best:
            return best
    except Exception:
        pass
    # Not a bplist (or bplist had no text) — try the typedstream format.
    return _decode_typedstream_string(data)


def group_key_for(chat_id) -> str:
    """WHICH iMessage conversation this is (NC-110).

    One definition, used by the importer above and the backfill below, because
    the bug being fixed here was two spellings of "the same conversation".
    chat.db's own chat id, which is stable across imports and unique per chat —
    unlike the label, which is generated from the head count when a chat has no
    subject and is therefore shared by every unnamed chat of that size.
    """
    return f"imessage:{chat_id}"


def backfill_group_keys(chatdb_path: str,
                        archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Give already-archived group messages the conversation id they were stored
    without. Returns {"filled", "ambiguous", "unknown"}.

    Exists because many iMessage group messages were archived before
    `group_key` did, and re-running the importer to backfill them is a much
    bigger act than it sounds: it reads macOS Contacts, creates contacts and
    re-derives handle maps. This needs only the chat.db copy and the archive,
    matches rows by the `source_key` they already carry, and **never touches
    contact_id** — so it cannot move a message onto a different person. Rows
    that already have a key are left alone, so running it twice is a no-op.

    A message that somehow belongs to two chats is counted as `ambiguous` and
    skipped rather than assigned to whichever came back first.
    """
    if not os.path.exists(chatdb_path) or not os.path.exists(archive_path):
        return {"filled": 0, "ambiguous": 0, "unknown": 0}

    im_conn = sqlite3.connect(snapshot_uri(chatdb_path), uri=True)
    try:
        rows = im_conn.execute("""
            SELECT cmj.message_id AS pk, cmj.chat_id AS chat_id
            FROM chat_message_join cmj
            JOIN (SELECT chat_id, COUNT(*) AS n
                  FROM chat_handle_join GROUP BY chat_id) cnt
              ON cnt.chat_id = cmj.chat_id
            WHERE cnt.n > 1
        """).fetchall()
    finally:
        im_conn.close()

    chat_of: dict = {}
    ambiguous = set()
    for pk, chat_id in rows:
        key = str(pk)
        if key in chat_of and chat_of[key] != chat_id:
            ambiguous.add(key)
        chat_of.setdefault(key, chat_id)
    for key in ambiguous:
        chat_of.pop(key, None)

    arc_conn = arc.get_archive_db(archive_path)
    try:
        pending = [r["source_key"] for r in arc_conn.execute(
            "SELECT source_key FROM messages "
            "WHERE channel = 'imessage' AND group_name <> '' AND group_key = ''")]
        filled = 0
        for source_key in pending:
            chat_id = chat_of.get(source_key)
            if chat_id is None:
                continue
            arc_conn.execute(
                "UPDATE messages SET group_key = ? WHERE channel = 'imessage' "
                "AND source_key = ? AND group_key = ''",
                (group_key_for(chat_id), source_key))
            filled += 1
        arc_conn.commit()
    finally:
        arc_conn.close()
    # The three numbers partition `pending` exactly — filled + ambiguous +
    # unknown == what was outstanding — so a row that quietly went nowhere
    # cannot hide inside a total that looks healthy.
    amb = len([k for k in pending if k in ambiguous])
    return {"filled": filled, "ambiguous": amb,
            "unknown": len(pending) - filled - amb}


def archive_own_group_messages(chatdb_path: str,
                               archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Store the messages the owner sent to group chats. Returns {"new","skipped"}.

    Every group conversation in the archive was a transcript with the owner's
    half deleted — every iMessage group message was inbound (NC-111). The reason
    was structural rather than deliberate: `archive_messages` attributes a
    message to a contact, an outbound message carries no sender handle, and the
    recipient of a group message is not one person. So its outbound branch is
    restricted to one-to-one chats and these fall through it.

    With NC-110's `group_key` there is somewhere for them to go that is not a
    person: `arc.NO_PERSON`, meaning the row belongs to the conversation.

    SCOPED TO CONVERSATIONS THE ARCHIVE ALREADY HOLDS, deliberately. Widening it
    to every group chat in chat.db would add threads with no known participant
    in them — conversations nobody's page would ever show. Completing what is already here is the job; inventing threads is not.
    The label is read back from the archive rather than re-derived, so the owner's half
    of a conversation cannot end up under a different name from the other half.
    """
    if not os.path.exists(chatdb_path) or not os.path.exists(archive_path):
        return {"new": 0, "skipped": 0}

    arc_conn = arc.get_archive_db(archive_path)
    try:
        known = {}
        for r in arc_conn.execute(
                "SELECT group_key, group_name FROM messages "
                "WHERE channel = 'imessage' AND group_key <> '' "
                "GROUP BY group_key, group_name"):
            known.setdefault(r["group_key"], r["group_name"])
    finally:
        arc_conn.close()

    chats = {}
    for key, label in known.items():
        try:
            chats[int(key.split(":", 1)[1])] = label
        except (IndexError, ValueError):
            continue
    if not chats:
        return {"new": 0, "skipped": 0}

    im_conn = sqlite3.connect(snapshot_uri(chatdb_path), uri=True)
    im_conn.row_factory = sqlite3.Row
    ph = ",".join("?" * len(chats))
    try:
        rows = im_conn.execute(f"""
            SELECT m.ROWID AS pk, m.date AS date_raw, m.text,
                   m.attributedBody, cmj.chat_id AS chat_id
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE cmj.chat_id IN ({ph})
              AND COALESCE(m.associated_message_type, 0) = 0
              AND COALESCE(m.is_from_me, 0) = 1
            ORDER BY m.date
        """, sorted(chats)).fetchall()
    finally:
        im_conn.close()

    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)
    new = skipped = 0
    try:
        for row in rows:
            body = row["text"] or ""
            if not body and row["attributedBody"]:
                body = decode_attributed_body(row["attributedBody"])
            body = body.strip()
            if not body:
                continue
            inserted = arc.insert_message(
                arc_conn,
                contact_id=arc.NO_PERSON,
                channel="imessage",
                direction=1,
                ts=apple_date_to_iso(row["date_raw"]),
                body=body,
                source_key=str(row["pk"]),
                group_name=chats[row["chat_id"]],
                group_key=group_key_for(row["chat_id"]),
            )
            if inserted:
                new += 1
            else:
                skipped += 1
        arc_conn.commit()
    finally:
        arc_conn.close()
    return {"new": new, "skipped": skipped}


def archive_and_chunk(chatdb_path: str, handle_to_cid: dict,
                      max_group_members: int | None = None,
                      archive_path: str | None = None) -> dict:
    """Every archive step an import runs, in order, then the search chunks.

    NC-199. NC-111 wrote `archive_own_group_messages` but nothing called it after
    a one-off run, so the daily import saved everyone else's half of a group
    conversation and not the owner's, and a fresh install never had their half at
    all. It runs here, AFTER the other step, because it only completes
    conversations the archive already holds.

    The owner's messages belong to no person (`arc.NO_PERSON`), so no contact id an
    import touched ever rebuilt their chunks: that id is added whenever any of
    them arrived. Returns {"new", "skipped", "own", "chunks"}.
    """
    path = archive_path or arc.ARCHIVE_DB_PATH
    got = archive_messages(chatdb_path, handle_to_cid, archive_path=path,
                           max_group_members=max_group_members)
    own = archive_own_group_messages(chatdb_path, archive_path=path)
    out = {"new": got["new"] + own["new"], "skipped": got["skipped"] + own["skipped"],
           "own": own["new"], "chunks": 0}
    if out["new"]:
        # NC-222: only the people given a new message. Everyone the import
        # matched used to be rebuilt, which cost a large amount of writes for one
        # message on a large archive.
        cids = set(got.get("cids") or ()) | ({arc.NO_PERSON} if own["new"] else set())
        arc_conn = arc.get_archive_db(path)
        try:
            out["chunks"] = arc.build_chunks(arc_conn, contact_ids=cids)
        finally:
            arc_conn.close()
    return out


def archive_messages(chatdb_path: str, handle_to_cid: dict,
                     archive_path: str = arc.ARCHIVE_DB_PATH,
                     max_group_members: int | None = None) -> dict:
    """Read message text from the chat.db copy and write to archive.db.

    Includes:
      • 1:1 chats: both directions.
      • group chats with 2..max_group_members other participants (a friends'
        chat, small business groups — IMSG_GROUP_MAX, a plain env var since NC-003
        took it out of the in-app config screen): inbound messages
        from known handles, labeled with the chat's name so timelines/search
        can tell group talk from DMs. Your own group replies are skipped —
        there's no honest way to attribute a group reply to one person.

    Tries the plain `text` column first; falls back to decoding `attributedBody`
    for messages where text is NULL (the common case on macOS 14+). Skips
    tapbacks (associated_message_type ≠ 0) and empty bodies. Returns {"new", "skipped"}.
    """
    if not handle_to_cid:
        return {"new": 0, "skipped": 0}
    if max_group_members is None:
        max_group_members = group_max_members()

    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)
    im_conn = sqlite3.connect(snapshot_uri(chatdb_path), uri=True)
    im_conn.row_factory = sqlite3.Row

    keys = list(handle_to_cid.keys())
    ph = ",".join("?" * len(keys))
    # group names live in chat.display_name; some minimal/old chat.dbs lack the
    # column (or table), so probe and fall back to unnamed groups
    chat_cols = {r[1] for r in im_conn.execute("PRAGMA table_info(chat)")}
    if "display_name" in chat_cols:
        name_expr = "COALESCE(c.display_name, '')"
        chat_join = "JOIN chat c   ON c.ROWID = cmj.chat_id"
    else:
        name_expr = "''"
        chat_join = ""
    rows = im_conn.execute(
        f"""
        -- Inbound: known handles in 1:1 chats and in qualifying group chats.
        -- cnt.n = number of OTHER participants.
        SELECT m.ROWID     AS pk,
               h.id        AS handle,
               m.date      AS date_raw,
               0           AS from_me,
               m.text,
               m.attributedBody,
               cnt.n       AS others,
               cmj.chat_id AS chat_id,
               {name_expr} AS chat_name
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN (
            SELECT chat_id, COUNT(*) AS n FROM chat_handle_join GROUP BY chat_id
        ) cnt ON cnt.chat_id = cmj.chat_id
        {chat_join}
        JOIN handle h ON h.ROWID = m.handle_id
        WHERE h.id IN ({ph})
          AND (cnt.n = 1 OR cnt.n BETWEEN 2 AND ?)
          AND COALESCE(m.associated_message_type, 0) = 0
          AND COALESCE(m.is_from_me, 0) = 0

        UNION ALL

        -- Outbound: 1:1 chats only — associate reply with the single recipient handle
        SELECT m.ROWID     AS pk,
               h.id        AS handle,
               m.date      AS date_raw,
               1           AS from_me,
               m.text,
               m.attributedBody,
               1           AS others,
               cmj.chat_id AS chat_id,
               ''          AS chat_name
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN (
            SELECT chat_id FROM chat_handle_join
            GROUP BY chat_id HAVING COUNT(*) = 1
        ) solo ON solo.chat_id = cmj.chat_id
        JOIN chat_handle_join chj ON chj.chat_id = cmj.chat_id
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE h.id IN ({ph})
          AND COALESCE(m.associated_message_type, 0) = 0
          AND COALESCE(m.is_from_me, 0) = 1

        ORDER BY date_raw
        """,
        keys + [max_group_members] + keys,
    ).fetchall()
    im_conn.close()

    new = skipped = 0
    touched: set = set()   # the people given a new message (NC-222)
    for row in rows:
        body = row["text"] or ""
        if not body and row["attributedBody"]:
            body = decode_attributed_body(row["attributedBody"])
        body = body.strip()
        if not body:
            continue
        cid = handle_to_cid.get(row["handle"])
        if cid is None:
            continue
        group = group_key = ""
        if row["others"] > 1:
            group = (row["chat_name"] or "").strip() or f"Group of {row['others'] + 1}"
            # NC-110. The label above is generated from the head count when a
            # chat has no subject, so it is the same string for every unnamed
            # chat of that size — "Group of 3" would be many people's
            # conversations under one name. chat.db's own chat id is what actually says which
            # conversation this is, and it was already in the query.
            group_key = group_key_for(row["chat_id"])
        ts = apple_date_to_iso(row["date_raw"])
        inserted = arc.insert_message(
            arc_conn,
            contact_id=cid,
            channel="imessage",
            direction=int(row["from_me"]),
            ts=ts,
            body=body,
            source_key=str(row["pk"]),
            group_name=group,
            group_key=group_key,
        )
        if inserted:
            new += 1
            touched.add(cid)
        else:
            skipped += 1

    arc_conn.commit()
    arc_conn.close()
    return {"new": new, "skipped": skipped, "cids": touched}


USAGE = """Usage: imessage_import.py [--chatdb PATH] [--dry-run] [--include-unknown] [--log-unmatched]

Imports iMessage history from a copy of ~/Library/Messages/chat.db.
  --chatdb PATH      the copy to read (default: the sync helper's copy)
  --dry-run          count what would change and write nothing
  --include-unknown  also add people who are not in the CRM yet
  --log-unmatched    list the numbers and addresses that matched nobody"""


def main():
    cli_guard.check(sys.argv[1:], USAGE, valued=("--chatdb",),
                    flags=("--dry-run", "--include-unknown", "--log-unmatched"))
    chatdb = CHAT_DB
    if "--chatdb" in sys.argv:
        chatdb = sys.argv[sys.argv.index("--chatdb") + 1]
    include_unknown = "--include-unknown" in sys.argv
    dry_run = "--dry-run" in sys.argv
    log_unmatched = "--log-unmatched" in sys.argv

    if not os.path.exists(chatdb):
        raise SystemExit(f"chat.db not found at: {chatdb}\n" + COPY_HELP)
    try:
        probe = sqlite3.connect(snapshot_uri(chatdb), uri=True)
        total = probe.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        probe.close()
    except sqlite3.DatabaseError as exc:
        raise SystemExit(_diagnose(chatdb, exc))
    print(f"Opened chat.db copy: {total:,} messages on file.")

    db.init_db()
    print("Reading macOS Contacts for name resolution…")
    # Say WHY when there is nothing, instead of printing a bare 0 that reads like
    # "your Contacts are empty". See addressbook_reason().
    ab, why = load_addressbook_report()
    print(f"  {len(ab)} phone/email → name mappings"
          + (f" — {why}" if why else ""))
    print("Aggregating 1-to-1 iMessage threads…")
    handles = aggregate_handles(chatdb)
    print(f"  {len(handles)} handles with direct conversations")

    stats, handle_to_cid = apply(handles, ab, include_unknown=include_unknown, dry_run=dry_run,
                                  log_unmatched=log_unmatched)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"\n{tag}Matched to existing contacts: {stats['matched']}\n"
        f"{tag}New contacts created:        {stats['created']}\n"
        f"{tag}Unknown handles:             {stats.get('unknown', 0)}\n"
        f"{tag}Skipped (exclude_list):      {stats.get('excluded', 0)}\n"
        f"{tag}Not in Contacts (ignored):   {stats['not_in_contacts']}"
    )
    if stats.get("csv_written"):
        print(f"\nWrote {os.path.basename(UNMATCHED_CSV)} (--log-unmatched).")
    if not dry_run and handle_to_cid:
        max_grp = group_max_members()
        if max_grp >= 2:
            grp_matches = match_group_handles(chatdb, handle_to_cid, ab, max_grp)
            if grp_matches:
                print(f"\nMatched {len(grp_matches)} more people from group chats "
                      f"(≤{max_grp} members, existing contacts only).")
            handle_to_cid = {**handle_to_cid, **grp_matches}
        print("\nArchiving message content…")
        arc_stats = archive_and_chunk(chatdb, handle_to_cid, max_group_members=max_grp)
        print(f"  {arc_stats['new']:,} new messages archived  "
              f"({arc_stats['skipped']:,} already stored)")
        if arc_stats["own"]:
            print(f"  {arc_stats['own']:,} of them yours, sent to group chats")
        if arc_stats["new"]:
            print(f"  {arc_stats['chunks']:,} conversation chunks ready for search")
    if not dry_run:
        # NC-204. The app recounts after every import it runs; this is for an
        # import run by hand, so its counts are right before the app next starts.
        conn = db.get_db()
        try:
            engagement.refresh(conn)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
