#!/usr/bin/env python3
"""One honest row per import source: how it runs, whether it is connected, when
it last ran.

Read directly by the Data page's one answer (crm/api_ledger.py). It served
GET /api/sources until NC-237 took that address away.

WHY THIS EXISTS
A single "last sync" figure in Settings cannot say which sources are current
and which are not, so the page shows one row per source: its state, and whether
it has been updated.

The single figure it replaces was NC-094's, and NC-094 was itself a repair —
before it, one source that had just run made every source that had not run for
a long time read as fresh. Naming the stalest source was honest but still one
number speaking for every source. This is every source.

THE THING THIS PAGE HAS TO GET RIGHT
"Stale" is only a criticism if something was supposed to have run. Five of these
sources have no clock at all and never did: the app cannot start them, and
saying "6 weeks ago" beside one of them accuses the app of neglect it is not
capable of. The same point holds for the LinkedIn profile scraper: it runs on its own
schedule, and the import of its results is sometimes run by hand.

That is right, and it goes further. `merge_scraped_facts.py` is
reachable from ONE place in this repo — importers._cmd_linkedin_facts, behind
the Data page's Run button — and appears in neither API_SOURCES nor
auto_import_paths(). The scraping itself runs wherever it was set up (typically a
LaunchAgent on a second Mac) and this repository has no code path to start,
schedule, or observe it. So `runs` below is a first-class field, and a source that
only the user can start is reported as waiting for them, never as overdue.

WHERE THE FACTS COME FROM, AND WHY NOTHING IS REIMPLEMENTED HERE
Every field is read from the module that already owns it, because a second
opinion about whether Gmail is configured is exactly the defect NC-087 is about:

  importers.REGISTRY          label, category, and `needs`
  api_import.API_SOURCES      which sources run on a clock, and how often
  api_import.auto_import_paths()   which sources import themselves when a file
                              changes — its own docstring calls it "the single
                              source of truth for is this source automatic"
  api_import._source_blockers()    why a source cannot collect right now
  db.state_get                the freshness stamps

WHAT THIS CANNOT KNOW, AND SAYS SO
Four things this page cannot know, each reported rather than smoothed over —
the residue idea `crm/intake.py` established and NC-087 says to generalise:

  * `google_photos` needs Gmail's OAuth client file, which is a FILE and so
    cannot be an ImporterSpec.needs (that field names an environment variable).
    Checked here explicitly, with the check named in the payload.
  * Four sources have no `import_synced_*` row on an install that has not run them, so
    "never set up" and "set up but never finished a run" look identical. Both
    report last_run "" with known=false, and the difference is stated as unknown
    rather than guessed.
  * `facts`, `style` and `engagement` each have TWO stamps written by two
    different paths — the Data-page button writes import_synced_<key>, the
    background chain writes <name>_chain_last — and nothing reconciles them.
    Reading either alone understates how recently the pass ran, so this takes
    the later of the two and says which it used.
  * `linkedin` has one stamp for two different routes (the token-backed API pull
    and the no-token CSV seed). api_linkedin.py:237 already documents that the
    stamp "cannot say which of the two last ran"; that ambiguity is passed
    through, not resolved by inventing a distinction.
"""

import logging
import os
from typing import Any, Dict, List

import api_import
import db
import importers

log = logging.getLogger("networkcrm")

# NC-237. This module used to serve GET /api/sources. Only the old Data and
# Settings pages asked it, so the address is gone; _sources is what the Data
# page's one answer (api_ledger) reads, and handle_sources keeps its summary.

STAMP_PREFIX = "import_synced_"

# The second stamp, for the three passes that have one. Key -> state key written
# by the background chain rather than by the Data-page button. Kept here rather
# than imported so the mapping is visible at the place that must reconcile it;
# the values are asserted against api_import's own constants by the tests.
CHAIN_STAMPS = {
    "facts": "facts_chain_last",
    "style": "style_chain_last",
}

# Prerequisites that are real but cannot be expressed as ImporterSpec.needs,
# which names an environment variable. Only file-shaped ones live here.
#
# google_photos reuses Gmail's OAuth client file. Without it the importer exits
# with "Missing … set up Gmail import first" from inside the subprocess, which
# is a message nobody sees until they have already pressed the button — so the
# row said "connected" right up to the point it failed.
FILE_PREREQS = {
    "google_photos": ("Gmail's Google sign-in file",
                      "Needs the Gmail import set up first — it reuses that login"),
}

# What a missing setting is called, for a person rather than for the .env file.
#
# The row read "NC_ICS_GOOGLE_URL is not set", which names the identifier and not
# the thing: a person needs plain words first, and the identifier afterwards only if
# it earns its place. The key stays in the payload under `needs` for anyone who wants
# to go and edit it; the SENTENCE says what is missing.
#
# Keyed by environment variable rather than by source, because two sources can
# want the same one and the phrase should not drift between them.
NEEDS_SAYS = {
    "NC_ICS_GOOGLE_URL": "No Google Calendar address yet",
    "NC_ICS_ICLOUD_URL": "No iCloud Calendar address yet",
    "GMAIL_ACCOUNTS": "No Gmail account set",
    "LINKEDIN_TOKEN": "No LinkedIn token",
    "IMAC": "No scraper machine set",
    "ANTHROPIC_API_KEY": "No Anthropic API key",
}

# Sources whose freshness stamp is known to UNDERSTATE how recently they ran, and
# why. Not a bug being hidden — a limit being published, the way crm/intake.py
# publishes its residue instead of quietly absorbing it.
#
# engagement.refresh() is called at startup and after every autosync batch
# (api_import.py:1267-1268) WITHOUT restamping import_synced_engagement, which
# only handle_import_run writes. So the date is the last time a person pressed the
# button, and the pass itself has almost certainly run since.
STAMP_CAVEATS = {
    # No dash in here: the sentence already arrives after one ("Last recorded
    # 18d ago — ..."), and two em-dashes in one line stops reading as a
    # correction and starts reading as an aside inside an aside.
    "engagement": "it also runs after every import without recording that, so "
                  "almost certainly more recently than this",
}

# What a person can do about a source, in the order the answer changes. Not a
# severity ramp: "you run this one" is not worse than "runs by itself", it is a
# different kind of thing, and the display groups by it rather than sorting on it.
RUNS_WATCHED = "watched"    # imports itself when its copy on disk changes
RUNS_CLOCK = "clock"        # imports itself on a timer
RUNS_CHAINED = "chained"    # runs after an import that brought new data
RUNS_MANUAL = "manual"      # only when a person starts it

# The three enrichment passes that run off the back of an import rather than on
# a clock of their own. engagement is unconditional after every autosync batch;
# facts and style are gated by their chain timers.
CHAINED = {"engagement", "facts", "style"}


def _gmail_credentials_present() -> bool:
    """Is there a Google OAuth client file for Gmail to reuse?

    Never raises and never touches anything outside this app's own secrets
    directory — a probe that fails reports False, because inventing a
    prerequisite is worse than missing one (the rule _source_blockers states).
    """
    try:
        import config
        d = config.SECRETS_DIR
        return any(n.startswith("gmail_credentials") and n.endswith(".json")
                   for n in os.listdir(d))
    except Exception:  # noqa: BLE001
        return False


def _how_it_runs(key: str, auto_paths: Dict[str, str],
                 clock: Dict[str, Any]) -> Dict[str, Any]:
    """Which of the four ways this source can start, and in plain words.

    Derived from the two structures that DECIDE it — auto_import_paths() and
    API_SOURCES — never from a list kept here, so a source that gains or loses a
    clock cannot leave this description behind.
    """
    if key in auto_paths:
        return {"runs": RUNS_WATCHED, "runs_says": "Imports itself when new data appears",
                "off_switch": auto_paths[key]}
    if key in clock:
        spec = clock[key]
        hours = int(os.environ.get(spec.get("hours_env", ""), "") or spec.get("hours", 24))
        every = "every hour" if hours == 1 else f"every {hours} hours"
        return {"runs": RUNS_CLOCK, "runs_says": f"Checks {every} on its own",
                "off_switch": spec.get("switch", ""), "every_hours": hours}
    if key in CHAINED:
        return {"runs": RUNS_CHAINED,
                "runs_says": "Runs after an import brings something new",
                "off_switch": "NC_AUTOSYNC"}
    return {"runs": RUNS_MANUAL, "runs_says": "Only when you press Import",
            "off_switch": ""}


def _connected(spec: Any, key: str, clock: Dict[str, Any]) -> Dict[str, Any]:
    """Is this source set up? Three answers, and the third is a real one.

    True / False / None, where None means the app genuinely cannot tell. A
    source with no prerequisite of any kind is not "configured", it is
    "nothing to configure" — reporting that as True is how `list_sources()`
    ends up calling an unconfigured Gmail connected.
    """
    # An environment variable the registry names, or that the clock does. Both
    # are consulted because they disagree: gmail's GMAIL_ACCOUNTS is named only
    # in API_SOURCES, so ImporterSpec.needs alone reports gmail configured on an
    # install that has never set it.
    needs = (getattr(spec, "needs", "") or "").strip()
    if not needs and key in clock:
        needs = (clock[key].get("needs") or "").strip()
    if needs:
        val = (os.environ.get(needs, "") or "").strip()
        return {"connected": bool(val), "needs": needs,
                "connected_says": "" if val else
                NEEDS_SAYS.get(needs, f"{needs} has no value yet")}
    if key in FILE_PREREQS:
        label, why = FILE_PREREQS[key]
        ok = _gmail_credentials_present()
        return {"connected": ok, "needs": label,
                "connected_says": "" if ok else why}
    return {"connected": None, "needs": "",
            "connected_says": "Nothing to set up — it works or it does not"}


def _last_run(conn: Any, key: str) -> Dict[str, Any]:
    """When this source last finished, taking the LATER of its two stamps.

    facts and style are written by two different paths that never reconcile:
    the Data-page button stamps import_synced_<key>, the background chain stamps
    <key>_chain_last. import_synced_facts can hold an old date while the chain
    has in fact run far more recently — so a display reading one stamp would
    call a working pass weeks stale.

    `from` names which stamp won, so the answer can be checked rather than
    trusted.
    """
    stamps = {"button": db.state_get(conn, STAMP_PREFIX + key, "") or ""}
    chain_key = CHAIN_STAMPS.get(key)
    if chain_key:
        stamps["chain"] = db.state_get(conn, chain_key, "") or ""
    best = max(stamps.items(), key=lambda kv: kv[1])
    return {"last_run": best[1], "last_run_from": best[1] and best[0],
            "last_run_known": bool(best[1]),
            "last_run_caveat": STAMP_CAVEATS.get(key, ""),
            "stamps": {k: v for k, v in stamps.items() if v}}


def _sources(conn: Any) -> List[Dict[str, Any]]:
    auto_paths: Dict[str, str] = {}
    try:
        # (path, switch, fp_key) -> the registry key that owns each path. Matched
        # through importers' own module attributes rather than by parsing the
        # path, so a moved directory cannot silently unclassify a source.
        owners = {
            importers.IMSG_AUTO_DB: "imessage",
            importers.WA_AUTO_DB: "whatsapp",
            importers.AB_AUTO_DB: "contacts",
            importers.WA_PHOTOS_AUTO_DIR: "whatsapp_photos",
            importers.CAL_AUTO_DB: "calendar_local",
        }
        for path, switch, _fingerprint_key in api_import.auto_import_paths():
            key = owners.get(path)
            if key:
                auto_paths[key] = switch
    except Exception:  # noqa: BLE001 — a status page must never 500
        log.debug("api_sources: could not read auto_import_paths", exc_info=True)

    clock = getattr(api_import, "API_SOURCES", {}) or {}
    try:
        # NC-177: source_blockers(), not _source_blockers(). The probes alone
        # leave out the sign-ins waiting on a person, and this ledger is where
        # Gmail actually appears on the Data page — the row list above it does
        # not include Gmail at all on a fresh install.
        blockers = api_import.source_blockers(conn)
    except Exception:  # noqa: BLE001
        blockers = {}

    out: List[Dict[str, Any]] = []
    for key, spec in importers.REGISTRY.items():
        row: Dict[str, Any] = {
            "key": key,
            "label": spec.label,
            "category": spec.category,
        }
        row.update(_how_it_runs(key, auto_paths, clock))
        row.update(_connected(spec, key, clock))
        row.update(_last_run(conn, key))
        row["blocked"] = blockers.get(key, "")
        out.append(row)
    return out


def handle_sources(conn: Any) -> Dict[str, Any]:
    """Every source, how it runs, and whether it has run.

    The summary counts deliberately do NOT partition into good and bad. A source
    that only starts when a person starts it is not failing by sitting still,
    and the count that would say otherwise is the one this endpoint exists to
    stop being drawn.
    """
    rows = _sources(conn)
    collecting = [r for r in rows if r["category"] == "import"]
    return {
        "sources": rows,
        "population": "every source in the importer registry, collecting and "
                      "enriching alike",
        "summary": {
            "total": len(rows),
            "collecting": len(collecting),
            "enriching": len(rows) - len(collecting),
            "automatic": sum(1 for r in rows
                             if r["runs"] in (RUNS_WATCHED, RUNS_CLOCK, RUNS_CHAINED)),
            "yours": sum(1 for r in rows if r["runs"] == RUNS_MANUAL),
            # Rendered as "set up" (NC-207): a source that needs a setting and
            # has it. The setup panel's "sources connected" is a different
            # count — sources that have brought data in.
            "connected": sum(1 for r in rows if r["connected"] is True),
            "not_connected": sum(1 for r in rows if r["connected"] is False),
            "nothing_to_connect": sum(1 for r in rows if r["connected"] is None),
            "blocked": sum(1 for r in rows if r["blocked"]),
            # Never run here. Reported rather than folded into "not connected",
            # because they are different states and the app genuinely cannot
            # tell them apart for four of these sources.
            "never_run": sum(1 for r in rows if not r["last_run_known"]),
        },
    }
