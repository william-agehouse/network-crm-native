"""
Local message archive — raw conversation content, kept in archive.db.

archive.db is separate from crm.db so it can be deleted without touching your
curated data. Set ARCHIVE_DB_PATH to override the default location.

TWO COLUMNS DESCRIBE A GROUP CHAT, AND CONFLATING THEM WAS NC-110.
`group_name` is the LABEL to show a human. `group_key` is WHICH CONVERSATION it
is. They have to be separate because a chat with no subject line has no label of
its own, so the importers generate one from the head count — and "Group of 3" is
then the name of every unnamed three-person chat there has ever been. Grouping by
that label alone put the messages of dozens of distinct people under the single
row "Group of 3". A label that claims three participants cannot describe dozens,
and that arithmetic is what the test asserts.

So: group by `group_key`, print `group_name`. `group_key` is
"{channel}:{source chat id}" — both importers already had the id in hand and
threw it away. It stays '' for one-to-one messages, and '' also means "imported
before this column existed", which is why the backfill in insert_message below
exists.

Tables
------
messages      — one row per message; UNIQUE(channel, source_key) makes every
                import idempotent via INSERT OR IGNORE.
messages_fts  — FTS5 content table over messages, kept in sync by triggers.
chunks        — rolling windows of message text, embedded for semantic search
                over conversation content (see search_cli.chunk_semantic_rank).

A MESSAGE CAN BELONG TO A CONVERSATION RATHER THAN A PERSON (NC-111).
`contact_id` answers "whose page is this on", and for a message the owner sent to a
group chat there is no honest answer: it went to everyone in the thread. Both
importers used to resolve that by dropping those messages, so every group
conversation in the app was a transcript with the owner's half deleted — every group
message was inbound. They are stored with `contact_id = NO_PERSON` now, and
found through `group_key` instead. Readers that COUNT PEOPLE must exclude them —
`is_a_person()` below is the one spelling of that — because a thread-owned row is
not a person and counting it as one is how a total starts overclaiming.

Channels: "whatsapp" | "imessage" | "email" | "telegram" | "messenger" | "instagram"
Direction: 0 = inbound (received), 1 = outbound (sent by me)
"""
import contextlib
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

import config  # resolves DATA_DIR / SECRETS_DIR and loads secrets/.env

CHUNK_GAP_HOURS = 4    # start a new chunk after this much silence
CHUNK_MAX_MSGS = 20    # ...or after this many messages, whichever comes first

# contact_id for a message that belongs to a conversation rather than to one
# person — see the NC-111 paragraph above. 0 rather than NULL because the column
# is NOT NULL and every reader already treats it as an integer; and because a
# missing person and an unknown person are the same thing here, so a third state
# would only be a third thing to get wrong.
NO_PERSON = 0

HERE = os.path.dirname(os.path.abspath(__file__))

ARCHIVE_DB_PATH = os.environ.get("ARCHIVE_DB_PATH", config.data_path("archive.db"))

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL,
    channel     TEXT    NOT NULL,
    direction   INTEGER NOT NULL DEFAULT 0,  -- 0 inbound / 1 outbound
    ts          TEXT    NOT NULL,            -- 'YYYY-MM-DD HH:MM:SS' UTC
    subject     TEXT    NOT NULL DEFAULT '', -- email subject; '' otherwise
    body        TEXT    NOT NULL DEFAULT '', -- message text (empty for media)
    source_key  TEXT    NOT NULL,            -- source-DB primary key as string
    group_name  TEXT    NOT NULL DEFAULT '', -- group-chat label; '' for 1:1
    group_key   TEXT    NOT NULL DEFAULT '', -- WHICH group; see NC-110 below
    UNIQUE(channel, source_key)
);
CREATE INDEX IF NOT EXISTS idx_msg_contact ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel, contact_id);
CREATE INDEX IF NOT EXISTS idx_msg_ts      ON messages(ts);
-- idx_msg_group is NOT here on purpose: see init_archive.

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    body, subject,
    content     = 'messages',
    content_rowid = 'id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body, subject)
    VALUES (new.id, new.body, new.subject);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body, subject)
    VALUES ('delete', old.id, old.body, old.subject);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body, subject)
    VALUES ('delete', old.id, old.body, old.subject);
    INSERT INTO messages_fts(rowid, body, subject)
    VALUES (new.id, new.body, new.subject);
END;

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    contact_id   INTEGER NOT NULL,
    channel      TEXT    NOT NULL,
    ts_start     TEXT    NOT NULL,
    ts_end       TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    content_hash TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    group_name   TEXT    NOT NULL DEFAULT '', -- NC-112: the conversation's label
    group_key    TEXT    NOT NULL DEFAULT ''  -- ...and which conversation it is
);
CREATE INDEX IF NOT EXISTS idx_chunk_contact ON chunks(contact_id);
"""


def is_a_person(alias: str = "") -> str:
    """SQL for "this row is somebody's, not a whole conversation's".

    One spelling, because the alternative is each counting site inventing its
    own and one of them forgetting: a thread-owned row counted as a person
    inflates "how many people have you talked to" by exactly one per channel,
    which is small enough to look like a rounding error and wrong in the
    direction that flatters the number.

    `alias` is the table alias plus a dot, for queries that join.
    """
    return f"COALESCE({alias}contact_id, 0) <> {NO_PERSON}"


def get_archive_db(path: str = ARCHIVE_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextlib.contextmanager
def open_if_present():
    """Yield a connection to archive.db, or None when it is absent or unreadable.

    The API layer must degrade to empty rather than 500: a fresh install has no
    archive.db at all, and a running import may hold the write lock. Reads the
    module-global ARCHIVE_DB_PATH at call time and calls get_archive_db through
    the module namespace, so tests that rebind either still take effect.
    """
    conn = None
    if os.path.exists(ARCHIVE_DB_PATH):
        try:
            conn = get_archive_db(ARCHIVE_DB_PATH)
        except Exception:  # noqa: BLE001 — a locked/corrupt archive must not 500
            conn = None
    try:
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def init_archive(path: str = ARCHIVE_DB_PATH) -> None:
    """Create archive.db and all tables / indexes / triggers if absent."""
    conn = get_archive_db(path)
    conn.executescript(_DDL)
    # migration for archives created before group-chat support
    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "group_name" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # NC-110. Added rather than rebuilt, so an existing large archive is not
    # rewritten; the rows fill in as each importer next runs — see the backfill
    # branch in insert_message.
    if "group_key" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN group_key TEXT NOT NULL DEFAULT ''")
    # After the ALTER, never inside _DDL. On an archive created before the
    # column, `CREATE TABLE IF NOT EXISTS messages` does nothing, so an index
    # in the DDL would name a column that does not exist yet and executescript
    # would raise. The migration tests in test_archive and test_style_profile
    # cover this.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_group ON messages(group_key, ts)")
    # NC-227. "Who is waiting on a reply?" (feed.owed_reply_candidates) has to look
    # at the newest message each way for every person, one-to-one chats only. With
    # no index shaped for that it read the whole messages table, bodies and all,
    # on every Home page load. This index holds exactly the four things the
    # question uses, so it is answered from the index alone: on a synthetic
    # archive of 300,000 messages the read went from about 258 ms to about 16 ms,
    # and fetching one person's newest message from 2.6 ms for twenty people to
    # 0.2 ms. `ts` comes before `direction` so that second read gets its newest
    # message straight off the index without sorting. After the ALTERs above, for
    # the same reason as idx_msg_group: it names group_name, which an archive
    # created before group chats does not have until then.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_owed "
                 "ON messages(group_name, contact_id, ts, direction)")
    # NC-112. A chunk of the owner's own group messages belongs to no person, so
    # the only way a search hit on it can say where it was said is for the chunk
    # to carry its conversation. Chunks are derived data: the ones that need
    # the label (NO_PERSON's) are rebuilt once, here, rather than guessed from
    # the "(in …)" in their text. Their text and hash come out the same, so
    # no embedding is recomputed.
    chunk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)")}
    if "group_key" not in chunk_cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE chunks ADD COLUMN group_key TEXT NOT NULL DEFAULT ''")
        conn.commit()
        build_chunks(conn, contact_id=NO_PERSON)
    conn.commit()
    conn.close()


# ── Write ────────────────────────────────────────────────────────────────────

def insert_message(conn, *, contact_id: int, channel: str, direction: int,
                   ts: str, body: str, source_key: str,
                   subject: str = "", group_name: str = "",
                   group_key: str = "") -> bool:
    """INSERT OR IGNORE one message. Returns True if newly stored.

    If the row already exists but was stored before group labels or group keys
    existed, backfill those so old group imports pick them up too. This is the
    whole migration path for NC-110: re-running an importer fills in the keys for
    history that is already here, and because both backfills are conditional on
    the stored value still being '', a row that already has one is never
    rewritten — so a second run does no work and fires no FTS re-index.
    """
    conn.execute(
        """INSERT OR IGNORE INTO messages
           (contact_id, channel, direction, ts, subject, body, source_key,
            group_name, group_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contact_id, channel, direction, ts, subject, body or "", source_key,
         group_name or "", group_key or ""),
    )
    if conn.execute("SELECT changes()").fetchone()[0]:
        return True
    if group_name:
        conn.execute(
            "UPDATE messages SET group_name = ? "
            "WHERE channel = ? AND source_key = ? AND group_name = ''",
            (group_name, channel, source_key),
        )
    if group_key:
        conn.execute(
            "UPDATE messages SET group_key = ? "
            "WHERE channel = ? AND source_key = ? AND group_key = ''",
            (group_key, channel, source_key),
        )
    # NC-142: repair a body that an older import truncated. Both email
    # importers used to cut at 5,000 characters and drop quoted lines, so a
    # stored email can stop mid-sentence — and INSERT OR IGNORE alone would
    # leave it that way forever, because the row exists.
    #
    # LONGER WINS, rather than "newest wins". A re-import must never be able to
    # shorten history: if a future importer regresses, or a source only offers a
    # preview of a message it once gave in full, overwriting would destroy text
    # that cannot be fetched again. Longer-wins can only ever add.
    #
    # Conditional in SQL, not in Python, so the common case — the same body
    # arriving again — writes nothing, fires no UPDATE trigger and does no FTS
    # re-indexing. That is what keeps a second run free.
    if body:
        conn.execute(
            "UPDATE messages SET body = ? "
            "WHERE channel = ? AND source_key = ? AND LENGTH(body) < LENGTH(?)",
            (body, channel, source_key, body),
        )
    return False


# ── Read ─────────────────────────────────────────────────────────────────────

def channel_watermark(conn, channel: str) -> str:
    """Latest ts already stored for this channel (across all contacts), or ''."""
    row = conn.execute(
        "SELECT MAX(ts) AS w FROM messages WHERE channel = ?", (channel,)
    ).fetchone()
    return (row["w"] or "") if row else ""


def thread_keys(conn, contact_id: int) -> List[str]:
    """The group conversations this contact took part in.

    Took part in == said something in it. No participant table is needed for
    that, and adding one would only record what the messages already say. The
    consequence is that somebody who was in a thread and never spoke does not
    appear to have been in it — true, and the honest limit of what the archive
    knows.
    """
    return [r["group_key"] for r in conn.execute(
        "SELECT DISTINCT group_key FROM messages "
        "WHERE contact_id = ? AND group_key <> ''", (contact_id,))]


def shift_ts(ts: str, hours: float) -> str:
    """A stored timestamp moved by `hours`, in the same 'YYYY-MM-DD HH:MM:SS'
    form. '' when it cannot be parsed, which every caller reads as "no bound"."""
    dt = _parse_ts(ts)
    if dt is None:
        return ""
    return (dt + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def get_messages(conn, contact_id: int, channel: Optional[str] = None,
                 limit: int = 50, before: Optional[str] = None,
                 include_threads: bool = False,
                 since: Optional[str] = None) -> List:
    """Fetch messages for a contact, newest first.

    `include_threads` widens it to the WHOLE of every group conversation this
    contact took part in, rather than only their own lines in it (NC-106).
    Requirement: group threads show on everyone's page.

    Off by default, deliberately. Every existing caller means "this person's
    messages" — counts, engagement, style profiling — and silently handing them
    other people's lines would make each of those overclaim. The person page and
    its episode view are the two that want the conversation.
    """
    where: List[str] = ["contact_id = ?"]
    params: list = [contact_id]
    if include_threads:
        keys = thread_keys(conn, contact_id)
        if keys:
            # A subquery would re-derive the same list per row; the keys are at
            # most a few dozen, so they go in as parameters.
            ph = ",".join("?" * len(keys))
            where = [f"(contact_id = ? OR group_key IN ({ph}))"]
            params = [contact_id, *keys]
    if channel:
        where.append("channel = ?")
        params.append(channel)
    if before:
        where.append("ts < ?")
        params.append(before)
    if since:
        # A lower bound as well as an upper one, so a caller can ask for one
        # window rather than "the N most recent before X" and then throw most of
        # it away. NC-106 needs it: a group episode is cut from one person's
        # lines, so the conversation around it starts before their first message
        # and ends after their last.
        where.append("ts >= ?")
        params.append(since)
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM messages WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC LIMIT ?",
        params,
    ).fetchall()


def contact_archive_counts(conn, contact_id: int) -> dict:
    """Per-channel archived message count for one contact."""
    rows = conn.execute(
        "SELECT channel, COUNT(*) AS n FROM messages "
        "WHERE contact_id = ? GROUP BY channel",
        (contact_id,),
    ).fetchall()
    return {r["channel"]: r["n"] for r in rows}


def fts_search(conn, query: str, limit: int = 30) -> List:
    """Full-text keyword search over archived messages, BM25-ranked."""
    # Escape FTS5 special chars to avoid query-parse errors on plain words
    safe = query.replace('"', '""')
    try:
        return conn.execute(
            """SELECT m.*,
                      snippet(messages_fts, 0, '<b>', '</b>', '…', 20) AS snippet
               FROM messages_fts f
               JOIN messages m ON m.id = f.rowid
               WHERE messages_fts MATCH ?
               ORDER BY bm25(messages_fts)
               LIMIT ?""",
            (f'"{safe}"', limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def total_archived(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    return row["n"] if row else 0


# ── Chunking (for semantic search over conversation content) ────────────────

def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime((ts or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _hours_between(ts1: str, ts2: str) -> float:
    a, b = _parse_ts(ts1), _parse_ts(ts2)
    if a is None or b is None:
        return 0.0
    return abs((b - a).total_seconds()) / 3600.0


def _flush_chunk(contact_id: int, channel: str, msgs: list) -> Optional[dict]:
    """Build one chunk dict from a run of same-contact, same-channel messages,
    or None if none of them had text (pure media messages)."""
    lines = []
    for m in msgs:
        body = (m["body"] or "").strip()
        if not body:
            continue
        who = "Me" if m["direction"] == 1 else "Them"
        group = (m["group_name"] or "") if "group_name" in m.keys() else ""
        # group messages carry their group label so search hits and AI reads
        # can tell "said to me" from "said in a group chat"
        lines.append(f"{who} (in {group}): {body}" if group else f"{who}: {body}")
    if not lines:
        return None
    keys = sorted(m["source_key"] for m in msgs)
    groups = sorted({(m["group_name"] or "") for m in msgs
                     if "group_name" in m.keys() and m["group_name"]})
    # group labels are part of the rendered text, so they must be part of the
    # hash — otherwise relabeling would silently keep a stale cached embedding.
    # Chunks without groups hash exactly as before, keeping their cache warm.
    #
    # NC-110 deliberately left `group_key` OUT of this. Splitting a welded
    # mega-thread changes which MESSAGES land in a chunk, so `keys` already
    # changes wherever the text does, and the affected chunks re-embed on their
    # own. Adding the key as well would change the hash of every group chunk —
    # including those of threads that have a real subject line and do not split
    # at all — and cost a re-encode of every cached vector to produce identical
    # text.
    hash_input = channel + "|" + "|".join(keys) + ("|g:" + "|".join(groups) if groups else "")
    content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    # One conversation per chunk: build_chunks breaks on a change of (key,
    # label), so the first message's pair is every message's.
    first = msgs[0].keys()
    return {
        "contact_id": contact_id,
        "channel": channel,
        "ts_start": msgs[0]["ts"],
        "ts_end": msgs[-1]["ts"],
        "text": "\n".join(lines),
        "content_hash": content_hash,
        "group_name": (msgs[0]["group_name"] or "") if "group_name" in first else "",
        "group_key": (msgs[0]["group_key"] or "") if "group_key" in first else "",
    }


def _insert_chunk(conn, chunk: dict) -> None:
    conn.execute(
        """INSERT INTO chunks (contact_id, channel, ts_start, ts_end, text, content_hash,
                               group_name, group_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (chunk["contact_id"], chunk["channel"], chunk["ts_start"], chunk["ts_end"],
         chunk["text"], chunk["content_hash"], chunk["group_name"], chunk["group_key"]),
    )


def build_chunks(conn, contact_id: Optional[int] = None, contact_ids=None) -> int:
    """Rebuild rolling-window conversation chunks for semantic search.

    A chunk breaks on a channel change, a gap of more than CHUNK_GAP_HOURS, or
    after CHUNK_MAX_MSGS messages — whichever comes first. Chunks are derived
    data, so rebuilding is scoped to one contact's own messages (or, if
    contact_id is None, every contact in turn) rather than the whole archive;
    re-running after a fresh import never reprocesses unrelated contacts.
    Unchanged windows get the same content_hash, so the embedding cache in
    search_cli.py skips re-encoding them. Returns the number of chunks written.

    `contact_ids` rebuilds exactly those people under one commit (NC-222): an
    import passes the people it gave a new message, not everyone it matched.
    """
    if contact_ids is not None:
        contact_ids = sorted(set(contact_ids))
    elif contact_id is not None:
        contact_ids = [contact_id]
    else:
        contact_ids = [
            r["contact_id"] for r in
            conn.execute("SELECT DISTINCT contact_id FROM messages")
        ]

    total = 0
    for cid in contact_ids:
        conn.execute("DELETE FROM chunks WHERE contact_id = ?", (cid,))
        rows = conn.execute(
            # group_key then group_name in the sort keeps each group chat
            # contiguous, so the group-change break below doesn't shred
            # interleaved days into dozens of tiny chunks. NC-110 put the key
            # first: sorting by the LABEL welded every unnamed three-person chat
            # into one run, because they all share the label "Group of 3".
            "SELECT * FROM messages WHERE contact_id = ? "
            "ORDER BY channel, group_key, group_name, ts",
            (cid,),
        ).fetchall()

        def _grp(row) -> tuple:
            """What makes this message's conversation a different one.

            The KEY, paired with the label. Key alone would be wrong while a
            backfill is outstanding — every un-keyed group message reads as ''
            and would weld to the one-to-one stream, which is worse than the
            bug. The pair is never coarser than the label alone was, so this
            can only split chunks that were previously merged.
            """
            keys = row.keys()
            return (row["group_key"] if "group_key" in keys else "",
                    row["group_name"] if "group_name" in keys else "")

        bucket: list = []
        cur_channel: Optional[str] = None
        for r in rows:
            starts_new = cur_channel is not None and (
                r["channel"] != cur_channel
                or len(bucket) >= CHUNK_MAX_MSGS
                or _grp(r) != _grp(bucket[-1])   # group chats chunk separately
                or _hours_between(bucket[-1]["ts"], r["ts"]) > CHUNK_GAP_HOURS
            )
            if starts_new:
                chunk = _flush_chunk(cid, cur_channel, bucket)
                if chunk:
                    _insert_chunk(conn, chunk)
                    total += 1
                bucket = []
            cur_channel = r["channel"]
            bucket.append(r)

        if bucket:
            chunk = _flush_chunk(cid, cur_channel, bucket)
            if chunk:
                _insert_chunk(conn, chunk)
                total += 1

    conn.commit()
    return total
