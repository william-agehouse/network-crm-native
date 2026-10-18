#!/usr/bin/env python3
"""
Facts enrichment for contacts — runs offline or with opt-in LLM.

Pass A  (always, free):
  • Phone area codes  → home_metro / home_city / home_country
  • Email TLD         → home_country (if not already set)
  • LinkedIn title    → role_type

Pass B  (free, needs archive.db):
  • Scans archived messages for "based in X", "living in X", "moved to X"

Pass C  (opt-in, --llm flag, costs money):
  • Sends a small context snippet to Claude → {city, country, metro, topics, role_type, blurb}
  • One contact at a time; skips contacts already tagged 'manual' or 'llm'.
  • Batch size capped at --limit (default 20) to control spend.

Usage:
  python crm/enrich_facts.py              # Pass A + B only
  python crm/enrich_facts.py --llm        # also run LLM pass (up to 20 contacts)
  python crm/enrich_facts.py --llm --limit 5   # only enrich 5 via LLM
  python crm/enrich_facts.py --force      # re-enrich all (not just untagged)
"""

from __future__ import annotations  # allow `str | None` hints on older Pythons (3.8)

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402,F401 — imported for its side effect: loads .env
import db
import figures  # NC-087: the one spelling of a contact-scope filter
import archive as arc


# ── Area code → metro ─────────────────────────────────────────────────────────
AREA_CODE_METRO: dict[int, str] = {
    # SF Bay Area
    415: "San Francisco", 650: "San Francisco", 408: "San Francisco",
    510: "San Francisco", 925: "San Francisco", 707: "San Francisco",
    669: "San Francisco", 628: "San Francisco",
    # New York
    212: "New York", 646: "New York", 917: "New York", 718: "New York",
    929: "New York", 347: "New York", 332: "New York",
    # Los Angeles
    213: "Los Angeles", 310: "Los Angeles", 323: "Los Angeles",
    424: "Los Angeles", 747: "Los Angeles", 818: "Los Angeles",
    # Chicago
    312: "Chicago", 773: "Chicago", 872: "Chicago",
    # Austin
    512: "Austin", 737: "Austin",
    # Seattle
    206: "Seattle", 253: "Seattle", 425: "Seattle", 360: "Seattle",
    # Boston
    617: "Boston", 857: "Boston",
    # Miami
    305: "Miami", 786: "Miami", 954: "Miami",
    # Denver
    303: "Denver", 720: "Denver",
    # Atlanta
    404: "Atlanta", 678: "Atlanta", 770: "Atlanta", 470: "Atlanta",
    # Washington DC
    202: "Washington DC", 703: "Washington DC", 571: "Washington DC",
    # Dallas
    214: "Dallas", 469: "Dallas", 972: "Dallas",
    # Houston
    713: "Houston", 281: "Houston", 832: "Houston",
    # Phoenix
    602: "Phoenix", 623: "Phoenix", 480: "Phoenix",
    # Portland
    503: "Portland", 971: "Portland",
    # Minneapolis
    612: "Minneapolis", 651: "Minneapolis",
    # San Diego
    619: "San Diego", 858: "San Diego",
    # Nashville
    615: "Nashville",
    # Charlotte
    704: "Charlotte", 980: "Charlotte",
    # Philadelphia
    215: "Philadelphia", 267: "Philadelphia",
}

AREA_CODE_COUNTRY: dict[int, str] = {k: "US" for k in AREA_CODE_METRO}

# ── Email TLD → country ────────────────────────────────────────────────────────
TLD_COUNTRY: dict[str, str] = {
    "se": "Sweden", "dk": "Denmark", "no": "Norway", "fi": "Finland",
    "de": "Germany", "fr": "France", "co.uk": "UK", "uk": "UK",
    "nl": "Netherlands", "es": "Spain", "it": "Italy", "ch": "Switzerland",
    "at": "Austria", "be": "Belgium", "pl": "Poland", "cz": "Czech Republic",
    "au": "Australia", "nz": "New Zealand", "ca": "Canada",
    "in": "India", "jp": "Japan", "sg": "Singapore", "hk": "Hong Kong",
    "br": "Brazil", "mx": "Mexico", "ar": "Argentina",
    "za": "South Africa", "ae": "UAE", "il": "Israel",
    "pt": "Portugal", "gr": "Greece", "hu": "Hungary", "ro": "Romania",
}

# ── Role keywords → role_type ─────────────────────────────────────────────────
# First match wins (list is priority order).
ROLE_PATTERNS: list[tuple[str, str]] = [
    ("founder",    r"\b(founder|co.?founder|ceo|chief executive officer)\b"),
    ("investor",   r"\b(venture capital|vc partner|investor|investing|angel investor)\b"),
    ("tech",       r"\b(software engineer|software developer|engineer|developer|architect|cto|chief technology|devops|data scientist|machine learning|ml engineer|backend|frontend|fullstack|sre)\b"),
    ("product",    r"\b(product manager|head of product|vp of product|director of product|pm\b)\b"),
    ("design",     r"\b(designer|ux|ui designer|creative director|design lead|head of design)\b"),
    ("sales",      r"\b(sales|account executive|account manager|business development|bdr|sdr|revenue)\b"),
    ("marketing",  r"\b(marketing|growth|brand|content|cmo|communications|pr\b|public relations)\b"),
    ("finance",    r"\b(cfo|chief financial|finance|financial analyst|investment bank|private equity|hedge fund|portfolio)\b"),
    ("operations", r"\b(operations|coo|chief operating|supply chain|logistics|ops)\b"),
    ("medical",    r"\b(doctor|physician|medical|surgeon|nurse|healthcare|md\b|chief medical)\b"),
    ("legal",      r"\b(lawyer|attorney|legal|counsel|solicitor|partner at .* law)\b"),
    ("research",   r"\b(researcher|scientist|phd|professor|academic|postdoc|research lead)\b"),
    ("recruiter",  r"\b(recruiter|talent acquisition|headhunter|people ops|head of talent)\b"),
    ("consultant", r"\b(consultant|consulting|advisory|advisor|management consultant)\b"),
]

# ── Location scan patterns (in archived messages) ────────────────────────────
LOC_SCAN_PATTERNS = [
    re.compile(r"(?:based|living|located|situated|working)\s+(?:in|out of)\s+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,2})", re.I),
    re.compile(r"(?:I'm|I am|we're|we are)\s+(?:now\s+)?(?:in|based in)\s+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,2})", re.I),
    re.compile(r"(?:moved|moving|relocated|relocating|just moved)\s+to\s+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,2})", re.I),
    re.compile(r"(?:just got to|arrived in|here in|heading to|visiting)\s+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,2})", re.I),
]
# Cities that are too generic / likely false positives
LOC_BLOCKLIST = {
    "the", "a", "an", "my", "our", "your", "this", "that", "here", "there",
    "inc", "ltd", "llc", "the us", "us", "usa", "uk",
    "work", "office", "home", "town", "city", "place", "area",
    "team", "process", "coming", "investment", "corporate", "welcome",
    "reception", "conference", "workshop", "meeting", "summit",
    "moment", "time", "week", "month", "year", "day", "space",
}


def _old_area_code(phone: str) -> int | None:
    """How this pass read an area code until NC-252: from ANY number of ten or
    more digits. Kept only so the corrections below can recognise the cities that
    reading wrote; nothing new is filled in from it."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("1") and len(digits) >= 11:
        digits = digits[1:]
    if len(digits) >= 10:
        return int(digits[:3])
    return None


def _extract_area_code(phone: str) -> int | None:
    """The US or Canadian area code of a number, or None.

    NC-252. A number written with a country code other than +1 is not a US or
    Canadian number, whatever its next three digits are: an Indian mobile
    "+91 71..." read as New York's 917. A number with no "+" stays ambiguous and
    keeps the old reading, because a US number stored as "650 555 0100" is
    common."""
    if phone.strip().startswith("+") and not re.sub(r"\D", "", phone).startswith("1"):
        return None
    return _old_area_code(phone)


CITY_FROM_HIDDEN_ID_KEY = "cities_from_hidden_ids"   # receipt for the NC-250 fix


def forget_cities_from_hidden_ids(conn) -> int:
    """NC-250. Clear the cities this pass read from WhatsApp hidden ids.

    Until NC-243 the WhatsApp import stored a hidden id ("<digits>@lid") as a
    phone number, and this pass read a US area code from its first digits, so
    an id beginning "1415" became San Francisco. NC-243 turned those rows into
    whatsapp_id records, which no pass reads. This undoes the cities they left
    behind.

    A city is cleared only when every sign says it came from the hidden id: this
    pass wrote it (facts_source 'offline'), city and metro are both exactly the
    metro that id's digits read as, the person did not type either one in, and
    none of their real numbers gives the same city. The country goes too when it
    is the one that area code gave. The next offline pass may fill them again
    from a real number or an email. Runs on every start and is idempotent: once
    cleared, nothing matches. Returns how many people were cleared, and records
    that in `state` so the change is visible rather than silent.
    """
    rows = conn.execute(
        "SELECT c.id, c.home_metro, c.home_city, c.home_country, i.value "
        "FROM identifiers i JOIN contacts c ON c.id = i.contact_id "
        "WHERE i.kind = 'whatsapp_id' AND c.home_metro != '' "
        "AND c.facts_source = 'offline'").fetchall()
    cleared = 0
    for r in rows:
        ac = _old_area_code("+" + r["value"].split("@", 1)[0])
        metro = AREA_CODE_METRO.get(ac) if ac else None
        if not metro or r["home_metro"] != metro or r["home_city"] != metro:
            continue
        if {"home_metro", "home_city"} & db.manual_field_set(db.get_contact(conn, r["id"])):
            continue
        real = [p["value"] for p in conn.execute(
            "SELECT value FROM identifiers WHERE contact_id = ? AND kind = 'phone'",
            (r["id"],))]
        if any(AREA_CODE_METRO.get(_extract_area_code(p) or 0) == metro for p in real):
            continue
        country = r["home_country"]
        if country == AREA_CODE_COUNTRY.get(ac, "US"):
            country = ""
        conn.execute("UPDATE contacts SET home_metro = '', home_city = '', home_country = ? "
                     "WHERE id = ?", (country, r["id"]))
        cleared += 1
    if cleared:
        db.state_set(conn, CITY_FROM_HIDDEN_ID_KEY, json.dumps(
            {"cleared": cleared, "at": db.now_iso()}))
    return cleared


CITY_FROM_FOREIGN_NUMBER_KEY = "cities_from_foreign_numbers"   # NC-252 receipt


def forget_cities_from_foreign_numbers(conn) -> int:
    """NC-252. Clear the cities this pass read from a foreign number as if it
    were a US area code. Same rules as forget_cities_from_hidden_ids: this pass
    wrote it, city and metro are exactly what the old reading gave, nobody typed
    it in, and no number the person has gives that city under the new reading.
    Idempotent; records how many it cleared."""
    rows = conn.execute(
        "SELECT DISTINCT c.id, c.home_metro, c.home_city, c.home_country "
        "FROM contacts c JOIN identifiers i ON i.contact_id = c.id "
        "WHERE i.kind = 'phone' AND i.value LIKE '+%' AND i.value NOT LIKE '+1%' "
        "AND c.home_metro != '' AND c.facts_source = 'offline'").fetchall()
    cleared = 0
    for r in rows:
        phones = [p["value"] for p in conn.execute(
            "SELECT value FROM identifiers WHERE contact_id = ? AND kind = 'phone'",
            (r["id"],))]
        misread = [p for p in phones if _extract_area_code(p) is None
                   and AREA_CODE_METRO.get(_old_area_code(p) or 0) == r["home_metro"]]
        if not misread or r["home_city"] != r["home_metro"]:
            continue
        if any(AREA_CODE_METRO.get(_extract_area_code(p) or 0) == r["home_metro"]
               for p in phones):
            continue
        if {"home_metro", "home_city"} & db.manual_field_set(db.get_contact(conn, r["id"])):
            continue
        country = r["home_country"]
        if country == AREA_CODE_COUNTRY.get(_old_area_code(misread[0]), "US"):
            country = ""
        conn.execute("UPDATE contacts SET home_metro = '', home_city = '', home_country = ? "
                     "WHERE id = ?", (country, r["id"]))
        cleared += 1
    if cleared:
        db.state_set(conn, CITY_FROM_FOREIGN_NUMBER_KEY, json.dumps(
            {"cleared": cleared, "at": db.now_iso()}))
    return cleared


def _email_country(email: str) -> str | None:
    parts = email.lower().rsplit(".", 1)
    if len(parts) < 2:
        return None
    tld = parts[-1]
    if tld == "com" or tld == "org" or tld == "net" or tld == "io":
        return None  # too generic
    # check two-level TLDs first (co.uk)
    two = email.lower().rsplit(".", 2)
    if len(two) == 3:
        two_tld = two[-2] + "." + two[-1]
        if two_tld in TLD_COUNTRY:
            return TLD_COUNTRY[two_tld]
    return TLD_COUNTRY.get(tld)


def _match_role_type(text: str) -> str:
    text = (text or "").lower()
    for role, pattern in ROLE_PATTERNS:
        if re.search(pattern, text):
            return role
    return ""


def _scan_messages_for_city(arc_conn, contact_id: int, limit: int = 200) -> str:
    """Scan archived messages for location mentions. Returns best city guess or ''."""
    try:
        rows = arc_conn.execute(
            "SELECT body FROM messages WHERE contact_id = ? AND direction = 0 "
            "AND length(body) > 10 ORDER BY ts DESC LIMIT ?",
            (contact_id, limit),
        ).fetchall()
    except Exception:
        return ""
    city_votes: dict[str, int] = {}
    for row in rows:
        body = row[0] or ""
        for pat in LOC_SCAN_PATTERNS:
            m = pat.search(body)
            if m:
                city = m.group(1).strip()
                first_word = city.split()[0].lower()
                if (city.lower() not in LOC_BLOCKLIST
                        and first_word not in LOC_BLOCKLIST
                        and len(city) >= 4
                        and not city[0].isdigit()):
                    city_votes[city] = city_votes.get(city, 0) + 1
    # require at least 2 mentions to avoid noisy one-offs
    city_votes = {k: v for k, v in city_votes.items() if v >= 2}
    if not city_votes:
        return ""
    return max(city_votes, key=city_votes.__getitem__)


def _metro_for_city(city: str) -> str:
    """Map a city name to a metro (coarse — just covers the most common cases)."""
    c = city.lower()
    if any(x in c for x in ("san francisco", "sf", "palo alto", "menlo park", "sunnyvale",
                              "mountain view", "redwood", "oakland", "berkeley", "san jose")):
        return "San Francisco"
    if any(x in c for x in ("new york", "nyc", "brooklyn", "manhattan", "queens", "bronx")):
        return "New York"
    if any(x in c for x in ("los angeles", "la", "venice", "santa monica", "pasadena", "burbank")):
        return "Los Angeles"
    if "london" in c:
        return "London"
    if "stockholm" in c:
        return "Stockholm"
    if "berlin" in c:
        return "Berlin"
    if "paris" in c:
        return "Paris"
    if "amsterdam" in c:
        return "Amsterdam"
    if "singapore" in c:
        return "Singapore"
    return city  # pass through unknown cities as their own metro


def enrich_pass_a(conn, contact_id: int, force: bool = False) -> dict:
    """Deterministic offline enrichment. Returns dict of changes (may be empty)."""
    row = db.get_contact(conn, contact_id)
    if not row:
        return {}
    # linkedin_scrape facts come from an LLM over the person's full profile —
    # strictly better than area-code/TLD guesses, so leave those rows alone too.
    if not force and row["facts_source"] in ("manual", "llm", "linkedin_scrape"):
        return {}

    changes: dict[str, str] = {}
    home_metro = row["home_metro"] or ""
    home_city = row["home_city"] or ""
    home_country = row["home_country"] or ""
    role_type = row["role_type"] or ""

    # role_type from title
    if not role_type or force:
        title_text = " ".join(filter(None, [row["position"], row["company"]]))
        rt = _match_role_type(title_text)
        if rt:
            changes["role_type"] = rt

    # phone area code → metro
    idents = db.get_identifiers(conn, contact_id)
    phones = [i["value"] for i in idents if i["kind"] == "phone"]
    for phone in phones:
        ac = _extract_area_code(phone)
        if ac and ac in AREA_CODE_METRO:
            metro = AREA_CODE_METRO[ac]
            if not home_metro or force:
                changes["home_metro"] = metro
                changes["home_city"] = metro
            if not home_country or force:
                changes["home_country"] = AREA_CODE_COUNTRY.get(ac, "US")
            break

    # email TLD → country
    if not home_country or force:
        emails = [i["value"] for i in idents if i["kind"] == "email"]
        for email in emails:
            country = _email_country(email)
            if country:
                changes.setdefault("home_country", country)
                break

    return changes


def enrich_pass_b(conn, arc_conn, contact_id: int, force: bool = False) -> dict:
    """Message-scan enrichment. Returns dict of changes."""
    row = db.get_contact(conn, contact_id)
    if not row:
        return {}
    if not force and (row["home_city"] or
                      row["facts_source"] in ("manual", "llm", "linkedin_scrape")):
        return {}

    city = _scan_messages_for_city(arc_conn, contact_id)
    if not city:
        return {}
    metro = _metro_for_city(city)
    return {"home_city": city, "home_metro": metro}


def _apply_changes(conn, contact_id: int, changes: dict, source: str) -> bool:
    if not changes:
        return False
    # Never overwrite a field the user hand-edited — drop those keys, but
    # still write the rest, so a manual city no longer blocks role/country/etc.
    row = db.get_contact(conn, contact_id)
    manual = db.manual_field_set(row) if row else set()
    changes = {k: v for k, v in changes.items() if k not in manual}
    if not changes:
        return False
    changes["facts_source"] = source
    changes["facts_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(
        f"UPDATE contacts SET {set_clause} WHERE id = ?",
        (*changes.values(), contact_id),
    )
    return True


# ── Pass C: LLM ──────────────────────────────────────────────────────────────

LLM_SYSTEM = """You are a contact-data enricher. Given info about a person,
extract structured facts. Respond ONLY with valid JSON, no prose.

JSON schema (all fields optional, use null if unknown):
{
  "city": "...",       // most likely city they're based in
  "country": "...",    // country name (English)
  "metro": "...",      // major metro, e.g. "San Francisco", "New York"
  "topics": [...],     // 1-5 topic strings (lowercase), e.g. ["AI", "venture capital"]
  "role_type": "...",  // one of: founder investor tech product design sales marketing finance operations medical legal research recruiter consultant
  "blurb": "..."       // 1-sentence summary of who this person is professionally (max 120 chars)
}"""


def _build_llm_context(conn, arc_conn, contact_id: int) -> str:
    row = db.get_contact(conn, contact_id)
    if not row:
        return ""
    parts = [f"Name: {row['name']}"]
    if row["position"]:
        parts.append(f"Title: {row['position']}")
    if row["company"]:
        parts.append(f"Company: {row['company']}")
    if row["how_we_met"]:
        parts.append(f"Context: {row['how_we_met'][:200]}")
    if row["auto_summary"]:
        parts.append(f"Summary: {row['auto_summary'][:200]}")
    notes = db.get_notes(conn, contact_id)
    if notes:
        parts.append(f"Notes: {notes[0]['body'][:200]}")
    # grab a handful of their inbound messages
    try:
        msgs = arc_conn.execute(
            "SELECT body FROM messages WHERE contact_id = ? AND direction = 0 "
            "AND length(body) > 20 ORDER BY ts DESC LIMIT 5",
            (contact_id,),
        ).fetchall()
        if msgs:
            snippets = " | ".join((r[0] or "")[:120] for r in msgs)
            parts.append(f"Recent messages (received): {snippets}")
    except Exception:
        pass
    return "\n".join(parts)


def enrich_pass_c_one(conn, arc_conn, contact_id: int, client) -> dict:
    """Call Claude for one contact. Returns dict of changes or {} on failure."""
    context = _build_llm_context(conn, arc_conn, contact_id)
    if not context.strip():
        return {}
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
        raw = msg.content[0].text.strip()
        # strip markdown fences if present
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        data = json.loads(raw)
    except Exception as exc:
        print(f"  LLM error for contact {contact_id}: {exc}", file=sys.stderr)
        return {}

    changes: dict[str, str] = {}
    if data.get("city"):
        changes["home_city"] = str(data["city"])[:80]
    if data.get("country"):
        changes["home_country"] = str(data["country"])[:80]
    if data.get("metro"):
        changes["home_metro"] = str(data["metro"])[:80]
    if data.get("topics"):
        try:
            changes["topics"] = json.dumps([str(t)[:40] for t in data["topics"][:5]])
        except Exception:
            pass
    if data.get("role_type"):
        changes["role_type"] = str(data["role_type"])[:40]
    if data.get("blurb"):
        changes["blurb"] = str(data["blurb"])[:200]
    return changes


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llm", action="store_true", help="run LLM pass (needs ANTHROPIC_API_KEY)")
    # The LLM-pass cap is a user-visible setting (ENRICH_LLM_MAX / ENRICH_LIMIT
    # in Settings), not a silent literal — an explicit --limit still wins. One
    # reader for it (settings.spend_cap), shared with the pages that print it.
    import settings
    _dflt = settings.spend_cap()
    parser.add_argument("--limit", type=int, default=_dflt,
                        help=f"max contacts for LLM pass (default {_dflt}, from ENRICH_LLM_MAX)")
    parser.add_argument("--force", action="store_true", help="re-enrich even already-tagged contacts")
    args = parser.parse_args()

    conn = db.get_db()
    db.init_db()

    # open archive.db if it exists
    arc_conn = None
    if os.path.exists(arc.ARCHIVE_DB_PATH):
        arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)

    rows = conn.execute(
        f"SELECT id, name, facts_source FROM contacts WHERE {figures.population_where('visible')} ORDER BY id"
    ).fetchall()

    stats = {"pass_a": 0, "pass_b": 0, "pass_c": 0, "skipped": 0}
    touched: set = set()  # unique contacts enriched by any offline pass (for the receipt)

    print(f"Enriching {len(rows)} contacts (Pass A + {'B' if arc_conn else 'no archive'})…")

    for row in rows:
        cid = row["id"]
        # Pass A
        changes_a = enrich_pass_a(conn, cid, force=args.force)
        if changes_a:
            _apply_changes(conn, cid, changes_a, "offline")
            stats["pass_a"] += 1
            touched.add(cid)

        # Pass B
        if arc_conn:
            changes_b = enrich_pass_b(conn, arc_conn, cid, force=args.force)
            if changes_b:
                current = db.get_contact(conn, cid)
                if args.force or not current["home_city"]:
                    _apply_changes(conn, cid, changes_b, "message_scan")
                    stats["pass_b"] += 1
                    touched.add(cid)

    conn.commit()
    print(f"Pass A: {stats['pass_a']} contacts enriched (area code / TLD / title)")
    print(f"Pass B: {stats['pass_b']} contacts enriched (message scan)")
    # canonical, single-line total for the result receipt (parse_receipts) — a
    # unique count, so a contact filled by both passes isn't double-counted.
    print(f"Enriched {len(touched)} contacts (offline facts pass)")

    if not args.llm:
        conn.close()
        if arc_conn:
            arc_conn.close()
        return

    # Pass C: LLM
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("No ANTHROPIC_API_KEY set — skipping LLM pass.", file=sys.stderr)
        conn.close()
        if arc_conn:
            arc_conn.close()
        return

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        print("anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
        conn.close()
        if arc_conn:
            arc_conn.close()
        return

    if arc_conn is None:
        # create a dummy connection for arc queries that won't find anything
        arc_conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH) if os.path.exists(arc.ARCHIVE_DB_PATH) else None

    # pick candidates: untagged contacts with some context (position or notes)
    candidates = conn.execute(
        f"""SELECT c.id, c.name FROM contacts c
           WHERE {figures.population_where('visible', 'c')}
             AND (c.facts_source = '' OR c.facts_source IS NULL OR ?)
             AND c.facts_source != 'manual'
             AND (c.position != '' OR c.how_we_met != '' OR c.auto_summary != ''
                  OR EXISTS (SELECT 1 FROM notes n WHERE n.contact_id = c.id))
           ORDER BY (c.msg_count + c.imsg_count + c.wa_count) DESC
           LIMIT ?""",
        (1 if args.force else 0, args.limit),
    ).fetchall()

    if not candidates:
        print("No LLM candidates found (all contacts already enriched or lack context).")
    else:
        print(f"\nPass C (LLM): enriching {len(candidates)} contacts…")
        if arc_conn is None:
            # open a dummy connection so enrich_pass_c_one doesn't crash
            import sqlite3 as _sq
            arc_conn = _sq.connect(":memory:")
            arc_conn.row_factory = _sq.Row
        for i, row in enumerate(candidates):
            cid = row["id"]
            name = row["name"]
            # NC-082. Release before the model call, commit after the write.
            #
            # enrich_pass_c_one() is a live Anthropic request. _apply_changes()
            # UPDATEs contacts and does not commit — the only commit was after the
            # whole loop — so the first enriched contact took crm.db's single
            # writer lock and held it across every remaining contact's model call
            # plus the 0.3s sleep between them. At the default --limit that is
            # minutes against a 20-second WRITE_LOCK_TIMEOUT_SEC. Pass C only runs
            # on an explicit click (a chained background pass is never --llm, and a
            # test enforces that), but a click still runs it as a subprocess beside
            # the live app, so what fails is whatever the user does next.
            #
            # Committing per contact also stops one failure late in the pass
            # discarding every enrichment already paid for.
            db.release_write_lock(conn)
            changes_c = enrich_pass_c_one(conn, arc_conn, cid, client)
            if changes_c:
                _apply_changes(conn, cid, changes_c, "llm")
                conn.commit()
                stats["pass_c"] += 1
                metro = changes_c.get("home_metro", "")
                role = changes_c.get("role_type", "")
                print(f"  [{i+1}/{len(candidates)}] {name}: metro={metro or '?'}, role={role or '?'}")
            else:
                print(f"  [{i+1}/{len(candidates)}] {name}: no data returned")
            # small delay to avoid hammering the API
            if i < len(candidates) - 1:
                time.sleep(0.3)
        conn.commit()
        print(f"Pass C: {stats['pass_c']} contacts enriched via LLM")

    conn.close()
    if arc_conn:
        arc_conn.close()


if __name__ == "__main__":
    main()
