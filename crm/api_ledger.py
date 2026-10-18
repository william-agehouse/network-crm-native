#!/usr/bin/env python3
"""The Data page's one ledger — NC-088, built with NC-114.

A route-registry module serving GET /api/data/ledger: every row the new Data page
draws, the "Needs you" list above them, and the figures for "This install", in
one answer.

WHY ONE ENDPOINT
The old Data page (deleted in NC-236) asked eight endpoints and decided on the
client which source needed you, which row covered which, and what each number
meant. That could put two true numbers on one screen that disagreed (NC-076,
NC-122), or say "No source needs you now" beside a panel asking for a token. The
fix is that every number on the page comes from one place. This is that place.
The page renders it and decides nothing.

It invents nothing either. Every fact is asked of the module that already owns it:

  api_sources._sources     how each source runs, whether it is set up, last run,
                           and why it cannot collect (source_blockers)
  api_fda                  what the sync helper copies and what macOS refuses
  api_linkedin             the three LinkedIn lanes and their one state word
  api_gmail                which Gmail accounts need a file or a sign-in
  api_contacts / db        people with history per channel, over `active`
  api_provenance           which people a source first added, over `active`
  api_import               what is sitting on this Mac to import (?scan=1 only)
  api_instance             the people ledger and what is on disk

THE ROW
One shape for every row (the LedgerRow): what it gives you, what you
have from it now, what switching it on takes here, and what that costs you. The
cost is the person's, on one scale — free, a file, an address, a wait, a code to
paste, a permission, money, a Google setup, a second Mac — and rows sort by it, so the
free thing comes first. A source the sync helper already keeps current costs
nothing more, so it rises to the top whether or not it is in use yet.

Every source is always listed, unused ones too, because hiding them would
stop people from trying them. An off row is an offer: what it gives,
and what it takes.

WHAT NEEDS YOU
Five conditions and no others: a file for a source is on this Mac and nothing
will import it; a copy that refreshes itself has stopped; a credential is
missing, refused or running out; a permission is being refused; a setting the
source needs is empty. A source nobody has started is never in this list — "no
LinkedIn token" is an offer on a fresh install and a need once LinkedIn has
brought people in.

THE SCAN
Finding files on this Mac globs folders macOS protects, which can wait on a
consent dialog. So the scan runs only when asked (?scan=1): the page asks once
without it, draws, then asks again with it. Both answers come from this module,
so the second only ever adds to the first.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import api_contacts
import api_import
import api_instance
import api_provenance
import api_setup
import api_sources
import archive as arc
import db
import figures
import imessage_import
import importers
import settings
import style_profile
import whatsapp_import

log = logging.getLogger("networkcrm")

ROUTES = {"GET": {"/api/data/ledger": "handle_data_ledger",
                  "/api/data/linkedin-wait": "handle_linkedin_wait"},
          "POST": {"/api/data/linkedin-asked": "handle_linkedin_asked"}}


# ── The cost scale ────────────────────────────────────────────────────────────
# kind -> (rank, the words on the row, the tone). The tone is the app's own
# staleness ramp reused to mean cost: `none` free, `low` a small
# cost or a wait, `mid` real friction or money, `stop` hardware or an account
# you may not have. Never used for anything but cost.
COSTS = {
    "free":       (0, "free", "none"),
    "file":       (1, "a file", "low"),
    "address":    (2, "an address", "low"),
    "wait":       (3, "a wait", "mid"),
    # NC-230: the chips are words, with no coloured dot, so
    # colour on these pages means only a row's state. "A secret" did not say
    # what you do; "a sign-in" hid five minutes on Google's developer site.
    "secret":     (4, "a code to paste", "mid"),
    "permission": (5, "a permission", "mid"),
    "money":      (6, "money", "mid"),
    "sign_in":    (7, "a Google setup", "stop"),
    "machine":    (8, "a second Mac", "stop"),
    "none":       (9, "not in this copy", "stop"),
}

# How many days a copy can sit before it counts as fallen behind. The old Data
# page's BEHIND_DAYS, moved here with the decision it drives.
BEHIND_DAYS = 7

# NC-215. How many of its own checks in a row a source on a timer may miss
# before its row stops being green. The background loop keeps quiet about every
# failure but a missing sign-in, on purpose (api_import: it waits out its period
# and tries again), so a source whose checks keep failing only shows it one way:
# its "last worked" time stops moving. Three missed checks is a pattern, not a
# blip. For Gmail's 12-hour clock that is a day and a half.
LATE_CHECKS = 3

# Registry keys folded into another row rather than given one (the old Data
# page's PART_OF).
PART_OF = {"whatsapp_photos": "whatsapp"}

# A finding for one of these is not asked for while a sibling that covers it has
# data at least as new (the old Data page's COVERED_BY). The covered row still
# renders — the link-based calendars lapse, and the hand-exported file is the way
# back — it just stops asking.
COVERED_BY = {
    "calendar": ("calendar_local", "calendar_google", "calendar_icloud"),
    "calendar_google": ("calendar_local",),
    "calendar_icloud": ("calendar_local",),
}

# Which row a registry key's findings and blockers land on. LinkedIn is one
# registry key and three rows; its export file belongs to the connections row.
ROW_OF = {"linkedin": "linkedin_connections", "whatsapp_photos": "whatsapp"}

# What a found file holds, by source — the old Data page's FOUND_UNIT. The iMessage
# count is who is in that copy, not who is in the app (NC-122).
FOUND_UNIT = {
    "whatsapp": ("conversation", "conversations"),
    "imessage": ("person in this copy", "people in this copy"),
    "contacts": ("card", "cards"),
    "calendar": ("meeting", "meetings"),
}


# ── The rows, in the order they sit within one cost ───────────────────────────
# `gives` is the offer, in the person's words. Everything that depends on this
# install — what it takes, what it costs, what you have — is worked out below.
ROWS: List[Dict[str, Any]] = [
    {"key": "whatsapp", "label": "WhatsApp",
     "sources": ("whatsapp", "whatsapp_photos"),
     "gives": "The other half of your messaging, and the photos people chose "
              "for themselves."},
    {"key": "contacts", "label": "Apple Contacts",
     "sources": ("contacts",),
     "gives": "The names, numbers and emails everything else attaches to."},
    {"key": "imessage", "label": "iMessage",
     "sources": ("imessage",),
     "gives": "Years of real conversation, searchable per person."},
    {"key": "calendar_local", "label": "Apple Calendar",
     "sources": ("calendar_local",),
     "gives": "Who you actually sit down with, and how often, from every "
              "calendar on this Mac."},
    {"key": "helper", "label": "Automatic copying",
     "sources": (),
     "gives": "Keeps iMessage, WhatsApp, Apple Contacts and Apple Calendar up to "
              "date by themselves, every couple of minutes, with no internet access."},
    {"key": "email", "label": "Email",
     "sources": ("email",),
     "gives": "Older email threads, from a mailbox you export."},
    {"key": "calendar", "label": "Calendar file",
     "sources": ("calendar",),
     "gives": "Meetings from any calendar that can save them to a file."},
    {"key": "calendar_google", "label": "Google Calendar link",
     "sources": ("calendar_google",),
     "gives": "Your Google meetings, fetched from the calendar's private address."},
    {"key": "calendar_icloud", "label": "iCloud Calendar link",
     "sources": ("calendar_icloud",),
     "gives": "Your iCloud meetings, from a calendar you publish."},
    {"key": "linkedin_connections", "label": "LinkedIn connections",
     "sources": ("linkedin",),
     "gives": "Everyone you are connected to, with their title and company."},
    {"key": "linkedin_messages", "label": "LinkedIn messages",
     "sources": ("linkedin",),
     "gives": "What you said to your connections, straight from LinkedIn."},
    {"key": "gmail", "label": "Gmail",
     "sources": ("gmail",),
     "gives": "The email threads you actually replied to, straight from Google."},
    {"key": "google_photos", "label": "Google contact photos",
     "sources": ("google_photos",),
     "gives": "The photos saved on your Google contacts."},
    {"key": "linkedin_facts", "label": "LinkedIn profile facts",
     "sources": ("linkedin_facts",),
     "gives": "Where people live, what they write about, and their headline."},
    {"key": "facts_llm", "label": "Facts written by AI",
     "sources": ("facts_llm",),
     "gives": "A city, topics and a short summary for people, written by Claude "
              "from your own messages."},
    {"key": "engagement", "label": "Closeness",
     "sources": ("engagement",),
     "gives": "How close you are to each person, from how often and how "
              "recently you talk."},
    {"key": "facts", "label": "Facts from your messages",
     "sources": ("facts",),
     "gives": "Cities, employers and topics read out of your messages, on this "
              "Mac, for free."},
    {"key": "style", "label": "How you write",
     "sources": ("style",),
     "gives": "Your own writing voice, so drafted messages sound like you."},
]
ORDER = {r["key"]: i for i, r in enumerate(ROWS)}

# NC-217. What an open row says under "How", written for this page. The
# registry's `export_help` was written for the old page: it pointed at "the panel
# at the top of this page", "'Found on this Mac' above", a "Run import" button
# and "this button", none of which exist here, and sent people to setting names
# such as NC_ICS_GOOGLE_URL. A row missing here says nothing under How rather
# than the old text.
HELP = {
    "whatsapp": (
        "If WhatsApp Desktop is on this Mac and signed in, **Import** reads it "
        "where it is. macOS asks whether Network CRM may read WhatsApp's folder "
        "each time, because the answer does not stick. Choose Allow.\n\n"
        "With automatic copying set up, nothing asks: WhatsApp is copied every "
        "couple of minutes and imported by itself.\n\n"
        "<whatsapp groups> Profile pictures come with it. A photo you set yourself "
        "is never replaced."),
    "contacts": (
        "1. In Contacts, select everyone, then choose File, Export, Export vCard.\n"
        "2. Choose that file here and press **Import**.\n\n"
        "Anyone new in it is added, because your address book is your own list of "
        "people. People already here get their phone, email, company and title "
        "filled in. Anything you typed yourself is never replaced.\n\n"
        "A file you export is only as fresh as the day you exported it. With "
        "automatic copying set up, your address book stays current by itself."),
    "imessage": (
        "1. Press **Open the Messages folder**. Finder opens with the three chat.db "
        "files already selected.\n"
        "2. Press ⌘C.\n"
        "3. Press **Open the import folder**, press ⌘V, and choose Replace if Finder "
        "asks.\n\n"
        "It imports itself from there, and again whenever you paste a fresher copy. "
        "Copy all three files: chat.db on its own leaves your newest messages "
        "behind.\n\n"
        "Each conversation is matched to a person by phone number or email. A "
        "number that is on nobody here is looked up in your Contacts app, and a "
        "number in neither is skipped. <imessage groups>"),
    "calendar_local": (
        "Reads every calendar in the Calendar app, including the Google and iCloud "
        "accounts you added there, so with this working you need none of the "
        "calendar links.\n\n"
        "Each meeting is saved against the people already here who were in it. "
        "Meetings with more than 6 other people are skipped. **Preview** shows "
        "what would be saved, and saves nothing."),
    "email": (
        "1. Save a mailbox as a file (.mbox). In Apple Mail: Mailbox, then Export "
        "Mailbox. From Gmail: Google's download of your data (Takeout), with Mail "
        "ticked.\n"
        "2. Choose that file here and press **Import**.\n\n"
        "Only real two-way conversations with people already here are counted."),
    "calendar": (
        "1. Save a calendar as a file (.ics). In Calendar: File, Export, Export. "
        "In Google Calendar: Settings, Import & export, Export, then unzip it.\n"
        "2. Choose that file here and press **Import**.\n\n"
        "A file is a snapshot of the day you saved it, and it will not see a "
        "meeting you book afterwards. Apple Calendar, or a calendar link, stays "
        "current by itself.\n\n"
        "Meetings are only matched to people already here, and ones with more than "
        "6 other people are skipped. A repeating meeting is read as its first "
        "date. Nothing leaves this Mac."),
    "calendar_google": (
        "1. Press **Open Google Calendar settings**, choose the calendar in the "
        "left column, then Integrate calendar, and copy **Secret address in iCal "
        "format**.\n"
        "2. Paste it here and press **Connect**. Treat it like a password: anyone "
        "who has it can read the whole calendar.\n"
        "3. Switch on **Download linked calendars**.\n\n"
        "Two calendars? Paste both addresses with a comma between them. Or add "
        "the Google account to the Mac's Calendar app and use the Apple Calendar "
        "row, which reads every calendar there, however many accounts.\n\n"
        "What leaves this Mac: one request to Google per address, carrying that "
        "address and nothing else, once a day. Would rather send nothing? Save the "
        "calendar to a file and use the Calendar file row."),
    "calendar_icloud": (
        "1. In Calendar, right-click the calendar, choose Share Calendar, tick "
        "Public Calendar, and copy the link.\n"
        "2. Paste it here and press **Connect**. Anyone with the link can read "
        "that calendar, so publishing it is a decision of its own.\n"
        "3. Switch on **Download linked calendars**.\n\n"
        "Two calendars? Paste both links with a comma between them.\n\n"
        "What leaves this Mac: one request to Apple per link, carrying the link and "
        "nothing else, once a day. If Apple Calendar is working here, you do not need "
        "this."),
    "gmail": (
        "Reads your Gmail directly, so it stays up to date by itself, with no "
        "export to make. Only mail to and from people already here is fetched. "
        "It does not download your inbox.\n\n"
        "Google will not let one app read many people's mail without a yearly "
        "security audit, so everyone makes their own key, once, in about five "
        "minutes. The steps are below, with a button for each.\n\n"
        "Would rather give no account access? Export the same mail from Google "
        "and use the Email row. That is a snapshot, so it stops at the day you "
        "export it."),
    "google_photos": (
        "Uses the Gmail setup: once Gmail works, press **Import**. A Google page "
        "opens once for each account, asking to read your contacts, separately "
        "from your mail.\n\n"
        "Photos are matched to people already here by email, phone or name. A "
        "photo you set yourself, or one from your address book, is never "
        "replaced."),
    "linkedin_facts": (
        "The facts LinkedIn will not hand over, like where someone lives, what "
        "they write about and their headline, come from a second Mac that visits "
        "a few dozen of your connections' profiles a day, most relevant first.\n\n"
        # NC-317: README's warning, said where the setup is.
        "**Before you set this up:** reading profiles automatically breaks "
        "LinkedIn's User Agreement, and the account the other Mac uses can be "
        "restricted or permanently banned. Going slowly lowers that risk; nothing "
        "makes it allowed. Everything else in this app works without it.\n\n"
        "Type that Mac's sign-in (as user@name) and where its key file is on this "
        "Mac, then press **Import** to bring in what it has found. Anything you "
        "typed yourself is never replaced, unless you allow it in Settings."),
    "facts_llm": (
        "Sends a short piece of each person's messages to Claude, which writes "
        "their city, country, topics, role and a one-line summary. The key is "
        "yours, and Anthropic charges your account for each run. One run covers "
        "at most the number of people shown under What it takes.\n\n"
        "Nothing is sent until you press **Import**."),
    "engagement": (
        "Works out how close you are to each person: on how many different days "
        "you have talked, who writes to whom, and for how long. It runs by itself "
        "after every import. Free, and nothing leaves this Mac."),
    "facts": (
        "Reads cities and countries out of phone numbers and email addresses, and "
        "roles out of job titles. It runs by itself after an import. Free, and "
        "nothing leaves this Mac."),
    "style": (
        "Measures how you write from the messages you sent: length, greetings, "
        "sign-offs, emoji and punctuation, <style languages> Drafted messages then "
        "follow your habits.\n\n"
        "It keeps numbers, not text: no message and no other person's name is "
        "stored. LinkedIn is left out, because its import cannot always tell who "
        "sent a message. Email needs your email address set in Settings. Nothing "
        "leaves this Mac."),
}


def _a(word: str) -> str:
    """'a' or 'an' before a source's name: an iMessage copy, a LinkedIn file."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def _sentence(text: str) -> str:
    """A blocker is written to follow a label ("LinkedIn: no token, so…"). As a
    row's own status it starts a sentence."""
    return text[:1].upper() + text[1:] if text else text


def _n(n: int, one: str, many: str) -> str:
    return f"{int(n):,} {one if int(n) == 1 else many}"


def _for(seconds: float) -> str:
    """How long, in the one unit a person would use: "40 minutes", "5 hours", "3 days"."""
    s = max(0.0, float(seconds or 0))
    if s < 3600:
        return _n(max(1, round(s / 60)), "minute", "minutes")
    if s < 2 * 86400:
        return _n(round(s / 3600), "hour", "hours")
    return _n(round(s / 86400), "day", "days")


def _seconds_since(iso: str) -> Optional[float]:
    """Seconds since a stored time, or None when it cannot be read. The stamps
    are written in UTC with their offset; one without is read as UTC too."""
    try:
        t = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def _cost(kind: str) -> Dict[str, Any]:
    rank, label, tone = COSTS[kind]
    return {"kind": kind, "rank": rank, "label": label, "tone": tone}


def _secret(key: str, label: str, placeholder: str = "") -> Dict[str, Any]:
    """A credential box: whether it holds a value, and its last four only."""
    val = (os.environ.get(key) or "").strip()
    return {"kind": "secret", "key": key, "label": label, "set": bool(val),
            "masked": settings.mask(val) if val else "", "placeholder": placeholder}


def _text(key: str, label: str, placeholder: str = "") -> Dict[str, Any]:
    """A plain setting box, shown back in full because it is not a secret."""
    return {"kind": "text", "key": key, "label": label,
            "value": (os.environ.get(key) or "").strip(), "placeholder": placeholder}


def _switch_act(key: str, label: str, on: bool) -> Dict[str, Any]:
    return {"kind": "switch", "key": key, "label": label, "on": bool(on)}


def _import_act(spec: Dict[str, Any], verb: str = "Import") -> Dict[str, Any]:
    return {"kind": "import", "source": spec["key"], "verb": verb,
            "pick": spec.get("pick_kind") or "",
            "folder": spec.get("kind") == "folder",
            "needs_path": spec.get("kind") in ("file", "folder"),
            "preview": bool(spec.get("supports_dry_run"))}


def _opens(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"kind": "open", "target": h["key"], "label": h["label"]}
            for h in spec.get("open_helpers") or [] if h.get("label")]


# ── What the page needs to know, gathered once ────────────────────────────────

def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — one unhappy owner must not blank the page
        log.debug("api_ledger: %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return default


def _photos_by_source(conn: Any) -> Dict[str, int]:
    active = figures.population_where("active")
    return {(r["photo_source"] or ""): r["n"] for r in conn.execute(
        "SELECT photo_source, COUNT(*) AS n FROM contacts "
        f"WHERE photo != '' AND {active} GROUP BY photo_source")}


def _context(conn: Any, scan: bool) -> Dict[str, Any]:
    import api_fda
    import api_gmail
    import api_linkedin
    ctx: Dict[str, Any] = {
        "sources": {r["key"]: r for r in _safe(lambda: api_sources._sources(conn), [])},
        "specs": {s["key"]: s for s in importers.list_sources()},
        "cov": _safe(lambda: api_contacts._channel_coverage(conn), {}),
        "scov": _safe(lambda: db.source_coverage(conn), {}),
        "msgs": _safe(api_setup._archive_messages_by_channel, {}),
        "week": _safe(_messages_this_week, {}),
        "photos": _safe(lambda: _photos_by_source(conn), {}),
        "prov": _safe(lambda: api_provenance._crm_counts(conn), {}),
        "fda": _safe(api_fda.handle_fda_status, {"state": "unavailable"}),
        "li": _safe(lambda: api_linkedin.handle_linkedin_status(conn), None),
        "gm": _safe(lambda: api_gmail.status(conn), {}),
        "li_wait": _safe(lambda: linkedin_wait(conn), {}),
        "autosync": os.environ.get("NC_AUTOSYNC", "1").strip() != "0",
        "found": None,
    }
    if scan:
        # NC-236: a check that failed is not a check that found nothing. The old
        # page kept the two apart (NC-033, NC-034) and so does this one.
        found = _safe(lambda: api_import.handle_import_detect(conn)["found"], None)
        ctx["scan_failed"] = found is None
        ctx["found"] = found or []
    return ctx


def _prov(ctx: Dict[str, Any], source: str, field: str) -> int:
    counts = (ctx["prov"].get(field) or {}).get("by_token") or {}
    return sum(counts.get(t, 0) for t in api_provenance.tokens_for(source, field))


def _synced(ctx: Dict[str, Any], key: str) -> bool:
    return bool((ctx["sources"].get(key) or {}).get("last_run_known"))


def _copying(ctx: Dict[str, Any], label: str) -> bool:
    return label in set(ctx["fda"].get("ok") or [])


def _helper_available(ctx: Dict[str, Any]) -> bool:
    return bool(ctx["fda"].get("available"))


# ── One row ───────────────────────────────────────────────────────────────────

def _base(row: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    key = row["sources"][0] if row["sources"] else ""
    src = ctx["sources"].get(key) or {}
    return {
        "key": row["key"], "label": row["label"],
        "sources": list(row["sources"]), "gives": row["gives"],
        "have": {"says": "", "figures": []},
        "takes": "", "cost": _cost("free"),
        "state": {"tone": "off", "says": ""},
        "in_use": False,
        "blocked": src.get("blocked", ""),
        "detail": {
            "runs": src.get("runs_says", ""),
            "last_run": src.get("last_run", ""),
            # Worked out here, where the stamp's time zone is known: the page
            # used to round it to the day, so a source that runs every two
            # minutes read "Last ran today" (NC-217).
            "last_run_seconds": _seconds_since(src.get("last_run") or ""),
            "last_run_caveat": src.get("last_run_caveat", ""),
            "help": _help(row["key"]),
            # NC-238. Sentences the open row's "Now" block adds after its state
            # and last run: facts the old LinkedIn panel said, each a whole
            # sentence, so the page only has to print them.
            "now": [],
            "manual": "",
            "acts": [],
            "provenance": api_provenance.resolve_source(key)
            if key in api_provenance.SOURCE_TOKENS else "",
        },
    }


def _say_have(out: Dict[str, Any], figs: List[Dict[str, Any]]) -> None:
    figs = [f for f in figs if f and f.get("n")]
    out["have"]["figures"] = figs
    out["have"]["says"] = " · ".join(figures.unit_says(f["unit"], f["n"])
                                     + (f" {f['says']}" if f.get("says") else "")
                                     for f in figs)


def _fig(n: int, unit: str, says: str = "", side: str = figures.SIDE_STORED,
         population: str = "active") -> Dict[str, Any]:
    return figures.figure(int(n or 0), unit, population, side=side, says=says)


def _messages_this_week() -> Dict[str, int]:
    """Messages saved per channel whose time is in the last 7 days. NC-233: a
    row said "N messages", a lifetime total, which is no evidence that
    anything arrived this week; the total is still in This install.

    WEEK_SQL groups by `+channel` so SQLite reads only this week's entries in
    the time index. Plain `channel` lets it pick the channel index instead and
    walk every message, which is far slower on a large archive."""
    with arc.open_if_present() as conn:
        if conn is None:
            return {}
        return {str(r[0] or ""): int(r[1] or 0) for r in conn.execute(WEEK_SQL)}


WEEK_SQL = ("SELECT channel, COUNT(*) FROM messages WHERE ts >= datetime('now', '-7 days') "
            "GROUP BY +channel")


def _week(out: Dict[str, Any], ctx: Dict[str, Any], channel: str,
          lifetime: int, figs: List[Dict[str, Any]]) -> None:
    """A message row's "You have now": this week's messages, then the rest."""
    week = int((ctx["week"] or {}).get(channel, 0))
    _say_have(out, [_fig(week, "messages", "in the last 7 days", side=figures.SIDE_ARCHIVE,
                         population="all")] + figs)
    if lifetime and not week:
        out["have"]["says"] = " · ".join(
            [s for s in (out["have"]["says"], "no messages in the last 7 days") if s])


def _msgs(n: int) -> Dict[str, Any]:
    # Messages are every message saved on this Mac, the same figure the page's
    # other message counts add up (NC-122) — so the population is `all`.
    return _fig(n, "messages", side=figures.SIDE_ARCHIVE, population="all")


def _paused(ctx: Dict[str, Any], key: str) -> str:
    """Why a source that runs by itself is not running now, or "".

    Two switches stop it: all automatic work at once (NC_AUTOSYNC, the autosync
    loop every automatic source runs on), or the source's own switch, which
    api_sources names as `off_switch`. Either one makes "Imports itself" untrue,
    and a fresh-install preview, which runs with NC_AUTOSYNC=0, must not show
    that sentence on a row that is never going to import anything."""
    src = ctx["sources"].get(key) or {}
    if src.get("runs") not in (api_sources.RUNS_WATCHED, api_sources.RUNS_CLOCK,
                               api_sources.RUNS_CHAINED):
        return ""
    if not ctx["autosync"]:
        return "Paused: automatic importing is off"
    switch = src.get("off_switch") or ""
    if switch and switch != "NC_AUTOSYNC" and os.environ.get(switch, "1").strip() == "0":
        return "Paused: automatic importing is off for this source"
    return ""


def _late(ctx: Dict[str, Any], key: str) -> str:
    """Why a source on a timer is behind, or "": its last check that worked was
    LATE_CHECKS checks ago or more (NC-215). Before this, green meant "set up",
    not "still arriving", and a Gmail whose every check was failing stayed green
    with "Checks every 12 hours on its own" under it."""
    src = ctx["sources"].get(key) or {}
    hours = int(src.get("every_hours") or 0)
    if src.get("runs") != api_sources.RUNS_CLOCK or hours <= 0:
        return ""
    ago = _seconds_since(src.get("last_run") or "")
    if ago is None or ago < hours * 3600 * LATE_CHECKS:
        return ""
    every = "every hour" if hours == 1 else f"every {hours} hours"
    return (f"The last check that worked was {_for(ago)} ago. It normally checks "
            f"{every}")


def _ok(out: Dict[str, Any], ctx: Dict[str, Any], says: str = "") -> Dict[str, str]:
    """The state of a source that is in use and has nothing wrong with it."""
    key = (out["sources"] or [""])[0]
    why = _paused(ctx, key)
    if why:
        return {"tone": "paused", "says": why}
    late = _late(ctx, key)
    if late:
        return {"tone": "behind", "says": late}
    return {"tone": "ok", "says": says or out["detail"]["runs"]}


def _running(out: Dict[str, Any], ctx: Dict[str, Any], says: str = "") -> None:
    out["in_use"] = True
    out["state"] = _ok(out, ctx, says)


def _helper_copy_row(row: Dict[str, Any], ctx: Dict[str, Any], label: str,
                     in_use: bool, off_takes: str, off_cost: str,
                     manual: str) -> Dict[str, Any]:
    """The four sources the sync helper copies share one rule for what they take.

    Copied by the helper: nothing more, it is free. Otherwise the manual route is
    the price, and the helper is named as the other way in (both routes, one
    switch — invariant 6)."""
    out = _base(row, ctx)
    spec = ctx["specs"].get(row["sources"][0]) or {}
    auto = _auto_copy(label)
    if _copying(ctx, label):
        out["takes"] = "Nothing. Automatic copying keeps it up to date."
        out["cost"] = _cost("free")
        # NC-216. Import used to ask you to choose a file here, for a copy the
        # app makes itself, deep inside its own data folder.
        out["detail"]["acts"] = [dict(_import_act(spec, "Import now"), path=auto,
                                      needs_path=False,
                                      path_says="The copy automatic copying made")]
    else:
        out["takes"] = off_takes
        out["cost"] = _cost(off_cost)
        out["detail"]["acts"] = [_import_act(spec)] + _opens(spec)
    out["detail"]["manual"] = manual
    if in_use:
        _running(out, ctx, "Kept up to date by automatic copying"
                 if _copying(ctx, label) else "")
    return out


def _groups(n: int) -> str:
    """The group-chat sentence, from the limit the importer really uses: the
    two limits are settings, so a number written into the text could go stale."""
    if n <= 0:
        return "Group chats are not saved: that is switched off."
    return (f"Group chats with up to {n} members are saved too, for people "
            "already here, under the group's name.")


def _and(words: List[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    words = [w for w in words if w]
    return ", ".join(words[:-1]) + " and " + words[-1] if len(words) > 1 else "".join(words)


def _languages() -> str:
    """NC-326. Which languages the writing measurement tells apart, named from
    the measuring module itself, so adding one there changes this sentence too.
    It said "Swedish and English kept apart", and a Finnish or German writer had
    no way to learn that their messages land in one pile with no language."""
    words = _safe(style_profile.measured_languages, [])
    if not words:
        return "measured together."
    return (f"measured by language. It tells {_and(words)} apart; messages in any "
            "other language are measured together, as one group.")


def _help(key: str) -> str:
    text = HELP.get(key, "")
    if " groups>" in text:
        text = (text.replace("<whatsapp groups>", _groups(_safe(whatsapp_import.group_max_members, 15)))
                    .replace("<imessage groups>", _groups(_safe(imessage_import.group_max_members, 8))))
    if "<style languages>" in text:
        text = text.replace("<style languages>", _languages())
    return text


def _auto_copy(label: str) -> str:
    """The copy automatic copying keeps of a source, by the helper's own label.
    Read when asked, not when this module loads, so it is always the path the
    importers use."""
    return {importers.HELPER_IMESSAGE: importers.IMSG_AUTO_DB,
            importers.HELPER_WHATSAPP: importers.WA_AUTO_DB,
            importers.HELPER_CONTACTS: importers.AB_AUTO_DB}.get(label, "")


def _row_whatsapp(row, ctx):
    n_people = ctx["cov"].get("whatsapp", 0)
    n_msgs = ctx["msgs"].get("whatsapp", 0)
    n_photos = ctx["photos"].get("whatsapp", 0)
    in_use = bool(n_people or n_msgs or _synced(ctx, "whatsapp"))
    out = _helper_copy_row(
        row, ctx, importers.HELPER_WHATSAPP, in_use,
        off_takes="Nothing but a yes. macOS asks whether this app may read "
                  "WhatsApp's folder, and asks again later.",
        off_cost="free",
        manual="Without automatic copying, this reads WhatsApp Desktop's own folder "
               "and macOS asks each time. With it, nothing asks.")
    # NC-216. Named as the cheapest place to start on every fresh install, even
    # on a Mac with no WhatsApp at all. Once the scan has looked and found none,
    # it says what it really takes and is not offered first.
    if (ctx["found"] is not None and not in_use
            and not _copying(ctx, importers.HELPER_WHATSAPP)
            and not any(f.get("source") == "whatsapp" for f in ctx["found"])):
        # NC-325. It named the condition and not the next step, and someone with
        # WhatsApp on their phone only had nothing to go on: the app reads
        # WhatsApp Desktop's own files and nothing else (no chat export).
        out["takes"] = ("WhatsApp Desktop, installed and signed in on this Mac. It was "
                        "not found here: install it and sign in with your phone, or "
                        "skip this source.")
        out["state"] = {"tone": "off",
                        "says": "WhatsApp on your phone alone gives this app nothing "
                                "to read. Install WhatsApp Desktop and sign in with "
                                "your phone, or skip this source"}
        out["absent"] = True
    _week(out, ctx, "whatsapp", n_msgs,
          [_fig(n_people, "people", "with history"), _fig(n_photos, "photos")])
    return out


def _row_contacts(row, ctx):
    n = _prov(ctx, "contacts", "people")
    out = _helper_copy_row(
        row, ctx, importers.HELPER_CONTACTS, bool(n or _synced(ctx, "contacts")),
        off_takes="A card file you export from Contacts, about a minute, or "
                  "nothing at all once automatic copying is set up.",
        off_cost="file",
        manual="With automatic copying set up, your address book is copied by "
               "itself and there is no file to export.")
    _say_have(out, [_fig(n, "people", "it first added")])
    return out


def _row_imessage(row, ctx):
    n_people = ctx["cov"].get("imessage", 0)
    n_msgs = ctx["msgs"].get("imessage", 0)
    out = _helper_copy_row(
        row, ctx, importers.HELPER_IMESSAGE,
        bool(n_people or n_msgs or _synced(ctx, "imessage")),
        off_takes="Three files you copy in Finder into the import folder, or "
                  "nothing at all once automatic copying is set up.",
        off_cost="file",
        manual="With automatic copying set up, the three files are copied by "
               "themselves every couple of minutes, and there is nothing to paste.")
    _week(out, ctx, "imessage", n_msgs, [_fig(n_people, "people", "with history")])
    return out


def _row_calendar_local(row, ctx):
    in_use = _synced(ctx, "calendar_local")
    out = _base(row, ctx)
    spec = ctx["specs"].get("calendar_local") or {}
    copying = _copying(ctx, importers.HELPER_CALENDAR)
    if copying:
        out["takes"], out["cost"] = ("Nothing. Automatic copying keeps it up to date.",
                                     _cost("free"))
    elif _helper_available(ctx):
        out["takes"] = "Automatic copying, once macOS lets it read your calendars."
        out["cost"] = _cost("permission")
    else:
        out["takes"] = ("This copy of the app has no sync helper. Use a calendar "
                        "file or a calendar link instead.")
        out["cost"] = _cost("none")
        out["state"] = {"tone": "na", "says": "Not in this copy of the app"}
    # Not "below": rows sort by cost, and the link rows (an address) come ABOVE
    # this one in exactly the states where this sentence shows.
    out["detail"]["manual"] = ("Without automatic copying, save your calendar to a "
                               "file (in Calendar, File, then Export) and use the "
                               "Calendar file row, or paste a calendar's address into "
                               "the Google or iCloud Calendar link row.")
    # NC-216. Import and Preview were offered with nothing for them to read:
    # this source only ever reads the copy automatic copying makes.
    out["detail"]["acts"] = ([_import_act(spec, "Import now")] if copying else
                             [{"kind": "row", "row": "helper",
                               "label": "Set up automatic copying"}]
                             if _helper_available(ctx) else [])
    if in_use and out["state"]["tone"] == "na":
        # Imported once, by a copy of the app that had the helper. What came in
        # is still here; nothing in this copy can refresh it, and a green
        # "working" dot would say otherwise.
        out["in_use"] = True
    elif in_use:
        _running(out, ctx)
    _calendar_have(out, ctx)
    return out


def _calendar_have(out, ctx):
    """The people you met, on every calendar row that is in use.

    All four calendars write through one importer under one provenance token
    (api_provenance: "the four share the count"), so which feed a meeting came
    on is not recorded. The figure is therefore the same on each row in use, and
    says so, rather than landing on one row and implying the others added none.
    A calendar row not in use loses it again in `_finish`, with every other row."""
    met = ctx["scov"].get("meetings", 0)
    if met:
        _say_have(out, [_fig(met, "people", "you met, from all your calendars together")])


FDA_STATES = {
    # state -> (tone, what the row says). Decided by api_fda; only worded here.
    "unavailable": ("na", "Not in this copy of the app"),
    "not_installed": ("off", ""),
    "no_report": ("blocked", "Set up, but it has not reported back yet"),
    "blocked": ("blocked", "macOS is refusing it"),
    "outdated": ("behind", "An older copy is running; set it up again"),
    "absent": ("ok", "Running, with nothing on this Mac to copy"),
    "quiet": ("behind", "Set up, but it has not run lately"),
    "ok": ("ok", "Running every couple of minutes"),
}


def _row_helper(row, ctx):
    out = _base(row, ctx)
    fda = ctx["fda"]
    state = fda.get("state", "unavailable")
    tone, says = FDA_STATES.get(state, ("off", ""))
    copies = list(fda.get("copies") or importers.HELPER_SOURCE_LABELS)
    ok = [lbl for lbl in copies if lbl in set(fda.get("ok") or [])]
    blocked = list(fda.get("blocked") or [])
    if state == "blocked" and blocked:
        says = "macOS is refusing it for " + ", ".join(blocked)
    if state == "outdated" and fda.get("newer_in_app") and not fda.get("missing"):
        # NC-223: every source copies, but a newer helper came with the app. The
        # cost goes in the sentence, because it is the reason to decide.
        says = ("A newer copy came with this version of the app. Setting it up "
                "again means giving it Full Disk Access again")
    ago = fda.get("ran_ago_seconds")
    if state == "quiet" and isinstance(ago, (int, float)):
        # NC-215: a time since, then what normal looks like.
        says = (f"It last reported {_for(ago)} ago. It normally reports every "
                "couple of minutes")
    out["state"] = {"tone": tone, "says": says}
    out["in_use"] = state not in ("unavailable", "not_installed")
    if state == "unavailable":
        out["takes"], out["cost"] = ("This copy of the app was built without it.",
                                     _cost("none"))
    elif state in ("ok", "absent"):
        out["takes"], out["cost"] = "Nothing more. It runs by itself.", _cost("free")
    else:
        out["takes"] = ("Installing one small program, then switching on one macOS "
                        "permission for it by hand in System Settings (Full Disk "
                        "Access). This app itself never gets that permission.")
        out["cost"] = _cost("permission")
    out["have"]["says"] = (f"Copies {len(ok)} of {len(copies)}: " + ", ".join(ok)
                           if ok else "")
    out["detail"]["runs"] = "Copies every couple of minutes, with no internet access"
    out["detail"]["manual"] = ("Without it, every source it copies can be handed "
                               "over by hand instead. Each of those rows says how.")
    out["detail"]["acts"] = [{"kind": "helper", "state": state,
                              "targets": fda.get("targets") or {},
                              "waits": fda.get("waits") or {}}]
    out["detail"]["provenance"] = ""
    return out


def _row_email(row, ctx):
    out = _base(row, ctx)
    spec = ctx["specs"].get("email") or {}
    out["takes"] = ("A mailbox file (.mbox) you export from Mail, or from Google's "
                    "download of your data.")
    out["cost"] = _cost("file")
    out["detail"]["acts"] = [_import_act(spec)] + _opens(spec)
    if _synced(ctx, "email"):
        _running(out, ctx)
    _week(out, ctx, "email", ctx["msgs"].get("email", 0),
          [_fig(ctx["cov"].get("email", 0), "people", "with email, from this and Gmail together")])
    return out


def _row_calendar_file(row, ctx):
    out = _base(row, ctx)
    spec = ctx["specs"].get("calendar") or {}
    out["takes"] = "A calendar file (.ics) you save from any calendar app."
    out["cost"] = _cost("file")
    out["detail"]["acts"] = [_import_act(spec)] + _opens(spec)
    if _synced(ctx, "calendar"):
        _running(out, ctx)
    _calendar_have(out, ctx)
    return out


def _row_calendar_link(row, ctx, env: str, what: str, fetch_on: bool):
    out = _base(row, ctx)
    key = row["sources"][0]
    spec = ctx["specs"].get(key) or {}
    box = _secret(env, f"{what} address", "https://…")
    # NC-215. It named the address and not the second yes, so a pasted link
    # sat saved and silent with nothing on the row to say why.
    service = "Google" if key == "calendar_google" else "Apple"
    out["takes"] = (f"The calendar's {what.lower()} address, pasted here once, and "
                    f"your yes to this app fetching it from {service} once a day.")
    out["cost"] = _cost("address")
    out["detail"]["acts"] = [box] + ([_import_act(spec)] if box["set"] else []) \
        + _opens(spec)
    if box["set"]:
        out["in_use"] = True
        if not fetch_on:
            # NC-215. The address alone does nothing: downloading is a second
            # yes, kept off by default because it is a request to the internet.
            # It was a grey line pointing at another page, with no button here,
            # and it never reached Needs you. The switch is on the row now, and
            # the same switch is in Settings; both write the one setting.
            out["state"] = {"tone": "ready",
                            "says": "Saved. Nothing downloads it until you switch "
                                    "downloading on"}
            out["detail"]["acts"] = [box, _switch_act(
                "NC_ICS_FETCH", "Download linked calendars", False)] + _opens(spec)
        else:
            out["state"] = _ok(out, ctx)
        _calendar_have(out, ctx)
    return out


def _row_linkedin_connections(row, ctx):
    out = _base(row, ctx)
    spec = ctx["specs"].get("linkedin") or {}
    li = ctx["li"] or {}
    n = int(((li.get("download") or {}).get("contacts_from_linkedin")) or 0)
    out["takes"] = ("Ask LinkedIn for a copy of your data, then wait a day or two "
                    "for the email with the file.")
    out["cost"] = _cost("wait")
    out["detail"]["runs"] = "Only when you import a new export"
    # Its own words rather than the registry's: the registry text is one card for
    # both LinkedIn routes and sends you to Settings for the token, which now
    # sits on the messages row below.
    out["detail"]["help"] = (
        "1. On LinkedIn, open **Settings & Privacy**, then **Data privacy**, then "
        "**Get a copy of your data**.\n"
        "2. Choose **Connections** only. That is the quickest file to arrive.\n"
        "3. LinkedIn emails you when it is ready, usually within a day or two. "
        "Download it and unzip it.\n"
        "4. Choose **Connections.csv** here and press **Import**. If it is in "
        "Downloads, it also turns up under Needs you on its own.")
    out["detail"]["acts"] = [dict(_import_act(spec), pick="csv", needs_path=True)]
    # The token lane's blocker belongs to the messages row, not this one.
    out["blocked"] = ""
    wait = ctx.get("li_wait") or {}
    if wait.get("waiting"):
        # NC-319. Asked for in setup, and not here yet: the row says so rather
        # than offering the request again as if nothing had happened.
        out["state"] = {"tone": "off", "says": wait.get("row_says", "")}
    if n:
        _running(out, ctx, "Imported from the file you exported")
    _say_have(out, [_fig(n, "people", "from the export")])
    return out


def _row_linkedin_messages(row, ctx):
    out = _base(row, ctx)
    li = ctx["li"] or {}
    ap = li.get("api") or {}
    tok = ap.get("token") or {}
    n_msgs, n_people = int(ap.get("messages") or 0), int(ap.get("people_with_messages") or 0)
    box = _secret("LINKEDIN_TOKEN", "LinkedIn token", "AQV…")
    out["takes"] = ("A LinkedIn developer app of your own, approved for LinkedIn's "
                    "copy-of-your-data product, then a token from it: a long code "
                    "you paste here. About 30 seconds once the app exists. It lasts "
                    "about a year.")
    out["cost"] = _cost("secret")
    out["detail"]["help"] = (
        "You need a LinkedIn developer app of your own that LinkedIn has "
        "approved for its copy-of-your-data product (Member Data Portability). "
        "Making one is a one-time step on LinkedIn's developer site.\n\n"
        "1. Open LinkedIn's token page with the button here.\n"
        "2. Pick your app. Tick the permission that lets the token read your own "
        "data (LinkedIn names it `r_dma_portability_self_serve`), and press "
        "**Request token**. LinkedIn then asks whether the app may read your "
        "data: press **Allow**. Only you can do this step.\n"
        "3. Copy the token with LinkedIn's copy button, paste it here and press "
        "**Connect**. A whole token is about 500 characters and starts with AQV. "
        "A much shorter one was cut off when you copied it: it saves, then stops "
        "working days later, so copy it again.\n\n"
        # NC-236: these three sentences were on the old Settings page's token
        # box (NC-073) and on the old LinkedIn panel, and went with them.
        "A token lasts about a year. Removing it stops LinkedIn message "
        # NC-238, from the old LinkedIn panel.
        "collection; messages already saved stay. A token from a developer app "
        "LinkedIn has not approved for that product lasts only 60 days, so if "
        "yours stops after two months, that is why. To have this row say the day "
        # NC-371: this sent people to edit the settings file by hand. The two
        # boxes are on the row now, once a token is connected.
        "it runs out, paste your app's client ID and client secret into the two "
        "boxes that appear here once the token is connected. LinkedIn shows both "
        "on your app's **Auth** page.\n\n"
        "LinkedIn only shows a recent stretch of messages, so each import tops up "
        "what is saved. Nothing saved earlier is ever removed.")
    acts: List[Dict[str, Any]] = [box, {"kind": "open", "target": "linkedin",
                                         "label": "Open LinkedIn's token page"}]
    if box["set"]:
        acts.append(_import_act(ctx["specs"].get("linkedin") or {"key": "linkedin"},
                                "Import"))
        # NC-371. The app's own credentials, which only LinkedIn's expiry check
        # needs (linkedin_token.credentials reads them at call time). Shown only
        # with a token, because without one there is nothing to check; the secret
        # is a credential box, masked to its last four like the token.
        acts.append(dict(_text("LINKEDIN_CLIENT_ID", "LinkedIn client ID",
                               "From your app's Auth page"), name="LinkedIn client ID"))
        acts.append(_secret("LINKEDIN_CLIENT_SECRET", "LinkedIn client secret",
                            "From your app's Auth page"))
    out["detail"]["acts"] = acts
    in_use = bool(n_msgs or box["set"])
    if in_use:
        out["in_use"] = True
        if out["blocked"]:
            out["state"] = {"tone": "blocked", "says": _sentence(out["blocked"])}
        elif tok.get("state") == "dead" or li.get("state") == "token_dead":
            # NC-068's case: a token LinkedIn has stopped accepting is no token,
            # and without this branch it read as working.
            out["state"] = {"tone": "blocked",
                            "says": "LinkedIn no longer accepts this token. Make a new "
                                    "one and press Replace"}
        elif tok.get("state") == "expiring":
            days = int(tok.get("days_left") or 0)
            out["state"] = {"tone": "behind",
                            "says": f"The token runs out in {_n(days, 'day', 'days')}"}
        elif box["set"] and not n_msgs and not _synced(ctx, "linkedin"):
            # NC-216. After Connect the row said "Checks every 24 hours on its
            # own", and nothing said whether to press Import or wait a day.
            out["state"] = {"tone": "ok",
                            "says": "Connected. Press Import to fetch your messages "
                                    "now. After that it checks every 24 hours by "
                                    "itself"}
        elif li.get("state") == "api_silent":
            age = ap.get("newest_message_age_days")
            out["state"] = {"tone": "behind",
                            "says": (f"No LinkedIn message has arrived for "
                                     f"{_for(float(age) * 86400)}. The token is the "
                                     "most likely cause"
                                     if isinstance(age, (int, float))
                                     else "No LinkedIn messages have arrived in a while")}
        else:
            out["state"] = _ok(out, ctx)
        # NC-238. The old LinkedIn panel said when the newest message arrived
        # and when the app last asked: the two facts that tell a quiet token
        # from a quiet inbox.
        age = ap.get("newest_message_age_days")
        out["detail"]["now"].append(
            f"The newest LinkedIn message saved is from {_for(float(age) * 86400)} ago."
            if isinstance(age, (int, float)) else "No LinkedIn message is saved yet.")
        tried = ap.get("last_attempt_age_hours")
        out["detail"]["now"].append(
            f"The app last asked LinkedIn for new messages {_for(float(tried) * 3600)} ago."
            if isinstance(tried, (int, float)) else "The app has not asked LinkedIn yet.")
        if box["set"] and (tok.get("state") == "unchecked" or not tok.get("configured")):
            # NC-371. Why there is no expiry date, and what brings one: the
            # check runs on the background loop, not when a box is saved.
            import linkedin_token
            hours = linkedin_token.CHECK_EVERY_HOURS
            out["detail"]["now"].append(
                "The day the token runs out is not known yet. That needs your app's "
                "client ID and client secret in the two boxes below; once both are "
                f"saved, the app checks within {_n(hours, 'hour', 'hours')}.")
    else:
        out["blocked"] = ""   # no token on a LinkedIn nobody uses is an offer
    _week(out, ctx, "linkedin", n_msgs, [_fig(n_people, "people", "you messaged")])
    return out


def _row_gmail(row, ctx):
    out = _base(row, ctx)
    gm = ctx["gm"] or {}
    accounts = gm.get("accounts") or []
    out["takes"] = ("About five minutes on Google's developer site making a key file "
                    "of your own, then a Google sign-in. The Email row gets much of "
                    "the same mail from one exported file.")
    out["cost"] = _cost("sign_in")
    out["detail"]["acts"] = [{"kind": "gmail"}]
    if accounts:
        # NC-236. The old Settings page had the whole list as one box, which was
        # the only way to remove an address or name a differently called key
        # file (you@gmail.com:file.json). The setup above can only add one.
        out["detail"]["acts"].append(dict(_text(
            "GMAIL_ACCOUNTS", "Every address this app reads, separated by commas. "
            "Delete one to stop reading it", "you@gmail.com"), name="Gmail addresses"))
        out["in_use"] = True
        if out["blocked"]:
            out["state"] = {"tone": "blocked", "says": _sentence(out["blocked"])}
        elif gm.get("needs_file"):
            out["state"] = {"tone": "blocked",
                            "says": "Needs the sign-in file from Google"}
        elif gm.get("needs_sign_in"):
            # GmailSetup asks for this too (gmail.js). Without
            # it, a sign-in that had run out read as working.
            who = ", ".join(gm["needs_sign_in"])
            out["state"] = {"tone": "blocked", "says": f"Sign in to Google again for {who}"}
        else:
            out["state"] = _ok(out, ctx)
    ready = len(gm.get("ready") or [])
    out["have"]["says"] = _n(ready, "account connected", "accounts connected") if ready else ""
    return out


def _row_google_photos(row, ctx):
    out = _base(row, ctx)
    spec = ctx["specs"].get("google_photos") or {}
    n = ctx["photos"].get("google", 0)
    out["takes"] = ("Gmail set up first, then one more Google sign-in, for your "
                    "contacts only.")
    out["cost"] = _cost("sign_in")
    gmail_ready = bool((ctx["gm"] or {}).get("ready"))
    # NC-216. Import was offered before Gmail existed, and could only fail.
    out["detail"]["acts"] = ([_import_act(spec)] if gmail_ready else
                             [{"kind": "row", "row": "gmail", "label": "Set up Gmail first"}])
    if n or _synced(ctx, "google_photos"):
        _running(out, ctx)
    _say_have(out, [_fig(n, "photos")])
    return out


def _row_linkedin_facts(row, ctx):
    out = _base(row, ctx)
    spec = ctx["specs"].get("linkedin_facts") or {}
    li = ctx["li"] or {}
    cov = ((li.get("scraper") or {}).get("coverage")) or {}
    host = (os.environ.get("IMAC") or "").strip()
    # NC-317. It said the slow reading kept the account "never flagged". It does
    # not: automated profile reading breaks LinkedIn's User Agreement, and the
    # account can be restricted or banned (README says the same).
    out["takes"] = ("A second Mac you set up to read LinkedIn profiles slowly. This "
                    "breaks LinkedIn's User Agreement, and the account it uses can be "
                    "restricted or banned.")
    out["cost"] = _cost("machine")
    out["detail"]["acts"] = [
        _text("IMAC", "The other Mac, as user@name", "you@other-mac"),
        _text("KEY", "Where its sign-in key file is on this Mac", "~/.ssh/…"),
    ] + ([_import_act(spec)] if host else []) + [
        # NC-234: this is the one source the switch affects,
        # so it moved here from Settings.
        _switch_act("SCRAPE_OVERRIDES_MANUAL", "A newer scraped title or company may replace one you typed",
                    os.environ.get("SCRAPE_OVERRIDES_MANUAL", "0").strip() == "1")]
    enriched = int(cov.get("enriched") or 0) + int(cov.get("due") or 0)
    scraper = li.get("scraper") or {}
    if host:
        _running(out, ctx)
        # NC-216. The old LinkedIn panel said both of these; the new row lost
        # them. api_linkedin already works them out.
        age = scraper.get("last_fact_age_hours")
        queued = int(scraper.get("queued") or 0)
        quiet_hours = scraper.get("quiet_hours")
        if (scraper.get("state") or {}).get("in_cooldown"):
            out["state"] = {"tone": "paused",
                            "says": "LinkedIn has paused this account for a while. "
                                    "Leave the other Mac alone until it lifts"}
            out["detail"]["acts"] = [a for a in out["detail"]["acts"] if a["kind"] != "import"]
        elif queued and isinstance(quiet_hours, (int, float)) and (
                age is None or age > quiet_hours):
            out["state"] = {"tone": "behind",
                            "says": (f"No new profiles for {_for((age or 0) * 3600)}, "
                                     f"with {_n(queued, 'person', 'people')} still waiting. "
                                     "The other Mac may be asleep or signed out of LinkedIn"
                                     if age is not None else
                                     f"No profiles have arrived yet, with "
                                     f"{_n(queued, 'person', 'people')} waiting")}
    if enriched:
        out["in_use"] = True
        if not host:
            out["state"] = {"tone": "off", "says": "No other Mac set on this one"}
    total = int(cov.get("total") or 0)
    if enriched:
        out["have"]["figures"] = [figures.figure(enriched, "people", "active",
                                                 of=total, side=figures.SIDE_STORED,
                                                 says="with profile facts")]
        out["have"]["says"] = f"{enriched:,} of {total:,} people with profile facts"
    # NC-238. The old LinkedIn panel split everyone five ways, and the last one
    # is the ceiling: a person with no profile link is someone the other Mac can
    # never reach, however long it runs.
    if (host or enriched) and total:
        out["detail"]["now"].append(
            f"Of {_n(total, 'person', 'people')}: {int(cov.get('enriched') or 0):,} "
            f"filled in, {int(cov.get('due') or 0):,} due to be read again, "
            f"{int(cov.get('queued') or 0):,} waiting their turn, "
            f"{int(cov.get('manual') or 0):,} kept as you typed them, and "
            f"{int(cov.get('no_url') or 0):,} with no LinkedIn profile link, which "
            "the other Mac can never reach.")
    pending = int(scraper.get("extras_pending") or 0)
    if pending:
        # The old panel's "Some scraped profiles have not been read yet". NC-372:
        # the step that reads them (enrich_li_extras.py) has a button now, run
        # through the ordinary import runner like Facts written by AI, and capped
        # by the same spend cap. It is billed per profile, so the sentence beside
        # it says what one press can cost, worked out from the script's own caps.
        said = (f"{_n(pending, 'profile', 'profiles')} the other Mac saved "
                f"{'has' if pending == 1 else 'have'} not yet been read for the small "
                "details worth a glance before you write, such as shared history and "
                "mutual connections.")
        key_set = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
        in_cooldown = bool((scraper.get("state") or {}).get("in_cooldown"))
        if host and not in_cooldown:
            import enrich_li_extras
            per = enrich_li_extras.most_per_profile_usd()
            batch = min(pending, api_import_cap())
            said += (f" Each press of Read shared history sends up to "
                     f"{_n(batch, 'profile', 'profiles')} to Anthropic. That costs your "
                     f"Anthropic account no more than about {_usd(per * batch)}, about "
                     f"{_usd(per)} a profile.")
            if key_set:
                # Before the switch, which belongs to the facts import above it.
                acts = out["detail"]["acts"]
                at = next((i for i, a in enumerate(acts) if a["kind"] == "switch"), len(acts))
                acts.insert(at, dict(_import_act(spec or {"key": "linkedin_facts"},
                                                 "Read shared history"),
                                     preview=False, form={"extras": True}))
            else:
                said += " It needs a key from Anthropic, added in Settings."
                out["detail"]["acts"].append({"kind": "settings", "row": "ai",
                                              "label": "Add the key in Settings"})
        out["detail"]["now"].append(said)
    return out


def _usd(dollars: float) -> str:
    """NC-372. A small amount of money the way a person says it: "9 US cents",
    "half a US cent", "2.40 US dollars"."""
    cents = dollars * 100
    if cents < 0.75:
        return "half a US cent" if cents >= 0.25 else "less than half a US cent"
    if cents < 99.5:
        n = max(1, round(cents))
        return f"{n} US {'cent' if n == 1 else 'cents'}"
    return f"{dollars:,.2f} US dollars"


def _row_facts_llm(row, ctx):
    out = _base(row, ctx)
    spec = ctx["specs"].get("facts_llm") or {}
    key_set = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    cap = api_import_cap()
    out["takes"] = (f"A key from Anthropic, the company that makes Claude, added in "
                    f"Settings. Anthropic charges your account for each run, and a "
                    f"run covers at most {_n(cap, 'person', 'people')}.")
    out["cost"] = _cost("money")
    out["detail"]["acts"] = ([_import_act(spec)] if key_set
                             else [{"kind": "settings", "row": "ai",
                                    "label": "Add the key in Settings"}])
    n = _prov(ctx, "facts_llm", "facts")
    if n or _synced(ctx, "facts_llm"):
        _running(out, ctx)
    if not key_set and out["in_use"]:
        out["state"] = {"tone": "off", "says": "No key from Anthropic in Settings"}
    _say_have(out, [_fig(n, "people", "with AI-written facts")])
    return out


def api_import_cap() -> int:
    """The most people one paid run covers — the enrichment script's own default.
    NC-364: settings.spend_cap() owns the reading now, beside the bounds of the
    Settings box that writes it; the LinkedIn shared-history button reads it too."""
    return settings.spend_cap()


def _row_chained(row, ctx):
    out = _base(row, ctx)
    out["takes"] = "Nothing. It runs by itself after an import brings something new."
    out["cost"] = _cost("free")
    # In use once it has run, not before: on an empty install nothing has been
    # worked out from anything, and a green "running" there would be the page's
    # first untrue sentence. It is not an offer either — there is nothing to
    # click — so `_next` never names it.
    out["in_use"] = _synced(ctx, row["key"])
    if not out["in_use"]:
        out["state"] = {"tone": "off", "says": "Starts by itself after your first import"}
    else:
        out["state"] = _ok(out, ctx)
    if row["key"] == "facts":
        _say_have(out, [_fig(_prov(ctx, "facts", "facts"), "people", "with facts")])
    return out


# Rows that run on their own after an import and have nothing to switch on —
# api_sources' own set, so the two cannot disagree about which they are.
CHAINED = api_sources.CHAINED

BUILDERS = {
    "whatsapp": _row_whatsapp,
    "contacts": _row_contacts,
    "imessage": _row_imessage,
    "calendar_local": _row_calendar_local,
    "helper": _row_helper,
    "email": _row_email,
    "calendar": _row_calendar_file,
    "calendar_google": lambda r, c: _row_calendar_link(
        r, c, "NC_ICS_GOOGLE_URL", "Private", os.environ.get("NC_ICS_FETCH", "0") == "1"),
    "calendar_icloud": lambda r, c: _row_calendar_link(
        r, c, "NC_ICS_ICLOUD_URL", "Public", os.environ.get("NC_ICS_FETCH", "0") == "1"),
    "linkedin_connections": _row_linkedin_connections,
    "linkedin_messages": _row_linkedin_messages,
    "gmail": _row_gmail,
    "google_photos": _row_google_photos,
    "linkedin_facts": _row_linkedin_facts,
    "facts_llm": _row_facts_llm,
    "engagement": _row_chained,
    "facts": _row_chained,
    "style": _row_chained,
}


# NC-231. Closeness, Facts from your messages and How you
# write are one thing to a person: work the app does by itself after an import,
# with nothing to switch on. They were three rows mixed in with the sources, and
# are now one, always last. Each is still built as before, then folded in.
FOLDED = ("engagement", "facts", "style")
WORKED_OUT = {"key": "worked_out", "label": "Worked out on this Mac",
              "sources": FOLDED,
              "gives": "How close you are to each person, facts read out of your "
                       "messages, and your writing voice, all redone after every "
                       "import."}
TONE_RANK = {"blocked": 0, "behind": 1, "paused": 2, "ready": 3, "ok": 4, "off": 5, "na": 6}


def _ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    return "just now" if seconds < 90 else f"{_for(seconds)} ago"


def _worked_out(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The one row the three automatic passes fold into."""
    used = [p for p in parts if p["in_use"]]
    worst = min(used, key=lambda p: TONE_RANK.get(p["state"]["tone"], 9)) if used else None
    ran = [p["detail"].get("last_run_seconds") for p in used
           if p["detail"].get("last_run_seconds") is not None]
    newest = min(ran) if ran else None
    last = max((p["detail"].get("last_run") or "" for p in used), default="")
    help_text = "\n\n".join(f"**{p['label']}.** {p['detail']['help']}"
                             for p in parts if p["detail"].get("help"))
    runs = parts[0]["detail"].get("runs", "") if parts else ""
    return {
        **{k: WORKED_OUT[k] for k in ("key", "label", "gives")},
        "sources": list(FOLDED),
        "have": {"says": f"Last worked out {_ago(newest)}" if newest is not None else "",
                 "figures": [f for p in parts for f in p["have"]["figures"]]},
        "takes": "Nothing. It runs by itself after an import brings something new.",
        "cost": _cost("free"),
        "state": (dict(worst["state"]) if worst and worst["state"]["tone"] != "ok"
                  else {"tone": "ok", "says": runs} if worst
                  else {"tone": "off", "says": "Starts by itself after your first import"}),
        "in_use": bool(used),
        "blocked": "",
        "detail": {
            "runs": runs, "last_run": last, "last_run_seconds": newest,
            "last_run_caveat": "", "help": help_text, "manual": "", "acts": [],
            "provenance": "",
            "parts": [{"key": p["key"], "label": p["label"], "in_use": p["in_use"],
                       "state": p["state"],
                       "last_run_seconds": p["detail"].get("last_run_seconds"),
                       "provenance": p["detail"].get("provenance", ""),
                       "have": p["have"]["says"]} for p in parts],
        },
    }


def _finish(out: Dict[str, Any]) -> Dict[str, Any]:
    """The one rule every row obeys last: a blocker on a row in use wins."""
    if out["in_use"] and out["blocked"] and out["state"]["tone"] != "blocked":
        out["state"] = {"tone": "blocked", "says": _sentence(out["blocked"])}
    if not out["in_use"]:
        out["blocked"] = ""
        # "What you have from it now" is a claim about a source in use. People
        # with email history on an Email row nobody switched on came from Gmail.
        out["have"] = {"says": "", "figures": []}
        if out["state"]["tone"] not in ("na",):
            out["state"] = {"tone": "off", "says": out["state"].get("says") or ""}
    return out


def _when(r: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """NC-233. A closed row in use said what switching it on
    takes, which had already been done, and nothing showed it ran today: the time was
    only inside the opened row, rounded to the day. It now says when."""
    if not r["in_use"]:
        return
    if r["key"] == "helper":
        s = (ctx["fda"] or {}).get("ran_ago_seconds")
        r["when"] = f"Last copied {_ago(float(s))}" if isinstance(s, (int, float)) else ""
        return
    s = r["detail"].get("last_run_seconds")
    r["when"] = f"Last import {_ago(s)}" if s is not None else "No import recorded yet"


def _fill_found_paths(ctx: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    """NC-216. The scan finds WhatsApp's database, and the row's Import still
    asked you to go and find it inside a hidden Library folder. A row's
    choose-a-file box now starts with the newest file the scan found for it;
    you can still choose another."""
    newest: Dict[str, str] = {}
    for f in sorted(ctx["found"] or [], key=lambda f: f.get("mtime") or ""):
        if f.get("path"):
            newest[f.get("source", "")] = f["path"]
    for r in rows:
        for act in r["detail"]["acts"]:
            if (act.get("kind") == "import" and act.get("needs_path")
                    and not act.get("path") and act.get("source") in newest):
                act["path"] = newest[act["source"]]


# NC-239. Why Apple Contacts goes before iMessage, from the old Data page and the
# setup wizard. Not a dependency: iMessage finds names on its own. But a person
# already here from the address book gets their messages on a full card.
CONTACTS_FIRST = ("Import Apple Contacts before iMessage if you can: people already "
                  "here then get their messages on a full card, with phone, email, "
                  "company and title, instead of arriving as bare names.")


def _contacts_first(rows: List[Dict[str, Any]]) -> None:
    by_key = {r["key"]: r for r in rows}
    contacts, imessage = by_key.get("contacts"), by_key.get("imessage")
    if contacts and imessage and not contacts["in_use"] and not imessage["in_use"]:
        imessage["detail"].setdefault("now", []).append(CONTACTS_FIRST)


def _rows(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [_finish(_safe(lambda r=r: BUILDERS[r["key"]](r, ctx), _base(r, ctx)))
            for r in ROWS]
    _fill_found_paths(ctx, rows)
    for r in rows:
        _when(r, ctx)
    _contacts_first(rows)
    parts = [r for r in rows if r["key"] in FOLDED]
    rows = [r for r in rows if r["key"] not in FOLDED]
    # NC-229: the rows you use first, then the rest cheapest
    # first; within one cost, the order above. A fresh install uses none, so it
    # reads exactly as before. The work the app does by itself comes last.
    rows.sort(key=lambda r: (not r["in_use"], r["cost"]["rank"], ORDER[r["key"]]))
    # NC-232: automatic copying leads, above the four sources
    # it makes free, unless this copy of the app has no helper at all.
    helper = next((r for r in rows if r["key"] == "helper"), None)
    if helper and helper["state"]["tone"] != "na":
        rows = [helper] + [r for r in rows if r is not helper]
    worked = _worked_out(sorted(parts, key=lambda r: ORDER[r["key"]]))
    if worked["in_use"]:
        worked["when"] = (f"Last run {_ago(worked['detail']['last_run_seconds'])}"
                          if worked["detail"]["last_run_seconds"] is not None else "")
    return rows + [worked]


# ── Needs you ─────────────────────────────────────────────────────────────────

def _behind(f: Dict[str, Any]) -> bool:
    return not f.get("live") and int(f.get("stale_days") or 0) >= BEHIND_DAYS


def _needs_you(f: Dict[str, Any]) -> bool:
    return not f.get("auto") or _behind(f)


def _found_holds(f: Dict[str, Any]) -> str:
    unit = FOUND_UNIT.get(f.get("source", ""))
    n = int(f.get("count") or 0)
    return _n(n, *unit) if unit and n > 0 else ""


def _imported_since_it_changed(ctx: Dict[str, Any], f: Dict[str, Any]) -> bool:
    """This source's own last import is at or after the file last changed — NC-208.

    Without this, "Needs you" kept asking for a file after you imported it: the
    old Data page dropped a finding only when a self-importing copy or a covering
    calendar was newer, and never looked at the source's own run. Exact times, not
    the finding's day: a file changed at six in the evening is newer than an
    import at ten that morning, and must still be asked for.

    Only for sources whose one way in is a file. A source that also runs on a
    clock (LinkedIn's token fetch, Gmail, the calendar links) stamps the same
    `import_synced_<key>` from the background, so for them a recent stamp says
    nothing about this file, and the finding is asked for as before.

    Scan mode only, like every finding: the scan has already touched this path."""
    source = f.get("source", "")
    if source in (getattr(api_import, "API_SOURCES", {}) or {}):
        return False
    last = str((ctx["sources"].get(source) or {}).get("last_run") or "")
    if not last:
        return False
    try:
        ran = datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
        changed = os.path.getmtime(f.get("path") or "")
    except (ValueError, OSError):
        return False
    return ran >= changed


MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


def _day(stamp: Any) -> str:
    """"12 September", with the year only when it is not this one."""
    try:
        d = datetime.fromisoformat(str(stamp or "")[:10])
    except ValueError:
        return "an unknown date"
    year = "" if d.year == datetime.now().year else f" {d.year}"
    return f"{d.day} {MONTHS[d.month - 1]}{year}"


def _where(path: str) -> str:
    """Which folder a found file is in, the way Finder names it."""
    home = os.path.expanduser("~")
    folder = os.path.dirname(path or "")
    for name, says in (("Downloads", "in Downloads"), ("Desktop", "on your Desktop"),
                       ("Documents", "in Documents")):
        top = os.path.join(home, name)
        if folder == top:
            return says
        if folder.startswith(top + os.sep):
            return f"in {os.path.basename(folder)}, inside {name}"
    return f"in the folder {os.path.basename(folder)}" if folder else "on this Mac"


def _file_needs(ctx: Dict[str, Any], rows: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The findings a person has to act on, one per source, freshest file first.

    The old Data page's rules, moved here unchanged: a file older than what
    already imports itself is not asked for, nor one a covering sibling has
    already brought in, and a second file for the same source rides along as an
    alternative instead of becoming a second row."""
    found = [f for f in (ctx["found"] or []) if f.get("source") not in PART_OF]
    auto = {}
    for f in found:
        if not _needs_you(f):
            prev = auto.get(f["source"])
            if not prev or (f.get("mtime") or "") > (prev.get("mtime") or ""):
                auto[f["source"]] = f

    def covered_since(source: str) -> str:
        return max([(ctx["sources"].get(k) or {}).get("last_run", "") or ""
                    for k in COVERED_BY.get(source, ())] or [""])

    cands = []
    for f in found:
        if not _needs_you(f):
            continue
        cover = auto.get(f["source"])
        if cover and (f.get("mtime") or "") <= (cover.get("mtime") or ""):
            continue
        sib = covered_since(f["source"])[:10]
        if sib and sib >= str(f.get("mtime") or "")[:10]:
            continue
        if _imported_since_it_changed(ctx, f):
            continue
        # A copy going stale because macOS refuses the helper has nothing to hand
        # over by hand: the fix is the one grant, and the helper's own row is
        # already in this list saying so (the old Data page's `finding.blocked`).
        if f.get("blocked") and _behind(f):
            continue
        cands.append(f)
    cands.sort(key=lambda f: (f.get("mtime") or "", int(f.get("count") or 0)), reverse=True)

    out, claimed = [], set()
    for f in cands:
        if f["source"] in claimed:
            continue
        claimed.add(f["source"])
        row_key = str(ROW_OF.get(f["source"]) or f["source"])
        label = (rows.get(row_key) or {}).get("label") or f.get("label", "")
        behind = _behind(f) and f.get("auto")
        holds = _found_holds(f)
        if behind:
            says = (f"Your {label} copy is {_n(f['stale_days'], 'day', 'days')} old "
                    "and nothing is refreshing it.")
            # NC-215. The row below must not say "Kept current by the sync
            # helper", green, under a need saying the same copy is stale: two
            # sentences on one screen that cannot both be true. The row takes
            # the need's word.
            row = rows.get(row_key)
            if row and row["in_use"] and row["state"]["tone"] in ("ok", "paused"):
                row["state"] = {"tone": "behind",
                                "says": f"The copy has not changed for "
                                        f"{_n(f['stale_days'], 'day', 'days')}"}
        else:
            # NC-217. It must not read "…file is on this Mac from <date>. Nothing
            # imports it by itself.": a machine date, no folder to look in, and
            # no step.
            says = (f"{_a(label).capitalize()} {label} file from "
                    f"{_day(f.get('mtime'))} is {_where(f.get('path', ''))}"
                    + (f", with {holds}" if holds else "") + ". Press Import to bring it in.")
        spec = ctx["specs"].get(f["source"]) or {"key": f["source"]}
        act = dict(_import_act(spec), path=f.get("path", ""), needs_path=False)
        out.append({
            "row": row_key, "source": f["source"], "tone": "behind" if behind else "ready",
            "says": says, "cost": "file",
            "acts": [act] + [dict(_import_act(spec), path=a.get("path", ""), needs_path=False,
                                  verb="Import this one instead")
                             for a in cands if a["source"] == f["source"] and a is not f]
            # NC-239. A copy that fell behind is fixed by pasting a fresher one,
            # and the old page offered the folders for that right on this line.
            # They had moved one click away, inside the row.
            + (_opens(spec) if behind else []),
            "record": _record(rows.get(row_key)),
        })
    return out


def _record(row: Optional[Dict[str, Any]]) -> str:
    """NC-239. The source whose "What it added" a need offers, or "": the old
    page offered it from a source's Needs you line, and the new one only inside
    the row. Its own field rather than one more act, so the acts stay the fix."""
    return ((row or {}).get("detail") or {}).get("provenance") or ""


# Rows whose own controls ARE the fix, so a need for one carries them rather
# than only a button that scrolls down to the row (NC-216).
FIX_IN_THE_NEED = ("linkedin_messages", "calendar_google", "calendar_icloud")

# The sources that decide which side of a conversation is yours by your name or
# email address (api_instance.identity_status names them).
NEEDS_YOUR_NAME = ("whatsapp", "imessage", "email", "gmail", "calendar_local",
                   "calendar", "calendar_google", "calendar_icloud")


def _needs(ctx: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key = {r["key"]: r for r in rows}
    out: List[Dict[str, Any]] = []
    # 1. Rows in use that cannot collect: a refused permission, a missing or
    #    dead credential, a sign-in waiting, a setting that went empty, or a yes
    #    it is waiting for.
    for r in rows:
        if r["in_use"] and r["state"]["tone"] in ("blocked", "behind", "ready"):
            acts = [{"kind": "row", "row": r["key"], "label": f"Open {r['label']}"}]
            if r["key"] in FIX_IN_THE_NEED:
                acts = [a for a in r["detail"]["acts"]
                        if a["kind"] in ("open", "secret", "switch")] + acts
            if r["key"] == "helper" and ctx["fda"].get("state") == "blocked":
                acts = [{"kind": "open", "target": (ctx["fda"].get("targets") or {})
                         .get("settings", "fda_settings"),
                         "label": "Open Full Disk Access settings"}] + acts
            out.append({"row": r["key"], "source": (r["sources"] or [""])[0],
                        "tone": r["state"]["tone"],
                        "says": f"{r['label']}: {r['state']['says']}",
                        "cost": r["cost"]["kind"], "acts": acts, "record": _record(r)})
    # 2. Automatic importing switched off, with sources that rely on it. Each
    #    row already says "Paused", but the page's top line said "Nothing. Every
    #    source is either current or switched off", which was false: nothing was
    #    importing at all (NC-215).
    paused = [r for r in rows if r["in_use"] and r["state"]["tone"] == "paused"
              and r["key"] != WORKED_OUT["key"]]   # not a source of its own
    if not ctx["autosync"] and paused:
        out.append({"row": "", "source": "", "tone": "paused", "cost": "free",
                    "says": (f"Automatic importing is off, so "
                             f"{_n(len(paused), 'source brings', 'sources bring')} "
                             "in nothing new until you press Import on "
                             f"{'it' if len(paused) == 1 else 'each one'}."),
                    "acts": [_switch_act("NC_AUTOSYNC", "Import automatically", False)]})
    # 3. Your name and email, once a source that needs them is in use. Without
    #    them the importers cannot tell your side of a conversation from anyone
    #    else's; only the Settings page said so (NC-216).
    ident = _safe(api_instance.identity_status, {"ok": True})
    if not ident.get("ok") and any(by_key.get(k, {}).get("in_use") for k in NEEDS_YOUR_NAME):
        out.append({"row": "", "source": "", "tone": "ready", "cost": "free",
                    "says": ident.get("message") or "Your name and email are not set.",
                    "acts": [{"kind": "settings", "row": "identity",
                              "label": "Set your name and email"}]})
    # 4. Files on this Mac nothing will import.
    out.extend(_file_needs(ctx, by_key))
    # Cheapest to fix first, and a refusal before a nudge within one cost.
    tone_rank = {"blocked": 0, "behind": 1, "paused": 2, "ready": 3}
    out.sort(key=lambda n: (COSTS.get(n["cost"], (9,))[0], tone_rank.get(n["tone"], 3)))
    return out


def _newest(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The in-use row whose last import is most recent, so "Nothing needs you"
    can say what it is based on rather than only assert it (NC-215)."""
    best = None
    for r in rows:
        at = r["detail"].get("last_run") or ""
        # The work the app does by itself is not an import.
        if r["key"] == WORKED_OUT["key"]:
            continue
        if r["in_use"] and at and (best is None or at > best["at"]):
            best = {"row": r["key"], "label": r["label"], "at": at,
                    "seconds": r["detail"].get("last_run_seconds")}
    return best


def _next(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The cheapest thing not yet switched on — what an empty install is told.
    By cost, not by place: the list leads with automatic copying (NC-232)."""
    offers = [r for r in rows
              if not (r["key"] in CHAINED or r["key"] == WORKED_OUT["key"] or r.get("absent"))
              and not r["in_use"] and r["state"]["tone"] != "na"]
    if not offers:
        return None
    r = min(offers, key=lambda r: (r["cost"]["rank"], ORDER[r["key"]]))
    out = {"row": r["key"], "label": r["label"], "takes": r["takes"], "why": ""}
    # NC-239. A fresh install used to be told to import Contacts before
    # iMessage, for fuller records; it was then told only "the cheapest", which
    # happened to be Contacts. The reason is back when it applies.
    imessage = next((x for x in rows if x["key"] == "imessage"), None)
    if r["key"] == "contacts" and imessage and not imessage["in_use"]:
        out["why"] = CONTACTS_FIRST
    return out


# ── This install ──────────────────────────────────────────────────────────────

# What each file in the data folder is, by the name a person would give it. The
# "On disk" line used to list the file names themselves: archive.db, photos,
# chunk_vectors.npz (NC-217).
PART_NAMES = (("archive.db", "Saved messages"), ("crm.db", "People and notes"),
              ("photos", "Photos"), ("chunk_vectors.npz", "Message search index"),
              ("crm_vectors.npz", "People search index"),
              ("linkedin_messages.db", "LinkedIn messages"),
              ("li_raw", "LinkedIn downloads"), ("calendar", "Calendar copies"))


def _part_name(file_name: str) -> str:
    for start, name in PART_NAMES:
        # crm.db-wal and crm.db-shm are the same database, still being written.
        if file_name == start or file_name.startswith(start + "-"):
            return name
    return "Other files"


def _install(conn: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
    total = int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] or 0)
    people = api_instance._people_ledger(conn, total)
    # The rows already counted these; a second pass over the whole archive was
    # the slowest query on the page.
    by_channel = {str(k or ""): int(v or 0) for k, v in (ctx["msgs"] or {}).items()}
    disk_bytes, disk_parts = _safe(api_instance.data_dir_bytes, (0, {}))
    named: Dict[str, int] = {}
    for file_name, n in disk_parts.items():
        named[_part_name(file_name)] = named.get(_part_name(file_name), 0) + int(n)
    return {
        "people": people,
        "messages": {"figure": _msgs(sum(by_channel.values())),
                     "by_channel": by_channel},
        "disk": {"bytes": disk_bytes, "parts": disk_parts,
                 "named": sorted(({"name": k, "bytes": v} for k, v in named.items()),
                                 key=lambda p: -p["bytes"])},
        "empty": total == 0,
        # NC-228: the duplicates finder lives in This install
        # once the old page goes. One pass over contacts and identifiers.
        "duplicates": _safe(lambda: len(db.find_duplicate_groups(conn)), None),
    }


# ── Waiting for LinkedIn's file (NC-319) ──────────────────────────────────────
# LinkedIn emails the connections file a day or two after you ask for it, so a
# person who asks during setup finishes setup with nobody from LinkedIn and
# nothing to say why. Setup records the moment they asked; the People page and
# the LinkedIn connections row say "waiting" until a LinkedIn file has brought
# someone in. Kept in crm.db's `state`, like the first-run flag beside it.
LI_ASKED_KEY = "linkedin_export_asked_at"
# After this many days the email has most likely come and gone, or never came:
# the sentence turns from "wait" to "look for the email, or ask again".
LI_USUAL_DAYS = 3


def linkedin_wait(conn: Any) -> Dict[str, Any]:
    """Whether this install is waiting for the LinkedIn file it asked for."""
    asked = str(db.state_get(conn, LI_ASKED_KEY, "") or "")
    if not asked:
        return {"waiting": False, "asked_at": "", "says": ""}
    arrived = conn.execute(
        "SELECT 1 FROM contacts WHERE source = 'linkedin' LIMIT 1").fetchone()
    if arrived:
        return {"waiting": False, "asked_at": asked, "says": ""}
    ago = _seconds_since(asked)
    when = f"{_for(ago)} ago" if ago is not None and ago >= 90 else "just now"
    late = ago is not None and ago >= LI_USUAL_DAYS * 86400
    row_says = (f"Waiting for your LinkedIn file. You asked for it {when}. "
                + ("It usually arrives by email within a day or two, so look for "
                   "LinkedIn's email (and your spam folder), or ask again."
                   if late else
                   "LinkedIn usually emails it within a day or two."))
    # `says` is for anywhere but the row itself, which is where it sends you.
    says = (row_says + " When it comes, choose Connections.csv on the LinkedIn "
            "connections row of the Data page.")
    return {"waiting": True, "asked_at": asked, "seconds": ago, "late": late,
            "says": says, "row_says": row_says}


def handle_linkedin_wait(conn: Any) -> Dict[str, Any]:
    """GET /api/data/linkedin-wait — cheap enough for the People page to ask
    on every visit: one state read and one indexed lookup."""
    return linkedin_wait(conn)


def handle_linkedin_asked(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/data/linkedin-asked {asked} — setup records that the person
    asked LinkedIn for their file (true), or takes it back (false)."""
    if (body or {}).get("asked", True):
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.state_set(conn, LI_ASKED_KEY, stamp)
    else:
        conn.execute("DELETE FROM state WHERE key = ?", (LI_ASKED_KEY,))
    conn.commit()
    return linkedin_wait(conn)


def handle_data_ledger(conn: Any, query: Dict[str, List[str]]) -> Dict[str, Any]:
    """GET /api/data/ledger[?scan=1] — every row, what needs you, this install."""
    scan = (query.get("scan", [""])[0] or "") == "1"
    ctx = _context(conn, scan)
    rows = _rows(ctx)
    return {
        "rows": rows,
        "needs": _needs(ctx, rows),
        "next": _next(rows),
        "in_use": sum(1 for r in rows if r["in_use"] and r["key"] != WORKED_OUT["key"]),
        "newest": _newest(rows),
        "install": _install(conn, ctx),
        "scanned": scan,
        "scan_failed": bool(ctx.get("scan_failed")),
        # The server's own ceiling on one import, so the page waits that long
        # before calling an import that is still running a failure (NC-176).
        "import_timeout_sec": _safe(api_import.import_timeout_sec, None),
        "population": "active",
        "population_says": figures.population_says("active"),
    }
