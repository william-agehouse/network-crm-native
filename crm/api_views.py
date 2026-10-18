#!/usr/bin/env python3
"""View compositions — what is left of them is the describe queue. THE CHURN ZONE.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

**This file is meant to be disposable.** The UI has been redesigned more than
once, and each redesign rewrites what a screen shows without changing what a
contact IS. So the rule here is: compositions only. No SQL, no serialization, no
archive opens, no JSON parsing. Every read goes through api_contacts /
api_notes / api_messages / api_dates / api_serialize.

When the next redesign lands you should be able to delete this file and write a
new one, with nothing underneath it moving. If you reach for conn.execute() in
here, the query belongs in the entity module that owns that table.
"""

from typing import Any, Dict, List

import api_contacts
import db
from api_messages import _last_messages
from api_serialize import _contact_detail, _last_seen


# ── Today is gone (NC-009) ───────────────────────────────────────────────────
# The Today screen was deleted rather than rebuilt. What went with it: the whole
# attention queue — dates landing in two days, replies owed, going-quiet,
# describe — its ranking and dedup, GET /api/today, GET /api/feed,
# static/app/today.js, static/css/view-home.css, the tab, the palette's "Start the
# daily review", and the empty route, which now lands on People.
#
# ONE thing the surface owned outlived it, because a second screen needed it: the
# onboarding wizard's "Remember three people" step read /api/today and took the
# first three of `queue`. It has its own endpoint now, which is a better shape
# anyway — asking a whole-dashboard endpoint for everything and using a twentieth
# of it was the only reason that step was coupled to a page it does not appear on.
#
# WHAT WAS DELIBERATELY LEFT ALONE. api_messages.owed_replies,
# api_notes.recent_happenings and api_contacts.skipped_today_count now have no
# caller. They are reads in the ENTITY modules, which this file's own header calls
# the durable layer underneath the disposable compositions — deleting them because
# a composition went is exactly what that header warns against. They are left in
# place, known to be unreferenced, rather than removed on a guess about the next
# screen.
DESCRIBE_QUEUE_SIZE = 3


def _describe_queue(conn: Any, size: int) -> List[Any]:
    """People worth describing: strongest relationships first (they are the ones
    worth remembering AND the easiest to say something about), then the classic
    review rotation to top up.

    Was `_today_queue`. Same ranking, same job — only the surface that asked for
    it changed."""
    rows = api_contacts.describe_candidates(conn)
    scored = sorted(
        rows,
        key=lambda r: (db.relationship_strength(r), _last_seen(r)),
        reverse=True,
    )
    out = scored[:size]
    if len(out) < size:
        seen = {r["id"] for r in out}
        for r in db.pick_review_batch(conn, limit=size, exclude=seen):
            if r["id"] not in seen:
                out.append(r)
                seen.add(r["id"])
            if len(out) >= size:
                break
    return out[:size]


def handle_describe_queue(conn: Any) -> Dict[str, Any]:
    """GET /api/describe-queue — a few people worth writing a line about.

    The onboarding wizard's third step, and the only caller. It used to ask
    /api/today for its entire payload and keep `queue[:3]`, which meant the
    wizard broke when a screen it has nothing to do with was deleted."""
    queue = []
    for r in _describe_queue(conn, DESCRIBE_QUEUE_SIZE):
        entry = _contact_detail(conn, r)
        entry["last_messages"] = _last_messages(r["id"])
        queue.append(entry)
    return {"queue": queue}
