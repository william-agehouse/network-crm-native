"""Engine fixes for People, Lists, Dates, Notes and the server.

One class per fix, named for what a person would notice. Each test fails
without its fix: the issue number is in the class docstring.
"""

import datetime
import http.client
import json
import os
import sqlite3
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, db  # noqa: E402

import archive as arc  # noqa: E402
import api_abilities  # noqa: E402
import api_dates  # noqa: E402
import api_instance  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _status(fn, *args):
    """The HTTP status an ApiError carries, or 200 when the call succeeds."""
    try:
        fn(*args)
    except app.ApiError as exc:
        return int(exc.status)
    return 200


class _Served(Base):
    """A real CrmHandler on a free port, over this test's own database."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(app.db, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.CrmHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = None if body is None else json.dumps(body).encode()
            hdrs = {"Content-Type": "application/json"} if data is not None else {}
            hdrs.update(headers or {})
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()


# ── NC-334 ───────────────────────────────────────────────────────────────────
class TestTakingANameOffTheHiddenListBringsThemBack(_Served):
    """NC-334. Adding a name hid the person at once; removing it only edited
    the file, so they stayed hidden."""

    def setUp(self):
        super().setUp()
        self.file = os.path.join(self.dir, "exclude_list.txt")
        patcher = mock.patch.object(app.api_contacts, "EXCLUDE_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remove_after_add_unhides_them(self):
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        app.handle_exclude_list_add(self.conn, {"name": "Ada Lovelace"})
        self.assertEqual(db.get_contact(self.conn, ada)["excluded"], 1)
        status, out = self.request("POST", "/api/exclude-list/remove", {"name": "ada lovelace"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["unhidden"], 1)
        self.assertEqual(db.get_contact(self.conn, ada)["excluded"], 0)
        self.assertEqual(db.load_exclude_list(self.file), set())

    def test_someone_deleted_stays_deleted(self):
        grace = db.create_contact(self.conn, "Grace Hopper")
        self.conn.commit()
        app.handle_delete_contact(self.conn, grace, {})
        app.handle_exclude_list_remove({"name": "Grace Hopper"}, self.conn)
        row = db.get_contact(self.conn, grace)
        self.assertEqual((row["deleted"], row["excluded"]), (1, 1))


# ── NC-335 ───────────────────────────────────────────────────────────────────
class TestARefusedWriteDoesNotBreakTheNextRequest(_Served):
    """NC-335. A write refused before its body was read left the body on the
    kept-alive connection, and the next request on it failed."""

    def test_the_next_request_on_the_same_connection_answers(self):
        refusals = [
            ({"Content-Type": "text/plain"}, 415),
            ({"Content-Type": "application/json", "Origin": "http://evil.example"}, 403),
        ]
        for headers, expected in refusals:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("POST", "/api/contacts", body=b'{"name": "Alan Turing"}',
                             headers=headers)
                first = conn.getresponse()
                first.read()
                self.assertEqual(first.status, expected)
                sock = conn.sock
                self.assertIsNotNone(sock, "the connection was not kept open")
                conn.request("GET", "/api/status")
                second = conn.getresponse()
                body = second.read()
                self.assertEqual(second.status, 200, body[:200])
                self.assertIs(conn.sock, sock, "the second request needed a new connection")
            finally:
                conn.close()
        self.assertEqual(db.counts(self.conn)["total"], 0, "a refused write still wrote")

    def test_a_body_too_large_to_read_closes_the_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/contacts")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(app.MAX_BODY_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 400)
            self.assertEqual((resp.getheader("Connection") or "").lower(), "close")
        finally:
            conn.close()


# ── NC-336 and NC-337 ────────────────────────────────────────────────────────
class TestAddPutsARemovedPersonBack(Base):
    """NC-336. Add skipped a member whose row was marked removed and said 0."""

    def test_remove_then_add_leaves_them_on_the_list(self):
        ada = db.create_contact(self.conn, "Ada")
        grace = db.create_contact(self.conn, "Grace")
        lid = app.handle_list_create(self.conn, {"name": "Dinner",
                                                 "contact_ids": [ada, grace]})["id"]
        app.handle_list_member(self.conn, lid, {"contact_id": ada, "remove": True})
        self.assertEqual({m["id"] for m in app.handle_list_detail(self.conn, lid)["members"]},
                         {grace})
        out = app.handle_list_add(self.conn, lid, {"contact_ids": [ada, grace]})
        self.assertEqual(out["added"], 1, "the person brought back is the one added")
        members = app.handle_list_detail(self.conn, lid)["members"]
        self.assertEqual({m["id"] for m in members}, {ada, grace})
        self.assertEqual({m["list_status"] for m in members}, {""})


class TestAnUnknownPersonIsARefusalNotAServerError(Base):
    """NC-337. An id that does not exist was a 500 FOREIGN KEY failure."""

    def test_create_refuses_an_unknown_id_and_creates_nothing(self):
        ada = db.create_contact(self.conn, "Ada")
        self.conn.commit()
        with self.assertRaises(app.ApiError) as caught:
            app.handle_list_create(self.conn, {"name": "Dinner", "contact_ids": [ada, 9999]})
        self.assertEqual(int(caught.exception.status), 400)
        self.assertIn("9999", caught.exception.message)
        self.assertEqual(app.handle_lists_get(self.conn)["lists"], [])

    def test_add_refuses_an_unknown_id_and_a_word(self):
        ada = db.create_contact(self.conn, "Ada")
        lid = app.handle_list_create(self.conn, {"name": "Dinner"})["id"]
        for bad in ([9999], ["abc"], [True]):
            self.assertEqual(_status(app.handle_list_add, self.conn, lid,
                                     {"contact_ids": bad}), 400, bad)
        # A number sent as text still works, as it always did.
        self.assertEqual(app.handle_list_add(self.conn, lid,
                                             {"contact_ids": [str(ada)]})["added"], 1)


# ── NC-338 ───────────────────────────────────────────────────────────────────
class TestOnlyRealDatesAreStored(Base):
    """NC-338. "tomorrow" was stored as a birthday and "soon" as an event date."""

    def test_a_persons_date_must_be_a_day(self):
        ada = db.create_contact(self.conn, "Ada")
        self.conn.commit()
        for bad in ("tomorrow", "2027-13-01", "2027-02-30", "12/03/2027", "2027-03-12T09:00"):
            self.assertEqual(_status(app.handle_set_date, self.conn, ada,
                                     {"kind": "birthday", "on_date": bad}), 400, bad)
        self.assertEqual(app.handle_get_contact(self.conn, ada)["dates"], [])
        out = app.handle_set_date(self.conn, ada, {"kind": "birthday", "on_date": "1990-03-12"})
        self.assertEqual([d["on_date"] for d in out["dates"]], ["1990-03-12"])

    def test_29_february_with_no_year_is_a_day(self):
        ada = db.create_contact(self.conn, "Ada")
        self.conn.commit()
        self.assertEqual(_status(app.handle_set_date, self.conn, ada,
                                 {"kind": "birthday", "on_date": "1900-02-29"}), 400)
        out = app.handle_set_date(self.conn, ada, {"kind": "birthday", "on_date": "1900-02-29",
                                                   "year_known": False})
        self.assertEqual(len(out["dates"]), 1)

    def test_a_lists_event_date_must_be_a_day(self):
        lid = app.handle_list_create(self.conn, {"name": "Dinner"})["id"]
        self.assertEqual(_status(app.handle_list_meta, self.conn, lid,
                                 {"event_date": "soon"}), 400)
        app.handle_list_meta(self.conn, lid, {"event_date": "2027-03-12"})
        self.assertEqual(app.handle_list_detail(self.conn, lid)["event_date"], "2027-03-12")
        app.handle_list_meta(self.conn, lid, {"venue": "Paris"})       # date left alone
        self.assertEqual(app.handle_list_detail(self.conn, lid)["event_date"], "2027-03-12")
        app.handle_list_meta(self.conn, lid, {"event_date": ""})       # "" still clears
        self.assertEqual(app.handle_list_detail(self.conn, lid)["event_date"], "")


# ── NC-339 ───────────────────────────────────────────────────────────────────
class TestAMissingNoteIsNotFound(Base):
    """NC-339. Editing or deleting a missing note answered ok."""

    def test_edit_and_delete_of_a_missing_note_are_404(self):
        self.assertEqual(_status(app.handle_update_note, self.conn, 9999, {"body": "x"}), 404)
        self.assertEqual(_status(app.handle_delete_note, self.conn, 9999), 404)

    def test_a_deleted_note_is_missing_too(self):
        ada = db.create_contact(self.conn, "Ada")
        self.conn.commit()
        nid = app.handle_add_note(self.conn, ada, {"body": "met at the workshop"})["note"]["id"]
        self.assertEqual(app.handle_update_note(self.conn, nid, {"body": "met in Paris"}),
                         {"ok": True})
        self.assertEqual(app.handle_delete_note(self.conn, nid), {"ok": True})
        self.assertEqual(_status(app.handle_delete_note, self.conn, nid), 404)
        self.assertEqual(_status(app.handle_update_note, self.conn, nid, {"body": "x"}), 404)


# ── NC-340 ───────────────────────────────────────────────────────────────────
class TestHistoryAndEpisodesSayWhatWentWrong(_Served):
    """NC-340. A bad limit was a 500 on episodes; history turned every error
    into an empty page; an unknown person was an empty 200 on both."""

    def setUp(self):
        super().setUp()
        self.ada = db.create_contact(self.conn, "Ada")
        self.conn.commit()
        self.archive = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.archive)
        patcher = mock.patch.object(arc, "ARCHIVE_DB_PATH", self.archive)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_bad_limit_is_400_on_both(self):
        for route in ("history", "episodes"):
            for bad in ("abc", "0", "-3"):
                status, out = self.request("GET", f"/api/contact/{self.ada}/{route}?limit={bad}")
                self.assertEqual(status, 400, (route, bad, out))
            status, _ = self.request("GET", f"/api/contact/{self.ada}/{route}?limit=5")
            self.assertEqual(status, 200, route)

    def test_an_unknown_person_is_404_on_both(self):
        for route in ("history", "episodes"):
            status, out = self.request("GET", f"/api/contact/9999/{route}")
            self.assertEqual(status, 404, (route, out))

    def test_an_unreadable_archive_is_an_error_not_an_empty_page(self):
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(arc, "get_messages", side_effect=locked):
            for route in ("history", "episodes"):
                status, out = self.request("GET", f"/api/contact/{self.ada}/{route}")
                self.assertEqual(status, 503, (route, out))
                self.assertIn("could not be read", out["error"])

    def test_no_messages_yet_is_still_an_empty_answer(self):
        empty = os.path.join(self.dir, "archive-empty.db")
        sqlite3.connect(empty).close()
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", empty):
            self.assertEqual(app.handle_get_history(self.ada, {}, self.conn)["messages"], [])
            self.assertEqual(app.handle_get_episodes(self.ada, {})["episodes"], [])


# ── NC-341 ───────────────────────────────────────────────────────────────────
class TestSearchingPeople(Base):
    """NC-341. Punctuation or emoji alone matched everyone, accents were not
    folded, and % and _ were wildcards in the company and role half."""

    def setUp(self):
        super().setUp()
        self.zoe = db.create_contact(self.conn, "Zoë Lovelace")
        self.zoe_plain = db.create_contact(self.conn, "Zoe Hopper")
        self.ada = db.create_contact(self.conn, "Ada Byron")
        self.alan = db.create_contact(self.conn, "Alan Turing")
        db.set_field(self.conn, self.ada, "company", "1% Club")
        db.set_field(self.conn, self.alan, "company", "10 Club")
        db.set_field(self.conn, self.zoe_plain, "company", "A_B Studio")
        db.set_field(self.conn, self.zoe, "company", "AxB Studio")
        self.conn.commit()

    def search(self, q, **more):
        query = {"q": [q], **{k: [v] for k, v in more.items()}}
        return app.handle_list_contacts(self.conn, query)

    def test_nothing_to_look_for_is_no_search(self):
        for q in ("!!", "   ", "\U0001F600", "%"):
            out = self.search(q)
            self.assertEqual(out["total"], 4, repr(q))
            self.assertEqual(out["q"], "", f"{q!r} was reported as a search")

    def test_accents_are_folded_both_ways(self):
        self.assertEqual({c["id"] for c in self.search("zoe")["contacts"]},
                         {self.zoe, self.zoe_plain})
        self.assertEqual({c["id"] for c in self.search("Zoë")["contacts"]},
                         {self.zoe, self.zoe_plain})
        self.assertEqual(self.search("zoe")["q"], "zoe")

    def test_percent_and_underscore_are_characters_not_wildcards(self):
        self.assertEqual([c["id"] for c in self.search("1%")["contacts"]], [self.ada])
        self.assertEqual([c["id"] for c in self.search("a_b")["contacts"]], [self.zoe_plain])

    def test_the_total_counts_every_match_past_the_page(self):
        out = self.search("zoe", limit="1")
        self.assertEqual((len(out["contacts"]), out["total"]), (1, 2))


# ── NC-305 ───────────────────────────────────────────────────────────────────
class TestMostMessagesFirst(Base):
    """NC-305. A server-side sort by messages exchanged, across every channel
    and across the whole list, not just the loaded page."""

    def test_the_person_with_the_most_messages_comes_first(self):
        day = "2024-01-02 10:00:00"
        quiet = db.create_contact(self.conn, "Ada")          # no messages at all
        linkedin = db.create_contact(self.conn, "Grace")
        both = db.create_contact(self.conn, "Alan")
        db.set_channel(self.conn, linkedin, "linkedin", 8, day)
        db.set_channel(self.conn, both, "whatsapp", 5, day)
        db.set_channel(self.conn, both, "imessage", 5, day)
        self.conn.commit()
        out = app.handle_list_contacts(self.conn, {"sort": ["messages"]})
        self.assertEqual([c["id"] for c in out["contacts"]], [both, linkedin, quiet])
        first = app.handle_list_contacts(self.conn, {"sort": ["messages"], "limit": ["1"]})
        self.assertEqual(([c["id"] for c in first["contacts"]], first["total"]), ([both], 3))
        searched = app.handle_list_contacts(self.conn, {"sort": ["messages"], "q": ["a"]})
        self.assertEqual([c["id"] for c in searched["contacts"]], [both, linkedin, quiet])


# ── NC-342 ───────────────────────────────────────────────────────────────────
class TestReadsChangeNothing(_Served):
    """NC-342. GET /api/onboarding stored done=1, and a GET dismissed the
    database-recovery notice."""

    def test_the_recovery_notice_is_dismissed_by_post_only(self):
        db.state_set(self.conn, "db_recovery_notice", "moved aside")
        self.conn.commit()
        status, _ = self.request("GET", "/api/db-recovery-notice/dismiss")
        self.assertEqual(status, 405)
        self.assertEqual(db.state_get(self.conn, "db_recovery_notice", ""), "moved aside")
        status, out = self.request("POST", "/api/db-recovery-notice/dismiss", {})
        self.assertEqual((status, out), (200, {"ok": True}))
        self.assertEqual(db.state_get(self.conn, "db_recovery_notice", ""), "")

    def test_asking_whether_setup_is_done_writes_nothing(self):
        for i in range(app.ONBOARDED_GRANDFATHER_CONTACTS + 1):
            db.create_contact(self.conn, f"Person {i}")
        self.conn.commit()
        status, out = self.request("GET", "/api/onboarding")
        self.assertEqual((status, out), (200, {"done": True}))
        self.assertEqual(db.state_get(self.conn, app.ONBOARDED_STATE_KEY, ""), "")


# ── NC-343 ───────────────────────────────────────────────────────────────────
class TestFarDatesReadAsADay(unittest.TestCase):
    """NC-343. Every date past tomorrow read "on <weekday>"."""

    def when(self, days):
        day = datetime.date(2026, 1, 5) + datetime.timedelta(days=days)
        return api_dates._when_word(days, day.isoformat())

    def test_the_coming_week_keeps_its_weekday(self):
        self.assertEqual(self.when(0), "today")
        self.assertEqual(self.when(1), "tomorrow")
        self.assertEqual(self.when(3), "on Thursday")      # 8 January 2026
        self.assertEqual(self.when(6), "on Sunday")

    def test_further_off_reads_with_day_and_month(self):
        self.assertEqual(self.when(40), "on 14 February")
        self.assertEqual(self.when(7), "on 12 January")
        self.assertEqual(self.when(400), "on 9 February 2027")


# ── NC-227 ───────────────────────────────────────────────────────────────────
class TestTheStatusCheckCountsHiddenPeopleOnlyAfterAWrite(Base):
    """NC-227, second bullet. Every 15-second status check read the whole
    contacts table for the hidden count. It is now recounted only when the
    database was written since. The key stays (the native app uses it)."""

    def status(self):
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            counts = app.handle_status(self.conn)["counts"]
        finally:
            self.conn.set_trace_callback(None)
        scans = [s for s in seen if "excluded" in s and "FROM contacts" in s]
        return counts, scans

    def test_an_unchanged_database_is_not_read_again(self):
        for name in ("Ada", "Grace", "Alan"):
            db.create_contact(self.conn, name)
        db.mark_reviewed(self.conn, 1, "skipped")
        db.set_excluded(self.conn, 2, 1)
        db.set_deleted(self.conn, 3, 1)
        self.conn.commit()
        counts, scans = self.status()
        self.assertEqual(counts, {**db.counts(self.conn),
                                  "excluded": app._excluded_count(self.conn)})
        self.assertEqual(counts["excluded"], 2)
        self.assertEqual(len(scans), 1)
        again, scans = self.status()
        self.assertEqual((again, scans), (counts, []), "nothing changed, and it read again")
        # A write is seen at the next check.
        db.set_excluded(self.conn, 1, 1)
        self.conn.commit()
        after, scans = self.status()
        self.assertEqual((after["excluded"], len(scans)), (3, 1))


# ── NC-237 ───────────────────────────────────────────────────────────────────
class TestTheOldAddressesAreGone(_Served):
    """NC-237. Addresses only the old Data and Settings pages asked."""

    def test_removed_addresses_answer_404_and_update_still_answers(self):
        for path in ("/api/sources", "/api/setup/progress", "/api/dashboard"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)
        # Kept on purpose (NC-118). The route calls the name app re-exports, so
        # that is the one patched, which also keeps this test off the network.
        with mock.patch.object(app, "handle_update_get", lambda conn, query: {"checked": False}):
            status, out = self.request("GET", "/api/update")
        self.assertEqual((status, out), (200, {"checked": False}))


# ── NC-350 ───────────────────────────────────────────────────────────────────
class TestAFailedSetupReadCountsAsDone(Base):
    """NC-350. A failed read of the setup flag made Settings offer setup again."""

    def test_install_says_done_when_the_flag_cannot_be_read(self):
        def boom(conn):
            raise sqlite3.OperationalError("database is locked")
        with mock.patch.object(api_instance, "onboarding_done", boom):
            self.assertTrue(api_abilities._install(self.conn)["setup_again"]["done"])


if __name__ == "__main__":
    unittest.main()
