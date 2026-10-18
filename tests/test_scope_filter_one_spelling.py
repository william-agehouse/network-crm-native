"""NC-087: "is this person in scope?" is spelled in exactly one place.

Every query that lists people has to say "…and only the ones I have not hidden or
deleted". There is no reason for that sentence to be typed out more than once, and
hand-written variants are how one question ends up with several different answers:
`excluded = 0`, `excluded=0`, `c.excluded = 0`, `COALESCE(excluded, 0) = 0`,
`COALESCE(deleted,0) = 0` and so on. Several pairs of those disagree the moment
either column holds NULL — both are nullable and default to 0, so the family
agrees until a NULL appears, by luck rather than by design.

All of them now go through `figures.population_where()`, which names the
population it means. This test stops another being typed by hand.

WHAT IS DELIBERATELY STILL ALLOWED, and why each one is not the same question:

  • Another table's own `deleted` column. `notes`, `chat_conversations` and `lists`
    each have one, and `c.` means `chat_conversations` in two db.py queries and
    `contacts` in a third. A filter is only a CONTACT-scope filter if its table is
    `contacts`, which is why this allowlist is keyed by file and fragment rather
    than by column name.
  • The guard on a write. `UPDATE contacts SET excluded = 1 WHERE … AND excluded = 0`
    means "do not re-hide someone already hidden". That is a concurrency guard, not
    a question about who is in scope.
  • `crm/figures.py` itself, which is where the sentence is written down.

NC-115 added the other direction. "Show me the people I hid" (`excluded = 1`,
`deleted = 1`, `COALESCE(…) <> 0`) is a different question, but the same rule
applies: every hand-written copy of it goes through the named populations
`hidden`, `deleted` and `hidden_only`, and `TestOneSpellingOfTheComplement` below
stops another. It allows nothing outside figures.py. The one thing it skips is a
write: `UPDATE … SET excluded = 1` hides someone, it does not ask who is hidden.
"""

import glob
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import code_of  # noqa: E402
from tests.helpers import config  # noqa: E402,F401 — data-dir bootstrap (see helpers)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM = os.path.join(REPO, "crm")

# An in-scope filter: mentions one of the two columns and asks for the ZERO side.
# `<> 0` / `= 1` are the complement and are out of scope for this gate on purpose.
IN_SCOPE = re.compile(
    r"(?:COALESCE\s*\(\s*)?(?:[a-z]\.)?\b(?:excluded|deleted)\b\s*(?:,\s*0\s*\))?"
    r"\s*=\s*0", re.I)

# (file, exact fragment) -> (how many, why it is not a contact-scope filter).
# A new hand-written filter either brings a fragment that is not here, or pushes
# one of these counts up. Both fail.
ALLOWED = {
    ("db.py", "deleted = 0"): (
        3, "the notes table, chat_conversations, and lists — each has its own "
           "deleted column and none of them is the contacts table"),
    ("db.py", "c.deleted = 0"): (
        2, "`c` is chat_conversations in get_conversations() and "
           "newest_conversation_id(), not contacts"),
    ("db.py", "l.deleted = 0"): (
        3, "`l` is the lists table in get_lists(), get_list_counts() and "
           "upcoming_events()"),
    ("db.py", "excluded = 0"): (
        1, "the guard on a write: UPDATE contacts SET excluded = 1 WHERE "
           "name_norm = ? AND excluded = 0 — 'do not re-hide someone already "
           "hidden', which is not a question about scope"),
}


# The complement: one of the two columns, asking for the NON-zero side. `\b`
# after the 1 so `excluded = 10` would not count as a match and hide a real one.
COMPLEMENT = re.compile(
    r"(?:COALESCE\s*\(\s*)?(?:[a-z]\.)?\b(?:excluded|deleted)\b\s*(?:,\s*0\s*\))?"
    r"\s*(?:=\s*1|<>\s*0|!=\s*0)\b", re.I)
# The SQL word nearest before a match says what it is doing: after SET it is a
# value being written; after WHERE, AND, OR, WHEN or NOT it is a test.
_LAST_CLAUSE = re.compile(r"\b(SET|WHERE|AND|OR|WHEN|ON|NOT|SELECT)\b", re.I)


def _is_a_write(code, at):
    words = _LAST_CLAUSE.findall(code, max(0, at - 200), at)
    return bool(words) and words[-1].upper() == "SET"


class TestOneSpelling(unittest.TestCase):

    def _fragments(self):
        """Every in-scope filter written by hand, outside figures.py."""
        found = {}
        for path in sorted(glob.glob(os.path.join(CRM, "*.py"))):
            base = os.path.basename(path)
            if base == "figures.py":
                continue          # where the sentence is written down
            code = code_of(path)  # comments and docstrings blanked, offsets kept
            for m in IN_SCOPE.finditer(code):
                frag = re.sub(r"\s+", " ", m.group(0)).strip()
                found[(base, frag)] = found.get((base, frag), 0) + 1
        return found

    def test_no_new_hand_written_contact_scope_filter(self):
        found = self._fragments()
        unexpected = {k: n for k, n in found.items() if k not in ALLOWED}
        self.assertEqual(
            unexpected, {},
            "\n\nA contact-scope filter is spelled by hand here:\n"
            + "".join(f"    {f}: {frag!r} ({n}x)\n"
                      for (f, frag), n in sorted(unexpected.items()))
            + "\nUse figures.population_where() instead, which names the "
              "population:\n"
              "    visible — everyone except the people you hid\n"
              "    live    — everyone except the people you deleted\n"
              "    active  — everyone you can actually see and use\n"
              "\nIt takes a table alias, so a joined query can use it too:\n"
              '    f\"... WHERE {figures.population_where(\'active\', \'c\')}\"\n'
              "\nIf this really is not a contacts-table filter (another table's "
              "own deleted column, a guard on a write), add it to ALLOWED in "
              "this file with the reason.")

    def test_the_allowed_exceptions_have_not_grown(self):
        found = self._fragments()
        for key, (expected, why) in ALLOWED.items():
            actual = found.get(key, 0)
            self.assertEqual(
                actual, expected,
                f"\n\n{key[0]}: {key[1]!r} was allowed {expected}x because "
                f"{why}.\nIt now appears {actual}x. If the new one is also not a "
                "contacts-table filter, raise the count here and say which query "
                "it is. If it IS one, use figures.population_where() instead.")

    def test_every_population_name_resolves(self):
        """The names this test tells people to use have to exist."""
        sys.path.insert(0, CRM)
        import figures
        for name in ("all", "visible", "live", "active",
                     "hidden", "deleted", "hidden_only"):
            self.assertIn(name, figures.POPULATIONS)
            self.assertTrue(figures.population_says(name))
        # And the alias form, since half the converted sites are joined queries.
        self.assertIn("c.excluded", figures.population_where("active", "c"))
        with self.assertRaises(ValueError):
            figures.population_where("everyone")


class TestOneSpellingOfTheComplement(unittest.TestCase):
    """NC-115. Asking for the people who ARE hidden or deleted goes through a
    named population too — the queries behind every breakdown, where a part
    spelled differently from its total is how a ledger stops adding up."""

    def _hand_written(self):
        found = []
        for path in sorted(glob.glob(os.path.join(CRM, "*.py"))):
            base = os.path.basename(path)
            if base == "figures.py":
                continue
            code = code_of(path)
            for m in COMPLEMENT.finditer(code):
                if _is_a_write(code, m.start()):
                    continue
                line = code.count("\n", 0, m.start()) + 1
                frag = re.sub(r"\s+", " ", m.group(0))
                found.append(f"{base}:{line}: {frag}")
        return found

    def test_no_hand_written_complement_filter(self):
        found = self._hand_written()
        self.assertEqual(found, [], "\n".join([
            "", "", "Who is hidden or deleted is spelled by hand here:",
            *("    " + f for f in found), "",
            "Name the population instead:",
            "    hidden      — the people you hid, including the ones you then deleted",
            "    deleted     — the people you deleted",
            "    hidden_only — hidden and not deleted (what a breakdown needs, since",
            "                  everyone deleted is also hidden)",
            "or figures.population_not_where('active') for hidden OR deleted."]))

    def test_the_premise_it_sees_the_spellings_it_forbids(self):
        """If the pattern stopped matching, the test above would pass by seeing
        nothing. Each of these was a real line in crm/ before NC-115."""
        for line in ("SELECT COUNT(*) AS n FROM contacts WHERE excluded=1",
                     "WHERE excluded = 1 OR COALESCE(deleted, 0) = 1",
                     "OR COALESCE(excluded, 0) <> 0",
                     "where.append(\"deleted = 1\")"):
            with self.subTest(line=line):
                m = COMPLEMENT.search(line)
                self.assertIsNotNone(m)
                self.assertFalse(_is_a_write(line, m.start()))

    def test_a_write_is_not_a_question(self):
        for line in ("UPDATE contacts SET excluded = 1 WHERE name_norm = ?",
                     "UPDATE notes SET deleted = 1, updated_at = datetime('now')"):
            with self.subTest(line=line):
                self.assertTrue(_is_a_write(line, COMPLEMENT.search(line).start()))
        # ...but a test that follows the SET in the same statement still counts.
        line = "UPDATE contacts SET x = 0 WHERE excluded = 1"
        self.assertFalse(_is_a_write(line, COMPLEMENT.search(line).start()))


if __name__ == "__main__":
    unittest.main()
