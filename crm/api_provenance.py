#!/usr/bin/env python3
"""What one import source actually put in this database — NC-037.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).
A route-registry module: it declares ROUTES and is listed in app.py's
ROUTE_MODULES, so it needs no branch in the route tables and no re-export block.

WHY THIS MODULE EXISTS
Each data source gets a row that reports what it brought. An exclude button
next to it could not be built honestly: photos are one file per person and a
lower-ranked source's bytes are overwritten rather than kept, and facts are a
whole-bundle write carrying a single source token — so "un-exclude Apple
Contacts' photo" would have nothing left to restore. What remains is the half
that is buildable: a row that reports what it brought.

THE ONE HARD PART, AND WHY THE MAP BELOW IS THE WHOLE DESIGN
A single UI source spans several provenance tokens in several different columns.
"LinkedIn" is `messages.channel='linkedin'` AND `contacts.source='linkedin'` AND
`identifiers.source='linkedin_csv'` AND `facts_source='linkedin_csv'`. Nothing in
the schema joins those, and nothing ever will — they are written by four
different functions in three different files. So the join lives here, once, as
SOURCE_TOKENS, and every query in this module is derived from it. Spread over two
files or inferred from a string prefix ("anything starting with linkedin_"), it
would drift the first time an importer renamed a token, which is the failure
this module exists to prevent.

Two things keep it from drifting anyway. TOKEN_WRITERS names not just the file
each token was read out of but the syntax it is written as, and
tests/test_provenance.py checks that exact position with `ast`. And the same test
file drives the importers' own writers and asks the panel where the result
landed, naming no token itself — so a rename fails the suite instead of quietly
reporting zero.

WHAT THIS CAN AND CANNOT HONESTLY ANSWER
Four of the six provenance columns are trustworthy and two are not, and the
difference is reported to the user rather than smoothed over:

  archive.db messages.channel  trustworthy. Survives a merge — db.merge_contacts
                               re-points the duplicate's rows at the kept contact
                               (_move_archived_messages).
  identifiers.source           trustworthy, and it survives a merge: the merge
                               moves each identifier through db.add_identifier
                               carrying its own source.
  contact_dates.source         trustworthy, and it survives a merge (NC-050).
                               db.merge_contacts' INSERT once selected
                               kind/label/on_date/year_known/recurring and NOT
                               `source`, so a folded record's dates fell back to
                               the column default — which is 'manual'. That did not
                               lose the provenance, it FORGED it, and invariant 4
                               would then freeze those dates against every future
                               correction. Fixed in db.py.
  contacts.source              only WHO CREATED THE ROW. It is written once by
                               db.create_contact / upsert_contact_from_linkedin
                               and never updated when a second source enriches
                               the person. So it answers "how did these people
                               first arrive", never "what does this source know".
  photo_source                 only the CURRENT WINNER. One file per person
                               (data/photos/<id>.jpg) and photos.save_photo
                               overwrites on rank, so a source that supplied a
                               photo and then lost the precedence race leaves no
                               trace at all.
  facts_source                 only the CURRENT WINNER, for the same reason in a
                               different shape: enrich_facts writes a whole
                               bundle under one token.

A shared token is the third honest gap. Three importers write
`identifiers.source='message_scan'` and two write `channel='email'`, so those
counts exist for the group and cannot be split per source. They are reported as
the group total with the sharers named, never divided by guesswork.

archive.db never leaves this machine (invariant 7). Only counts cross this
boundary — no bodies, no subjects, no names, no rows of any kind.

NC-122: these counts used to be unfiltered, on the
argument that a hidden or deleted person is still something the source brought.
On screen that argument never arrived. "People it first added: X of Y" sat
on the same page as "Z people", both under the word "people", with nothing
saying why they differed. Every count here is now over `active` — the people
you can see and use, the same people as every other count of people on the Data
page — and so are the phone numbers, dates, photos and facts that belong to
them. The one exception is the message COUNT, which stays every message saved
on this Mac, as it is everywhere else on the page; the people it names are
`active` like the rest.
"""

from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

import archive as arc
import db
import figures
import importers
from api_core import ApiError

ROUTES = {
    "GET": {"/api/provenance": "handle_provenance"},
}


# ── The six provenance columns ───────────────────────────────────────────────
# Ordered as the panel renders them: the bulk first, the derived last.
FIELDS: Tuple[str, ...] = (
    "messages", "people", "identifiers", "dates", "photos", "facts",
)

FIELD_LABELS = {
    "messages": "Messages archived",
    "people": "People it first added",
    "identifiers": "Phone numbers and emails",
    "dates": "Dates",
    "photos": "Photos still in use",
    "facts": "Facts still in use",
}

# The same six fields as a plain noun, for the one sentence that names what a
# source does NOT bring. FIELD_LABELS cannot do this job: "brings no photos still
# in use" is not English.
FIELD_NOUNS = {
    "messages": "messages",
    "people": "new people",
    "identifiers": "phone numbers and emails",
    "dates": "dates",
    "photos": "photos",
    "facts": "facts",
}

# NC-087. What each line's count and its `of_total` count, and over whom. The two
# numbers used to travel as bare integers under one field name that meant six
# different things (rows of messages, of phone numbers, of dates, and people);
# each line now carries a figure that says which. Messages are every message saved
# on this Mac; everything else is over the people the People page lists.
FIELD_FIGURES = {
    "messages": ("messages", "all", figures.SIDE_ARCHIVE),
    "people": ("people", "active", figures.SIDE_STORED),
    "identifiers": ("handles", "active", figures.SIDE_STORED),
    "dates": ("dates", "active", figures.SIDE_STORED),
    "photos": ("photos", "active", figures.SIDE_STORED),
    "facts": ("people", "active", figures.SIDE_STORED),
}

# What a shared-token note calls the column, in words a human reads.
FIELD_COLUMNS = {
    "messages": "message channel",
    "people": "record source",
    "identifiers": "identifier source",
    "dates": "date source",
    "photos": "photo source",
    "facts": "fact source",
}

# How much the column can be trusted for this field, independent of any source.
# "exact" is the default; the two liars are named here so no query has to
# remember which is which.
FIELD_CERTAINTY = {
    "people": "created_only",
    "photos": "current_only",
    "facts": "current_only",
}


# ── Every token, and the exact line of code that writes it ───────────────────
# (field, token) -> (module, anchor). Read out of the named file, never inferred
# from the token's spelling.
#
# The ANCHOR is what makes this a gate rather than a comment. A test that merely
# looked for the string "imessage" somewhere in imessage_import.py would pass
# even after `channel="imessage"` was renamed, because `source="imessage"` is in
# the same file — that mutation would pass without the anchors. So each entry
# names the syntax it is written as, and
# tests/test_provenance.py checks that shape with `ast`:
#
#   kw:<name>      a keyword argument, `<name>="<token>"`
#   arg:<callee>   a positional string argument to a call to <callee>
#   const:<NAME>   a module-level constant, `NAME = "<token>"`
#   sql            a quoted literal inside a longer SQL string, '<token>'
TOKEN_WRITERS: Dict[Tuple[str, str], Tuple[str, str]] = {
    # archive.db messages.channel — the `channel=` every importer passes to
    # archive.insert_message.
    ("messages", "imessage"): ("imessage_import.py", "kw:channel"),      # :660
    ("messages", "whatsapp"): ("whatsapp_import.py", "kw:channel"),      # :284, :435
    ("messages", "linkedin"): ("linkedin_import.py", "kw:channel"),      # :217
    ("messages", "email"): ("email_import.py", "kw:channel"),            # :455 (+gmail)

    # contacts.source — db.create_contact(source=…), or the literal in
    # db.upsert_contact_from_linkedin's INSERT.
    ("people", "contacts"): ("contacts_import.py", "kw:source"),         # :369
    ("people", "linkedin"): ("db.py", "sql"),                            # :444
    ("people", "imessage"): ("imessage_import.py", "kw:source"),         # :378, :384
    ("people", "whatsapp"): ("whatsapp_import.py", "kw:source"),         # :214
    ("people", "email"): ("email_import.py", "kw:source"),               # :303 (+gmail)

    # identifiers.source — db.add_identifier(source=…).
    ("identifiers", "contacts"): ("contacts_import.py", "const:IDENT_SOURCE"),   # :57
    ("identifiers", "linkedin_csv"): ("db.py", "kw:source"),             # :448, both routes
    ("identifiers", "message_scan"): ("imessage_import.py", "kw:source"),  # :392 (+wa, +email)

    # contact_dates.source — the literal in db.upsert_calendar_date's INSERT, and
    # db.upsert_birthday's `source` argument.
    ("dates", "calendar"): ("db.py", "sql"),                             # :2124
    ("dates", "contacts"): ("contacts_import.py", "const:IDENT_SOURCE"),  # :329

    # contacts.photo_source — the fourth argument to photos.save_photo.
    ("photos", "contacts"): ("contacts_import.py", "arg:save_photo"),        # :384
    ("photos", "whatsapp"): ("whatsapp_photos.py", "arg:save_photo"),        # :159
    ("photos", "google"): ("google_photos_import.py", "arg:save_photo"),     # :180
    ("photos", "linkedin"): ("import_scraped_photos.py", "const:PHOTO_SOURCE"),  # :70

    # contacts.facts_source — the source argument to enrich_facts._apply_changes,
    # the literal in seed_from_csv's UPDATE, and the scraper's own constant.
    ("facts", "offline"): ("enrich_facts.py", "arg:_apply_changes"),         # :439
    ("facts", "message_scan"): ("enrich_facts.py", "arg:_apply_changes"),    # :449
    ("facts", "llm"): ("enrich_facts.py", "arg:_apply_changes"),             # :516
    ("facts", "linkedin_csv"): ("seed_from_csv.py", "sql"),                  # :47
    ("facts", "linkedin_scrape"): ("scrape_linkedin_profiles.py", "const:FACTS_SOURCE"),  # :101
}

# ── source key -> the tokens it owns, per column ─────────────────────────────
# Keys are importers.REGISTRY keys and nothing else. A field a source does not
# write is simply absent, and the panel says so in words rather than showing a
# zero. `part_of` folds a sub-source's contributions into its parent's row, the
# same relationship crm/api_ledger.py's PART_OF carries for the Data page (NC-030).
#
# `manual` and `intake` tokens appear in these columns too and are deliberately
# owned by nobody: they are the user and the intake flow, not import sources.
SOURCE_TOKENS: Dict[str, Dict[str, Any]] = {
    "contacts": {
        "people": ("contacts",),
        "identifiers": ("contacts",),
        "dates": ("contacts",),
        "photos": ("contacts",),
    },
    "linkedin": {
        "messages": ("linkedin",),
        "people": ("linkedin",),
        "identifiers": ("linkedin_csv",),
        "facts": ("linkedin_csv",),
    },
    "imessage": {
        "messages": ("imessage",),
        "people": ("imessage",),
        "identifiers": ("message_scan",),
    },
    "whatsapp": {
        "messages": ("whatsapp",),
        "people": ("whatsapp",),
        "identifiers": ("message_scan",),
        # Rolled up from whatsapp_photos below: the cache lives inside WhatsApp's
        # own container, and NC-030 folded its row into this one.
        "photos": ("whatsapp",),
    },
    "whatsapp_photos": {
        "part_of": "whatsapp",
    },
    "email": {
        "messages": ("email",),
        "people": ("email",),
        "identifiers": ("message_scan",),
    },
    "gmail": {
        "messages": ("email",),
        "people": ("email",),
    },
    # All FOUR calendars import through ics_import.py's apply(), which writes the
    # one token 'calendar' — calendar_local (NC-042) reads Apple's own store but
    # hands the same event dicts to the same writer, deliberately, so there is no
    # second implementation of "which contact is this attendee". Which feed a
    # meeting arrived on is therefore not recorded, the four share the count, and
    # the panel says so rather than implying a per-row attribution it cannot make.
    "calendar": {"dates": ("calendar",)},
    "calendar_local": {"dates": ("calendar",)},
    "calendar_google": {"dates": ("calendar",)},
    "calendar_icloud": {"dates": ("calendar",)},
    "google_photos": {"photos": ("google",)},
    "facts": {"facts": ("offline", "message_scan")},
    "facts_llm": {"facts": ("llm",)},
    "linkedin_facts": {
        "facts": ("linkedin_scrape",),
        "photos": ("linkedin",),
    },
    # Two rows that write no provenance at all. They recompute numbers on people
    # who are already here, which is exactly NC-032's category error, and the
    # panel states it in one sentence instead of showing six zeros.
    "engagement": {},
    "style": {},
}


def tokens_for(source: str, field: str) -> Tuple[str, ...]:
    """The tokens `source` owns in `field` — () when it never writes that column.

    Resolves part_of first, so asking a sub-source gives its parent's answer.
    """
    entry = SOURCE_TOKENS.get(resolve_source(source), {})
    return tuple(entry.get(field) or ())


def resolve_source(source: str) -> str:
    """The key whose panel answers for `source` — itself, or its parent."""
    entry = SOURCE_TOKENS.get(source) or {}
    return str(entry.get("part_of") or source)


def _panel_keys() -> List[str]:
    """Every source that has a panel of its own (a sub-source has none)."""
    return [k for k, v in SOURCE_TOKENS.items() if not v.get("part_of")]


def _sharers(field: str, token: str) -> List[str]:
    """Panel keys that claim this token in this column, in registry order.

    Computed rather than declared, so a token that becomes shared by a new
    importer starts reporting as shared without anyone remembering to say so.
    """
    order = list(importers.REGISTRY)
    keys = [k for k in _panel_keys() if token in tokens_for(k, field)]
    return sorted(keys, key=lambda k: order.index(k) if k in order else len(order))


def _label(source: str) -> str:
    spec = importers.REGISTRY.get(source)
    return spec.label if spec else source


def _join(parts: List[str], last: str) -> str:
    """One item, "A and B", or "A, B and C" — a sentence either way."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" {last} " + parts[-1]


def _and_list(labels: List[str]) -> str:
    return _join(labels, "and")


def _or_list(nouns: List[str]) -> str:
    return _join(nouns, "or")


# ── Wording ──────────────────────────────────────────────────────────────────
# The sentences live here rather than in the page on purpose: what the app can
# and cannot know is the content of this feature, so it is asserted by tests
# instead of reviewed by eye. ASD-STE100 (DESIGN_DIRECTION §3): active voice,
# short sentences, one idea each.

# There was a FIELD_CAVEATS table here, for a gap belonging to the COLUMN rather
# than to how well it could be attributed — additive, so it could survive
# alongside a "shared" note. It held exactly one entry, about merges dropping
# contact_dates.source, and it is gone because that bug is fixed (NC-050): the
# panel stopped needing to apologise for it. Removed rather than left empty,
# because a caveat table with nothing in it is a branch no test can exercise and
# reads, later, as though caveats are being applied somewhere. If a real
# column-level gap turns up again it is four lines and a dict.


def _note(field: str, source: str, certainty: str, sharers: List[str]) -> str:
    label = _label(source)
    if certainty == "unavailable":
        # Nothing was read at all, so no other caveat about the column applies.
        return ("This Mac has no message archive yet, so there is nothing to "
                "count.")
    lead = ""
    if certainty == "shared":
        # Quoted, because a registry label can contain a comma of its own
        # ("Calendar, exported by hand") and an unquoted list of three then has
        # no readable boundary at all.
        others = _and_list([f"“{_label(k)}”" for k in sharers])
        lead = (f"The database records the {FIELD_COLUMNS[field]} and not which "
                f"import wrote it. This number covers {others} together. The app "
                "cannot split it.")
    elif certainty == "created_only":
        lead = (f"This counts the records {label} created. The app never rewrites "
                f"that column, so it says how these people first arrived. It does "
                f"not say what {label} knows about them now.")
    elif certainty == "current_only" and field == "photos":
        lead = (f"One photo is kept per person, and a better source overwrites it. "
                f"This counts the photos from {label} that are still in use. A "
                f"photo {label} gave you and then lost is not recorded anywhere, "
                f"so the app cannot tell you about it.")
    elif certainty == "current_only" and field == "facts":
        lead = (f"Facts are written as one bundle under one source name. This "
                f"counts the people whose bundle came from {label}. A fact "
                f"{label} gave you that a better source replaced is not recorded "
                f"anywhere, so the app cannot tell you about it.")
    return lead


NOTHING_RECORDED = ("Nothing in the database records {label}. It recomputes "
                    "values for people you already have, and adds no data of "
                    "its own.")


# ── Queries. Aggregates only: GROUP BY and COUNT, never a row. ───────────────

def _group(conn: Any, sql: str) -> Dict[str, int]:
    return {(r[0] or ""): int(r[1] or 0) for r in conn.execute(sql).fetchall()}


def _sum(counts: Dict[str, int], tokens: Tuple[str, ...]) -> int:
    return sum(counts.get(t, 0) for t in tokens)


def _crm_counts(conn: Any) -> Dict[str, Dict[str, Any]]:
    """Every crm.db provenance column, grouped, over the people you can see.

    NC-122: `active` everywhere. The tables that are not `contacts` join to it,
    because a phone number or a date belongs to a person and should leave the
    count when that person does."""
    on = figures.population_where("active")
    on_c = figures.population_where("active", "c")
    return {
        "people": {
            "by_token": _group(conn, f"SELECT source, COUNT(*) FROM contacts "
                                     f"WHERE {on} GROUP BY source"),
            "total": int(conn.execute(
                f"SELECT COUNT(*) FROM contacts WHERE {on}").fetchone()[0] or 0),
        },
        "identifiers": {
            "by_token": _group(conn, "SELECT i.source, COUNT(*) FROM identifiers i "
                                     "JOIN contacts c ON c.id = i.contact_id "
                                     f"WHERE {on_c} GROUP BY i.source"),
            "total": int(conn.execute(
                "SELECT COUNT(*) FROM identifiers i JOIN contacts c ON c.id = i.contact_id "
                f"WHERE {on_c}").fetchone()[0] or 0),
            "by_kind": [(str(r[0] or ""), str(r[1] or ""), int(r[2] or 0)) for r in conn.execute(
                "SELECT i.source, i.kind, COUNT(*) FROM identifiers i "
                "JOIN contacts c ON c.id = i.contact_id "
                f"WHERE {on_c} GROUP BY i.source, i.kind").fetchall()],
        },
        "dates": {
            "by_token": _group(conn, "SELECT d.source, COUNT(*) FROM contact_dates d "
                                     "JOIN contacts c ON c.id = d.contact_id "
                                     f"WHERE {on_c} GROUP BY d.source"),
            "total": int(conn.execute(
                "SELECT COUNT(*) FROM contact_dates d JOIN contacts c ON c.id = d.contact_id "
                f"WHERE {on_c}").fetchone()[0] or 0),
            "by_kind": [(str(r[0] or ""), str(r[1] or ""), int(r[2] or 0)) for r in conn.execute(
                "SELECT d.source, d.kind, COUNT(*) FROM contact_dates d "
                "JOIN contacts c ON c.id = d.contact_id "
                f"WHERE {on_c} GROUP BY d.source, d.kind").fetchall()],
        },
        "photos": {
            "by_token": _group(conn, "SELECT photo_source, COUNT(*) FROM contacts "
                                     f"WHERE photo != '' AND {on} GROUP BY photo_source"),
            "total": int(conn.execute(
                f"SELECT COUNT(*) FROM contacts WHERE photo != '' AND {on}").fetchone()[0] or 0),
        },
        "facts": {
            "by_token": _group(conn, "SELECT facts_source, COUNT(*) FROM contacts "
                                     f"WHERE facts_source != '' AND {on} GROUP BY facts_source"),
            "total": int(conn.execute("SELECT COUNT(*) FROM contacts "
                                      f"WHERE facts_source != '' AND {on}").fetchone()[0] or 0),
        },
    }


def _message_counts(inside: Optional[frozenset] = None) -> Optional[Dict[str, Any]]:
    """Per-channel message and people counts, or None when archive.db is absent.

    None and zero are different answers and the panel says different things about
    them, so they stay different all the way to the screen. Counts only cross
    this boundary — archive.db never leaves the machine (invariant 7).

    `inside` is the ids the PEOPLE count may include — `db.ids_in(conn,
    "active")`, passed in because archive.db has no `excluded` column (NC-122).
    An inclusion set rather than a list of ids to leave out, because an archive
    row can name a person who has no row in crm.db at all. None counts every
    person the archive names. The message count is not filtered: it is every
    message saved on this Mac.
    """
    with arc.open_if_present() as arc_conn:
        if arc_conn is None:
            return None
        try:
            # COUNT(*) wants every message; COUNT of PEOPLE must not count a
            # row that belongs to a conversation rather than a person, or every
            # channel with group chats reads one person too many (NC-111).
            rows = arc_conn.execute(
                "SELECT channel, COUNT(*) FROM messages GROUP BY channel").fetchall()
            pairs = arc_conn.execute(
                "SELECT DISTINCT channel, contact_id FROM messages "
                f"WHERE {arc.is_a_person()}").fetchall()
        except Exception:  # noqa: BLE001 — a locked/corrupt archive must not 500
            return None
    by_token = {str(r[0] or ""): int(r[1] or 0) for r in rows}
    people: Dict[str, int] = {}
    for channel, cid in pairs:
        if inside is None or cid in inside:
            people[str(channel or "")] = people.get(str(channel or ""), 0) + 1
    return {"by_token": by_token, "people": people,
            "total": sum(by_token.values())}


IRREGULAR = {"person": "people"}


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n:,} {word}"
    return f"{n:,} {IRREGULAR.get(word, word + 's')}"


def _kind_detail(rows: List[Tuple[str, str, int]], tokens: Tuple[str, ...]) -> str:
    """"12 meetings · 3 birthdays" — the breakdown a count alone hides."""
    per: Dict[str, int] = {}
    for token, kind, n in rows:
        if token in tokens:
            per[kind] = per.get(kind, 0) + n
    ordered = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(_plural(n, k) for k, n in ordered if n)


def _kind_entry(field: str, source: str, tokens: Tuple[str, ...],
                counts: Any, detail: str = "") -> Dict[str, Any]:
    """One line of the panel: a number, and what the number does not mean."""
    order = list(importers.REGISTRY)
    sharers = sorted({k for t in tokens for k in _sharers(field, t)},
                     key=lambda k: order.index(k) if k in order else len(order))
    if counts is None:
        certainty, count, total = "unavailable", None, None
    else:
        count = _sum(counts["by_token"], tokens)
        total = counts["total"]
        certainty = FIELD_CERTAINTY.get(field, "exact")
        # A shared token beats the column's own certainty: not knowing which of
        # three importers wrote a row is the larger gap, and stacking both
        # sentences on one line reads as hedging.
        if len(sharers) > 1:
            certainty = "shared"
    unit, population, side = FIELD_FIGURES[field]
    return {
        "kind": field,
        "label": FIELD_LABELS[field],
        "count": count,
        "of_total": total,
        "figure": (None if count is None else
                   figures.figure(count, unit, population, of=total, side=side)),
        "detail": detail,
        "certainty": certainty,
        "note": _note(field, source, certainty, sharers),
    }


def _panel(source: str, crm: Dict[str, Dict[str, Any]],
           msgs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One source's panel, from aggregates the caller already gathered.

    The aggregates are passed in rather than queried here so that answering for
    every source costs the same five statements as answering for one.
    """
    key = resolve_source(source)
    kinds: List[Dict[str, Any]] = []
    not_recorded: List[str] = []
    for field in FIELDS:
        tokens = tokens_for(key, field)
        if not tokens:
            not_recorded.append(FIELD_NOUNS[field])
            continue
        if field == "messages":
            detail = "" if msgs is None else _plural(
                _sum(msgs["people"], tokens), "person")
            kinds.append(_kind_entry(field, key, tokens, msgs, detail))
        else:
            counts = crm[field]
            detail = _kind_detail(counts.get("by_kind") or [], tokens)
            kinds.append(_kind_entry(field, key, tokens, counts, detail))
    # A source that records NOTHING gets one sentence saying so, not six lines of
    # "does not bring" — the empty state is a statement, never a list of blanks.
    records_nothing = not kinds
    missing = [] if records_nothing else not_recorded
    return {
        "source": key,
        "label": _label(key),
        "resolved_from": source if source != key else "",
        "kinds": kinds,
        "not_recorded": missing,
        "not_recorded_note": (f"{_label(key)} does not bring "
                              f"{_or_list(missing)}." if missing else ""),
        "records_nothing": records_nothing,
        "note": NOTHING_RECORDED.format(label=_label(key)) if records_nothing else "",
        "archive_present": msgs is not None,
    }


def handle_provenance(conn: Any, query: Dict[str, List[str]]) -> Dict[str, Any]:
    """GET /api/provenance[?source=<key>] — what a source put in the database.

    With `source`, one panel. Without, every panel: the aggregates are the same
    five statements either way, so the whole page costs no more than one row of
    it and the front end can open a panel with no second request.
    """
    wanted = (query.get("source", [""])[0] or "").strip()
    if wanted and wanted not in SOURCE_TOKENS:
        raise ApiError(HTTPStatus.NOT_FOUND, f"no source called '{wanted}'")
    crm = _crm_counts(conn)
    msgs = _message_counts(frozenset(db.ids_in(conn, "active")))
    keys = [wanted] if wanted else _panel_keys()
    return {"panels": [_panel(k, crm, msgs) for k in keys],
            # NC-122: who every count of people in these panels is about.
            "population": "active",
            "population_says": figures.population_says("active")}
