"""
Import Google contact photos via the People API — the last big compliant photo
source (profile pictures of the people you email, wherever they have a Google
account with a photo).

Reuses the Gmail importer's OAuth client credentials (gmail_credentials_*.json)
but keeps its OWN token files (google_photos_token_<slug>.json) because the
scopes differ — your Gmail tokens are untouched. First run per account opens a
browser tab once for consent; access is read-only contacts and revocable at
myaccount.google.com/permissions.

What it reads: your saved Google Contacts and the auto-collected "other
contacts" (people you've corresponded with) — names, emails, phone numbers,
photos. Matching is against people ALREADY in the CRM (email, then phone, then
exact name); default silhouette avatars are skipped; a photo you set yourself
or imported from your address book is never overwritten (photos.SOURCE_RANK).

Usage:
    python google_photos_import.py                       # every configured account
    python google_photos_import.py you@example.com       # one account
    python google_photos_import.py --dry-run             # count matches, write nothing
"""

import os
import re
import sys
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

import config  # loads .env, and owns the credential-file permissions
import db
import photos
from gmail_import import accounts_from_env, no_accounts_error

SCOPES = [
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts.other.readonly",
]
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))
PHOTO_SIZE = 512
FETCH_TIMEOUT = 20


def _token_path(account_email: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", account_email.lower())
    return config.secret_path(f"google_photos_token_{slug}.json")


def get_people_service(account_email: str, credentials_file: str):
    """Authenticated People API service; browser consent on first run only."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not os.path.exists(credentials_file):
        raise SystemExit(
            f"Missing {credentials_file} — set up Gmail import first "
            "(same OAuth client is reused here)."
        )
    token_path = _token_path(account_email)
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            print(f"\nOpening browser for {account_email} — click Allow in the Google tab.")
            creds = flow.run_local_server(port=0, open_browser=True)
        with open(token_path, "w") as fh:
            fh.write(creds.to_json())
        # NC-147. A refresh token for contacts.readonly — the whole Google
        # address book, renewable indefinitely — and open() leaves it 0644, so
        # it is made owner-only here. The Gmail importer does the same for its
        # token; the chmod itself lives in config so there is one copy of it.
        config.harden(token_path)
    return build("people", "v1", credentials=creds, cache_discovery=False)


# ── People API iteration ──────────────────────────────────────────────────────

def _paginate(request_fn, list_key: str) -> Iterable[dict]:
    page_token = None
    while True:
        resp = request_fn(page_token).execute()
        yield from resp.get(list_key, [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def iter_people(service) -> Iterable[dict]:
    """All saved contacts + auto-collected 'other contacts' for the account."""
    fields = "names,emailAddresses,phoneNumbers,photos"
    yield from _paginate(
        lambda tok: service.people().connections().list(
            resourceName="people/me", personFields=fields,
            pageSize=1000, pageToken=tok),
        "connections",
    )
    yield from _paginate(
        lambda tok: service.otherContacts().list(
            readMask="names,emailAddresses,phoneNumbers,photos",
            pageSize=1000, pageToken=tok),
        "otherContacts",
    )


def real_photo_url(person: dict) -> str:
    """The person's actual photo URL at PHOTO_SIZE, or '' for silhouettes."""
    for p in person.get("photos", []):
        if p.get("default") or p.get("metadata", {}).get("default"):
            continue
        url = p.get("url") or ""
        if url:
            return re.sub(r"=s\d+(-c)?$", f"=s{PHOTO_SIZE}", url) \
                if re.search(r"=s\d+(-c)?$", url) else url + f"=s{PHOTO_SIZE}"
    return ""


def person_identity(person: dict) -> Tuple[str, List[str], List[str]]:
    """(display name, emails, phones) from a People API person record."""
    names = person.get("names", [])
    name = (names[0].get("displayName") or "").strip() if names else ""
    emails = [e.get("value", "").strip().lower()
              for e in person.get("emailAddresses", []) if e.get("value")]
    phones = [p.get("value", "") for p in person.get("phoneNumbers", []) if p.get("value")]
    return name, emails, phones


def match_contact(conn, name: str, emails: List[str], phones: List[str]) -> Optional[int]:
    """CRM contact id for this Google person — email, then phone, then exact name."""
    for email in emails:
        row = db.find_by_handle(conn, email)
        if row is not None:
            return row["id"]
    for phone in phones:
        row = db.find_by_handle(conn, phone)
        if row is not None:
            return row["id"]
    if name:
        rows = db.find_exact(conn, name)
        if rows:
            return rows[0]["id"]
    return None


def fetch_photo(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:
            return resp.read()
    except Exception:  # noqa: BLE001 — a dead CDN URL just skips this person
        return b""


def import_account(account_email: str, credentials_file: str, *, dry_run: bool) -> Dict[str, int]:
    service = get_people_service(account_email, credentials_file)
    conn = db.get_db()
    db.ensure_columns(conn)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    stats = {"people": 0, "with_photo": 0, "matched": 0, "saved": 0,
             "kept_better": 0, "excluded": 0}
    seen_cids: set = set()

    for person in iter_people(service):
        stats["people"] += 1
        url = real_photo_url(person)
        if not url:
            continue
        stats["with_photo"] += 1
        name, emails, phones = person_identity(person)
        if name and db.normalize_name(name) in exclude_norms:
            stats["excluded"] += 1
            continue
        cid = match_contact(conn, name, emails, phones)
        if cid is None or cid in seen_cids:
            continue
        seen_cids.add(cid)
        stats["matched"] += 1
        if dry_run:
            continue
        # NC-082. Release before the fetch, commit after the write.
        #
        # photos.save_photo() UPDATEs contacts and does not commit (nothing in
        # crm/photos.py does — every caller is expected to). This loop's only
        # commit used to be the one after the loop, so the first saved photo took
        # crm.db's single writer lock and held it across every remaining person's
        # fetch_photo() — a urllib call to Google's photo CDN with FETCH_TIMEOUT =
        # 20s each. Dozens of matched contacts is enough to exceed the app's
        # 20-second WRITE_LOCK_TIMEOUT_SEC without anything going wrong at all,
        # and this runs as a subprocess while the app is live, so what fails is
        # the user's next edit or Ask question.
        #
        # Per-photo, not batched: the network fetch beside it costs hundreds of
        # milliseconds, so an fsync per photo is not measurable here. See
        # db.release_write_lock for when batching is the right answer instead.
        # Two backstops for one rule, and EITHER ALONE IS CORRECT: the release
        # below covers the previous iteration's write, the commit after
        # save_photo covers this one.
        db.release_write_lock(conn)
        data = fetch_photo(url)
        if data and photos.save_photo(conn, cid, data, "google"):
            stats["saved"] += 1
            conn.commit()
        else:
            stats["kept_better"] += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main() -> None:
    args = [a for a in sys.argv[1:]]
    dry_run = "--dry-run" in args
    wanted = [a for a in args if not a.startswith("-")]
    configured = accounts_from_env()
    if not configured:
        raise no_accounts_error()
    accounts = {k: v for k, v in configured.items() if not wanted or k in wanted}
    if not accounts:
        raise SystemExit(f"No matching account. Configured: {', '.join(configured)}")

    db.init_db()
    tag = "[dry-run] " if dry_run else ""
    for account, creds in accounts.items():
        print(f"\nGoogle contact photos for {account}:")
        s = import_account(account, creds, dry_run=dry_run)
        print(
            f"{tag}People seen:                 {s['people']}\n"
            f"{tag}With a real photo:           {s['with_photo']}\n"
            f"{tag}Matched to your CRM:         {s['matched']}\n"
            f"{tag}Photos saved/refreshed:      {s['saved']}\n"
            f"{tag}Kept existing better photo:  {s['kept_better']}\n"
            f"{tag}Skipped (excluded):          {s['excluded']}"
        )


if __name__ == "__main__":
    main()
