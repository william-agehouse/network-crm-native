"""
Import an Apple Contacts vCard (.vcf) export into crm.db — runs entirely on your Mac.

This is the primary seeding path for people who aren't on LinkedIn. You export your
whole address book from the Contacts app and point this script at the resulting
Contacts.vcf file. Nothing needs special macOS permission — it's just a file you
chose to export.

Unlike the message importers (iMessage/WhatsApp/email/…), which only enrich people
you actually talk to, the address book is your curated list of people you know — so
by default we DO create a contact for anyone new in it, and enrich the ones already
in the CRM (e.g. LinkedIn connections) with the phones/emails/company/title from
the card.

For each card we:
  • match it to an existing contact by name, by a phone identifier (last-9-digits),
    or by an email identifier (case-insensitive), then
  • add any new phone/email identifiers and back-fill an empty company/title, or
  • create a new contact (source=contacts) and attach all its phones/emails.

People in exclude_list.txt (anyone you chose never to index) and cards with no name
are skipped. Pass --enrich-only to update existing contacts without ever creating
new ones; --dry-run to preview without writing.

It reads only the address-book fields it needs (name, phones, emails, org, title,
photo). Embedded PHOTO data is stored locally in data/photos/ so faces show up in
the app — nothing private is summarized or sent anywhere. (Enable "Export photos
in vCards" in Contacts → Settings → vCard before exporting to include them.)

Usage:
    python contacts_import.py Contacts.vcf                 # import all
    python contacts_import.py Contacts.vcf --dry-run        # preview, write nothing
    python contacts_import.py Contacts.vcf --enrich-only    # update existing only, no creation
"""

import base64
import os
import re
import sys
from datetime import date
from typing import Dict, List, Optional, Tuple

import config  # noqa: F401 — loads .env
import db
import photos

HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))


# Provenance for identifiers this importer writes, on the facts_source ladder
# (invariant 4, ranked in db.IDENT_SOURCE_RANK). An address-book export is a
# file a script parsed, not something typed into the app, so it sits in the same
# tier as the other file-backed imports and loses to 'manual' when the same
# number is spelled two ways. Same token photos.SOURCE_RANK already uses for
# this importer.
IDENT_SOURCE = "contacts"


# ── Helpers ─────────────────────────────────────────────────────────────────
def normalize_phone(s: str) -> str:
    """Last 9 digits — the *matching* key, not the stored form.

    Mirrors imessage_import.normalize_phone and db.phone_key so a phone matches
    the same contact no matter which importer wrote it. What gets stored is
    db.canonical_phone(), which keeps the country code."""
    d = re.sub(r"\D", "", s or "")
    return d[-9:] if len(d) >= 9 else d


# ── vCard parsing ─────────────────────────────────────────────────────────────
class VCard:
    """A single parsed vCard record."""

    __slots__ = ("name", "phones", "emails", "company", "title", "photo",
                 "birthday", "birth_year_known")

    def __init__(self) -> None:
        self.name = ""
        self.phones: List[str] = []
        self.emails: List[str] = []
        self.company = ""
        self.title = ""
        self.photo: bytes = b""
        # 'YYYY-MM-DD' or "". When the source gave a day but no year the date
        # carries a placeholder year and birth_year_known is 0, so nothing ever
        # computes an age from it.
        self.birthday = ""
        self.birth_year_known = 1


def unfold_lines(text: str) -> List[str]:
    """Undo RFC-6350 line folding: a line starting with a space or tab continues
    the previous one. The leading whitespace character is the fold marker and is
    dropped; any remaining content (including a space that was part of the value)
    is appended directly."""
    out: List[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line[:1] in (" ", "\t") and out:
            # Apple Contacts folds at word boundaries, so the leading space on
            # the continuation is the word separator. Keep it intact so that
            # "FN:Carol\r\n Carlsson" unfolds to "FN:Carol Carlsson".
            out[-1] += line  # leading space is content, not just a fold marker
        else:
            out.append(line)
    return out


def split_field(line: str) -> Tuple[str, str]:
    """Split a vCard line into (field-with-params, value) on the FIRST ':' only.

    'TEL;TYPE=CELL:+46…' -> ('TEL;TYPE=CELL', '+46…'). Returns ('', '') if the
    line has no colon."""
    idx = line.find(":")
    if idx == -1:
        return "", ""
    return line[:idx], line[idx + 1:]


def field_name(field: str) -> str:
    """The bare field name before any ';' params, upper-cased, with any vCard
    group prefix dropped. 'EMAIL;TYPE=WORK' -> 'EMAIL', 'item1.PHOTO;…' -> 'PHOTO'."""
    bare = field.split(";", 1)[0].strip().upper()
    return bare.split(".")[-1]


def decode_photo(field: str, value: str) -> bytes:
    """Decode an embedded vCard PHOTO value to raw image bytes, or b''.

    Handles vCard 3.0 base64 (`PHOTO;ENCODING=b;TYPE=JPEG:…`, Apple's export
    format) and vCard 4.0 data URIs. URL photos are ignored — this importer
    never fetches anything from the network."""
    params = field.upper()
    payload = ""
    if "ENCODING=B" in params or "BASE64" in params:
        payload = value
    elif value.lower().startswith("data:image/"):
        _, _, payload = value.partition(",")
    if not payload:
        return b""
    try:
        # unfold_lines keeps the fold-continuation space; base64 ignores it once stripped
        return base64.b64decode(re.sub(r"\s+", "", payload))
    except (ValueError, TypeError):
        return b""


def parse_vcards(path: str) -> List[VCard]:
    """Parse a .vcf file (UTF-8) into a list of VCard records.

    Records run BEGIN:VCARD … END:VCARD. We collect FN/TEL/EMAIL/ORG/TITLE,
    allow multiple TEL and EMAIL lines, normalize phones, lowercase emails, take
    the first ';'-separated part of ORG, and skip any PHOTO line."""
    with open(path, encoding="utf-8") as fh:
        lines = unfold_lines(fh.read())

    cards: List[VCard] = []
    current: Optional[VCard] = None
    for line in lines:
        field, value = split_field(line)
        if not field:
            continue
        name = field_name(field)
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            current = VCard()
            continue
        if name == "END" and value.strip().upper() == "VCARD":
            if current is not None:
                cards.append(current)
            current = None
            continue
        if current is None:
            continue
        if name == "PHOTO":
            if not current.photo:
                current.photo = decode_photo(field, value)
            continue
        _absorb_field(current, name, value)
    return cards


# Apple writes birthdays as a bare year sentinel when you leave the year out, and
# the sentinel is not the same one in every export. 1604 is what the macOS Contacts
# Core Data store uses; 1900 and 0000 both appear in vCards in the wild. Any of
# them means "the day is real, the year is not" — year_known=0, so no age is ever
# computed from a placeholder.
NO_YEAR_SENTINELS = (1604, 1900, 0)
# The year a no-year birthday is stored under. It has to be a real, non-leap-safe
# year because on_date is always a full 'YYYY-MM-DD' (see the schema comment), and
# 1604 keeps the sentinel visible in the raw data rather than inventing a year that
# looks plausible.
NO_YEAR_PLACEHOLDER = 1604


def parse_bday(value: str) -> Tuple[str, int]:
    """One vCard BDAY value -> ('YYYY-MM-DD', year_known) or ('', 1).

    Handles every form Apple and Google actually emit: '19850315', '1985-03-15',
    '--0315' and '--03-15' (vCard 4.0, no year), a datetime suffix
    ('1985-03-15T00:00:00Z'), and the year sentinels above. Anything else yields
    '' — a birthday guessed wrong is worse than one missing, because it surfaces
    on the wrong day every year afterwards.
    """
    v = (value or "").strip()
    if not v:
        return "", 1
    v = v.split("T", 1)[0].strip()          # drop any time part
    m = re.fullmatch(r"--(\d{2})-?(\d{2})", v)
    if m:                                    # no year at all
        month, day = int(m.group(1)), int(m.group(2))
        year, known = NO_YEAR_PLACEHOLDER, 0
    else:
        m = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", v)
        if not m:
            return "", 1
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        known = 0 if year in NO_YEAR_SENTINELS else 1
        if not known:
            year = NO_YEAR_PLACEHOLDER
    try:
        return date(year, month, day).isoformat(), known
    except ValueError:                       # 31 February and friends
        return "", 1


def _absorb_field(card: VCard, name: str, value: str) -> None:
    """Apply one parsed field to the card it belongs to."""
    value = value.strip()
    if name == "FN":
        card.name = value
    elif name == "TEL":
        # Keep the number whole. This used to store normalize_phone(value) — the
        # last-9-digits *matching* key — which threw the country code away, so a
        # card that also arrived over iMessage left the contact holding both
        # '700000000' and '+46700000000'. Matching still keys on the last nine
        # digits; that is a lookup concern, not a storage one.
        phone = db.canonical_phone(value)
        if phone:
            card.phones.append(phone)
    elif name == "EMAIL":
        email = value.lower()
        if "@" in email:
            card.emails.append(email)
    elif name == "ORG" and not card.company:
        card.company = value.split(";", 1)[0].strip()
    elif name == "TITLE" and not card.title:
        card.title = value
    elif name == "BDAY" and not card.birthday:
        day, known = parse_bday(value)
        if day:
            card.birthday = day
            card.birth_year_known = known


# ── Matching to existing contacts ─────────────────────────────────────────────
def build_identifier_index(conn, kind: str) -> Dict[str, int]:
    """{normalized identifier value: contact_id} for one identifier kind.

    Phones are keyed by last-9-digits; everything else (email) is lower-cased."""
    idx: Dict[str, int] = {}
    for r in conn.execute("SELECT contact_id, value FROM identifiers WHERE kind = ?", (kind,)):
        raw = r["value"] or ""
        key = normalize_phone(raw) if kind == "phone" else raw.strip().lower()
        if key:
            idx.setdefault(key, r["contact_id"])
    return idx


def match_contact(conn, card: VCard, phone_idx: Dict[str, int],
                  email_idx: Dict[str, int]) -> Optional[int]:
    """Resolve a card to an EXISTING contact id, or None.

    (a) name matches an existing contact (normalized), then
    (b) any of its phones matches a phone identifier (last-9-digits), then
    (c) any of its emails matches an email identifier (case-insensitive)."""
    existing = db.find_exact(conn, card.name)
    if existing:
        return existing[0]["id"]
    for phone in card.phones:
        cid = phone_idx.get(normalize_phone(phone))   # index is keyed last-9
        if cid is not None:
            return cid
    for email in card.emails:
        cid = email_idx.get(email)
        if cid is not None:
            return cid
    return None


# ── Apply to crm.db ───────────────────────────────────────────────────────────
def _add_card_identifiers(conn, cid: int, card: VCard) -> None:
    """Attach all of a card's phones and emails as identifiers.

    db.add_identifier canonicalises and collapses, so re-importing the same
    export writes nothing new (invariant 3) and a number already on the contact
    in another spelling stays one row."""
    for phone in card.phones:
        db.add_identifier(conn, cid, "phone", phone, source=IDENT_SOURCE)
    for email in card.emails:
        db.add_identifier(conn, cid, "email", email, source=IDENT_SOURCE)


def _backfill_company_title(conn, cid: int, card: VCard) -> None:
    """Fill company/position only when currently empty — never clobber edits."""
    if card.company:
        conn.execute(
            "UPDATE contacts SET company = ? WHERE id = ? AND (company IS NULL OR company = '')",
            (card.company, cid),
        )
    if card.title:
        conn.execute(
            "UPDATE contacts SET position = ? WHERE id = ? AND (position IS NULL OR position = '')",
            (card.title, cid),
        )


def _absorb_birthday(conn, cid: int, card: VCard, stats: dict) -> None:
    """Write the card's birthday, if it has one, and count what happened.

    Split out because both the matched and the created path need it and the two
    branches of apply() are far apart — the photo write below has the same shape
    and the same reason. "kept" counts a date the import declined to touch
    because the owner had set it themselves; it is not a failure and must not read as
    one in the summary line.
    """
    if not card.birthday:
        return
    verdict = db.upsert_birthday(conn, cid, card.birthday,
                                 year_known=card.birth_year_known,
                                 source=IDENT_SOURCE)
    if verdict in ("created", "moved"):
        stats["birthdays"] += 1
    elif verdict == "refused_manual":
        stats["birthdays_kept"] += 1


def apply(cards: List[VCard], *, enrich_only: bool, dry_run: bool) -> dict:
    conn = db.get_db()
    db.ensure_columns(conn)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    phone_idx = build_identifier_index(conn, "phone")
    email_idx = build_identifier_index(conn, "email")
    stats = {"parsed": len(cards), "matched": 0, "created": 0, "updated": 0,
             "photos": 0, "skipped_excluded": 0, "skipped_no_name": 0,
             "birthdays": 0, "birthdays_kept": 0}

    for card in cards:
        if not card.name:
            stats["skipped_no_name"] += 1
            continue
        if db.normalize_name(card.name) in exclude_norms:
            stats["skipped_excluded"] += 1
            continue

        cid = match_contact(conn, card, phone_idx, email_idx)
        if cid is not None:
            stats["matched"] += 1
            stats["updated"] += 1
            if not dry_run:
                _add_card_identifiers(conn, cid, card)
                _backfill_company_title(conn, cid, card)
                _absorb_birthday(conn, cid, card, stats)
        elif enrich_only:
            continue  # no match and creation disabled — nothing to do
        else:
            stats["created"] += 1
            if not dry_run:
                cid = db.create_contact(conn, card.name, source="contacts")
                if card.company:
                    db.set_field(conn, cid, "company", card.company)
                if card.title:
                    db.set_field(conn, cid, "position", card.title)
                _add_card_identifiers(conn, cid, card)
                _absorb_birthday(conn, cid, card, stats)

        # keep the in-memory index current so two cards for the same new person
        # (or a later card sharing a phone) resolve to the one row we just made
        if cid is not None and not dry_run:
            for phone in card.phones:
                phone_idx.setdefault(normalize_phone(phone), cid)
            for email in card.emails:
                email_idx.setdefault(email, cid)
            if card.photo:
                # NC-082. photos.save_photo() shells out to `sips` to normalise
                # the image (crm/photos.py _normalize_image, timeout=30), and the
                # writes above for this card — create_contact, set_field,
                # identifiers, birthday — are still uncommitted. Without this
                # release, every vCard with a photo holds crm.db's writer lock
                # across a 30-second subprocess ceiling, and an address book of a
                # few hundred cards holds it for most of the import.
                #
                # This is the one site where per-item committing is a real trade:
                # the loop is otherwise a bulk write and holding the lock while
                # writing is correct (see db.release_write_lock). The photo cards
                # are the minority and each carries a subprocess, so committing
                # around them costs an fsync where there was already a fork.
                # Two backstops, either alone correct (see the same note in
                # google_photos_import.py). Removing both is caught.
                db.release_write_lock(conn)
                if photos.save_photo(conn, cid, card.photo, "contacts"):
                    stats["photos"] += 1
                    conn.commit()

    if not dry_run:
        conn.commit()
    conn.close()
    return stats


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args(argv: List[str]) -> Tuple[Optional[str], bool, bool]:
    path: Optional[str] = None
    enrich_only = "--enrich-only" in argv
    dry_run = "--dry-run" in argv
    for arg in argv[1:]:
        if not arg.startswith("-"):
            path = arg
            break
    return path, enrich_only, dry_run


def main() -> None:
    path, enrich_only, dry_run = _parse_args(sys.argv)

    if not path:
        raise SystemExit(
            "Usage: python contacts_import.py <Contacts.vcf> "
            "[--enrich-only] [--dry-run]"
        )
    if not os.path.exists(path):
        raise SystemExit(f"vCard file not found: {path}")

    db.init_db()
    print(f"Reading vCard: {path}")
    cards = parse_vcards(path)
    print(f"  {len(cards)} cards parsed")

    stats = apply(cards, enrich_only=enrich_only, dry_run=dry_run)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"\n{tag}Parsed cards:                {stats['parsed']}\n"
        f"{tag}Matched to existing contacts: {stats['matched']}\n"
        f"{tag}New contacts created:        {stats['created']}\n"
        f"{tag}Existing contacts updated:   {stats['updated']}\n"
        f"{tag}Photos imported:             {stats['photos']}\n"
        f"{tag}Birthdays imported:          {stats['birthdays']}"
        + (f" ({stats['birthdays_kept']} left as you set them)"
           if stats['birthdays_kept'] else "")
        + f"\n{tag}Skipped (exclude_list):      {stats['skipped_excluded']}\n"
        f"{tag}Skipped (no name):           {stats['skipped_no_name']}"
    )


if __name__ == "__main__":
    main()
