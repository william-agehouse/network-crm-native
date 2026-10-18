#!/usr/bin/env python3
"""Contacts — the aggregate root. Listing, filtering, facets, and every edit.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

SOLE owner of EXCLUDE_FILE: the same file the importers honour, so excluding
someone in the UI survives the next re-import. It is deliberately not re-exported
by value anywhere — two copies would diverge silently while the tests stayed
green and writes went to the real exclude_list.txt.

Two invariants live here and are not reimplemented anywhere else:
  * facts_source precedence — every field write goes through db.set_field, and
    handle_set_field's anti-clobber branch returns {'ok', 'unchanged'} rather
    than blanking a populated field with an empty value.
  * photo precedence — handle_set_photo calls photos.save_photo(..., 'manual')
    and photos.photo_url; the manual > linkedin > vCard > whatsapp/google order
    is enforced in crm/photos.py, not here.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from collections import Counter
from http import HTTPStatus
from typing import Any, Dict, List

import archive as arc
import config
import db
import figures  # NC-087: the one spelling of a contact-scope filter
import photos
import search_cli
from api_core import ApiError
from api_guards import _require_contact
from api_serialize import _channel_counts, _contact_detail, _contact_summary

log = logging.getLogger("networkcrm")


# Same file the importers honor — excluding someone in the UI writes here too, so
# the choice survives the next re-import.
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))


REVIEW_MAX_SIZE = 20


ALLOWED_CATEGORIES = {"personal", "professional", "both", "unknown"}


ALLOWED_FIELDS = {"name", "company", "position", "home_city", "home_country",
                  "home_metro", "role_type", "blurb"}


# People-list paging. The first page stays small so the list paints at once; the
# UI says how many of how many it is showing and asks for the rest on request.
# CONTACTS_MAX is the whole network in one response — a few thousand people is
# small as JSON, which is nothing over loopback but is worth a stated ceiling.
CONTACTS_PAGE = int(os.environ.get("NC_CONTACTS_PAGE", "500"))


CONTACTS_MAX = int(os.environ.get("NC_CONTACTS_MAX", "20000"))


# "quiet for N months" — one definition, used by the dashboard, the attention
# queue and the See-all list, so the count and the rows can never disagree.
RECONNECT_MONTHS = 6


# Review-status actions exposed at /api/contact/{id}/{action} -> stored status.
#   skip       → "skipped"     : decide later; resurfaces in the top-up rotation
#   nevermet   → "never_met"   : you don't actually know them; leaves the flow for good
#   nocontext  → "no_context"  : you've met them but have nothing to write; stop nagging
REVIEW_STATUS_ACTIONS = {"skip": "skipped", "nevermet": "never_met", "nocontext": "no_context"}


def _excluded_count(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM contacts WHERE "
                       + figures.population_where("hidden")).fetchone()
    return row["n"] or 0


# NC-227. The hidden count /api/status carries, and the state of the database
# files when it was counted. See status_counts.
_HIDDEN_CACHE: Dict[str, Any] = {"stamp": None, "n": 0}
_HIDDEN_LOCK = threading.Lock()


def _files_stamp(conn: Any) -> Any:
    """When and how big this connection's database file and its write-ahead log
    are, or None when that cannot be told (an in-memory database).

    Every committed write lands in one of those two files, by this app or by an
    importer running as its own process, so an unchanged stamp means nothing
    was written since. It relies on the file times having nanoseconds, as they
    do on the Mac's own disk format (APFS), which is where the app keeps its data.
    """
    try:
        path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    except Exception:  # noqa: BLE001 — no stamp just means count every time
        return None
    if not path:
        return None
    stamp: List[Any] = [path]
    for suffix in ("", "-wal"):
        try:
            st = os.stat(path + suffix)
            stamp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def status_counts(conn: Any) -> Dict[str, int]:
    """The five numbers /api/status carries.

    NC-227. The window asks /api/status every 15 seconds. db.counts() answers
    its four from the small index on status, but the hidden count has no index
    and read the whole contacts table every time, for a number the web page
    does not even show. It is now counted again only when the database has been
    written since the last count. `excluded` stays in the answer, because the
    native app works out how many people you have as total minus excluded.

    The stamp is taken BEFORE counting, so a write that lands during the count
    can only make the next check count again, never keep an old number.
    """
    counts = db.counts(conn)
    stamp = _files_stamp(conn)
    with _HIDDEN_LOCK:
        if stamp is not None and _HIDDEN_CACHE["stamp"] == stamp:
            return {**counts, "excluded": _HIDDEN_CACHE["n"]}
    hidden = _excluded_count(conn)
    with _HIDDEN_LOCK:
        _HIDDEN_CACHE.update(stamp=stamp, n=hidden)
    return {**counts, "excluded": hidden}


# The orders the People list can be asked for. "messages" (NC-305) is most
# messages first, counting every channel, the same total the row shows.
CONTACT_SORTS = {"", "quiet", "recent", "closeness", "messages"}


def _fold(text: str) -> str:
    """Lower-case and drop accents, so "zoe" finds Zoë and "Zoë" finds Zoe.

    NC-341. Applied to both sides of a search and never stored: importers match
    people on the stored name_norm, which keeps its accents on purpose.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _matches(row: Any, name_q: str, text_q: str) -> bool:
    """Whether one person answers a search: the name (punctuation and accents
    ignored) or the company or role (accents ignored) contains it.

    Done in Python rather than in a LIKE, so a % or _ someone types is a
    character to find, never a wildcard that matches everyone."""
    name = _fold(row["name_norm"] or db.normalize_name(row["name"] or ""))
    if name_q in name:
        return True
    return any(text_q in _fold(row[col] or "") for col in ("company", "position"))


def _message_total(row: Any) -> int:
    return sum(_channel_counts(row).values())


def handle_list_contacts(conn: Any, query: Dict[str, List[str]]) -> Dict[str, Any]:
    q = (query.get("q", [""])[0] or "").strip()
    flt = (query.get("filter", ["all"])[0] or "all").strip()
    if flt not in {"all", "todo", "described", "excluded", "deleted"}:
        flt = "all"
    location = (query.get("location", [""])[0] or "").strip()
    role = (query.get("role", [""])[0] or "").strip()
    topic = (query.get("topic", [""])[0] or "").strip()
    sort = (query.get("sort", [""])[0] or "").strip()
    if sort not in CONTACT_SORTS:
        sort = ""
    # The page size is the caller's to choose, because the whole network is
    # small as JSON on loopback — a fixed 500 was never about bandwidth, it just
    # silently truncated thousands of people down to 500 with nothing on screen
    # to say so. Still clamped: an absent or junk value gets the default, and a
    # huge one can't be used to ask the server to build an unbounded response.
    try:
        limit = int((query.get("limit", [""])[0] or "").strip() or CONTACTS_PAGE)
    except ValueError:
        limit = CONTACTS_PAGE
    limit = max(1, min(limit, CONTACTS_MAX))
    facets = dict(flt=flt, location=location, role=role, topic=topic)
    # NC-341. A query that is nothing but spaces, punctuation or emoji leaves
    # nothing to look for once the name is normalised. It used to match every
    # name, so the list looked searched while showing everyone; now it is no
    # search at all, and `q` in the answer says so by coming back empty.
    name_q = _fold(db.normalize_name(q))
    if not name_q and sort != "messages":
        rows = db.list_contacts(conn, sort=sort, limit=limit, **facets)
        total = db.count_contacts(conn, **facets)
    else:
        # A search, or the most-messages order: both have to look at every
        # matching person before cutting the page, or the person who belongs on
        # top can sit past the cut (NC-305: "across the whole list, not just the
        # loaded page"). The list's own order is kept, so a search under
        # "Closest first" still reads closest first.
        everyone = max(1, db.count_contacts(conn, **facets))
        rows = db.list_contacts(conn, sort="" if sort == "messages" else sort,
                                limit=everyone, **facets)
        if name_q:
            text_q = _fold(q)
            rows = [r for r in rows if _matches(r, name_q, text_q)]
        if sort == "messages":
            rows.sort(key=lambda r: (-_message_total(r), (r["name"] or "").lower()))
        total = len(rows)
        rows = rows[:limit]
    return {"contacts": [_contact_summary(r) for r in rows], "total": total,
            "limit": limit, "q": q if name_q else ""}


def handle_locations(conn: Any) -> Dict[str, Any]:
    """GET /api/locations — distinct home_metro values for the Network view's
    location filter chips (falls back to home_city when metro isn't set)."""
    rows = conn.execute(
        f"""SELECT DISTINCT COALESCE(NULLIF(home_metro, ''), home_city) AS place
           FROM contacts
           WHERE {figures.population_where('visible')} AND COALESCE(NULLIF(home_metro, ''), home_city) != ''
           ORDER BY place"""
    ).fetchall()
    return {"locations": [r["place"] for r in rows if r["place"]]}


def handle_facets(conn: Any) -> Dict[str, Any]:
    """GET /api/facets — the role_type and topic values actually present in the
    network, for the Network view's role/topic filters. Roles come straight from
    the distinct role_type column; topics are tallied out of the per-contact JSON
    array strings and returned most-common-first (capped)."""
    from collections import Counter

    role_rows = conn.execute(
        f"""SELECT DISTINCT role_type FROM contacts
           WHERE {figures.population_where('visible')} AND role_type != ''
           ORDER BY role_type"""
    ).fetchall()
    roles = [r["role_type"] for r in role_rows if r["role_type"]]

    counter: "Counter[str]" = Counter()
    for r in conn.execute(
        f"SELECT topics FROM contacts WHERE {figures.population_where('visible')} AND topics NOT IN ('', '[]')"
    ):
        try:
            for t in json.loads(r["topics"] or "[]"):
                t = (t or "").strip()
                if t:
                    counter[t] += 1
        except Exception:
            continue
    topics = [t for t, _ in counter.most_common(20)]
    return {"roles": roles, "topics": topics}


def _channel_coverage(conn: Any) -> Dict[str, int]:
    """How many people have any history on each channel, plus the number with
    any history at all. One aggregate query.

    NC-122: over `active`, the people the rest of the Data page counts. It was
    `visible`, which still counted a person deleted without being hidden."""
    row = conn.execute(
        f"""SELECT
             SUM(msg_count  > 0) AS linkedin,
             SUM(imsg_count > 0) AS imessage,
             SUM(wa_count   > 0) AS whatsapp,
             SUM(em_count   > 0) AS email,
             SUM(tg_count   > 0) AS telegram,
             SUM(msgr_count > 0) AS messenger,
             SUM(ig_count   > 0) AS instagram,
             SUM((msg_count + imsg_count + wa_count + em_count
                  + tg_count + msgr_count + ig_count) > 0) AS any_history
           FROM contacts WHERE {figures.population_where('active')}"""
    ).fetchone()
    keys = ("linkedin", "imessage", "whatsapp", "email", "telegram",
            "messenger", "instagram", "any_history")
    return {k: (row[k] or 0) for k in keys}


def handle_semantic_search(conn: Any, query: Dict[str, List[str]]) -> Dict[str, Any]:
    """Meaning-based search over contacts, using the local embedding model.

    Degrades gracefully: if sentence-transformers/numpy aren't installed, returns
    available=False with an install hint instead of erroring, so the GUI can fall
    back to keyword search. The model + vectors are cached, so only the first
    query after a data change is slow."""
    q = (query.get("q", [""])[0] or "").strip()
    if not q:
        return {"available": True, "contacts": [], "query": ""}
    if not search_cli.embeddings_available():
        return {
            "available": False,
            "contacts": [],
            "query": q,
            "message": "Semantic search needs a one-time install: "
                       "pip install sentence-transformers numpy",
        }
    try:
        ranked = search_cli.semantic_rank(conn, q, limit=30)
    except Exception as exc:  # noqa: BLE001 — surface model/runtime issues cleanly
        return {"available": False, "contacts": [], "query": q,
                "message": f"Semantic search failed: {exc}"}
    contacts = []
    for score, cid in ranked:
        row = db.get_contact(conn, cid)
        if row is None:
            continue
        contacts.append({**_contact_summary(row), "score": round(float(score), 3)})
    return {"available": True, "contacts": contacts, "query": q}


def _parse_months(query: Dict[str, List[str]], default: int = 6) -> int:
    """Read ?months= from the query, default 6, clamp to 1..120, ignore junk."""
    raw = (query.get("months", [str(default)])[0] or "").strip()
    try:
        months = int(raw)
    except (TypeError, ValueError):
        months = default
    return max(1, min(120, months))


def handle_reconnect(conn: Any, query: Dict[str, List[str]]) -> Dict[str, Any]:
    months = _parse_months(query)
    rows = db.find_reconnect(conn, months=months)
    return {"contacts": [_contact_summary(r) for r in rows], "months": months}


def _parse_size(query: Dict[str, List[str]], default: int = 5) -> int:
    """Read ?size= from the query, default 5, clamp to 1..20, ignore junk."""
    raw = (query.get("size", [str(default)])[0] or "").strip()
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = default
    return max(1, min(REVIEW_MAX_SIZE, size))


def _new_count(conn: Any) -> int:
    """How many contacts are still waiting to be described (status='new', not excluded)."""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM contacts WHERE status='new' AND {figures.population_where('visible')}"
    ).fetchone()
    return row["n"] or 0


def handle_get_contact(conn: Any, contact_id: int) -> Dict[str, Any]:
    row = _require_contact(conn, contact_id)
    return _contact_detail(conn, row)


def handle_reconnect_skip(conn: Any, contact_id: int) -> Dict[str, Any]:
    """POST /api/contact/{id}/reconnectskip — snooze someone out of the
    reconnect / 'Worth a message' list for a while (see db.RECONNECT_SNOOZE_DAYS)."""
    _require_contact(conn, contact_id)
    db.dismiss_reconnect(conn, contact_id)
    conn.commit()
    return {"ok": True}


def handle_create_contact(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError(HTTPStatus.BAD_REQUEST, "name is required")
    new_id = db.create_contact(conn, name, source="manual")
    conn.commit()
    return {"id": new_id}


def handle_set_category(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    category = str(body.get("category") or "").strip().lower()
    if category not in ALLOWED_CATEGORIES:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"category must be one of {sorted(ALLOWED_CATEGORIES)}",
        )
    _require_contact(conn, contact_id)
    db.set_field(conn, contact_id, "category", category)
    conn.commit()
    return {"ok": True}


# NC-150. The sentence the UI shows when the file write failed. It says what did
# and did not happen, in that order, because "hidden" IS true — the column
# changed and every screen in the app honours it. What did not happen is the
# half the importers read, and the consequence is specific enough to name.
EXCLUDE_FILE_UNWRITABLE = (
    "Hidden here, but the skip list on disk could not be written, so the next "
    "import may bring their messages back. Check that the data folder is not "
    "full or read-only.")


def _mirror_exclude(conn: Any, name: str, excluded: bool) -> bool:
    """Keep exclude_list.txt saying what the `excluded` column says.

    The column is what the app reads; the file is what the ten importers read.
    A choice recorded in one and not the other is a choice the other half of
    the program never hears — NC-146, where deleting someone set the column and
    left the file alone, so the importers went on collecting for people the app
    had been told to forget.

    The removal is guarded: a name is only taken out of the file when no OTHER
    excluded contact still answers to it. Two people share a name more often
    than the file has entries, and un-hiding one of them must not quietly
    un-hide the other at the next import.

    Returns whether the file now agrees with the column. False is not an error —
    the app's own half worked — but it is the caller's job to say so rather than
    return a tick over the top of it (NC-150).
    """
    name = (name or "").strip()
    if not name:
        return True
    if excluded:
        return db.add_to_exclude_file(EXCLUDE_FILE, name)
    still = conn.execute(
        f"SELECT 1 FROM contacts WHERE {figures.population_where('hidden')} "
        "AND name_norm = ? LIMIT 1",
        (db.normalize_name(name),),
    ).fetchone()
    if still:
        return True
    return db.remove_from_exclude_file(EXCLUDE_FILE, name)


def handle_set_excluded(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    if "excluded" not in body:
        raise ApiError(HTTPStatus.BAD_REQUEST, "excluded is required")
    row = _require_contact(conn, contact_id)
    value = 1 if body.get("excluded") else 0
    db.set_excluded(conn, contact_id, value)
    conn.commit()
    # Mirror the choice into exclude_list.txt so the next import keeps honoring it
    # (otherwise a re-import would re-count this person's messages).
    saved = _mirror_exclude(conn, row["name"], bool(value))
    if saved:
        return {"ok": True, "exclude_file_saved": True}
    return {"ok": True, "exclude_file_saved": False,
            "warning": EXCLUDE_FILE_UNWRITABLE}


def handle_exclude_list_get(conn: Any) -> Dict[str, Any]:
    """Return names in exclude_list.txt, flagging those with no DB record."""
    entries = []
    if os.path.exists(EXCLUDE_FILE):
        with open(EXCLUDE_FILE, encoding="utf-8") as fh:
            for line in fh:
                raw = line.split("#", 1)[0].strip()
                if raw:
                    entries.append(raw)
    result = []
    for name in entries:
        rows = db.find_exact(conn, name)
        in_db = bool(rows)
        result.append({"name": name, "in_db": in_db})
    return {"entries": result}


def handle_exclude_list_remove(body: Dict[str, Any], conn: Any = None) -> Dict[str, Any]:
    """Remove a name from exclude_list.txt and bring back the people it hid.

    NC-334. Adding a name hides every matching person at once; removing it used
    to edit only the file, so they stayed hidden with nothing on the list to say
    why. It now mirrors add: everyone whose normalised name matches comes back.
    Someone you deleted stays deleted, because deleting is a stronger choice
    than this list and is undone from the Deleted view. `conn` is what does the
    un-hiding, and the route always passes it."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError(HTTPStatus.BAD_REQUEST, "name required")
    saved = db.remove_from_exclude_file(EXCLUDE_FILE, name)
    unhidden = 0
    if conn is not None:
        unhidden = conn.execute(
            "UPDATE contacts SET excluded = ?, updated_at = datetime('now') "
            f"WHERE name_norm = ? AND {figures.population_where('hidden_only')}",
            (0, db.normalize_name(name))).rowcount
        conn.commit()
    if saved:
        return {"ok": True, "exclude_file_saved": True, "unhidden": unhidden}
    return {"ok": True, "exclude_file_saved": False, "unhidden": unhidden,
            "warning": "The skip list on disk could not be rewritten, so the "
                       "next import will still skip this name. Check that the "
                       "data folder is not full or read-only."}


def handle_exclude_list_add(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """Add a name to exclude_list.txt and hide any matching contacts right away
    (imports already honor the file, this just applies it without a re-import).
    Returns the refreshed list so the UI can update in one round-trip."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError(HTTPStatus.BAD_REQUEST, "name required")
    saved = db.add_to_exclude_file(EXCLUDE_FILE, name)
    if db.apply_exclude_list(conn, {db.normalize_name(name)}):
        conn.commit()
    out = handle_exclude_list_get(conn)
    out["exclude_file_saved"] = saved
    if not saved:
        out["warning"] = EXCLUDE_FILE_UNWRITABLE
    return out


def handle_set_howwemet(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    text = str(body.get("text") or "").strip()
    _require_contact(conn, contact_id)
    db.set_field(conn, contact_id, "how_we_met", text)
    conn.commit()
    return {"ok": True}


def handle_review_status(conn: Any, contact_id: int, status: str) -> Dict[str, Any]:
    """Mark a contact's review status so it drops out of the review batch. Mirrors
    the Telegram 'Skip' action; idempotent. `status` is from REVIEW_STATUS_ACTIONS."""
    _require_contact(conn, contact_id)
    db.mark_reviewed(conn, contact_id, status)
    conn.commit()
    return {"ok": True}


def handle_skip(conn: Any, contact_id: int) -> Dict[str, Any]:
    """Back-compat shim: skip == review status 'skipped'."""
    return handle_review_status(conn, contact_id, "skipped")


def handle_delete_contact(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/contact/{id}/delete — reversible soft-delete. {deleted: false}
    restores. Deleting a typo/junk contact used to be impossible in-app."""
    row = _require_contact(conn, contact_id)
    deleted = bool(body.get("deleted", True))
    db.set_deleted(conn, contact_id, 1 if deleted else 0)
    conn.commit()
    # NC-146. set_deleted() sets `excluded` too, and for a long time that was
    # where it stopped — so delete, the STRONGER of the two statements, was the
    # one the importers could not see. Same line hiding writes, both ways.
    saved = _mirror_exclude(conn, row["name"], deleted)
    out = {"ok": True, "deleted": deleted, "exclude_file_saved": saved}
    if not saved:
        out["warning"] = EXCLUDE_FILE_UNWRITABLE
    return out


PURGE_ARCHIVE_BUSY = ("Their messages could not be removed just now, so nothing was deleted. "
                      "Try again in a moment.")


def handle_purge_contact(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/contact/{id}/purge — PERMANENT delete. Irreversible, so
    the UI only offers it from the Deleted view behind an explicit confirm.
    Clears the contact's notes/identifiers/list memberships/photo in crm.db and
    its archived messages + chunks in archive.db.

    NC-172. That "only from the Deleted view" was enforced by the UI alone, and
    the UI is not the API. A purge of someone who was never deleted took the row
    away with nothing left behind to say so — and a row is the only thing an
    importer looks at. The next import would meet them as a stranger and create
    them again, which of the three possible outcomes is the worst one for a
    button that says "cannot be undone".

    So two things, in this order. Refuse unless they are already deleted, which
    is the state the UI guarantees anyway. Then put the name on the importers'
    skip list BEFORE the row is destroyed, because once it is gone that line is
    the only protection there is — and delete's own write to it can have failed
    (NC-150), in which case this is the last chance to notice.
    """
    row = _require_contact(conn, contact_id)
    if not row["deleted"]:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Delete this contact first. Permanent deletion is only offered for "
            "an already-deleted contact, because deleting is what tells the "
            "importers to stop collecting them.",
        )
    # NC-087. Their messages go first, or nothing goes. This used to purge the row,
    # commit, and only then try the archive inside a bare `except: pass`, so a busy
    # archive (an import holds it every couple of minutes) left their messages behind
    # under an id nobody has any more, counted by every archive figure and shown by
    # none, while the answer said 0 removed. Now the archive's delete runs first and
    # holds its write lock; if it cannot, nothing is touched and the person stays in
    # Deleted to try again. Committed after crm.db, so a failure in between leaves the
    # messages, never a person without them.
    removed = 0
    with arc.open_if_present() as arc_conn:
        if arc_conn is None and os.path.exists(arc.ARCHIVE_DB_PATH):
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, PURGE_ARCHIVE_BUSY)
        if arc_conn is not None:
            try:
                cur = arc_conn.execute("DELETE FROM messages WHERE contact_id = ?", (contact_id,))
                removed = cur.rowcount or 0
                arc_conn.execute("DELETE FROM chunks WHERE contact_id = ?", (contact_id,))
            except sqlite3.Error:
                log.exception("purge of contact %s: the message archive refused the delete", contact_id)
                arc_conn.rollback()
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, PURGE_ARCHIVE_BUSY)
        saved = _mirror_exclude(conn, row["name"], True)
        try:
            photos.delete_photo(conn, contact_id)
        except Exception:  # noqa: BLE001 — a missing photo file must not block the purge
            pass
        db.purge_contact(conn, contact_id)
        conn.commit()
        if arc_conn is not None:
            arc_conn.commit()
    out = {"ok": True, "purged": True, "messages_removed": removed,
           "exclude_file_saved": saved}
    if not saved:
        out["warning"] = EXCLUDE_FILE_UNWRITABLE
    return out


def handle_set_field(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    field = str(body.get("field") or "").strip()
    if field not in ALLOWED_FIELDS:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"field must be one of {sorted(ALLOWED_FIELDS)}",
        )
    value = str(body.get("value") or "").strip()
    if field == "name" and not value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "name cannot be empty")
    row = _require_contact(conn, contact_id)
    # Anti-clobber: never let an empty value silently wipe a field that already
    # has one (a stray blur, or the old Role·Company split bug that blanked the
    # half you didn't retype). Clearing on purpose requires an explicit clear.
    if not value and not body.get("clear") and (row[field] or "").strip():
        return {"ok": True, "unchanged": True}
    db.set_field(conn, contact_id, field, value)
    conn.commit()
    return {"ok": True}


def handle_set_photo(conn: Any, contact_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/contact/{id}/photo — body {data: <base64>}. Stored as a manual
    photo, which outranks every imported source."""
    import base64 as _b64
    raw = str(body.get("data") or "")
    if raw.lower().startswith("data:"):  # accept a full data URI from the browser
        raw = raw.partition(",")[2]
    if not raw:
        raise ApiError(HTTPStatus.BAD_REQUEST, "data (base64 image) is required")
    row = _require_contact(conn, contact_id)
    try:
        data = _b64.b64decode(re.sub(r"\s+", "", raw))
    except (ValueError, TypeError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid base64: {exc}") from exc
    if not photos.save_photo(conn, contact_id, data, "manual"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "not a usable JPEG/PNG/HEIC image")
    conn.commit()
    return {"ok": True, "photo": photos.photo_url(db.get_contact(conn, row["id"]))}


def handle_delete_photo(conn: Any, contact_id: int) -> Dict[str, Any]:
    _require_contact(conn, contact_id)
    photos.delete_photo(conn, contact_id)
    conn.commit()
    return {"ok": True}


def describe_candidates(conn: Any) -> List[Any]:
    """Every contact still waiting to be described (and not hidden).

    The view layer ranks and slices these; the query itself belongs to whoever
    owns the contacts table, which is this module.
    """
    return conn.execute(
        f"SELECT * FROM contacts WHERE status = 'new' AND {figures.population_where('visible')}"
    ).fetchall()


def skipped_today_count(conn: Any) -> int:
    """How many people you skipped in today's review — the 'come back later' pile."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE status = 'skipped' "
        "AND date(last_reviewed_at) = date('now')").fetchone()
    return (row["n"] if row else 0) or 0
