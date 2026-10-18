"""
Import your WhatsApp history into crm.db — runs entirely on your Mac.

WhatsApp Desktop stores everything in a local Core Data SQLite file,
ChatStorage.sqlite, structured much like iMessage's chat.db:

  • ZWACHATSESSION — one row per chat (ZSESSIONTYPE 0 = individual, ZPARTNERNAME
    is the contact's display name, ZCONTACTJID is "<number>@s.whatsapp.net")
  • ZWAMESSAGE    — the messages (ZMESSAGEDATE = seconds since 2001, ZCHATSESSION
    links to the chat)

For each 1-to-1 chat it counts messages and the last date, resolves the person to
a name (WhatsApp already stores the saved contact name in ZPARTNERNAME), then
matches an existing contact or creates a new one — same rules as the iMessage
import:
  • only people in your Contacts are imported (a chat that shows only a raw phone
    number = not saved = ignored, like a random number),
  • names in exclude_list.txt are skipped,
  • small group chats (≤ WA_GROUP_MAX members, default 15) are archived too:
    inbound messages from known people, labeled with the group's name. Group
    membership alone never creates a contact.

Reuses the iMessage helpers so behavior stays identical across channels.

Like iMessage, WhatsApp's database sits in a protected folder, so you copy
ChatStorage.sqlite somewhere yourself and point --db at the copy (see COPY_HELP).
No app is ever granted Full Disk Access.

Usage:
    python whatsapp_import.py --dry-run     # preview
    python whatsapp_import.py               # apply
    python whatsapp_import.py --db /path/to/ChatStorage.sqlite
"""

import os
import re
import sys
import glob
import sqlite3

import cli_guard
import config  # noqa: F401 — loads .env
import db
import archive as arc
import engagement  # NC-092: the one writer of wa_count
from imessage_import import (
    apple_date_to_iso, load_addressbook_report, resolve_name,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SELF_NAME = os.environ.get("SELF_NAME", "")
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))

# WhatsApp Desktop keeps ChatStorage.sqlite in a Group Container; path varies a bit
# by install (App Store vs standalone), so try the known locations.
#
# Our own import folder comes FIRST, and the order is the point. Everything below
# it is inside a macOS-protected container, so merely globbing for it can raise a
# consent dialog; the first entry is a copy the sync helper wrote into the app's
# own folder, where nothing prompts. When the helper is installed the protected
# paths are never reached at all.
WA_GLOBS = [
    "~/Library/Application Support/NetworkCRM/whatsapp/ChatStorage.sqlite",
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
    "~/Library/Group Containers/*.whatsapp.*/ChatStorage.sqlite",
    "~/Library/Containers/*WhatsApp*/Data/Library/Application Support/*/ChatStorage.sqlite",
]

COPY_HELP = """
macOS protects WhatsApp's database, so this app can't read it in place — and you
don't want to give it Full Disk Access. Instead, copy it yourself and point the
import at the copy:

  1. Quit WhatsApp Desktop.
  2. In Finder press Cmd+Shift+G and paste:
        ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/
     (If Finder asks for permission, that's Finder — not this app — and it's safe
      to allow.)
  3. Copy  ChatStorage.sqlite  to your Desktop.
  4. In the app's Import panel, pick the copy, or run:
        python whatsapp_import.py --db ~/Desktop/ChatStorage.sqlite

If you can't find the folder, WhatsApp Desktop may not be installed/signed in on
this Mac.
"""


def _diagnose(path: str, err: Exception) -> str:
    msg = str(err).lower()
    real = os.path.realpath(os.path.expanduser(path))
    in_protected = "group containers" in real.lower() or "/library/" in real.lower()
    if "authorization denied" in msg or in_protected:
        return ("That file is in a protected folder, so it can't be read directly.\n"
                + COPY_HELP)
    if "no such table" in msg:
        return (f"Opened '{path}', but it has no WhatsApp tables — that doesn't look "
                "like ChatStorage.sqlite.\n" + COPY_HELP)
    if "not a database" in msg or "file is encrypted" in msg or "malformed" in msg:
        return (f"'{path}' isn't a readable SQLite database — the copy may be "
                "incomplete. Re-copy it and try again.\n" + COPY_HELP)
    if "locked" in msg or "busy" in msg:
        return (f"'{path}' is locked — quit WhatsApp, re-copy, and retry.\n" + COPY_HELP)
    return f"Couldn't open '{path}': {err}\n" + COPY_HELP


def find_db() -> str | None:
    for pattern in WA_GLOBS:
        hits = glob.glob(os.path.expanduser(pattern))
        if hits:
            return hits[0]
    return None


def has_alpha(s: str) -> bool:
    return any(c.isalpha() for c in (s or ""))


def aggregate_sessions(path: str) -> list[dict]:
    """One entry per 1-to-1 chat: {jid, name, count, first, last}."""
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    rows = conn.execute(
        """
        SELECT s.ZCONTACTJID AS jid,
               s.ZPARTNERNAME AS name,
               COUNT(*)              AS cnt,
               MIN(m.ZMESSAGEDATE)   AS first_raw,
               MAX(m.ZMESSAGEDATE)   AS last_raw
        FROM ZWACHATSESSION s
        JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
        WHERE s.ZSESSIONTYPE = 0                 -- individual chats only
          AND s.ZCONTACTJID NOT LIKE '%@g.us'    -- belt and suspenders: no groups
        GROUP BY s.Z_PK
        """
    ).fetchall()
    conn.close()
    out = []
    for jid, name, cnt, first_raw, last_raw in rows:
        out.append({
            "jid": jid or "",
            "name": name or "",
            "count": cnt,
            "first": apple_date_to_iso(first_raw),
            "last": apple_date_to_iso(last_raw),
        })
    return out


def _by_old_fake_phone(conn, hidden_digits: str):
    """The person an older import filed this hidden id on, as "+<hidden id>".
    Exact match only: find_by_handle compares the last nine digits, which for a
    random id could land on a stranger's real number."""
    return conn.execute(
        "SELECT c.* FROM contacts c JOIN identifiers i ON i.contact_id = c.id "
        "WHERE i.kind = 'phone' AND i.value = ? AND i.source = 'message_scan' "
        "AND NOT c.excluded LIMIT 1", ("+" + hidden_digits,)).fetchone()


def _drop_old_fake_phone(conn, cid: int, hidden_digits: str) -> int:
    """Remove the "+<hidden id>" phone an older import stored on this person.
    Only the importer's own row: a number typed by hand is never touched
    (invariant 4), even in the unlikely case it has the same digits. Returns
    how many rows went. Nothing is lost: the person is now recognised by the
    whatsapp_id row just written."""
    return conn.execute(
        "DELETE FROM identifiers WHERE contact_id = ? AND kind = 'phone' "
        "AND value = ? AND source = 'message_scan'",
        (cid, "+" + hidden_digits)).rowcount


def apply(sessions: list[dict], ab: dict, *, include_unknown: bool,
          dry_run: bool) -> tuple[dict, dict]:
    """Match WhatsApp sessions to CRM contacts.

    Returns (stats, jid_to_cid) where jid_to_cid maps WhatsApp JID strings
    to contact IDs for every successfully matched/created contact. The caller
    uses this to write message content to archive.db.

    It writes no message count (NC-092). `wa_count` is the number of messages
    the app can show, so engagement.recount_channels counts it from archive.db
    once the messages are stored. This used to add WhatsApp's own total here,
    which counts photos and deleted messages the archive never keeps.
    """
    conn = db.get_db()
    db.ensure_columns(conn)
    self_norm = db.normalize_name(SELF_NAME)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    stats = {"matched": 0, "created": 0, "excluded": 0, "not_in_contacts": 0,
             "fake_phones_removed": 0}
    jid_to_cid: dict[str, int] = {}

    for s in sessions:
        jid_suffix = s["jid"].split("@", 1)[-1]   # "s.whatsapp.net" or "lid"
        jid_prefix = s["jid"].split("@", 1)[0]

        # Resolve the real phone number.
        # @s.whatsapp.net: JID prefix IS the number (e.g. "46700000000").
        # @lid: JID prefix is WhatsApp's hidden id, NOT a phone number. The only
        #       number there is is the one in a formatted ZPARTNERNAME
        #       ("+46 70 000 00 00" → "46700000000"); with a saved name there is
        #       none. Using the prefix would put a phone number that does not
        #       exist on every such person, and give some of them a US city from
        #       its first digits (NC-243).
        hidden = jid_suffix == "lid"
        if hidden and not has_alpha(s["name"]):
            phone = re.sub(r"\D", "", s["name"])   # strip formatting from "+46 70 000 00 00"
        elif hidden:
            phone = ""
        else:
            phone = jid_prefix

        cid = None

        # 0. Someone this hidden id was matched to before. It is what keeps a
        #    re-import on the same person after a merge or a rename, which the
        #    fake phone number used to do by accident.
        direct = None
        if hidden:
            direct = db.find_by_whatsapp_id(conn, s["jid"]) or _by_old_fake_phone(conn, jid_prefix)

        # 1. Direct phone match against CRM identifiers — works without AddressBook,
        #    catches contacts whose phone was stored by a prior iMessage / contacts import,
        #    even when their WhatsApp display name is a raw number instead of a saved name.
        if not direct and phone:
            direct = db.find_by_handle(conn, phone)
        if direct:
            name = direct["name"]
            if db.normalize_name(name) in exclude_norms:
                stats["excluded"] += 1
                continue
            cid = direct["id"]
            stats["matched"] += 1
        else:
            # 2. Fall back to display-name matching.
            # WhatsApp stores the saved contact name in ZPARTNERNAME; if it's a real
            # name the person is in your Contacts. If it's just a number, fall back to
            # an AddressBook lookup by phone (and ignore if still unknown).
            name = s["name"].strip() if has_alpha(s["name"]) else resolve_name(phone, ab)

            if name and db.normalize_name(name) in exclude_norms:
                stats["excluded"] += 1
                continue
            if not name or db.normalize_name(name) == self_norm:
                if include_unknown and phone:
                    name = f"Unknown (+{phone})"
                else:
                    stats["not_in_contacts"] += 1
                    continue

            existing = db.find_exact(conn, name)
            if existing:
                cid = existing[0]["id"]
                stats["matched"] += 1
            else:
                cid = None if dry_run else db.create_contact(conn, name, source="whatsapp")
                stats["created"] += 1

        if cid and not dry_run:
            jid_to_cid[s["jid"]] = cid
            # Store the phone as an identifier so future re-imports match by phone directly.
            if phone.isdigit():
                db.add_identifier(conn, cid, "phone", "+" + phone, source="message_scan")
            if hidden:
                db.add_identifier(conn, cid, "whatsapp_id", s["jid"], source="message_scan")
                stats["fake_phones_removed"] += _drop_old_fake_phone(conn, cid, jid_prefix)

    if not dry_run:
        conn.commit()
    conn.close()
    return stats, jid_to_cid


def archive_messages(wa_path: str, jid_to_cid: dict,
                     archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Read full message text from the WA copy and write to archive.db.

    Only contacts already matched (present in jid_to_cid) are archived.
    INSERT OR IGNORE makes every call idempotent — re-running after a new
    export only stores the genuinely new messages.

    Returns {"new": int, "skipped": int, "cids": the people given new messages}.
    """
    if not jid_to_cid:
        return {"new": 0, "skipped": 0, "cids": set()}

    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)

    wa_conn = sqlite3.connect(f"file:{wa_path}?immutable=1", uri=True)

    # Probe schema: ZISFROMME may be absent in some WA Desktop versions.
    cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWAMESSAGE)")}
    direction_expr = "COALESCE(m.ZISFROMME, 0)" if "ZISFROMME" in cols else "0"
    text_col = "ZTEXT" if "ZTEXT" in cols else "ZFROMJID"   # fallback; usually ZTEXT

    placeholders = ",".join("?" * len(jid_to_cid))
    rows = wa_conn.execute(
        f"""SELECT m.Z_PK            AS pk,
                   s.ZCONTACTJID     AS jid,
                   m.ZMESSAGEDATE    AS date_raw,
                   COALESCE(m.{text_col}, '') AS body,
                   {direction_expr}  AS from_me
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
           WHERE s.ZCONTACTJID IN ({placeholders})
             AND s.ZSESSIONTYPE = 0
             AND COALESCE(m.{text_col}, '') != ''
           ORDER BY m.ZMESSAGEDATE""",
        list(jid_to_cid.keys()),
    ).fetchall()
    wa_conn.close()

    new = skipped = 0
    touched: set = set()
    for row in rows:
        cid = jid_to_cid.get(row[1])   # row[1] = jid
        if not cid:
            continue
        ts = apple_date_to_iso(row[2])  # row[2] = date_raw
        inserted = arc.insert_message(
            arc_conn,
            contact_id=cid,
            channel="whatsapp",
            direction=int(row[4]),      # row[4] = from_me
            ts=ts,
            body=row[3],                # row[3] = body
            source_key=str(row[0]),     # row[0] = pk
        )
        if inserted:
            new += 1
            touched.add(cid)
        else:
            skipped += 1

    arc_conn.commit()
    arc_conn.close()
    return {"new": new, "skipped": skipped, "cids": touched}


def group_max_members() -> int:
    """How big a WhatsApp group may be and still get imported. Settings-managed
    (WA_GROUP_MAX, default 15); 0 disables group import."""
    try:
        return max(0, int(os.environ.get("WA_GROUP_MAX", "15") or 0))
    except ValueError:
        return 15


def match_group_senders(member_jids: list, jid_to_cid: dict) -> dict:
    """Match group members who have no 1:1 chat with you against people
    ALREADY in the CRM (by phone). Match-only — group membership alone never
    creates a contact. Classic <phone>@s.whatsapp.net members match by phone.
    Hidden @lid members match only through the whatsapp_id a one-to-one chat
    stored earlier; without one they stay unmatched. Returns {member_jid: contact_id} for the new matches."""
    out: dict = {}
    conn = db.get_db()
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    for jid in member_jids:
        if not jid or jid in jid_to_cid:
            continue
        phone = jid.split("@", 1)[0]
        if not phone.isdigit():
            continue
        if jid.endswith("@lid"):
            # A hidden id is not a phone number (NC-243); it resolves only to
            # someone a one-to-one chat already tied it to.
            direct = db.find_by_whatsapp_id(conn, jid) or _by_old_fake_phone(conn, phone)
        else:
            direct = db.find_by_handle(conn, phone)
        if direct and db.normalize_name(direct["name"]) not in exclude_norms:
            out[jid] = direct["id"]
    conn.close()
    return out


def group_key_for(session) -> str:
    """WHICH WhatsApp conversation this is (NC-110). See the twin in
    imessage_import for why the label cannot serve as the identity: every
    unnamed group here is called "WhatsApp group"."""
    return f"whatsapp:{session}"


def backfill_group_keys(wa_path: str,
                        archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """The WhatsApp half of the NC-110 backfill — see
    imessage_import.backfill_group_keys for why this exists rather than a
    re-import. Group source keys carry the `wag_` prefix, which is what makes
    them recoverable: the number after it is ZWAMESSAGE.Z_PK.
    """
    if not os.path.exists(wa_path) or not os.path.exists(archive_path):
        return {"filled": 0, "unknown": 0}

    wa_conn = sqlite3.connect(f"file:{wa_path}?immutable=1", uri=True)
    try:
        session_of = {str(pk): sess for pk, sess in wa_conn.execute(
            "SELECT Z_PK, ZCHATSESSION FROM ZWAMESSAGE "
            "WHERE ZGROUPMEMBER IS NOT NULL")}
    except sqlite3.Error:
        return {"filled": 0, "unknown": 0}
    finally:
        wa_conn.close()

    arc_conn = arc.get_archive_db(archive_path)
    try:
        pending = [r["source_key"] for r in arc_conn.execute(
            "SELECT source_key FROM messages "
            "WHERE channel = 'whatsapp' AND group_name <> '' AND group_key = ''")]
        filled = 0
        for source_key in pending:
            sess = session_of.get(source_key[4:]
                                  if source_key.startswith("wag_") else source_key)
            if sess is None:
                continue
            arc_conn.execute(
                "UPDATE messages SET group_key = ? WHERE channel = 'whatsapp' "
                "AND source_key = ? AND group_key = ''",
                (group_key_for(sess), source_key))
            filled += 1
        arc_conn.commit()
    finally:
        arc_conn.close()
    return {"filled": filled, "unknown": len(pending) - filled}


def archive_and_chunk(wa_path: str, jid_to_cid: dict,
                      max_members: int | None = None,
                      archive_path: str | None = None) -> dict:
    """Every archive step an import runs, in order, then the search chunks.

    NC-199 — the same gap as imessage_import.archive_and_chunk, whose docstring
    has the history: `archive_own_group_messages` below has to be called here, or
    the user's own half of every group conversation is never archived. It runs
    last because it only completes conversations the other two steps stored.
    Returns the three steps' stats plus {"new", "skipped", "chunks"}.
    """
    path = archive_path or arc.ARCHIVE_DB_PATH
    one = archive_messages(wa_path, jid_to_cid, archive_path=path)
    grp = archive_group_messages(wa_path, jid_to_cid, max_members=max_members,
                                 archive_path=path)
    own = archive_own_group_messages(wa_path, archive_path=path)
    out = {"one": one, "groups": grp, "own": own,
           "new": one["new"] + grp["new"] + own["new"],
           "skipped": one["skipped"] + grp["skipped"] + own["skipped"],
           "chunks": 0}
    if out["new"]:
        # NC-222: only the people given a new message, as in imessage_import.
        cids = set(one.get("cids") or ()) | set(grp.get("cids") or ())
        if own["new"]:
            cids.add(arc.NO_PERSON)
        arc_conn = arc.get_archive_db(path)
        try:
            out["chunks"] = arc.build_chunks(arc_conn, contact_ids=cids)
        finally:
            arc_conn.close()
    return out


def archive_own_group_messages(wa_path: str,
                               archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Store the messages the owner sent to WhatsApp group chats (NC-111).

    The twin of imessage_import.archive_own_group_messages — see that docstring
    for why these were missing and why `arc.NO_PERSON` is where they go. The
    WhatsApp cause was its own: `archive_group_messages` filters
    `ZGROUPMEMBER IS NOT NULL`, and a message the owner sent has no group-member row
    pointing at anybody else, so it never matched; the insert then hardcoded
    direction=0 for what was left.

    Scoped to conversations the archive already holds. Unscoped it would add many
    more messages in chats where nobody the user knows ever spoke — threads no page
    would show.
    """
    if not os.path.exists(wa_path) or not os.path.exists(archive_path):
        return {"new": 0, "skipped": 0}

    arc_conn = arc.get_archive_db(archive_path)
    try:
        known = {}
        for r in arc_conn.execute(
                "SELECT group_key, group_name FROM messages "
                "WHERE channel = 'whatsapp' AND group_key <> '' "
                "GROUP BY group_key, group_name"):
            known.setdefault(r["group_key"], r["group_name"])
    finally:
        arc_conn.close()

    sessions = {}
    for key, label in known.items():
        try:
            sessions[int(key.split(":", 1)[1])] = label
        except (IndexError, ValueError):
            continue
    if not sessions:
        return {"new": 0, "skipped": 0}

    wa_conn = sqlite3.connect(f"file:{wa_path}?immutable=1", uri=True)
    ph = ",".join("?" * len(sessions))
    try:
        msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWAMESSAGE)")}
        if "ZTEXT" not in msg_cols or "ZISFROMME" not in msg_cols:
            return {"new": 0, "skipped": 0}
        rows = wa_conn.execute(f"""
            SELECT Z_PK AS pk, ZCHATSESSION AS session, ZMESSAGEDATE AS date_raw,
                   ZTEXT AS body
            FROM ZWAMESSAGE
            WHERE ZCHATSESSION IN ({ph})
              AND COALESCE(ZISFROMME, 0) = 1
              AND COALESCE(ZTEXT, '') != ''
            ORDER BY ZMESSAGEDATE
        """, sorted(sessions)).fetchall()
    except sqlite3.Error:
        return {"new": 0, "skipped": 0}
    finally:
        wa_conn.close()

    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)
    new = skipped = 0
    try:
        for pk, session, date_raw, body in rows:
            inserted = arc.insert_message(
                arc_conn,
                contact_id=arc.NO_PERSON,
                channel="whatsapp",
                direction=1,
                ts=apple_date_to_iso(date_raw),
                body=body,
                # Same wag_ prefix as the inbound group path, so group source
                # keys stay distinguishable from one-to-one ones.
                source_key=f"wag_{pk}",
                group_name=sessions[session],
                group_key=group_key_for(session),
            )
            if inserted:
                new += 1
            else:
                skipped += 1
        arc_conn.commit()
    finally:
        arc_conn.close()
    return {"new": new, "skipped": skipped}


def archive_group_messages(wa_path: str, jid_to_cid: dict,
                           max_members: int | None = None,
                           archive_path: str = arc.ARCHIVE_DB_PATH) -> dict:
    """Archive inbound messages sent by known contacts in small group chats
    (≤max_members — WA_GROUP_MAX, a plain env var since NC-003 took it out of
    the in-app config screen), labeled with the group's name.

    Only inbound messages are archived (ZFROMJID = sender JID); your own group
    replies can't honestly be attributed to one person. Senders are the 1:1
    matches plus group senders matched by phone against existing contacts.
    Returns {"new": int, "skipped": int, "cids": the people given new messages}.
    It no longer writes wa_count: engagement.recount_channels does, for everyone
    (NC-092; this recounted only the people it had just touched).
    """
    if not jid_to_cid:
        return {"new": 0, "skipped": 0}
    if max_members is None:
        max_members = group_max_members()
    if max_members < 2:
        return {"new": 0, "skipped": 0}

    wa_conn = sqlite3.connect(f"file:{wa_path}?immutable=1", uri=True)

    # Check that ZWAGROUPMEMBER table exists (some WA Desktop versions may not have it)
    tables = {r[0] for r in wa_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ZWAGROUPMEMBER" not in tables:
        wa_conn.close()
        return {"new": 0, "skipped": 0}

    gm_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWAGROUPMEMBER)")}
    msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWAMESSAGE)")}
    text_col = "ZTEXT" if "ZTEXT" in msg_cols else None
    if not text_col:
        wa_conn.close()
        return {"new": 0, "skipped": 0}

    # Probe active-member filter (ZISACTIVE may be absent in older schemas)
    active_clause = "AND gm.ZISACTIVE = 1" if "ZISACTIVE" in gm_cols else ""

    small_groups = wa_conn.execute(f"""
        SELECT gm.ZCHATSESSION
        FROM ZWAGROUPMEMBER gm
        WHERE 1=1 {active_clause}
        GROUP BY gm.ZCHATSESSION
        HAVING COUNT(*) <= ?
    """, (max_members,)).fetchall()

    if not small_groups:
        wa_conn.close()
        return {"new": 0, "skipped": 0}

    # A group message's sender is NOT ZFROMJID (that's the group's own @g.us
    # JID) — it's the ZGROUPMEMBER foreign key into ZWAGROUPMEMBER, whose
    # ZMEMBERJID matches the sender's 1:1 session JID when you also DM them.
    msg_cols_full = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWAMESSAGE)")}
    if "ZGROUPMEMBER" not in msg_cols_full:
        wa_conn.close()
        return {"new": 0, "skipped": 0}

    group_pks = [r[0] for r in small_groups]
    g_ph = ",".join("?" * len(group_pks))

    # group subject lives on the group's chat session row
    group_names = {r[0]: (r[1] or "").strip() for r in wa_conn.execute(
        f"SELECT Z_PK, ZPARTNERNAME FROM ZWACHATSESSION WHERE Z_PK IN ({g_ph})",
        group_pks)}

    # member pk → member jid, for every member of a qualifying group
    member_jid = {r[0]: r[1] or "" for r in wa_conn.execute(
        f"SELECT Z_PK, ZMEMBERJID FROM ZWAGROUPMEMBER WHERE ZCHATSESSION IN ({g_ph})",
        group_pks)}

    # people who only ever talk to you inside these groups still match by phone
    senders = {**jid_to_cid,
               **match_group_senders(sorted(set(member_jid.values())), jid_to_cid)}

    rows = wa_conn.execute(f"""
        SELECT m.Z_PK AS pk, m.ZGROUPMEMBER AS member_pk, m.ZMESSAGEDATE AS date_raw,
               COALESCE(m.{text_col}, '') AS body, m.ZCHATSESSION AS session
        FROM ZWAMESSAGE m
        WHERE m.ZCHATSESSION IN ({g_ph})
          AND m.ZGROUPMEMBER IS NOT NULL
          AND COALESCE(m.{text_col}, '') != ''
        ORDER BY m.ZMESSAGEDATE
    """, group_pks).fetchall()
    wa_conn.close()

    if not rows:
        return {"new": 0, "skipped": 0}

    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)

    new = skipped = 0
    touched_cids: set = set()

    for row in rows:
        cid = senders.get(member_jid.get(row[1], ""))
        if not cid:
            continue
        ts = apple_date_to_iso(row[2])
        group = group_names.get(row[4]) or "WhatsApp group"
        # NC-110. That fallback label is shared by every unnamed group, so the
        # chat session's own id is what identifies the conversation.
        group_key = group_key_for(row[4])
        # Use 'wag_' prefix so group source keys don't collide with 1:1 message keys
        inserted = arc.insert_message(
            arc_conn,
            contact_id=cid,
            channel="whatsapp",
            direction=0,
            ts=ts,
            body=row[3],
            source_key=f"wag_{row[0]}",
            group_name=group,
            group_key=group_key,
        )
        if inserted:
            new += 1
            touched_cids.add(cid)
        else:
            skipped += 1

    arc_conn.commit()
    arc_conn.close()
    return {"new": new, "skipped": skipped, "cids": touched_cids}


USAGE = """Usage: whatsapp_import.py [--db PATH] [--dry-run] [--include-unknown]

Imports WhatsApp history from a copy of WhatsApp Desktop's ChatStorage.sqlite.
  --db PATH          the copy to read (default: the first one found)
  --dry-run          count what would change and write nothing
  --include-unknown  also add people who are not in the CRM yet"""


def main():
    cli_guard.check(sys.argv[1:], USAGE, valued=("--db",),
                    flags=("--dry-run", "--include-unknown"))
    path = None
    if "--db" in sys.argv:
        path = sys.argv[sys.argv.index("--db") + 1]
    else:
        path = find_db()
    include_unknown = "--include-unknown" in sys.argv
    dry_run = "--dry-run" in sys.argv

    if not path or not os.path.exists(path):
        raise SystemExit("WhatsApp ChatStorage.sqlite not found.\n" + COPY_HELP)
    try:
        probe = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        total = probe.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
        probe.close()
    except sqlite3.DatabaseError as exc:
        raise SystemExit(_diagnose(path, exc))
    print(f"Opened ChatStorage.sqlite copy: {total:,} messages on file.")

    db.init_db()
    print(f"Reading WhatsApp DB: {path}")
    print("Reading macOS Contacts for name resolution…")
    # Same bare-0 problem as the iMessage importer, same fix — this caller was
    # resolving names against an empty mapping just as silently.
    ab, why = load_addressbook_report()
    print(f"  {len(ab)} phone/email → name mappings"
          + (f" — {why}" if why else ""))
    sessions = aggregate_sessions(path)
    print(f"  {len(sessions)} one-to-one WhatsApp chats")

    stats, jid_to_cid = apply(sessions, ab, include_unknown=include_unknown, dry_run=dry_run)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"\n{tag}Matched to existing contacts: {stats['matched']}\n"
        f"{tag}New contacts created:        {stats['created']}\n"
        f"{tag}Skipped (exclude_list):      {stats['excluded']}\n"
        f"{tag}Not in Contacts (ignored):   {stats['not_in_contacts']}"
    )
    if stats["fake_phones_removed"]:
        print(f"Removed {stats['fake_phones_removed']} phone numbers that were really "
              "WhatsApp hidden ids (NC-243)")
    if not dry_run and jid_to_cid:
        max_grp = group_max_members()
        print(f"\nArchiving message content (groups of ≤{max_grp} members)…")
        out = archive_and_chunk(path, jid_to_cid, max_members=max_grp)
        for label, part in (("1-to-1", out["one"]), ("groups", out["groups"]),
                            ("yours in groups", out["own"])):
            print(f"  {label}: {part['new']:,} new  ({part['skipped']:,} already stored)")
        # one canonical, scrapeable total so the result receipt gets an Archived
        # chip — the per-scope lines above stay for the raw log
        print(f"  {out['new']:,} new messages archived  "
              f"({out['skipped']:,} already stored)")
        if out["new"]:
            print(f"  {out['chunks']:,} conversation chunks ready for search")
        # NC-092. The app recounts after every import it runs; this is for an
        # import run by hand, so its counts are right before the app next starts.
        conn = db.get_db()
        try:
            engagement.refresh(conn)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
