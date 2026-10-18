"""
Import Gmail messages into archive.db for contacts already in crm.db.

On first run for each account, a browser tab opens for Google OAuth consent.
Tokens are stored locally in gmail_token_<account>.json — no password is ever
stored and access can be revoked any time from myaccount.google.com/permissions.

Only messages to/from email addresses already linked to crm.db contacts are
fetched — the importer does NOT download your entire inbox.

Usage:
    python gmail_import.py                          # every account in the GMAIL_ACCOUNTS setting
    python gmail_import.py you@example.com          # single account
    python gmail_import.py --dry-run                # preview match counts, no writes
    python gmail_import.py --since YYYY-MM-DD       # only messages after this date
    python gmail_import.py --repair                 # re-fetch messages an old
                                                    # import cut at 5,000 chars
    python gmail_import.py --scan                   # sweep all mail, match by name
    python gmail_import.py --connect                # sign in only, write the token, stop
    python gmail_import.py --no-browser             # never open a sign-in tab; exit 4
                                                    # if consent is the only way on
    python gmail_import.py --scan --create-new      # also create contacts for new people
                                                    # appearing ≥3 times who look like
                                                    # a real person (not a service/bot)

Setup:
    1. Get an OAuth client JSON from Google Cloud Console → APIs & Services →
       Credentials, and save it into the secrets folder.
    2. Set GMAIL_ACCOUNTS in Settings, comma-separated, as either
       `you@example.com` or `you@example.com:that_credentials_file.json`.
    3. Run — browser opens once per account for consent, then auto-refreshes forever.

This is a lot of setup, and it is inherent rather than lazy: gmail.readonly is a
Google *restricted* scope, so a shared client that anyone could just click through
would need OAuth brand verification plus an annual third-party CASA security
assessment. Until then such a client is capped at 100 users behind an "unverified
app" warning. If that trade is not worth it, the Email source imports a Google
Takeout .mbox with no account access at all.
"""

import base64
import os
import random
import time
import re
import sys
from datetime import datetime
from email.header import decode_header as _decode_header
from typing import Dict, List, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import email_text
import config  # loads .env, and owns the credential-file permissions
import archive as arc
import db
import figures  # NC-087: the one spelling of a contact-scope filter
# NC-180. accounts_from_env and the token path moved to a module with no Google
# imports in it, so the server can ask "is this account able to run" on every
# Data page load without pulling google.auth into the web process. Imported back
# here so there is still one parser for GMAIL_ACCOUNTS, not two.
from gmail_accounts import accounts_from_env, token_path as _token_path
# NC-177. What this script exits with when somebody has to sign in to Google
# again and this run was not allowed to ask them — deliberately not 1, so the
# background loop can tell "you need to click something" apart from "the import
# broke". Defined in importers.py, which owns both ends of that contract.
from importers import EXIT_NEEDS_SIGN_IN, SIGN_IN_MARKER

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def no_accounts_error() -> SystemExit:
    """Tell the user how to fix it, naming the credential files actually present.

    An empty GMAIL_ACCOUNTS is the normal state on a fresh install, so this has
    to read as a setup step rather than a failure — and it has to be
    self-solving. Listing what is in the secrets dir does both.
    """
    try:
        found = sorted(
            f for f in os.listdir(config.secret_path(""))
            if f.startswith("gmail_credentials_") and f.endswith(".json")
        )
    except OSError:
        found = []
    lines = [
        "No Gmail accounts configured.",
        "",
        "Set GMAIL_ACCOUNTS in Settings to a comma-separated list, either",
        "`you@example.com` or `you@example.com:credentials_file.json`.",
    ]
    if found:
        lines += [
            "",
            f"Credential files already in your secrets folder: {', '.join(found)}",
            "Pair each with the address it belongs to, e.g.",
            f"  you@example.com:{found[0]}",
        ]
    else:
        lines += [
            "",
            "You will also need an OAuth client JSON from Google Cloud Console →",
            "APIs & Services → Credentials, saved into the secrets folder.",
            "If that is more setup than you want, import a Google Takeout .mbox",
            "with the Email source instead — it needs no account access at all.",
        ]
    return SystemExit("\n".join(lines))


# ── Auth ──────────────────────────────────────────────────────────────────────

class NeedsSignIn(Exception):
    """Google consent is required, and this run must not go and ask for it."""

    def __init__(self, account_email: str, why: str):
        self.account_email = account_email
        self.why = why
        super().__init__(f"{account_email} needs you to sign in to Google again "
                         f"— {why}.")


def browser_allowed(argv: Optional[List[str]] = None) -> bool:
    """May this run open a browser tab on somebody's screen?

    No, when either gate says no, and both are load-bearing:

    * `--no-browser` in the arguments. The explicit one — the scheduled run
      passes it (crm/importers.py::_cmd_gmail) and you can read it in the argv
      of a running process.
    * `NC_UNATTENDED=1` in the environment. The backstop — the background loop
      sets it on every child it spawns (crm/api_import.py::run_autosync), so an
      importer added later that decides to prompt, or a registry entry that
      forgets the flag, still cannot open a tab from a timer.

    NOT `NC_NO_BROWSER`, which already exists and means something else: "the
    native window is showing the UI, so the server must not also pop open a
    browser tab". The packaged .app sets it on the ENGINE it starts
    (native/Sources/Engine.swift), and the engine's environment is what every
    child inherits — so reusing that name would have made the shipped app refuse
    to sign in when somebody clicked the button, which is the one thing this fix
    must never do. `NC_UNATTENDED` says the true thing anyway: nobody is
    watching this run.

    Neither is a knob and neither is offered anywhere a person can reach: this
    is not a preference about browsers, it is the difference between a window
    somebody asked for and a window that appeared on its own.
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--no-browser" in argv:
        return False
    return os.environ.get("NC_UNATTENDED", "").strip().lower() not in ("1", "true", "yes")


def get_service(account_email: str, credentials_file: str,
                allow_browser: Optional[bool] = None):
    """Return an authenticated Gmail API service for the given account.

    A run somebody started opens a browser tab for Google's consent page the
    first time and writes a token file; later runs load that token and refresh
    it by themselves, with no tab and no clicking.

    A run nobody started must never open that tab — see NC-177. `allow_browser`
    is that line: left None it asks browser_allowed(), so the three call sites
    in this file get the safe answer without having to remember to pass it, and
    a caller that means it can still say so outright. When the answer is no and
    consent is the only way forward, this raises NeedsSignIn instead of opening
    anything, and main() turns that into EXIT_NEEDS_SIGN_IN so the Data page can
    say "Gmail needs you to sign in again" and wait to be clicked."""
    allow = browser_allowed() if allow_browser is None else bool(allow_browser)
    if not os.path.exists(credentials_file):
        raise SystemExit(
            f"Missing {credentials_file}\n"
            "Download it from Google Cloud Console → APIs & Services → "
            "Credentials → your OAuth client → Download JSON."
        )
    token_path = _token_path(account_email)
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError as exc:
                # The person revoked this app at myaccount.google.com, or Google
                # retired the grant. A refresh token that has been withdrawn
                # cannot be repaired by trying again — the only cure is consent,
                # so this lands in exactly the same place as no token at all.
                if not allow:
                    raise NeedsSignIn(
                        account_email,
                        "the permission you gave Google was withdrawn or expired") from exc
                creds = None
        if not refreshed:
            if not allow:
                raise NeedsSignIn(
                    account_email,
                    "there is no saved Google permission left to use")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            print(f"\nOpening browser for {account_email} — click Allow in the Google tab.")
            creds = flow.run_local_server(port=0, open_browser=True)
        with open(token_path, "w") as fh:
            fh.write(creds.to_json())
        # This token grants read access to the whole mailbox, and open() leaves
        # it 0644. Owner-only, like every other credential this project writes.
        config.harden(token_path)
        print(f"  Token saved to {os.path.basename(token_path)}")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── Email address helpers ─────────────────────────────────────────────────────

def _decode_str(s: str) -> str:
    parts = _decode_header(s or "")
    result = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result)


_ADDR_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.[a-z]{2,}", re.I)


def _extract_addresses(header_value: str) -> List[str]:
    return [m.lower() for m in _ADDR_RE.findall(header_value or "")]


def _header(msg_payload: dict, name: str) -> str:
    for h in msg_payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ── Body extraction ───────────────────────────────────────────────────────────

def _decode_body_part(part: dict) -> str:
    data = (part.get("body") or {}).get("data", "")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """Readable text from a Gmail API payload — the WHOLE message (NC-142).

    Plain text is preferred; HTML is the fallback rather than nothing, which is
    what an HTML-only email used to store. There is no character cap and nothing
    is dropped for what it says: a quoted line is often where someone answered
    inline, and text below a "--" is still text they sent. See email_text.py for
    the reasoning behind that change.
    """
    html_fallback = ""

    def walk(node: dict) -> str:
        nonlocal html_fallback
        mime = node.get("mimeType", "")
        parts = node.get("parts") or []
        if not parts:
            if mime.startswith("text/plain"):
                return _decode_body_part(node)
            if mime.startswith("text/html") and not html_fallback:
                html_fallback = _decode_body_part(node)
            return ""
        for part in parts:
            pm = part.get("mimeType", "")
            # An attached .txt or .html file is a file, not the message.
            if (part.get("filename") or "").strip():
                continue
            if pm.startswith("text/plain"):
                return _decode_body_part(part)
            if pm.startswith("text/html") and not html_fallback:
                html_fallback = _decode_body_part(part)
            if pm.startswith("multipart/"):
                found = walk(part)
                if found:
                    return found
        return ""

    plain = walk(payload)
    if plain.strip():
        return email_text.tidy(plain)
    return email_text.html_to_text(html_fallback)


# ── Core import logic ─────────────────────────────────────────────────────────

# The ceiling both importers used to apply, kept only so the repair pass can
# recognise its victims. Nothing writes it any more.
OLD_CAP = 5000

# ── Rate limits, and the repair pass ─────────────────────────────────────────
# Gmail bills a "Total Query Cost" per minute per user, and a large sweep walks
# straight into it. A scan that only printed a warning for each refused fetch
# would lose message bodies — the same shape of defect NC-142 itself was: text
# that did not arrive, with nothing holding on to the fact that it did not
# arrive. A 403 rateLimitExceeded is not a failure, it is the server asking for
# a pause.
_RETRY_MAX = 6
_RETRY_BASE = 2.0


def _is_rate_limited(err: Exception) -> bool:
    text = str(err)
    return ("rateLimitExceeded" in text or "Quota exceeded" in text
            or "userRateLimitExceeded" in text or "429" in text)


def _with_retry(call, what: str = ""):
    """Run a Gmail API call, waiting out the per-minute quota rather than
    dropping the message. Anything that is not a rate limit is re-raised
    unchanged — a retry loop that swallows a real error is worse than no loop."""
    for attempt in range(_RETRY_MAX):
        try:
            return call()
        except Exception as e:  # noqa: BLE001 — googleapiclient's HttpError
            if not _is_rate_limited(e) or attempt == _RETRY_MAX - 1:
                raise
            # Full jitter: a hundred messages that all back off in lockstep
            # arrive together and are all refused together.
            delay = random.uniform(0, _RETRY_BASE * (2 ** attempt))
            if what:
                print(f"  rate limited on {what}; waiting {delay:.1f}s", flush=True)
            time.sleep(delay)
    return None


def truncated_rows(arc_conn):
    """The stored messages an older import probably cut, newest last.

    Separate from repair_truncated so a test can exercise THIS, rather than a
    copy of it: a test that re-implements the query stays green when the window
    is widened or the gmail-only condition is dropped.

    Two conditions, both earning their place. `source_key LIKE 'gmail_%'` keeps
    the pass off rows the .mbox importer wrote, which carry no Gmail id and
    could never be re-fetched by one. The upper bound is OLD_CAP + 201, not
    OLD_CAP: an email body is stored as subject + blank line + body, so a cut
    one lands ABOVE the cap by the length of its subject, and a window that
    stopped at 5,000 would find almost none of them.
    """
    return arc_conn.execute(
        "SELECT id, source_key, contact_id, LENGTH(body) AS n FROM messages "
        "WHERE channel = 'email' AND source_key LIKE 'gmail@_%' ESCAPE '@' "
        f"  AND LENGTH(body) BETWEEN {OLD_CAP - 2} AND {OLD_CAP + 201} "
        "ORDER BY id").fetchall()


def repair_truncated(accounts: Dict[str, str],
                     archive_path: str = arc.ARCHIVE_DB_PATH,
                     dry_run: bool = False) -> dict:
    """Re-fetch stored messages an older import cut short (NC-142).

    The ordinary import can only ASK Gmail for mail matching a known address or
    a known display name, so it never reaches a message that matches neither —
    for example one whose sender uses an address the CRM does not have.

    This pass needs no matching at all, because the archive already stores each
    message's Gmail id as its source_key. It asks for exactly those ids, and
    hands the result to archive.insert_message, whose longer-wins rule does the
    repair. A message that comes back the same length writes nothing.
    """
    arc_conn = arc.get_archive_db(archive_path)
    rows = truncated_rows(arc_conn)
    print(f"{len(rows)} stored messages look cut at the old {OLD_CAP:,}-character limit")
    if dry_run or not rows:
        return {"looked_at": len(rows), "repaired": 0, "grew_by": 0, "missing": 0}

    services = {}
    for acct, creds in accounts.items():
        try:
            services[acct] = get_service(acct, creds)
        except Exception as e:  # noqa: BLE001
            print(f"  {acct}: could not connect ({e})")

    repaired = grew = missing = failed = 0
    for i, row in enumerate(rows, 1):
        msg_id = row["source_key"][len("gmail_"):].split("_")[0]
        payload = None
        for acct, service in services.items():
            try:
                full = _with_retry(
                    lambda: service.users().messages().get(
                        userId="me", id=msg_id, format="full").execute(),
                    what=f"message {i}/{len(rows)}")
            except Exception:  # noqa: BLE001 — wrong mailbox, or gone
                continue
            if full:
                payload = full.get("payload", {})
                break
        if payload is None:
            missing += 1
            continue
        subject = _decode_str(_header(payload, "Subject"))
        body = _extract_body(payload)
        body = (subject + "\n\n" + body).strip() if subject else body
        if len(body) > row["n"]:
            arc_conn.execute("UPDATE messages SET body = ? WHERE id = ?",
                             (body, row["id"]))
            repaired += 1
            grew += len(body) - row["n"]
        if i % 50 == 0:
            arc_conn.commit()
            print(f"  {i}/{len(rows)} — {repaired} repaired, {missing} not in these mailboxes",
                  flush=True)
    arc_conn.commit()
    print(f"\n  {repaired} repaired, {grew:,} characters recovered, "
          f"{missing} not found in these mailboxes, {failed} failed")
    return {"looked_at": len(rows), "repaired": repaired, "grew_by": grew,
            "missing": missing}


def _list_message_ids(service, query: str) -> List[str]:
    """Return all Gmail message IDs matching a search query."""
    ids = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _parse_date(date_str: str) -> str:
    """Parse RFC 2822 email date → 'YYYY-MM-DD HH:MM:SS' or ''."""
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
    ):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            pass
    # Try email.utils parser as fallback
    try:
        import email.utils
        ts = email.utils.parsedate_to_datetime(date_str)
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def import_account(
    account_email: str,
    credentials_file: str,
    email_to_cid: Dict[str, int],
    *,
    dry_run: bool = False,
    since: Optional[str] = None,
    archive_path: str = arc.ARCHIVE_DB_PATH,
) -> dict:
    """Fetch and archive Gmail messages for one account.

    Returns {"new": int, "skipped": int, "unmatched": int, "contacts": int}.
    """
    print(f"\n── {account_email} ──")
    service = get_service(account_email, credentials_file)
    self_email = account_email.lower()

    if not email_to_cid:
        print("  No contact email addresses in crm.db yet — nothing to search.")
        return {"new": 0, "skipped": 0, "unmatched": 0, "contacts": 0}

    if not dry_run:
        arc.init_archive(archive_path)
        arc_conn = arc.get_archive_db(archive_path)

    new = skipped = unmatched = 0
    touched_cids: set = set()
    per_contact_stats: Dict[int, Dict] = {}

    # Search per known contact email — targeted, avoids downloading entire inbox
    since_query = f" after:{since.replace('-', '/')}" if since else ""
    processed_msg_ids: set = set()  # deduplicate across contact searches

    for contact_email, cid in email_to_cid.items():
        query = f"(from:{contact_email} OR to:{contact_email}){since_query}"
        msg_ids = _list_message_ids(service, query)
        if not msg_ids:
            continue

        for msg_id in msg_ids:
            if msg_id in processed_msg_ids:
                continue
            processed_msg_ids.add(msg_id)

            try:
                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()
            except Exception as e:
                print(f"  Warning: could not fetch {msg_id}: {e}")
                continue

            payload = msg.get("payload", {})
            from_addrs = _extract_addresses(_header(payload, "From"))
            to_addrs = _extract_addresses(_header(payload, "To"))
            cc_addrs = _extract_addresses(_header(payload, "Cc"))
            all_to = to_addrs + cc_addrs

            date_str = _header(payload, "Date")
            ts = _parse_date(date_str)
            subject = _decode_str(_header(payload, "Subject"))
            body = _extract_body(payload)

            if not body and not subject:
                unmatched += 1
                continue

            # Determine direction and which contact this belongs to
            from_me = self_email in from_addrs
            if from_me:
                direction = 1
                # Recipient side — find the matching contact
                matched_cid = next(
                    (email_to_cid[a] for a in all_to if a in email_to_cid), None
                )
            else:
                direction = 0
                # Sender side — find the matching contact
                matched_cid = next(
                    (email_to_cid[a] for a in from_addrs if a in email_to_cid), None
                )

            if matched_cid is None:
                unmatched += 1
                continue

            source_key = f"gmail_{msg_id}"
            if dry_run:
                new += 1
                touched_cids.add(matched_cid)
                continue

            inserted = arc.insert_message(
                arc_conn,
                contact_id=matched_cid,
                channel="email",
                direction=direction,
                ts=ts,
                body=(subject + "\n\n" + body).strip() if subject else body,
                source_key=source_key,
            )
            if inserted:
                new += 1
                touched_cids.add(matched_cid)
            else:
                skipped += 1

    if not dry_run:
        arc_conn.commit()
        if touched_cids:
            # Recompute em_count + em_last per touched contact
            crm_conn = db.get_db()
            for cid in touched_cids:
                row = arc_conn.execute(
                    "SELECT COUNT(*) AS cnt, MAX(ts) AS last FROM messages "
                    "WHERE contact_id = ? AND channel = 'email'",
                    (cid,),
                ).fetchone()
                if row and row["cnt"]:
                    db.set_channel(crm_conn, cid, "email", row["cnt"], row["last"] or "")
            crm_conn.commit()
            crm_conn.close()

            print(f"  Building chunks for {len(touched_cids)} contacts…")
            n_chunks = sum(arc.build_chunks(arc_conn, contact_id=cid) for cid in touched_cids)
            print(f"  {n_chunks:,} conversation chunks updated")
        arc_conn.close()

    return {
        "new": new,
        "skipped": skipped,
        "unmatched": unmatched,
        "contacts": len(touched_cids),
    }


# ── Full scan (match by display name) ────────────────────────────────────────

_NAME_RE = re.compile(r'^"?([^"<]+?)"?\s*(?:<[^>]+>)?$')

# Patterns that disqualify a string from being a real person's name
_JUNK_RE = re.compile(
    r"(team|support|no.?reply|noreply|info|hello|hi\b|news|newsletter|notification"
    r"|updates?|alert|admin|service|billing|account|do.not.reply"
    r"|\bab\b|\binc\b|\bllc\b|\bltd\b|\bgmbh\b|@|\.[a-z]{2,4}$)",
    re.I,
)


def _looks_like_person(name: str) -> bool:
    """Return True if `name` looks like a real human (First Last) not a service/bot."""
    parts = name.strip().split()
    if len(parts) < 2:
        return False
    # Every word must be purely alphabetic (hyphens/apostrophes allowed in names)
    for p in parts:
        cleaned = p.replace("-", "").replace("'", "")
        if not cleaned.isalpha():
            return False
    if _JUNK_RE.search(name):
        return False
    return True


def _display_name(header_value: str) -> str:
    """Extract display name from 'John Smith <john@example.com>' or bare address."""
    v = _decode_str((header_value or "").strip())
    m = _NAME_RE.match(v)
    return m.group(1).strip() if m else ""


def _batch_get_metadata(service, msg_ids: List[str]) -> List[dict]:
    """Fetch metadata for up to 100 message IDs in one HTTP batch call."""
    results: List[dict] = []

    def _cb(request_id, response, exception):
        if exception is None and response:
            results.append(response)

    batch = service.new_batch_http_request(callback=_cb)
    for mid in msg_ids:
        batch.add(
            service.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "To", "Cc", "Date", "Subject"],
            )
        )
    batch.execute()
    return results


def scan_all_messages(
    account_email: str,
    credentials_file: str,
    norm_to_cid: Dict[str, int],
    email_to_cid: Dict[str, int],
    *,
    since: str,
    dry_run: bool = False,
    create_new: bool = False,
    archive_path: str = arc.ARCHIVE_DB_PATH,
) -> dict:
    """Sweep ALL messages since `since` and match by display name or email address.

    Falls back to email-address matching for headers that have no display name.
    When create_new=True, any unmatched sender whose name looks like a real person
    and appears in ≥3 messages gets a new contact created and their messages archived.
    Returns {"new": int, "skipped": int, "matched": int, "scanned": int}.
    """
    print(f"\n── {account_email} (full scan since {since}) ──")
    service = get_service(account_email, credentials_file)
    self_email = account_email.lower()
    self_norm = db.normalize_name(account_email.split("@")[0])

    # Convert YYYY-MM-DD → Gmail's after:YYYY/MM/DD format
    after_q = "after:" + since.replace("-", "/")
    print(f"  Listing all message IDs…")
    all_ids = _list_message_ids(service, after_q)
    print(f"  {len(all_ids):,} messages to scan")

    if not all_ids:
        return {"new": 0, "skipped": 0, "matched": 0, "scanned": 0}

    if not dry_run:
        arc.init_archive(archive_path)
        arc_conn = arc.get_archive_db(archive_path)

    new = skipped = matched_count = 0
    touched_cids: set = set()
    # {norm: {"name": str, "msg_ids": [...], "dates": [...]}} for unmatched inbound senders
    candidates: Dict[str, Dict] = {}
    BATCH = 100

    for i in range(0, len(all_ids), BATCH):
        chunk = all_ids[i: i + BATCH]
        meta_list = _batch_get_metadata(service, chunk)

        for meta in meta_list:
            payload = meta.get("payload", {})
            msg_id = meta.get("id", "")

            from_hdr = _header(payload, "From")
            to_hdr = _header(payload, "To")
            cc_hdr = _header(payload, "Cc")
            date_hdr = _header(payload, "Date")
            subj_hdr = _header(payload, "Subject")

            from_name = _display_name(from_hdr)
            from_norm = db.normalize_name(from_name) if from_name else ""
            from_addrs = _extract_addresses(from_hdr)

            # Skip messages I sent to myself
            from_me = self_email in from_addrs or from_norm == self_norm

            # Collect all participant names + addresses on the other side
            other_headers = (to_hdr + "," + cc_hdr) if from_me else from_hdr
            other_names = [_display_name(p.strip()) for p in other_headers.split(",")]
            other_addrs = _extract_addresses(other_headers)

            # Try to find a matching contact
            cid: Optional[int] = None

            if from_me:
                direction = 1
                for name in other_names:
                    norm = db.normalize_name(name)
                    if norm and norm in norm_to_cid:
                        cid = norm_to_cid[norm]
                        break
                if cid is None:
                    cid = next((email_to_cid[a] for a in other_addrs if a in email_to_cid), None)
            else:
                direction = 0
                if from_norm and from_norm in norm_to_cid:
                    cid = norm_to_cid[from_norm]
                if cid is None:
                    cid = next((email_to_cid[a] for a in from_addrs if a in email_to_cid), None)

            if cid is None:
                # Track unmatched inbound senders for possible --create-new
                if create_new and not from_me and from_name and from_norm:
                    if from_norm != self_norm and from_norm not in norm_to_cid:
                        entry = candidates.setdefault(from_norm, {
                            "name": from_name,
                            "msg_ids": [],
                            "dates": [],
                        })
                        entry["msg_ids"].append(msg_id)
                        entry["dates"].append(date_hdr)
                continue

            matched_count += 1
            source_key = f"gmail_{msg_id}"

            if dry_run:
                new += 1
                touched_cids.add(cid)
                continue

            # Fetch full body only for matched messages
            try:
                full = _with_retry(lambda: service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute())
            except Exception as e:
                print(f"  Warning: could not fetch body for {msg_id}: {e}")
                continue

            body = _extract_body(full.get("payload", {}))
            subject = _decode_str(subj_hdr)
            ts = _parse_date(date_hdr)
            combined = (subject + "\n\n" + body).strip() if subject else body

            if not combined:
                continue

            inserted = arc.insert_message(
                arc_conn,
                contact_id=cid,
                channel="email",
                direction=direction,
                ts=ts,
                body=combined,
                source_key=source_key,
            )
            if inserted:
                new += 1
                touched_cids.add(cid)
            else:
                skipped += 1

        done = min(i + BATCH, len(all_ids))
        print(f"  scanned {done:,}/{len(all_ids):,}  matched {matched_count}  new {new}", end="\r")

    print()  # newline after progress line

    # ── Create new contacts from recurring unmatched senders ─────────────────
    new_created = 0
    if create_new and not dry_run and candidates:
        eligible = [
            (norm, data) for norm, data in candidates.items()
            if len(data["msg_ids"]) >= 3 and _looks_like_person(data["name"])
        ]
        if eligible:
            print(f"  Creating {len(eligible)} new contacts from recurring unmatched senders…")
            crm_conn = db.get_db()
            for norm, data in eligible:
                name = data["name"]
                # Skip if already added in a previous account's scan this run
                if norm in norm_to_cid:
                    continue
                cid = db.create_contact(crm_conn, name, source="email")
                norm_to_cid[norm] = cid
                new_created += 1
                # NC-082. Commit the new contact before fetching its messages.
                #
                # The loop below calls the Gmail API once per message and the only
                # commit used to be after the whole eligible-sender loop, so
                # crm_conn held crm.db's single writer lock across every one of
                # those round trips — a subprocess blocking the live app well past
                # its 20-second WRITE_LOCK_TIMEOUT_SEC. Committing here is also
                # strictly better on its own terms: the contact survives a failure
                # part way through fetching its history, and the archive writes
                # below go to arc_conn, a different database with its own lock.
                crm_conn.commit()

                for msg_id, date_hdr in zip(data["msg_ids"], data["dates"]):
                    try:
                        full = _with_retry(lambda: service.users().messages().get(
                            userId="me", id=msg_id, format="full"
                        ).execute())
                    except Exception:
                        continue
                    full_payload = full.get("payload", {})
                    body = _extract_body(full_payload)
                    subj_hdr = _header(full_payload, "Subject")
                    subject = _decode_str(subj_hdr)
                    ts = _parse_date(date_hdr)
                    combined = (subject + "\n\n" + body).strip() if subject else body
                    if not combined:
                        continue
                    inserted = arc.insert_message(
                        arc_conn,
                        contact_id=cid,
                        channel="email",
                        direction=0,
                        ts=ts,
                        body=combined,
                        source_key=f"gmail_{msg_id}",
                    )
                    if inserted:
                        new += 1
                        touched_cids.add(cid)
                    else:
                        skipped += 1
            crm_conn.commit()
            crm_conn.close()
            print(f"  {new_created} new contacts created")

    if not dry_run:
        arc_conn.commit()
        if touched_cids:
            crm_conn = db.get_db()
            for cid in touched_cids:
                row = arc_conn.execute(
                    "SELECT COUNT(*) AS cnt, MAX(ts) AS last FROM messages "
                    "WHERE contact_id = ? AND channel = 'email'",
                    (cid,),
                ).fetchone()
                if row and row["cnt"]:
                    db.set_channel(crm_conn, cid, "email", row["cnt"], row["last"] or "")
            crm_conn.commit()
            crm_conn.close()

            print(f"  Building chunks for {len(touched_cids)} contacts…")
            n_chunks = sum(arc.build_chunks(arc_conn, contact_id=cid) for cid in touched_cids)
            print(f"  {n_chunks:,} conversation chunks updated")
        arc_conn.close()

    return {
        "new": new,
        "skipped": skipped,
        "matched": matched_count,
        "scanned": len(all_ids),
        "contacts": len(touched_cids),
        "created": new_created,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _default_since_3y() -> str:
    from datetime import date
    return (date.today().replace(year=date.today().year - 3)).isoformat()


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    do_scan = "--scan" in args
    create_new = "--create-new" in args
    do_repair = "--repair" in args
    do_connect = "--connect" in args
    # --no-browser is read by browser_allowed() straight off sys.argv, not from
    # here, so that every get_service() in this file is covered whether or not
    # it went through main(). It is stripped here only so it cannot be mistaken
    # for a positional argument.
    args = [a for a in args
            if a not in ("--dry-run", "--scan", "--create-new", "--no-browser",
                         "--connect")]

    since: Optional[str] = None
    for i, a in enumerate(args):
        if a == "--since" and i + 1 < len(args):
            since = args[i + 1]
            args = args[:i] + args[i + 2:]
            break
        if a.startswith("--since="):
            since = a.split("=", 1)[1]
            args = [x for x in args if not x.startswith("--since=")]
            break

    if do_scan and since is None:
        since = _default_since_3y()

    # Remaining positional args are account emails; default to every configured one
    configured = accounts_from_env()
    if not configured:
        raise no_accounts_error()
    requested = [a.lower() for a in args if "@" in a]
    if requested:
        accounts = {e: c for e, c in configured.items() if e in requested}
        unknown = [e for e in requested if e not in configured]
        if unknown:
            raise SystemExit(
                f"Unknown account(s): {', '.join(unknown)}\n"
                f"Configured: {', '.join(configured) or '(none)'}\n"
                "Add them to GMAIL_ACCOUNTS in Settings."
            )
    else:
        accounts = configured

    # NC-117. Sign in and stop — no database, no contacts, no mail. This is
    # what the Data page's Connect button runs, and separating it from the
    # import is what makes the waiting state honest: the only thing happening
    # while it waits is a person looking at a Google tab, so the moment they
    # click Allow it is over, instead of being over some minutes later when a
    # three-year sweep of their mailbox finishes.
    #
    # It also makes the failure legible. A token that cannot be obtained and an
    # import that found nothing used to arrive at the same place; now the first
    # is the only thing this mode can report.
    if do_connect:
        for account_email, creds_file in accounts.items():
            print(f"\nSigning in to {account_email}…", flush=True)
            get_service(account_email, creds_file)
            print(f"  {account_email} is connected.", flush=True)
        print(f"\nDone. {len(accounts)} account(s) connected.")
        return

    # NC-142's repair pass needs no CRM lookups at all — it works from the
    # Gmail ids already stored in the archive — so it runs before any of the
    # matching machinery is built, and returns.
    if do_repair:
        out = repair_truncated(accounts, dry_run=dry_run)
        print(f"\nDone. {out['repaired']} message(s) repaired, "
              f"{out['grew_by']:,} characters recovered.")
        return

    db.init_db()
    crm_conn = db.get_db()

    # Email-address map (used by both modes)
    id_rows = crm_conn.execute(
        "SELECT i.value, i.contact_id FROM identifiers i "
        "JOIN contacts c ON c.id = i.contact_id "
        f"WHERE i.kind = 'email' AND {figures.population_where('visible', 'c')}"
    ).fetchall()
    email_to_cid: Dict[str, int] = {r["value"].lower(): r["contact_id"] for r in id_rows}

    # Name-norm map (used by --scan only)
    norm_to_cid: Dict[str, int] = {}
    if do_scan:
        name_rows = crm_conn.execute(
            f"SELECT id, name_norm FROM contacts WHERE {figures.population_where('visible')}"
        ).fetchall()
        norm_to_cid = {r["name_norm"]: r["id"] for r in name_rows}

    crm_conn.close()

    if do_scan:
        print(f"{len(norm_to_cid):,} contacts available for name matching")
        print(f"+ {len(email_to_cid)} known email addresses as fallback")
    else:
        print(f"{len(email_to_cid)} contact email addresses loaded from crm.db")
        if since:
            print(f"Filtering to messages after {since}")

    total_new = total_skipped = 0
    for account_email, creds_file in accounts.items():
        if do_scan:
            stats = scan_all_messages(
                account_email,
                creds_file,
                norm_to_cid,
                email_to_cid,
                since=since,
                dry_run=dry_run,
                create_new=create_new,
            )
            tag = "[dry-run] " if dry_run else ""
            created_str = f"  {stats.get('created', 0)} new contacts" if create_new else ""
            print(
                f"  {tag}{stats['new']:,} new  {stats['skipped']:,} already stored  "
                f"{stats['contacts']} contacts  (scanned {stats['scanned']:,} messages)"
                + created_str
            )
        else:
            stats = import_account(
                account_email,
                creds_file,
                email_to_cid,
                dry_run=dry_run,
                since=since,
            )
            tag = "[dry-run] " if dry_run else ""
            print(
                f"  {tag}{stats['new']:,} new messages  "
                f"{stats['skipped']:,} already stored  "
                f"{stats['unmatched']:,} unmatched  "
                f"{stats['contacts']} contacts touched"
            )
        total_new += stats["new"]
        total_skipped += stats["skipped"]

    print(f"\nDone. {total_new:,} new messages archived across {len(accounts)} account(s).")
    if not do_scan and total_new == 0 and total_skipped == 0:
        print("Tip: run with --scan to sweep all messages and match by contact name.")


if __name__ == "__main__":
    try:
        main()
    except NeedsSignIn as exc:
        # NC-177. Not a crash and not an import failure — a thing for a person to
        # do, and the exit code says which so the background loop can put it on
        # the Data page instead of discarding it like any other non-zero.
        # NC-181. The address on its own line, ahead of the prose, so the
        # scheduler can name it on the Data page without parsing a sentence
        # written for a person to read.
        print(f"{SIGN_IN_MARKER} {exc.account_email}")
        print(str(exc))
        print("Open the Data page, find Gmail, and run the import there — that "
              "is a run you started, so it may open the Google tab.")
        sys.exit(EXIT_NEEDS_SIGN_IN)
