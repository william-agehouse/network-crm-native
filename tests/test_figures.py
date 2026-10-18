"""NC-087 gates: one definition of what a number counts.

WHY THIS FILE EXISTS
--------------------
"How many people are in this app" had seven answers — three slightly different
totals — and they agreed only because hidden and deleted people were few and
never overlapped. That coincidence is what kept the defect invisible, so the
first gate here breaks it deliberately: a fixture where the three populations
are forced apart, so a query that filters wrongly cannot pass by luck.

Every gate is tied to a way this app has actually mis-counted, or to the specific
coincidence that hid it.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db  # noqa: E402 — CRM_DATA_DIR bootstrap

import figures as fg                # noqa: E402


class TestThePopulationsAreForcedApart(Base):
    """In an ordinary database hidden and deleted people are few and rarely
    overlap, so three different definitions land within a few people of each
    other and nothing catches a wrong one. This fixture makes them disagree by a
    wide, distinguishable margin.

    NC-115 widened it from 10 people to 13, so the three complement populations
    get answers of their own too: at 3 hidden-only, `hidden_only` and `deleted`
    both came to 3, and a swap between them would have passed."""

    def setUp(self):
        super().setUp()
        # 13 people: 4 hidden only, 2 deleted only, 1 both, 6 plain.
        for i in range(13):
            db.create_contact(self.conn, f"P{i}")
        ids = [r[0] for r in self.conn.execute("SELECT id FROM contacts ORDER BY id")]
        self.conn.execute("UPDATE contacts SET excluded=1 WHERE id IN (?,?,?,?)", ids[:4])
        self.conn.execute("UPDATE contacts SET deleted=1 WHERE id IN (?,?)", ids[4:6])
        self.conn.execute("UPDATE contacts SET excluded=1, deleted=1 WHERE id=?", (ids[6],))
        self.conn.commit()

    def ids(self, population):
        where = fg.population_where(population)
        sql = "SELECT id FROM contacts" + (f" WHERE {where}" if where else "")
        return {r[0] for r in self.conn.execute(sql)}

    def n(self, population):
        where = fg.population_where(population)
        sql = "SELECT COUNT(*) FROM contacts" + (f" WHERE {where}" if where else "")
        return self.conn.execute(sql).fetchone()[0]

    def test_every_population_gives_a_different_answer(self):
        got = {p: self.n(p) for p in fg.POPULATIONS}
        self.assertEqual(got, {"all": 13, "visible": 8, "live": 10, "active": 6,
                               "hidden": 5, "deleted": 3, "hidden_only": 4},
                         "the predicates no longer separate. If two of them "
                         "agree on this fixture, one of them is wrong — an "
                         "ordinary database cannot tell you, because there they "
                         "agree by coincidence")

    def test_active_is_not_the_same_as_either_half(self):
        """`excluded` and `deleted` are written by separate paths — a sync can
        write them independently — so "both filters" is separately reachable and
        not the intersection of two others by construction."""
        self.assertLess(self.n("active"), self.n("visible"))
        self.assertLess(self.n("active"), self.n("live"))

    def test_each_way_out_is_exactly_who_its_way_in_leaves_out(self):
        """NC-115. `hidden` and `visible` split the file with nobody in both and
        nobody in neither; so do `deleted` and `live`. Checked on the ids, not
        the counts, so two wrong sets of the right size cannot pass."""
        everyone = self.ids("all")
        for way_in, way_out in (("visible", "hidden"), ("live", "deleted")):
            with self.subTest(pair=(way_in, way_out)):
                self.assertEqual(self.ids(way_in) & self.ids(way_out), set())
                self.assertEqual(self.ids(way_in) | self.ids(way_out), everyone)

    def test_the_ledgers_three_parts_are_everyone_once_each(self):
        """The partition the people ledger publishes: everyone you can use, the
        ones you only hid, the ones you deleted. `hidden` in place of
        `hidden_only` would count the person who is both twice."""
        parts = [self.ids(p) for p in ("active", "hidden_only", "deleted")]
        self.assertEqual(sum(len(p) for p in parts), len(self.ids("all")))
        self.assertEqual(set().union(*parts), self.ids("all"))

    def test_a_value_other_than_one_still_lands_on_exactly_one_side(self):
        """Both columns are plain integers with no constraint. `excluded = 1`
        would put a 2 in neither `visible` nor `hidden`; `<> 0` puts it in one."""
        pid = db.create_contact(self.conn, "Odd")
        self.conn.execute("UPDATE contacts SET excluded=2, deleted=NULL WHERE id=?", (pid,))
        self.assertIn(pid, self.ids("hidden"))
        self.assertNotIn(pid, self.ids("visible"))
        self.assertIn(pid, self.ids("live"))

    def test_an_alias_reaches_the_same_rows(self):
        """Without an alias this is unusable in exactly the queries that most
        need it: the joined ones."""
        w = fg.population_where("active", "c")
        self.assertIn("c.excluded", w)
        n = self.conn.execute(
            f"SELECT COUNT(*) FROM contacts c WHERE {w}").fetchone()[0]
        self.assertEqual(n, self.n("active"))

    def test_all_returns_an_empty_predicate_rather_than_a_tautology(self):
        """So a caller can test for emptiness and omit WHERE entirely, instead of
        every query growing a `1=1`."""
        self.assertEqual(fg.population_where("all"), "")


class TestAnUnknownNameIsRefusedLoudly(unittest.TestCase):
    def test_a_fifth_population_must_be_registered_not_improvised(self):
        with self.assertRaises(ValueError) as e:
            fg.population_where("everyone_except_bots")
        self.assertIn("seven answers", str(e.exception),
                      "the error should say why inline filters are the problem, "
                      "not merely that the name is unknown")

    def test_an_unknown_unit_is_refused(self):
        with self.assertRaises(ValueError):
            fg.figure(1, "contacts", "all")   # a synonym for people — the drift

    def test_an_unknown_side_is_refused(self):
        with self.assertRaises(ValueError):
            fg.figure(1, "people", "all", side="guessed")


class TestAFigureCanBeChecked(unittest.TestCase):
    def test_it_carries_all_three_axes(self):
        f = fg.figure(40, "people", "active", side=fg.SIDE_ARCHIVE)
        for k in ("n", "unit", "population", "side"):
            self.assertIn(k, f)

    def test_the_population_meaning_travels_with_the_number(self):
        """So a screen cannot render the figure and describe it from memory."""
        f = fg.figure(1, "people", "visible")
        self.assertEqual(f["population_says"], fg.population_says("visible"))
        self.assertIn("hid", f["population_says"])

    def test_a_denominator_renders_as_n_of_m(self):
        f = fg.figure(40, "people", "active", of=1200)
        self.assertEqual(fg.figure_says(f), "40 of 1,200 people")

    def test_a_bare_figure_still_names_its_unit(self):
        self.assertEqual(fg.figure_says(fg.figure(1200, "people", "active")),
                         "1,200 people")

    def test_one_of_a_thing_is_singular(self):
        """"1 messages were skipped" reads like a bug even when it is right."""
        self.assertEqual(fg.figure_says(fg.figure(1, "messages", "all")), "1 message")

    def test_the_share_is_computed_once_here_not_per_screen(self):
        self.assertEqual(fg.figure(1, "people", "all", of=8)["share"], 12.5)

    def test_a_zero_denominator_does_not_divide(self):
        self.assertIsNone(fg.figure(0, "people", "all", of=0)["share"])

    def test_a_caveat_can_be_published_rather_than_absorbed(self):
        f = fg.figure(3, "messages", "all", says="the counter has no bodies behind it")
        self.assertIn("no bodies", fg.figure_says(f))


class TestTheResidueLedger(unittest.TestCase):
    """crm/intake.py and crm/style_profile.py, generalised. The contract: if a
    future change starts losing people, the number goes non-zero and the receipt
    says so instead of the count quietly shrinking."""

    def test_an_identity_that_holds_says_nothing(self):
        led = fg.ledger(10, {"a": 6, "b": 4})
        self.assertEqual(led["unexplained"], 0)
        self.assertEqual(fg.ledger_says(led), "")

    def test_a_leak_is_named(self):
        led = fg.ledger(10, {"a": 6})
        self.assertEqual(led["unexplained"], 4)
        self.assertIn("not accounted for", fg.ledger_says(led))

    def test_double_counting_is_a_different_complaint_from_leaking(self):
        """Signed on purpose. A max(0, ...) here would hide the one bug that
        makes a total look healthier than it is."""
        led = fg.ledger(10, {"a": 6, "b": 7})
        self.assertEqual(led["unexplained"], -3)
        self.assertIn("counted twice", fg.ledger_says(led))
        self.assertNotIn("not accounted for", fg.ledger_says(led))


class TestOnePayloadCannotContradictItself(unittest.TestCase):
    """The rule NC-087 set, as a function so the test is not its only reader."""

    def test_a_consistent_payload_has_no_complaints(self):
        p = {"people": fg.figure(1200, "people", "active"),
             "with_history": fg.figure(500, "people", "active", of=1200)}
        self.assertEqual(fg.check_payload(p), [])

    def test_a_part_larger_than_its_whole_is_caught(self):
        p = {"x": fg.figure(5, "people", "all", of=3)}
        self.assertEqual(len(fg.check_payload(p)), 1)

    def test_a_hand_written_meaning_that_drifted_is_caught(self):
        f = fg.figure(1, "people", "visible")
        f["population_says"] = "everyone"
        self.assertIn("written by hand", fg.check_payload({"f": f})[0])

    def test_it_finds_figures_nested_in_lists(self):
        p = {"rows": [{"deep": {"f": fg.figure(5, "people", "all", of=3)}}]}
        self.assertEqual(len(fg.check_payload(p)), 1)

    def test_a_figure_is_recognised_by_shape_not_by_a_marker(self):
        """So an endpoint cannot half-adopt this by leaving the marker off."""
        found = dict(fg.walk({"a": {"n": 1, "unit": "people", "population": "all"}}))
        self.assertEqual(list(found), ["a"])

    def test_a_plain_dict_of_numbers_is_not_mistaken_for_a_figure(self):
        self.assertEqual(list(fg.walk({"total": 5, "shown": 2})), [])


class TestSettingsAndPeopleCountTheSamePeople(Base):
    """NC-087's headline: within one window, Settings said one total under the word
    "contacts" and the People page said a slightly different one under the word
    "people". One click
    apart, both true, and neither naming its population.

    The fixture forces the three states apart, because in an ordinary database they
    are few — close enough that a wrong filter still looks plausible.
    """

    def setUp(self):
        super().setUp()
        for i in range(12):
            db.create_contact(self.conn, f"P{i}")
        ids = [r[0] for r in self.conn.execute("SELECT id FROM contacts ORDER BY id")]
        # 5 hidden-only, 3 deleted (which db.set_deleted also hides), and — the
        # case an ordinary database does not have — 1 deleted WITHOUT being hidden.
        # `excluded` and `deleted` are separate columns and a sync can write
        # them independently; exactly one function keeps "deleted implies
        # excluded" true. Without this row, `visible` and `active` agree and a
        # filter that drops the deleted check passes.
        self.conn.execute("UPDATE contacts SET excluded=1 WHERE id IN (?,?,?,?,?)", ids[:5])
        self.conn.execute(
            "UPDATE contacts SET excluded=1, deleted=1 WHERE id IN (?,?,?)", ids[5:8])
        self.conn.execute("UPDATE contacts SET deleted=1 WHERE id = ?", (ids[8],))
        self.conn.commit()
        self.diag = app.api_instance._settings_diagnostics(self.conn)

    def test_the_parts_add_up_to_the_file_by_construction(self):
        led = self.diag["people"]
        self.assertEqual(led["total"], 12)
        self.assertEqual(led["parts"], {"active": 3, "hidden": 5, "deleted": 4})
        self.assertEqual(led["unexplained"], 0)

    def test_hidden_does_not_double_count_the_deleted(self):
        """`db.set_deleted` sets BOTH columns, so a deleted person is also
        excluded. Counting them in both buckets makes the breakdown add up to
        more than the file holds — which `unexplained` would then report as
        negative, the double-counting complaint."""
        led = self.diag["people"]
        excluded_any = self.conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE COALESCE(excluded,0)=1").fetchone()[0]
        self.assertEqual(excluded_any, 8, "fixture: 5 hidden plus 3 deleted-and-hidden")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM contacts WHERE "
                              "COALESCE(deleted,0)=1 AND COALESCE(excluded,0)=0")
            .fetchone()[0], 1,
            "fixture: one person deleted without being hidden, so `visible` and "
            "`active` cannot agree by accident")
        self.assertEqual(led["parts"]["hidden"], 5,
                         "hidden must exclude the deleted, or the parts sum to 16")

    def test_settings_leads_with_the_number_the_people_page_shows(self):
        """The two screens, asked the same question, in the same test."""
        people_page = db.count_contacts(self.conn, "", "all")
        self.assertEqual(self.diag["people"]["parts"]["active"], people_page,
                         "Settings and the People page report different totals "
                         "again — the exact defect NC-087 opened with")

    def test_the_figure_carries_its_population_and_its_denominator(self):
        f = self.diag["people"]["figure"]
        self.assertEqual(f["population"], "active")
        self.assertEqual(f["of"], 12)
        self.assertEqual(fg.figure_says(f), "3 of 12 people")

    def test_the_payload_does_not_contradict_itself(self):
        self.assertEqual(fg.check_payload(self.diag), [])

    def test_a_leak_would_be_reported_rather_than_absorbed(self):
        """The residue's whole purpose. A fourth state nobody counted must make
        the number go non-zero and the sentence appear, instead of the total
        quietly shrinking."""
        led = dict(self.diag["people"])
        led = fg.ledger(led["total"], {"active": 3, "hidden": 5})
        self.assertEqual(led["unexplained"], 4)
        self.assertIn("not accounted for", fg.ledger_says(led))

    def test_the_bare_contacts_field_is_still_there_for_its_callers(self):
        """Five callers read `contacts` as a plain int. Removing it to make a
        point would break them; the ledger is added beside it, not instead."""
        self.assertEqual(self.diag["contacts"], 12)


class TestTheScreenRendersTheLedgerRatherThanRecomputingIt(unittest.TestCase):
    """The display half — people.js's rule: one owner, nothing counts again.

    These gates fail if the component stops drawing the server's ledger, wherever
    it lives: settings.js, then sources.js (NC-105), the Data page now. Knowing
    nothing about which page it is on is what made that move two import lines.
    """

    @staticmethod
    def _js():
        # NC-236: was sources.js (DataStats), gone with the old Data page. The
        # People line of This install in data.js draws the same ledger now.
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "app", "data.js")
        src = open(path, encoding="utf-8").read()
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        return re.sub(r"(?<![:/])//[^\n]*", "", src)

    def test_the_stat_reads_the_server_ledger(self):
        js = self._js()
        i = js.index('label="People"')
        block = js[i:i + 250]
        self.assertIn("<${Figure} fig=${people.figure}", block,
                      "the People line is no longer drawn from the server's "
                      "figure, which counts the people you can see and use")
        self.assertIn("const people = install.people", js,
                      "the People line no longer reads the ledger's people")
        # The value is the server's figure, which is the active count. Pin the
        # server half too, so the page cannot keep reading a field that has
        # quietly come to mean the whole file.
        py = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "crm", "api_instance.py"),
            encoding="utf-8").read()
        j = py.index('led["figure"] = figures.figure(')
        self.assertIn("active", py[j:j + 80],
                      "the people figure no longer counts the active people, so "
                      "it asserts a different total from the People page again")

    def test_the_residue_is_shown_only_when_it_is_non_zero(self):
        """An identity that holds needs no sentence. Rendering "0 unexplained"
        teaches the reader to ignore the line that matters. The other half: when
        the parts stop adding up, the page has to say so. The rebuilt People
        line works out "more you hid or deleted" from the parts and never reads
        the ledger's `unexplained`, so a leak would show as an ordinary
        breakdown."""
        js = self._js()
        i = js.index('label="People"')
        self.assertIn("unexplained", js[i:i + 700],
                      "the People line no longer branches on the residue, so a "
                      "leak would render as an ordinary breakdown")

    def test_it_does_not_add_the_parts_up_itself(self):
        """`left` sums hidden + deleted, which is a presentation choice about two
        named parts — not a second answer to "how many people". What it must
        never do is recompute the total."""
        js = self._js()
        self.assertNotIn("parts.active + ", js)
        self.assertNotIn("+ p.parts.active", js)
        self.assertNotIn("+ people.parts.active", js)


class TestItStaysAVocabularyAndNotAQueryLayer(unittest.TestCase):
    """db.py owns the contacts table. If this file grows SELECTs, the app has two
    query layers rather than one shared vocabulary — which is how it got seven
    definitions in the first place."""

    @staticmethod
    def _code():
        """figures.py with its docstrings removed — the file explains SQL at
        length, so a gate reading raw source would trip on its own prose. Same
        trap `code_of` and `js_code` exist for in tests/helpers.py."""
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "crm", "figures.py")
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and ast.get_docstring(node):
                doc = node.body[0]
                spans.append((doc.lineno, doc.end_lineno))
        lines = src.split("\n")
        keep = [ln for i, ln in enumerate(lines, 1)
                if not any(a <= i <= b for a, b in spans)]
        return "\n".join(keep)

    def test_it_holds_no_sql_beyond_the_four_predicates(self):
        code = self._code()
        for word in ("SELECT", "FROM ", "JOIN ", "GROUP BY", "conn."):
            self.assertNotIn(
                word, code,
                f"figures.py now contains {word!r}. It is a vocabulary, not a "
                "query layer — db.py owns the contacts table, and a second query "
                "layer is how this app came to have seven definitions.")

    def test_the_predicates_it_does_hold_are_only_the_registered_ones(self):
        """The other half: don't satisfy the gate above by moving the filters
        out of here, which would put them back to being spelled inline."""
        code = self._code()
        # Eight COALESCE calls: 'all' has none, 'visible', 'live', 'hidden' and
        # 'deleted' one each, 'active' and 'hidden_only' two (NC-115 added the
        # last three). Every one of them is inside the registry.
        in_registry = sum(sql.count("COALESCE(") for _says, sql in fg.POPULATIONS.values())
        self.assertEqual(in_registry, 8)
        self.assertEqual(code.count("COALESCE("), in_registry,
                         "a population predicate was added or removed without "
                         "going through the registry")

    def test_it_imports_nothing_from_this_app(self):
        """A vocabulary every layer can use must sit below all of them."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "crm", "figures.py"), encoding="utf-8").read()
        # Read the real import statements, not lines that merely start with
        # "from " — a reflowed docstring can begin a line that way.
        import ast
        for node in ast.parse(src).body:
            if isinstance(node, ast.Import):
                self.fail(f"figures.py imports {ast.unparse(node)!r}; it must "
                          "stay importable from db.py without a cycle")
            if isinstance(node, ast.ImportFrom):
                self.assertTrue(node.module == "typing" and not node.level,
                                f"figures.py imports {ast.unparse(node)!r}; it must "
                                "stay importable from db.py without a cycle")


if __name__ == "__main__":
    unittest.main()
