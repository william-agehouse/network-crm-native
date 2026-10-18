"""Contacts as an API resource: the contact detail payload, the review queue, and duplicate detection/merge."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, db,
)


class TestAppApi(Base):
    def test_handlers(self):
        # create
        cid = app.handle_create_contact(self.conn, {"name": "Ada"})["id"]
        # add note marks described; how_we_met is not auto-seeded from it, which
        # would be a frozen, uneditable copy of note #1
        app.handle_add_note(self.conn, cid, {"body": "Met at a salon."})
        detail = app.handle_get_contact(self.conn, cid)
        self.assertEqual(detail["status"], "described")
        self.assertEqual(detail["how_we_met"], "")
        self.assertEqual(len(detail["notes"]), 1)
        # how_we_met is its own editable field
        app.handle_set_howwemet(self.conn, cid, {"text": "Met at a salon."})
        self.assertEqual(app.handle_get_contact(self.conn, cid)["how_we_met"], "Met at a salon.")
        # anti-clobber: an empty value never wipes a field that has one
        app.handle_set_field(self.conn, cid, {"field": "company", "value": "ACME"})
        app.handle_set_field(self.conn, cid, {"field": "company", "value": ""})
        self.assertEqual(app.handle_get_contact(self.conn, cid)["company"], "ACME")
        app.handle_set_field(self.conn, cid, {"field": "company", "value": "", "clear": True})
        self.assertEqual(app.handle_get_contact(self.conn, cid)["company"], "")
        # identifiers can be added AND removed
        app.handle_add_identifier(self.conn, cid, {"kind": "phone", "value": "+46701740605"})
        self.assertEqual([i["value"] for i in db.get_identifiers(self.conn, cid)], ["+46701740605"])
        app.handle_add_identifier(self.conn, cid, {"kind": "phone", "value": "+46701740605", "remove": True})
        self.assertEqual(db.get_identifiers(self.conn, cid), [])
        # list + status
        self.assertTrue(app.handle_list_contacts(self.conn, {})["contacts"])
        self.assertIn("counts", app.handle_status(self.conn))
        # validation errors
        with self.assertRaises(app.ApiError) as e1:
            app.handle_set_category(self.conn, cid, {"category": "nonsense"})
        self.assertEqual(e1.exception.status, 400)
        with self.assertRaises(app.ApiError) as e2:
            app.handle_get_contact(self.conn, 999999)
        self.assertEqual(e2.exception.status, 404)


class TestReview(Base):
    """The review-batch guarantees: only 'new', never excluded, never already
    described, plus the remaining count and the serializer shape.

    These used to go through GET /api/review, which had zero frontend callers and
    was deleted (NC-017). The guarantees are NOT dead with it — Today's queue calls
    the same db.pick_review_batch (crm/api_views.py:59) — so the tests were pointed
    at the machinery instead of at the retired endpoint rather than deleted with it.

    pick_review_batch tops a short batch up with already-reviewed people so a
    session stays productive, so these keep a full queue of 'new' contacts and
    check the status='new' guarantee directly."""

    def _batch_ids(self, size=5):
        rows = db.pick_review_batch(self.conn, limit=size)
        out = {"contacts": [app._contact_detail(self.conn, r) for r in rows],
               "remaining": app._new_count(self.conn)}
        return {c["id"] for c in out["contacts"]}, out

    def _seed_new(self, n, prefix="Filler"):
        return [db.create_contact(self.conn, f"{prefix} {i}") for i in range(n)]

    def test_review_only_new_non_excluded_and_remaining(self):
        new_ids = set(self._seed_new(6))            # more 'new' than the batch size
        hide = db.create_contact(self.conn, "Hidden Hank")
        db.set_excluded(self.conn, hide, 1)
        done = db.create_contact(self.conn, "Done Dora")
        db.mark_reviewed(self.conn, done, "described")
        self.conn.commit()
        ids, out = self._batch_ids(size=5)
        self.assertEqual(len(ids), 5)
        self.assertTrue(ids <= new_ids)            # every returned id is a 'new' contact
        self.assertNotIn(hide, ids)                # excluded never surfaces
        self.assertNotIn(done, ids)                # described never surfaces
        self.assertEqual(out["remaining"], 6)      # six 'new', non-excluded left
        # serializer is the full _contact_detail shape
        self.assertIn("notes", out["contacts"][0])
        self.assertIn("how_we_met", out["contacts"][0])

    def test_size_parsed_and_clamped(self):
        ids, out = self._batch_ids()
        self.assertEqual(out, {"contacts": [], "remaining": 0})  # empty db, valid shape
        # junk -> default 5, huge -> capped at REVIEW_MAX_SIZE, zero -> floor 1
        self.assertEqual(app._parse_size({"size": ["junk"]}), 5)
        self.assertEqual(app._parse_size({"size": ["999"]}), app.REVIEW_MAX_SIZE)
        self.assertEqual(app._parse_size({"size": ["0"]}), 1)

    def test_skip_removes_from_fresh_batch(self):
        # 6 'new' contacts, batch size 5: the fallback that tops a short batch up
        # with reviewed people never fires, so a skipped person simply drops out.
        a = db.create_contact(self.conn, "Skip Sam")
        fillers = set(self._seed_new(5))
        self.conn.commit()
        self.assertIn(a, set(self._batch_ids(size=6)[0]))  # appears before skipping
        res = app.handle_skip(self.conn, a)
        self.assertEqual(res, {"ok": True})
        self.assertEqual(db.get_contact(self.conn, a)["status"], "skipped")
        ids_after, out_after = self._batch_ids(size=5)
        self.assertNotIn(a, ids_after)             # skipped drops out of a fresh batch
        self.assertEqual(ids_after, fillers)
        self.assertEqual(out_after["remaining"], 5)  # only the 5 fillers remain 'new'

    def test_add_note_marks_described_and_removes_from_batch(self):
        a = db.create_contact(self.conn, "Note Nora")
        fillers = set(self._seed_new(5))
        self.conn.commit()
        self.assertIn(a, set(self._batch_ids(size=6)[0]))  # appears before describing
        app.handle_add_note(self.conn, a, {"body": "Met at a conference."})
        self.assertEqual(db.get_contact(self.conn, a)["status"], "described")
        ids_after, out_after = self._batch_ids(size=5)
        self.assertNotIn(a, ids_after)             # described drops out
        self.assertEqual(ids_after, fillers)
        self.assertEqual(out_after["remaining"], 5)

    def test_skip_on_missing_contact_404(self):
        with self.assertRaises(app.ApiError) as e:
            app.handle_skip(self.conn, 999999)
        self.assertEqual(e.exception.status, 404)

    def test_never_met_and_no_context_leave_the_flow(self):
        nm = db.create_contact(self.conn, "Stranger Stan")
        nc = db.create_contact(self.conn, "Foggy Fred")
        fillers = set(self._seed_new(5))
        self.conn.commit()
        # both appear in a big-enough batch first
        self.assertTrue({nm, nc} <= set(self._batch_ids(size=8)[0]))
        app.handle_review_status(self.conn, nm, "never_met")
        app.handle_review_status(self.conn, nc, "no_context")
        self.assertEqual(db.get_contact(self.conn, nm)["status"], "never_met")
        self.assertEqual(db.get_contact(self.conn, nc)["status"], "no_context")
        # neither returns, even when the batch is short enough to trigger top-up
        ids_after = set(self._batch_ids(size=8)[0])
        self.assertNotIn(nm, ids_after)
        self.assertNotIn(nc, ids_after)
        self.assertEqual(ids_after, fillers)

    def test_never_met_excluded_from_reconnect_but_no_context_kept(self):
        old = "2000-01-01 00:00:00"
        nm = db.create_contact(self.conn, "Stranger Stan")
        nc = db.create_contact(self.conn, "Foggy Fred")
        for cid in (nm, nc):  # give both a relationship signal + stale last-seen
            self.conn.execute("UPDATE contacts SET imsg_count=3, imsg_last=? WHERE id=?", (old, cid))
        db.mark_reviewed(self.conn, nm, "never_met")
        db.mark_reviewed(self.conn, nc, "no_context")
        self.conn.commit()
        ids = {r["id"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertNotIn(nm, ids)   # never met → not a reconnect candidate
        self.assertIn(nc, ids)      # met but no context → still worth reconnecting

    def test_review_status_action_map_dispatch(self):
        self.assertEqual(app.REVIEW_STATUS_ACTIONS["nevermet"], "never_met")
        self.assertEqual(app.REVIEW_STATUS_ACTIONS["nocontext"], "no_context")
        self.assertEqual(app.REVIEW_STATUS_ACTIONS["skip"], "skipped")


class TestDuplicates(Base):
    def _ids(self, groups):
        return [sorted(c["id"] for c in g) for g in groups]

    def test_same_name_norm_groups(self):
        a = db.create_contact(self.conn, "Ada Lovelace")
        b = db.create_contact(self.conn, "ada  lovelace")  # different glyphs, same norm
        db.create_contact(self.conn, "Alan Turing")        # unrelated singleton
        self.conn.commit()
        groups = db.find_duplicate_groups(self.conn)
        self.assertEqual(self._ids(groups), [sorted([a, b])])

    def test_shared_identifier_groups(self):
        a = db.create_contact(self.conn, "Bob Smith")
        b = db.create_contact(self.conn, "Robert Smith")  # different names
        db.add_identifier(self.conn, a, "phone", "+46 70 174 0611")
        db.add_identifier(self.conn, b, "phone", "+46 70 174 0611")  # same value
        self.conn.commit()
        groups = db.find_duplicate_groups(self.conn)
        self.assertEqual(self._ids(groups), [sorted([a, b])])

    def test_shared_identifier_case_insensitive(self):
        a = db.create_contact(self.conn, "Grace Hopper")
        b = db.create_contact(self.conn, "G Hopper")
        db.add_identifier(self.conn, a, "email", "Grace@Navy.EXAMPLE")
        db.add_identifier(self.conn, b, "email", "grace@navy.example")
        self.conn.commit()
        groups = db.find_duplicate_groups(self.conn)
        self.assertEqual(self._ids(groups), [sorted([a, b])])

    def test_transitive_merge_of_signals(self):
        # A == B by name; B == C by phone -> all three in one group
        a = db.create_contact(self.conn, "Carol King")
        b = db.create_contact(self.conn, "carol king")
        c = db.create_contact(self.conn, "Caroline King")
        db.add_identifier(self.conn, b, "phone", "+46 70 174 0622")
        db.add_identifier(self.conn, c, "phone", "+46 70 174 0622")
        self.conn.commit()
        groups = db.find_duplicate_groups(self.conn)
        self.assertEqual(self._ids(groups), [sorted([a, b, c])])

    def test_unrelated_do_not_group(self):
        db.create_contact(self.conn, "Alice Alpha")
        db.create_contact(self.conn, "Bob Beta")
        self.conn.commit()
        self.assertEqual(db.find_duplicate_groups(self.conn), [])

    def test_excluded_names_not_grouped(self):
        a = db.create_contact(self.conn, "Same Name")
        b = db.create_contact(self.conn, "Same Name")
        db.set_excluded(self.conn, b, 1)
        self.conn.commit()
        # name signal is dropped for the excluded contact -> no group
        self.assertEqual(db.find_duplicate_groups(self.conn), [])

    def test_groups_ordered_largest_first(self):
        # group of 3 by name, group of 2 by name
        for _ in range(3):
            db.create_contact(self.conn, "Trio Person")
        for _ in range(2):
            db.create_contact(self.conn, "Pair Person")
        self.conn.commit()
        sizes = [len(g) for g in db.find_duplicate_groups(self.conn)]
        self.assertEqual(sizes, [3, 2])

    def test_handle_duplicates_returns_groups(self):
        a = db.create_contact(self.conn, "Dup A")
        b = db.create_contact(self.conn, "dup a")
        self.conn.commit()
        out = app.handle_duplicates(self.conn)
        self.assertEqual(len(out["groups"]), 1)
        member_ids = {m["id"] for m in out["groups"][0]}
        self.assertEqual(member_ids, {a, b})
        self.assertIn("name", out["groups"][0][0])
        self.assertIn("counts", out["groups"][0][0])

    def test_handle_merge_success(self):
        a = db.create_contact(self.conn, "Keeper")
        b = db.create_contact(self.conn, "Keeper")
        db.add_note(self.conn, b, "carried over")
        self.conn.commit()
        res = app.handle_merge(self.conn, {"keep_id": a, "dup_id": b})
        self.assertEqual(res, {"ok": True, "kept_id": a})
        self.assertEqual(len(db.find_exact(self.conn, "Keeper")), 1)
        self.assertEqual(len(db.get_notes(self.conn, a)), 1)

    def test_handle_merge_same_id_is_400(self):
        a = db.create_contact(self.conn, "Solo")
        self.conn.commit()
        with self.assertRaises(app.ApiError) as e:
            app.handle_merge(self.conn, {"keep_id": a, "dup_id": a})
        self.assertEqual(e.exception.status, 400)

    def test_handle_merge_missing_id_is_400(self):
        a = db.create_contact(self.conn, "Exists")
        self.conn.commit()
        with self.assertRaises(app.ApiError) as e:
            app.handle_merge(self.conn, {"keep_id": a, "dup_id": 999999})
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(app.ApiError) as e2:
            app.handle_merge(self.conn, {"keep_id": a})
        self.assertEqual(e2.exception.status, 400)


class TestSharedHistoryDedupe(Base):
    """The LinkedIn extractor emits both a general and a dated version of the same
    fact, which used to render as two near-identical bullets on the person page."""

    def dedupe(self, items):
        return app.api_serialize._dedupe_history(items)

    def test_dated_variant_supersedes_the_general_one(self):
        self.assertEqual(
            self.dedupe(["You both studied at Northfield College",
                         "You both studied at Northfield College from Jan 1, 2016 to Dec 1, 2019"]),
            ["You both studied at Northfield College from Jan 1, 2016 to Dec 1, 2019"])

    def test_distinct_facts_are_both_kept(self):
        # 'worked at' vs 'studied at' is not a prefix of the other: two real facts.
        pair = ["You both worked at Northfield College",
                "You both studied at Northfield College"]
        self.assertEqual(self.dedupe(pair), pair)

    def test_exact_repeat_collapses_to_first_appearance(self):
        self.assertEqual(self.dedupe(["A", "A"]), ["A"])

    def test_prefix_chain_leaves_only_the_most_specific(self):
        self.assertEqual(self.dedupe(["A", "A long", "A longer"]), ["A longer"])

    def test_order_is_preserved(self):
        self.assertEqual(self.dedupe(["b thing", "a thing"]), ["b thing", "a thing"])

    def test_junk_and_non_lists_never_raise(self):
        self.assertEqual(self.dedupe(["keep", None, 42, "", "   "]), ["keep"])
        self.assertEqual(self.dedupe("not a list"), [])
        self.assertEqual(self.dedupe(None), [])

    def test_detail_payload_is_deduped_end_to_end(self):
        import json
        cid = app.handle_create_contact(self.conn, {"name": "Kim"})["id"]
        self.conn.execute(
            "UPDATE contacts SET li_extra = ? WHERE id = ?",
            (json.dumps({"mutual_names": ["Alan"],
                         "shared_history": ["You both studied at Northfield College",
                                            "You both studied at Northfield College from Jan 2016"],
                         "recent_activity": "Posted about gardening"}), cid))
        detail = app.handle_get_contact(self.conn, cid)
        self.assertEqual(detail["mutuals"]["shared_history"],
                         ["You both studied at Northfield College from Jan 2016"])
        # the neighbouring fields must survive the change
        self.assertEqual(detail["mutuals"]["mutual_names"], ["Alan"])
        self.assertEqual(detail["mutuals"]["recent_activity"], "Posted about gardening")
