"""
SQLite data layer for the Network CRM bot.

One file, one database (crm.db). Holds the contacts you describe, a timestamped
note timeline per contact, multiple identifiers (phone/email/social) per person,
and a little key/value table for bot state.

The heavy source data (LinkedIn messages, thousands of contacts, embeddings) lives in the
separate, read-only linkedin_messages.db. This database only holds what *you*
consciously write plus a compact enrichment summary copied over from there.

Designed to run the same wherever a plain file works. No Postgres, no server —
just SQLite + WAL.
"""

from __future__ import annotations  # allow `str | None` hints on older Pythons (3.8)

import json
import math
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import config  # resolves DATA_DIR / SECRETS_DIR and loads secrets/.env
import figures

# crm.db lives in data/ unless overridden (set CRM_DB_PATH)
DB_PATH = os.environ.get("CRM_DB_PATH", config.data_path("crm.db"))


# ── Connection ──────────────────────────────────────────────────────────────
# How long a write on a get_db() connection waits for another connection's write
# lock before giving up (NC-052). WAL means a reader never blocks a writer, so
# the only collision left is one writer holding the lock while another wants in.
#
# **This timeout is not the fix for "database is locked".** Raising it would only
# make the failure slower. The cause is a write held open across something slow:
# crm/api_ask.py's handle_chat writes the user's turn on its connection and must
# commit before it runs the Anthropic tool-use loop, because nothing that loop
# touches (search_people, get_contact, semantic_search — all reads) releases the
# lock. A 40-round answer would hold the writer lock for minutes, and no timeout
# survives that. handle_chat therefore commits before the network call.
#
# With the lock never held across a network call, anything still blocked for 20
# seconds is a genuinely contended database, not a slow answer, and a save that
# hangs for half a minute before failing is worse than one that says so. 20s is
# also this codebase's own answer to "how long may one operation make another
# wait" — CALENDAR_FETCH_TIMEOUT_DEFAULT in crm/api_import.py is the same number.
#
# A write blocked past the ceiling still fails. _LockedConnection below is what
# stops that failure reading as corruption.
WRITE_LOCK_TIMEOUT_SEC = 20

# sqlite3's own wording for the failure above ("database is locked") names no
# cause and suggests nothing to do about it — which is exactly why it reads as
# corruption rather than contention (NC-052). Every write made through get_db() goes
# through a _LockedConnection, so translating it once here reaches every
# caller: the HTTP handlers (whose generic 500 catch is `str(exc)`, so this IS
# the string a browser ends up showing) and the CLI tools that already catch
# sqlite3.OperationalError around a get_db() connection (style_profile.py,
# ics_import.py) — which is why this raises the same exception type with a
# different message, not some other class those `except` clauses would miss.
LOCKED_MESSAGE = (
    "the database is busy with another write right now — most often a live Ask "
    "answer that hasn't finished. Nothing was saved; wait a few seconds and try again."
)


def _relock(exc: sqlite3.OperationalError) -> sqlite3.OperationalError:
    """sqlite3.OperationalError("database is locked") -> the same type, a
    message a person can act on. Any other OperationalError (a missing table,
    a malformed file) is returned unchanged — this only ever touches the one
    string NC-052 covers."""
    if "locked" in str(exc).lower():
        return sqlite3.OperationalError(LOCKED_MESSAGE)
    return exc


class _LockedConnection(sqlite3.Connection):
    """sqlite3.Connection, but every write-shaped call turns a lock timeout
    into LOCKED_MESSAGE. Reads are untouched on purpose: WAL never blocks a
    reader, so a SELECT cannot raise this in the first place, and wrapping it
    anyway would just be more code with nothing to catch."""

    def execute(self, *args, **kwargs):  # INSERT/UPDATE/DELETE and ad hoc DDL
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            raise _relock(exc) from exc

    def executemany(self, *args, **kwargs):
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            raise _relock(exc) from exc

    def executescript(self, *args, **kwargs):  # init_db's SCHEMA
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            raise _relock(exc) from exc

    def commit(self):  # WAL's own commit can briefly need the writer lock too
        try:
            return super().commit()
        except sqlite3.OperationalError as exc:
            raise _relock(exc) from exc


def release_write_lock(conn) -> bool:
    """Commit any pending write so the writer lock is not held across something
    slow. Returns True if it actually committed. NC-082.

    WHY THIS IS A NAMED FUNCTION AND NOT A BARE conn.commit()
    ---------------------------------------------------------
    SQLite in WAL mode allows exactly one writer at a time, and Python's sqlite3
    opens a write transaction IMPLICITLY on the first INSERT/UPDATE/DELETE and
    holds it until commit. Readers are never blocked; every other WRITER waits,
    and after WRITE_LOCK_TIMEOUT_SEC it fails with LOCKED_MESSAGE above.

    So a write followed by something SLOW, before the commit, blocks the whole
    app for the duration of the slow thing. NC-052 was this in crm/api_ask.py
    (a write, then the entire Anthropic loop). NC-082 covers five more places,
    each holding the lock across a network call, an importer subprocess or a
    `sips` invocation — up to 300 seconds against a 20-second ceiling. The worst
    case made two due WhatsApp folders in one autosync pass fail to import at
    all, because the second importer child could not get the lock its own parent
    was holding.

    A bare commit() at each of those sites would be correct and unreadable: the
    next person cannot tell a commit that is there for durability from one that
    is there to let other writers through, so they move it, or drop it as
    redundant. This says which. It is also a no-op when nothing is pending, so
    it is safe to put in front of any slow call without reasoning about what
    came before.

    WHERE IT DOES NOT BELONG
    ------------------------
    A bulk loop that writes rows back to back SHOULD hold the lock — it is
    writing, that is what the lock is for, and committing per row turns one
    fsync into thousands. The rule is about holding the lock while
    NOT writing. Call this before the slow call, not inside a tight write loop.
    """
    if conn.in_transaction:
        conn.commit()
        return True
    return False


def get_db(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=WRITE_LOCK_TIMEOUT_SEC,
                           factory=_LockedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {WRITE_LOCK_TIMEOUT_SEC * 1000}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,
    company         TEXT DEFAULT '',
    position        TEXT DEFAULT '',
    linkedin_url    TEXT DEFAULT '',
    connected_on    TEXT DEFAULT '',
    category        TEXT DEFAULT '',      -- personal | professional | both | unknown
    how_we_met      TEXT DEFAULT '',      -- the single most useful context field
    photo_file_id   TEXT DEFAULT '',      -- telegram file_id for a profile picture
    -- LinkedIn enrichment (written by linkedin_import.py from the API archive)
    msg_count       INTEGER DEFAULT 0,
    first_contact   TEXT DEFAULT '',
    last_contact    TEXT DEFAULT '',
    auto_summary    TEXT DEFAULT '',
    -- iMessage enrichment (written by imessage_import.py), kept distinct from LinkedIn
    imsg_count      INTEGER DEFAULT 0,
    imsg_last       TEXT DEFAULT '',
    -- WhatsApp enrichment (written by whatsapp_import.py)
    wa_count        INTEGER DEFAULT 0,
    wa_last         TEXT DEFAULT '',
    -- Email enrichment
    em_count        INTEGER DEFAULT 0,
    em_last         TEXT DEFAULT '',
    -- Telegram enrichment
    tg_count        INTEGER DEFAULT 0,
    tg_last         TEXT DEFAULT '',
    -- Messenger enrichment
    msgr_count      INTEGER DEFAULT 0,
    msgr_last       TEXT DEFAULT '',
    -- Instagram enrichment
    ig_count        INTEGER DEFAULT 0,
    ig_last         TEXT DEFAULT '',
    -- review tracking
    status          TEXT DEFAULT 'new',   -- new | described | skipped | never_met | no_context
    excluded        INTEGER DEFAULT 0,    -- 1 = never index (hidden by the user)
    times_reviewed  INTEGER DEFAULT 0,
    last_reviewed_at TEXT,
    source          TEXT DEFAULT '',      -- linkedin | manual
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contacts_norm   ON contacts(name_norm);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    kind        TEXT DEFAULT 'note',      -- note | how_we_met | voice | auto
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    deleted     INTEGER DEFAULT 0        -- soft-delete for sync tombstones
);
CREATE INDEX IF NOT EXISTS idx_notes_contact ON notes(contact_id);

CREATE TABLE IF NOT EXISTS identifiers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- phone | email | instagram | facebook | telegram | linkedin | whatsapp_id (hidden, NC-243)
    value       TEXT NOT NULL,            -- phones canonicalised by canonical_phone()
    source      TEXT DEFAULT '',          -- which importer wrote it, on the facts_source ladder
    UNIQUE(contact_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_ident_contact ON identifiers(contact_id);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- Ask view conversation log (crm/chat.py). role: user | assistant.
CREATE TABLE IF NOT EXISTS chat_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Ask sorted into chats. One row is one thread in the Ask view — the thing you
-- exit to start a new one — holding a DERIVED title (chat_title(), never written
-- by an LLM: invariant 8 forbids requiring a key, and billing money for a label
-- is wrong), the answer shape it last ran with, and a soft-delete flag so a
-- removed thread leaves a tombstone the way lists and notes do.
--
-- The TABLE is safe in SCHEMA: it is new, so CREATE TABLE genuinely runs on an
-- existing database as well as a fresh one. The MEMBERSHIP COLUMN
-- (chat_history.conversation_id) is NOT safe here, and neither is its index —
-- both live in ensure_columns(). init_db runs executescript(SCHEMA) BEFORE
-- ensure_columns(), so on an existing database the chat_history CREATE TABLE
-- above is a no-op, conversation_id does not exist yet, and a CREATE INDEX
-- naming it fails the WHOLE script with "no such column: conversation_id" —
-- the app cannot start. An index on contact_dates(source_uid) has the same
-- shape (see the note at the end of SCHEMA). A test on a fresh database cannot
-- see it, because there every table is new.
CREATE TABLE IF NOT EXISTS chat_conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT '',   -- derived by chat_title(), never LLM-written
    shape_json  TEXT DEFAULT '',            -- the answer shape this chat last ran with
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    deleted     INTEGER DEFAULT 0           -- soft-delete, like lists and notes
);

-- Saved lists / event pipelines ("Spring workshop", "founders to follow up", …).
-- Member status tracks a lightweight invite pipeline per list.
CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    deleted     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS list_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    status      TEXT DEFAULT '',   -- '' (to invite) | invited | replied | attending | declined
    added_at    TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(list_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_list_members ON list_members(list_id);

-- Dates worth remembering per person: birthdays, anniversaries, an upcoming
-- meeting, or any custom date. A TABLE, not columns: one person has many dates
-- of many kinds and meetings accumulate.
--
-- The rule: this app NEVER reads the OS calendar. No EventKit, no
-- ~/Library/Calendars — those need permissions the app will not ask for
-- (invariant 6), and since macOS Sequoia even probing them BLOCKS on a consent
-- dialog. What DOES exist is crm/ics_import.py,
-- which reads a .ics file or URL the user supplied by hand, exactly like the
-- iMessage route: the user is privileged, the app is not.
--
-- So rows here have two origins, and `source` is what keeps them apart. A
-- meeting the owner typed must never be indistinguishable from one a parser guessed
-- at from an invite.
CREATE TABLE IF NOT EXISTS contact_dates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'custom',  -- birthday|anniversary|meeting|custom
    label       TEXT DEFAULT '',
    on_date     TEXT NOT NULL,                   -- always full 'YYYY-MM-DD'
    year_known  INTEGER NOT NULL DEFAULT 1,      -- 0 = placeholder year (no age)
    recurring   INTEGER NOT NULL DEFAULT 0,      -- 1 = annual
    source      TEXT DEFAULT 'manual',           -- manual | calendar
    source_uid  TEXT DEFAULT '',                 -- the .ics UID, '' when hand-typed
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(contact_id, kind, on_date, label)
);
CREATE INDEX IF NOT EXISTS idx_cdates_contact ON contact_dates(contact_id);
CREATE INDEX IF NOT EXISTS idx_cdates_on ON contact_dates(on_date);
-- The index on (contact_id, source_uid) is NOT here, and must not be moved here.
-- init_db runs executescript(SCHEMA) BEFORE ensure_columns(), so on an existing
-- database the CREATE TABLE above is a no-op and source_uid does not exist yet —
-- a CREATE INDEX naming it fails the whole script with "no such column:
-- source_uid", and the app cannot start. A fresh database survives that (the
-- column comes from CREATE TABLE) which is exactly why the test suite missed it:
-- every test builds a fresh database. ensure_columns() owns that index.
"""


# SQLite refuses `ADD COLUMN ... DEFAULT (datetime('now'))` once a table holds
# rows ("Cannot add a column with non-constant default"), so an updated_at
# column cannot be added the way CREATE TABLE spells it. Add it bare, seed the
# existing rows from whatever creation stamp the table already carries, and let
# the insert sites pass datetime('now') explicitly — that way a migrated
# database and a freshly created one behave the same.
def _add_stamp_column(conn, table: str, seed_col: str) -> None:
    conn.execute(f"ALTER TABLE {table} ADD COLUMN updated_at TEXT")
    conn.execute(f"UPDATE {table} SET updated_at = COALESCE({seed_col}, datetime('now')) "
                 "WHERE updated_at IS NULL")


def ensure_columns(conn) -> None:
    """Idempotently add newer columns to already-created tables."""
    have_chat = {r["name"] for r in conn.execute("PRAGMA table_info(chat_history)")}
    if have_chat and "contacts_json" not in have_chat:
        # contact cards shown under an assistant reply, so they survive a reload
        conn.execute("ALTER TABLE chat_history ADD COLUMN contacts_json TEXT DEFAULT ''")
    if have_chat and "truncated" not in have_chat:
        # whether the model ran out of output tokens mid-answer. Persisted for the
        # same reason the cards are: on reload the reply still visibly breaks off
        # mid-sentence, and without this there is nothing left to explain why.
        conn.execute("ALTER TABLE chat_history ADD COLUMN truncated INTEGER DEFAULT 0")
    if have_chat and "receipt_json" not in have_chat:
        # NC-155. What this one answer cost and what it called: the `searched`
        # counts, the tool-call `trail`, and `spend` (dollars, tokens, rounds,
        # model). All three are computed, sent to the browser and rendered; with
        # nowhere to keep them, reloading the page would lose the price of every
        # earlier answer, so the cost line would exist only for the few seconds
        # after an answer landed.
        #
        # ONE column holding one JSON object rather than three columns. The three
        # are written together, read together and rendered together by a single
        # component, and a shape that travels as one piece should not be taken
        # apart and reassembled at every boundary. It also means a fourth number
        # later is a key, not a migration.
        conn.execute("ALTER TABLE chat_history ADD COLUMN receipt_json TEXT DEFAULT ''")
    if have_chat and "ask_json" not in have_chat:
        # NC-156. The question this turn asked back, and the buttons it offered.
        #
        # Its own column and not a key in receipt_json, even though both are
        # "extra facts about one turn", because they are read by different
        # things at different times: the receipt is provenance nobody acts on,
        # and this is a control the user clicks. A reload has to put the buttons
        # back — a question whose options vanish is a dead end, since the answer
        # was never written and the thread cannot go anywhere.
        #
        # Nothing records whether it was ANSWERED. That is not an omission: the
        # next turn in the transcript IS the answer, so a question with a turn
        # after it has been answered and one at the end has not. Storing it as
        # well would be a second copy of the same fact, free to disagree with
        # the first.
        conn.execute("ALTER TABLE chat_history ADD COLUMN ask_json TEXT DEFAULT ''")
    if have_chat and "conversation_id" not in have_chat:
        # Which chat this turn belongs to. Added HERE and not in SCHEMA — read the
        # comment on chat_conversations for the crash that rule prevents.
        #
        # The column must default to NULL, which SQLite requires anyway for an
        # ALTER that carries a REFERENCES clause. NULL therefore means exactly one
        # thing: "written before Ask had chats". _adopt_orphan_chat_history() below
        # then adopts those rows in the same call, so a NULL is a transient state
        # inside one init_db and never something a UI has to render.
        conn.execute("ALTER TABLE chat_history ADD COLUMN conversation_id INTEGER "
                     "REFERENCES chat_conversations(id) ON DELETE CASCADE")
        have_chat.add("conversation_id")
    have_conv = {r["name"] for r in conn.execute("PRAGMA table_info(chat_conversations)")}
    if have_chat and "conversation_id" in have_chat:
        # UNCONDITIONAL, and outside the ALTER guard above — the same shape as
        # idx_cdates_uid below, for the same reason. Two paths must both end up
        # with this index: a database that just migrated (the guard fired) and one
        # where a future SCHEMA change puts the column in CREATE TABLE (the guard
        # never fires). Every transcript read filters on this column.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_conv "
                     "ON chat_history(conversation_id, id)")
        if have_conv:
            _adopt_orphan_chat_history(conn)
    have_lists = {r["name"] for r in conn.execute("PRAGMA table_info(lists)")}
    for col, decl in (("event_date", "TEXT DEFAULT ''"), ("venue", "TEXT DEFAULT ''"),
                      ("deleted", "INTEGER DEFAULT 0")):
        if have_lists and col not in have_lists:
            conn.execute(f"ALTER TABLE lists ADD COLUMN {col} {decl}")
    if have_lists and "updated_at" not in have_lists:
        _add_stamp_column(conn, "lists", "created_at")
    have_lm = {r["name"] for r in conn.execute("PRAGMA table_info(list_members)")}
    if have_lm and "updated_at" not in have_lm:
        _add_stamp_column(conn, "list_members", "added_at")
    have_cd = {r["name"] for r in conn.execute("PRAGMA table_info(contact_dates)")}
    if have_cd and "source" not in have_cd:
        # Every row that predates the calendar importer was typed by a human, so
        # 'manual' is the correct backfill and the DEFAULT makes it automatic for
        # existing rows. Getting this the wrong way round would relabel the owner's
        # own meetings as machine guesses, which is the precise confusion the
        # column exists to prevent.
        conn.execute("ALTER TABLE contact_dates ADD COLUMN source TEXT DEFAULT 'manual'")
    if have_cd and "source_uid" not in have_cd:
        conn.execute("ALTER TABLE contact_dates ADD COLUMN source_uid TEXT DEFAULT ''")
    if have_cd:
        # UNCONDITIONAL, and outside the ALTER guard above. Two paths have to end up
        # with this index: a migrated database, where the guard just fired, and a
        # FRESH one, where CREATE TABLE already made the column so the guard never
        # fires at all. Putting it inside the guard would leave every new install
        # without the index. It cannot live in SCHEMA either — see the comment there.
        # Not UNIQUE: one calendar event legitimately produces one row per matched
        # attendee, so a UID repeats across contacts; the pair identifies a row.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cdates_uid "
                     "ON contact_dates(contact_id, source_uid)")
    have_ident = {r["name"] for r in conn.execute("PRAGMA table_info(identifiers)")}
    if have_ident and "source" not in have_ident:
        # Which importer wrote this identifier, on the facts_source ladder
        # (invariant 4). Decides which spelling of a number survives a collapse,
        # so a message_scan handle can never overwrite a hand-typed one.
        conn.execute("ALTER TABLE identifiers ADD COLUMN source TEXT DEFAULT ''")
    if have_ident:
        # Heal the rows written before add_identifier() canonicalised phones:
        # the same number arrived from LinkedIn, iMessage and a vCard export in
        # three spellings and got three rows, which the person page rendered as
        # three pills. One scan, no writes when there is nothing to fix, so it
        # can run on every init_db instead of being a migration nobody re-runs.
        dedupe_phone_identifiers(conn)
    have_notes = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
    if have_notes and "deleted" not in have_notes:
        conn.execute("ALTER TABLE notes ADD COLUMN deleted INTEGER DEFAULT 0")
    if have_notes and "updated_at" not in have_notes:
        _add_stamp_column(conn, "notes", "created_at")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
    for col, decl in (("imsg_count", "INTEGER DEFAULT 0"),
                      ("imsg_last", "TEXT DEFAULT ''"),
                      ("wa_count", "INTEGER DEFAULT 0"),
                      ("wa_last", "TEXT DEFAULT ''"),
                      ("em_count", "INTEGER DEFAULT 0"),
                      ("em_last", "TEXT DEFAULT ''"),
                      ("tg_count", "INTEGER DEFAULT 0"),
                      ("tg_last", "TEXT DEFAULT ''"),
                      ("msgr_count", "INTEGER DEFAULT 0"),
                      ("msgr_last", "TEXT DEFAULT ''"),
                      ("ig_count", "INTEGER DEFAULT 0"),
                      ("ig_last", "TEXT DEFAULT ''"),
                      ("excluded", "INTEGER DEFAULT 0"),
                      # facts layer — enriched by enrich_facts.py
                      ("home_city", "TEXT DEFAULT ''"),
                      ("home_country", "TEXT DEFAULT ''"),
                      ("home_metro", "TEXT DEFAULT ''"),
                      ("topics", "TEXT DEFAULT '[]'"),
                      ("role_type", "TEXT DEFAULT ''"),
                      ("blurb", "TEXT DEFAULT ''"),
                      ("facts_source", "TEXT DEFAULT ''"),
                      ("facts_updated_at", "TEXT DEFAULT ''"),
                      # networking extras re-parsed from the archived LinkedIn
                      # profile text by enrich_li_extras.py — one tidy JSON blob
                      # (shared_history / mutual_names / recent_activity / …)
                      ("li_extra", "TEXT DEFAULT ''"),
                      ("li_extra_updated_at", "TEXT DEFAULT ''"),
                      # local profile photo (data/photos/<id>.jpg) — see crm/photos.py
                      ("photo", "TEXT DEFAULT ''"),
                      ("photo_source", "TEXT DEFAULT ''"),
                      # cached 'story so far' (AI-written or hand-edited)
                      ("brief", "TEXT DEFAULT ''"),
                      ("brief_updated_at", "TEXT DEFAULT ''"),
                      # receipt for the brief: JSON with the
                      # messages/channels it was built from + built_at, so the
                      # UI can render "From N messages · M channels · built …"
                      # and flag staleness when newer messages exist
                      ("brief_meta", "TEXT DEFAULT ''"),
                      # 'not relevant right now' snooze for the reconnect/Worth-a-
                      # message list — see dismiss_reconnect()
                      ("reconnect_dismissed_at", "TEXT DEFAULT ''"),
                      # engagement features aggregated from archive.db by
                      # crm/engagement.py — feed the familiarity score
                      ("eng_active_days", "INTEGER DEFAULT 0"),
                      ("eng_sent", "INTEGER DEFAULT 0"),
                      ("eng_recv", "INTEGER DEFAULT 0"),
                      ("eng_recent90", "INTEGER DEFAULT 0"),
                      ("eng_first", "TEXT DEFAULT ''"),
                      ("eng_last", "TEXT DEFAULT ''"),
                      ("eng_updated_at", "TEXT DEFAULT ''"),
                      # reversible soft-delete: a deleted contact is ALSO
                      # marked excluded=1, so it inherits every existing excluded
                      # filter (review, reconnect, search, dashboard, Ask, the
                      # default list); the flag only keeps it out of the Excluded
                      # view and powers a Deleted view to restore from.
                      ("deleted", "INTEGER DEFAULT 0"),
                      # per-field manual provenance: JSON array of fact
                      # fields the user hand-edited. Enrichment + the LinkedIn
                      # scrape-merge skip only THESE fields, not the whole row —
                      # so correcting a city no longer sterilizes role/topics/
                      # blurb forever. (Legacy rows with facts_source='manual'
                      # are treated as manual-for-all-fields for back-compat.)
                      ("manual_fields", "TEXT DEFAULT ''")):
        if col not in have:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {decl}")
    # NC-250. A data fix, not a column, but it lives here for the same reason
    # as _adopt_orphan_chat_history: this runs on every start, so the fix
    # reaches every install once and then finds nothing more to do. Lazy import,
    # because enrich_facts imports this module.
    # An older database may not have the table yet; it has nothing to fix.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'identifiers'").fetchone():
        import enrich_facts
        enrich_facts.forget_cities_from_hidden_ids(conn)
        enrich_facts.forget_cities_from_foreign_numbers(conn)   # NC-252


def init_db(path: str | None = None) -> None:
    conn = get_db(path)
    conn.executescript(SCHEMA)
    ensure_columns(conn)
    conn.commit()
    conn.close()


# ── Helpers ─────────────────────────────────────────────────────────────────
def normalize_name(name: str) -> str:
    """Lowercase, drop decoration glyphs (e.g. the LinkedIn ⟢), collapse spaces.

    Keeps accented letters so 'Núñez' stays distinct, but strips punctuation and
    decoration symbols so 'Ada Lovelace ⟢' and 'Ada Lovelace' match.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFC", name).lower().strip()
    # keep word characters (incl. accented letters) and spaces, drop the rest
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Contacts ────────────────────────────────────────────────────────────────
def derive_role_type(title_text: str) -> str:
    """Best-guess role_type ('investor', 'founder', …) from a title/company
    string, or '' when nothing matches.

    The pattern list lives in enrich_facts (the enrichment domain owns it), but
    the LinkedIn upsert below needs it too — so it's imported lazily here rather
    than at module load, which would be a cycle (enrich_facts imports db). One
    canonical implementation, used by both the one-click LinkedIn path and the
    CSV fallback, which previously disagreed about what facts you got for free."""
    if not (title_text or "").strip():
        return ""
    try:
        import enrich_facts
        return enrich_facts._match_role_type(title_text) or ""
    except Exception:  # noqa: BLE001 — a missing//broken enricher must not block an import
        return ""


def upsert_contact_from_linkedin(conn, *, name, position, company, connected_on,
                                 linkedin_url="", email="") -> int:
    """Insert a LinkedIn contact if a contact with the same normalized name does
    not already exist. Returns the contact id. Idempotent — safe to re-run."""
    norm = normalize_name(name)
    row = conn.execute("SELECT id FROM contacts WHERE name_norm = ?", (norm,)).fetchone()
    if row:
        cid = row["id"]
        # backfill any empty professional fields without clobbering edits
        conn.execute(
            """UPDATE contacts SET
                 company      = CASE WHEN company='' THEN ? ELSE company END,
                 position     = CASE WHEN position='' THEN ? ELSE position END,
                 linkedin_url = CASE WHEN linkedin_url='' THEN ? ELSE linkedin_url END,
                 connected_on = CASE WHEN connected_on='' THEN ? ELSE connected_on END
               WHERE id = ?""",
            (company or "", position or "", linkedin_url or "", connected_on or "", cid),
        )
    else:
        cur = conn.execute(
            """INSERT INTO contacts
                 (name, name_norm, company, position, linkedin_url, connected_on, source)
               VALUES (?, ?, ?, ?, ?, ?, 'linkedin')""",
            (name, norm, company or "", position or "", linkedin_url or "", connected_on or ""),
        )
        cid = cur.lastrowid
    if email:
        add_identifier(conn, cid, "email", email, source="linkedin_csv")
    # Parity with the CSV path (seed_from_csv derived this inline): fill
    # role_type from the title when it's still empty, so which LinkedIn route
    # you used no longer decides whether you get the fact for free.
    rt = derive_role_type(" ".join(filter(None, [position, company])))
    if rt:
        conn.execute(
            "UPDATE contacts SET role_type = ? WHERE id = ? AND role_type = ''",
            (rt, cid),
        )
    return cid


def create_contact(conn, name: str, *, source="manual") -> int:
    cur = conn.execute(
        "INSERT INTO contacts (name, name_norm, source) VALUES (?, ?, ?)",
        (name.strip(), normalize_name(name), source),
    )
    return cur.lastrowid


def get_contact(conn, contact_id: int):
    return conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()


def search_contacts(conn, query: str, limit: int = 8):
    """Fuzzy-ish name search: normalized substring match, best (shortest) first."""
    q = normalize_name(query)
    if not q:
        return []
    rows = conn.execute(
        """SELECT * FROM contacts
           WHERE name_norm LIKE ?
           ORDER BY (name_norm = ?) DESC, length(name_norm) ASC, last_contact DESC
           LIMIT ?""",
        (f"%{q}%", q, limit),
    ).fetchall()
    return rows


def find_exact(conn, name: str):
    return conn.execute(
        "SELECT * FROM contacts WHERE name_norm = ?", (normalize_name(name),)
    ).fetchall()


def find_by_handle(conn, handle: str):
    """Return a contact row whose stored identifier matches this phone/email handle.

    Enables iMessage/WhatsApp imports to match contacts by phone without needing
    macOS Contacts (AddressBook) access — works once contacts have been imported
    via contacts_import.py which stores phone numbers as identifiers.

    Phone match uses last-9-digits to handle +/0 country-prefix variants.
    """
    handle = (handle or "").strip()
    if not handle:
        return None
    if "@" in handle:
        return conn.execute(
            "SELECT c.* FROM contacts c JOIN identifiers i ON i.contact_id = c.id "
            "WHERE i.kind = 'email' AND LOWER(i.value) = LOWER(?) AND NOT c.excluded LIMIT 1",
            (handle,),
        ).fetchone()
    digits = re.sub(r"\D", "", handle)
    suffix = digits[-9:] if len(digits) >= 9 else digits
    if not suffix:
        return None
    return conn.execute(
        "SELECT c.* FROM contacts c JOIN identifiers i ON i.contact_id = c.id "
        "WHERE i.kind = 'phone' "
        "AND SUBSTR(REPLACE(REPLACE(REPLACE(i.value, '+', ''), '-', ''), ' ', ''), -9) = ? "
        "AND NOT c.excluded LIMIT 1",
        (suffix,),
    ).fetchone()


def pick_review_batch(conn, limit: int = 5, exclude: set[int] | None = None):
    """Choose the next people to describe.

    Priority: never-described first; then the ones you've interacted with most
    recently (freshest in memory, easiest to describe); then most-messaged.
    """
    exclude = exclude or set()
    rows = conn.execute(
        f"""SELECT * FROM contacts
           WHERE status = 'new' AND {figures.population_where('visible')}
           ORDER BY (last_contact <> '') DESC, last_contact DESC, msg_count DESC, name ASC
           LIMIT ?""",
        (limit + len(exclude),),
    ).fetchall()
    out = [r for r in rows if r["id"] not in exclude][:limit]
    if len(out) >= limit:
        return out
    # fall back to least-recently-reviewed described contacts to top up.
    # never_met / no_context are intentionally left out — once you've said you
    # haven't met someone (or have no context), the daily flow stops nagging.
    extra = conn.execute(
        f"""SELECT * FROM contacts
           WHERE status NOT IN ('new', 'never_met', 'no_context') AND {figures.population_where('visible')}
           ORDER BY (last_reviewed_at IS NULL) DESC, last_reviewed_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    seen = {r["id"] for r in out} | exclude
    for r in extra:
        if r["id"] not in seen:
            out.append(r)
        if len(out) >= limit:
            break
    return out


def mark_reviewed(conn, contact_id: int, status: str = "described"):
    conn.execute(
        """UPDATE contacts
           SET status = ?, times_reviewed = times_reviewed + 1,
               last_reviewed_at = datetime('now'), updated_at = datetime('now')
           WHERE id = ?""",
        (status, contact_id),
    )


# The enrichment "facts" — the fields enrich_facts / the LinkedIn scrape write.
FACT_FIELDS = ("home_city", "home_country", "home_metro", "topics", "role_type", "blurb")
# Facts editable one-at-a-time from the person page (topics has its own path).
MANUAL_TRACKED = {"home_city", "home_country", "home_metro", "role_type", "blurb"}


def manual_field_set(row) -> set:
    """Which fact fields the user has hand-edited on this row. Reads the
    per-field manual_fields set; a legacy row-wide facts_source='manual' is
    treated as manual-for-all-facts so pre-existing edits stay protected."""
    keys = row.keys()
    try:
        s = set(json.loads((row["manual_fields"] if "manual_fields" in keys else "") or "[]"))
    except Exception:
        s = set()
    if "facts_source" in keys and (row["facts_source"] or "") == "manual":
        s |= set(FACT_FIELDS)
    return s


def set_field(conn, contact_id: int, field: str, value: str):
    allowed = {
        "category", "how_we_met", "photo_file_id", "company", "position", "name",
        "home_city", "home_country", "home_metro", "role_type", "blurb",
    }
    if field not in allowed:
        raise ValueError(f"field not editable: {field}")
    if field == "name":
        conn.execute(
            "UPDATE contacts SET name = ?, name_norm = ?, updated_at = datetime('now') WHERE id = ?",
            (value, normalize_name(value), contact_id),
        )
    elif field in MANUAL_TRACKED:
        # Record that the user hand-edited THIS field — enrichment and the
        # LinkedIn scrape-merge will skip just this field, not the whole row.
        # (Was: facts_source='manual' row-wide, which locked the contact out of
        # every future enrichment pass forever.)
        row = get_contact(conn, contact_id)
        try:
            mf = set(json.loads((row["manual_fields"] or "[]"))) if row else set()
        except Exception:
            mf = set()
        mf.add(field)
        conn.execute(
            f"UPDATE contacts SET {field} = ?, manual_fields = ?, updated_at = datetime('now') WHERE id = ?",
            (value, json.dumps(sorted(mf)), contact_id),
        )
    else:
        conn.execute(
            f"UPDATE contacts SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
            (value, contact_id),
        )


def ids_outside(conn, population: str) -> set:
    """Contact ids that fall OUTSIDE a named population — the ids to refuse.

    NC-145. Several surfaces needed "who must not appear" as a set to test ids
    against, and each wrote its own `SELECT id FROM contacts WHERE excluded = 1`:
    two in crm/search_cli.py, two in crm/chat.py. All four were hidden-only, so
    all four disagreed with the `population_where('visible')` queries beside
    them the moment a row was deleted without being hidden — and none of them
    could be seen by tests/test_scope_filter_one_spelling.py, whose docstring
    puts the complement direction deliberately out of its own scope.

    One implementation, taking the population by name so the caller still says
    which question it is asking.
    """
    pred = figures.population_not_where(population)
    if not pred:                      # 'all' excludes nobody
        return set()
    return {r["id"] for r in
            conn.execute(f"SELECT id FROM contacts WHERE {pred}")}


def ids_in(conn, population: str) -> set:
    """Contact ids INSIDE a named population — the ids to count.

    NC-122. The other direction from `ids_outside`, and not interchangeable
    with it when the ids being tested come from archive.db. A message there can
    name a person who has no row here at all — a permanent delete removes the
    row first and cleans the archive only if the archive is free — and such an
    id is outside nothing, so "not in ids_outside" counts it. "In ids_in" does
    not."""
    pred = figures.population_where(population)
    sql = "SELECT id FROM contacts" + (f" WHERE {pred}" if pred else "")
    return {r["id"] for r in conn.execute(sql)}


def set_excluded(conn, contact_id: int, value: int = 1):
    conn.execute(
        "UPDATE contacts SET excluded = ?, updated_at = datetime('now') WHERE id = ?",
        (1 if value else 0, contact_id),
    )


def purge_contact(conn, contact_id: int) -> None:
    """PERMANENTLY remove a contact and everything hanging off it in crm.db.
    Irreversible — reachable only from the Deleted view behind an explicit
    confirm. archive.db rows (messages/chunks) live in a separate database and
    are cleaned by the caller; photos go through crm/photos.py."""
    conn.execute("DELETE FROM notes WHERE contact_id = ?", (contact_id,))
    conn.execute("DELETE FROM identifiers WHERE contact_id = ?", (contact_id,))
    conn.execute("DELETE FROM list_members WHERE contact_id = ?", (contact_id,))
    conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))


def set_deleted(conn, contact_id: int, value: int = 1):
    """Reversible soft-delete. Reuses excluded — already filtered from
    review, reconnect, search, dashboard, Ask and the default list — and adds a
    deleted flag so the row stays out of the Excluded view and can be restored."""
    v = 1 if value else 0
    conn.execute(
        "UPDATE contacts SET deleted = ?, excluded = ?, updated_at = datetime('now') WHERE id = ?",
        (v, v, contact_id),
    )


def load_exclude_list(path: str) -> set[str]:
    """Read a names-to-skip file (one name per line, '#' comments). Returns the
    set of normalized names. Missing file -> empty set."""
    names = set()
    if not path or not os.path.exists(path):
        return names
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                names.add(normalize_name(line))
    return names


def add_to_exclude_file(path: str, name: str) -> bool:
    """Append a name to the exclude-list file unless an equivalent (normalized)
    entry is already there. Creates the file if needed.

    Returns True when the file now says what it should — including the case
    where the name was already in it. False means the write failed and the ten
    importers have not been told.

    NC-150. This used to swallow the OSError and return nothing, and its caller
    returned {"ok": true} over the top: on a read-only or full data directory
    the Hide button said it had worked and the file never changed. The failure
    is still not raised — a toggle should not 500 — but it is no longer
    silent. It is SAID, at the screen that claimed it happened.
    """
    name = (name or "").strip()
    if not path or not name:
        return False
    if normalize_name(name) in load_exclude_list(path):
        return True
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(name + "\n")
    except OSError:
        return False
    return True


def remove_from_exclude_file(path: str, name: str) -> bool:
    """Drop every line matching `name` (normalized) from the exclude-list file,
    preserving comments and other entries.

    Returns True when the file no longer names them — a file that does not exist
    counts, since a name cannot be in it. False means the rewrite failed and the
    importers are still skipping someone the app has un-hidden. NC-150.
    """
    name = (name or "").strip()
    if not path or not name:
        return False
    if not os.path.exists(path):
        return True
    target = normalize_name(name)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        kept = [
            line for line in lines
            if normalize_name(line.split("#", 1)[0].strip()) != target
            or not line.split("#", 1)[0].strip()
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
    except OSError:
        return False
    return True


def mirror_excluded_to_file(conn, path: str) -> int:
    """Write any excluded contact's name that the exclude file is missing.

    NC-146. There are two copies of "people to leave alone": the `excluded`
    column, which the app reads, and exclude_list.txt, which the ten importers
    read. They are one list only while every write to either reaches the other,
    and deleting someone wrote the column and not the file — so people the app
    had been told to forget were still being collected by the importers.

    Idempotent, and deliberately one-directional: the file already flows back
    into the column through apply_exclude_list(), and a name in the file with no
    contact behind it is a name the owner typed, not a bug to clean up. Returns
    how many names it added.
    """
    have = load_exclude_list(path)
    added = 0
    for row in conn.execute(
            f"SELECT name FROM contacts WHERE {figures.population_where('hidden')}"):
        name = (row["name"] or "").strip()
        if not name:
            continue
        norm = normalize_name(name)
        if norm in have:
            continue
        if not add_to_exclude_file(path, name):
            continue          # NC-150: never count a line that was not written
        have.add(norm)
        added += 1
    return added


def apply_exclude_list(conn, norm_names: set[str]) -> int:
    """Mark every contact whose normalized name is in the set as excluded.
    Returns how many were affected."""
    n = 0
    for norm in norm_names:
        cur = conn.execute(
            "UPDATE contacts SET excluded = 1 WHERE name_norm = ? AND excluded = 0", (norm,)
        )
        n += cur.rowcount
    return n


def counts(conn) -> dict:
    row = conn.execute(
        """SELECT
             COUNT(*) AS total,
             SUM(status = 'new')       AS new,
             SUM(status = 'described') AS described,
             SUM(status = 'skipped')   AS skipped
           FROM contacts"""
    ).fetchone()
    return {k: (row[k] or 0) for k in ("total", "new", "described", "skipped")}


def source_coverage(conn) -> dict:
    """How much each import source has actually contributed, in one query.

    Feeds the setup screen's "what did that step buy me" panel (api_setup.py).
    Lives here rather than there because db.py owns the contacts table — the same
    reason counts() is here.

    `history` is the headline number and the only one that took thought: a person
    you have *any* real conversation with, on any channel. Contacts alone can add
    thousands of names and change it by zero, which is exactly the point — it is
    what separates an address book from a memory.

    NC-122: every figure here counts `active` — the people you can see and use,
    the same people the People page lists. It used to count `visible`, which
    still counted a person you deleted without hiding, so "people in total" at
    the top of the Data page and "people" at the bottom of it could differ by
    exactly those people.
    """
    row = conn.execute(
        f"""SELECT
             COUNT(*)                                        AS people,
             SUM(imsg_count > 0)                             AS imessage,
             SUM(wa_count > 0)                               AS whatsapp,
             SUM(em_count > 0)                               AS email,
             SUM(TRIM(COALESCE(linkedin_url, '')) <> '')     AS linkedin,
             -- NC-091: this counted `photo_file_id`, the retired Telegram bot's
             -- picture handle, which is 0 for every row now that the bot is
             -- gone. The column that holds a real picture is
             -- `photo`. NC-122: api_import, api_instance and api_provenance
             -- count it over the same `active` people as this, so the four
             -- photo figures are one figure.
             SUM(TRIM(COALESCE(photo, '')) <> '')            AS photos,
             SUM(TRIM(COALESCE(position, '')) <> ''
                 OR TRIM(COALESCE(company, '')) <> '')       AS titles,
             SUM(imsg_count > 0 OR wa_count > 0 OR em_count > 0
                 OR tg_count > 0 OR msgr_count > 0
                 OR ig_count > 0 OR msg_count > 0)           AS history
           FROM contacts
           WHERE {figures.population_where('active')}"""
    ).fetchone()
    out = {k: (row[k] or 0) for k in (
        "people", "imessage", "whatsapp", "email", "linkedin",
        "photos", "titles", "history")}
    # Meetings are their own table, so they need their own pass.
    #
    # NC-076: this used to be an unfiltered COUNT(DISTINCT contact_id) over the
    # whole table, and `contact_dates` holds four kinds (birthday, anniversary,
    # meeting, custom) from two sources (manual, calendar). So a birthday the
    # ADDRESS BOOK import wrote counted as evidence the CALENDAR step had landed:
    # a person with only an address-book birthday looked like a person with a
    # calendar meeting.
    # An install that had never touched a calendar could therefore show Calendar
    # as a connected source. Both halves of the filter are load-bearing: `kind`
    # keeps birthdays out, `source` keeps the step's count about the step.
    #
    # NC-201: and the same population as every figure above it. This query never
    # joined `contacts`, so a hidden person's meeting counted toward what the
    # Calendar step added while their messages and photo did not.
    out["meetings"] = conn.execute(
        "SELECT COUNT(DISTINCT d.contact_id) FROM contact_dates d "
        "JOIN contacts c ON c.id = d.contact_id "
        "WHERE d.kind = 'meeting' AND d.source = 'calendar' "
        f"AND {figures.population_where('active', 'c')}"
    ).fetchone()[0] or 0
    return out


# Map an importer's channel name to its (count, last) column pair.
CHANNEL_COLUMNS = {
    "email":     ("em_count", "em_last"),
    "telegram":  ("tg_count", "tg_last"),
    "messenger": ("msgr_count", "msgr_last"),
    "instagram": ("ig_count", "ig_last"),
}

# Every per-channel (count, last-seen) column pair, including the three that
# predate bump_channel (LinkedIn / iMessage / WhatsApp). Used by reset_channel
# and channel_watermark so re-imports can be made idempotent / incremental.
ALL_CHANNEL_COLUMNS = {
    "linkedin":  ("msg_count", "last_contact"),
    "imessage":  ("imsg_count", "imsg_last"),
    "whatsapp":  ("wa_count", "wa_last"),
    **CHANNEL_COLUMNS,
}


def reset_channel(conn, channel: str) -> None:
    """Zero a channel's count + last-seen across ALL contacts.

    Importers add to counts (a person can have several handles/addresses), so a
    full re-import calls this first — then re-adding from the archive rebuilds the
    exact current total instead of doubling it. Only the auto-derived count/date
    columns are touched; notes, categories, how-we-met and review status are not.
    """
    cols = ALL_CHANNEL_COLUMNS.get(channel)
    if cols is None:
        raise ValueError(f"unknown channel: {channel}")
    count_col, last_col = cols
    conn.execute(f"UPDATE contacts SET {count_col} = 0, {last_col} = ''")


def channel_watermark(conn, channel: str) -> str:
    """The latest message date already recorded for a channel (max of its
    last-seen column across contacts), or '' if none. A subsequent sync only needs
    to look at messages strictly after this to add what's new."""
    cols = ALL_CHANNEL_COLUMNS.get(channel)
    if cols is None:
        raise ValueError(f"unknown channel: {channel}")
    _count_col, last_col = cols
    row = conn.execute(
        f"SELECT MAX({last_col}) AS w FROM contacts WHERE {last_col} <> ''"
    ).fetchone()
    return (row["w"] or "") if row else ""


def bump_channel(conn, contact_id: int, channel: str, count: int, last_at: str) -> None:
    """Add `count` messages to a contact on `channel` and advance its last-seen date.

    The count is summed (a person may have several handles or addresses) and the
    date keeps the latest of the existing and incoming values. Corrected (NC-204):
    this said it mirrors how imessage_import.apply writes imsg_count. Nothing
    there writes it now; WhatsApp, iMessage and LinkedIn are recounted from the
    archive by engagement.recount_channels. `channel` is one of email|telegram|messenger|
    instagram; anything else raises ValueError.
    """
    cols = CHANNEL_COLUMNS.get(channel)
    if cols is None:
        raise ValueError(f"unknown channel: {channel}")
    count_col, last_col = cols
    row = get_contact(conn, contact_id)
    new_last = max((row[last_col] if row else "") or "", last_at or "")
    conn.execute(
        f"UPDATE contacts SET {count_col} = {count_col} + ?, {last_col} = ? WHERE id = ?",
        (count, new_last, contact_id),
    )


def set_channel(conn, contact_id: int, channel: str, count: int, last_at: str) -> None:
    """Overwrite a contact's count + last-seen on `channel` (vs bump_channel which
    adds). Importers that aggregate the full per-contact total before writing use
    this so a re-import recomputes the same value instead of doubling it. Works for
    any channel in ALL_CHANNEL_COLUMNS. `last_at` is taken as-is (the caller already
    knows the archive's latest); pass the per-contact max."""
    cols = ALL_CHANNEL_COLUMNS.get(channel)
    if cols is None:
        raise ValueError(f"unknown channel: {channel}")
    count_col, last_col = cols
    conn.execute(
        f"UPDATE contacts SET {count_col} = ?, {last_col} = ? WHERE id = ?",
        (count, last_at or "", contact_id),
    )


# ── Notes ───────────────────────────────────────────────────────────────────
def add_note(conn, contact_id: int, body: str, kind: str = "note") -> int:
    cur = conn.execute(
        "INSERT INTO notes (contact_id, body, kind, updated_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (contact_id, body.strip(), kind),
    )
    conn.execute("UPDATE contacts SET updated_at = datetime('now') WHERE id = ?", (contact_id,))
    return cur.lastrowid


def get_notes(conn, contact_id: int):
    return conn.execute(
        "SELECT * FROM notes WHERE contact_id = ? AND deleted = 0 ORDER BY created_at DESC, id DESC",
        (contact_id,),
    ).fetchall()


def update_note(conn, note_id: int, body: str):
    conn.execute(
        "UPDATE notes SET body = ?, updated_at = datetime('now') WHERE id = ?",
        (body.strip(), note_id),
    )


def delete_note(conn, note_id: int):
    # Soft-delete so sync tombstones propagate; hard-delete for contact wipe is done inline
    conn.execute(
        "UPDATE notes SET deleted = 1, updated_at = datetime('now') WHERE id = ?",
        (note_id,),
    )


# ── Ask/Chat history ──────────────────────────────────────────────────────────
# A chat's title is DERIVED from its first question. Never LLM-generated: Ask has
# to work with zero API keys (invariant 8), so a label that needed a key would be
# the one part of the page that silently disappeared without one — and paying an
# API bill to name a thread the user already named by typing it is wrong.
CHAT_TITLE_MAX = 48                  # characters of question kept in a title
CHAT_TITLE_FALLBACK = "New chat"     # a chat with nothing to derive from


def chat_title(text: str) -> str:
    """A chat's title, derived from the question that started it. Pure.

    Collapse whitespace, drop a trailing question mark ("who do I know in Berlin?"
    is a title, the "?" is not), then cut to CHAT_TITLE_MAX on a WORD boundary and
    mark the cut with an ellipsis, so a long first message reads as a truncated
    title rather than as a title that happens to stop mid-word. Anything with no
    usable text left — empty, whitespace, "???" — falls back to CHAT_TITLE_FALLBACK
    instead of returning "".

    Pure and total on purpose: it takes no connection and reads no environment, so
    ensure_columns() can call it during a migration (which is why it lives in db.py
    and not in api_ask.py — db.py cannot import api_ask), and no input can make it
    raise. Nothing here is tunable; the size is a display constant, not a policy.
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = t.rstrip("?").strip()
    if not t:
        return CHAT_TITLE_FALLBACK
    if len(t) <= CHAT_TITLE_MAX:
        return t
    cut = t[:CHAT_TITLE_MAX]
    space = cut.rfind(" ")
    if space >= CHAT_TITLE_MAX // 2:     # only if a word boundary is actually near
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


CHAT_ADOPTION_STATE_KEY = "chat_adoption"   # receipt for the one-time upgrade


def _adopt_orphan_chat_history(conn) -> Dict[str, Any]:
    """Adopt every pre-chats chat_history row into ONE conversation.

    Called from ensure_columns(), so it runs on every init_db (invariant 9) and is
    idempotent: after it runs there are no rows with conversation_id IS NULL, so
    the next call finds nothing and writes nothing.

    The existing transcript IS a conversation — one long one. So it becomes a real
    chat rather than being deleted or left unreachable: titled from the first
    question in it, and stamped with the real MIN/MAX created_at of the rows it
    holds, so it sorts by when it actually happened instead of by the moment the
    user happened to install the update. Nothing is lost and no click is needed.

    Returns the receipt, and also parks it in `state` under
    CHAT_ADOPTION_STATE_KEY so the Ask view can say where the old transcript went
    (GET /api/chats carries it). A migration that silently reorganises the user's
    own words would be the same defect as a cap nobody can see.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
        "FROM chat_history WHERE conversation_id IS NULL"
    ).fetchone()
    orphans = (row["n"] if row else 0) or 0
    if not orphans:
        return {"adopted": 0, "conversation_id": 0, "title": ""}
    seed = conn.execute(
        "SELECT content FROM chat_history WHERE conversation_id IS NULL AND role = 'user' "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if seed is None:      # an assistant-only transcript is odd but must still adopt
        seed = conn.execute(
            "SELECT content FROM chat_history WHERE conversation_id IS NULL "
            "ORDER BY id LIMIT 1"
        ).fetchone()
    title = chat_title(seed["content"] if seed else "")
    first_at = row["first_at"] or now_iso()
    last_at = row["last_at"] or first_at
    cur = conn.execute(
        "INSERT INTO chat_conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
        (title, first_at, last_at),
    )
    conversation_id = cur.lastrowid
    adopted = conn.execute(
        "UPDATE chat_history SET conversation_id = ? WHERE conversation_id IS NULL",
        (conversation_id,),
    ).rowcount
    receipt = {"adopted": adopted, "conversation_id": conversation_id, "title": title,
               "created_at": first_at, "updated_at": last_at}
    state_set(conn, CHAT_ADOPTION_STATE_KEY, json.dumps(receipt))
    return receipt


def create_conversation(conn, title: str = "", shape_json: str = "") -> int:
    """A new, empty chat. `title` is stored as given (already derived by the
    caller, or '' to be named by the first question — see
    title_conversation_if_unset)."""
    cur = conn.execute(
        "INSERT INTO chat_conversations (title, shape_json, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        ((title or "").strip(), shape_json or ""),
    )
    return cur.lastrowid


def get_conversation(conn, conversation_id: int):
    return conn.execute(
        "SELECT * FROM chat_conversations WHERE id = ? AND deleted = 0",
        (conversation_id,),
    ).fetchone()


def get_conversations(conn):
    """Every live chat, most recently active first, with its turn count.

    `turns` is a COUNT over chat_history, so the list can never claim a number the
    transcript disagrees with. Ordered by last real activity, falling back to the
    chat's own stamp so a brand-new empty chat still appears at the top — and so
    renaming a chat does not shuffle it up the list.
    """
    return conn.execute(
        """SELECT c.id, c.title, c.shape_json, c.created_at, c.updated_at,
                  COUNT(h.id) AS turns, MAX(h.created_at) AS last_message_at
           FROM chat_conversations c
                LEFT JOIN chat_history h ON h.conversation_id = c.id
           WHERE c.deleted = 0
           GROUP BY c.id
           ORDER BY COALESCE(MAX(h.created_at), c.updated_at) DESC, c.id DESC"""
    ).fetchall()


def newest_conversation_id(conn):
    """The most recently active live chat, or None when there are no chats at all.

    Same ordering as get_conversations, so "the newest chat" means the same thing
    to the legacy /api/chat/history aliases as it does to the chat list.
    """
    row = conn.execute(
        """SELECT c.id
           FROM chat_conversations c
                LEFT JOIN chat_history h ON h.conversation_id = c.id
           WHERE c.deleted = 0
           GROUP BY c.id
           ORDER BY COALESCE(MAX(h.created_at), c.updated_at) DESC, c.id DESC
           LIMIT 1"""
    ).fetchone()
    return row["id"] if row else None


def rename_conversation(conn, conversation_id: int, title: str) -> str:
    """Rename a chat. Returns the title actually stored — chat_title() normalises
    a hand-typed name the same way it derives one, so the list never has to render
    a 300-character 'title'."""
    name = chat_title(title)
    conn.execute(
        "UPDATE chat_conversations SET title = ?, updated_at = datetime('now') WHERE id = ?",
        (name, conversation_id),
    )
    return name


def title_conversation_if_unset(conn, conversation_id: int, text: str) -> str:
    """Name a chat from its first question. Returns the live title.

    Only fires while the chat is still unnamed ('' or the fallback) AND holds no
    more than one user turn — the one just written. So the first question names the
    thread, later questions do not rewrite it, and a title the user typed themselves
    is never overwritten.
    """
    row = get_conversation(conn, conversation_id)
    if row is None:
        return ""
    current = (row["title"] or "").strip()
    if current and current != CHAT_TITLE_FALLBACK:
        return current
    turns = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_history WHERE conversation_id = ? AND role = 'user'",
        (conversation_id,),
    ).fetchone()["n"] or 0
    if turns > 1:
        return current or CHAT_TITLE_FALLBACK
    title = chat_title(text)
    conn.execute("UPDATE chat_conversations SET title = ? WHERE id = ?",
                 (title, conversation_id))
    return title


def touch_conversation(conn, conversation_id: int, shape_json: str | None = None) -> None:
    """Bump a chat's updated_at, and persist the answer shape it just ran with.

    The shape used to be per-request and unpersisted, so a reloaded turn showed the
    client's CURRENT dropdown rather than the shape that produced the answer. It
    has a column now, one per chat, so reopening a thread restores the controls it
    was last answered with.
    """
    if shape_json is None:
        conn.execute("UPDATE chat_conversations SET updated_at = datetime('now') WHERE id = ?",
                     (conversation_id,))
        return
    conn.execute(
        "UPDATE chat_conversations SET shape_json = ?, updated_at = datetime('now') WHERE id = ?",
        (shape_json, conversation_id),
    )


def delete_conversation(conn, conversation_id: int) -> None:
    """Soft-delete, like delete_list and delete_note: the thread leaves the list
    and its turns stay on disk, so a mis-click is recoverable and a future sync
    can carry the tombstone. The caller reports how many turns went with it."""
    conn.execute(
        "UPDATE chat_conversations SET deleted = 1, updated_at = datetime('now') WHERE id = ?",
        (conversation_id,),
    )


def add_chat_message(conn, role: str, content: str, contacts_json: str = "",
                     truncated: bool = False, conversation_id: int | None = None,
                     receipt_json: str = "", ask_json: str = "") -> int:
    """Append one turn. `conversation_id=None` writes an unassigned row, which
    only pre-chats callers do — ensure_columns() adopts any such row into a real
    chat on the next init_db rather than leaving it invisible.

    `receipt_json` is NC-155: what the answer cost and what it called. `ask_json`
    is NC-156: the question this turn asked back and the options it offered, when
    it ended with a question instead of an answer. Both defaulted so every
    existing caller — the user turn, the tests, the CLI — keeps working without
    passing something it does not have."""
    cur = conn.execute(
        "INSERT INTO chat_history (role, content, contacts_json, truncated, "
        "                          conversation_id, receipt_json, ask_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (role, content, contacts_json or "", 1 if truncated else 0, conversation_id,
         receipt_json or "", ask_json or ""),
    )
    return cur.lastrowid


def get_chat_history(conn, limit: int = 20, conversation_id: int | None = None):
    """Most recent `limit` messages, oldest first (ready to feed straight into
    an LLM conversation or render top-to-bottom in the UI).

    `conversation_id` scopes it to one chat. None means NO filter — every row,
    exactly as this function behaved before Ask had chats — which is what keeps
    the legacy /api/chat/history alias honest on a database whose rows have not
    been adopted into a chat yet.

    `limit` is the caller's decision and the two callers now pass different
    numbers: the LLM context window (chat_history_context) and the rendered
    transcript (chat_transcript_max) are separate settings. See api_ask.py.
    """
    where, params = "", []
    if conversation_id is not None:
        where = "WHERE conversation_id = ? "
        params.append(int(conversation_id))
    params.append(limit)
    rows = conn.execute(
        "SELECT id, conversation_id, role, content, created_at, contacts_json, "
        "       truncated, receipt_json, ask_json "
        "FROM chat_history " + where + "ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return list(reversed(rows))


def last_assistant_message(conn, conversation_id: int | None = None):
    """The newest assistant turn of one chat, or None. NC-160.

    One row, asked for directly. The caller used to answer this by scanning the
    window it was already feeding the model — which made a rule about what Ask
    is ALLOWED to do depend on NC_CHAT_HISTORY_CONTEXT, a knob whose only job is
    to trade memory for money. Set that low enough and the window no longer
    reached the previous answer, so the rule silently stopped applying.

    Scoped by conversation_id like every other read here; None means no filter,
    which is the pre-chats behaviour the legacy routes still rely on.
    """
    where, params = "", []
    if conversation_id is not None:
        where = "AND conversation_id = ? "
        params.append(int(conversation_id))
    return conn.execute(
        "SELECT id, conversation_id, role, content, created_at, contacts_json, "
        "       truncated, receipt_json, ask_json "
        "FROM chat_history WHERE role = 'assistant' " + where +
        "ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()


def count_chat_messages(conn, conversation_id: int | None = None) -> int:
    """How many turns exist — the number a `limit` gets compared against, so a
    trimmed transcript can say what it trimmed instead of just looking short."""
    if conversation_id is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM chat_history").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM chat_history WHERE conversation_id = ?",
                           (int(conversation_id),)).fetchone()
    return (row["n"] if row else 0) or 0


def clear_chat_history(conn, conversation_id: int | None = None) -> int:
    """Delete turns and return how many went. `conversation_id=None` clears every
    row (the pre-chats behaviour); an id clears just that chat and leaves the
    thread itself in the list, empty."""
    if conversation_id is None:
        cur = conn.execute("DELETE FROM chat_history")
    else:
        cur = conn.execute("DELETE FROM chat_history WHERE conversation_id = ?",
                           (int(conversation_id),))
    return cur.rowcount


def _contact_filters(q: str = "", flt: str = "all", location: str = "",
                     role: str = "", topic: str = ""):
    """The WHERE clause behind both list_contacts and count_contacts. Shared so
    the count can never disagree with the list it is counting."""
    where, params = [], []
    if flt == "todo":
        where.append(f"status = 'new' AND {figures.population_where('visible')}")
    elif flt == "described":
        where.append(f"status = 'described' AND {figures.population_where('visible')}")
    elif flt == "deleted":
        where.append(figures.population_where("deleted"))
    elif flt == "excluded":
        # excluded-but-not-deleted — soft-deleted rows have their own view
        where.append(figures.population_where("hidden_only"))
    else:
        # default 'all'/'Everyone' (and the ⌘K jump query, which sends no filter)
        # must also hide people you explicitly Excluded — every other search path
        # already filters them, this one silently did not.
        #
        # NC-087: this used to be a bare `excluded = 0` with the comment "deleted
        # rows are excluded=1 too, so this hides them as well" — a real dependency
        # on a convention that ONE function maintains. `db.set_deleted` sets both
        # columns; cloud sync writes them independently. Give it one row where
        # they disagree and the People page lists a person the owner deleted, while
        # Settings' own count of the same population does not — which is how the
        # two came to differ in the first place.
        #
        # So it names the population instead of relying on the convention. Where
        # no row is deleted-without-being-hidden this changes no number on
        # screen; it removes a way for one to change without anyone touching a
        # contact.
        where.append(figures.population_where("active"))
    if q:
        where.append("(name_norm LIKE ? OR lower(company) LIKE ? OR lower(position) LIKE ?)")
        params += [f"%{normalize_name(q)}%", f"%{q.lower()}%", f"%{q.lower()}%"]
    if location:
        where.append("(home_metro = ? OR home_city = ?)")
        params += [location, location]
    if role:
        where.append("role_type = ?")
        params.append(role)
    if topic:
        where.append("LOWER(topics) LIKE '%'||LOWER(?)||'%'")
        params.append(topic)
    return where, params


def count_contacts(conn, q: str = "", flt: str = "all", location: str = "",
                   role: str = "", topic: str = "") -> int:
    """How many contacts match, ignoring any limit. The list is paged; without
    this the UI can only report the size of the page it happens to be holding,
    which is how thousands of contacts read as "500"."""
    where, params = _contact_filters(q, flt, location, role, topic)
    sql = "SELECT COUNT(*) FROM contacts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, params).fetchone()[0])


def list_contacts(conn, q: str = "", flt: str = "all", limit: int = 500, location: str = "",
                  role: str = "", topic: str = "", sort: str = ""):
    """List/search contacts for the desktop app. flt: all|todo|described|excluded.

    Optional facets: `role` is an exact role_type match; `topic` is a
    case-insensitive substring match inside the topics JSON-array string.

    `sort`: '' = recent-first (historic default) · 'quiet' = oldest contact
    first. The sort MUST happen here, not client-side: with LIMIT 500 over
    thousands of contacts, a client re-sort of the recent-first slice can never
    surface the genuinely quiet people the neglect view exists to show.

    `limit` is a page size, not a ceiling on the network — pair it with
    count_contacts so the caller can say which page of what it is showing."""
    where, params = _contact_filters(q, flt, location, role, topic)
    sql = "SELECT * FROM contacts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if sort == "closeness":
        # Closeness is computed in Python (relationship_strength), not stored in
        # a column — so it has to be ranked over EVERY matching row before the
        # cap. Ranking a last-contact-ordered slice instead would just put
        # whoever happens to be recent on top, which is the bug this replaces.
        rows = conn.execute(sql + " ORDER BY name ASC", params).fetchall()
        rows.sort(key=lambda r: (-relationship_strength(r), (r["name"] or "").lower()))
        return rows[:limit]
    # Effective last touch across every channel — last_contact alone is the
    # LinkedIn column, and sorting by it calls WhatsApp-yesterday people
    # "quiet". Matches what the UI displays as `last` (_last_seen).
    eff = ("MAX(last_contact, imsg_last, wa_last, em_last, tg_last, "
           "msgr_last, ig_last)")
    if sort == "quiet":
        # contacted people first, longest silence first; never-contacted
        # strangers sink to the tail (and usually past the limit — by design)
        sql += f" ORDER BY ({eff} <> '') DESC, {eff} ASC, name ASC LIMIT ?"
    elif sort == "recent":
        sql += f" ORDER BY ({eff} <> '') DESC, {eff} DESC, name ASC LIMIT ?"
    else:
        sql += " ORDER BY (last_contact <> '') DESC, last_contact DESC, name ASC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


# ── Reconnect ───────────────────────────────────────────────────────────────
def row_last_seen(row) -> str:
    """Most recent touchpoint across channels: lexicographic max of all seven
    channel date columns ('YYYY-MM-DD ...'). Empty string if the contact has none.

    The one implementation — crm/app.py's _last_seen delegates here, so the API
    and the bots can never disagree about when you last spoke to someone.
    """
    candidates = [row["last_contact"] or "", row["imsg_last"] or "", row["wa_last"] or "",
                  row["em_last"] or "", row["tg_last"] or "", row["msgr_last"] or "",
                  row["ig_last"] or ""]
    nonempty = [c for c in candidates if c]
    return max(nonempty) if nonempty else ""


_row_last_seen = row_last_seen  # back-compat alias


RECONNECT_MIN_MESSAGES = 3    # a single old InMail shouldn't count as "worth a message"
RECONNECT_SNOOZE_DAYS = 60    # how long a manual "skip" hides someone from this list


def find_reconnect(conn, months: int = 6, limit: int = 100):
    """People you have a real relationship with but haven't touched in `months`.

    A contact qualifies only if it's NOT excluded, NOT recently dismissed, and
    shows a genuine signal: status='described', ≥1 note (both are deliberate
    curation — worth trusting regardless of message volume), OR at least
    RECONNECT_MIN_MESSAGES messages on some channel (so a single old recruiter
    message doesn't surface someone you don't actually know). A contact is
    then surfaced when its most-recent touchpoint is older than the cutoff, or
    when it has no recorded touchpoint at all.

    Returns contact Rows: those with a known last_seen first, oldest (most
    overdue) at the top; contacts with no touchpoint after them, by name. The
    result is capped at `limit`.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    snooze_cutoff = (datetime.now(timezone.utc) - timedelta(days=RECONNECT_SNOOZE_DAYS)).isoformat()
    rows = conn.execute(
        f"""SELECT c.* FROM contacts c
           WHERE {figures.population_where('visible', 'c')}
             AND c.status != 'never_met'
             AND (c.reconnect_dismissed_at = '' OR c.reconnect_dismissed_at < ?)
             AND (
                   c.status = 'described'
                   OR EXISTS (SELECT 1 FROM notes n WHERE n.contact_id = c.id)
                   OR (c.msg_count + c.imsg_count + c.wa_count
                       + c.em_count + c.tg_count + c.msgr_count + c.ig_count) >= ?
                 )""",
        (snooze_cutoff, RECONNECT_MIN_MESSAGES),
    ).fetchall()

    overdue = []  # has a last_seen but it's before the cutoff
    cold = []     # no touchpoint recorded at all
    for r in rows:
        last_seen = _row_last_seen(r)
        if last_seen == "":
            cold.append(r)
        elif last_seen[:10] < cutoff:
            overdue.append((last_seen, r))

    overdue.sort(key=lambda pair: pair[0])  # oldest touchpoint first = most overdue
    cold.sort(key=lambda r: (r["name"] or "").lower())
    return ([r for _, r in overdue] + cold)[:limit]


def reconnect_due(conn, row, months: int = 6) -> bool:
    """Would this single contact appear on find_reconnect's list right now?

    The same qualification test as find_reconnect — not excluded, not
    never_met, not snoozed (RECONNECT_SNOOZE_DAYS), a genuine signal
    (described / a note / ≥ RECONNECT_MIN_MESSAGES), and a last touchpoint
    older than the cutoff (or none at all) — evaluated for one row, so the
    person page can derive its next-action card without scanning everyone."""
    if row["excluded"]:
        return False
    if (row["status"] or "") == "never_met":
        return False
    dismissed = (row["reconnect_dismissed_at"] or "") if "reconnect_dismissed_at" in row.keys() else ""
    snooze_cutoff = (datetime.now(timezone.utc) - timedelta(days=RECONNECT_SNOOZE_DAYS)).isoformat()
    if dismissed and dismissed >= snooze_cutoff:
        return False
    has_signal = (
        (row["status"] or "") == "described"
        or conn.execute("SELECT 1 FROM notes WHERE contact_id = ? LIMIT 1",
                        (row["id"],)).fetchone() is not None
        or sum((row[c] or 0) for c, _ in _STRENGTH_CHANNELS) >= RECONNECT_MIN_MESSAGES
    )
    if not has_signal:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    last_seen = _row_last_seen(row)
    return last_seen == "" or last_seen[:10] < cutoff


def dismiss_reconnect(conn, contact_id: int) -> None:
    """Snooze a contact out of the reconnect / 'Worth a message' list for a
    while (RECONNECT_SNOOZE_DAYS) rather than forever — new activity or the
    passage of time naturally brings them back if they're still relevant."""
    conn.execute(
        "UPDATE contacts SET reconnect_dismissed_at = ? WHERE id = ?",
        (now_iso(), contact_id),
    )


# ── Relationship strength ─────────────────────────────────────────────────────
# Every (count, last-seen) column pair that feeds the strength score.
_STRENGTH_CHANNELS = [
    ("msg_count", "last_contact"),   # LinkedIn
    ("imsg_count", "imsg_last"),     # iMessage
    ("wa_count", "wa_last"),         # WhatsApp
    ("em_count", "em_last"),         # Email
    ("tg_count", "tg_last"),         # Telegram
    ("msgr_count", "msgr_last"),     # Messenger
    ("ig_count", "ig_last"),         # Instagram
]


def _days_since(date_str: str):
    """Whole days since a 'YYYY-MM-DD …' string, or None if empty/unparseable."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return max(0, (datetime.now(timezone.utc) - d).days)


# Familiarity model constants — see strength_breakdown for the full story.
_DAYS_SAT = 365          # distinct conversation-days that count as "a lifetime shared"
_TENURE_SAT_DAYS = 1095  # 3 years of knowing someone maxes the tenure component
_VOLUME_SAT_LOG = 3.3    # log10 scale → volume saturates near ~2,000 messages
_MSGS_PER_DAY_EST = 4    # fallback: estimate active days from counts (≈4 msgs/day)
_RECIP_MIN_SAMPLE = 6    # need this many archived msgs before judging reciprocity
_ONE_WAY_MULT = 0.35     # a pure broadcast stream is mostly not a relationship
_WARMTH_FLOOR = 0.55     # an old close friend never loses more than 45% to silence
_DEFAULT_EXPECTED_GAP = 45.0  # days of silence that count as "in rhythm" w/o history


def _tenure_days(row) -> "int | None":
    """Days between the first and last known touchpoint, or None if unknown.
    Prefers real archived timestamps (eng_first/eng_last); falls back to
    first_contact/connected_on → last-seen from the channel columns."""
    keys = row.keys()
    first = (row["eng_first"] or "") if "eng_first" in keys else ""
    first = first or (row["first_contact"] or "") or (row["connected_on"] or "")
    last = _row_last_seen(row)
    if "eng_last" in keys and (row["eng_last"] or "") > last:
        last = row["eng_last"]
    d_first, d_last = _days_since(first), _days_since(last)
    if d_first is None or d_last is None:
        return None
    return max(0, d_first - d_last)


def strength_breakdown(row) -> dict:
    """The transparent components behind relationship_strength(row).

    Pure function of a single contact Row (no DB query). The model is

        familiarity = 100 · bond · (0.55 + 0.45 · warmth)

    **bond** — how much relationship exists at all (slow-moving):
      days     distinct calendar days you've actually exchanged messages
               (from archive.db via crm/engagement.py; estimated from channel
               counts when nothing is archived). Log-scaled, saturating at a
               year's worth — the single best "shared life" signal.
      tenure   how long you've known each other (first→last touchpoint, 3y max).
      volume   total messages, log-scaled (a supporting signal, no longer king).
      depth    your own curation: described / how-we-met / a summary.
      …then multiplied by:
      reciprocity  min/max of sent vs received. A one-way stream (newsletter,
                   support bot, cold outreach) is crushed to 0.35×; unknown
                   direction data stays neutral at 1.0.
      breadth      +8% per extra shared channel (max +24%).

    **warmth** — the current temperature, measured against the pair's OWN
    rhythm: expected gap ≈ 4× their average interval between conversation-days
    (clamped 21–240 days). Inside it → 1.0; beyond it, halves every 2.5
    expected-gaps. Warmth only modulates bond (floor 0.55), so a dear friend
    gone quiet stays clearly non-zero while a stranger who messaged yesterday
    can't ride recency to a high score.
    """
    keys = row.keys()
    total = sum((row[c] or 0) for c, _ in _STRENGTH_CHANNELS)
    channels_used = sum(1 for c, _ in _STRENGTH_CHANNELS if (row[c] or 0) > 0)

    eng_days = (row["eng_active_days"] or 0) if "eng_active_days" in keys else 0
    sent = (row["eng_sent"] or 0) if "eng_sent" in keys else 0
    recv = (row["eng_recv"] or 0) if "eng_recv" in keys else 0

    active_days = eng_days or (max(1, round(total / _MSGS_PER_DAY_EST)) if total else 0)
    tenure_days = _tenure_days(row)

    days_c = min(1.0, math.log10(active_days + 1) / math.log10(_DAYS_SAT + 1)) if active_days else 0.0
    volume_c = min(1.0, math.log10(total + 1) / _VOLUME_SAT_LOG) if total else 0.0

    depth_c = 0.0
    if (row["status"] or "") == "described":
        depth_c += 0.55
    if (row["how_we_met"] or "").strip():
        depth_c += 0.25
    brief = (row["brief"] or "") if "brief" in keys else ""
    if brief.strip() or (row["auto_summary"] or "").strip():
        depth_c += 0.20
    depth_c = min(1.0, depth_c)

    # weighted core; when tenure is unknown, renormalize over what we do know
    if tenure_days is not None:
        tenure_c = min(1.0, tenure_days / _TENURE_SAT_DAYS)
        bond_core = 0.45 * days_c + 0.15 * tenure_c + 0.20 * volume_c + 0.20 * depth_c
    else:
        tenure_c = 0.0
        bond_core = (0.45 * days_c + 0.20 * volume_c + 0.20 * depth_c) / 0.85

    if sent + recv >= _RECIP_MIN_SAMPLE:
        if sent == 0 or recv == 0:
            reciprocity = _ONE_WAY_MULT
        else:
            ratio = min(sent, recv) / max(sent, recv)
            reciprocity = 0.45 + 0.55 * math.sqrt(ratio)
    else:
        reciprocity = 1.0  # not enough direction data — don't punish the unknown

    breadth_mult = 1.0 + 0.08 * min(3, max(0, channels_used - 1))
    bond = min(1.0, bond_core * reciprocity * breadth_mult)

    days_since = _days_since(_row_last_seen(row) if "eng_last" not in keys
                             else max(_row_last_seen(row), row["eng_last"] or ""))
    if days_since is None or active_days == 0:
        warmth = 0.0
    else:
        if tenure_days and active_days:
            avg_gap = max(1.0, tenure_days / active_days)
            expected = min(240.0, max(21.0, 4.0 * avg_gap))
        else:
            expected = _DEFAULT_EXPECTED_GAP
        warmth = 1.0 if days_since <= expected else 0.5 ** ((days_since - expected) / (2.5 * expected))

    return {
        "days": days_c,
        "tenure": tenure_c,
        "volume": volume_c,
        "depth": depth_c,
        "reciprocity": reciprocity,
        "bond": bond,
        "warmth": warmth,
        "active_days": active_days,
        "total_messages": total,
        "channels_used": channels_used,
        "days_since": days_since,
    }


def relationship_strength(row) -> int:
    """A 0–100 familiarity score for a contact Row.

    bond (shared days · tenure · volume · curation, scaled by reciprocity and
    channel breadth) modulated by warmth (recency relative to the pair's own
    rhythm, floored at 0.55 so history is never erased by silence). See
    strength_breakdown for the component definitions."""
    b = strength_breakdown(row)
    return round(100 * b["bond"] * (_WARMTH_FLOOR + (1 - _WARMTH_FLOOR) * b["warmth"]))


def strength_label(score: int) -> str:
    """Coarse tier for display alongside the number."""
    if score >= 65:
        return "close"
    if score >= 40:
        return "active"
    if score >= 18:
        return "loose"
    return "faint"


def top_by_strength(conn, limit: int = 8):
    """The strongest non-excluded relationships, highest score first.

    Returns a list of (score, Row). Contacts with a zero score (no history and
    nothing curated) are left out so the dashboard shows real relationships."""
    rows = conn.execute(f"SELECT * FROM contacts WHERE {figures.population_where('visible')}").fetchall()
    scored = [(relationship_strength(r), r) for r in rows]
    scored = [(s, r) for s, r in scored if s > 0]
    scored.sort(key=lambda pair: (-pair[0], (pair[1]["name"] or "").lower()))
    return scored[:limit]


# ── Identifiers ─────────────────────────────────────────────────────────────
# facts_source precedence (invariant 4) applied to identifier rows: when one
# number arrives twice, the better-sourced spelling is the one that survives.
# Importers map onto this ladder rather than inventing a second one — a number
# typed into the person page is 'manual', anything a script read out of a file
# or a message archive is 'message_scan'/'linkedin_csv'. An unknown or missing
# source sits at the floor, so a row written before this column existed can
# never outrank a real write (nor be stuck: it ties with 'offline' and the
# spelling tiebreak decides).
# 'contacts' (the vCard address-book import) is not on invariant 4's ladder —
# it predates it and photos.SOURCE_RANK already names it — so it is placed in
# the file-backed-import tier rather than given a rank of its own.
IDENT_SOURCE_RANK = {"manual": 3, "llm": 2, "linkedin_scrape": 2,
                     "message_scan": 1, "linkedin_csv": 1, "contacts": 1,
                     "offline": 0}


def ident_source_rank(source: str) -> int:
    return IDENT_SOURCE_RANK.get((source or "").strip().lower(), 0)


def canonical_phone(value: str) -> str:
    """The one spelling of a phone number we store.

    Punctuation goes; an international access prefix ('0046…') becomes '+46…';
    a leading '+' is kept. A bare national form ('0700000000', '700000000') is
    left as-is — we cannot invent a country code that was never given, and
    guessing one from the CRM owner's locale would silently mangle the numbers
    of everyone abroad.

    phone_key() is invariant under this, which is what makes the collapse safe:
    canonicalising only drops non-digits and a leading access code, so the last
    nine digits never move.
    """
    s = (value or "").strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return s
    if s.startswith("+"):
        return "+" + digits
    # '00' is the international access code in most of the world. Only read it
    # as one when what follows is long enough to be a real international number,
    # so a short internal extension keeps its leading zeros.
    if digits.startswith("00") and len(digits) - 2 >= 9:
        return "+" + digits[2:]
    return digits


def phone_key(value: str) -> str:
    """Identity of a phone number: its last nine digits.

    Deliberately the same convention find_by_handle() and every importer match
    on (invariant 1), so two spellings that would resolve to the same contact
    also collapse to the same stored row. Numbers shorter than nine digits key
    on themselves, so two short numbers sharing a suffix stay distinct.
    """
    d = re.sub(r"\D", "", value or "")
    return d[-9:] if len(d) >= 9 else d


def _phone_precedence(value: str, source: str) -> tuple:
    """Sort key for competing spellings of one number — highest wins.

    Source first, so a lower-precedence importer can never overwrite a better
    one (invariant 4). Only then the more informative spelling: an international
    '+46…' form beats a national one, and more digits beat fewer. The value
    itself is the last tiebreak, so the winner never depends on row order.
    """
    return (ident_source_rank(source),
            1 if value.startswith("+") else 0,
            len(re.sub(r"\D", "", value)),
            value)


def _collapse_phones(conn, rows: List[Any], incoming: tuple | None = None) -> int:
    """Leave exactly one identifier row for one number. Returns rows deleted.

    `rows` are existing phone identifiers of a single contact that all share a
    phone_key; `incoming` is an optional (value, source) pair being written now.
    The best candidate by _phone_precedence is written into the lowest-id row
    and the rest are deleted — losers first, because UNIQUE(contact_id, kind,
    value) would reject the survivor's new value while a duplicate still exists.

    Idempotent: over an already-collapsed, already-canonical group it executes
    no writes at all.
    """
    candidates = [(canonical_phone(r["value"]), r["source"] or "") for r in rows]
    if incoming:
        candidates.append(incoming)
    best = max(candidates, key=lambda c: _phone_precedence(*c))
    keep = min(rows, key=lambda r: r["id"])
    removed = 0
    for r in rows:
        if r["id"] != keep["id"]:
            conn.execute("DELETE FROM identifiers WHERE id = ?", (r["id"],))
            removed += 1
    if (keep["value"], keep["source"] or "") != best:
        conn.execute("UPDATE identifiers SET value = ?, source = ? WHERE id = ?",
                     (best[0], best[1], keep["id"]))
    return removed


def dedupe_phone_identifiers(conn, contact_id: int | None = None) -> int:
    """Collapse phone rows that are one number spelled several ways.

    Called from ensure_columns() on every init_db (invariant 9) so an existing
    database heals itself with no manual step, and safe to call again for one
    contact after a bulk import. Returns the number of rows removed.
    """
    sql = "SELECT id, contact_id, value, source FROM identifiers WHERE kind = 'phone'"
    params: tuple = ()
    if contact_id is not None:
        sql += " AND contact_id = ?"
        params = (contact_id,)
    groups: Dict[tuple, List[Any]] = {}
    for r in conn.execute(sql + " ORDER BY id", params).fetchall():
        key = phone_key(r["value"])
        if key:
            groups.setdefault((r["contact_id"], key), []).append(r)
    return sum(_collapse_phones(conn, rows) for rows in groups.values())


def add_identifier(conn, contact_id: int, kind: str, value: str, *, source: str = ""):
    """Attach a phone/email/social to a contact, one row per real identifier.

    Phones are canonicalised and collapsed on write: if the contact already has
    this number under a different spelling, the two are merged into a single row
    holding the better-sourced, more informative form. Without that, LinkedIn,
    iMessage and a vCard export each wrote their own spelling of one number and
    the person page grew a pill per source.

    `source` names the writer on the facts_source ladder (see IDENT_SOURCE_RANK)
    and is upgraded — never downgraded — when an identifier is written again.
    """
    value = (value or "").strip()
    if not value:
        return
    if kind == "phone":
        value = canonical_phone(value)
        key = phone_key(value)
        if key:
            same = [r for r in conn.execute(
                "SELECT id, value, source FROM identifiers "
                "WHERE contact_id = ? AND kind = 'phone' ORDER BY id", (contact_id,),
            ).fetchall() if phone_key(r["value"]) == key]
            if same:
                _collapse_phones(conn, same, (value, source))
                return
    row = conn.execute(
        "SELECT id, source FROM identifiers WHERE contact_id = ? AND kind = ? AND value = ?",
        (contact_id, kind, value),
    ).fetchone()
    if row:
        if ident_source_rank(source) > ident_source_rank(row["source"]):
            conn.execute("UPDATE identifiers SET source = ? WHERE id = ?", (source, row["id"]))
        return
    conn.execute(
        "INSERT OR IGNORE INTO identifiers (contact_id, kind, value, source) VALUES (?, ?, ?, ?)",
        (contact_id, kind, value, source),
    )


# Kinds the app keeps only to recognise someone on the next import. They are
# nobody's phone number or address, so no page, list or export shows them.
# whatsapp_id is WhatsApp's hidden id ("<digits>@lid"), which stands in for a
# phone number the app is not told (NC-243).
HIDDEN_KINDS = ("whatsapp_id",)


def get_identifiers(conn, contact_id: int):
    marks = ",".join("?" * len(HIDDEN_KINDS))
    return conn.execute(
        f"SELECT kind, value FROM identifiers WHERE contact_id = ? "
        f"AND kind NOT IN ({marks}) ORDER BY kind", (contact_id, *HIDDEN_KINDS)
    ).fetchall()


def find_by_whatsapp_id(conn, jid: str):
    """The person a WhatsApp hidden id ("<digits>@lid") was matched to before."""
    return conn.execute(
        "SELECT c.* FROM contacts c JOIN identifiers i ON i.contact_id = c.id "
        "WHERE i.kind = 'whatsapp_id' AND i.value = ? AND NOT c.excluded LIMIT 1",
        (jid,),
    ).fetchone()


def delete_identifier(conn, contact_id: int, kind: str, value: str):
    """Remove one phone/email from a contact. Safe: archived messages are stored
    by contact_id, not by identifier, so dropping the mapping never orphans
    history — it just stops a wrong/outdated handle from re-matching on import.

    Phones are matched on phone_key, so a removal lands whichever spelling the
    caller sends — including a pre-canonicalisation one from a stale open tab."""
    value = (value or "").strip()
    if kind == "phone" and phone_key(value):
        key = phone_key(value)
        for r in conn.execute(
            "SELECT id, value FROM identifiers WHERE contact_id = ? AND kind = 'phone'",
            (contact_id,),
        ).fetchall():
            if phone_key(r["value"]) == key:
                conn.execute("DELETE FROM identifiers WHERE id = ?", (r["id"],))
        return
    conn.execute(
        "DELETE FROM identifiers WHERE contact_id = ? AND kind = ? AND value = ?",
        (contact_id, kind, value),
    )


# ── Merge ───────────────────────────────────────────────────────────────────
def merge_contacts(conn, keep_id: int, dup_id: int):
    """Fold dup into keep: move notes + identifiers, backfill empty fields, then
    delete the duplicate. Note timelines are combined, never lost."""
    if keep_id == dup_id:
        return
    conn.execute("UPDATE notes SET contact_id = ? WHERE contact_id = ?", (keep_id, dup_id))
    # Move identifiers through add_identifier so the merge collapses spellings
    # too: folding a duplicate is exactly how one person ends up holding the
    # same number in +46 and national form. fetchall() first — add_identifier
    # writes to the table we are reading.
    for r in conn.execute(
        "SELECT kind, value, source FROM identifiers WHERE contact_id = ?", (dup_id,)
    ).fetchall():
        add_identifier(conn, keep_id, r["kind"], r["value"], source=r["source"] or "")
    keep = get_contact(conn, keep_id)
    dup = get_contact(conn, dup_id)
    for f in ("company", "position", "linkedin_url", "connected_on", "category",
              "how_we_met", "photo_file_id", "auto_summary", "first_contact", "last_contact",
              # enrichment facts were previously DROPPED on merge, so the
              # discarded record's location/role/blurb/story were lost even when
              # the kept record was bare. Backfill them too, non-empty wins.
              "home_city", "home_country", "home_metro", "role_type", "blurb",
              "facts_source", "facts_updated_at", "li_extra", "li_extra_updated_at",
              "brief", "brief_updated_at", "brief_meta"):
        if (not keep[f]) and dup[f]:
            conn.execute(f"UPDATE contacts SET {f} = ? WHERE id = ?", (dup[f], keep_id))
    # topics is a JSON array — treat '' and '[]' as empty when backfilling
    kt, dt = (keep["topics"] or "").strip(), (dup["topics"] or "").strip()
    if kt in ("", "[]") and dt not in ("", "[]"):
        conn.execute("UPDATE contacts SET topics = ? WHERE id = ?", (dt, keep_id))
    # message stats belong to the same person across handles → sum counts, keep
    # the most recent date for each channel
    for cnt, last in (("msg_count", None), ("imsg_count", "imsg_last"), ("wa_count", "wa_last"),
                      ("em_count", "em_last"), ("tg_count", "tg_last"),
                      ("msgr_count", "msgr_last"), ("ig_count", "ig_last")):
        total = (keep[cnt] or 0) + (dup[cnt] or 0)
        conn.execute(f"UPDATE contacts SET {cnt} = ? WHERE id = ?", (total, keep_id))
        if last:
            newest = max(keep[last] or "", dup[last] or "")
            conn.execute(f"UPDATE contacts SET {last} = ? WHERE id = ?", (newest, keep_id))
    # engagement features: sum the counters, keep the widest date span. Distinct
    # active days can overlap so the sum is an upper bound — the next
    # engagement.refresh() recomputes exact values from the archive.
    for col in ("eng_active_days", "eng_sent", "eng_recv", "eng_recent90"):
        conn.execute(f"UPDATE contacts SET {col} = ? WHERE id = ?",
                     ((keep[col] or 0) + (dup[col] or 0), keep_id))
    eng_first = min(filter(None, [keep["eng_first"], dup["eng_first"]]), default="")
    eng_last = max(keep["eng_last"] or "", dup["eng_last"] or "")
    conn.execute("UPDATE contacts SET eng_first = ?, eng_last = ? WHERE id = ?",
                 (eng_first, eng_last, keep_id))
    # list memberships follow the kept contact (dup's status wins only where
    # the kept contact wasn't already on the list)
    for r in conn.execute("SELECT list_id, status FROM list_members WHERE contact_id = ?", (dup_id,)):
        conn.execute(
            "INSERT OR IGNORE INTO list_members (list_id, contact_id, status, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (r["list_id"], keep_id, r["status"]),
        )
    conn.execute("DELETE FROM list_members WHERE contact_id = ?", (dup_id,))
    # Dates move too. They used to be lost outright: contact_dates references
    # contacts ON DELETE CASCADE and PRAGMA foreign_keys is ON, so deleting the
    # duplicate silently took its birthdays and anniversaries with it — no
    # orphans left behind to notice afterwards. UNIQUE(contact_id, kind, on_date,
    # label) makes OR IGNORE drop only the ones the kept contact already has.
    #
    # `source` and `source_uid` MUST be in this list (NC-050). They were once
    # missing, and because `source` defaults to 'manual' the omission did not
    # lose the column — it FORGED it. Every calendar meeting and imported
    # birthday that went through a merge came out the other side claiming the owner
    # had typed it themselves. Reproduction:
    #
    #     upsert_birthday(dup, "2000-01-01", source="contacts")  -> created
    #     before merge:  ('birthday', '2000-01-01', 'contacts')
    #     after  merge:  ('birthday', '2000-01-01', 'manual')      <- forged
    #     next sync tries to correct the year                -> refused_manual
    #
    # That last line is the damage. Invariant 4 puts `manual` at the top of the
    # precedence ladder precisely so an importer can never overwrite something a
    # human set, so a date promoted to 'manual' by a merge is **frozen against
    # every future correction**, permanently — and upsert_birthday reports it as
    # "left as you set them", about a date they never set. Silent, one-way, and
    # indistinguishable afterwards from a real hand edit.
    #
    # source_uid matters for a second reason: idx_cdates_uid is how ics_import
    # recognises an event it has already seen. Dropping it means a meeting that
    # later MOVES arrives as a new row instead of updating the old one. The index
    # is deliberately not UNIQUE (one event legitimately produces one row per
    # matched attendee), so carrying the value through cannot collide.
    conn.execute(
        """INSERT OR IGNORE INTO contact_dates
               (contact_id, kind, label, on_date, year_known, recurring,
                source, source_uid)
           SELECT ?, kind, label, on_date, year_known, recurring,
                  source, source_uid
             FROM contact_dates WHERE contact_id = ?""",
        (keep_id, dup_id),
    )
    # photo: keep's photo wins unless dup's came from a better source
    import photos as _photos
    _photos.move_photo(conn, dup_id, keep_id)
    conn.execute("DELETE FROM identifiers WHERE contact_id = ?", (dup_id,))
    conn.execute("DELETE FROM contacts WHERE id = ?", (dup_id,))
    conn.execute("UPDATE contacts SET updated_at = datetime('now') WHERE id = ?", (keep_id,))
    _move_archived_messages(dup_id, keep_id)


def _move_archived_messages(dup_id: int, keep_id: int) -> None:
    """Re-point the duplicate's archived messages at the kept contact.

    Lives here rather than in the HTTP handler so every caller gets it — the
    command-line tools merge through merge_contacts() directly, and without
    this their merges leave history, episodes and engagement orphaned under an
    id that no longer exists.

    A separate database, so it is deliberately best-effort: a locked or absent
    archive must never fail a merge that has already been applied to crm.db."""
    try:
        import archive as _arc
        if not os.path.exists(_arc.ARCHIVE_DB_PATH):
            return
        arc_conn = _arc.get_archive_db(_arc.ARCHIVE_DB_PATH)
        try:
            arc_conn.execute("UPDATE messages SET contact_id = ? WHERE contact_id = ?",
                             (keep_id, dup_id))
            arc_conn.execute("UPDATE chunks SET contact_id = ? WHERE contact_id = ?",
                             (keep_id, dup_id))
            arc_conn.commit()
        finally:
            arc_conn.close()
    except Exception:  # noqa: BLE001 — sqlite3.Error, OSError, import problems
        pass


# ── Duplicate detection ───────────────────────────────────────────────────────
def find_duplicate_groups(conn):
    """Find likely-duplicate contacts and return them grouped, largest-first.

    Two signals link contacts into the same group:
      (a) identical name_norm shared by 2+ non-excluded contacts, and
      (b) the same identifier value (case-insensitive) attached to 2+ contacts.
    Signals are merged transitively via union-find, so if A==B by name and B==C
    by phone, all three land in one group. Groups of size 1 are dropped.

    Cost is one scan of contacts plus one scan of identifiers — no fuzzy/O(n²)
    matching. Each group is a list of contact Rows ordered by id.
    """
    parent: Dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    rows = conn.execute(
        "SELECT id, name_norm, excluded FROM contacts ORDER BY id"
    ).fetchall()
    for r in rows:
        parent.setdefault(r["id"], r["id"])

    # (a) identical normalized names among non-excluded contacts
    by_name: Dict[str, List[int]] = {}
    for r in rows:
        if r["excluded"]:
            continue
        norm = r["name_norm"] or ""
        if norm:
            by_name.setdefault(norm, []).append(r["id"])
    for ids in by_name.values():
        for other in ids[1:]:
            union(ids[0], other)

    # (b) shared identifier value, compared case-insensitively
    by_value: Dict[str, List[int]] = {}
    for r in conn.execute("SELECT contact_id, value FROM identifiers"):
        cid = r["contact_id"]
        if cid not in parent:  # identifier dangling past a deleted contact
            continue
        key = (r["value"] or "").strip().lower()
        if key:
            by_value.setdefault(key, []).append(cid)
    for ids in by_value.values():
        uniq = list(dict.fromkeys(ids))  # collapse repeats per contact, keep order
        for other in uniq[1:]:
            union(uniq[0], other)

    # collect members per root, then materialize only multi-member groups
    members: Dict[int, List[int]] = {}
    for cid in parent:
        members.setdefault(find(cid), []).append(cid)

    groups: List[List[Any]] = []
    for ids in members.values():
        if len(ids) < 2:
            continue
        ids.sort()
        placeholders = ",".join("?" * len(ids))
        contacts = conn.execute(
            f"SELECT * FROM contacts WHERE id IN ({placeholders}) ORDER BY id", ids
        ).fetchall()
        groups.append(contacts)

    groups.sort(key=lambda g: (-len(g), g[0]["id"]))
    return groups


# ── Saved lists / event pipelines ────────────────────────────────────────────
LIST_MEMBER_STATUSES = {"", "invited", "replied", "attending", "declined"}


def create_list(conn, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO lists (name, updated_at) VALUES (?, datetime('now'))",
        (name.strip(),))
    return cur.lastrowid


# ── The ONE definition of a list's pipeline counts ───────────────────────────
# NC-089.
#
# These four numbers used to be computed twice: here, for the rail down the left
# of the Lists page, and again IN THE BROWSER (static/app/lists.js) for the
# detail pane — which sits directly BESIDE the rail in the same flex row, with no
# scrolling between them. The two definitions disagreed, in the same four words.
#
# `replied` here is CUMULATIVE: someone who is attending has obviously replied,
# so they count. The browser counted only rows whose status is literally
# 'replied'. A list holding one member at 'attending' and none at 'replied'
# therefore made the rail read "1 replied · 1 going" and the detail pane read
# "1 going" — both true under their own rule, both on screen, for the same list.
#
# So the browser no longer counts anything: get_list_counts() below answers for
# one list with these same expressions, the detail endpoint returns it, and the
# page renders it. One owner.
#
# `_MEMBER` is deliberately the same population get_list_members() shows — a live
# membership row whose contact still exists. Soft-deleted contacts were excluded
# there and counted here, which is a SECOND way these two could drift apart. On
# a database where no list has a soft-deleted member, adding it changes no
# number on screen. It removes a way for one to change without anyone touching
# a list.
#
# What this does NOT settle, on purpose — see NC-090: whether these three labels
# should read cumulatively at all. `invited` is exclusive (someone who has since
# replied is no longer counted as invited) while `replied` is cumulative, and
# 'declined' counts as not having replied even though declining IS a reply. That
# is a question about what the words should mean, not about who owns them.
_MEMBER = f"m.status <> '__removed__' AND {figures.population_where('live', 'c')}"

_PIPELINE = f"""
             COUNT(CASE WHEN {_MEMBER} THEN m.id END)                        AS members,
             SUM(CASE WHEN {_MEMBER} AND m.status = 'invited'
                      THEN 1 ELSE 0 END)                                     AS invited,
             SUM(CASE WHEN {_MEMBER} AND m.status IN ('replied', 'attending')
                      THEN 1 ELSE 0 END)                                     AS replied,
             SUM(CASE WHEN {_MEMBER} AND m.status = 'attending'
                      THEN 1 ELSE 0 END)                                     AS attending"""

_PIPELINE_KEYS = ("members", "invited", "replied", "attending")


def get_lists(conn):
    """All non-deleted lists, newest first, with member/pipeline counts."""
    return conn.execute(
        f"""SELECT l.id, l.name, l.created_at, l.event_date, l.venue,
                   {_PIPELINE}
            FROM lists l
            LEFT JOIN list_members m ON m.list_id = l.id
            LEFT JOIN contacts c ON c.id = m.contact_id
            WHERE l.deleted = 0
            GROUP BY l.id ORDER BY l.id DESC"""
    ).fetchall()


def get_list_counts(conn, list_id: int) -> dict:
    """The same four numbers as get_lists(), for one list.

    Exists so the detail pane can render the counts rather than recomputing them
    from the member rows it happens to be holding. Same SQL, same population, so
    the rail and the pane beside it cannot disagree. A list that does not exist,
    or holds nothing, answers zeros rather than raising — the caller has already
    established the list exists.
    """
    row = conn.execute(
        f"""SELECT {_PIPELINE}
            FROM lists l
            LEFT JOIN list_members m ON m.list_id = l.id
            LEFT JOIN contacts c ON c.id = m.contact_id
            WHERE l.id = ? AND l.deleted = 0""",
        (list_id,),
    ).fetchone()
    return {k: ((row[k] or 0) if row else 0) for k in _PIPELINE_KEYS}


def get_list(conn, list_id: int):
    return conn.execute("SELECT * FROM lists WHERE id = ? AND deleted = 0", (list_id,)).fetchone()


def get_list_members(conn, list_id: int):
    """Member rows joined with their contacts, pipeline order: undecided first,
    then invited → replied → attending → declined, newest-added first within."""
    # NC-104: was `m.status != '__removed__' AND c.deleted = 0` — the same rule as
    # _MEMBER, spelled differently. The difference is not cosmetic: _MEMBER wraps
    # the column in COALESCE and this did not, so a NULL `deleted` would have put
    # a person in the list the counts beside it say is not there. A database
    # with no such NULL never showed it; that is not the same as being safe.
    return conn.execute(
        f"""SELECT c.*, m.status AS list_status, m.added_at AS list_added_at
           FROM list_members m JOIN contacts c ON c.id = m.contact_id
           WHERE m.list_id = ? AND {_MEMBER}
           ORDER BY CASE m.status WHEN '' THEN 0 WHEN 'invited' THEN 1
                    WHEN 'replied' THEN 2 WHEN 'attending' THEN 3 ELSE 4 END,
                    m.id DESC""",
        (list_id,),
    ).fetchall()


def add_list_members(conn, list_id: int, contact_ids) -> int:
    n = 0
    for cid in contact_ids:
        cur = conn.execute(
            "INSERT OR IGNORE INTO list_members (list_id, contact_id, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (list_id, int(cid)),
        )
        n += cur.rowcount
    return n


def set_list_member_status(conn, list_id: int, contact_id: int, status: str) -> None:
    if status not in LIST_MEMBER_STATUSES:
        raise ValueError(f"bad list member status: {status}")
    conn.execute(
        "UPDATE list_members SET status = ?, updated_at = datetime('now') WHERE list_id = ? AND contact_id = ?",
        (status, list_id, contact_id),
    )


def remove_list_member(conn, list_id: int, contact_id: int) -> None:
    # Soft-delete for sync propagation
    conn.execute(
        "UPDATE list_members SET updated_at = datetime('now'), status = '__removed__' WHERE list_id = ? AND contact_id = ?",
        (list_id, contact_id),
    )


def delete_list(conn, list_id: int) -> None:
    # Soft-delete for sync propagation; hard cascade handled at merge time
    conn.execute(
        "UPDATE lists SET deleted = 1, updated_at = datetime('now') WHERE id = ?",
        (list_id,),
    )


def rename_list(conn, list_id: int, name: str) -> None:
    conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name.strip(), list_id))


def set_list_fields(conn, list_id: int, event_date=None, venue=None) -> None:
    """Set a list's event date and/or venue — the event-planning metadata
    that also powers 'who am I seeing this week' without any calendar permission."""
    sets, params = [], []
    if event_date is not None:
        sets.append("event_date = ?"); params.append((event_date or "").strip())
    if venue is not None:
        sets.append("venue = ?"); params.append((venue or "").strip())
    if not sets:
        return
    params.append(list_id)
    conn.execute(f"UPDATE lists SET {', '.join(sets)} WHERE id = ?", params)


# ── The ONE definition of the LinkedIn scrape queue ──────────────────────────
# NC-095.
#
# This clause is read by two programs. `api_linkedin.py` counts it to render
# "N queued"; `scrape_linkedin_profiles.py` selects it to decide what to
# actually scrape, and a user may run that on a second Mac. It is the one number
# in the app that is a PREDICTION ABOUT ANOTHER PROGRAM'S BEHAVIOUR, so a
# mismatch is the least recoverable kind — no single screen shows both figures,
# because the scraper prints its own count where nobody is looking.
#
# They were written out separately and had drifted in three ways, all of them
# inert today and none of them by design:
#
#   1. The panel filtered `deleted = 0`; the scraper did not. So the panel
#      under-promised the moment a soft-deleted person carried a LinkedIn
#      address. Deleted wins: scraping someone the user deleted spends the
#      scraper's night writing facts onto a record that should not exist. The
#      two sides only agreed while no soft-deleted contact happened to carry a
#      LinkedIn address.
#   2. The panel was null-safe (TRIM/COALESCE); the scraper was not. Every one
#      of these columns carries a DEFAULT but none is NOT NULL, so a direct
#      INSERT can still write NULL — and `facts_source != 'manual'` is NULL, not
#      true, for a NULL row. The scraper would silently skip such a person
#      forever while the panel kept counting them.
#   3. "linkedin_scrape" was a literal in both files, under two different
#      constant names (FACTS_SCRAPE / FACTS_SOURCE).
#
# The cutoff stays a PARAMETER rather than being computed in here, because the
# two programs may each load their own .env: see li_rescrape_days() below.
LI_FACTS_SOURCE = "linkedin_scrape"


def li_rescrape_days() -> int:
    """How old a scrape must be before it is worth redoing.

    Read from the environment at CALL time, not captured at import: the same
    reason IMPORT_TIMEOUT_SEC and CHAT_MAX_CARDS were unfrozen — a value the
    Settings view can write is a value that must take effect without a restart.

    ONE CAVEAT THAT THIS FUNCTION CANNOT FIX. A scraper running on a different
    machine has its own secrets/.env, so setting NC_LI_RESCRAPE_DAYS here
    changes what the panel PREDICTS and not what that scraper DOES. Before this
    existed the drift was worse and invisible — the panel read the variable and
    the scraper had 180 hardcoded, so the two disagreed by construction the
    moment it was set to anything else. Now they disagree only if the two .env
    files disagree, which is a fact about the setup rather than about the code.
    """
    try:
        return max(1, int(os.environ.get("NC_LI_RESCRAPE_DAYS", "180")))
    except (TypeError, ValueError):
        return 180


def li_scrape_cutoff(now: datetime | None = None) -> str:
    """The ISO stamp a scrape must predate to be worth redoing."""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=li_rescrape_days())).isoformat(timespec="seconds")


def li_queue_where(cutoff: str) -> tuple:
    """(where_clause, params) for "this contact is worth (re)scraping".

    Returned as a fragment rather than a whole statement because the two callers
    need different SELECTs over it — a COUNT(*) for the panel, a SELECT * for the
    scraper, which then ranks the rows by relationship strength in Python.
    """
    return (
        "TRIM(COALESCE(linkedin_url, '')) <> '' "
        f"AND {figures.population_where('active')} "
        "AND COALESCE(facts_source, '') != 'manual' "
        "AND (COALESCE(facts_source, '') != ? "
        "     OR COALESCE(facts_updated_at, '') = '' "
        "     OR facts_updated_at < ?)",
        (LI_FACTS_SOURCE, cutoff),
    )


# ── Dates: birthdays, anniversaries, meetings ───────────────────────────────
DATE_KINDS = {"birthday", "anniversary", "meeting", "custom"}
# kinds that repeat every year unless the user says otherwise
RECURRING_BY_DEFAULT = {"birthday", "anniversary"}


def next_occurrence(on_date: str, recurring: int, today: str = "") -> str:
    """The next date this one lands on, as 'YYYY-MM-DD', or '' if it's a
    one-off already in the past.

    Today counts — a birthday today is the whole point of the feature. Feb 29 in
    a common year falls back to Feb 28 rather than being skipped for three years.
    """
    from datetime import date as _date
    try:
        y, m, d = (int(x) for x in (on_date or "")[:10].split("-"))
    except (ValueError, TypeError):
        return ""
    tod = today[:10] if today else _today_local_str()
    if not recurring:
        return on_date[:10] if on_date[:10] >= tod else ""
    ty, tm, td = (int(x) for x in tod.split("-"))

    def _mk(year: int) -> str:
        try:
            return _date(year, m, d).isoformat()
        except ValueError:      # Feb 29 in a common year
            return _date(year, m, d - 1).isoformat()

    this_year = _mk(ty)
    return this_year if this_year >= tod else _mk(ty + 1)


def add_contact_date(conn, contact_id: int, kind: str, on_date: str, *,
                     label: str = "", year_known: int = 1, recurring=None) -> int:
    """Add a date for a contact. `recurring` defaults by kind (birthdays and
    anniversaries repeat; meetings don't), but an explicit value always wins so
    'car service every year' stays representable."""
    kind = (kind or "custom").strip().lower()
    if kind not in DATE_KINDS:
        raise ValueError(f"bad date kind: {kind}")
    if not (on_date or "").strip():
        raise ValueError("on_date is required")
    if recurring is None:
        recurring = 1 if kind in RECURRING_BY_DEFAULT else 0
    cur = conn.execute(
        """INSERT OR IGNORE INTO contact_dates
             (contact_id, kind, label, on_date, year_known, recurring)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (contact_id, kind, (label or "").strip(), on_date[:10],
         1 if year_known else 0, 1 if recurring else 0),
    )
    return cur.lastrowid or 0


def upsert_calendar_date(conn, contact_id: int, on_date: str, *, source_uid: str,
                         label: str = "", recurring: int = 0) -> str:
    """Write one meeting that came from a calendar file. Returns what it did:
    "created" | "moved" | "unchanged" | "refused_manual".

    Three behaviours the plain add_contact_date cannot give this caller:

    1. IT NEVER TOUCHES A MANUAL ROW. If a row for this contact and UID exists with
       source='manual' — because the owner typed it, or corrected the imported one —
       the import leaves it exactly alone and says so. That is invariant 4's
       precedence rule applied to dates: a later automatic pass must never
       overwrite a better source.
    2. A RESCHEDULED EVENT MOVES INSTEAD OF DUPLICATING. add_contact_date is
       INSERT OR IGNORE against UNIQUE(contact_id, kind, on_date, label), which is
       idempotent only while the date never changes. Move a meeting from Tuesday to
       Thursday, re-import, and that gives you two meetings. Keying on the .ics UID
       is what makes a move a move.
    3. IT REPORTS. The caller needs to tell the user what actually happened rather
       than what it attempted, so every branch returns a distinct verdict.

    `source_uid` is required and must be non-empty — a calendar row with no UID
    cannot be reconciled on a later run, so there is no honest way to write one.
    """
    uid = (source_uid or "").strip()
    if not uid:
        raise ValueError("source_uid is required for a calendar date")
    day = (on_date or "").strip()[:10]
    if not day:
        raise ValueError("on_date is required")
    lab = (label or "").strip()

    row = conn.execute(
        "SELECT id, on_date, label, source FROM contact_dates "
        "WHERE contact_id = ? AND source_uid = ? LIMIT 1", (contact_id, uid)
    ).fetchone()

    if row is not None:
        if (row["source"] or "") == "manual":
            return "refused_manual"
        if (row["on_date"] or "")[:10] == day and (row["label"] or "") == lab:
            return "unchanged"
        conn.execute(
            "UPDATE contact_dates SET on_date = ?, label = ?, recurring = ? WHERE id = ?",
            (day, lab, 1 if recurring else 0, row["id"]))
        return "moved"

    # No row for this UID. A hand-typed meeting on the same day with the same label
    # would collide on the UNIQUE constraint; INSERT OR IGNORE means the import
    # yields to it rather than failing, and the verdict says the row was not ours.
    cur = conn.execute(
        """INSERT OR IGNORE INTO contact_dates
             (contact_id, kind, label, on_date, year_known, recurring, source, source_uid)
           VALUES (?, 'meeting', ?, ?, 1, ?, 'calendar', ?)""",
        (contact_id, lab, day, 1 if recurring else 0, uid))
    return "created" if cur.lastrowid else "refused_manual"


def upsert_birthday(conn, contact_id: int, on_date: str, *, year_known: int = 1,
                    source: str = "contacts") -> str:
    """Write one birthday that came from an address book. Returns what it did:
    "created" | "moved" | "unchanged" | "refused_manual".

    A person has one birthday, so this keys on (contact_id, kind='birthday')
    rather than on the date — which is the whole difference from
    add_contact_date. add_contact_date is INSERT OR IGNORE against
    UNIQUE(contact_id, kind, on_date, label): correct the year in Apple Contacts,
    re-import, and that leaves the contact holding two birthdays with no way to
    tell which is right. Keying on the person is what makes a correction a
    correction.

    Manual always wins (invariant 4): if the owner typed or fixed the date themselves
    an import must not overwrite it, so the row is left exactly alone and the
    verdict says so. `year_known=0` is Apple's no-year birthday — the day is
    real, the year is a placeholder and no age should ever be shown for it.
    """
    day = (on_date or "").strip()[:10]
    if not day:
        raise ValueError("on_date is required")
    known = 1 if year_known else 0

    row = conn.execute(
        "SELECT id, on_date, year_known, source FROM contact_dates "
        "WHERE contact_id = ? AND kind = 'birthday' LIMIT 1", (contact_id,)
    ).fetchone()

    if row is not None:
        if (row["source"] or "manual") == "manual":
            return "refused_manual"
        if (row["on_date"] or "")[:10] == day and int(row["year_known"] or 0) == known:
            return "unchanged"
        conn.execute(
            "UPDATE contact_dates SET on_date = ?, year_known = ?, source = ? WHERE id = ?",
            (day, known, source, row["id"]))
        return "moved"

    cur = conn.execute(
        """INSERT OR IGNORE INTO contact_dates
             (contact_id, kind, label, on_date, year_known, recurring, source)
           VALUES (?, 'birthday', '', ?, ?, 1, ?)""",
        (contact_id, day, known, source))
    # A hand-typed birthday on the same day would have been caught above; an
    # ignored insert here means the UNIQUE constraint fired for some other reason,
    # so claiming "created" would be a lie.
    return "created" if cur.lastrowid else "unchanged"


# NC-055. What calendar date is it, in the timezone the user is standing in.
#
# The three callers below all used datetime.now(timezone.utc).strftime(...) and
# were all wrong for the same window: between local midnight and UTC midnight
# they answer "yesterday". For a user N hours ahead of UTC that window is the
# first N hours of every local day, and in it the whole upcoming-dates strip
# shifts by a day — a birthday TODAY renders as "tomorrow", which is precisely
# when the strip most needs to be right. Tests that build their fixtures with
# date.today(), which is local, fail inside that window if the code uses UTC.
#
# A birthday, an anniversary, a dinner on Thursday: these are dates on a wall
# calendar, not instants on a timeline. UTC is the right default for a timestamp
# and the wrong one for a day.
#
# One function rather than three call sites, because three copies of "what day is
# it" can disagree. The `days since we last spoke` helpers elsewhere in this file
# still use UTC; the same skew applies but is invisible at their resolution
# (months), so they are left alone.
def _today_local_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_contact_dates(conn, contact_id: int):
    return conn.execute(
        "SELECT * FROM contact_dates WHERE contact_id = ? ORDER BY on_date", (contact_id,)
    ).fetchall()


def delete_contact_date(conn, date_id: int, contact_id: int = None) -> None:
    if contact_id is None:
        conn.execute("DELETE FROM contact_dates WHERE id = ?", (date_id,))
    else:
        conn.execute("DELETE FROM contact_dates WHERE id = ? AND contact_id = ?",
                     (date_id, contact_id))


def upcoming_dates(conn, days_ahead: int = 14, today: str = "", limit: int = 50):
    """(next_occurrence, days_until, row) for every non-excluded contact's dates
    landing within `days_ahead`, soonest first. Recurrence is resolved here so
    no caller — and no frontend — reimplements the calendar math."""
    tod = today[:10] if today else _today_local_str()
    rows = conn.execute(
        f"""SELECT d.*, c.name FROM contact_dates d JOIN contacts c ON c.id = d.contact_id
           WHERE {figures.population_where('visible', 'c')}"""
    ).fetchall()
    from datetime import date as _date
    t = _date(*(int(x) for x in tod.split("-")))
    out = []
    for r in rows:
        nxt = next_occurrence(r["on_date"], r["recurring"], tod)
        if not nxt:
            continue
        days = (_date(*(int(x) for x in nxt.split("-"))) - t).days
        if 0 <= days <= days_ahead:
            out.append((nxt, days, r))
    out.sort(key=lambda x: (x[0], (x[2]["name"] or "").lower()))
    return out[:limit]


def upcoming_events(conn, days_ahead: int = 14, today: str = ""):
    """Dated lists (events) landing within the window — the other half of
    'who am I seeing this week', from lists.event_date."""
    tod = today[:10] if today else _today_local_str()
    from datetime import date as _date, timedelta as _td
    end = (_date(*(int(x) for x in tod.split("-"))) + _td(days=days_ahead)).isoformat()
    # NC-104: this was a THIRD definition of "how many people are on this list",
    # and it counted everybody — including members the owner had explicitly removed.
    # An event list with one removed member made the strip above People count one
    # more person than the Lists page did for the same list, one flex row apart,
    # because the removed member carries status '__removed__'.
    #
    # NC-089 unified the rail and the detail pane, but this one lives in a dates
    # function, joins the same two tables by hand, and its alias is `members`
    # while api_dates renames it to `people` on the way out. So the ONE
    # definition — _MEMBER above — is used here too, and the contacts join it
    # needs comes with it.
    return conn.execute(
        f"""SELECT l.*,
                   COUNT(CASE WHEN {_MEMBER} THEN m.id END)                AS members,
                   SUM(CASE WHEN {_MEMBER} AND m.status = 'attending'
                            THEN 1 ELSE 0 END)                             AS attending
            FROM lists l
            LEFT JOIN list_members m ON m.list_id = l.id
            LEFT JOIN contacts c ON c.id = m.contact_id
            WHERE l.deleted = 0 AND l.event_date != '' AND l.event_date BETWEEN ? AND ?
            GROUP BY l.id ORDER BY l.event_date""",
        (tod, end),
    ).fetchall()


# ── State (key/value) ───────────────────────────────────────────────────────
def state_set(conn, key: str, value: str):
    conn.execute(
        """INSERT INTO state (key, value, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')""",
        (key, value),
    )


def state_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# An allowlist table and its add_to_allowlist()/get_allowlist() helpers used to
# live here. They held the Telegram usernames permitted to talk to the old chat
# bot, so they went when the bot did. The CREATE TABLE is gone too; an existing
# install keeps an empty, unread table, which is harmless — there is no
# migration to run.


if __name__ == "__main__":
    init_db()
    print(f"Initialised {DB_PATH}")
