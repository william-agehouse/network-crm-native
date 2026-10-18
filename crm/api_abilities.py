#!/usr/bin/env python3
"""The rebuilt Settings page's one answer — NC-114, built with NC-088.

A route-registry module serving GET /api/settings/ledger: the eight things the app
can do that are off until you give it something, the "Needs you" list above
them, and what this install is. The same shape as the Data page's
/api/data/ledger (crm/api_ledger.py), drawn by the same parts
(static/app/ledger.js), so the two pages are learned once.

WHAT A SETTINGS ROW IS
One rule sorts every control on both pages, checked in order:

  Needs you      if the app can name, right now, the one thing it cannot do
                 without you
  the ledger     if it belongs to exactly one of the things this page keeps a
                 row for
  This install   if it is read-only, or hands you a copy of what you have, and
                 describes the whole install
  the other page if it belongs to no single row here

A Data row is a thing that brings information in. A Settings row is a thing the
app can do, for you or on its own, that is off until you give it something.
Eight of them: the scrape switch lives on its LinkedIn row on Data (NC-234),
setting up again lives in This install (NC-235), and the dates look-ahead is a
row of its own (NC-236). The six per-source credentials the old Settings page
held (the LinkedIn token, Gmail accounts, the scraper's Mac and key, the two
calendar addresses) live on their sources' rows on Data, with no copy here —
"every source is always listed" on Data removes the only reason a copy existed,
and a read-only copy of a credential would bring back the LinkedIn-token confusion.

Kept in code, with no box: the other MANAGED_KEYS (per-source import switches,
schedules). The rule's last clause is what cut them — another control on the
page already answers the question each one asks — and they are still settable
in secrets/.env. The spend cap and the two prices were in that list until
NC-364: they are money a person can feel, so each has a box on the AI row,
beside the key and the model.

Nothing is decided on the page. Each row's words come from here, and every fact
behind them from the module that owns it: settings (what is set), owner (what
you are working on), api_instance (identity, dependencies, the search install,
first-run setup), updates (the version).
"""

import logging
import os
from typing import Any, Dict, List

import api_dates
import api_instance
import api_ledger
import owner
import settings
import updates

log = logging.getLogger("networkcrm")

ROUTES = {"GET": {"/api/settings/ledger": "handle_settings_ledger"}}


def _on(key: str) -> bool:
    """A switch's value, with the default settings.py declares for it."""
    default = str((settings.MANAGED_KEYS.get(key) or {}).get("default", "0"))
    return (os.environ.get(key, default) or default).strip() == "1"


def _row(key: str, label: str, gives: str) -> Dict[str, Any]:
    return {"key": key, "label": label, "gives": gives,
            "have": {"says": "", "figures": []}, "takes": "",
            "cost": api_ledger._cost("free"),
            "state": {"tone": "off", "says": ""}, "in_use": False,
            "detail": {"runs": "", "last_run": "", "last_run_caveat": "",
                       "help": "", "manual": "", "acts": [], "provenance": ""}}


def _switch(key: str, label: str) -> Dict[str, Any]:
    return {"kind": "switch", "key": key, "label": label, "on": _on(key)}


def _identity() -> Dict[str, Any]:
    out = _row("identity", "Know which messages are yours",
               "Every import can tell your side of a conversation from theirs, "
               "and drafts are written as you.")
    name = (os.environ.get("SELF_NAME") or "").strip()
    email = (os.environ.get("SELF_EMAIL") or "").strip()
    out["takes"] = "Your name and email address, typed once."
    status = api_instance.identity_status()
    out["in_use"] = bool(name or email)
    if status.get("ok"):
        out["state"] = {"tone": "ok", "says": "Set"}
    else:
        # The row says it short; "Needs you" carries the whole sentence, so the
        # page does not print it twice. Waiting for you, not "Stopped": nothing
        # that never started has stopped (NC-218).
        out["state"] = {"tone": "ready", "says": "Not set"}
        out["need_says"] = status.get("message") or "Your name and email are not set."
    out["have"]["says"] = " · ".join(v for v in (name, email) if v)
    out["detail"]["acts"] = [
        api_ledger._text("SELF_NAME", "Your name", "As people write it to you"),
        api_ledger._text("SELF_EMAIL", "Your email addresses", "you@example.com, you@work.example"),
    ]
    # Under How: these are reasons, not another way in (NC-218).
    out["detail"]["help"] = ("Your name is how the chat apps' exports label your "
                             "own messages. Your email is how mailbox and "
                             "calendar files mark yours.")
    return out


def _goals(conn: Any) -> Dict[str, Any]:
    out = _row("goals", "Answers that know what you are working on",
               "Ask, meeting briefs and drafted messages take into account what you "
               "are working on, which nothing can work out from your messages.")
    text = (owner.load(conn).get("goals") or "").strip()
    out["takes"] = "A paragraph in your own words, written once. Free."
    out["in_use"] = bool(text)
    out["state"] = {"tone": "ok", "says": "Written"} if text else {"tone": "off", "says": ""}
    words = len(text.split())
    out["have"]["says"] = (f"{words:,} {'word' if words == 1 else 'words'}" if text
                           else "Not written yet")
    out["detail"]["acts"] = [{"kind": "goals"}]
    return out


def _autosync() -> Dict[str, Any]:
    out = _row("autosync", "Keep your data up to date",
               "New copies are imported as they appear, and LinkedIn, Gmail and "
               "the calendar links are checked on a schedule.")
    on = _on("NC_AUTOSYNC")
    out["takes"] = ("Nothing. It is on unless you switch it off." if on else
                    "Switching it on. While it is off, nothing imports until you "
                    "press Import on the Data page.")
    out["in_use"] = on
    out["state"] = ({"tone": "ok", "says": "On"} if on else
                    {"tone": "paused", "says": "Off: nothing imports until you press Import"})
    out["have"]["says"] = "On" if on else ""   # off: the paused line under it says it
    out["detail"]["acts"] = [_switch("NC_AUTOSYNC", "Keep your data up to date")]
    return out


def _intake_web() -> Dict[str, Any]:
    out = _row("intake_web", "Intake can read a link you paste",
               "When you paste a link into Intake, it opens the page and files "
               "what it finds about the person.")
    on = _on("NC_INTAKE_WEB")
    out["takes"] = "Your yes to fetching a web page each time you paste a link."
    out["in_use"] = on
    out["state"] = {"tone": "ok", "says": "On"} if on else {"tone": "off", "says": ""}
    out["have"]["says"] = "On" if on else "Off"
    out["detail"]["acts"] = [_switch("NC_INTAKE_WEB", "Let Intake read a web page")]
    out["detail"]["help"] = ("While this is off, Intake reads only the text you "
                             "paste, and a link stays a link.")
    return out


def _dates() -> Dict[str, Any]:
    """NC-236: the old Settings page's box for how far
    ahead the dates above the People table look would have gone with that page.
    It was kept, as a row of its own. It is the one number here, and it
    is on the page because it is a limit you can see the effect of (NC-040)."""
    out = _row("dates", "Birthdays and dates coming up",
               "Birthdays, anniversaries and meetings coming up, in a strip above "
               "the People table.")
    days = api_dates._dates_lookahead()
    out["takes"] = "Nothing: it is on. You can change how far ahead it looks."
    out["in_use"] = True
    out["state"] = {"tone": "ok", "says": "On"}
    out["have"]["says"] = f"Looks {days:,} {'day' if days == 1 else 'days'} ahead"
    out["detail"]["acts"] = [dict(api_ledger._text(
        "NC_DATES_LOOKAHEAD_DAYS", "How many days ahead to look, from 1 to 365", "14"),
        name="Days ahead")]
    out["detail"]["help"] = ("The strip shows the next three, and says how many more "
                             "there are in that many days. Empty, it looks 14 days "
                             "ahead.")
    return out


def _ics_fetch() -> Dict[str, Any]:
    out = _row("ics_fetch", "Download the calendars you linked",
               "The Google and iCloud calendar addresses you pasted on the Data "
               "page are fetched, so their meetings come in.")
    on = _on("NC_ICS_FETCH")
    linked = [n for k, n in (("NC_ICS_GOOGLE_URL", "Google"), ("NC_ICS_ICLOUD_URL", "iCloud"))
              if (os.environ.get(k) or "").strip()]
    out["takes"] = "Your yes to reaching those two addresses over the internet."
    out["in_use"] = on
    if on and not linked:
        out["state"] = {"tone": "off", "says": "On, but no calendar is linked yet"}
    elif on:
        out["state"] = {"tone": "ok", "says": "On"}
    out["have"]["says"] = ("On" if on else "Off") + (
        f" · {' and '.join(linked)} linked" if linked else "")
    out["detail"]["acts"] = [
        _switch("NC_ICS_FETCH", "Download linked calendars"),
        {"kind": "page", "page": "data", "row": "calendar_google",
         "label": "Open the calendar links on the Data page"},
    ]
    return out


def _ai() -> Dict[str, Any]:
    out = _row("ai", "Ask, drafts and facts written by AI",
               "Questions about your own network, drafts in your voice, and short "
               "summaries of people.")
    key_set = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    cap = api_ledger.api_import_cap()
    out["takes"] = (f"A key from Anthropic, the company that makes Claude, pasted "
                    f"here. Anthropic charges your account for each question, draft "
                    f"or summary. A run of AI-written facts covers at most "
                    f"{cap:,} {'person' if cap == 1 else 'people'}.")
    out["cost"] = api_ledger._cost("money")
    out["in_use"] = key_set
    model = (os.environ.get("ANTHROPIC_MODEL")
             or str((settings.MANAGED_KEYS.get("ANTHROPIC_MODEL") or {}).get("default", ""))).strip()
    out["state"] = {"tone": "ok", "says": "Connected"} if key_set else {"tone": "off", "says": ""}
    out["have"]["says"] = f"Connected · {model}" if key_set else "Not set"
    acts: List[Dict[str, Any]] = [api_ledger._secret("ANTHROPIC_API_KEY", "Key from Anthropic",
                                                     "sk-ant-…")]
    if not key_set:
        # NC-218. The box alone was a dead end: nothing said where a key comes from.
        acts.append({"kind": "open", "target": "anthropic_keys",
                     "label": "Open Anthropic's key page"})
    if key_set:
        acts.append({"kind": "model", "key": "ANTHROPIC_MODEL", "value": model})
        acts.extend(_money_boxes())
        out["detail"]["help"] = (
            "The most people per paid run caps one press of Facts written by AI, "
            "and of Read shared history on the LinkedIn profile facts row, both on "
            "the Data page. Each person is one paid request; the rest wait for the "
            "next press.\n\n"
            "The two prices are what the app assumes Anthropic charges for the "
            "model above, per million tokens (a token is a short piece of a word). "
            "They are used only to show what each Ask answer and each Intake "
            "proposal cost, and change nothing Anthropic bills. If you choose "
            "another model, or Anthropic changes its prices, copy that model's two "
            "prices from Anthropic's pricing page.\n\n"
            "An empty box uses the number shown in it.")
    out["detail"]["acts"] = acts
    out["detail"]["manual"] = ("Without a key, Ask still answers from rules on this "
                               "Mac, and nothing costs money.")
    return out


def _money_boxes() -> List[Dict[str, Any]]:
    """NC-364. The three numbers on this page that are money: the most people one
    paid run may send, and the two prices the cost under each Ask answer is
    worked out from. Each says what it does and its bounds; settings.VALIDATORS
    refuses anything outside them with a sentence, which the box shows. The
    grey number in an empty box is the default (settings.MANAGED_KEYS)."""
    def default(key: str) -> str:
        return str(settings.MANAGED_KEYS[key]["default"])
    lo, hi, top = settings.SPEND_CAP_MIN, settings.SPEND_CAP_MAX, settings.PRICE_MAX
    return [
        dict(api_ledger._text(
            "ENRICH_LLM_MAX",
            f"Most people one paid run may send to Anthropic, from {lo} to {hi:,}",
            default("ENRICH_LLM_MAX")),
            name="Most people per paid run"),
        dict(api_ledger._text(
            "NC_PRICE_IN_PER_MTOK",
            f"Price per million tokens Anthropic reads, in US dollars, from 0 to {top:,}",
            default("NC_PRICE_IN_PER_MTOK")),
            name="Price of what it reads"),
        dict(api_ledger._text(
            "NC_PRICE_OUT_PER_MTOK",
            f"Price per million tokens Anthropic writes, in US dollars, from 0 to {top:,}",
            default("NC_PRICE_OUT_PER_MTOK")),
            name="Price of what it writes"),
    ]


def _semantic() -> Dict[str, Any]:
    out = _row("semantic", "Find people by meaning, not just words",
               "“Who works in climate” finds people who never used the word.")
    st = api_instance.semantic_search_status()
    ins = st.get("install") or {}
    gb = st.get("size_gb") or 2
    out["takes"] = f"A one-time download of about {gb} GB."
    cost = api_ledger._cost("wait")
    cost["label"] = f"{gb} GB download"
    out["cost"] = cost
    if st.get("available"):
        out["in_use"] = True
        out["state"] = {"tone": "ok", "says": "On"}
        out["have"]["says"] = "On"
        # NC-218. The cost must not still read "2 GB download" in gold after the
        # download.
        out["takes"] = "Nothing more. It is installed."
        out["cost"] = api_ledger._cost("free")
    elif ins.get("state") == "running":
        out["in_use"] = True
        out["state"] = {"tone": "behind", "says": "Installing"}
    elif ins.get("state") == "succeeded" and ins.get("restart_required"):
        out["in_use"] = True
        out["state"] = {"tone": "behind", "says": "Installed. Quit and reopen the app to turn it on"}
    elif ins.get("state") == "failed":
        out["in_use"] = True
        out["state"] = {"tone": "blocked", "says": "The install stopped"}
    else:
        out["state"] = {"tone": "off", "says": "Off: keyword search still works"}
        out["have"]["says"] = "Off: keyword search still works"
    out["detail"]["acts"] = [{"kind": "semantic"}]
    return out


def _safe_done(conn: Any) -> bool:
    """Whether first-run setup is done, counting a failed read as done.

    NC-350. A failed read used to count as not done, which offered setup again
    beside a page that otherwise works. The web page has always treated a failed
    read of the same flag as done, so the two now agree: offering setup again
    is the one answer that would be wrong to give by mistake."""
    try:
        return api_instance.onboarding_done(conn)
    except Exception:  # noqa: BLE001 — a line in This install must not blank the page
        return True


def _needs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the app cannot do without you, right now. Three things only."""
    by_key = {r["key"]: r for r in rows}
    out: List[Dict[str, Any]] = []
    ident = by_key["identity"]
    if ident["state"]["tone"] == "ready":
        out.append({"row": "identity", "tone": "ready",
                    "says": ident.get("need_says") or ident["state"]["says"],
                    "cost": "free",
                    "acts": [{"kind": "row", "row": "identity", "label": "Set your name and email"}]})
    deps = api_instance.deps_status()
    if not deps.get("ok"):
        # NC-218. Say what to do, not the marker's own text (which can be
        # "dependency status file is malformed"). The app does retry on the next
        # open (crm/python_env.py, the marker's "retry" line).
        # NC-236: the line also names the install's log, the only record of why
        # it failed.
        log_file = str(deps.get("log_file") or "")
        out.append({"row": "", "tone": "blocked", "cost": "free", "acts": [],
                    "says": "Some optional parts of the app did not finish installing. "
                            "Everything else works without them. Quit and reopen the "
                            "app while connected to the internet to try again."
                            + (f" What went wrong is written in {log_file}." if log_file else "")})
    sem = by_key["semantic"]
    if sem["state"]["tone"] in ("behind", "blocked") and sem["state"]["says"] != "Installing":
        out.append({"row": "semantic", "tone": sem["state"]["tone"],
                    "says": f"Meaning search: {sem['state']['says']}.", "cost": "free",
                    "acts": [{"kind": "row", "row": "semantic", "label": "Open it"}]})
    return out


def _install(conn: Any) -> Dict[str, Any]:
    return {
        # NC-235: setting up again is not something the app
        # does for you, so it left the list of abilities for This install.
        "setup_again": {"done": bool(_safe_done(conn))},
        "version": updates.CURRENT_VERSION,
        # NC-118: no update controls until releases are published. The honest
        # line is the whole feature. NC-315: a handed-out copy says where its new
        # versions are published, without going online to say it. Corrected: it
        # used to say "Checks GitHub for a newer version once a day", but nothing
        # in either app asks GET /api/update, so no check ever ran.
        "updates_says": updates.channel_says(),
        "data_folder": {"kind": "open", "target": "data", "label": "Show the data folder"},
    }


# The eight rows, in the page's order, including the dates row (NC-236). Key
# and label live here as well as in each builder so a row whose facts cannot
# be read is still listed.
ROWS = [
    ("identity", "Know which messages are yours", lambda conn: _identity()),
    ("goals", "Answers that know what you are working on", _goals),
    ("autosync", "Keep your data up to date", lambda conn: _autosync()),
    ("intake_web", "Intake can read a link you paste", lambda conn: _intake_web()),
    ("ics_fetch", "Download the calendars you linked", lambda conn: _ics_fetch()),
    ("dates", "Birthdays and dates coming up", lambda conn: _dates()),
    ("ai", "Ask, drafts and facts written by AI", lambda conn: _ai()),
    ("semantic", "Find people by meaning, not just words", lambda conn: _semantic()),
]


def handle_settings_ledger(conn: Any) -> Dict[str, Any]:
    """GET /api/settings/ledger — the eight abilities, what needs you, this install."""
    rows = []
    for key, label, build in ROWS:
        try:
            rows.append(build(conn))
        except Exception:  # noqa: BLE001 — one unhappy owner must not blank the page
            log.debug("api_abilities: %s failed", key, exc_info=True)
            row = _row(key, label, "")
            row["takes"] = "Could not be read just now."
            row["state"] = {"tone": "na", "says": "Could not be read just now"}
            rows.append(row)
    return {"rows": rows, "needs": _needs(rows), "install": _install(conn)}
