#!/usr/bin/env python3
"""
Single registry of every import/enrich source: what it needs, how to explain it to a
human in the Import wizard, and how to build the subprocess argv for it.

This does not reimplement any importer or enricher — it only unifies how `app.py`
describes and invokes the existing scripts (contacts_import.py, linkedin_import.py,
seed_from_csv.py, imessage_import.py, whatsapp_import.py, email_import.py,
telegram_import.py, meta_import.py x2, gmail_import.py, enrich_facts.py x2,
merge_scraped_facts.py) so the front-end never hardcodes a source list.
"""

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Callable, Dict, List, Literal, Optional

import json
import os
import re
import sys

import config  # resolves DATA_DIR (packaged .app vs repo checkout)


class ImporterError(Exception):
    """Bad input for building a command — carries an HTTP status like ApiError."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ImporterSpec:
    key: str
    label: str
    category: Literal["import", "enrich"]
    kind: Literal["file", "folder", "none"]
    pick_kind: Optional[str]          # key into PICK_SCRIPTS in api_import.py, or None
    export_help: str                  # steps shown in the Import wizard
    build_cmd: Callable[[Dict[str, Any]], List[str]]   # (form dict) -> argv
    # Can this source show you what it WOULD change without changing it? Only
    # true where the underlying script honors --dry-run end to end; a
    # silently-ignored preview flag would be worse than none.
    supports_dry_run: bool = False
    # Can this source change crm.db / archive.db AT ALL? A different question
    # from supports_dry_run: that one asks "can it preview", this one asks "can
    # it ever write". True for every source in the registry today — including the
    # three calendar ones, since ics_import.apply() landed.
    #
    # It is load-bearing rather than documentation: api_import.handle_import_run
    # skips the post-run bookkeeping — the engagement refresh, the chained facts
    # pass, and the "last synced" stamp the Data card renders — for a source that
    # writes nothing. Stamping a preview "synced 2 minutes ago" would tell the
    # user data landed when none did, which is the same lie as a receipt reading
    # "Added 12" after a run that added nothing.
    #
    # A field comment that describes a value the field no longer holds is worse
    # than none: a stale "False today for the three calendar sources" is the
    # reason three cards told users nothing was written while the button wrote
    # (NC-005).
    writes: bool = True
    # A setting this source cannot work without, or "" if it needs none (NC-006).
    #
    # The row still renders — hiding a capability is how a user concludes it does
    # not exist — but it renders as NOT SET UP, naming the setting, instead of
    # offering a button that fails. `linkedin_facts` is why this exists. There is
    # no default for IMAC: with one, the button would be live on every install and
    # its error would name a computer the user had never heard of, and nothing
    # could tell "configured" from "never touched". So the value is a
    # MANAGED_KEYS setting — which also means a user can change scraper machines
    # without editing a script.
    # The settings key this source cannot run without, or "" when there is
    # genuinely nothing to configure. NOT documentation: list_sources() turns
    # it into `configured`, and an empty string means "configured", so a
    # source that forgets to declare its setting claims to be ready forever.
    #
    # NC-144 is what that costs. Gmail declared nothing, so the Data page
    # showed it as a working source with a date beside it while GMAIL_ACCOUNTS
    # was empty — and the date was real, it was just the last time it worked
    # rather than evidence it still could. No email arriving looked exactly like
    # a quiet inbox. NC-116 was the same omission on facts_llm, offering a paid
    # Claude run with no key set.
    #
    # One declaration out of seventeen was not a convention, it was an
    # accident. tests/test_sources_declare_what_they_need.py now requires
    # every spec to be classified, so a new source cannot quietly skip it.
    needs: str = ""


def _py() -> str:
    return sys.executable


def _require_path(form: Dict[str, Any], hint: str) -> str:
    path = str(form.get("path") or "").strip()
    if not path:
        raise ImporterError(f"pick {hint} first")
    return path


def _years(form: Dict[str, Any]) -> int:
    try:
        years = int(form.get("years", 3))
    except (TypeError, ValueError):
        years = 3
    return max(1, min(10, years))


# ── The two URL-fed calendars ───────────────────────────────────────────────
# Metadata only. NO fetching happens in this module: api_import.fetch_calendar_ics()
# owns the network call, because it is the only outbound request this app makes
# that isn't an Ask call and it belongs next to the endpoint that can refuse it.
# Each source names the settings key holding its (SECRET) URL and the local file
# its download lands in, so build_cmd stays pure argv construction.
CALENDAR_URL_SOURCES: Dict[str, Dict[str, str]] = {
    "calendar_google": {"env": "NC_ICS_GOOGLE_URL", "file": "google.ics",
                        "label": "Google Calendar"},
    "calendar_icloud": {"env": "NC_ICS_ICLOUD_URL", "file": "icloud.ics",
                        "label": "iCloud Calendar"},
}

# Where a downloaded calendar is kept. Deliberately a real folder under data/ and
# not a temp file: a snapshot the user can open in Calendar.app is the difference
# between "the app read my calendar" and "trust me". The directory is created by
# the fetch, never at import — importing this module must not touch the disk.
CALENDAR_DIR = config.data_path("calendar")


def calendar_snapshot_path(source: str) -> str:
    """Local file a URL-fed calendar downloads to. One stable name per source, so
    re-fetching replaces the snapshot instead of piling up dated copies of a
    file that contains every meeting the user has."""
    spec = CALENDAR_URL_SOURCES.get(source)
    if spec is None:
        raise ImporterError(f"'{source}' is not a URL-fed calendar")
    return os.path.join(CALENDAR_DIR, spec["file"])


# ── build_cmd closures — one per source, same argv the old _import_command /
#    _enrich_command produced, just addressed by key instead of if/elif chain ──

def _cmd_contacts(form: Dict[str, Any]) -> List[str]:
    """One row, two routes to the same contacts (NC-029).

    A `.abcddb` path means the address book itself, via the copy the FDA helper
    writes — that is the automatic route and it needs no argument, because
    addressbook_import.py defaults to that copy. Anything else is a hand-exported
    `.vcf`, the fallback for a Mac with no helper installed.

    Dispatching here rather than adding a second registry entry is deliberate: the
    Data page renders one row per source, and two rows both called Apple Contacts
    is the exact duplication NC-030 just removed for WhatsApp's photos."""
    path = str(form.get("path") or "").strip()
    if path.endswith(".abcddb"):
        cmd = [_py(), "addressbook_import.py", "--dir", os.path.dirname(path)]
        if form.get("dry_run"):
            cmd.append("--dry-run")
        return cmd
    return [_py(), "contacts_import.py", _require_path(form, "a Contacts.vcf file")]


def _cmd_linkedin(form: Dict[str, Any]) -> List[str]:
    # One card, two routes to the same contacts: the normal one-click run uses
    # LinkedIn's API (connections + the recent DM window in one go); when
    # "Found on this Mac" hands us a Connections.csv path instead, seed from
    # that file — the no-token fallback.
    path = str(form.get("path") or "").strip()
    if path:
        return [_py(), "seed_from_csv.py", path]
    return [_py(), "linkedin_import.py"]


def _cmd_imessage(form: Dict[str, Any]) -> List[str]:
    path = _require_path(form, "a chat.db copy")
    return [_py(), "imessage_import.py", "--chatdb", path]


def _cmd_whatsapp(form: Dict[str, Any]) -> List[str]:
    path = _require_path(form, "a ChatStorage.sqlite copy")
    return [_py(), "whatsapp_import.py", "--db", path]


def _cmd_whatsapp_photos(form: Dict[str, Any]) -> List[str]:
    path = _require_path(form, "the Profile folder")
    return [_py(), "whatsapp_photos.py", "--dir", path]


def _cmd_email(form: Dict[str, Any]) -> List[str]:
    path = _require_path(form, "an .mbox file")
    cmd = [_py(), "email_import.py", path, "--years", str(_years(form))]
    if form.get("summarize"):
        cmd.append("--summarize")
    return cmd


def _cmd_calendar(form: Dict[str, Any]) -> List[str]:
    """A real run writes; a preview does not.

    The bare argv with NO flag would not do: ics_import.py previews by default, so
    the Data card would parse the calendar, print a proposal, and write nothing.
    The write path exists and is tested; --apply is what connects the button to
    it.
    """
    path = _require_path(form, "a calendar (.ics) file")
    cmd = [_py(), "ics_import.py", path]
    cmd.append("--dry-run" if form.get("dry_run") else "--apply")
    return cmd


def _cmd_calendar_local(form: Dict[str, Any]) -> List[str]:
    """Apple Calendar's own store, via the helper's copy (NC-042).

    No path argument: calendar_local_import defaults to the copy, and there is
    nothing else it could sensibly read. `--apply` for the same reason
    _cmd_calendar carries it — ics_import previews by default, and a card whose
    button silently previewed was NC-005.
    """
    return [_py(), "calendar_local_import.py",
            "--dry-run" if form.get("dry_run") else "--apply"]


def _cmd_calendar_url(form: Dict[str, Any]) -> List[str]:
    """Shared by both URL-fed calendars — they differ only in which settings key
    holds the address, and that difference is resolved during the fetch.

    api_import.handle_import_run downloads the calendar FIRST and hands the
    snapshot's path back through the form, so by the time this runs there is a
    plain local file to import and no network I/O happens here. That keeps
    build_cmd pure and testable offline, and it means a caller that skipped the
    fetch gets this refusal rather than a silent run against a stale snapshot.
    """
    path = str(form.get("path") or "").strip()
    if not path:
        raise ImporterError(
            "no calendar has been downloaded yet — the URL is fetched before the "
            "import runs, and that fetch is off until NC_ICS_FETCH=1 in Settings")
    cmd = [_py(), "ics_import.py", path]
    cmd.append("--dry-run" if form.get("dry_run") else "--apply")
    return cmd


# NC-177. The one exit code from an importer child that means something other
# than "it worked" or "it broke": somebody has to sign in again, and the child
# refused to go and ask them because nobody started this run.
#
# It lives here rather than in gmail_import.py because this module is where the
# server's side of the importer contract already lives — build_cmd on the way
# out, parse_receipts on the way back, and now this on the way back too. One
# definition, imported by both sides, so the two cannot drift apart into a
# scheduler watching for 4 and a child exiting 3.
EXIT_NEEDS_SIGN_IN = 4

# NC-181. The exit code says a sign-in is waiting; this says WHOSE. GMAIL_ACCOUNTS
# is a list, and everything the Data page knows about a source is keyed by source
# rather than by account, so without a line like this the page could say Gmail
# needed signing in and not say which address — with several configured, that
# is a person opening Google once per address to find out.
#
# A marker line rather than reading the prose, for the same reason parse_receipts
# exists: the sentence is written for a human and will be rewritten for a human,
# and a parser aimed at it breaks the next time somebody improves the wording.
SIGN_IN_MARKER = "needs-sign-in:"


def parse_sign_in_accounts(output: str) -> List[str]:
    """Addresses named on SIGN_IN_MARKER lines, in order, without duplicates."""
    seen: List[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.lower().startswith(SIGN_IN_MARKER):
            continue
        who = line[len(SIGN_IN_MARKER):].strip()
        if who and who not in seen:
            seen.append(who)
    return seen


def _cmd_gmail(form: Dict[str, Any]) -> List[str]:
    # No file to pick — OAuth against accounts configured in gmail_import.py's
    # GMAIL_ACCOUNTS. --scan sweeps + matches by name (recommended default for
    # a first run); pass account="a@b.example" to restrict to one account.
    cmd = [_py(), "gmail_import.py", "--scan"]
    # NC-177. A run the background loop started is never allowed to put a Google
    # sign-in tab on somebody's screen. Without this the 12-hour timer re-entered
    # the consent flow whenever the saved permission had been revoked or had
    # expired — a browser window appearing out of nowhere, twice a day, from a
    # thread nobody started, which is the single behaviour most likely to read as
    # "this app has been compromised". With it the child exits 4 and run_autosync
    # puts "Gmail needs you to sign in again" on the Data page instead.
    if form.get("background"):
        cmd.append("--no-browser")
    # Without --since the script sweeps its 3-year default every run. That is the
    # right shape for a deliberate one-off from the UI, and the wrong shape for a
    # timer: the scheduled runs pass a high-water mark so they only fetch what
    # arrived since last time (see app.run_autosync).
    since = str(form.get("since") or "").strip()
    if since:
        cmd += ["--since", since]
    account = str(form.get("account") or "").strip()
    if account:
        cmd.append(account)
    return cmd


def _cmd_engagement(_form: Dict[str, Any]) -> List[str]:
    return [_py(), "engagement.py"]


def _cmd_google_photos(form: Dict[str, Any]) -> List[str]:
    cmd = [_py(), "google_photos_import.py"]
    account = str(form.get("account") or "").strip()
    if account:
        cmd.append(account)
    return cmd


# The two passes the app runs FOR the user, on its own, with no row to click.
# Named functions rather than REGISTRY lookups on purpose: api_import chains both
# of them automatically, and a chain that resolves its argv through
# REGISTRY["facts"] silently stops working the moment that entry is retired —
# `_build_command` raises, the caller's `except Exception: pass` eats it, and the
# Settings toggle goes on claiming the pass runs after every import. The UI row
# and the automatic behaviour must not share a single point of failure.
def facts_pass_cmd() -> List[str]:
    """The FREE offline facts pass: locations and role types from what we already
    have. Never the paid LLM pass — that is a separate, explicit click."""
    return [_py(), "enrich_facts.py"]


def style_pass_cmd() -> List[str]:
    """Re-measure how the user writes, so AI drafts keep matching their voice.
    api_briefs.py reads the saved profile, so this is chained after an import;
    without it the voice would freeze at whatever the last manual click
    produced."""
    return [_py(), "style_profile.py"]


def _cmd_facts(_form: Dict[str, Any]) -> List[str]:
    return facts_pass_cmd()


def _cmd_facts_llm(_form: Dict[str, Any]) -> List[str]:
    """No --limit, and that is the fix rather than an omission.

    This used to pass `--limit 20` literally. enrich_facts.py already derives
    that flag's DEFAULT from ENRICH_LLM_MAX, then ENRICH_LIMIT, then 20
    (enrich_facts.py:404-414) — and an explicit --limit wins over the default. So
    both keys sat in settings.MANAGED_KEYS, were read at call time, and governed
    nothing whenever the pass was run from the Data card, which is the usual way
    to run it. A third way for a setting to do nothing, after "captured at
    import" and "never registered": overridden by argv.

    Behaviour is unchanged when neither key is set — the script's own default is
    the same 20 — and the spend ceiling now moves when the user moves it.
    """
    return [_py(), "enrich_facts.py", "--llm"]


def _cmd_linkedin_facts(form: Dict[str, Any]) -> List[str]:
    """The facts the other Mac found, merged in. Free.

    NC-372: with `extras` in the form, the row's second button instead: the
    same saved profiles read by Anthropic for shared history and mutual
    connections (enrich_li_extras.py), which is billed per profile and capped at
    the Settings page's spend cap. It rides this source rather than a registry
    entry of its own because it is one step of the same lane, from the same
    other Mac, on the same row. Never --force: that re-bills every profile
    already read (api_linkedin's docstring). Refused, with the reason, when no
    key is set, so a press can never start a run that would only fail."""
    if form.get("extras"):
        if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            raise ImporterError(
                "Reading shared history sends each saved profile to Anthropic, so it "
                "needs a key from Anthropic. Add one in Settings, under Ask, drafts "
                "and facts written by AI, then press the button again.")
        cmd = [_py(), "enrich_li_extras.py", "--pull"]
        if form.get("dry_run"):
            cmd.append("--dry-run")   # honored by enrich(): no model call, no write
        return cmd
    cmd = [_py(), "merge_scraped_facts.py", "--pull"]
    if form.get("dry_run"):
        cmd.append("--dry-run")   # honored by merge(): counts, writes nothing
    return cmd


def _cmd_style(form: Dict[str, Any]) -> List[str]:
    """No --limit or --min-chars here on purpose: every tunable is an NC_STYLE_*
    key that style_profile.py re-reads from os.environ on every call, and
    handle_import_run passes env=os.environ.copy() to the subprocess. So a value
    saved in Settings applies to the next run with no argv to keep in sync. A
    flag duplicating a setting would give that limit two sources of truth.

    `--dry-run` IS passed, and it is honored end to end: style_profile.main()
    skips save() entirely under the flag (style_profile.py:2274-2276), which is
    the file's only write, so a dry run leaves the `state` table empty.
    """
    cmd = [_py(), "style_profile.py"]
    if form.get("dry_run"):
        cmd.append("--dry-run")
    return cmd


REGISTRY: Dict[str, ImporterSpec] = {
    "contacts": ImporterSpec(
        key="contacts", label="Apple Contacts", category="import",
        kind="file", pick_kind="vcf",
        # NC-191. This said the helper only had to be *installed*, and never
        # named the grant: copying Contacts needs the helper to hold Full Disk
        # Access, which nothing prompts for (see helper_blocked). No "panel at
        # the top of this page" either: the first-run wizard shows this text too,
        # and there the helper is set up in the next step.
        export_help=(
            "Your address book keeps itself up to date here **once the sync "
            "helper is set up**. It copies Contacts along with iMessage and "
            "WhatsApp, and this reads the copy on its own: nothing to export, "
            "nothing to click. Copying Contacts needs one macOS permission, Full "
            "Disk Access, given once to that small helper and never to this "
            "app.\n\n"
            "This is your curated list of people, so anyone new in it is added; "
            "existing contacts get their phone, email, company and title filled "
            "in. An edit you made here is never overwritten.\n\n"
            "**Without the helper**, macOS will not let the app read your address "
            "book at all, so it falls back to a file you export by hand: Contacts "
            "→ select all → File → Export → Export vCard…, then pick that file "
            "below. That copy is only as fresh as the day you exported it."
        ),
        build_cmd=_cmd_contacts,
    ),
    "linkedin": ImporterSpec(
        key="linkedin", label="LinkedIn", category="import",
        kind="none", pick_kind=None,
        export_help=(
            "One click pulls **everything LinkedIn's API offers about your own "
            "account**: all your connections (name, company, position, when you "
            "connected) and your recent DMs — real conversation text, browsable "
            "in each person's timeline and searchable. LinkedIn only exposes a "
            "recent window of messages, so re-run now and then to keep topping "
            "it up. It only ever adds: messages saved from earlier downloads stay, "
            "so each person's count keeps them.\n\n"
            "Needs a LinkedIn token, pasted on the **LinkedIn messages** row of the "
            "Data page: use **Open "
            "LinkedIn's token page** below, pick your app, tick the "
            "`r_dma_portability_self_serve` scope, and Request token.\n\n"
            "No token? A `Connections.csv` from LinkedIn's data export dropped "
            "in Downloads shows up under **Needs you** (connections "
            "only, no messages). What the API can't see (location, headline, "
            "skills) comes from the **LinkedIn profile facts** row instead."
        ),
        build_cmd=_cmd_linkedin,
    ),
    "imessage": ImporterSpec(
        key="imessage", label="iMessage", category="import",
        kind="file", pick_kind="chatdb",
        # NC-123. This opened with "macOS won't let any app read your Messages
        # folder, and this one never asks for the permission that would", and
        # never mentioned the sync helper at all — though the helper copies
        # iMessage and is the panel at the top of the same page. That put a
        # no-Full-Disk-Access rule into the copy as a virtue. The row now does
        # what Apple Contacts already does:
        # the simple route first, the one that needs no permission second, and
        # a sentence saying they are one switch. Rule 6 in docs/EDITING.md.
        export_help=(
            "Your messages keep themselves up to date here **if the sync helper "
            "is set up** — the panel at the top of this page. It copies iMessage "
            "every couple of minutes and this reads the copy on its own. It needs "
            "one macOS permission, Full Disk Access, given once to that small "
            "helper and never to this app.\n\n"
            "**Without the helper**, nothing needs any permission: you copy the "
            "files yourself — Finder is allowed to, on your say-so — and this "
            "reads your copy.\n\n"
            "1. Click **Open the Messages folder** below. Finder opens with the "
            "three `chat.db` files already selected.\n"
            "2. Press **⌘C**.\n"
            "3. Click **Open the import folder** below, press **⌘V**, and pick "
            "Replace if Finder asks.\n\n"
            "Done — it imports itself from there, and again whenever you paste a "
            "fresher copy. Keep all three files selected: `chat.db` on its own "
            "leaves your newest messages behind in SQLite's write-ahead log.\n\n"
            "You can switch between the two at any time. Turn the helper's access "
            "off in System Settings and this goes back to reading the copy you "
            "paste; the row says which one it is using.\n\n"
            # This paragraph replaces "Only people already in your Contacts are
            # matched", which was wrong twice over: "your Contacts" reads as
            # this app's own list rather than the macOS Contacts app, and it
            # implied a prerequisite the importer does not have. What the code actually
            # does (crm/imessage_import.py:5-15, :209-236) is three-way — match
            # an existing identifier, else resolve the name from the macOS
            # address book and CREATE the person, else skip. Say all three.
            "iMessage matches each conversation by phone number or email. If that "
            "number or email is already on a person here, it attaches to them. If "
            "not, it reads the name from your macOS Contacts app and adds that "
            "person. A number in neither place is skipped.\n\n"
            # NC-120. Said "adjustable in Settings". IMSG_GROUP_MAX
            # left the registry in NC-003 and has no field, so the sentence sent
            # people looking for a control that is not there — and they would then
            # doubt the rest of the page, which is the real cost.
            "Small group chats (friends, business groups of up to 8 members) are "
            "archived too, labeled with the group's name."
        ),
        build_cmd=_cmd_imessage,
    ),
    "whatsapp": ImporterSpec(
        key="whatsapp", label="WhatsApp", category="import",
        kind="file", pick_kind="wadb",
        # NC-192. This said WhatsApp's data was "readable in place, nothing to
        # copy and no special permissions". Since macOS 15 reading it in place
        # raises a consent dialog that does not durably stick — the reason
        # helper_covers_whatsapp() exists. It never needed Full Disk Access, and
        # still doesn't; it needs either the helper or that dialog.
        export_help=(
            "If WhatsApp Desktop is installed, it syncs with **one click from "
            "Needs you** on the Data page, nothing to copy. **Once the sync helper "
            "is set up** it keeps itself up to date: the helper copies WhatsApp "
            "along with iMessage, and this reads the copy, so macOS never has to "
            "ask this app about it.\n\n"
            "**Without the helper**, this reads WhatsApp's data where it is. Since "
            "macOS 15 that brings up a macOS dialog asking whether to allow it, "
            "and it asks again later, because the answer does not stick. Choose "
            "Allow to import.\n\n"
            # NC-120, the twin of the iMessage line above: WA_GROUP_MAX has no
            # field either.
            "Group chats with up to 15 members are "
            "archived too — messages from people already in your CRM, labeled "
            "with the group's name.\n\n"
            "Profile pictures come with it. WhatsApp Desktop caches every one it "
            "has seen, so they get put on the people you already have here; a "
            "photo you set yourself is never overwritten, and group avatars are "
            "skipped.\n\n"
            "Only if it wasn't detected: copy `ChatStorage.sqlite` from "
            "`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/` "
            "and pick your copy below."
        ),
        build_cmd=_cmd_whatsapp,
    ),
    # NOT a row on the Data page (NC-030): "if whatsapp is
    # imported, import the photos". It sat directly under WhatsApp's row, both of
    # them saying they synced themselves daily, which is two rows for one thing.
    # crm/api_ledger.py's PART_OF folds it into WhatsApp's row and drops it
    # from the findings, and WhatsApp's export_help above says photos come along.
    #
    # It stays, and it is not decoration: this is the spec run_autosync
    # builds argv from when the once-a-day boot pass finds Media/Profile inside
    # the container (crm/api_import.py:_autosync_pending), and /api/import/run
    # plus `python whatsapp_photos.py` still reach it. export_help is no longer
    # displayed anywhere.
    "whatsapp_photos": ImporterSpec(
        key="whatsapp_photos", label="WhatsApp profile photos", category="import",
        kind="folder", pick_kind="profilefolder",
        export_help=(
            "Puts faces on your contacts — WhatsApp Desktop already caches every "
            "profile picture it has seen. If it's installed, this syncs with **one "
            "click from Needs you** on the Data page; the picker below is only the "
            "fallback for a manually copied `Media/Profile` folder.\n\n"
            "Photos only attach to people already in your CRM; a photo you set "
            "yourself is never overwritten. Group avatars are skipped."
        ),
        build_cmd=_cmd_whatsapp_photos,
    ),
    "email": ImporterSpec(
        key="email", label="Email (.mbox)", category="import",
        kind="file", pick_kind="mbox",
        export_help=(
            "1. Export a mailbox to `.mbox`: Apple Mail → Mailbox → Export Mailbox…, "
            "or Gmail → Google Takeout → Mail.\n"
            "2. Pick the resulting `.mbox` file below.\n\n"
            "Only two-way conversations with people already in your CRM are counted."
        ),
        build_cmd=_cmd_email,
    ),
    # DROPPED (NC-015). Telegram, Messenger and Instagram each had a
    # row here and none of the three imported anything on its own: nothing
    # ever filled in `tg_count`, `msgr_count` or `ig_count`. Three rows a
    # stranger would try, in a list that was already too long.
    #
    # Messenger was the worst of them and is why all three went rather than one
    # being finished: default end-to-end encryption keeps recent chats out of the
    # standard Meta export entirely, and the ones that remain need a separate
    # PIN-protected flow meta_import.py does not know. A row that can only ever
    # return a fraction of your history is worse than no row.
    #
    # Every one of them was a hand-made download in a product whose rule is now
    # that imports run themselves (NC-036), so none of the three was ever going
    # to be automatic anyway.
    #
    # WHAT SURVIVES, and this is deliberate:
    #   · crm/telegram_import.py and crm/meta_import.py are untouched and still
    #     work from the command line. The capability is not deleted, its row is.
    #   · The archive channels, the `tg_count`/`msgr_count`/`ig_count` columns and
    #     the instagram/facebook/telegram identifier kinds all stay. A person's
    #     Instagram handle is a real field you can type in by hand, and anything
    #     already imported by an earlier CLI run keeps counting.
    # ── Calendar: one parser (crm/ics_import.py), three ways to feed it ──────
    # A file you exported, a Google secret .ics address, or a published iCloud
    # webcal link. There is no EventKit route and nothing reads
    # ~/Library/Calendars: those are TCC-protected, so invariant 6 rules them out
    # permanently — the same reason iMessage is a copy-the-file flow.
    #
    # All three WRITE. ics_import.apply() adds matched meetings to contact_dates
    # and commits; a real run passes `--apply` and a preview passes `--dry-run`,
    # so the two argvs genuinely differ and `supports_dry_run=True` is honest.
    #
    # Claiming otherwise ("All three are PREVIEW ONLY: ics_import.py has no
    # write path") left three cards telling users "Nothing is written to your
    # CRM yet" next to a button that writes. See NC-005. If you change the write
    # behaviour, the three export_help strings below are part of that change,
    # not documentation of it.
    "calendar_local": ImporterSpec(
        key="calendar_local", label="Apple Calendar", category="import",
        kind="none", pick_kind=None,
        export_help=(
            "**Your Mac's own calendar, with nothing to set up.** No secret "
            "link, no address to paste, no switch to find — the sync helper "
            "copies Calendar's database the same way it copies iMessage and "
            "Contacts, and this reads the copy.\n\n"
            "It attaches every meeting it can to a person already in your CRM "
            "and reports the ones it refused and why. **It writes to your "
            "CRM** — *Preview* shows the decisions first.\n\n"
            "This reads every calendar in Calendar.app, including the Google "
            "and iCloud accounts you have added there. If this row is working "
            "you do not need the secret-link rows below it, and they fold "
            "away.\n\n"
            "**It needs the sync helper to have Full Disk Access**, like "
            "iMessage and Contacts. Nothing here can ask macOS for that — see "
            "the panel at the top of this page. Network CRM itself never holds "
            "it; the helper is one small program whose only job is copying "
            "those files somewhere this app can already read.\n\n"
            # NC-123. Invariant 6 now says every source the helper covers keeps a
            # way in that needs no permission, and says so on its own row. This
            # row had the helper route only; the no-permission route existed —
            # the file row below — and pointed up here, but nothing pointed down.
            "**Without the helper**, the same meetings come in with no "
            "permission at all: in Calendar choose File → Export → Export…, then "
            "pick that file on the **Calendar file** row. That copy "
            "is only as fresh as the day you exported it."
        ),
        build_cmd=_cmd_calendar_local,
        supports_dry_run=True,
    ),
    "calendar_google": ImporterSpec(
        key="calendar_google", label="Google Calendar (secret link)", category="import",
        kind="none", pick_kind=None, needs="NC_ICS_GOOGLE_URL",
        export_help=(
            "Your Google calendar, read over https from the private address "
            "Google hands out for it — no sign-in, no OAuth, nothing uploaded. "
            "**It writes to your CRM**: matched meetings are saved against the "
            "people they involve. *Preview* shows the decisions first.\n\n"
            "1. **Open Google Calendar settings** below → pick the calendar in "
            "the left column → *Integrate calendar* → copy **Secret address in "
            "iCal format**.\n"
            "2. Paste it into **Private address** on this row and press **Connect**. Treat that "
            "link like a password: anyone holding it can read the whole calendar, "
            "with no sign-in and no trace. For a second Google calendar, copy its "
            "secret address the same way and paste both into the same box with a "
            "comma between them.\n"
            "3. Switch on **Download linked calendars** in Settings. Downloading a "
            "calendar reaches the internet, so it stays off until you turn it on.\n"
            "4. Click **Run import**.\n\n"
            "What leaves your Mac: one request to Google for each address, carrying "
            "that address and nothing else — no contact, no message, no note. Once step 3 is "
            "on, that request also happens **once a day on its own**, so the "
            "calendar stays current without you coming back here — turn off "
            "**Keep your data up to date** in Settings to stop that. "
            "Prefer to send nothing "
            "at all? Use the **Calendar file** row — same parser, "
            "same results, and nothing leaves your Mac.\n\n"
            "**Several Google calendars on this Mac?** If the Apple Calendar row at "
            "the top of this group is working, it already reads every calendar in "
            "Calendar.app, including each Google account you have added there, with "
            "no address to paste — you do not need this row."
        ),
        build_cmd=_cmd_calendar_url,
        supports_dry_run=True,
        writes=True,
    ),
    "calendar_icloud": ImporterSpec(
        key="calendar_icloud", label="iCloud Calendar (published link)", category="import",
        kind="none", pick_kind=None, needs="NC_ICS_ICLOUD_URL",
        export_help=(
            "An iCloud calendar you have published, read over https from its "
            "`webcal://` link. **It writes to your CRM** — matched meetings are "
            "saved against the people they involve, and *Preview* shows the "
            "decisions first.\n\n"
            "1. Calendar.app → right-click the calendar → **Share Calendar…** → "
            "tick *Public Calendar* → **Copy Link**.\n"
            "2. Paste the `webcal://…` link into **Public address** on this row and "
            "press **Connect**. Publishing a calendar is its own decision, and the "
            "link is a password: anyone with it can read the calendar. For a second "
            "calendar, paste both links into the same box with a comma between "
            "them.\n"
            "3. Switch on **Download linked calendars** in Settings, then click "
            "**Run import**.\n\n"
            "Nothing is uploaded — the request carries the link and nothing "
            "else. Once step 3 is on it repeats **once a day on its own** so the "
            "calendar stays current — turn off **Keep your data up to date** "
            "in Settings to stop that. There is no "
            "EventKit here and nothing "
            "reads `~/Library/Calendars` — which is the pre-Ventura location "
            "and does not exist on a current macOS anyway. **If the Apple "
            "Calendar row at the top of this group is working you do not need "
            "this one**: it reads every calendar in Calendar.app, including the "
            "Google and iCloud accounts you have added there, including this "
            "one. No helper, and no wish to publish a calendar? Use the **Calendar "
            "file** row instead."
        ),
        build_cmd=_cmd_calendar_url,
        supports_dry_run=True,
        writes=True,
    ),
    "calendar": ImporterSpec(
        key="calendar", label="Calendar, exported by hand", category="import",
        kind="file", pick_kind="ics",
        export_help=(
            "**Use one of the rows above instead if you can** — a file you "
            "export is a snapshot of the day you exported it, and it will not "
            "notice a single meeting you book afterwards. This row is here for "
            "when nothing above it is an option.\n\n"
            "**You can read your Apple Calendar directly** — that is the row at "
            "the top of the calendar group. Calendar's store lives in a group "
            "container, not in `~/Library/Calendars`, which is the pre-Ventura "
            "location and does not exist on a current macOS. The sync helper "
            "copies it the same way it copies iMessage and Contacts — which "
            "needs Full Disk Access granted to that helper and nothing broader. "
            "Network CRM itself never holds it.\n\n"
            "It attaches every meeting it can to a person already in your CRM and "
            "reports each one it refused and why. **It writes to your CRM** — use "
            "*Preview* first if you want to see the decisions before any of them "
            "are saved.\n\n"
            "1. Calendar.app → **File → Export → Export…** (or Google Calendar → "
            "Settings → Import & export → **Export**, then unzip it).\n"
            "2. Pick the `.ics` file below. One sitting in Downloads or on the "
            "Desktop already shows up under **Needs you**.\n\n"
            "Only people already in your CRM are matched — a calendar invite is "
            "never a reason to create someone. Events with more than 6 other "
            "people on them are skipped: a 40-person all-hands is not a meeting "
            "with someone. A repeating event is read as its first occurrence and "
            "reported as repeating; the later dates are never invented.\n\n"
            "No network and no permissions — this is a file you exported yourself."
        ),
        build_cmd=_cmd_calendar,
        supports_dry_run=True,
        # True since ics_import.apply() landed: a real run adds meetings to
        # contact_dates, so the post-run bookkeeping and the "last synced" stamp
        # are now telling the truth rather than claiming a preview changed data.
        writes=True,
    ),
    "google_photos": ImporterSpec(
        key="google_photos", label="Google contact photos", category="import",
        kind="none", pick_kind=None, needs="GMAIL_ACCOUNTS",
        export_help=(
            "Profile pictures of the people you email, from Google's People API — "
            "reuses the Gmail import's OAuth client, nothing to export.\n\n"
            "1. Set up Gmail import first (its credentials files are reused).\n"
            "2. Click **Run import** — a browser tab opens once per account for a "
            "read-only contacts consent (separate token; your Gmail access is untouched).\n\n"
            "Matches by email/phone/name against people already in your CRM. Photos "
            "you set yourself or imported from your address book are never overwritten."
        ),
        build_cmd=_cmd_google_photos,
    ),
    "gmail": ImporterSpec(
        key="gmail", label="Gmail", category="import",
        kind="none", pick_kind=None, needs="GMAIL_ACCOUNTS",
        # NC-117. What this said before, verbatim: "2. Click **Connect** below —
        # a browser tab opens for Google sign-in." There was no Connect control
        # anywhere in the front end and there never had been. It also named
        # `secrets/gmail_credentials_<slug>.json` — a folder with no button to
        # open it, under a name derived by a regular expression that appears
        # nowhere on screen — and pointed at a "README §Setup" that does not
        # exist. Three instructions, none of them followable.
        #
        # The steps now live in the panel below this (static/app/gmail.js),
        # where the buttons they describe actually are. What is left here is the
        # thing a person needs BEFORE deciding to spend five minutes: what it
        # collects, what it costs, and that there is a way out if they would
        # rather not.
        #
        # No numbered list: Md (static/app/ui.js) renders `-` bullets and
        # paragraphs, and nothing else. A line starting "1." comes out as the
        # characters "1." in a paragraph — which is exactly what the old text
        # did, and part of why it read as a procedure while being unusable as
        # one.
        export_help=(
            "Reads your Gmail directly, so it stays current on its own — no "
            "export to make and no file to keep.\n\n"
            "Only messages to and from people already in your CRM are fetched. "
            "It does not download your inbox.\n\n"
            "Setting it up means making your own key with Google, once, in "
            "about five minutes. Google will not let one app read many people's "
            "mail without a yearly security audit, so everyone makes their own. "
            "The steps are below, with the buttons for each one.\n\n"
            "Would rather not? The **Email (.mbox)** source imports a Google "
            "Takeout export of the same mail with no account access at all — "
            "but it is a snapshot, so it goes stale the moment it is made."
        ),
        build_cmd=_cmd_gmail,
    ),
    "engagement": ImporterSpec(
        key="engagement", label="Recompute closeness", category="enrich",
        kind="none", pick_kind=None,
        export_help=(
            "Recomputes every contact's familiarity score from the message archive — "
            "distinct days you've talked, sent/received balance, tenure, and rhythm. "
            "Runs automatically at startup and after imports; this button is for an "
            "on-demand refresh. Free, offline, safe to re-run."
        ),
        build_cmd=_cmd_engagement,
    ),
    "facts": ImporterSpec(
        key="facts", label="Fill facts (free, offline)", category="enrich",
        kind="none", pick_kind=None,
        export_help=(
            "Rule-based pass, no API key needed: fills home city/country from phone area "
            "codes and email domains, and role type from LinkedIn titles. Safe to re-run."
        ),
        build_cmd=_cmd_facts,
    ),
    "facts_llm": ImporterSpec(
        key="facts_llm", label="Fill facts with AI (Claude)", category="enrich",
        kind="none", pick_kind=None, needs="ANTHROPIC_API_KEY",
        export_help=(
            "Sends a short context snippet per contact to Claude to fill city/country/topics/"
            "role/blurb. Costs a small amount and needs an Anthropic key (**Key from Anthropic** in Settings). "
            "Capped at 20 contacts per run to control spend."
        ),
        build_cmd=_cmd_facts_llm,
    ),
    "linkedin_facts": ImporterSpec(
        key="linkedin_facts", label="LinkedIn profile facts", category="enrich",
        kind="none", pick_kind=None,
        export_help=(
            "The data LinkedIn's API refuses to give you — **location, topics, "
            "role and a one-line blurb per person** — comes from your own scraper "
            "machine, which visits connection profiles slowly "
            "(most-relevant people first).\n\n"
            "**You need a second Mac for this, and its address on this row.** Put "
            "that machine in **The other Mac, as user@name** — a name that resolves "
            "from any network works — and its sign-in key's location in **Where its sign-in "
            "key file is on this Mac** if it is not at "
            "`~/.ssh/imac_scraper`. Without those there is nothing to pull from, "
            "and this row says so rather than offering a button that fails.\n\n"
            "This pulls its latest results over SSH and merges them in. "
            "Safe to re-run whenever; anything you've edited by hand is never "
            "overwritten. Coverage grows daily — run it again next week and more "
            "people will have cities and topics."
        ),
        build_cmd=_cmd_linkedin_facts,
        supports_dry_run=True,
        # No IMAC, no scraper machine to pull from — see the field's comment.
        needs="IMAC",
    ),
    "style": ImporterSpec(
        key="style", label="Measure how you write", category="enrich",
        kind="none", pick_kind=None,
        export_help=(
            "Measures how you write, from the messages **you sent**. It counts "
            "message length, sentence length, greetings, sign-offs, emoji, "
            "questions and punctuation, and it keeps Swedish and English apart. "
            "Drafts can then match your habits instead of a generic voice.\n\n"
            "Nothing leaves this Mac. There is no API call and no network "
            "request. It reads the message archive and changes nothing in it. It "
            "saves one row in this Mac's `state` table, and nothing copies "
            "that table anywhere.\n\n"
            "It stores **numbers, not text** — counts, medians and percentages, "
            "plus your own greetings and sign-offs from a fixed list of common "
            "ones. No message body and no other person's name is kept.\n\n"
            "Every figure names how many messages it was measured on, and the "
            "run lists what it left out with a count: short messages, group "
            "chats, and messages the language test cannot decide. Two "
            "exclusions are worth knowing. **LinkedIn is left out by default**, "
            "because the LinkedIn import decides who sent a message by guessing "
            "(\"the sender is not one of my contacts, so I sent it\"), which "
            "would measure recruiter mail as your writing. **Email "
            "needs your address** (**Your email addresses** in Settings) — without it, that channel cannot "
            "tell your messages from theirs, and the run says so.\n\n"
            "With no archive yet, the run saves an empty profile and reports the "
            "reason. Drafts keep working without a profile.\n\n"
            "Free, offline, safe to re-run. Use **Preview** to see the numbers "
            "and save nothing."
        ),
        build_cmd=_cmd_style,
        # True because the flag is real, not because a preview would be nice:
        # style_profile.main() calls save() only when --dry-run is absent, and
        # save() is the module's only write (style_profile.py:1828), so a dry run
        # leaves the `state` table with no row. If that flag is ever removed, flip
        # this to False in the same change, or _build_command will accept a
        # preview request and run the real pass.
        supports_dry_run=True,
        # It writes: one row in crm.db's `state` table. So the post-run
        # bookkeeping in handle_import_run is honest here — unlike the calendar
        # previews, this run really did change the database.
        writes=True,
    ),
}


# ── Result receipts: distill importer stdout into verb-count chips ────────
# Heuristic regexes over the summary lines the importers already print (see
# each script's main()). Unknown output just yields fewer receipts — worst
# case an empty list, and the raw log is still shown behind the disclosure.
RECEIPT_PATTERNS = [
    (r"([\d,]+)\s+new messages archived", "Archived"),
    (r"Matched to existing contacts:\s+([\d,]+)", "Matched"),
    (r"\bMerged\s+([\d,]+)\s+of\s+[\d,]+", "Merged"),
    (r"([\d,]+)\s+conversation chunks", "Chunked"),
    # colon form (whatsapp/imessage/telegram/meta/email) and number-first form
    # (gmail's "3 new contacts created", seed_from_csv's "+3 new")
    (r"New contacts (?:created|added):\s+([\d,]+)", "Added"),
    (r"([\d,]+)\s+new contacts created", "Added"),
    (r"Seed complete:\s+\+([\d,]+)\s+new", "Added"),
    (r"\+([\d,]+)\s+contacts", "Added"),
    (r"[Cc]ontacts updated:\s+([\d,]+)", "Updated"),
    (r"Photos (?:imported|saved/refreshed):\s+([\d,]+)", "Photos"),
    (r"refreshed for ([\d,]+) contacts", "Refreshed"),
    (r"Enriched ([\d,]+) contacts", "Enriched"),
    # Calendar preview (ics_import.py). "Proposed", never "Added": that run
    # writes nothing, so a chip reading "Added 12" would be a lie about the
    # database. The verbs have to stay as honest as the report they summarize.
    (r"([\d,]+)\s+calendar events? read", "Read"),
    (r"([\d,]+)\s+dates? would be added", "Proposed"),
    # A real calendar import (--apply) prints its own receipt, "3 meetings added.",
    # and no longer the preview's "would be added" (2026-10-17), so without this
    # its card showed only the "Read" chip.
    (r"([\d,]+)\s+meetings?\s+added", "Added"),
    # Writing-style pass (style_profile.py). "Measured", never "Added": the run
    # saves one derived profile and creates no contact, note or message. Without
    # these two the card's receipt row would be empty, and the numbers the pass
    # already prints would only be visible behind the raw-log disclosure.
    (r"([\d,]+)\s+outbound messages? read", "Read"),
    (r"([\d,]+) used for the statistics", "Measured"),
]
RECEIPT_CAP = 5


def parse_receipts(output: str) -> List[Dict[str, Any]]:
    """Parse an importer's stdout into [{verb, count}, ...] receipt chips.

    First match per verb wins (a run prints each summary line once; dedupe
    guards re-printed sections), in output order, capped at RECEIPT_CAP.
    Returns [] when nothing recognizable was printed."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for line in (output or "").splitlines():
        for pattern, verb in RECEIPT_PATTERNS:
            if verb in seen:
                continue
            m = re.search(pattern, line)
            if not m:
                continue
            try:
                count = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            seen.add(verb)
            out.append({"verb": verb, "count": count})
    return out[:RECEIPT_CAP]


# ── Auto-detection: files that can be imported with one click, no copying ────
# Each detector returns a list of {source, path, label, mtime} findings. The
# WhatsApp Desktop container needs no Full Disk Access, but it IS protected:
# since macOS 15 it is in the app-data category, and reading it raises a consent
# dialog (NC-192; see WA_AUTO_DB and helper_covers_whatsapp below). So it is read
# in place only when the helper has not copied it. Desktop/Downloads are scanned
# for exports and previous manual copies.

def _mtime(path: str) -> str:
    try:
        import datetime
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except OSError:
        return ""


def _finding(source: str, path: str, label: str, count: int = 0,
             blocked: bool = False, helper_current: bool = False) -> Dict[str, Any]:
    return {"source": source, "path": path, "label": label, "mtime": _mtime(path),
            "count": count, "blocked": blocked, "helper_current": helper_current}


def _quick_count(source: str, path: str) -> int:
    """Fast row-count against a SQLite db to show the user a preview number.
    Never raises — returns 0 on any error (permission wall, WAL lock, etc.)."""
    import sqlite3
    try:
        if source == "whatsapp":
            # ZWACHATSESSION is one row per conversation (not per message)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as c:
                return c.execute("SELECT COUNT(*) FROM ZWACHATSESSION").fetchone()[0]
        if source == "imessage":
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as c:
                return c.execute("SELECT COUNT(*) FROM handle").fetchone()[0]
        if source == "contacts":
            if path.endswith(".abcddb"):
                # EVERY store in the folder, not just the one named here. The
                # helper copies Sources/<account>/AddressBook-v22.abcddb beside
                # the flat file, and on a Mac that syncs contacts the flat file
                # holds almost nothing. Counting only it made the row read a tiny
                # count beside a healthy green status, which
                # is the NC-043 bug. addressbook_import.read_all() is the real
                # importer and looks at all of them; this preview must agree.
                #
                # immutable=1 rather than mode=ro because a preview number is not
                # worth a WAL replay on every page load. The importer itself does
                # replay the WAL — see addressbook_import.read_store.
                import addressbook_import
                total = 0
                for store in addressbook_import.store_paths(os.path.dirname(path)):
                    try:
                        with sqlite3.connect(f"file:{store}?immutable=1",
                                            uri=True, timeout=2) as c:
                            total += c.execute(
                                "SELECT COUNT(*) FROM ZABCDRECORD "
                                "WHERE COALESCE(ZFIRSTNAME,'') <> '' "
                                "   OR COALESCE(ZLASTNAME,'') <> ''"
                                "   OR COALESCE(ZORGANIZATION,'') <> ''").fetchone()[0]
                    except Exception:
                        continue
                return total
            # .vcf — count BEGIN:VCARD markers in the first 256 KB
            with open(path, "r", errors="replace") as f:
                sample = f.read(262144)
            return sample.count("BEGIN:VCARD")
        if source == "calendar":
            # .ics — count BEGIN:VEVENT markers in the first 256 KB. Same idiom
            # and the same first-chunk approximation as the .vcf branch above,
            # and reached only for a path glob() already returned, so it opens
            # nothing the Desktop/Downloads scan hasn't already been cleared for.
            with open(path, "r", errors="replace") as f:
                sample = f.read(262144)
            return sample.upper().count("BEGIN:VEVENT")
    except Exception:
        pass
    return 0


# The root detect_sources() scans for loose exports (Desktop, Downloads). A
# module constant rather than an expanduser() call inside the function, so tests
# can point the scan somewhere harmless: Desktop and Downloads are both
# TCC-gated categories, and a probe of either can block on a consent dialog
# instead of failing. See tests/helpers.py.
SCAN_HOME = os.path.expanduser("~")

WA_CONTAINER = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared")
# The import folder: an ordinary folder the app watches. The user pastes their
# own Finder copy of chat.db here (the zero-permission route — Finder is
# privileged, we are not), and the opt-in FDA helper would write here too.
IMSG_AUTO_DB = os.path.expanduser(
    "~/Library/Application Support/NetworkCRM/imessage/chat.db")
IMSG_INBOX = os.path.dirname(IMSG_AUTO_DB)
# The protected original. Its contents are unreadable without Full Disk Access,
# but file *metadata* is not protected — os.path.exists() on each name works, so
# we can reveal exactly the files that are there.
IMSG_SOURCE_DIR = os.path.expanduser("~/Library/Messages")
# All three, because chat.db alone is missing whatever is still in the WAL.
IMSG_COPY_NAMES = ("chat.db", "chat.db-wal", "chat.db-shm")

# Apple Calendar's own store, as the helper's copy (NC-042). There is no
# paste-it-yourself route for this one and deliberately no picker: the original
# lives in ~/Library/Group Containers/group.com.apple.calendar, and asking a
# person to find that in Finder is worse than the secret-link flow this replaces.
# Helper or nothing.
#
# NOT ~/Library/Calendars, which does not exist on Ventura or later.
CAL_AUTO_DB = os.path.expanduser(
    "~/Library/Application Support/NetworkCRM/calendar/Calendar.sqlitedb")
CAL_INBOX = os.path.dirname(CAL_AUTO_DB)

# The same arrangement for WhatsApp, and it exists for a sharper reason than
# iMessage's. WA_CONTAINER above is technically readable in place — but since
# Sequoia put it in the app-data TCC category, reading it from Python raises a
# consent dialog, attributed to "python3" rather than to this app. That made the
# background loop unusable: it asked every NC_AUTOSYNC_MINUTES, forever. The loop
# now looks only here, at a copy the FDA helper writes, which is inside our own
# container and therefore prompts for nothing.
WA_AUTO_DB = os.path.expanduser(
    "~/Library/Application Support/NetworkCRM/whatsapp/ChatStorage.sqlite")
WA_INBOX = os.path.dirname(WA_AUTO_DB)
# The profile-photo cache, copied to the same shape it has in the container so
# whatsapp_photos.find_chatstorage() locates ChatStorage.sqlite two levels up
# without being told (NC-047). Until this existed, Media/Profile was the last
# thing in the app that could only be read IN PLACE — which is why the paragraph
# above was true of the message database and false of the photos.
WA_PHOTOS_AUTO_DIR = os.path.join(WA_INBOX, "Media", "Profile")
WA_COPY_NAMES = ("ChatStorage.sqlite", "ChatStorage.sqlite-wal", "ChatStorage.sqlite-shm")

# And the same again for Apple Contacts (NC-029). Its folder is protected too,
# by macOS's Contacts permission rather than WhatsApp's app-data one (NC-192:
# its consent dialog asks about the user's contacts, not about "data from other
# apps"), and Full Disk Access covers it. So the same answer applies: the FDA
# helper copies it here and nothing else ever touches the original. Without
# that, Apple Contacts would move ONLY when a human exported a .vcf and pressed
# a button, which is the manual step that NC-036 rules out.
#
# The -wal is not optional: it can be several times the size of the main file.
# Copy the main file alone and most of the recent address book is simply not in
# the snapshot.
AB_AUTO_DB = os.path.expanduser(
    "~/Library/Application Support/NetworkCRM/addressbook/AddressBook-v22.abcddb")
AB_INBOX = os.path.dirname(AB_AUTO_DB)
AB_COPY_NAMES = ("AddressBook-v22.abcddb", "AddressBook-v22.abcddb-wal",
                 "AddressBook-v22.abcddb-shm")
# LaunchAgent that packaging/install_imessage_sync.sh installs. Its presence is
# the only honest way to tell "a helper keeps this fresh" from "you pasted it".
IMSG_HELPER_LABEL = "io.github.william-agehouse.networkcrm.imessagesync"
IMSG_HELPER_PLIST = os.path.expanduser(
    f"~/Library/LaunchAgents/{IMSG_HELPER_LABEL}.plist")
# Where install_imessage_sync.sh puts the compiled binary, and where its stdout
# goes. Both are the installer's choices; duplicated here rather than parsed out
# of the script, with tests/test_packaging.py asserting the two agree.
IMSG_HELPER_BIN = os.path.expanduser(
    "~/Library/Application Support/NetworkCRM/bin/networkcrm-imessage-sync")
IMSG_HELPER_LOG = os.path.expanduser("~/Library/Logs/NetworkCRM/imessage-sync.log")
# The helper's own report of what happened on its last run — see "THE REPORT" in
# packaging/src/imessage_sync.swift. This is the ONLY channel by which the app
# learns that Full Disk Access is still missing: the exit code goes to launchd,
# the log is prose, and the app must never look at the protected folders itself.
IMSG_HELPER_REPORT = os.path.expanduser(
    "~/Library/Application Support/NetworkCRM/sync-status.json")


def imsg_helper_installer() -> str:
    """Absolute path to the opt-in FDA helper's installer, or "" if this install
    does not carry one.

    Two layouts, because the payload lives in different places:
      · packaged app — appify.sh puts it in Contents/Resources/imessage-sync,
        a sibling of Resources/app (which is config.ROOT), deliberately outside
        the Python tree that tests/test_dev_parity.py holds byte-identical.
      · repo checkout — packaging/, where it has always been.

    Returning "" is the honest answer for a bundle whose helper did not compile
    or sign (every build needs swiftc for the window since NC-330, so that is the
    only way to lack one): the UI must then not offer an option that this copy of
    the app cannot deliver.
    """
    if config._running_from_app_bundle():
        cand = os.path.join(os.path.dirname(config.ROOT), "imessage-sync",
                            "install_imessage_sync.sh")
    else:
        cand = os.path.join(config.ROOT, "packaging", "install_imessage_sync.sh")
    return cand if os.path.exists(cand) else ""


def imsg_helper_uninstaller() -> str:
    """The installer's other half, or "" (NC-187). Same two layouts: appify.sh
    ships both scripts side by side, and so does packaging/."""
    installer = imsg_helper_installer()
    if not installer:
        return ""
    cand = os.path.join(os.path.dirname(installer), "uninstall_imessage_sync.sh")
    return cand if os.path.exists(cand) else ""


def imsg_helper_installed() -> bool:
    """True only if the opt-in FDA helper's LaunchAgent is actually installed."""
    try:
        return os.path.exists(IMSG_HELPER_PLIST)
    except OSError:
        return False


def imsg_helper_report() -> Dict[str, Any]:
    """The helper's last run, as it described it, or {} if it has never reported.

    Deliberately incurious: whatever the file says is passed up, and the only
    thing checked is that it parses and has the shape the UI iterates over. A
    helper too old to write a report, a truncated write, a hand-edited file — all
    of them mean the same thing to the caller ("no report"), and inventing a
    partial one from a broken file would be worse than saying so.

    Reads a file inside the CRM's own Application Support folder, which is why
    this needs no permission the app does not already have.

    A helper that is not installed has no report, whatever the file says
    (NC-187). Turning the helper off leaves its last report behind, and every
    reader of it — auto_copy_label, helper_current, helper_blocked, the Data
    page's panel — would go on saying "Updated automatically" about a program
    that no longer exists.
    """
    if not imsg_helper_installed():
        return {}
    try:
        with open(IMSG_HELPER_REPORT, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        return {}
    return data


# The two source names the helper uses in its report, which are also the two the
# UI shows. Kept next to the function that reads them so a rename in the Swift
# breaks in one place.
HELPER_IMESSAGE = "iMessage"
HELPER_WHATSAPP = "WhatsApp"
# Must match the `label:` of the third Source in packaging/src/imessage_sync.swift
# exactly — it is the key the helper's own report is looked up by.
HELPER_CONTACTS = "Contacts"
# Must match the `label:` of the fourth Source in packaging/src/imessage_sync.swift.
HELPER_CALENDAR = "Calendar"
# Every source THIS BUILD's helper copies, in the helper's own order. api_fda
# compares it against what the INSTALLED binary reported: a LaunchAgent keeps
# running the binary it was installed with, so adding a source here without
# reinstalling leaves the helper quietly copying a subset forever.
HELPER_SOURCE_LABELS = (HELPER_IMESSAGE, HELPER_WHATSAPP, HELPER_CONTACTS,
                        HELPER_CALENDAR)


# The two answers auto_copy_label() can give, as constants because the tests need
# to tell them apart WITHOUT matching on the prose. The old tests asserted
# `assertIn("helper", label)`, which is the same source-reading-its-own-words trap
# NC-056 hit: it passes for any wording containing that word and fails for any
# correct wording that does not, so rewording the copy — exactly what NC-063 did —
# breaks a test that has nothing to say about the change.
AUTO_COPY_HELPER = "Updated automatically"
AUTO_COPY_MANUAL = "Your copy in the import folder"


def auto_copy_label(source_label: str) -> str:
    """How to describe the copy in the import folder: did a working helper put it
    there, or did the user paste it?

    This used to answer "is the LaunchAgent installed?" — `imsg_helper_installed()`,
    which is `os.path.exists()` on a plist. That is necessary and nowhere near
    sufficient: **Full Disk Access is a separate, manual grant that nothing prompts
    for**, so the overwhelmingly common state right after installing is a helper
    that is installed, scheduled, running on time, and blocked on every single
    run. In that state the old label told the user their iMessage copy was "kept
    current by the iMessage helper" while the Data page's own permission panel, a
    few rows above it, said "macOS is blocking iMessage". A user could not tell
    which of the two to believe.

    Answering "is this working?" with a signal that only proves "this is
    installed" is a recurring trap — see the EXIT CODE CONTRACT at the bottom of
    packaging/src/imessage_sync.swift for another instance. The signal that
    actually answers it is the helper's own report: ask what its last run DID to
    this specific source.

    Per-source on purpose. WhatsApp needs no Full Disk Access and copies fine while
    iMessage is blocked, so "the helper works" is not one fact — it is two, and
    collapsing them is how a partial block reads as total success.
    """
    for row in imsg_helper_report().get("sources", []):
        if not isinstance(row, dict) or row.get("label") != source_label:
            continue
        # The same two outcomes api_fda._GOOD treats as fine. Anything else —
        # blocked, absent, or a value this version does not know — falls through to
        # the honest description of a file somebody put there by hand.
        if row.get("outcome") in ("copied", "current"):
            # NC-035: this said "the iMessage helper" for every source, so the
            # WhatsApp row read "kept current by the iMessage helper" — after four
            # paragraphs above about why the two must not be collapsed. One binary
            # copies both, so name it for the job and not for one of its inputs.
            #
            # NC-063: the label used to say "kept current by the sync helper".
            # Two things were wrong with it. It is jargon — "the sync helper" is
            # a thing you have to already know about for the sentence to parse —
            # and at 31 characters it did not fit the `need` slot beside the
            # source name, so what was actually shown was "kept current by…", a
            # sentence cut off before the only part carrying information.
            #
            # The slot answers "what does this row need from you?". For a source
            # the helper handles, the answer is nothing, and the shortest honest
            # way to say that is to say what happens instead.
            return AUTO_COPY_HELPER
    return AUTO_COPY_MANUAL


def helper_current(source_label: str) -> bool:
    """Did the helper's last run vouch for THIS source's copy being up to date?

    The third function reading the same report, and the distinction it draws is
    the one the Data page was getting wrong. `copied` means the helper refreshed
    our copy on that run; `current` means it compared and found our copy already
    identical to the live original. Both mean the copy matches reality — so an
    OLD FILE MTIME IS NOT STALENESS for these sources. The helper skips a file it
    has already got, by design (that is what makes a two-minute timer affordable),
    so a source nobody has touched in a fortnight keeps a fortnight-old mtime
    while being perfectly current.

    Reading mtime instead puts the Apple Contacts row in "Needs you" with a
    "paste a fresh copy" message — for a source that has no paste-it-yourself
    route and that the helper had successfully checked minutes earlier. The
    docstring on auto_copy_label above names a signal that only looks like the
    answer to "is this working?"; file age is another one.
    """
    for row in imsg_helper_report().get("sources", []):
        if isinstance(row, dict) and row.get("label") == source_label:
            return row.get("outcome") in ("copied", "current")
    return False


def _photo_cache_has_files(path: str) -> bool:
    """Does our own copy of the photo cache hold anything importable?

    An empty directory is not a source. The helper creates the destination before
    it knows whether the sweep matched anything, so "the folder exists" and "there
    are photos in it" are genuinely different questions — offering a row for the
    first would put an import in front of the user that could only report zero.

    Only .jpg/.thumb, which is exactly what whatsapp_photos.pick_best() reads.
    """
    try:
        return any(n.endswith((".jpg", ".thumb")) for n in os.listdir(path))
    except OSError:
        return False


def helper_covers_whatsapp() -> bool:
    """Is there a helper-written WhatsApp copy, making the live container
    redundant?

    This is the switch that stops the app raising macOS's "would like to access
    data from other apps" dialog (NC-047). Reading WhatsApp's group container is
    in the app-data TCC category, so a probe of it — os.path.exists is enough —
    can raise that dialog. detect_sources() called it "the highest-value
    zero-copy import in the app" and the autosync loop rationed it to once a
    day, on the boot pass, with a comment saying it costs a dialog.

    Once a day, on the boot pass, IS "once per startup of the program", which is
    exactly the behaviour that was ruled out. And a TCC grant made to a bare
    interpreter does not durably stick, so the dialog was never going to stop
    coming back however patiently you answered it.

    The container route is not deleted — for someone with no helper it is still
    the only way WhatsApp works at all, and deleting a capability to remove a
    control is the NC-015 defect. It is skipped when it would tell us nothing we
    do not already have.

    NC-187 considered requiring the helper to be installed here too, so that
    turning it off would send WhatsApp back to the live container. It would also
    bring back the page-load dialog this function exists to stop, for everyone
    who ever had the helper, and tests/test_whatsapp_consent.py holds that line.
    So after "off" WhatsApp keeps reading the helper's last copy, labelled as a
    copy (imsg_helper_report is empty, so auto_copy_label says so) — stale, but
    said to be, and without a dialog nobody asked for.
    """
    return os.path.exists(WA_AUTO_DB)


def helper_blocked(source_label: str) -> bool:
    """Did the helper's last run get refused by macOS for THIS source?

    The other half of auto_copy_label. That function answers "who put this file
    here"; a stale answer of "you did" is then indistinguishable, on the page,
    from a file the user really did paste and forget — so the Data page told
    the user to "paste a fresh copy into the import folder" for a copy the helper
    writes itself every two minutes and was merely being refused permission to
    refresh. Pasting would not help, and the fix (one grant, in the panel
    directly above that row) has to be named where the user is looking.

    Only `blocked` counts. `absent` means the source is not on this Mac at all,
    which is not a permission problem, and an unknown outcome from a newer helper
    must not be read as one either.
    """
    for row in imsg_helper_report().get("sources", []):
        if isinstance(row, dict) and row.get("label") == source_label:
            return row.get("outcome") == "blocked"
    return False


# fold_wal() used to live here. It got the newest messages into a
# copied chat.db by running `PRAGMA wal_checkpoint(TRUNCATE)` on the copy, which
# was correct but expensive: writing scattered pages into an APFS clone breaks
# copy-on-write block by block, so a few MB of log cost several times that in
# real disk writes — every single sync cycle, gigabytes a day at a 2-minute
# cadence.
# imessage_import.snapshot_uri() now *reads* the -wal instead of folding it, for
# nothing. Don't reintroduce a checkpoint here (NC-028).


def detect_unprotected_sources(count: bool = True) -> List[Dict[str, Any]]:
    """The importable things that can be found WITHOUT touching a folder macOS
    guards: the copies inside our own import folders.

    This exists because detect_sources() cannot be called on a timer. It stats
    WhatsApp's group container and globs Desktop/ and Downloads/ — all three are
    TCC-protected since Sequoia added the app-data category, so a background loop
    calling it raised a consent dialog every NC_AUTOSYNC_MINUTES, for the life of
    the process. The dialog names "python3", not "Network CRM", which makes a
    routine prompt look like something foreign asking for the user's Messages.

    Both paths are under our own Application Support directory. An app's own
    container is not protected, so this raises nothing and can run as often as
    we like — which is what keeps a database that appeared after launch from
    needing an app restart to be noticed.

    WhatsApp must be listed here: the autosync loop filters for WA_CONTAINER
    paths, which this function cannot return, so without its own copy the
    WhatsApp branch would match nothing on every pass but the first and WhatsApp
    would quietly stop updating until the app was restarted. So WhatsApp has an
    unprotected copy of its own (the FDA helper writes one) rather than letting
    the loop back into the container.

    `count=False` is for the automatic import check, which runs every couple of
    minutes and never shows the count (NC-212). Counting opens each copy, and the
    helper replaces the copies with fresh files, so every count rebuilt a copy's
    32 KB index and read it cold from the SSD, for a number nobody saw.
    """
    out: List[Dict[str, Any]] = []

    def n(source: str, path: str) -> int:
        return _quick_count(source, path) if count else 0

    if _readable(IMSG_AUTO_DB):
        label = auto_copy_label(HELPER_IMESSAGE)
        out.append(_finding("imessage", IMSG_AUTO_DB, label,
                            n("imessage", IMSG_AUTO_DB),
                            blocked=helper_blocked(HELPER_IMESSAGE),
                            helper_current=helper_current(HELPER_IMESSAGE)))
    if _readable(WA_AUTO_DB):
        label = auto_copy_label(HELPER_WHATSAPP)
        out.append(_finding("whatsapp", WA_AUTO_DB, label,
                            n("whatsapp", WA_AUTO_DB),
                            blocked=helper_blocked(HELPER_WHATSAPP),
                            helper_current=helper_current(HELPER_WHATSAPP)))
    if _readable(AB_AUTO_DB):
        label = auto_copy_label(HELPER_CONTACTS)
        out.append(_finding("contacts", AB_AUTO_DB, label,
                            n("contacts", AB_AUTO_DB),
                            blocked=helper_blocked(HELPER_CONTACTS),
                            helper_current=helper_current(HELPER_CONTACTS)))
    # The helper's copy of the photo cache (NC-047). A DIRECTORY, unlike the three
    # database copies above, so it is offered whenever it holds anything at all.
    if _photo_cache_has_files(WA_PHOTOS_AUTO_DIR):
        out.append(_finding("whatsapp_photos", WA_PHOTOS_AUTO_DIR,
                            auto_copy_label(HELPER_WHATSAPP),
                            blocked=helper_blocked(HELPER_WHATSAPP),
                            helper_current=helper_current(HELPER_WHATSAPP)))
    # Apple Calendar's store (NC-042). No paste-it-yourself branch anywhere,
    # unlike the other three: the original is inside a group container, and
    # pointing a person at that in Finder is worse than the secret link it
    # replaces. Helper or nothing, which is why `blocked` matters more here.
    if _readable(CAL_AUTO_DB):
        out.append(_finding("calendar_local", CAL_AUTO_DB,
                            auto_copy_label(HELPER_CALENDAR),
                            blocked=helper_blocked(HELPER_CALENDAR),
                            helper_current=helper_current(HELPER_CALENDAR)))
    return out


def _readable(path: str) -> bool:
    try:
        return os.path.exists(path) and os.access(path, os.R_OK)
    except OSError:
        return False


def detect_sources() -> List[Dict[str, Any]]:
    """Everything importable found on this Mac right now, ready to run with a
    prefilled path. Read-only checks; anything unreadable is simply skipped."""
    import glob
    home = SCAN_HOME
    out: List[Dict[str, Any]] = []

    def readable(path: str) -> bool:
        try:
            return os.path.exists(path) and os.access(path, os.R_OK)
        except OSError:
            return False

    # WhatsApp Desktop's live container. Probed ONLY when the helper has not
    # already copied it, because the probe itself can raise a consent dialog —
    # see helper_covers_whatsapp(). This function backs the Data PAGE, so before
    # NC-047 that dialog could appear on an ordinary page load.
    if not helper_covers_whatsapp():
        wa_db = os.path.join(WA_CONTAINER, "ChatStorage.sqlite")
        wa_photos = os.path.join(WA_CONTAINER, "Media", "Profile")
        if readable(wa_db):
            out.append(_finding("whatsapp", wa_db, "WhatsApp Desktop on this Mac",
                                 _quick_count("whatsapp", wa_db)))
        if readable(wa_photos):
            out.append(_finding("whatsapp_photos", wa_photos,
                                 "WhatsApp Desktop's photo cache"))

    # Whatever is sitting in the import folder. Normally that is the copy the user
    # pasted there themselves; if the opt-in FDA helper is installed AND macOS is
    # letting it read, it refreshes the same file every couple of minutes.
    #
    # The LaunchAgent alone cannot tell a pasted copy from a helper's copy: it
    # says the helper is scheduled, not that it can read anything. The helper's
    # own report can, so ask that. See auto_copy_label().
    if readable(IMSG_AUTO_DB):
        label = auto_copy_label(HELPER_IMESSAGE)
        out.append(_finding("imessage", IMSG_AUTO_DB, label,
                             _quick_count("imessage", IMSG_AUTO_DB),
                            blocked=helper_blocked(HELPER_IMESSAGE),
                            helper_current=helper_current(HELPER_IMESSAGE)))
    # The helper writes WhatsApp here too. Listed BEFORE the WA_CONTAINER finding
    # above would be wrong — that one is the live original and is genuinely
    # fresher — but the Data page collapses to one row per source and prefers the
    # newest file, so both can be offered and the right one wins on mtime.
    if readable(WA_AUTO_DB):
        label = auto_copy_label(HELPER_WHATSAPP)
        out.append(_finding("whatsapp", WA_AUTO_DB, label,
                             _quick_count("whatsapp", WA_AUTO_DB),
                            blocked=helper_blocked(HELPER_WHATSAPP),
                            helper_current=helper_current(HELPER_WHATSAPP)))
    # The helper's Contacts copy. Listed here as well as in the unprotected scan
    # for the same reason the other two are: this function backs the page, that
    # one backs the timer, and a source only present in one of them either never
    # appears or never runs.
    if readable(AB_AUTO_DB):
        label = auto_copy_label(HELPER_CONTACTS)
        out.append(_finding("contacts", AB_AUTO_DB, label,
                             _quick_count("contacts", AB_AUTO_DB),
                            blocked=helper_blocked(HELPER_CONTACTS),
                            helper_current=helper_current(HELPER_CONTACTS)))

    # The helper's copy of the photo cache (NC-047). A DIRECTORY, unlike the three
    # database copies above, so it is offered whenever it holds anything at all.
    if _photo_cache_has_files(WA_PHOTOS_AUTO_DIR):
        out.append(_finding("whatsapp_photos", WA_PHOTOS_AUTO_DIR,
                            auto_copy_label(HELPER_WHATSAPP),
                            blocked=helper_blocked(HELPER_WHATSAPP),
                            helper_current=helper_current(HELPER_WHATSAPP)))
    # Apple Calendar's store (NC-042). No paste-it-yourself branch anywhere,
    # unlike the three above: the original lives in a group container, and
    # pointing a person at that in Finder is worse than the secret link it
    # replaces. Helper or nothing, which is why `blocked` carries more weight
    # here — there is no second route for the row to fall back to.
    if readable(CAL_AUTO_DB):
        out.append(_finding("calendar_local", CAL_AUTO_DB,
                            auto_copy_label(HELPER_CALENDAR),
                            blocked=helper_blocked(HELPER_CALENDAR),
                            helper_current=helper_current(HELPER_CALENDAR)))
    # Exports and previous manual copies in the usual spots.
    patterns = [
        ("imessage", ["Desktop/chat.db", "Downloads/chat.db"], "your chat.db copy"),
        ("whatsapp", ["Desktop/ChatStorage.sqlite", "Downloads/ChatStorage.sqlite"], "your ChatStorage copy"),
        ("whatsapp_photos", ["Desktop/Profile", "Downloads/Profile"], "your Profile folder copy"),
        ("contacts", ["Downloads/*.vcf", "Desktop/*.vcf"], "a Contacts export"),
        ("linkedin", ["Downloads/Connections*.csv", "Desktop/Connections*.csv"], "a LinkedIn export"),
        ("email", ["Downloads/*.mbox", "Desktop/*.mbox"], "a mailbox export"),
        # A calendar exported from Calendar.app or Google Takeout. Same glob
        # machinery as every line above it — no new probe, no new TCC surface.
        # Never auto-imported: handle_import_detect only marks the WhatsApp
        # container and the iMessage inbox `auto`, so this is offered, not run.
        ("calendar", ["Downloads/*.ics", "Desktop/*.ics"], "a calendar export"),
    ]
    try:
        for source, pats, label in patterns:
            hits: List[str] = []
            for pat in pats:
                hits += glob.glob(os.path.join(home, pat))
            for hit in sorted(hits, key=lambda p: -os.path.getmtime(p))[:2]:
                out.append(_finding(source, hit, f"{label} · {os.path.basename(hit)}",
                                    _quick_count(source, hit)))
    except OSError:
        pass  # Downloads/Desktop unreadable on this machine — fine, no findings
    return out


# ── "Open …" helpers: the button that replaces a paragraph of instructions ───
# Server-side allowlist (target key → Finder path or export URL). Only these
# targets are ever opened; the client sends just a key from this table, never a
# path. `for` attaches a target to a source whose key it doesn't share (the two
# iMessage buttons: copy from here, paste to there). `reveal` selects those
# names in Finder instead of only opening the folder; `mkdir` creates it first.
OPEN_TARGETS: Dict[str, Dict[str, Any]] = {
    "imessage": {"path": IMSG_SOURCE_DIR, "reveal": IMSG_COPY_NAMES,
                 "label": "Open the Messages folder"},
    "imessage_inbox": {"for": "imessage", "path": IMSG_INBOX, "mkdir": True,
                       "label": "Open the import folder"},
    # NC-079. Was ".../tools/oauth", which is the tools LANDING page — it lists
    # the OAuth tools and generates nothing. The generator is its own URL, and
    # landing a step short of it is the whole of "the link is to the wrong
    # place". A dead-link check cannot catch it: the landing page, the
    # generator and the inspector (see Microsoft's LinkedIn Developer Portal
    # Tools docs) each return 200.
    "linkedin": {"url": "https://www.linkedin.com/developers/tools/oauth/token-generator",
                 "label": "Open LinkedIn's token generator"},
    "email": {"url": "https://takeout.google.com/", "label": "Open Google Takeout"},
    # NC-117. The page where a sign-in client is actually made, not the console's
    # front door — the same distinction NC-079 turned out to matter for
    # LinkedIn's generator, where landing one step short WAS the whole bug. This
    # is Credentials under APIs & Services, which is where both the "Create
    # credentials" button and the download button for an existing client live.
    #
    # Keyed `gmail_console` rather than `gmail` deliberately: a target keyed
    # after a source becomes a button in that source's generic run row
    # (open_helpers_for), and this one belongs beside the steps that say what to
    # do when you get there, not fifteen lines below them. The Gmail panel calls
    # it by name. Same arrangement as `data` and the two `fda_` targets, which
    # are also reached by a panel rather than a row.
    "gmail_console": {"url": "https://console.cloud.google.com/apis/credentials",
                      "label": "Open Google Cloud Console"},
    # Where each calendar's link is copied from. The file-based .ics source gets
    # no button on purpose: Calendar.app's own path moved between macOS releases,
    # and a button that opens the wrong thing is worse than the written step.
    "calendar_google": {"url": "https://calendar.google.com/calendar/r/settings",
                        "label": "Open Google Calendar settings"},
    "calendar_icloud": {"url": "https://www.icloud.com/calendar/",
                        "label": "Open iCloud Calendar"},
    # NC-218. Where a key for Ask and AI-written facts is made. The old Settings
    # page named the site in words; the rebuilt one had only the paste box.
    "anthropic_keys": {"url": "https://console.anthropic.com/settings/keys",
                       "label": "Open Anthropic's key page"},
    # Not attached to any source: this one answers "where does my data actually
    # live", for backing it up or moving it to another Mac. Reached from the ⌘K
    # palette ("Show my data folder in Finder").
    "data": {"path": config.DATA_DIR, "reveal": ["crm.db", "archive.db"],
             "label": "Open my data folder"},
    # The two buttons the Full Disk Access panel on the Data page is built from.
    # macOS offers no API to REQUEST Full Disk Access — there is no prompt to
    # trigger, which is exactly why this needed a panel and not a toast. All the
    # app can do is put the user in front of the right pane with the right binary
    # already selected in Finder, so the remaining step is one drag.
    "fda_settings": {"url": "x-apple.systempreferences:"
                            "com.apple.preference.security?Privacy_AllFiles",
                     "label": "Open Full Disk Access settings"},
    "fda_binary": {"path": os.path.dirname(IMSG_HELPER_BIN),
                   "reveal": [os.path.basename(IMSG_HELPER_BIN)],
                   "label": "Show the helper in Finder"},
}


def resolve_open_target(key: str) -> Optional[Dict[str, Any]]:
    """An allowlisted target as ready-to-run argv, or None if `key` isn't on the
    list. `argv` is a chain: try each until one exits 0, so a reveal macOS
    refuses degrades to opening the plain folder.

    `open -R a b c` asks Finder to select those files, which works on
    ~/Library/Messages even though the app cannot read a byte of it — Finder
    does the revealing, and existence checks aren't TCC-protected."""
    target = OPEN_TARGETS.get(key)
    if target is None:
        return None
    label = str(target.get("label", ""))
    url = str(target.get("url") or "")
    if url:
        return {"label": label, "argv": [["open", url]]}
    path = str(target.get("path") or "")
    if target.get("mkdir") and path:
        # 0700 — the import folder ends up holding a copy of every message the
        # user has ever sent. Failure is not fatal: Finder reports it instead.
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError:
            pass
    argv: List[List[str]] = []
    reveal = [os.path.join(path, name) for name in target.get("reveal", ())]
    reveal = [p for p in reveal if os.path.exists(p)]
    if reveal:
        argv.append(["open", "-R"] + reveal)
    argv.append(["open", path])
    return {"label": label, "argv": argv}


def open_helpers_for(source: str) -> List[Dict[str, str]]:
    """Every allowlisted target belonging to this source, in button order."""
    return [{"key": key, "label": str(target.get("label", ""))}
            for key, target in OPEN_TARGETS.items()
            if str(target.get("for", key)) == source]


def list_sources() -> List[Dict[str, Any]]:
    """Frontend-facing shape for GET /api/import/sources — no build_cmd (not JSON-safe)."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "category": spec.category,
            "kind": spec.kind,
            "pick_kind": spec.pick_kind,
            "export_help": spec.export_help,
            # open_helper: the first button, kept for the onboarding wizard;
            # open_helpers: all of them (iMessage has two — copy from, paste to)
            "open_helper": OPEN_TARGETS.get(spec.key, {}).get("label", ""),
            "open_helpers": open_helpers_for(spec.key),
            "supports_dry_run": spec.supports_dry_run,
            # False = this source cannot change your database. No source is
            # False today. Additive: a frontend that ignores the key behaves
            # exactly as before, and one that reads it can warn before a write
            # instead of promising one that cannot happen.
            "writes": spec.writes,
            # "" when nothing is required; otherwise the setting's name, and
            # whether it currently holds a value. Read at call time, so setting
            # it in Settings makes the row live without a restart.
            "needs": spec.needs,
            "configured": (not spec.needs
                           or bool(os.environ.get(spec.needs, "").strip())),
        }
        for spec in REGISTRY.values()
    ]
