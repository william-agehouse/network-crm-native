"""NC-122. Every count of people on the Data page counts the same people.

The Data page had five answers to "how many people", all under the word
"people", from four different groups:

  the setup panel's "people in total"   everyone not hidden (`visible`)
  the tile at the bottom, "people"      everyone not hidden or deleted (`active`)
  the LinkedIn coverage bar             everyone not deleted (`live`)
  the LinkedIn download headline        everyone, hidden and deleted included
  "What it brought"                     everyone, hidden and deleted included

Two of them could agree only because every person who had been deleted was also
hidden. With data that does not have that coincidence, the page showed "PEOPLE IN
TOTAL 2" above "PEOPLE 1".

Now every one of them counts `active` — the people you can see and use, the ones
the People page lists — and the page says so once, with how many that leaves
out. The fixture below has four people, one in each state, each carrying every
kind of data the page counts. So every number of people must be
1, and any figure still counting a different group reads 2, 3 or 4.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, arc, db, js_code_of, up_to  # noqa: E402

import api_contacts  # noqa: E402
import api_import  # noqa: E402
import api_instance  # noqa: E402
import api_linkedin  # noqa: E402
import api_provenance  # noqa: E402
import api_setup  # noqa: E402
import figures  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")

# The one person anyone can see; the other three are the ways out.
PEOPLE = ("Plain Person", "Hidden Only", "Hidden And Deleted", "Deleted Only")


class _FourPeople(Base):
    def setUp(self):
        super().setUp()
        self.arcdir = tempfile.mkdtemp(prefix="nc122-arc-")
        self._orig_arc = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(self.arcdir, "archive.db")
        arc.init_archive(arc.ARCHIVE_DB_PATH)
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)

        self.ids = {}
        for name in PEOPLE:
            cid = db.create_contact(self.conn, name)
            self.ids[name] = cid
            self.conn.execute(
                "UPDATE contacts SET source = 'linkedin', linkedin_url = ?, "
                "photo = ?, photo_source = 'linkedin', facts_source = ?, "
                "position = 'Engineer', imsg_count = 1, wa_count = 1, "
                "msg_count = 1, em_count = 1 WHERE id = ?",
                (f"https://linkedin.com/in/p{cid}", f"/p/{cid}.jpg",
                 api_linkedin.FACTS_CSV, cid))
            db.add_identifier(self.conn, cid, "email", f"p{cid}@example.com",
                              source=api_linkedin.FACTS_CSV)
            self.conn.execute(
                "INSERT INTO contact_dates (contact_id, kind, label, on_date, "
                "source, source_uid) VALUES (?, 'meeting', 'Lunch', "
                "'2026-06-01', 'calendar', ?)", (cid, f"m{cid}"))
            arc.insert_message(ac, contact_id=cid, channel="linkedin", direction=0,
                               ts="2026-06-01 10:00:00", body="x",
                               source_key=f"li-{cid}")
        # A group conversation's own row: a message, but nobody's.
        arc.insert_message(ac, contact_id=arc.NO_PERSON, channel="linkedin",
                           direction=0, ts="2026-06-02 10:00:00", body="x",
                           source_key="li-group")
        # A person deleted for good while the archive was busy: the row in
        # crm.db is gone, their message is not. Nobody's in any group, so it
        # must not count as a person anywhere.
        arc.insert_message(ac, contact_id=987654, channel="linkedin",
                           direction=0, ts="2026-06-03 10:00:00", body="x",
                           source_key="li-orphan")
        ac.commit()
        ac.close()

        db.set_excluded(self.conn, self.ids["Hidden Only"], 1)
        db.set_deleted(self.conn, self.ids["Hidden And Deleted"], 1)
        # Deleted without being hidden. db.set_deleted never writes this, but a
        # sync can, and it is the one state where `visible` and `active` differ.
        self.conn.execute("UPDATE contacts SET deleted = 1 WHERE id = ?",
                          (self.ids["Deleted Only"],))
        self.conn.commit()

    def tearDown(self):
        arc.ARCHIVE_DB_PATH = self._orig_arc
        shutil.rmtree(self.arcdir, ignore_errors=True)
        super().tearDown()


class TestTheFixtureSeparatesTheGroups(_FourPeople):
    def test_the_four_groups_give_four_different_answers(self):
        """The premise. If these ever coincide, every test below passes whether
        or not the page counts one group."""
        def n(pop):
            where = figures.population_where(pop)
            return self.conn.execute("SELECT COUNT(*) FROM contacts"
                                     + (f" WHERE {where}" if where else "")).fetchone()[0]
        self.assertEqual({p: n(p) for p in ("all", "visible", "live", "active")},
                         {"all": 4, "visible": 2, "live": 2, "active": 1})
        self.assertEqual(db.ids_outside(self.conn, "visible"),
                         {self.ids["Hidden Only"], self.ids["Hidden And Deleted"]})
        self.assertEqual(db.ids_outside(self.conn, "live"),
                         {self.ids["Hidden And Deleted"], self.ids["Deleted Only"]})


class TestTheStatsCommandCountsTheSamePeople(_FourPeople):
    """NC-087. `search_cli.py --stats` printed "With message history" from
    LinkedIn's column alone, over all four people."""

    def test_it_prints_one_person_with_history(self):
        import contextlib
        import io
        from unittest import mock
        import search_cli
        out = io.StringIO()
        with mock.patch.object(db, "DB_PATH", self.path), contextlib.redirect_stdout(out):
            search_cli.stats()
        text = out.getvalue()
        self.assertIn("People: 1  (", text)
        self.assertIn("People you have messages with, on any channel: 1\n", text)
        self.assertNotIn("With message history", text)


class TestEveryPeopleCountIsOne(_FourPeople):

    def test_the_setup_panel(self):
        out = api_setup.handle_setup_progress(self.conn)
        self.assertEqual(out["people"], 1, "people at the top of the page")
        self.assertEqual(out["history"], 1)
        self.assertEqual(out["photos"], 1)
        self.assertEqual(out["titles"], 1)
        for s in out["steps"]:
            with self.subTest(step=s["key"]):
                self.assertLessEqual(s["have"], 1, f"{s['key']}: {s['have']} {s['unit']}")
        steps = {s["key"]: s["have"] for s in out["steps"]}
        for key in ("contacts", "calendar", "imessage", "whatsapp", "linkedin",
                    "enrichment"):
            with self.subTest(step=key):
                self.assertEqual(steps[key], 1)

    def test_the_setup_panel_says_who_it_counted(self):
        out = api_setup.handle_setup_progress(self.conn)
        self.assertEqual(out["scope"], {
            "population": "active",
            "says": figures.population_says("active"),
            "left_out": 3,
        })
        self.assertEqual(figures.check_payload(out), [])
        self.assertEqual({f["population"] for _, f in figures.walk(out)}, {"active"})

    def test_the_source_rows(self):
        cov = api_contacts._channel_coverage(self.conn)
        for key in ("linkedin", "imessage", "whatsapp", "email"):
            with self.subTest(channel=key):
                self.assertEqual(cov[key], 1, f"{key} with history")
        self.assertEqual(cov["any_history"], 1)
        photos = api_import.handle_import_sources(self.conn)
        self.assertEqual(photos["photos_count"], 1)
        self.assertEqual(photos["photos_by_source"], {"linkedin": 1})

    def test_the_linkedin_panel(self):
        st = api_linkedin.handle_linkedin_status(self.conn)
        self.assertEqual(st["download"]["contacts_from_linkedin"], 1,
                         "people from the export")
        self.assertEqual(st["download"]["facts_from_csv"], 1)
        self.assertEqual(st["api"]["people_with_messages"], 1, "across N people")
        self.assertEqual(st["api"]["messages"], 6,
                         "the message count is every LinkedIn message saved, "
                         "including a group's own row and the hidden people's")
        sc = st["scraper"]
        self.assertEqual(sc["contacts_total"], 1)
        self.assertEqual(sc["with_url"], 1)
        self.assertEqual(sc["no_url"], 0,
                         "a hidden person counted as having no profile link")
        self.assertEqual(sc["coverage"]["total"], 1)
        self.assertEqual(sc["coverage"]["population"], "active")
        self.assertEqual(sc["coverage"]["unexplained"], 0)

    def test_what_it_brought(self):
        out = api_provenance.handle_provenance(self.conn, {"source": ["linkedin"]})
        self.assertEqual(out["population"], "active")
        kinds = {k["kind"]: k for k in out["panels"][0]["kinds"]}
        self.assertEqual((kinds["people"]["count"], kinds["people"]["of_total"]), (1, 1),
                         "People it first added: N of M")
        self.assertEqual(kinds["messages"]["detail"], "1 person")
        self.assertEqual(kinds["messages"]["count"], 6)
        self.assertEqual(kinds["identifiers"]["count"], 1)
        self.assertEqual(kinds["facts"]["count"], 1)
        cal = api_provenance.handle_provenance(self.conn, {"source": ["calendar"]})
        dates = {k["kind"]: k for k in cal["panels"][0]["kinds"]}["dates"]
        self.assertEqual(dates["count"], 1, "a hidden person's meeting counted")

    def test_the_tile_at_the_bottom(self):
        diag = api_instance._settings_diagnostics(self.conn)
        self.assertEqual(diag["people"]["parts"]["active"], 1)
        self.assertEqual(diag["photos"], 1)


class TestThePageSaysItOnce(unittest.TestCase):
    """The page half: the stated group comes from the server, and no count of
    people is typed as a bare plural."""

    @staticmethod
    def js(name):
        """The code with its comments blanked, so a comment that quotes the old
        wording does not read as the old wording coming back."""
        return js_code_of(os.path.join(APP, name))

    # NC-236: "the headline draws the server's sentence" went with the old setup
    # panel, whose headline printed a group sentence of its own. The new page has
    # no such headline. Its figures come from Figure (ledger.js), which refuses to
    # draw a number that does not say which people it counts, and the People and
    # Messages lines of This install use it.
    def test_the_install_figures_say_who_they_count(self):
        data = self.js("data.js")
        for label in ("People", "Messages"):
            with self.subTest(row=label):
                block = up_to(data, f'label="{label}"', "/>")
                self.assertIn("<${Figure} fig=", block,
                              f"the {label} line of This install is a bare number again")
        ledger = self.js("ledger.js")
        self.assertIn("!fig.population", ledger,
                      "Figure no longer refuses a number with no group named")
        self.assertIn("fig.population_says", ledger,
                      "Figure no longer names the group when you hover it")

    def test_no_count_of_people_is_a_bare_plural(self):
        """"1 people with history" is what a number written straight in front of the
        word "people" produces; it cannot say "person"."""
        bare = re.compile(r"\}\s+people\b(?!=)")
        # NC-236: setup-progress.js, linkedin.js, sources.js and dataview.js are
        # gone; data.js and ledger.js draw what they showed.
        for name in ("data.js", "ledger.js", "settings.js", "ui.js"):
            with self.subTest(file=name):
                hits = [ln.strip() for ln in self.js(name).splitlines()
                        if bare.search(ln)]
                self.assertEqual(hits, [])

    def test_the_setup_units_agree_with_their_number(self):
        for s in api_setup.STEPS:
            with self.subTest(step=s["key"]):
                self.assertTrue(api_setup._unit(s["unit"], 1).startswith("person"),
                                s["unit"])
                self.assertEqual(api_setup._unit(s["unit"], 2), s["unit"])


if __name__ == "__main__":
    unittest.main()
