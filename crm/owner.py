#!/usr/bin/env python3
"""What the app knows about its own user, and the prompt block that carries it.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

This module backs a personal page where the user describes their own goals. It is
a module rather than three lines inside chat.py because the same text has to reach
THREE different prompts — Ask, the contact brief, and the draft message — and they
must not drift apart.

**Declared, not measured.** crm/style_profile.py already measures how the user
writes, from their own sent messages, and injects that as a voice directive. That
is the measured half of "who is this person". This is the declared half: free text
they typed about what they are trying to do, which nothing can infer from a
message archive. The two are orthogonal and are appended to prompts separately —
the same reasoning api_briefs already applies to the style block ("the
hand-written half describes the JOB, the measured half describes the VOICE").

Apart from this module, what the app knows about its user is SELF_NAME, SELF_EMAIL
and the style profile, and `chat.py`'s SYSTEM_PROMPT has no free-text block about
the user at all — so without it no code path would pick such text up even if a
page existed to type it into.

It also keeps a person's actual name out of every shipped prompt: the name comes
from the SELF_NAME setting through display_name(), never from a hardcoded person,
so a draft is never signed with the wrong human's name.
"""

import os
from http import HTTPStatus
from typing import Any, Dict

import db
from api_core import ApiError

# One key in crm.db's `state` table, plain text, beside style_profile's
# STATE_KEY = "writing_style_profile". Deliberately not a settings/env value:
# this is prose the user edits in the app, not configuration, and secrets/.env is
# for configuration. It also means it travels with crm.db, which is the syncable
# database — the style profile lives there for the same reason.
GOALS_STATE_KEY = "owner_goals"

# A ceiling on the free text, in characters. Not a settings field: limits the user
# can *feel* (money, disk, wait time) belong in Settings, while plumbing is simply
# decided. This one is plumbing with a cost attached — see
# the caching note in chat._system_blocks. ~4,000 characters is ~1,000 tokens,
# which is a paragraph or two of real intent and nowhere near enough to matter
# against a cached prefix. Text longer than this is truncated on write, not
# rejected: silently dropping what someone typed is worse than trimming it.
GOALS_MAX_CHARS = 4000


def load(conn: Any) -> Dict[str, str]:
    """Everything the app knows about its user: {name, email, goals}.

    Name and email come from the SELF_NAME / SELF_EMAIL settings, read at call
    time so editing them in Settings takes effect without a restart. Goals come
    from the state row. Any of the three may be "".
    """
    return {
        "name": (os.environ.get("SELF_NAME") or "").strip(),
        "email": (os.environ.get("SELF_EMAIL") or "").strip(),
        "goals": (db.state_get(conn, GOALS_STATE_KEY, "") or "").strip(),
    }


def save_goals(conn: Any, text: str) -> str:
    """Store the goals text and return what was actually stored.

    Returns the stored value rather than None so a caller can show the user the
    truncated text instead of letting the UI keep displaying something longer
    than what is now in the database.

    **Commits.** db.state_set() does not, and app.py's _with_conn closes the
    connection in a `finally` with no commit — so without this line the write is
    rolled back the moment the request ends. The write is still visible to later
    reads on the SAME connection, so a test on one connection passes and the
    endpoint answers 200 with the saved text echoed back. Only a second request,
    which is a different connection, shows it: the box goes clean, the toast says
    Saved, and GET /api/owner returns "".
    """
    clean = (text or "").strip()[:GOALS_MAX_CHARS]
    db.state_set(conn, GOALS_STATE_KEY, clean)
    conn.commit()
    return clean


def display_name(conn: Any) -> str:
    """How to refer to the user inside a prompt. Never a hardcoded person.

    "the user" rather than a guess. A prompt that names the wrong human is worse
    than one that names no human: the model will write in that person's voice and
    sign their name, and the output looks confident while being wrong about who
    is sending it.
    """
    return load(conn)["name"] or "the user"


def context_block(conn: Any) -> str:
    """The prompt block describing the user, or "" when they have told us nothing.

    "" on purpose, and every caller must handle it: an install with no name and no
    goals must produce the exact request it produced before this module existed.
    A block that says "the user has not told you anything about themselves" spends
    tokens to add nothing and invites the model to comment on the absence.

    The last paragraph is not padding. Goals are a fact about the OWNER, and
    without that sentence a model reading "wants to hire a designer" in the
    system prompt will cheerfully attribute it to whichever contact it is
    describing — the brief and draft prompts both run per-contact.
    """
    who = load(conn)
    if not who["name"] and not who["goals"]:
        return ""
    lines = ["ABOUT THE PERSON WHOSE CRM THIS IS"]
    if who["name"]:
        lines.append(f"Their name is {who['name']}.")
    if who["goals"]:
        lines.append("In their own words, what they are working on and what they "
                     "want from their network:")
        lines.append('"""')
        lines.append(who["goals"])
        lines.append('"""')
        lines.append("Weigh this when judging who is worth their attention — it is "
                     "what they told you, so prefer it over generic closeness when "
                     "the two disagree. It describes THEM, never a contact: do not "
                     "attribute any of it to the person you are writing about.")
    return "\n".join(lines)


# ── The endpoint pair, so the text has somewhere to come from ────────────────
# The other half of the goals feature. A ROUTE_MODULES module rather than three branches in
# app.py's route tables: owner-facing state is its own small domain, and the
# registry exists precisely so adding one does not touch a shared file.
#
# Two routes, not one, because reading and writing have different shapes: the GET
# hands the UI everything it needs to render the field INCLUDING the ceiling, so
# the character counter is not a number duplicated in JavaScript that drifts from
# GOALS_MAX_CHARS the first time anyone changes it.
ROUTES = {
    "GET": {"/api/owner": "handle_owner"},
    "POST": {"/api/owner/goals": "handle_owner_goals"},
}


def handle_owner(conn: Any) -> Dict[str, Any]:
    """GET /api/owner — what the app knows about its user, plus the write limit."""
    who = load(conn)
    return {"name": who["name"], "email": who["email"], "goals": who["goals"],
            "max_chars": GOALS_MAX_CHARS}


def handle_owner_goals(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/owner/goals — save the free text, and say what was really stored.

    `truncated` is returned rather than inferred by the caller because the client
    cannot work it out: it knows the length it sent and the ceiling, but not
    whether .strip() took the difference. The UI shows the stored text either way,
    so a truncated save never leaves a longer draft on screen than exists in the
    database.
    """
    text = body.get("goals", "")
    if not isinstance(text, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, "goals must be a string")
    stored = save_goals(conn, text)
    return {"goals": stored, "max_chars": GOALS_MAX_CHARS,
            "truncated": len(text.strip()) > len(stored)}
