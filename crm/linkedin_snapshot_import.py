"""
Recover the LinkedIn messages stranded in an old scraper's frozen snapshot (NC-070).

WHAT WENT WRONG. `linkedin_messages.db` is a snapshot the retired local scraper
wrote and nothing has written to since. Its two readers — `seed_from_linkedin.py`
and `enrich_local.py` — were deleted on the belief that "that history now lives
in archive.db (via linkedin_import.py)". For some of its messages it does not.
LinkedIn's portability API only returns a recent window, so the API import never
fetched the old end of the file, and nothing else ever read it.

WHY IT CAN GO UNNOTICED. `linkedin_import.archive_messages` used to
refuse to lower a contact's `msg_count` so that the scraper's deep history
survives the API's shallow window. That guard preserved the NUMBER. Nothing
preserved the BODIES. Some people ended up showing a message count on their card
with an empty timeline underneath — a card saying "12 messages" while
`/api/contact/<id>/history` returned zero. A signal that proves the wrong thing,
the same shape as NC-035, NC-061 and NC-068.

WHY THIS IS SAFE TO RE-RUN. `archive.db` declares `UNIQUE(channel, source_key)`
and `archive.insert_message` is `INSERT OR IGNORE`. The snapshot's `messages.id`
is *already* the `source_key` the archive uses — both are
`sha1("{conversation_id}|{date_utc}|{sender}|{body}")`, so a snapshot message
that is already archived matches its archived row by id. So this can neither
duplicate a message nor overwrite one, and running it twice is a no-op.

Corrected (NC-107): that guard is gone. The count is now the
messages the app can show, recounted from archive.db for everyone
(engagement.recount_channels), so a number with no bodies behind it cannot
outlive them again.

WHY IT DELEGATES THE WRITE. The insert and the direction rule live in
`linkedin_import.archive_messages`. Rather than reimplement two tested things, this
reshapes each snapshot row into the CSV-shaped dict that function already takes
and hands the mapping to it. The only new logic here is *which contact a message
belongs to*, because the snapshot stores display names where the API gives
profile URLs.

WHY A MESSAGE THAT LOOKS STRANDED MAY NOT BE (NC-071). This file used to report
messages it could not place, on the grounds that their counterparties were "in
the snapshot's own `contacts` table but never imported into `crm.db`". That can
be wrong: such a message may belong to someone who was in `crm.db` all along —
the snapshot can spell a name with a DOUBLE SPACE between
first and last name, and the lookup below compared raw lower-cased strings while
every other name match in the app goes through `db.normalize_name`, which
collapses runs of whitespace. Two spaces against one was the whole of it. The
keys now go through `db.normalize_name` like everything else, which attaches
those messages and creates nobody.

That same mismatch was a live hazard, not just a missed match. `archive_messages`
reads "sender is in the map" as "this message came IN", so the owner has to be
excluded from the map or every message the owner sent is filed as inbound to
their own card. A display name can carry a decoration glyph (LinkedIn's ⟢) on the
snapshot side, so excluding the owner by their SNAPSHOT spelling would leave
their `crm.db` spelling in the map. Normalizing both sides closes that by
construction rather than by luck. Normalized keys also cannot contain a comma, which matters because
`archive_messages` splits its recipient field on one.

IT CREATES A CONTACT (NC-071). The remaining people are added rather than only
reported. These are people whose only record anywhere is the snapshot's own
`contacts` row: a name, a profile URL and a connection date, plus their
messages. Nothing is invented: the
name is the one the scraper captured (its slug says the same), and a person whose
normalized name matches somebody the user has HIDDEN OR DELETED is never created
and never attached, because filing messages onto a card they cannot see is the
exact bug this file exists to fix. Pass --no-create to go back to reporting them.

Bodyless rows are still skipped, for the same reason `archive_messages` skips
them — they are the media and system placeholders, and the existing importer has
always left them out.

Usage:
    python linkedin_snapshot_import.py --dry-run    # report, change nothing
    python linkedin_snapshot_import.py              # recover, creating what is missing
    python linkedin_snapshot_import.py --no-create  # recover, create nobody
"""

import os
import figures  # NC-087: the one spelling of a contact-scope filter
import sqlite3
import sys
import unicodedata
from typing import Any, Dict, List, Optional

import config  # noqa: F401 — loads .env
import archive as arc

SNAPSHOT_PATH = os.environ.get(
    "NC_LI_SNAPSHOT", config.data_path("linkedin_messages.db")
)

# A row whose counterparty we can only identify through the thread gets a
# stand-in key in the name map. It cannot collide with a real display name
# because a name never contains a colon in this position.
CONVO_KEY = "convo:{}"


def _key(name: str) -> str:
    """A name reduced to the form the CRM matches names in.

    Delegated to `db.normalize_name` rather than reimplemented, because a second
    definition of "the same person" is how messages go missing: this file
    lower-cased and stripped, `db` also collapses inner
    whitespace and drops decoration glyphs, and a double space fell in the gap.
    Imported lazily to keep the module importable with no DB configured; after
    the first call it is a `sys.modules` lookup.
    """
    import db
    return db.normalize_name(name or "")


def _display_name(name: str) -> str:
    """The name to STORE for someone being created — not a lookup key.

    Strips the invisible direction and formatting marks LinkedIn display names
    can arrive with (a left-to-right mark can sit where a surname would be) and
    collapses the double spaces described above,
    so a card is not created with characters nobody can see or delete.
    """
    cleaned = "".join(
        ch for ch in unicodedata.normalize("NFC", name or "")
        if unicodedata.category(ch) not in ("Cf", "Cc")
    )
    return " ".join(cleaned.split())


def _open(path: str) -> Optional[sqlite3.Connection]:
    """Read-only handle on the snapshot, or None when it is not there.

    A fresh install has no snapshot and never will — this file only exists on
    machines that ran the old scraper — so absence is a quiet skip, not an error.
    """
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def owner_name(snap: sqlite3.Connection) -> str:
    """Whose account this snapshot is. Every message in a one-to-one thread has
    either the owner or the other person as `sender`, so across many threads
    the most frequent sender is the owner by a margin nothing else can reach,
    against a long tail of distinct senders on the inbound side.

    Derived rather than configured because the snapshot carries no owner field,
    and a wrong answer here would flip every message's direction — so it is
    checked by a test against the direction the archive already assigned.
    """
    row = snap.execute(
        "SELECT sender FROM messages WHERE COALESCE(TRIM(sender),'') <> '' "
        "GROUP BY sender ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    # Positional, not by name: this takes a connection it did not open, and a
    # caller whose row_factory is the sqlite3 default hands back plain tuples.
    return ((row[0] if row else "") or "").strip()


def name_index(crm_conn: Any, owner: str) -> Dict[str, int]:
    """Display name → contact id, for people this CRM actually holds.

    Keyed through `_key`, so the two sides of the comparison agree — see the
    double-space paragraph in the module docstring for what raw string equality
    cost here.

    The owner is excluded on purpose. `archive_messages` reads "sender is in the
    map" as "this message came IN", so leaving the owner in it would mark every
    message they sent as inbound. Names that appear twice are dropped rather than
    resolved arbitrarily — picking one of two
    people to attach a private message to is not a guess worth making.
    """
    seen: Dict[str, int] = {}
    dupes = set()
    owner_key = _key(owner)
    for cid, name in crm_conn.execute(
        f"SELECT id, name FROM contacts WHERE {figures.population_where('visible')} AND {figures.population_where('live')}"
    ):
        key = _key(name)
        if not key or key == owner_key:
            continue
        if key in seen:
            dupes.add(key)
        else:
            seen[key] = cid
    for key in dupes:
        seen.pop(key, None)
    return seen


def thread_index(snap: sqlite3.Connection, archive_path: str) -> Dict[str, int]:
    """conversation_id → contact id, learned from messages that already crossed.

    The archive is the authority here: Many of the snapshot's rows are already
    in it with a contact attached, so for those threads the answer has already
    been decided once and this just reads it back rather than re-deriving it.

    Threads the archive attached to MORE than one contact are dropped. Those are
    group conversations, where "the contact for this
    thread" has no single answer and the name path is the only honest route.
    """
    if not os.path.exists(archive_path):
        return {}
    rows = []
    try:
        snap.execute("ATTACH ? AS a", (archive_path,))
        rows = snap.execute(
            "SELECT x.conversation_id AS cv, m.contact_id AS cid "
            "FROM messages x JOIN a.messages m "
            "  ON m.channel = 'linkedin' AND m.source_key = x.id "
            "GROUP BY 1, 2"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            snap.execute("DETACH a")
        except sqlite3.Error:
            pass

    found: Dict[str, int] = {}
    ambiguous = set()
    for r in rows:
        cv, cid = r[0], r[1]   # positional — see owner_name
        if cv in found and found[cv] != cid:
            ambiguous.add(cv)
        found.setdefault(cv, cid)
    for cv in ambiguous:
        found.pop(cv, None)
    return found


def _counterparties(row: sqlite3.Row, owner_key: str) -> List[str]:
    """Everyone on the message who is not the account holder, as `_key` keys.

    `owner_key` must be `_key(owner)`, not a hand-lowered name, or the account
    holder survives the filter — the hazard the module docstring describes.

    `recipient` holds a list when a thread has several people in it; both comma
    and semicolon appear as the separator in this file.
    """
    parts = [(row["sender"] or "")]
    parts += (row["recipient"] or "").replace(";", ",").split(",")
    out = []
    for p in parts:
        key = _key(p)
        if key and key != owner_key and key not in out:
            out.append(key)
    return out


def missing_rows(snap: sqlite3.Connection, archive_path: str) -> List[sqlite3.Row]:
    """Snapshot messages with no archived twin, by the id both sides share."""
    # _counterparties reads these by column name, so the factory is set here
    # rather than assumed — plan() opens the snapshot, but the function is
    # public and a test or a future caller may not.
    snap.row_factory = sqlite3.Row
    if not os.path.exists(archive_path):
        return snap.execute("SELECT * FROM messages").fetchall()
    try:
        snap.execute("ATTACH ? AS a", (archive_path,))
        return snap.execute(
            "SELECT * FROM messages x WHERE NOT EXISTS ("
            "  SELECT 1 FROM a.messages m "
            "  WHERE m.channel = 'linkedin' AND m.source_key = x.id)"
        ).fetchall()
    finally:
        try:
            snap.execute("DETACH a")
        except sqlite3.Error:
            pass


def plan(snapshot_path: str = SNAPSHOT_PATH,
         archive_path: str = arc.ARCHIVE_DB_PATH,
         crm_conn: Any = None) -> Dict[str, Any]:
    """Work out what can be recovered, without writing anything.

    Returns the CSV-shaped rows to hand to `linkedin_import.archive_messages`,
    the name map it resolves them through, and a count of everything left behind
    with the reason. The reasons are the point: "we recovered 12" means nothing
    without "and N could not be attached, because —".

    Also returns `creatable`: the people the snapshot can describe well enough to
    add, with the message count each is holding. This function still writes
    nothing — `run` decides whether to act on it — so the report and the act stay
    separable, and --dry-run keeps meaning what it says.
    """
    import db  # local: keeps this module importable with no DB configured

    snap = _open(snapshot_path)
    if snap is None:
        return {"available": False, "rows": [], "map": {}, "skipped": {},
                "creatable": {}}

    close_crm = crm_conn is None
    if crm_conn is None:
        crm_conn = db.get_db()
    try:
        owner = owner_name(snap)
        owner_key = _key(owner)
        names = name_index(crm_conn, owner)
        threads = thread_index(snap, archive_path)

        # Everything the snapshot knows about a person, keyed the same way as
        # everything else, so a row that cannot be attached can still say who it
        # belongs to. `setdefault` because two snapshot rows can normalize to one
        # key; the first is as good an answer as the second and neither is a
        # guess about identity.
        snap_people: Dict[str, Dict[str, str]] = {}
        for url, nm, pos, comp, con in snap.execute(
            "SELECT url, name, position, company, connected_on FROM contacts"
        ):
            key = _key(nm)
            if key:
                snap_people.setdefault(key, {
                    "name": _display_name(nm), "linkedin_url": (url or "").strip(),
                    "position": (pos or "").strip(), "company": (comp or "").strip(),
                    "connected_on": (con or "").strip(),
                })

        # People who ARE in crm.db but hidden or deleted. They are absent from
        # `names` for a reason the user chose, so creating them would resurrect
        # them and attaching to them would file messages onto a card they cannot
        # open — a count with no timeline, which is the bug NC-070 was about.
        # The guard is what keeps that a fact rather than a habit.
        away = {
            _key(nm) for (nm,) in crm_conn.execute(
                "SELECT name FROM contacts "
                f"WHERE {figures.population_not_where('active')}")
            if _key(nm)
        }

        out_rows: List[Dict[str, str]] = []
        # Only the keys actually used are handed on, so the count refresh at the
        # end of archive_messages touches the contacts this run concerns rather
        # than all of them.
        used: Dict[str, int] = {}
        skipped = {"no body": 0, "person not in the CRM": 0,
                   "person you hid or deleted": 0, "name not recognised": 0}
        # key -> the snapshot's record of them, plus how many messages wait on it.
        creatable: Dict[str, Dict[str, Any]] = {}

        for row in missing_rows(snap, archive_path):
            if not (row["body"] or "").strip():
                skipped["no body"] += 1
                continue

            others = _counterparties(row, owner_key)
            # Through `_key`, not `.lower()`. A raw comparison here would
            # silently file every recovered message as INBOUND — including the
            # ones the owner sent — because the owner's snapshot spelling can
            # carry the ⟢ that `_key` strips. A test in test_linkedin_snapshot.py
            # fails if a bare .lower() comparison reappears.
            outbound = _key(row["sender"]) == owner_key
            match = next((p for p in others if p in names), None)
            key: Optional[str] = match

            if key is None:
                cid = threads.get(row["conversation_id"])
                if cid is not None:
                    key = CONVO_KEY.format(row["conversation_id"])
                    used[key] = cid
                else:
                    hidden = next((p for p in others if p in away), None)
                    known = next((p for p in others if p in snap_people), None)
                    if hidden is not None:
                        skipped["person you hid or deleted"] += 1
                    elif known is not None:
                        skipped["person not in the CRM"] += 1
                        who = creatable.setdefault(
                            known, {**snap_people[known], "messages": 0})
                        who["messages"] += 1
                    else:
                        skipped["name not recognised"] += 1
                    continue
            else:
                used[key] = names[key]

            # Shaped so that archive_messages' own branch reaches the contact we
            # resolved and labels the direction we already know: it treats a
            # sender it can look up as inbound, and falls through to the
            # recipients otherwise.
            out_rows.append({
                "CONVERSATION ID": row["conversation_id"] or "",
                "DATE": row["date_utc"] or "",
                "FROM": row["sender"] or "",
                "CONTENT": row["body"] or "",
                "SENDER PROFILE URL": "" if outbound else key,
                "RECIPIENT PROFILE URLS": key if outbound else "",
            })

        return {"available": True, "rows": out_rows, "map": used,
                "skipped": skipped, "owner": owner, "creatable": creatable}
    finally:
        snap.close()
        if close_crm:
            crm_conn.close()


def create_missing(creatable: Dict[str, Dict[str, Any]]) -> int:
    """Add the people the snapshot can describe. Returns how many were created.

    Goes through `db.upsert_contact_from_linkedin` rather than `create_contact`
    so a person gets their profile URL, title, company and connection date in
    one write, gets `role_type` derived the same way every other LinkedIn route
    derives it, and — the reason it matters here — is matched against EVERY
    contact by normalized name rather than only the visible ones, so a card can
    never be duplicated. `plan` has already withheld anyone hidden or deleted,
    so the ids that come back are cards the owner can actually open.

    A person with no name is skipped: there is nothing to create, and an
    empty-named card is unfindable and undeletable from the UI.
    """
    import db
    # The snapshot writes dates the way LinkedIn shows them ("01 Mar 2020");
    # every connected_on already in crm.db is ISO. Delegated rather than parsed
    # again here — a second date reader is the same mistake as a second name
    # normalizer: two readers of one field drift apart.
    import linkedin_import as li

    conn = db.get_db()
    try:
        made = 0
        for who in creatable.values():
            name = (who.get("name") or "").strip()
            if not name:
                continue
            before = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE name_norm = ?",
                (_key(name),)).fetchone()[0]
            db.upsert_contact_from_linkedin(
                conn, name=name,
                position=who.get("position", ""), company=who.get("company", ""),
                connected_on=li._parse_connected_on(who.get("connected_on", "")),
                linkedin_url=who.get("linkedin_url", ""))
            # Counted from the table, not assumed from the call: the upsert
            # backfills an existing card just as happily as it inserts, and
            # reporting "created 1" for a backfill would be a wrong count on
            # the result screen.
            if not before:
                made += 1
        conn.commit()
        return made
    finally:
        conn.close()


def run(snapshot_path: str = SNAPSHOT_PATH,
        archive_path: str = arc.ARCHIVE_DB_PATH,
        dry_run: bool = False,
        create_contacts: bool = True) -> Dict[str, Any]:
    """Recover what can be attached. Returns the plan plus what was written."""
    result = plan(snapshot_path, archive_path)
    if not result["available"]:
        return {**result, "new": 0, "created": 0, "dry_run": dry_run}

    # Create first, then plan AGAIN, so the new cards are reached by the same
    # name lookup as everyone else. Re-deriving is cheaper than it sounds — the
    # scan covers only messages with no archived twin — and
    # it means there is exactly one route from a message to a contact instead of
    # a second one bolted on for people created a moment ago.
    created = 0
    if not dry_run and create_contacts and result.get("creatable"):
        created = create_missing(result["creatable"])
        if created:
            result = plan(snapshot_path, archive_path)

    if dry_run or not result["rows"]:
        return {**result, "new": 0, "created": created, "dry_run": dry_run}

    # Imported here, not at module scope: linkedin_import pulls in `requests`,
    # and a migration script is no reason for that to be a hard dependency of
    # importing this file.
    import linkedin_import as li

    wrote = li.archive_messages(result["rows"], result["map"],
                                archive_path=archive_path)

    # Chunks are what makes a message findable by search, and they are derived
    # data that no importer builds as a side effect of the insert — every one of
    # them rebuilds afterwards (linkedin_import.py:317-325 is the closest twin).
    # Without this the recovered messages would be readable on a timeline
    # and invisible to search, which is its own version of the bug this fixes.
    # Keyed on "there was something to recover", not on "the insert reported
    # new rows". Those differ exactly once: if a previous run inserted the
    # messages but died before chunking, `new` is 0 and the work is still
    # outstanding. This condition goes false on its own the run after a
    # successful recovery, because those messages are no longer missing.
    chunks = 0
    if result["rows"]:
        arc_conn = arc.get_archive_db(archive_path)
        try:
            # Scoped to the contacts this run actually touched, not the whole
            # archive: build_chunks is per-contact by design so a recovery does
            # not re-encode hundreds of people's history to add one person's.
            for cid in set(result["map"].values()):
                chunks += arc.build_chunks(arc_conn, contact_id=cid)
            arc_conn.commit()
        finally:
            arc_conn.close()

    # NC-107. Recount, so the cards say how many messages are now there to show.
    # Imported here for the same reasons as linkedin_import and db above.
    import db
    import engagement
    conn = db.get_db()
    try:
        engagement.refresh(conn, archive_path=archive_path)
    finally:
        conn.close()

    return {**result, "new": wrote.get("new", 0), "created": created,
            "chunks": chunks, "already_there": wrote.get("skipped", 0),
            "dry_run": False}


def _cli() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    r = run(dry_run=dry, create_contacts="--no-create" not in args)
    if not r["available"]:
        print(f"No snapshot at {SNAPSHOT_PATH} — nothing to recover.")
        return

    total = len(r["rows"]) + sum(r["skipped"].values())
    print(f"Snapshot messages with no archived copy: {total}")
    print(f"  can be attached to a contact: {len(r['rows'])}")
    for reason, n in r["skipped"].items():
        if n:
            print(f"  left behind ({reason}): {n}")
    waiting = r.get("creatable") or {}
    if waiting:
        held = sum(w["messages"] for w in waiting.values())
        n = len(waiting)
        print(f"  {n} {'person' if n == 1 else 'people'} there "
              f"{'is' if n == 1 else 'are'} not in the CRM, holding "
              f"{held} of those messages"
              + ("" if n == 1 else " between them"))
    if r["dry_run"]:
        print("\n--dry-run: nothing written.")
    else:
        if r.get("created"):
            print(f"\nContacts created: {r['created']}")
        print(f"\nRecovered into archive.db: {r['new']}")
        if r.get("chunks"):
            print(f"Search chunks rebuilt: {r['chunks']}")
        if r.get("already_there"):
            print(f"Already present, ignored: {r['already_there']}")


if __name__ == "__main__":
    _cli()
