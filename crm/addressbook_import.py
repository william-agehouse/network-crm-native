"""
Import macOS Contacts straight from the address book, with nobody clicking anything.

WHY THIS EXISTS ALONGSIDE contacts_import.py
contacts_import.py reads a `.vcf` you exported by hand. That works, and it can
never be automatic: exporting is a human act in the Contacts app, so the CRM's
copy of your address book is only ever as fresh as the last time you remembered
to do it. That is why syncs have to be automatic, never manual (NC-029,
NC-036).

So this reads the address book itself — `AddressBook-v22.abcddb`, the SQLite
database Contacts.app keeps — and it reads it from the unprotected COPY that the
opt-in Full Disk Access helper writes, exactly as the iMessage and WhatsApp
importers do. That is what makes it safe to run on a timer.

IT DOES NOT READ THE PROTECTED ORIGINAL, and that is deliberate rather than
incidental. `~/Library/Application Support/AddressBook` is TCC-protected app
data: from an unprivileged process, `stat()` on the file inside it succeeds while
`listdir()` on the folder raises "Operation not permitted".
A background loop that touched the original would be the "consent dialog every
two minutes, attributed to python3" bug all over again — the one
importers.detect_unprotected_sources() exists to prevent. The default root is
therefore the copy, and only an explicit --dir can point this anywhere else.

WHAT IT WRITES
Nothing of its own. It turns each address-book record into the same VCard object
contacts_import.py produces from a `.vcf`, then hands the list to
contacts_import.apply(). Every rule about matching, creating, back-filling,
excluding and photo precedence therefore lives in exactly one place, and this
file cannot drift away from the vCard path's behaviour.

THE SCHEMA IS DISCOVERED, NOT ASSUMED
Column names in this database vary by macOS version, and the file cannot be
inspected to check without the permission this whole design avoids. So every optional column is looked up with PRAGMA table_info and simply
skipped when absent. Only three things are required: a record table with a name,
and that is it. A macOS release that renames ZORGANIZATION loses the company
back-fill and keeps everything else, instead of failing the import.

Usage:
    python addressbook_import.py                    # the helper's copy
    python addressbook_import.py --dry-run          # preview, write nothing
    python addressbook_import.py --dir ~/some/copy  # a copy somewhere else
    python addressbook_import.py --enrich-only      # never create, only enrich
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import config  # noqa: F401 — loads .env
import contacts_import
import db
from imessage_import import addressbook_paths, addressbook_reason, snapshot_uri

# The copy the FDA helper writes. Mirrors importers.IMSG_AUTO_DB / WA_AUTO_DB, and
# matches the Contacts destination in packaging/src/imessage_sync.swift — the two
# must agree or the import silently finds nothing.
AB_COPY_DIR = os.path.join(config.APP_SUPPORT_DIR, "addressbook")

# Where each piece of a person lives, when the column exists. Ordered by
# preference: the first present column wins, so a macOS version that has both
# ZORGANIZATION and ZDEPARTMENT still puts the organisation in `company`.
NAME_COLUMNS = ("ZFIRSTNAME", "ZLASTNAME")
COMPANY_COLUMNS = ("ZORGANIZATION",)
TITLE_COLUMNS = ("ZJOBTITLE", "ZTITLE")
BIRTHDAY_COLUMNS = ("ZBIRTHDAY",)
# Core Data stores a date as seconds since 2001-01-01 UTC, not the Unix epoch.
# Getting this wrong doesn't fail, it shifts every birthday by 31 years.
COREDATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# Whole-record fallback for a company card: Contacts stores those with no first
# or last name at all, and the organisation IS the name.
ORG_IS_NAME_COLUMN = "ZISCOMPANY"


def _columns(conn: sqlite3.Connection, table: str) -> set:
    """Column names of `table`, or an empty set if the table isn't there."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _first_present(have: set, wanted: Tuple[str, ...]) -> str:
    for col in wanted:
        if col in have:
            return col
    return ""


def _coredata_birthday(raw: Any) -> Tuple[str, int]:
    """One ZBIRTHDAY value -> ('YYYY-MM-DD', year_known) or ('', 1).

    Contacts stores a birthday as a Core Data timestamp: seconds since
    2001-01-01 UTC, which is 978307200 seconds after the Unix epoch. A birthday
    entered without a year is stored under year 1604 — Apple's sentinel — and
    that must survive as year_known=0 rather than becoming a 421-year-old
    contact. The conversion is done in UTC deliberately: the stored value is the
    day Contacts shows, and re-interpreting it in a local timezone is what moves
    a midnight birthday to the day before.
    """
    if raw is None or raw == "":
        return "", 1
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return "", 1
    try:
        when = COREDATA_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, OSError, ValueError):
        return "", 1
    if when.year in contacts_import.NO_YEAR_SENTINELS:
        return (when.replace(year=contacts_import.NO_YEAR_PLACEHOLDER)
                .date().isoformat(), 0)
    return when.date().isoformat(), 1


def store_paths(root: str = "") -> List[str]:
    """Every AddressBook store in the copy: the flat "On My Mac" one and one per
    account under Sources/.

    Public because importers._quick_count needs the same list: the Data page's
    preview number and this importer must count the same stores, or the row
    misreports the size of the address book (NC-043).
    """
    return addressbook_paths(root or AB_COPY_DIR)


def read_store(path: str) -> List[contacts_import.VCard]:
    """Every named person in one AddressBook store, as VCard records.

    Read-only and lock-free, through imessage_import.snapshot_uri — which picks
    `mode=ro` when a -wal is beside the file and `immutable=1` when there is not.
    That distinction is not cosmetic: `immutable=1` makes SQLite ignore the -wal
    ENTIRELY, and the address book's -wal is where its recent state lives (it can
    be several times larger than the main file and written minutes earlier). The
    same trap, with the same two flags, is written up at length in
    snapshot_uri's own docstring, so this module calls that function rather than
    choosing the flags itself.
    """
    conn = sqlite3.connect(snapshot_uri(path), uri=True)
    try:
        rec = _columns(conn, "ZABCDRECORD")
        if not rec or not (rec & set(NAME_COLUMNS)):
            return []
        company_col = _first_present(rec, COMPANY_COLUMNS)
        title_col = _first_present(rec, TITLE_COLUMNS)
        bday_col = _first_present(rec, BIRTHDAY_COLUMNS)
        cols = ["Z_PK"] + [c for c in NAME_COLUMNS if c in rec]
        for extra in (company_col, title_col, bday_col):
            if extra:
                cols.append(extra)
        by_pk: Dict[int, contacts_import.VCard] = {}
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM ZABCDRECORD"):
            vals = dict(zip(cols, row))
            first = (vals.get("ZFIRSTNAME") or "").strip()
            last = (vals.get("ZLASTNAME") or "").strip()
            company = (vals.get(company_col) or "").strip() if company_col else ""
            name = " ".join(x for x in (first, last) if x)
            # A company card has no person name; its organisation is the name.
            # Skipping those lost real entries from the address book, and the
            # vCard path never skipped them (Contacts writes FN for them).
            if not name:
                name = company
            if not name:
                continue
            card = contacts_import.VCard()
            card.name = name
            card.company = "" if company == name else company
            card.title = (vals.get(title_col) or "").strip() if title_col else ""
            if bday_col:
                card.birthday, card.birth_year_known = _coredata_birthday(
                    vals.get(bday_col))
            by_pk[vals["Z_PK"]] = card

        _attach(conn, by_pk, "ZABCDPHONENUMBER", "ZFULLNUMBER", "phones")
        _attach(conn, by_pk, "ZABCDEMAILADDRESS", "ZADDRESS", "emails")
        return list(by_pk.values())
    finally:
        conn.close()


def _attach(conn: sqlite3.Connection, by_pk: Dict[int, contacts_import.VCard],
            table: str, value_col: str, field: str) -> None:
    """Hang every phone or email on the record that owns it.

    Values go through the same normalisation the vCard path uses — canonical_phone
    for numbers, lower-case for addresses — so an identifier written from here
    collapses onto one written from a `.vcf` instead of becoming a second row."""
    have = _columns(conn, table)
    if not have or "ZOWNER" not in have or value_col not in have:
        return
    for owner, raw in conn.execute(f"SELECT ZOWNER, {value_col} FROM {table}"):
        card = by_pk.get(owner)
        if card is None or not raw:
            continue
        if field == "phones":
            value = db.canonical_phone(str(raw))
        else:
            value = str(raw).strip().lower()
            if "@" not in value:
                continue
        if value and value not in getattr(card, field):
            getattr(card, field).append(value)


def read_all(root: str = "") -> Tuple[List[contacts_import.VCard], str]:
    """(cards, reason) over every store under `root` — the copy dir by default.

    Merged across stores, deduplicated by name, because a Mac can hold both the
    flat store and one per account and the same person is often in both."""
    base = root or AB_COPY_DIR
    paths = addressbook_paths(base)
    if not paths:
        return [], addressbook_reason(base)
    merged: Dict[str, contacts_import.VCard] = {}
    for path in paths:
        for card in read_store(path):
            key = db.normalize_name(card.name)
            prev = merged.get(key)
            if prev is None:
                merged[key] = card
                continue
            # Same person in two stores: union the identifiers, keep the first
            # non-empty company/title. Dropping the duplicate outright would
            # throw away a phone number that is only in one of the two.
            for phone in card.phones:
                if phone not in prev.phones:
                    prev.phones.append(phone)
            for email in card.emails:
                if email not in prev.emails:
                    prev.emails.append(email)
            prev.company = prev.company or card.company
            prev.title = prev.title or card.title
            # A birthday with a real year beats a no-year one for the same person,
            # whichever store it came from — otherwise which of the two accounts
            # happened to be read first would decide whether an age is knowable.
            if card.birthday and (not prev.birthday
                                  or (card.birth_year_known
                                      and not prev.birth_year_known)):
                prev.birthday = card.birthday
                prev.birth_year_known = card.birth_year_known
    cards = list(merged.values())
    return cards, ("" if cards else "the address book holds no named contacts")


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args(argv: List[str]) -> Tuple[str, bool, bool]:
    root = ""
    for i, arg in enumerate(argv):
        if arg == "--dir" and i + 1 < len(argv):
            root = os.path.expanduser(argv[i + 1])
    return root, "--enrich-only" in argv, "--dry-run" in argv


def main() -> None:
    root, enrich_only, dry_run = _parse_args(sys.argv[1:])
    db.init_db()
    base = root or AB_COPY_DIR
    print(f"Reading macOS Contacts: {base}")
    cards, why = read_all(root)
    print(f"  {len(cards)} contacts read" + (f" — {why}" if why else ""))
    if not cards:
        # Exit 0, not an error. An absent copy is the normal state on a Mac where
        # the sync helper was never installed, and the row's job is to report
        # that, not to make the autosync loop retry a source that cannot appear.
        return
    stats = contacts_import.apply(cards, enrich_only=enrich_only, dry_run=dry_run)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"\n{tag}Contacts read:               {stats['parsed']}\n"
        f"{tag}Matched to existing contacts: {stats['matched']}\n"
        f"{tag}New contacts created:        {stats['created']}\n"
        f"{tag}Existing contacts updated:   {stats['updated']}\n"
        f"{tag}Birthdays imported:          {stats['birthdays']}"
        + (f" ({stats['birthdays_kept']} left as you set them)"
           if stats['birthdays_kept'] else "")
        + f"\n{tag}Skipped (exclude_list):      {stats['skipped_excluded']}\n"
        f"{tag}Skipped (no name):           {stats['skipped_no_name']}"
    )


if __name__ == "__main__":
    main()
