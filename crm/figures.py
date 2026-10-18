"""One definition of what a number counts — the population, the unit, the side.

NC-087. Nothing in this app shared a definition of what it was counting. "How
many people are in this app" had SEVEN answers, and they differed depending on
whether hidden people and soft-deleted people were filtered out. Within one
window, Settings could say one number under the word "contacts" and the People
page a smaller one under the word "people", one click apart.

**They agree only by coincidence, not by design.** `db.set_deleted` hides
everyone it deletes, so on an ordinary file every soft-deleted person is also
hidden. Then `visible` and `active` return the same number, and `live` differs
from both. Un-hide a deleted person, or let a sync write `deleted` without
`excluded`, and two figures both labelled "people" give different answers. That
coincidence is load-bearing in the worst way: it is what has kept the defect
invisible.

(A docstring that quotes figures from one particular file goes stale, so none
are quoted here — the predicates below are the part that does not rot.)

WHY THIS FILE IS A GENERALISATION AND NOT A FIFTH SYSTEM
--------------------------------------------------------
Four subsystems here had already solved this, in isolation, and none of them
knew about the others:

  crm/intake.py         publishes arithmetic identities AS NUMBERS rather than
                        asserting them in comments, and exposes each residue as
                        an `unexplained` integer that the receipt renders.
  crm/style_profile.py  the same, with three identities over the message archive,
                        and the contract in words: "If a future change starts
                        dropping messages, this number goes non-zero and the
                        receipt says so instead of the count quietly shrinking."
  static/app/people.js  the display half — ONE owner object, three populations
                        reconciled, a different sentence per disagreement, so no
                        consumer can count again.
  crm/intake.py         `Index.haystack()`, which names its denominators: "A
                        match report with no denominator is not a report."

The defect was never that the pattern was missing. It is that it was solved four
times and never generalised, so the four surfaces that most needed it were the
four without it. This file is those four, made shared. It invents nothing.

THE THREE AXES, IN THE DATA RATHER THAN IN THE PROSE
-----------------------------------------------------
1. WHICH POPULATION — all rows, hidden removed, deleted removed, or both.
   Stated, never inferred, and never relying on the coincidence above.
2. WHICH SIDE OF THE WALL — a stored counter in crm.db, or live rows in
   archive.db. These are different measurements and they demonstrably differ:
   some people have a card claiming messages the app cannot show.
3. WHAT THE UNIT IS — people, messages, rows, handles, conversations. The old
   `of_total` field in the provenance payload was six different populations
   wearing one field name: three counted rows, three counted people, and the
   JSON said nothing. Calendar is the live proof — more than one row per
   person, both figures on the same page.

WHAT THIS FILE DELIBERATELY DOES NOT DO
----------------------------------------
It does not touch `contacts.source`, `identifiers.source` or `photos.SOURCE_RANK`.
Those are the precedence ladder (invariant 4) — splitting a token there would
silently change which spelling of a phone number wins. Provenance and population
are different questions and this file only answers the second.

It also holds no SQL of its own beyond the population predicates. It is a
vocabulary, not a query layer: `db.py` still owns the contacts table.

APPLIED (NC-087)
----------------
At first this file was the right answer that nothing used: two call sites, while
42 contact-scope filters were still typed out by hand in 12 files, in 14 different
spellings. All 42 now go through `population_where()`, and
`tests/test_scope_filter_one_spelling.py` fails if a 43rd is written by hand.
Every old spelling returns exactly the same rows as its replacement, so the
conversion changed no behaviour — what it changed is that the family can no
longer drift apart. The complement direction
("show me the people I hid" — `excluded = 1`, `deleted = 1`) was left for NC-115,
so that diff stayed reviewable.

APPLIED (NC-115)
----------------
The complements have names too: `hidden`, `deleted` and `hidden_only`. The 15
hand-written copies that were left went through them, and the same test now
fails on a hand-written `excluded = 1` too. The reason was not tidiness. Those are
the queries behind the breakdowns, like the "hidden" and "deleted" parts of the
people ledger and the Hidden and Deleted tabs on the People page. A breakdown that
defines its parts differently from the total it breaks down stops adding up,
which is the thing `ledger()` below exists to catch.
"""

from typing import Any, Dict, Iterable, Optional, Tuple

# ── The named populations ─────────────────────────────────────────────────────
#
# Four that say who counts, then three that say who was left out. Of the first
# four, the fourth is not redundant. `excluded` and `deleted` are separate
# columns written by separate paths — cloud sync writes them independently, and
# exactly one function keeps "deleted implies excluded" true. So "both filters"
# is a real, separately-reachable answer, not the intersection of two others by
# construction.
#
# The predicate is the ONLY place each filter is spelled. A count that writes
# `WHERE excluded = 0` by hand is a count that can drift from the one beside it,
# which is the entire defect this file exists to close.
#
#   name -> (what it means in plain words, the SQL predicate over `contacts`)
POPULATIONS: Dict[str, Tuple[str, str]] = {
    "all": (
        "everyone in the file, including the people you hid and the people you "
        "deleted",
        "",
    ),
    "visible": (
        "everyone except the people you hid",
        "COALESCE({a}excluded, 0) = 0",
    ),
    "live": (
        "everyone except the people you deleted",
        "COALESCE({a}deleted, 0) = 0",
    ),
    "active": (
        "everyone you can actually see and use",
        "COALESCE({a}excluded, 0) = 0 AND COALESCE({a}deleted, 0) = 0",
    ),
    # And the ways out (NC-115). `hidden` is exactly who `visible` leaves out,
    # and `deleted` exactly who `live` leaves out. Each is the other's test
    # negated, and `<> 0` rather than `= 1`, so a stray 2 still lands on exactly
    # one side. `hidden_only` exists because `db.set_deleted` sets BOTH
    # columns: everyone deleted is also hidden, so a breakdown that counted
    # "hidden" and "deleted" side by side would count those people twice.
    # active + hidden_only + deleted is everyone, once each.
    "hidden": (
        "the people you hid, including the ones you then deleted",
        "COALESCE({a}excluded, 0) <> 0",
    ),
    "deleted": (
        "the people you deleted",
        "COALESCE({a}deleted, 0) <> 0",
    ),
    "hidden_only": (
        "the people you hid and have not deleted",
        "COALESCE({a}excluded, 0) <> 0 AND COALESCE({a}deleted, 0) = 0",
    ),
}


def population_where(name: str, alias: str = "") -> str:
    """The SQL predicate for a named population, ready to drop after WHERE.

    Returns "" for `all` — deliberately, so a caller can write
    `WHERE 1=1 AND {population_where(...)}` without a special case, or test the
    string for emptiness and omit the WHERE entirely.

    `alias` is the table alias when the query joins (`"c"` -> `c.excluded`).
    Passing it is what stops this being un-usable in exactly the queries that
    most need it — the joined ones.
    """
    if name not in POPULATIONS:
        raise ValueError(
            f"unknown population {name!r}. They are {sorted(POPULATIONS)}. "
            "Add another here, with a plain-words meaning, rather than spelling "
            "a new filter inline — an inline filter is how this app came to have "
            "seven answers to 'how many people'.")
    _says, sql = POPULATIONS[name]
    return sql.format(a=(alias + "." if alias else ""))


def population_not_where(name: str, alias: str = "") -> str:
    """The complement — the predicate for everyone this population leaves out.

    Derived here rather than typed, so the two directions cannot drift: widen
    `visible` and its complement narrows in the same edit. For a question a
    person would ask ("who did I hide?"), name the population instead —
    `hidden`, `deleted`, `hidden_only` — so the query says which one it means.

    Returns "" for `all`, whose complement is nobody — the caller tests for
    emptiness exactly as it does with `population_where`. This stays vocabulary
    rather than a query: it hands back a string, and `db.ids_outside` is the one
    place that runs it, because db.py owns the contacts table.
    """
    pred = population_where(name, alias)
    return f"NOT ({pred})" if pred else ""


def population_says(name: str) -> str:
    """What this population means, for a person reading the screen."""
    if name not in POPULATIONS:
        raise ValueError(f"unknown population {name!r}")
    return POPULATIONS[name][0]


# ── The unit ──────────────────────────────────────────────────────────────────
#
# A closed set on purpose. An open string drifts into synonyms — "contacts" and
# "people" and "records" for one thing — and then two figures that count the
# same rows read as different measurements. Each entry is (singular, plural).
#
# `rows` is the honest fallback and is meant to look unsatisfying: a figure that
# can only say "rows" is a figure nobody can check.
UNITS: Dict[str, Tuple[str, str]] = {
    "people": ("person", "people"),
    "messages": ("message", "messages"),
    "conversations": ("conversation", "conversations"),
    "handles": ("handle", "handles"),          # an email address, a phone number
    "dates": ("date", "dates"),                # a row in contact_dates
    "photos": ("photo", "photos"),
    "profiles": ("profile", "profiles"),       # a LinkedIn profile scrape
    "sources": ("source", "sources"),
    "lists": ("list", "lists"),
    "rows": ("row", "rows"),
}


def unit_says(unit: str, n: int) -> str:
    """"1 person", "2 people" — because "1 messages" reads like a bug even when
    the number is right. Lifted from `style_profile._n`, which is where this app
    first noticed."""
    if unit not in UNITS:
        raise ValueError(f"unknown unit {unit!r}. The set is {sorted(UNITS)}.")
    one, many = UNITS[unit]
    return f"{n:,} {one if n == 1 else many}"


# ── Which side of the wall ────────────────────────────────────────────────────
#
# Not a quality ranking. A stored counter is not a worse archive count; it is a
# different measurement, and the app needs both — the counter is what makes the
# People table sortable without opening archive.db. Naming the side is what stops
# them being read as two attempts at one number.
SIDE_STORED = "stored"    # a counter column on contacts, in crm.db
SIDE_ARCHIVE = "archive"  # live rows in archive.db
SIDE_DERIVED = "derived"  # computed from other figures, owning no rows itself

SIDE_SAYS = {
    SIDE_STORED: "counted from the running totals kept on each person",
    SIDE_ARCHIVE: "counted from the messages themselves",
    SIDE_DERIVED: "worked out from the other numbers here",
}


def figure(n: int, unit: str, population: str, *,
           of: Optional[int] = None,
           side: str = SIDE_DERIVED,
           as_of: str = "",
           says: str = "") -> Dict[str, Any]:
    """One number that can be checked, rather than a bare integer.

    Every figure carries the three axes plus its denominator, so a reader — and a
    test — can tell two figures apart without reading the query behind them.

    `of` is the denominator. It must be a count of the SAME unit over the SAME
    population, because "12 of 40" is only meaningful when both halves count
    the same kind of thing the same way. Nothing here can enforce that across two
    separate calls, so `check_payload` does it across a whole payload instead.

    `says` is an optional sentence for a caller that knows something the shape
    cannot express — the way `crm/api_sources.py` publishes a caveat rather than
    absorbing it.
    """
    if unit not in UNITS:
        raise ValueError(f"unknown unit {unit!r}. The set is {sorted(UNITS)}.")
    if population not in POPULATIONS:
        raise ValueError(f"unknown population {population!r}")
    if side not in SIDE_SAYS:
        raise ValueError(f"unknown side {side!r}")
    if of is not None and of < 0:
        raise ValueError("a denominator cannot be negative")
    out: Dict[str, Any] = {
        "n": int(n),
        "unit": unit,
        "population": population,
        "population_says": population_says(population),
        "side": side,
        "side_says": SIDE_SAYS[side],
        "says": says,
    }
    if of is not None:
        out["of"] = int(of)
        # Published rather than left to the renderer, so every surface agrees on
        # the rounding and none of them has to know the rule.
        out["share"] = round(100.0 * int(n) / int(of), 1) if of else None
    if as_of:
        out["as_of"] = as_of
    return out


def figure_says(fig: Dict[str, Any]) -> str:
    """The one sentence for a figure, decided here so no two screens word it
    differently. "12 of 40 people" — never a bare number."""
    n, unit = int(fig.get("n", 0)), fig.get("unit", "rows")
    lead = unit_says(unit, n)
    if fig.get("of") is not None:
        one, many = UNITS[unit]
        lead = f"{n:,} of {int(fig['of']):,} {one if fig['of'] == 1 else many}"
    return f"{lead} — {fig.get('says')}" if fig.get("says") else lead


# ── The residue ledger ────────────────────────────────────────────────────────

def ledger(total: int, parts: Dict[str, int], *, name: str = "total") -> Dict[str, Any]:
    """An arithmetic identity published as numbers, not asserted in a comment.

    This is `crm/intake.py`'s and `crm/style_profile.py`'s `accounting` block,
    generalised. The contract in both was the same shape:

        total == part + part + part

    and each exposed the subtraction as `unexplained`. The point, in
    style_profile's own words: "If a future change starts dropping messages, this
    number goes non-zero and the receipt says so instead of the count quietly
    shrinking."

    `unexplained` is signed on purpose. Negative means the parts add up to MORE
    than the total — double-counting, which is a different bug from leaking and
    should not be hidden by an abs() or a max(0, ...).
    """
    explained = sum(int(v) for v in parts.values())
    return {
        "of": name,
        "total": int(total),
        "parts": {k: int(v) for k, v in parts.items()},
        "explained": explained,
        "unexplained": int(total) - explained,
    }


def ledger_says(led: Dict[str, Any]) -> str:
    """What to render, and only when there is something to say. An identity that
    holds needs no sentence; a residue is the whole reason the block exists."""
    u = int(led.get("unexplained", 0))
    if not u:
        return ""
    if u > 0:
        return (f"{abs(u):,} of {int(led['total']):,} are not accounted for by "
                f"anything above. That is a leak, not a rounding.")
    return (f"the parts above add up to {abs(u):,} more than the "
            f"{int(led['total']):,} total, so something is counted twice.")


# ── The gate, as a function rather than only as a test ────────────────────────

def walk(payload: Any) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Every figure in a payload, with a dotted path to it. A figure is any dict
    carrying the three axes — recognised by shape rather than by a marker key, so
    a caller cannot half-adopt this by omitting the marker."""
    def go(node: Any, path: str):
        if isinstance(node, dict):
            if {"n", "unit", "population"} <= set(node):
                yield path, node
                return
            for k, v in node.items():
                yield from go(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                yield from go(v, f"{path}[{i}]")
    yield from go(payload, "")


def check_payload(payload: Any) -> list:
    """Every way one payload can contradict itself. Returns a list of complaints,
    empty when it is consistent.

    NC-087 requires exactly this: no two figures in one payload may describe the
    same population differently. Written as a function rather than only as a
    test so an endpoint can assert on itself in a debug build, and so the test
    does not become the only reader of the rule.
    """
    out = []
    for path, fig in walk(payload):
        unit, pop = fig.get("unit"), fig.get("population")
        if unit not in UNITS:
            out.append(f"{path}: unit {unit!r} is not one of {sorted(UNITS)}")
        if pop not in POPULATIONS:
            out.append(f"{path}: population {pop!r} is not one of {sorted(POPULATIONS)}")
        if fig.get("population_says") and pop in POPULATIONS \
                and fig["population_says"] != population_says(pop):
            out.append(
                f"{path}: says {fig['population_says']!r} but the registry says "
                f"{population_says(pop)!r} — one of them was written by hand")
        of = fig.get("of")
        if of is not None and int(fig.get("n", 0)) > int(of):
            out.append(f"{path}: {fig['n']} of {of} — a part larger than its whole")
    return out
