"""
Import an email archive (.mbox) into crm.db — runs entirely on your Mac.

You export a mailbox from your mail client (Apple Mail: Mailbox → Export…, Gmail:
Takeout) and point this script at the resulting .mbox file. Nothing needs special
macOS permission — it's just a file you chose to export.

Like the iMessage and WhatsApp imports, this follows the "only people you actually
know, two-way only, no noise" philosophy:

  • It enriches EXISTING contacts (your LinkedIn connections etc.) with email
    correspondence stats — it does not create hundreds of contacts from every
    newsletter sender. An address matches a contact when it's already an `email`
    identifier on that contact, or when the sender's display name matches a
    contact's name. Pass --create-new to also create a contact from a real sender
    name.
  • Automated/bulk senders are filtered out: no-reply / notifications / mailer-
    daemon / newsletter / support style local-parts, plus any message carrying a
    List-Unsubscribe or List-Id header (the mark of bulk mail).
  • People in exclude_list.txt (anyone you chose never to index) are skipped.
  • Two-way only: when SELF_EMAIL is set, an address counts only if there's at
    least one message FROM them and at least one FROM you to them. Cold one-sided
    blasts in either direction don't count. (If SELF_EMAIL is unset we can't tell
    direction, so we fall back to counting any correspondence with matched
    contacts and print a warning.) SELF_EMAIL may list several of your own
    addresses, separated by commas: mail from or to any of them is yours.

It reads only header metadata (From / To / Cc / Date and a couple of list
headers) — never message bodies — so nothing private is summarized or sent
anywhere.

Usage:
    python email_import.py archive.mbox                    # apply
    python email_import.py archive.mbox --dry-run           # preview, write nothing
    python email_import.py archive.mbox --create-new        # also create from real sender names
    python email_import.py archive.mbox --since YYYY-MM-DD  # ignore emails before this date
    python email_import.py archive.mbox --years 3           # ignore emails older than 3 years
    python email_import.py archive.mbox --summarize         # AI summary per contact (opt-in)
"""

import os
import re
import sys
import mailbox
from datetime import date, timedelta, timezone
from email.utils import parseaddr, getaddresses, parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

import email_text
import config  # noqa: F401 — loads .env
import db
import archive as arc

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5-20251001")
# Your own address, or several: SELF_EMAIL may list them separated by commas
# (NC-328). Kept as the raw text so a test or a caller can still set it as one
# string; self_addresses() is what the code below reads.
SELF_EMAIL = os.environ.get("SELF_EMAIL", "").strip().lower()
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE", config.data_path("exclude_list.txt"))


def self_addresses() -> frozenset:
    """Every address that is yours, lowercase; empty when none is set.

    NC-328. SELF_EMAIL used to be a single address, so mail written from your
    other address read as from a stranger, and (with the setting on) mail sent to
    your other address was dropped as not addressed to you. It may now hold
    several, separated by commas, semicolons or spaces. Read at call time from
    the module value, so a changed setting or a test that rebinds it still works.
    """
    return frozenset(a for a in re.split(r"[,;\s]+", (SELF_EMAIL or "").lower()) if a)

# Automated / bulk senders. Matched against an address's local part and its
# domain (case-insensitive, substring). These are the addresses you never have a
# real two-way relationship with.
AUTOMATED_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"no-?reply",
        r"do-?not-?reply",
        r"donotreply",
        r"noreply",
        r"notifications?",
        r"mailer-daemon",
        r"postmaster",
        r"bounce",
        r"newsletter",
        r"support",
    )
]

# Headers that mark bulk / list mail — any message carrying one is skipped.
BULK_HEADERS = ("List-Unsubscribe", "List-Id")


# ── Header parsing ────────────────────────────────────────────────────────────
def parse_date_iso(raw: Optional[str]) -> str:
    """Header Date → 'YYYY-MM-DD HH:MM:SS' in UTC. '' if missing/unparseable."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_automated(addr: str) -> bool:
    """True if the address's local part or domain looks like an automated sender."""
    addr = (addr or "").lower()
    if "@" not in addr:
        return any(p.search(addr) for p in AUTOMATED_PATTERNS)
    local, _, domain = addr.partition("@")
    return any(p.search(local) or p.search(domain) for p in AUTOMATED_PATTERNS)


def is_bulk(msg) -> bool:
    """True if the message carries a bulk-mail header (List-Unsubscribe/List-Id)."""
    return any(msg.get(h) is not None for h in BULK_HEADERS)


def extract_addresses(values: List[Optional[str]]) -> List[str]:
    """Flatten one or more To/Cc header values into clean lowercase addresses."""
    pairs = getaddresses([v for v in values if v])
    out = []
    for _name, addr in pairs:
        addr = (addr or "").strip().lower()
        if addr and "@" in addr:
            out.append(addr)
    return out


def looks_like_real_name(name: str, addr: str) -> bool:
    """A From display name is a real name (not just the address echoed back) and
    contains at least one alphabetic character."""
    name = (name or "").strip()
    if not name:
        return False
    if "@" in name:  # display name is itself an email -> not a real name
        return False
    if name.lower() == (addr or "").strip().lower():
        return False
    return any(c.isalpha() for c in name)


# ── mbox aggregation ──────────────────────────────────────────────────────────
class AddressAgg:
    """Per-external-address running tally collected from the mbox."""

    __slots__ = ("inbound", "outbound", "first", "last", "name", "subjects")

    def __init__(self) -> None:
        self.inbound = 0          # messages FROM this address (to you)
        self.outbound = 0         # messages FROM you that include this address
        self.first = ""
        self.last = ""
        self.name = ""            # best display name seen for this address
        self.subjects: List[str] = []  # up to 20 subject lines (inbound)

    def touch(self, date: str) -> None:
        if date:
            if not self.first or date < self.first:
                self.first = date
            if date > self.last:
                self.last = date


def aggregate_mbox(path: str, since: Optional[str] = None) -> Dict[str, AddressAgg]:
    """Scan the mbox once and return {address: AddressAgg} for every external
    address that appears in a non-bulk message with a usable From address.

    Direction (inbound/outbound) is determined by SELF_EMAIL (one address or
    several of yours): a message whose From is you is outbound to each external
    To/Cc address; a message From an external address is inbound from that
    address.

    If ``since`` is given ('YYYY-MM-DD'), messages older than that date are skipped."""
    box = mailbox.mbox(path)
    agg: Dict[str, AddressAgg] = {}
    selves = self_addresses()

    def slot(address: str) -> AddressAgg:
        a = agg.get(address)
        if a is None:
            a = agg[address] = AddressAgg()
        return a

    for msg in box:
        try:
            if is_bulk(msg):
                continue
            from_name, from_addr = parseaddr(msg.get("From", "") or "")
            from_addr = (from_addr or "").strip().lower()
            if not from_addr or "@" not in from_addr:
                continue
            date = parse_date_iso(msg.get("Date"))
            recipients = extract_addresses([msg.get("To"), msg.get("Cc")])
        except (TypeError, ValueError, AttributeError):
            # malformed/missing headers on this message — skip it cleanly
            continue

        if since and date and date[:10] < since:
            continue  # older than cutoff — skip

        from_is_self = from_addr in selves

        if from_is_self:
            # outbound: you wrote to each external recipient
            for rcpt in recipients:
                if rcpt in selves or is_automated(rcpt):
                    continue
                a = slot(rcpt)
                a.outbound += 1
                a.touch(date)
        else:
            # inbound: this external person wrote
            if is_automated(from_addr):
                continue
            # When we know who we are, only count mail actually addressed to us
            # (To/Cc) so a forwarded list message doesn't read as a 1:1 reply.
            if selves and selves.isdisjoint(recipients):
                continue
            a = slot(from_addr)
            a.inbound += 1
            a.touch(date)
            if not a.name and looks_like_real_name(from_name, from_addr):
                a.name = from_name.strip()
            subject = (msg.get("Subject") or "").strip()
            if subject and len(a.subjects) < 20:
                a.subjects.append(subject)

    box.close()
    return agg


def qualifying_addresses(agg: Dict[str, AddressAgg]) -> Dict[str, AddressAgg]:
    """Keep only addresses we should count.

    With SELF_EMAIL set this is strict two-way: ≥1 inbound AND ≥1 outbound.
    Without SELF_EMAIL we can't tell direction, so keep any address that appears
    at all (inbound tallies hold every From we saw)."""
    if self_addresses():
        return {a: v for a, v in agg.items() if v.inbound > 0 and v.outbound > 0}
    return {a: v for a, v in agg.items() if (v.inbound + v.outbound) > 0}


# ── Matching to existing contacts ─────────────────────────────────────────────
def build_email_index(conn) -> Dict[str, int]:
    """{lowercased email identifier: contact_id} from the identifiers table."""
    idx: Dict[str, int] = {}
    for r in conn.execute("SELECT contact_id, value FROM identifiers WHERE kind = 'email'"):
        value = (r["value"] or "").strip().lower()
        if value:
            idx.setdefault(value, r["contact_id"])
    return idx


def match_contact(conn, email_idx: Dict[str, int], address: str,
                  display_name: str) -> Optional[int]:
    """Resolve an address to an EXISTING contact id, or None.

    (a) the address equals an existing email identifier (case-insensitive), or
    (b) the sender's display name normalizes to an existing contact's name_norm.
    """
    cid = email_idx.get(address.strip().lower())
    if cid is not None:
        return cid
    if looks_like_real_name(display_name, address):
        existing = db.find_exact(conn, display_name)
        if existing:
            return existing[0]["id"]
    return None


def summarize_email(client, name: str, subjects: List[str]) -> str:
    lines = "\n".join(f"- {s}" for s in subjects[:20])
    msg = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=120,
        messages=[{
            "role": "user",
            "content": (
                f"These are email subject lines from my correspondence with {name}. "
                "In at most 2 sentences, summarize what topics we've discussed and our "
                "relationship, factually and concisely. No preamble.\n\n" + lines
            ),
        }],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


# ── Apply to crm.db ───────────────────────────────────────────────────────────
def apply(agg: Dict[str, AddressAgg], *, create_new: bool, dry_run: bool,
          client=None) -> Tuple[dict, Dict[str, int]]:
    conn = db.get_db()
    db.ensure_columns(conn)
    exclude_norms = db.load_exclude_list(EXCLUDE_FILE)
    email_idx = build_email_index(conn)
    stats = {"matched": 0, "created": 0, "automated": 0, "excluded": 0, "unmatched": 0}

    qualifying = qualifying_addresses(agg)

    # Resolve every qualifying address to a contact id first, then aggregate per
    # contact BEFORE writing so a person with several addresses gets one bump.
    per_contact: Dict[int, Dict[str, object]] = {}

    for address, a in qualifying.items():
        if is_automated(address):           # belt-and-suspenders; mostly pre-filtered
            stats["automated"] += 1
            continue

        cid = match_contact(conn, email_idx, address, a.name)
        if cid is None and create_new and looks_like_real_name(a.name, address):
            if db.normalize_name(a.name) in exclude_norms:
                stats["excluded"] += 1
                continue
            cid = None if dry_run else db.create_contact(conn, a.name, source="email")
            stats["created"] += 1
            if cid is not None:
                email_idx[address] = cid
        elif cid is not None:
            row = db.get_contact(conn, cid)
            if row and db.normalize_name(row["name"]) in exclude_norms:
                stats["excluded"] += 1
                continue
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1
            continue

        count = a.inbound + a.outbound
        bucket = per_contact.setdefault(
            cid, {"count": 0, "last": "", "addresses": [], "subjects": []}
        )
        bucket["count"] += count                                    # type: ignore[operator]
        bucket["last"] = max(bucket["last"], a.last)                 # type: ignore[type-var]
        bucket["addresses"].append(address)                         # type: ignore[union-attr]
        if a.subjects:
            existing_subj = bucket["subjects"]                      # type: ignore[index]
            needed = 20 - len(existing_subj)
            if needed > 0:
                existing_subj.extend(a.subjects[:needed])

    summarized = 0
    if not dry_run:
        for cid, bucket in per_contact.items():
            if cid is None:
                continue
            db.set_channel(conn, cid, "email", bucket["count"], bucket["last"])
            for address in bucket["addresses"]:                     # type: ignore[union-attr]
                db.add_identifier(conn, cid, "email", address, source="message_scan")

        if client:
            # NC-082. Commit the channel and identifier writes above BEFORE the
            # first model call, and commit each summary as it lands.
            #
            # summarize_email() is a live Anthropic request, made once per contact
            # with archived subject lines. Every write in this function used to
            # share the single commit at the end of it, so the loop above took
            # crm.db's writer lock and held it across the entire summarisation
            # pass — hundreds of network round trips against a 20-second
            # WRITE_LOCK_TIMEOUT_SEC. It also meant a failure anywhere in the pass
            # discarded the channel counts the scan had already worked out, which
            # is the opposite of what the try/except around each summary intends.
            db.release_write_lock(conn)
            for cid, bucket in per_contact.items():
                if cid is None or not bucket["subjects"]:           # type: ignore[index]
                    continue
                row = db.get_contact(conn, cid)
                if not row:
                    continue
                try:
                    summary = summarize_email(client, row["name"], bucket["subjects"])  # type: ignore[index]
                    conn.execute("UPDATE contacts SET auto_summary = ? WHERE id = ?",
                                 (summary, cid))
                    conn.commit()
                    summarized += 1
                    if summarized % 20 == 0:
                        print(f"  …{summarized} summaries generated")
                except Exception as exc:
                    print(f"  summary failed for {row['name']}: {exc}")

        conn.commit()
    conn.close()

    stats["contacts_touched"] = len([c for c in per_contact if c is not None])
    stats["summarized"] = summarized
    # Build addr→cid map for the archive step: one address may reach the same cid
    addr_to_cid: Dict[str, int] = {}
    if not dry_run:
        for cid, bucket in per_contact.items():
            if cid is not None:
                for addr in bucket["addresses"]:          # type: ignore[union-attr]
                    addr_to_cid[addr] = cid
    return stats, addr_to_cid


def _part_text(part) -> str:
    """One MIME part, decoded with its own charset and never raising."""
    charset = part.get_content_charset() or "utf-8"
    payload = part.get_payload(decode=True) or b""
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg) -> str:
    """Readable text from an email.Message — the WHOLE message (NC-142).

    Plain text wins; HTML is the fallback rather than nothing, which is what an
    HTML-only mail used to store. No character cap, and no line is dropped for
    what it contains — a quoted line is frequently where someone answered
    inline, and text below a "--" is still text they sent. The reasoning and the
    measurements are in email_text.py; this must stay in step with
    gmail_import._extract_body, which makes the same choices for the API's own
    payload shape.
    """
    html_fallback = ""
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            continue          # an attached file is a file, not the message
        ctype = part.get_content_type()
        if ctype == "text/plain":
            text = email_text.tidy(_part_text(part))
            if text:
                return text
        elif ctype == "text/html" and not html_fallback:
            html_fallback = _part_text(part)
    return email_text.html_to_text(html_fallback)


def archive_messages(mbox_path: str, addr_to_cid: Dict[str, int],
                     archive_path: str = arc.ARCHIVE_DB_PATH,
                     since: Optional[str] = None) -> dict:
    """Read message bodies from the mbox and write to archive.db.

    Only addresses in addr_to_cid (matched/created by apply()) are archived.
    Direction: 1 (sent) if the From address is one of yours (SELF_EMAIL), else
    0 (received).
    Returns {"new": int, "skipped": int}.
    """
    if not addr_to_cid:
        return {"new": 0, "skipped": 0}

    selves = self_addresses()
    arc.init_archive(archive_path)
    arc_conn = arc.get_archive_db(archive_path)

    new = skipped = 0
    mbox = mailbox.mbox(mbox_path)
    try:
        for msg in mbox:
            raw_date = msg.get("Date", "")
            try:
                dt = parsedate_to_datetime(raw_date)
                ts = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts = ""

            if since and ts and ts[:10] < since:
                continue

            from_name, from_addr = parseaddr(msg.get("From", ""))
            from_addr = from_addr.strip().lower()
            from_me = from_addr in selves

            if from_me:
                # Outbound: archive to every matched recipient
                recipients = [a.strip().lower() for _, a in
                              getaddresses([msg.get("To", ""), msg.get("Cc", "")])]
                cids = {addr_to_cid[r] for r in recipients if r in addr_to_cid}
                channel_addr = from_addr  # doesn't matter, cid is from recipients
            else:
                cids = {addr_to_cid[from_addr]} if from_addr in addr_to_cid else set()

            if not cids:
                continue

            body = _extract_body(msg)
            if not body:
                continue

            subject = msg.get("Subject", "") or ""
            msg_id = (msg.get("Message-ID") or "").strip()
            if not msg_id:
                import hashlib as _hl
                msg_id = _hl.md5(f"{from_addr}{ts}{subject}".encode()).hexdigest()

            for cid in cids:
                source_key = f"{msg_id}_{cid}" if len(cids) > 1 else msg_id
                inserted = arc.insert_message(
                    arc_conn,
                    contact_id=cid,
                    channel="email",
                    direction=int(from_me),
                    ts=ts,
                    body=body,
                    source_key=source_key,
                    subject=subject,
                )
                if inserted:
                    new += 1
                else:
                    skipped += 1
    finally:
        mbox.close()

    arc_conn.commit()
    arc_conn.close()
    return {"new": new, "skipped": skipped}


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_since(argv: List[str]) -> Optional[str]:
    """Return a 'YYYY-MM-DD' cutoff from --since YYYY-MM-DD or --years N, or None."""
    for i, arg in enumerate(argv):
        if arg == "--since" and i + 1 < len(argv):
            raw = argv[i + 1]
            try:
                date.fromisoformat(raw)  # validates format
            except ValueError:
                raise SystemExit(f"--since expects YYYY-MM-DD, got: {raw!r}")
            return raw
        if arg == "--years" and i + 1 < len(argv):
            try:
                n = int(argv[i + 1])
                if n < 1:
                    raise ValueError
            except ValueError:
                raise SystemExit(f"--years expects a positive integer, got: {argv[i + 1]!r}")
            cutoff = date.today() - timedelta(days=365 * n)
            return cutoff.strftime("%Y-%m-%d")
    return None


def _parse_args(argv: List[str]) -> Tuple[Optional[str], bool, bool, bool, Optional[str]]:
    path: Optional[str] = None
    create_new = "--create-new" in argv
    dry_run = "--dry-run" in argv
    summarize = "--summarize" in argv
    since = _parse_since(argv)
    for arg in argv[1:]:
        if not arg.startswith("-"):
            path = arg
            break
    return path, create_new, dry_run, summarize, since


def main() -> None:
    path, create_new, dry_run, do_summarize, since = _parse_args(sys.argv)

    if not path:
        raise SystemExit(
            "Usage: python email_import.py <archive.mbox> "
            "[--create-new] [--dry-run] [--since YYYY-MM-DD] [--years N] [--summarize]"
        )
    if not os.path.exists(path):
        raise SystemExit(f"mbox file not found: {path}")

    if not self_addresses():
        print("WARNING: SELF_EMAIL not set — can't determine message direction, so "
              "two-way filtering is OFF.\n    Counting any correspondence with "
              "matched contacts. Set SELF_EMAIL in .env to enable two-way mode.")

    if since:
        print(f"  Cutoff: ignoring emails before {since}")

    client = None
    if do_summarize:
        print("WARNING: --summarize: email subject lines will be sent to the Anthropic API.")
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        except KeyError:
            raise SystemExit("ANTHROPIC_API_KEY not set — needed for --summarize.")

    db.init_db()
    print(f"Reading mbox: {path}")
    agg = aggregate_mbox(path, since=since)
    print(f"  {len(agg)} external addresses seen")

    stats, addr_to_cid = apply(agg, create_new=create_new, dry_run=dry_run, client=client)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"\n{tag}Matched to existing contacts: {stats['matched']}\n"
        f"{tag}New contacts created:        {stats['created']}\n"
        f"{tag}Skipped (automated sender):  {stats['automated']}\n"
        f"{tag}Skipped (exclude_list):      {stats['excluded']}\n"
        f"{tag}Unmatched (ignored):         {stats['unmatched']}\n"
        f"{tag}Contacts updated:            {stats['contacts_touched']}"
    )
    if stats.get("summarized"):
        print(f"{tag}AI summaries generated:      {stats['summarized']}")
    if not dry_run and addr_to_cid:
        print("\nArchiving message content…")
        arc_stats = archive_messages(path, addr_to_cid, since=since)
        print(f"  {arc_stats['new']:,} new messages archived  "
              f"({arc_stats['skipped']:,} already stored)")
        if arc_stats["new"]:
            arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
            n_chunks = sum(arc.build_chunks(arc_conn, contact_id=cid)
                          for cid in set(addr_to_cid.values()))
            arc_conn.close()
            print(f"  {n_chunks:,} conversation chunks ready for search")


if __name__ == "__main__":
    main()
