#!/usr/bin/env python3
"""Reads of archive.db for the UI — history, episodes, and conversation search.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

These handlers mostly take NO crm.db connection: archive.db is the bulk, local-only
layer and is opened per call, so they sit outside CrmHandler._with_conn. Every open
goes through arc.open_if_present(), because a fresh install has no archive.db and a
running import may hold the write lock. No archive, or one with no messages yet, is an
empty answer; history and episodes say so when the archive is there and cannot be
read (NC-340), because an empty page there reads as a person you never wrote to.

The exception is `threads=1` (NC-106). Widening a page to the WHOLE of every group
conversation the person took part in means showing other people's lines, and a line
nobody can attribute is worse than no line — so the widening and the crm.db
connection that names the speakers arrive together or not at all. `conn=None` means
`threads=1` is ignored rather than served anonymously.

Off by default, and that default is load-bearing: `api_briefs` reads episodes three
times to write a brief ABOUT one person, and folding a group chat's other voices into
that would have the AI describe somebody else's remarks as theirs.

Episodes are conversation slices: same channel, same conversation, gaps under
EPISODE_GAP_HOURS.
"""

import sqlite3
from http import HTTPStatus
from typing import Any, Dict, List

import archive as arc
import db
import feed
import figures  # NC-115: the one spelling of "the people you hid"
import search_cli
from api_core import ApiError
from api_guards import _require_contact
from api_serialize import _archive_preview


EPISODE_GAP_HOURS = 4


EPISODE_MAX_MSGS = 60


EPISODE_INLINE_MSGS = 6


EPISODE_SCAN_MSGS = 600


def _wants_threads(query: Dict[str, List[str]], conn: Any) -> bool:
    """Whether this request asked for whole conversations AND can name them."""
    asked = (query.get("threads", ["0"])[0] or "0").strip() not in ("", "0", "false")
    return bool(asked and conn is not None)


# What a line in a whole-conversation view is labelled with when its speaker is
# someone the owner asked not to see (NC-200). The line stays, because a thread
# where someone answers a line that is not there cannot be read; the name goes,
# because Hide promises "Hidden from every view, search and AI answer".
SPEAKER_HIDDEN = "Someone you hid"
SPEAKER_DELETED = "Someone you deleted"
SPEAKER_GONE = "Someone not in your contacts"


def _speakers(conn: Any, msgs: Any, contact_id: int) -> Dict[int, str]:
    """contact_id → the label for their lines, for the people talking in these
    messages.

    Only group messages get a label: in a one-to-one thread the page already
    says whose it is, and `direction` says which side each line is. In a group
    both of those stop being enough — a thread rendered as one voice is
    unreadable, and worse, it reads as if the person whose page it is said all
    of it.

    A message stored under `arc.NO_PERSON` is the owner's own (see NC-111); it
    gets no label because `direction = 1` already says so.

    NC-200. Every other speaker gets a label, never an empty string: the page
    shows `m.who || <this person's name>`, so an empty label put the line in the
    mouth of whoever's page it was. That is how a purged speaker's lines were
    attributed until now, and it is also why a hidden speaker is labelled
    rather than left blank. Someone hidden or deleted is named only on their
    own page, where the owner has gone to them on purpose.
    """
    ids = {m["contact_id"] for m in msgs
           if (m["group_key"] if "group_key" in m.keys() else "")
           and m["contact_id"] != arc.NO_PERSON}
    hidden = db.ids_outside(conn, "visible") if ids else set()
    deleted = db.ids_outside(conn, "live") if ids else set()
    out: Dict[int, str] = {}
    for cid in ids:
        row = db.get_contact(conn, cid)
        if row is None:
            out[cid] = SPEAKER_GONE
        elif cid != contact_id and cid in deleted:
            out[cid] = SPEAKER_DELETED
        elif cid != contact_id and cid in hidden:
            out[cid] = SPEAKER_HIDDEN
        else:
            out[cid] = row["name"]
    return out


def _grp(row: Any) -> tuple:
    """What makes this message's conversation a different one.

    The KEY first, then the label — the same pair `archive.build_chunks` uses,
    and for the same reason (NC-110): an unnamed chat is labelled from its head
    count, so breaking on the label alone merged every unnamed three-person chat
    into one episode. Missed when NC-110 fixed the other two readers.
    """
    keys = row.keys()
    return (row["group_key"] if "group_key" in keys else "",
            row["group_name"] if "group_name" in keys else "")


def _page_size(query: Dict[str, List[str]], default: int, ceiling: int) -> int:
    """The `limit` a caller asked for: the default when absent, at most
    `ceiling`, and a 400 for anything that is not a whole number from 1 up.

    NC-340. On /episodes a word here was a 500, and on /history it was hidden
    as an empty answer, which reads as a person you never wrote to."""
    raw = (query.get("limit", [""])[0] or "").strip()
    if not raw:
        return default
    try:
        size = int(raw)
    except ValueError:
        size = 0
    if size < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       f"limit must be a whole number from 1 to {ceiling}")
    return min(size, ceiling)


def _archive_not_started(exc: Exception) -> bool:
    """An archive.db with no messages table yet: a fresh install, before the
    first import. That is an honest "no messages", not a fault."""
    return isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc)


def _unreadable(exc: Exception) -> ApiError:
    """NC-340. A locked or damaged archive is said, not shown as an empty
    history: an empty answer reads as a person you never wrote to."""
    return ApiError(HTTPStatus.SERVICE_UNAVAILABLE,
                    f"Your saved messages could not be read just now: {exc}")


def _last_messages(contact_id: int, limit: int = 3) -> List[Dict[str, Any]]:
    """Most recent archived messages, oldest-first, for the review card's
    'last exchange' quote. [] when archive.db is absent."""
    msgs = _archive_preview(contact_id)[:limit]
    return list(reversed([
        {"channel": m["channel"], "direction": m["direction"], "ts": m["ts"],
         "body": (m["subject"] + " — " if m["subject"] else "") + (m["body"] or "")}
        for m in msgs
    ]))


def handle_get_history(contact_id: int, query: Dict[str, List[str]],
                       conn: Any = None) -> Dict[str, Any]:
    """Paginated message history for a contact from archive.db.

    GET /api/contact/{id}/history?channel=whatsapp&before=<timestamp>&since=&limit=50
    Add threads=1 for the WHOLE of every group conversation they took part in
    rather than only their own lines in it (NC-106); it needs `conn` to name the
    other speakers and is ignored without one. `since` bounds the window from
    below, which threads=1 needs — see thread_since on an episode.
    Returns {messages: [...], has_more: bool, threads: bool}. Empty if
    archive.db is absent or holds no messages yet. NC-340: an unknown person is
    a 404 (when `conn` is given, which the route always does), a bad `limit` a
    400, and an archive that cannot be read a 503 rather than an empty page.
    """
    threads = _wants_threads(query, conn)
    if conn is not None:
        _require_contact(conn, contact_id)
    limit = _page_size(query, 50, 200)
    try:
        channel = (query.get("channel", [None])[0] or None)
        before = (query.get("before", [None])[0] or None)
        since = (query.get("since", [None])[0] or None)
        with arc.open_if_present() as arc_conn:
            if arc_conn is None:
                return {"messages": [], "has_more": False, "threads": False}
            msgs = arc.get_messages(arc_conn, contact_id, channel=channel,
                                    limit=limit + 1, before=before,
                                    include_threads=threads, since=since)
            names = _speakers(conn, msgs, contact_id) if threads else {}
        has_more = len(msgs) > limit
        return {
            "messages": [
                {
                    "id": m["id"], "channel": m["channel"],
                    "direction": m["direction"], "ts": m["ts"],
                    "subject": m["subject"], "body": m["body"],
                    "group": m["group_name"] if "group_name" in m.keys() else "",
                    "who": names.get(m["contact_id"], ""),
                }
                for m in msgs[:limit]
            ],
            "has_more": has_more,
            "threads": threads,
        }
    except Exception as exc:  # noqa: BLE001 — sorted into "none yet" and "cannot read"
        if _archive_not_started(exc):
            return {"messages": [], "has_more": False, "threads": False}
        raise _unreadable(exc) from exc


def handle_get_episodes(contact_id: int, query: Dict[str, List[str]]) -> Dict[str, Any]:
    """GET /api/contact/{id}/episodes?before=&limit= — archived messages grouped
    into conversation episodes (same channel, same conversation, gaps under
    EPISODE_GAP_HOURS), newest episode first. Small episodes carry their messages
    inline; larger ones are expanded lazily via /history with the episode's time
    window.

    THIS LIST IS NEVER WIDENED TO WHOLE CONVERSATIONS, and there is no option to
    (NC-106). It scans the person's 600 most recent messages, so folding in every
    other voice from their group chats would let one busy thread fill that budget
    and push their one-to-one history off their own page. The list stays a record
    of knowing THEM; `handle_get_history` with threads=1 is what shows the
    conversation, once someone opens an episode. Gated by
    test_the_episode_list_is_not_widened, which fails if it is wired up here.

    NC-340: a bad `limit` is a 400 and an archive that cannot be read a 503.
    An unknown person is a 404 too, checked by the route, because this handler
    takes no crm.db connection (see above).
    """
    limit = _page_size(query, 25, 100)
    before = (query.get("before", [None])[0] or None)
    # open_if_present() only proves the FILE exists — an archive.db that exists but
    # has no `messages` table yet raises OperationalError from the query below, and
    # that surfaced as a 500 on the person page of every fresh install: opening
    # anyone before the first message import is the most ordinary thing a new user
    # can do. handle_get_history above already degrades to empty for exactly this
    # reason; episodes was the one path that did not.
    try:
        with arc.open_if_present() as arc_conn:
            if arc_conn is None:
                return {"episodes": [], "has_more": False}
            msgs = arc.get_messages(arc_conn, contact_id,
                                    limit=EPISODE_SCAN_MSGS, before=before)
    except Exception as exc:  # noqa: BLE001 — sorted into "none yet" and "cannot read"
        if _archive_not_started(exc):
            return {"episodes": [], "has_more": False}
        raise _unreadable(exc) from exc

    episodes: List[Dict[str, Any]] = []
    bucket: List[Any] = []

    def flush() -> None:
        if not bucket:
            return
        # bucket is newest→oldest within one episode
        bodies = [m["body"] for m in bucket if (m["body"] or "").strip()]
        snippet = (bodies[0].strip().replace("\n", " ")[:140]) if bodies else "(media)"
        group = next((m["group_name"] for m in bucket
                      if "group_name" in m.keys() and m["group_name"]), "")
        ep = {
            "channel": bucket[0]["channel"],
            "ts_start": bucket[-1]["ts"],
            "ts_end": bucket[0]["ts"],
            "count": len(bucket),
            "sent": sum(1 for m in bucket if m["direction"]),
            "snippet": (f"[{group}] " + snippet) if group else snippet,
            "group": group,
        }
        if group:
            # WHICH WINDOW THE SURROUNDING CONVERSATION OCCUPIES (NC-106).
            #
            # An episode is cut from ONE person's messages, so its own bounds are
            # the first and last thing THEY said. The other voices in the thread
            # fall outside that by definition — somebody answering four minutes
            # later is after `ts_end`. Widening the fetch without widening the
            # window returns almost nothing: an opened group episode would show
            # one bubble.
            #
            # The window is the episode plus EPISODE_GAP_HOURS either side —
            # the same silence that ends an episode, so "the conversation around
            # this exchange" is a rule that can be said in a sentence rather
            # than a number tuned until the screen looked right. Computed here
            # so the gap has one definition and the client does no date maths.
            ep["thread_since"] = arc.shift_ts(bucket[-1]["ts"], -EPISODE_GAP_HOURS)
            ep["thread_before"] = arc.shift_ts(bucket[0]["ts"], EPISODE_GAP_HOURS)
        if len(bucket) <= EPISODE_INLINE_MSGS:
            ep["messages"] = [
                {"channel": m["channel"], "direction": m["direction"], "ts": m["ts"],
                 "subject": m["subject"], "body": m["body"],
                 "group": m["group_name"] if "group_name" in m.keys() else ""}
                for m in reversed(bucket)
            ]
        episodes.append(ep)

    consumed = 0
    for m in msgs:
        if len(episodes) >= limit:
            break
        starts_new = bool(bucket) and (
            m["channel"] != bucket[-1]["channel"]
            # a CONVERSATION is its own episode — see _grp, and NC-110 for what
            # breaking on the label alone merged
            or _grp(m) != _grp(bucket[-1])
            or len(bucket) >= EPISODE_MAX_MSGS
            or arc._hours_between(bucket[-1]["ts"], m["ts"]) > EPISODE_GAP_HOURS
        )
        if starts_new:
            flush()
            bucket = []
        if len(episodes) >= limit:
            break
        bucket.append(m)
        consumed += 1
    if bucket and len(episodes) < limit:
        flush()
        bucket = []

    exhausted = len(msgs) < EPISODE_SCAN_MSGS and consumed >= len(msgs) and not bucket
    next_before = episodes[-1]["ts_start"] if episodes else None
    return {"episodes": episodes, "has_more": not exhausted,
            "next_before": next_before}


def _someone_in(conn: Any, arc_conn: Any, group_key: str, ts: str,
                unusable: set, seen: Dict[str, Any]) -> Any:
    """A person whose page shows this conversation, or None.

    A hit on one of the owner's own group messages has no person of its own
    (NC-111), but the conversation is on the page of everyone in it: a group
    episode there widens to the whole thread (NC-106). So the hit opens the
    page of whoever spoke nearest to it in time — through the (group_key, ts)
    index, ten messages either side — skipping anyone hidden or deleted.
    `seen` caches the answer per conversation for one search.
    """
    if not group_key:
        return None
    if group_key in seen:
        return seen[group_key]
    near = []
    for sql in ("SELECT contact_id, ts FROM messages WHERE group_key = ? AND ts <= ? "
                f"AND {arc.is_a_person()} ORDER BY ts DESC LIMIT 10",
                "SELECT contact_id, ts FROM messages WHERE group_key = ? AND ts > ? "
                f"AND {arc.is_a_person()} ORDER BY ts LIMIT 10"):
        near += arc_conn.execute(sql, (group_key, ts)).fetchall()
    near.sort(key=lambda r: abs(arc._hours_between(r["ts"], ts)))
    found = next((r["contact_id"] for r in near
                  if r["contact_id"] not in unusable
                  and db.get_contact(conn, r["contact_id"]) is not None), None)
    seen[group_key] = found
    return found


def _conversation_hit(row: Any, ts: str, snippet: str, open_id: Any) -> Dict[str, Any]:
    """A hit that belongs to a conversation rather than a person (NC-112).

    `contact_id` stays NO_PERSON, because that is the truth; `conversation`
    is what the row is labelled with, and `open_id` is where clicking it goes.
    """
    keys = row.keys()
    label = (row["group_name"] if "group_name" in keys else "") or ""
    return {
        "contact_id": arc.NO_PERSON,
        "contact_name": "",
        "conversation": label or "a group conversation",
        "open_id": open_id,
        "channel": row["channel"],
        "ts": ts,
        "snippet": snippet,
    }


def handle_conversation_search(conn: Any, query: Dict[str, List[str]]) -> Dict[str, Any]:
    """Search archived message content directly — the rest of what someone
    said, not just their contact card. mode=keyword uses FTS5 over raw
    messages; mode=semantic uses the embedded conversation chunks. Both skip
    excluded contacts and degrade to available=False if archive.db doesn't
    exist yet (nothing imported) or, for semantic, the embedding deps aren't
    installed.

    NC-112. A message the owner sent to a group belongs to the conversation, not
    to a person, and both branches used to look its person up, find none, and
    drop it — so searching for something they said in a group found it, ranked
    it, and showed nothing. Those hits now come back labelled with the
    conversation (see _conversation_hit)."""
    q = (query.get("q", [""])[0] or "").strip()
    mode = (query.get("mode", ["keyword"])[0] or "keyword").strip()
    if not q:
        return {"available": True, "conversations": [], "query": "", "mode": mode}
    with arc.open_if_present() as arc_conn:
        if arc_conn is None:
            # available=False lets the UI say "nothing imported yet" instead of
            # showing a bare no-results — an empty archive is not a miss.
            return {"available": False, "conversations": [], "query": q, "mode": mode,
                    "message": "No conversations imported yet — import WhatsApp, "
                               "iMessage or email in Data."}

        excluded = {r["id"] for r in conn.execute(
            f"SELECT id FROM contacts WHERE {figures.population_where('hidden')}")}
        # For where a conversation's hit OPENS: a page the owner can actually use.
        unusable = db.ids_outside(conn, "active")
        seen: Dict[str, Any] = {}

        if mode == "semantic":
            if not search_cli.embeddings_available():
                return {"available": False, "conversations": [], "query": q, "mode": mode,
                        "message": "Semantic search needs a one-time install: "
                                   "pip install sentence-transformers numpy"}
            try:
                ranked = search_cli.chunk_semantic_rank(arc_conn, q, limit=15, crm_conn=conn)
            except Exception as exc:  # noqa: BLE001 — surface model/runtime issues cleanly
                return {"available": False, "conversations": [], "query": q, "mode": mode,
                        "message": f"Semantic search failed: {exc}"}
            out = []
            for score, row in ranked:
                if row["contact_id"] == arc.NO_PERSON:
                    keys = row.keys()
                    gkey = row["group_key"] if "group_key" in keys else ""
                    hit = _conversation_hit(
                        row, row["ts_end"], row["text"][:280],
                        _someone_in(conn, arc_conn, gkey, row["ts_end"], unusable, seen))
                    hit["score"] = round(float(score), 3)
                    out.append(hit)
                    continue
                c = db.get_contact(conn, row["contact_id"])
                if c is None:
                    continue
                out.append({
                    "contact_id": row["contact_id"],
                    "contact_name": c["name"],
                    "channel": row["channel"],
                    "ts": row["ts_end"],
                    "snippet": row["text"][:280],
                    "score": round(float(score), 3),
                })
            return {"available": True, "conversations": out, "query": q, "mode": mode}

        rows = arc.fts_search(arc_conn, q, limit=60)
        out = []
        for row in rows:
            if row["contact_id"] == arc.NO_PERSON:
                out.append(_conversation_hit(
                    row, row["ts"], row["snippet"],
                    _someone_in(conn, arc_conn,
                                row["group_key"] if "group_key" in row.keys() else "",
                                row["ts"], unusable, seen)))
                if len(out) >= 15:
                    break
                continue
            if row["contact_id"] in excluded:
                continue
            c = db.get_contact(conn, row["contact_id"])
            if c is None:
                continue
            out.append({
                "contact_id": row["contact_id"],
                "contact_name": c["name"],
                "channel": row["channel"],
                "ts": row["ts"],
                "snippet": row["snippet"],
            })
            if len(out) >= 15:
                break
        return {"available": True, "conversations": out, "query": q, "mode": mode}


def owed_replies(conn: Any, *, cap: int, days: int = None) -> List[Any]:
    """Conversations where they spoke last and you still owe a reply.

    Pushed down here so the view layer never opens archive.db itself: this is the
    one archive read the Today view needs. Degrades to whatever it managed to
    collect if the archive is missing, locked, or errors mid-scan — Today must
    render on a fresh install, and during an import that holds the write lock.
    """
    rows: List[Any] = []
    with arc.open_if_present() as arc_conn:
        if arc_conn is None:
            return rows
        try:
            for item in feed.owed_reply_candidates(
                    conn, arc_conn,
                    days=feed.OWED_REPLY_TODAY_DAYS if days is None else days,
                    cap=cap):
                rows.append(item)
        except Exception:  # noqa: BLE001 — attention degrades, never crashes Today
            pass
    return rows
