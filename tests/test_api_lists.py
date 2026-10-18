"""Lists: create, membership, rename, export."""

import ast
import datetime
import os
import sys
import tokenize

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, db,
)


def _soon(days: int = 3) -> str:
    """A date inside the upcoming-events window, computed rather than hardcoded:
    a fixed date walks out of the window and the test starts finding nothing."""
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


class TestLists(Base):
    """Saved lists / event pipelines: db CRUD, API handlers, merge behavior."""

    def _mklist(self, name="Team dinner", people=("Ada", "Bob")):
        ids = [db.create_contact(self.conn, n) for n in people]
        out = app.handle_list_create(self.conn, {"name": name, "contact_ids": ids})
        return out["id"], ids

    def test_create_list_members_and_counts(self):
        lid, (ada, bob) = self._mklist()
        lists = app.handle_lists_get(self.conn)["lists"]
        self.assertEqual(lists[0]["name"], "Team dinner")
        self.assertEqual(lists[0]["members"], 2)

        detail = app.handle_list_detail(self.conn, lid)
        self.assertEqual({m["id"] for m in detail["members"]}, {ada, bob})
        self.assertTrue(all(m["list_status"] == "" for m in detail["members"]))

        # duplicates are ignored, new people are added
        eve = db.create_contact(self.conn, "Eve")
        out = app.handle_list_add(self.conn, lid, {"contact_ids": [ada, eve]})
        self.assertEqual(out["added"], 1)

    def test_list_meta_and_attendee_export(self):
        lid, (ada, bob) = self._mklist()
        db.set_field(self.conn, ada, "home_metro", "Paris")
        db.add_identifier(self.conn, ada, "email", "ada@example.com")
        self.conn.commit()
        # event date + venue round-trip
        app.handle_list_meta(self.conn, lid, {"event_date": "2027-03-12", "venue": "Paris"})
        detail = app.handle_list_detail(self.conn, lid)
        self.assertEqual(detail["event_date"], "2027-03-12")
        self.assertEqual(detail["venue"], "Paris")
        self.assertEqual(app.handle_lists_get(self.conn)["lists"][0]["event_date"], "2027-03-12")
        # attendee roster CSV
        csv_text, name = app._list_export_csv(self.conn, lid)
        self.assertEqual(name, "Team dinner")
        self.assertIn("Ada", csv_text)
        self.assertIn("ada@example.com", csv_text)
        self.assertIn("Paris", csv_text)
        # CSV formula-injection is neutralized
        evil = db.create_contact(self.conn, "=HYPERLINK(evil)")
        app.handle_list_add(self.conn, lid, {"contact_ids": [evil]})
        self.conn.commit()
        csv2, _n = app._list_export_csv(self.conn, lid)
        self.assertIn("'=HYPERLINK(evil)", csv2)   # prefixed → text, not a formula

    def test_member_status_pipeline_and_remove(self):
        lid, (ada, bob) = self._mklist()
        app.handle_list_member(self.conn, lid, {"contact_id": ada, "status": "invited"})
        app.handle_list_member(self.conn, lid, {"contact_id": bob, "status": "attending"})
        lists = app.handle_lists_get(self.conn)["lists"]
        self.assertEqual(lists[0]["invited"], 1)
        self.assertEqual(lists[0]["attending"], 1)

        with self.assertRaises(app.ApiError):
            app.handle_list_member(self.conn, lid, {"contact_id": ada, "status": "maybe"})

        app.handle_list_member(self.conn, lid, {"contact_id": ada, "remove": True})
        detail = app.handle_list_detail(self.conn, lid)
        self.assertEqual([m["id"] for m in detail["members"]], [bob])

    def test_the_rail_and_the_detail_pane_report_the_same_pipeline_counts(self):
        """NC-089. These two surfaces sit BESIDE each other in one flex row, and
        they used to count separately: the rail's `replied` counts anyone who has
        replied OR is attending, while the detail pane recounted in the browser
        and only counted the literal 'replied' status. One member at 'attending'
        was enough — the rail read "1 replied · 1 going" and the pane read
        "1 going", at the same moment, for the same list.

        The assertion is deliberately NOT about what the numbers are. It is that
        there is ONE of each. What the words should mean is a separate question
        (NC-090); that they must agree is not.

        test_member_status_pipeline_and_remove above asserts `invited` and
        `attending` and skips `replied`, which is exactly why the mismatch went unnoticed.
        """
        lid, _ = self._mklist(people=("Ada", "Bob", "Cy", "Di"))
        ids = [m["id"] for m in app.handle_list_detail(self.conn, lid)["members"]]
        for cid, status in zip(ids, ("invited", "replied", "attending", "declined")):
            app.handle_list_member(self.conn, lid, {"contact_id": cid, "status": status})

        rail = app.handle_lists_get(self.conn)["lists"][0]
        pane = app.handle_list_detail(self.conn, lid)["counts"]
        for key in ("members", "invited", "replied", "attending"):
            self.assertEqual(rail[key], pane[key],
                             f"the rail and the pane beside it disagree on {key}: "
                             f"{rail[key]} vs {pane[key]}")
        # And the pane must not be able to answer by recounting: the count of
        # 'attending' members has to survive a member whose contact row is gone
        # from the pane's own list, which is the other way these two could drift.
        self.assertEqual(pane["attending"], 1)
        self.assertEqual(pane["replied"], 2, "attending implies replied")

    def test_a_soft_deleted_member_is_counted_the_same_by_both(self):
        """The second way these could drift: get_list_members() hides a member
        whose contact was soft-deleted, and the rail's counts used to include it.
        Lists rarely have one, so this is the case that would start the
        disagreement again later rather than now."""
        lid, _ = self._mklist(people=("Ada", "Bob"))
        ids = [m["id"] for m in app.handle_list_detail(self.conn, lid)["members"]]
        app.handle_list_member(self.conn, lid, {"contact_id": ids[0], "status": "attending"})
        self.conn.execute("UPDATE contacts SET deleted = 1 WHERE id = ?", (ids[0],))
        self.conn.commit()

        rail = app.handle_lists_get(self.conn)["lists"][0]
        detail = app.handle_list_detail(self.conn, lid)
        self.assertNotIn(ids[0], [m["id"] for m in detail["members"]],
                         "a soft-deleted contact should not be listed")
        self.assertEqual(rail["members"], len(detail["members"]),
                         "the rail counts a member the pane does not show")
        self.assertEqual(rail["attending"], detail["counts"]["attending"])
        self.assertEqual(detail["counts"]["attending"], 0,
                         "the only attending member is soft-deleted, so nobody is "
                         "attending — the rail used to say 1")

    def test_the_upcoming_strip_counts_the_same_people_as_the_lists_page(self):
        """NC-104. The THIRD definition, which NC-089 unified two of and missed.

        `db.upcoming_events()` counted `COUNT(m.id)` with no filter at all — so a
        member the user had explicitly REMOVED still counted, and so would one
        whose contact was soft-deleted. `api_dates.handle_week` then renames that
        field to `people` on the way out, which is why grepping for a second
        `members` never found it.

        Before the fix, an event on the strip above People could say a larger
        `people` number than the `members` number the Lists page gave for the
        same list, one flex row apart, because removed members carry status
        '__removed__'.

        It hid for three reasons at once, all worth naming: it is in a DATES
        function rather than a lists one; it joins the two tables by hand instead
        of through `_MEMBER`; and its alias is renamed before it reaches a screen.
        """
        lid, _ = self._mklist(people=("Ada", "Bob", "Cy"))
        ids = [m["id"] for m in app.handle_list_detail(self.conn, lid)["members"]]
        app.handle_list_meta(self.conn, lid,
                             {"event_date": _soon(), "venue": "Paris"})
        app.handle_list_member(self.conn, lid,
                               {"contact_id": ids[0], "status": "attending"})
        # One removed, one soft-deleted — the two ways this could over-count.
        app.handle_list_member(self.conn, lid,
                               {"contact_id": ids[1], "remove": True})
        # The soft-deleted one is ALSO attending, so `attending` has something to
        # get wrong too. Without that, both halves of the query agree by accident
        # and an `attending` count that forgot its filter would go unnoticed.
        app.handle_list_member(self.conn, lid,
                               {"contact_id": ids[2], "status": "attending"})
        self.conn.execute("UPDATE contacts SET deleted = 1 WHERE id = ?", (ids[2],))
        self.conn.commit()

        rail = app.handle_lists_get(self.conn)["lists"][0]
        pane = app.handle_list_detail(self.conn, lid)
        strip = [e for e in db.upcoming_events(self.conn, days_ahead=30)
                 if e["id"] == lid]
        self.assertEqual(len(strip), 1, "the dated list is not on the strip")
        self.assertEqual(
            strip[0]["members"], rail["members"],
            "the upcoming-events strip and the Lists page report different "
            f"numbers for one list: {strip[0]['members']} vs {rail['members']}")
        self.assertEqual(strip[0]["members"], len(pane["members"]),
                         "the strip counts people the detail pane does not show")
        self.assertEqual(strip[0]["members"], 1,
                         "one of three: one removed, one soft-deleted")
        self.assertEqual(strip[0]["attending"], rail["attending"])
        self.assertEqual(strip[0]["attending"], 1,
                         "two members are marked attending and one of them is "
                         "soft-deleted, so the strip must say 1")

    def test_a_null_deleted_column_counts_as_present_everywhere(self):
        """Why `_MEMBER` wraps the column in COALESCE, in one assertion.

        SQLite's `c.deleted = 0` is NULL — not true — when the column is NULL, so
        a bare comparison silently drops the person. `contacts.deleted` has a
        DEFAULT of 0 and no row is NULL today, which is exactly the condition
        under which this would go unnoticed until a migration, a cloud-sync write
        or a hand-edited database produced one.

        Asserted through all three surfaces, because the failure is that they
        stop agreeing rather than that any one of them is obviously wrong.
        """
        lid, _ = self._mklist(people=("Ada", "Bob"))
        ids = [m["id"] for m in app.handle_list_detail(self.conn, lid)["members"]]
        app.handle_list_meta(self.conn, lid, {"event_date": _soon()})
        app.handle_list_member(self.conn, lid,
                               {"contact_id": ids[0], "status": "attending"})
        self.conn.execute("UPDATE contacts SET deleted = NULL WHERE id = ?", (ids[0],))
        self.conn.commit()

        rail = app.handle_lists_get(self.conn)["lists"][0]
        pane = app.handle_list_detail(self.conn, lid)
        strip = next(e for e in db.upcoming_events(self.conn, days_ahead=30)
                     if e["id"] == lid)
        for where, n in (("the Lists rail", rail["members"]),
                         ("the detail pane", len(pane["members"])),
                         ("the upcoming strip", strip["members"])):
            self.assertEqual(n, 2, f"{where} lost a member whose `deleted` "
                                   "column is NULL rather than 0")
        self.assertEqual(strip["attending"], 1)
        self.assertEqual(rail["attending"], 1)

    def test_no_fourth_definition_of_a_list_member_is_spelled_by_hand(self):
        """The structural half. Three sites answered "who is on this list" and
        two of them spelled the rule out; the third counted everyone. Every one
        of them now goes through `_MEMBER`, so pin that nothing spells it again.

        The two hand-written spellings were not even identical — `_MEMBER` wraps
        the column as `COALESCE(c.deleted, 0) = 0` and `get_list_members` had a
        bare `c.deleted = 0`, so a NULL would have put a person in the list the
        counts beside it say is not there.
        """
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "crm", "db.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        # Comments blanked, not removed: db.py explains this rule at length and a
        # raw grep would trip on the prose describing it — including the comment
        # NC-104 left quoting the very spellings this forbids. Blanked with
        # tokenize rather than a regex, because a `#` inside a SQL string is not
        # a comment and a regex cannot tell. Scoped to the four functions that
        # answer "who is on this list": `c.deleted = 0` is legitimate elsewhere
        # in this file, where `c` is chat_conversations, a different table.
        out = list(src)
        with open(path, "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type != tokenize.COMMENT:
                    continue
                heads = [len(x) + 1 for x in src.split("\n")]
                start = sum(heads[:tok.start[0] - 1]) + tok.start[1]
                end = sum(heads[:tok.end[0] - 1]) + tok.end[1]
                for i in range(start, min(end, len(out))):
                    out[i] = " "
        code = "".join(out)
        tree = ast.parse(code)
        lines = code.split("\n")
        bodies = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in (
                    "get_list_members", "get_lists", "get_list_counts",
                    "upcoming_events"):
                bodies[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
        self.assertEqual(sorted(bodies), ["get_list_counts", "get_list_members",
                                          "get_lists", "upcoming_events"],
                         "one of the four list-count functions has been renamed "
                         "or removed; this gate no longer covers it")
        for name, body in sorted(bodies.items()):
            self.assertNotIn(
                "__removed__", body,
                f"{name}() spells the removed-member rule out again. It belongs "
                "to _MEMBER — that is the whole point of NC-089 and NC-104")
            self.assertNotIn(
                "c.deleted = 0", body,
                f"{name}() has a bare `c.deleted = 0` again. _MEMBER uses "
                "COALESCE(c.deleted, 0) = 0, and the two disagree on a NULL")
            # `_PIPELINE` counts as reaching it: it is built FROM `_MEMBER`,
            # so the rule still has one owner.
            self.assertTrue(
                "{_MEMBER}" in body or "{_PIPELINE}" in body,
                f"{name}() no longer goes through _MEMBER, directly or through "
                "_PIPELINE — so it is answering the question on its own again")

    def test_rename_delete_and_missing_list_404(self):
        lid, _ = self._mklist()
        app.handle_list_rename(self.conn, lid, {"name": "Team brunch"})
        self.assertEqual(db.get_list(self.conn, lid)["name"], "Team brunch")
        app.handle_list_delete(self.conn, lid)
        with self.assertRaises(app.ApiError) as ctx:
            app.handle_list_detail(self.conn, lid)
        self.assertEqual(ctx.exception.status, 404)

    def test_merge_moves_membership(self):
        lid, (ada, bob) = self._mklist()
        db.set_list_member_status(self.conn, lid, bob, "invited")
        db.merge_contacts(self.conn, ada, bob)   # bob folds into ada
        self.conn.commit()
        detail = app.handle_list_detail(self.conn, lid)
        self.assertEqual([m["id"] for m in detail["members"]], [ada])
