#!/usr/bin/env python3
"""Setup progress: what each step has actually bought the user so far.

WHY THIS EXISTS
---------------
Setup is the only friction between installing this app and it being useful, and
every step in it costs the user something real — a permission dialog, a trip to
System Settings, minutes in a developer console. People abandon multi-step
setup when the payoff is invisible, so this endpoint makes the payoff countable:
after each step, exactly what it added, and what the next one will add.

The honest metric is `history` — people you have a real conversation with. It is
deliberately NOT the contact count, because the contact count is the flattering
number. Importing Apple Contacts can add thousands of names and move `history` by
zero. That gap is the product's whole argument: an address book is not a memory.

A flat crm/api_* sibling. It had a ROUTES table until NC-237 removed its one
address, which only the old Data page asked. Read-only: it runs SELECTs and
nothing else.
"""
import os
from http import HTTPStatus
from typing import Any, Dict, List

import db
import archive as arc
import figures
import importers
from api_core import ApiError

# NC-237. This module used to declare ROUTES = {"GET": {"/api/setup/progress":
# ...}}. Only the old Data page asked that address, and it is gone, so the
# address went with it. handle_setup_progress below is still the one place the
# per-step contribution is worked out, and the Data page's answer (api_ledger)
# reads _archive_messages_by_channel from here directly.


# The order here is the order of the runbook, which is ordered by yield per
# second of the user's attention — not by how the code is arranged. `metric` is
# the key in db.source_coverage() that proves the step landed.
#
# `unit` is what the number counts, and it is not always people: a step that adds
# depth to people you already have needs to say so, or the panel looks broken
# when the headline number doesn't move.
#
# PRICES (NC-186). `cost` is what the step costs once nothing stands in its way.
# For the four sources the sync helper copies (`helper` is the name it reports
# them under), that is only true while the helper is actually copying them —
# otherwise Apple Contacts and Calendar would read "one tap" on a fresh install
# where the tap needs a Full Disk Access grant nobody has given yet, and the
# grant would be priced rows later, at iMessage. _price() below decides the real
# price from the helper's own report; `grant` marks the sources that need Full
# Disk Access (WhatsApp does not), `manual` is the short price of the way in that
# needs no permission, and `manual_how` is that way in, in words — the panel
# names it on the next step's row, because invariant 6 says the help describes
# both routes, not only the simple one.
STEPS: List[Dict[str, Any]] = [
    {
        "key": "contacts", "label": "Apple Contacts", "metric": "people",
        "unit": "people", "cost": "one tap",
        "gives": "The names, numbers and emails everything else attaches to.",
        "helper": importers.HELPER_CONTACTS, "grant": True,
        "manual": "export by hand",
        "manual_how": "export a card file: in Contacts choose File → Export → "
                      "Export vCard…, then pick it in the Apple Contacts row below",
    },
    {
        # NC-076: the unit named a population the SQL did not count. `meetings`
        # was every row in contact_dates, so hand-typed birthdays and address-book
        # birthdays counted as "people you've met" — and as evidence this step was
        # done. db.source_coverage() now filters to calendar-sourced meetings; the
        # unit says which population that is, rather than implying all of them.
        "key": "calendar", "label": "Calendar", "metric": "meetings",
        "unit": "people your calendar says you met", "cost": "one tap",
        "gives": "Who you actually sit down with, and how often.",
        "helper": importers.HELPER_CALENDAR, "grant": True,
        "manual": "export by hand",
        "manual_how": "paste your Google Calendar's secret address, or export a "
                      "file from Calendar (File → Export → Export…) and pick it "
                      "in the calendar rows below",
    },
    {
        "key": "imessage", "label": "iMessage", "metric": "imessage",
        "unit": "people with history", "cost": "one tap",
        "gives": "Years of real conversation, searchable per person.",
        "helper": importers.HELPER_IMESSAGE, "grant": True,
        "manual": "copy by hand",
        "manual_how": "copy the three chat.db files yourself in Finder — the "
                      "iMessage row below walks you through it",
    },
    {
        # Was "same grant as iMessage", which priced WhatsApp at a permission it
        # has never needed: the helper copies it without Full Disk Access.
        "key": "whatsapp", "label": "WhatsApp", "metric": "whatsapp",
        "unit": "people with history", "cost": "one tap",
        # NC-192: "at no extra permission" was true of Full Disk Access and false
        # of the macOS dialog that reading WhatsApp in place raises; `manual_how`
        # names that dialog instead.
        "gives": "The other half of your messaging.",
        "helper": importers.HELPER_WHATSAPP, "grant": False,
        "manual": "no Full Disk Access needed",
        "manual_how": "this reads WhatsApp's own folder, and macOS asks whether to "
                      "allow that — and asks again later, because the answer does "
                      "not stick",
    },
    {
        # NC-067: this said "connections", which is not what the metric counts.
        # `source_coverage()["linkedin"]` is SUM(linkedin_url <> '') — people who
        # HAVE a LinkedIn link, from any source, including the ones the scraper
        # attached to contacts that came from the address book. It read a larger
        # number of "connections" than the LinkedIn panel, further down the same
        # page, read as people from COUNT(source = 'linkedin'). Both true, and
        # neither said which population it had counted.
        # Naming the population is the fix; forcing one number would make one of
        # the two surfaces answer a question nobody asked.
        "key": "linkedin", "label": "LinkedIn", "metric": "linkedin",
        "unit": "people with a LinkedIn link", "cost": "archive download + token",
        "gives": "Your whole professional network, plus every DM you've sent.",
    },
    {
        "key": "email", "label": "Email", "metric": "email",
        "unit": "people with threads", "cost": "Takeout, or your own OAuth app",
        "gives": "The conversations that never happened on a phone.",
    },
    {
        "key": "enrichment", "label": "Profile enrichment", "metric": "titles",
        # NC-076: "current" was a freshness claim the SQL never checks — the
        # metric is `position <> '' OR company <> ''`, with no reference to
        # facts_updated_at. A title scraped years ago and never revisited counts
        # the same as one refreshed today, so the unit says what was recorded.
        "unit": "people with a job title or company", "cost": "runs by itself",
        "gives": "Current job, company and photo — and it keeps improving.",
        "ongoing": True,
    },
]


# The one price a Full Disk Access step carries when it is the first step that
# needs the grant. Short on purpose: it sits on the step's title row, which does
# not wrap, beside the label and up to two tags, in a sidebar as narrow as 340px.
# The sentence that says who holds the grant and what the other way in is goes
# on the line under it (`how`).
GRANT_PRICE = "Full Disk Access, once"


def _helper_state() -> Dict[str, Any]:
    """{"available": bool, "copying": {labels}} — what the sync helper can do here.

    Asked of api_fda, the module that owns the helper's state, never re-derived
    (the same rule _blockers() follows). `copying` is every source the helper's
    last run copied or found current — the only proof that a step's "one tap" is
    real rather than hoped for.

    If the probe fails, the answer is "available, copying nothing": that prices
    every helper step at the grant, which can overstate a cost but can never make
    a step look cheaper than it is — the direction NC-186 was about.
    """
    try:
        import api_fda
        st = api_fda.handle_fda_status()
        return {"available": bool(st.get("available")),
                "copying": set(st.get("ok") or [])}
    except Exception:  # noqa: BLE001 — a failed probe must not take down setup
        return {"available": True, "copying": set()}


def _price(spec: Dict[str, Any], done: bool, helper: Dict[str, Any],
           grant_step: str) -> Dict[str, Any]:
    """The step's real price right now: {"cost", "heavy", "how"}.

    `grant_step` is the label of the step that already carries the Full Disk
    Access price in this list, or "" if none does yet — so the grant is priced
    ONCE, at the first step that still needs it, and the steps after it say they
    need nothing more than that.
    """
    label = spec.get("helper")
    if not label or label in helper["copying"]:
        # Not a helper source at all, or the helper has already copied it: the
        # plain price is the true one.
        return {"cost": spec["cost"], "heavy": False, "how": ""}
    if not spec.get("grant"):
        # WhatsApp: the helper copies it with no Full Disk Access, and without the
        # helper it is read from WhatsApp Desktop's own folder — which costs a
        # macOS dialog (NC-192), so say so rather than nothing.
        how = spec["manual_how"]
        if helper["available"]:
            how = "Without the sync helper, " + how + ". With it, nothing asks."
        else:
            how = how[0].upper() + how[1:] + "."
        return {"cost": spec["manual"], "heavy": False, "how": how}
    how = spec["manual_how"]
    if not helper["available"]:
        # This build ships no helper, so the way in that needs no permission is
        # the only way in, and it is the price.
        return {"cost": spec["manual"], "heavy": False,
                "how": how[0].upper() + how[1:] + "."}
    how = ("The sync helper copies it once it has Full Disk Access — given to that "
           "one small program, never to this app. Or, with no permission at all, "
           + how + ".")
    if grant_step or done:
        # Either an earlier step already carries the grant, or this one is done by
        # hand: say what it needs without charging for the grant a second time.
        return {"cost": f"same grant as {grant_step}" if grant_step else GRANT_PRICE,
                "heavy": False, "how": how}
    return {"cost": GRANT_PRICE, "heavy": True, "how": how}


def _archive_messages_by_channel() -> Dict[str, int]:
    """Raw message volume per channel, or {} when there is no archive yet.

    Guarded on purpose. A fresh install has no archive.db at all, and this panel
    is the FIRST thing such an install renders — so a missing archive has to read
    as "nothing imported yet", never as a 500 on the setup screen.

    Through open_if_present, not get_archive_db. get_archive_db() CREATES an
    empty archive.db when there is none, so opening the setup screen or the Data
    page on a fresh install wrote a database to the disk; and its default path is
    fixed when the module loads, so a test that points the archive elsewhere was
    silently answered from a different file (NC-217).
    """
    with arc.open_if_present() as conn:
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT channel, COUNT(*) AS n FROM messages GROUP BY channel"
            ).fetchall()
            return {r[0]: r[1] for r in rows}
        except Exception:  # noqa: BLE001 — table may not exist yet either
            return {}


def _blockers() -> Dict[str, str]:
    """{step_key: short reason} for sources that HAVE data but cannot collect now.

    NC-076, and the reason this endpoint needed a second fact at all.

    `done` below is `have > 0` — "this source has ever produced a row". That was
    rendered on screen as "connected", present tense, and the two are not the
    same thing: a LinkedIn import that put people in the database long ago, with
    its token since removed, would make the panel say "N/N sources connected"
    beside a warning that LinkedIn messages were not being collected. Both read
    true data. Only one of them answers the question the user is asking.

    "A signal that only proves the wrong thing" is a recurring failure in this
    codebase (NC-035, NC-061, NC-067, NC-068, NC-070), so the fix is a concept
    rather than a patch: `done` keeps meaning "ever produced", `blocked` means
    "cannot right now", and the UI is required to distinguish them.

    The two rules that keep this from becoming the same bug again:

      • Ask the module that OWNS the decision. Both calls below are that module's
        own exported answer, not a reimplementation of it here.
      • A blocker is only reported when a local signal PROVES it. Absence of
        evidence is not a blocker — a source with no helper and no token report
        is unknown, not broken, and guessing would be this bug pointing the other
        way. So the steps with no definitive local signal (email, enrichment,
        contacts) simply never appear here, and that is deliberate.

    Never raises. A blocker probe that throws must not take down the setup panel
    — this endpoint renders on a fresh install where none of these exist yet.
    """
    out: Dict[str, str] = {}
    # Lazy, and separately guarded: these are the only imports in this module
    # that reach outside its own two tables, and either can be absent or unhappy
    # on a partial install without that being this endpoint's problem.
    try:
        import api_linkedin
        reason = api_linkedin.collection_blocked()
        if reason:
            out["linkedin"] = reason
    except Exception:  # noqa: BLE001 — a probe that fails reports nothing
        pass
    try:
        import api_fda
        out.update(api_fda.collection_blocked())
    except Exception:  # noqa: BLE001
        pass
    return out


def _unit(unit: str, n: int) -> str:
    """"1 person with history", not "1 people with history" (NC-122). Every step
    unit starts with "people", so the singular is that one word swapped."""
    if n == 1 and unit.startswith("people"):
        return "person" + unit[len("people"):]
    return unit


def _scope(conn: Any, people: int) -> Dict[str, Any]:
    """Who every count of people on the Data page is about, said once (NC-122).

    Every people figure the page draws counts `active`: the setup panel, the
    source rows, the LinkedIn panel, "What it brought" and the tile at the
    bottom. `left_out` is how many people in the file that leaves out — the ones
    you hid or deleted — so the page can say it rather than let two numbers
    that differ by them look like a mistake."""
    everyone = int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] or 0)
    return {
        "population": "active",
        "says": figures.population_says("active"),
        "left_out": max(0, everyone - people),
    }


def handle_setup_progress(conn: Any) -> Dict[str, Any]:
    """Per-step contribution, for the setup screen. No longer served at an
    address (NC-237); see the note above STEPS."""
    try:
        cov = db.source_coverage(conn)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                       f"could not read setup progress: {exc}") from exc

    by_channel = _archive_messages_by_channel()
    # linkedin DMs land in archive.db under this channel name
    # (crm/linkedin_import.py), so message volume comes from there, not crm.db.
    messages_total = sum(by_channel.values())

    blocked = _blockers()
    # Read at call time, not import, so flipping the switch in Settings takes
    # effect without a restart — the contract every other managed key has.
    autosync = os.environ.get("NC_AUTOSYNC", "1").strip() != "0"
    enrich_auto = os.environ.get("NC_ENRICH_AUTO", "1").strip() != "0"

    helper = _helper_state()
    grant_step = ""   # the label of the step that carries the grant, once one does

    steps = []
    for s in STEPS:
        have = int(cov.get(s["metric"], 0) or 0)
        price = _price(s, have > 0, helper, grant_step)
        if price["heavy"]:
            grant_step = s["label"]
        steps.append({
            "key": s["key"], "label": s["label"],
            "have": have, "unit": _unit(s["unit"], have),
            # NC-186: the price for THIS install, from the helper's own report —
            # not the static best case, which is what hid the grant.
            "cost": price["cost"], "gives": s["gives"],
            # Both ways in, in one sentence, for the row that is next. "" when the
            # price needs no explaining.
            "how": price["how"],
            "done": have > 0,
            # NC-076. "" when nothing is known to be wrong. A step can be done
            # and blocked at once — that IS the state this field exists for, and
            # the UI must not collapse the pair back into one word.
            "blocked": blocked.get(s["key"], "") if have > 0 else "",
            # "which one do I do next" is NOT a field here on purpose: it is the
            # first not-done step in this list's order, which the client can see
            # for itself. There is deliberately no server-side `spine`/`next`
            # flag: two places deciding the same thing is how they come to
            # disagree.
            # NC-186: true only on the step that carries the Full Disk Access
            # price, so "needs a permission" appears once for the one grant.
            "heavy": price["heavy"],
            "ongoing": bool(s.get("ongoing")),
            # NC-076. The step bar draws `have / people` — "how much of your
            # network this source reaches". For the step whose metric IS the
            # people count, numerator and denominator are the same SQL, so the
            # bar is pinned at 100% no matter what happened. A full bar that
            # cannot be anything else is not a measurement, and the panel's own
            # rule (see StepBars) is that a placeholder must never be drawn in
            # the shape of one. The client skips the track when this is false.
            "share_meaningful": s["metric"] != "people",
            # NC-076. `ongoing` is a fact about the step; this is a fact about
            # right now. "Keeps running" was rendered from the former, so it
            # claimed an active pass on an install where the master switch was
            # off and nothing was running at all.
            "ongoing_active": bool(s.get("ongoing")) and autosync and enrich_auto,
            "messages": int(by_channel.get(s["key"], 0) or 0),
        })

    people = int(cov.get("people", 0) or 0)
    history = int(cov.get("history", 0) or 0)
    return {
        "people": people,
        "history": history,
        "scope": _scope(conn, people),
        # The same two numbers, carrying who they count (NC-122).
        "figures": {
            "people": figures.figure(people, "people", "active",
                                     side=figures.SIDE_STORED),
            "history": figures.figure(history, "people", "active", of=people,
                                      side=figures.SIDE_STORED,
                                      says="with a conversation on any channel"),
        },
        # The one number that says "this is a memory, not a list". Guarded
        # against a zero-contact install rather than dividing by zero on the
        # very first render.
        "history_pct": round(100.0 * history / people, 1) if people else 0.0,
        "photos": int(cov.get("photos", 0) or 0),
        "titles": int(cov.get("titles", 0) or 0),
        "messages": messages_total,
        "steps": steps,
        "steps_done": sum(1 for s in steps if s["done"]),
        "steps_total": len(steps),
        # NC-076. The count the headline needs in order NOT to certify a number.
        # steps_done alone cannot say "everything is connected" truthfully; this
        # is the second half of that sentence.
        "blocked_count": sum(1 for s in steps if s["blocked"]),
        # NC-076. The master switch, so the panel can stop describing automatic
        # behaviour that is currently switched off.
        "autosync": autosync,
    }
