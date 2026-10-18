"""The composed view surfaces: Today (queue, attention, streak, week) plus episodes, chat wiring and the Home feed."""

import datetime
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, arc, db, feed,
)


class TestTodayEpisodesChat(Base):
    """/api/describe-queue, /api/contact/{id}/episodes (gap/channel grouping),
    story text, and chat-history contact-card persistence.

    Was mostly about /api/today, which is gone (NC-009). Where the deleted
    endpoint was only a way to REACH something that still exists — the note
    streak, the skip counter, the describe ranking, the week lookahead — the test
    now calls that thing directly, which is where it should always have been
    aimed. Where it tested the composition itself (the attention queue's ranking
    and dedup, the payload's key set) it is gone with the composition, and the
    class name is left as-is because renaming it would lose the history.
    Deletions, individually noted below."""

    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="crm_arc_")
        self._orig_arc = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(self.arc_dir, "archive.db")
        arc.init_archive(arc.ARCHIVE_DB_PATH)

    def tearDown(self):
        arc.ARCHIVE_DB_PATH = self._orig_arc
        super().tearDown()

    def _msg(self, cid, ts, body, direction=0, channel="whatsapp", key=None,
             group="", group_key=""):
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        arc.insert_message(ac, contact_id=cid, channel=channel, direction=direction,
                           ts=ts, body=body, source_key=key or (ts + body),
                           group_name=group, group_key=group_key)
        ac.commit()
        ac.close()

    def test_describe_queue_ranked_and_context(self):
        """The onboarding wizard's third step. Same ranking the Today deck used —
        strongest first, because they are both the ones worth remembering and the
        easiest to say something about — reached now by an endpoint that returns
        only this, rather than a whole dashboard the wizard used three keys of."""
        strong = db.create_contact(self.conn, "Strong Sam")
        db.set_channel(self.conn, strong, "whatsapp", 400, "2026-07-01 10:00:00")
        weak = db.create_contact(self.conn, "Weak Wanda")
        self.conn.commit()
        self._msg(strong, "2026-07-01 10:00:00", "see you at the lab!")
        d = app.handle_describe_queue(self.conn)
        self.assertEqual(d["queue"][0]["name"], "Strong Sam")
        self.assertEqual(d["queue"][0]["last_messages"][-1]["body"], "see you at the lab!")
        self.assertIn("Weak Wanda", [q["name"] for q in d["queue"]])
        # Exactly what the wizard renders, and no more: it used to ask for
        # everything and slice(0, 3) it.
        self.assertEqual(set(d), {"queue"})
        self.assertLessEqual(len(d["queue"]), app.DESCRIBE_QUEUE_SIZE)

    def test_today_streak_counts_note_days(self):
        cid = db.create_contact(self.conn, "Notey")
        self.conn.execute("INSERT INTO notes (contact_id, body, created_at) VALUES (?, 'a', datetime('now'))", (cid,))
        self.conn.execute("INSERT INTO notes (contact_id, body, created_at) VALUES (?, 'b', datetime('now', '-1 day'))", (cid,))
        self.conn.execute("INSERT INTO notes (contact_id, body, created_at) VALUES (?, 'c', datetime('now', '-3 day'))", (cid,))
        self.conn.commit()
        # Was reached through /api/today's payload. The streak and the happenings
        # list both still compute correctly; nothing displays either one now.
        streak = app.api_notes._note_streak(self.conn)
        self.assertEqual(streak["days"], 2)   # today + yesterday; the -3d note broke the run
        self.assertEqual(streak["today"], 1)
        self.assertEqual(app.api_notes.recent_happenings(self.conn, limit=4)[0]["name"],
                         "Notey")

    def _ago(self, days):
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # DELETED with the Today surface (NC-009):
    # test_attention_ranks_owed_over_cold_over_describe_and_dedups — it tested
    # _attention's four-way ranking and dedup, which was the composition itself,
    # not a read underneath it. Nothing is left to rank. The reads it ranked are
    # still covered: owed_replies by the test below, find_reconnect by
    # test_db_strength, upcoming_dates by test_dates_crud_and_week.

    def test_attention_reply_owed_window_is_180_not_14(self):
        import feed
        cid = db.create_contact(self.conn, "Quiet Quinn")
        self.conn.commit()
        self._msg(cid, self._ago(90), "did you see my note?", direction=0)
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        wide = [r[2]["id"] for r in feed.owed_reply_candidates(self.conn, ac, days=180, cap=20)]
        narrow = [r[2]["id"] for r in feed.owed_reply_candidates(self.conn, ac, days=14, cap=20)]
        ac.close()
        self.assertIn(cid, wide)        # Today's 180-day queue keeps it
        self.assertNotIn(cid, narrow)   # the feed's 14-day glance has dropped it

    def test_owed_replies_survives_a_missing_archive(self):
        """The archive is a separate database that can be absent or locked, and a
        read that needs it must degrade rather than 500. Aimed at owed_replies
        now: the Today payload was only how this used to be provoked."""
        arc.ARCHIVE_DB_PATH = os.path.join(self.arc_dir, "does-not-exist.db")
        db.create_contact(self.conn, "Somebody")
        self.conn.commit()
        self.assertEqual(list(app.api_messages.owed_replies(self.conn, cap=8)), [])

    def test_skip_does_not_advance_streak_and_is_counted(self):
        cid = db.create_contact(self.conn, "Skipme")
        self.conn.commit()
        before = app.api_notes._note_streak(self.conn)
        app.handle_skip(self.conn, cid)      # skip writes no note
        after = app.api_notes._note_streak(self.conn)
        self.assertEqual(after["days"], before["days"])
        self.assertEqual(after["today"], 0)
        self.assertEqual(app.api_contacts.skipped_today_count(self.conn), 1)

    def test_next_occurrence_recurrence_and_leap_year(self):
        n = db.next_occurrence
        self.assertEqual(n("1990-07-25", 1, "2026-07-25"), "2026-07-25")  # today counts
        self.assertEqual(n("1990-07-01", 1, "2026-07-25"), "2027-07-01")  # rolls over
        self.assertEqual(n("1992-02-29", 1, "2027-01-01"), "2027-02-28")  # common year
        self.assertEqual(n("1992-02-29", 1, "2028-01-01"), "2028-02-29")  # leap year
        self.assertEqual(n("2026-10-15", 0, "2026-07-25"), "2026-10-15")  # one-off ahead
        self.assertEqual(n("2026-01-01", 0, "2026-07-25"), "")            # one-off past
        self.assertEqual(n("nonsense", 1, "2026-07-25"), "")              # garbage in

    def test_dates_crud_and_week(self):
        """Was test_dates_crud_attention_and_week. The attention half went with
        the Today surface (NC-009) — there is nothing left that ranks an urgent
        birthday above a reply owed. Dates themselves are untouched: they are
        still stored, still recur, still resolve on a person's page, and
        handle_week still aggregates them. Nothing DISPLAYS that aggregate any
        more, which is the one real loss in removing Today."""
        import datetime as _dt
        cid = db.create_contact(self.conn, "Birthday Bo")
        self.conn.commit()
        today = _dt.date.today()
        soon = today + _dt.timedelta(days=1)
        app.handle_set_date(self.conn, cid, {
            "kind": "birthday", "on_date": f"1990-{soon:%m-%d}"})
        self.assertTrue(any(w["kind"] == "birthday"
                            for w in app.handle_week(self.conn, days=None)))
        cid2 = db.create_contact(self.conn, "Later Lars")
        self.conn.commit()
        far = today + _dt.timedelta(days=10)
        app.handle_set_date(self.conn, cid2, {"kind": "meeting", "on_date": f"{far}",
                                             "label": "coffee"})
        wk = app.handle_week(self.conn, days=14)
        self.assertTrue(any(w.get("contact_id") == cid2 for w in wk))
        # it shows on the person page with the recurrence resolved server-side
        det = app.handle_get_contact(self.conn, cid)
        self.assertEqual(len(det["dates"]), 1)
        self.assertTrue(det["dates"][0]["recurring"])      # birthdays default annual
        # bad input is rejected, and remove works
        with self.assertRaises(app.ApiError):
            app.handle_set_date(self.conn, cid, {"kind": "nope", "on_date": "2026-01-01"})
        app.handle_set_date(self.conn, cid, {"remove": True, "date_id": det["dates"][0]["id"]})
        self.assertEqual(app.handle_get_contact(self.conn, cid)["dates"], [])

    def test_past_oneoff_date_reads_as_the_day_not_past(self):
        # A one-off meeting already behind us used to render the bare word
        # "past" (→ "🤝 past" chip). It must now show the actual day + label.
        cid = db.create_contact(self.conn, "Old Friend")
        self.conn.commit()
        app.handle_set_date(self.conn, cid, {
            "kind": "meeting", "on_date": "2024-03-07", "label": "coffee"})
        dates = app.handle_get_contact(self.conn, cid)["dates"]
        self.assertEqual(len(dates), 1)
        reason = dates[0]["reason"]
        self.assertNotEqual(reason, "past")
        self.assertIn("Met", reason)
        self.assertIn("2024", reason)
        self.assertIn("coffee", reason)
        # a custom past date falls back to its label, still never "past"
        app.handle_set_date(self.conn, cid, {
            "kind": "custom", "on_date": "2023-11-02", "label": "signed the deal"})
        reasons = [d["reason"] for d in app.handle_get_contact(self.conn, cid)["dates"]]
        self.assertNotIn("past", reasons)
        self.assertTrue(any("signed the deal" in r for r in reasons))

    # DELETED with the Today surface (NC-009): test_today_backcompat_keys_present
    # asserted /api/today returned seven named keys. There is no payload to keep
    # backwards-compatible — the endpoint is gone and its one surviving consumer
    # has its own, whose key set test_describe_queue_ranked_and_context pins.

    def test_episodes_group_by_gap_and_channel(self):
        cid = db.create_contact(self.conn, "Chatty")
        self.conn.commit()
        # one episode (5 min apart), a >4h gap, then a different channel
        self._msg(cid, "2026-06-01 10:00:00", "hello")
        self._msg(cid, "2026-06-01 10:05:00", "on my way", direction=1)
        self._msg(cid, "2026-06-01 18:00:00", "made it home")
        self._msg(cid, "2026-06-01 18:10:00", "emailed you the deck", channel="email")
        d = app.handle_get_episodes(cid, {})
        self.assertEqual(len(d["episodes"]), 3)
        newest = d["episodes"][0]
        self.assertEqual(newest["channel"], "email")
        two_msg = d["episodes"][2]
        self.assertEqual(two_msg["count"], 2)
        self.assertEqual(two_msg["sent"], 1)
        self.assertEqual(len(two_msg["messages"]), 2)  # small → inline
        self.assertEqual(two_msg["messages"][0]["body"], "hello")

    def test_a_group_episode_can_be_widened_to_the_whole_conversation(self):
        """NC-106, through the endpoint. threads=1 brings the other voices and
        names each of them; without it the page shows only her own lines."""
        ada = db.create_contact(self.conn, "Ada Lovelace")
        bo = db.create_contact(self.conn, "Bo Lind")
        self.conn.commit()
        for cid, ts, body in ((ada, "2026-06-01 10:00:00", "dinner sunday?"),
                              (bo, "2026-06-01 10:01:00", "I am in"),
                              (arc.NO_PERSON, "2026-06-01 10:02:00", "me too")):
            self._msg(cid, ts, body, group="Weekend plans", key=f"g{cid}",
                      group_key="imessage:1",
                      direction=1 if cid == arc.NO_PERSON else 0)

        narrow = app.handle_get_history(ada, {}, self.conn)
        self.assertEqual([m["body"] for m in narrow["messages"]],
                         ["dinner sunday?"])
        self.assertFalse(narrow["threads"])

        wide = app.handle_get_history(ada, {"threads": ["1"]}, self.conn)
        self.assertTrue(wide["threads"])
        got = {m["body"]: m["who"] for m in wide["messages"]}
        self.assertEqual(set(got), {"dinner sunday?", "I am in", "me too"})
        self.assertEqual(got["I am in"], "Bo Lind",
                         "another person's line arrived with no name on it")
        self.assertEqual(got["dinner sunday?"], "Ada Lovelace")
        self.assertEqual(got["me too"], "",
                         "the owner's own line does not need a name — direction says it")

    def test_the_episode_list_is_not_widened(self):
        """The design decision, gated. Widening the LIST would let one busy
        group chat fill the 600 messages the server scans and push a person's
        one-to-one history off their own page. The list stays their history;
        opening a group episode is what shows the conversation."""
        ada = db.create_contact(self.conn, "Ada Lovelace")
        bo = db.create_contact(self.conn, "Bo Lind")
        self.conn.commit()
        self._msg(ada, "2026-06-01 10:00:00", "dinner sunday?", group="Weekend plans",
                  key="ga", group_key="imessage:1")
        for i in range(5):
            self._msg(bo, f"2026-06-01 10:0{i + 1}:00", f"bo talking {i}",
                      group="Weekend plans", key=f"gb{i}", group_key="imessage:1")
        # Asked for as loudly as a caller can, and still not widened.
        d = app.handle_get_episodes(ada, {"threads": ["1"]})
        self.assertEqual(len(d["episodes"]), 1)
        self.assertEqual(d["episodes"][0]["count"], 1,
                         "the episode list counted other people's messages as "
                         "hers, which buries her own history")

    def test_two_conversations_sharing_a_label_are_two_episodes(self):
        """NC-110's third reader. Episodes broke on the group LABEL, and an
        unnamed chat is labelled from its head count — so every unnamed
        three-person chat merged into one episode."""
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        self._msg(ada, "2026-06-01 10:00:00", "in one chat", group="Group of 3",
                  key="e1", group_key="imessage:1")
        self._msg(ada, "2026-06-01 10:01:00", "in another", group="Group of 3",
                  key="e2", group_key="imessage:2")
        d = app.handle_get_episodes(ada, {})
        self.assertEqual(len(d["episodes"]), 2,
                         "two separate conversations became one episode "
                         "because they share a generated label")

    def test_the_owners_own_line_is_never_given_a_name(self):
        """A message the owner sent to a group is stored under arc.NO_PERSON
        (NC-111), and `direction = 1` already says it was them. Naming it would
        put somebody else's name on the owner's own words.

        A contact row is forced into id 0 here on purpose. Without one,
        db.get_contact returns None for it and the guard in `_speakers` is
        unreachable, so nothing would ever exercise it.
        """
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.execute(
            "INSERT INTO contacts (id, name, name_norm) VALUES (?, ?, ?)",
            (arc.NO_PERSON, "Row Zero", "row zero"))
        self.conn.commit()
        self._msg(ada, "2026-06-01 10:00:00", "hers", group="Weekend plans",
                  key="z1", group_key="imessage:1")
        self._msg(arc.NO_PERSON, "2026-06-01 10:01:00", "mine", group="Weekend plans",
                  key="z2", group_key="imessage:1", direction=1)
        d = app.handle_get_history(ada, {"threads": ["1"]}, self.conn)
        got = {m["body"]: m["who"] for m in d["messages"]}
        self.assertEqual(got["mine"], "",
                         "the owner's own group message was attributed to a contact")
        self.assertEqual(got["hers"], "Ada Lovelace")

    def test_a_deleted_speaker_keeps_their_line_and_loses_their_name(self):
        """The conversation happened. Dropping the line would edit history to
        tidy a missing name.

        Corrected (NC-200): this used to assert the label was "", and "" is not
        "no name" on screen. The page shows `m.who || <this person's name>`, so
        an empty label put the line in Ada's mouth. It gets a label that says
        what happened instead."""
        ada = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        self._msg(ada, "2026-06-01 10:00:00", "hers", group="Weekend plans",
                  key="d1", group_key="imessage:1")
        self._msg(4242, "2026-06-01 10:01:00", "from a contact that is gone",
                  group="Weekend plans", key="d2", group_key="imessage:1")
        d = app.handle_get_history(ada, {"threads": ["1"]}, self.conn)
        got = {m["body"]: m["who"] for m in d["messages"]}
        self.assertIn("from a contact that is gone", got)
        self.assertEqual(got["from a contact that is gone"], app.api_messages.SPEAKER_GONE)

    def _book_club(self):
        """Ada's page; Bo hidden, Cy soft-deleted, Di plain — all in one chat."""
        ada = db.create_contact(self.conn, "Ada Lovelace")
        bo = db.create_contact(self.conn, "Bo Hidden")
        cy = db.create_contact(self.conn, "Cy Deleted")
        di = db.create_contact(self.conn, "Di Plain")
        db.set_excluded(self.conn, bo, 1)
        db.set_deleted(self.conn, cy, 1)
        self.conn.commit()
        for cid, n, body in ((ada, 0, "who brings food"), (bo, 1, "bo says cake"),
                             (cy, 2, "cy says bread"), (di, 3, "di says fruit")):
            self._msg(cid, f"2026-06-01 10:0{n}:00", body, group="Book club",
                      key=f"bc{n}", group_key="imessage:9")
        return ada, bo

    def test_someone_you_hid_keeps_their_line_and_not_their_name(self):
        """NC-200. Hide promises "Hidden from every view, search and AI
        answer". Opening a group conversation on someone else's page named them
        anyway. A hidden person's line stays in the thread, but their name is
        removed."""
        ada, _bo = self._book_club()
        d = app.handle_get_history(ada, {"threads": ["1"]}, self.conn)
        got = {m["body"]: m["who"] for m in d["messages"]}
        self.assertEqual(got["bo says cake"], app.api_messages.SPEAKER_HIDDEN)
        self.assertEqual(got["cy says bread"], app.api_messages.SPEAKER_DELETED)
        self.assertEqual(got["di says fruit"], "Di Plain")
        self.assertEqual(got["who brings food"], "Ada Lovelace")
        blob = repr(d)
        for name in ("Bo Hidden", "Cy Deleted"):
            self.assertNotIn(name, blob, f"{name} is still named in the thread")

    def test_on_their_own_page_a_hidden_person_is_still_named(self):
        """The owner opened their page on purpose, so the thread there says who spoke."""
        _ada, bo = self._book_club()
        d = app.handle_get_history(bo, {"threads": ["1"]}, self.conn)
        got = {m["body"]: m["who"] for m in d["messages"]}
        self.assertEqual(got["bo says cake"], "Bo Hidden")

    def test_no_one_elses_line_arrives_without_a_label(self):
        """The page fills an empty label with the page person's name, so an
        empty label on anyone else's line is a line attributed to the wrong
        person. The owner's own lines are the one exception, and direction says so."""
        ada, _bo = self._book_club()
        self._msg(4242, "2026-06-01 10:09:00", "from a contact that is gone",
                  group="Book club", key="bc9", group_key="imessage:9")
        d = app.handle_get_history(ada, {"threads": ["1"]}, self.conn)
        blank = [m["body"] for m in d["messages"] if not m["who"] and not m["direction"]]
        self.assertEqual(blank, [])

    def test_story_text_template(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.set_field(self.conn, cid, "how_we_met", "We met at the Acme Summit")
        db.set_channel(self.conn, cid, "whatsapp", 120, "2026-06-30 08:00:00")
        self.conn.commit()
        story = app._story_text(self.conn, db.get_contact(self.conn, cid))
        self.assertIn("We met at the Acme Summit.", story)
        self.assertIn("120 WhatsApp messages", story)
        # a saved brief wins over the template
        self.conn.execute("UPDATE contacts SET brief='You met her at the Acme Summit 2024.' WHERE id=?", (cid,))
        story2 = app._story_text(self.conn, db.get_contact(self.conn, cid))
        self.assertEqual(story2, "You met her at the Acme Summit 2024.")

    def test_the_story_does_not_lower_case_the_place_it_just_read(self):
        """NC-046. `"based in New York".capitalize()` is
        `"Based in new york"` — str.capitalize() lower-cases everything after
        the first character. That sentence was rendered on every contact with a
        metro: the app destroying a proper noun the importer had got right.

        Example of the output before the fix: "... Based in new york".
        """
        import api_dates
        cid = db.create_contact(self.conn, "Grace Hopper")
        db.set_field(self.conn, cid, "home_metro", "New York")
        self.conn.execute('UPDATE contacts SET topics=? WHERE id=?',
                          ('["AI", "US Navy"]', cid))
        self.conn.commit()
        story = app._story_text(self.conn, db.get_contact(self.conn, cid))
        self.assertIn("Based in New York", story)
        self.assertNotIn("new york", story)
        # The topics keep whatever case they were stored in, for the same reason.
        self.assertIn("into AI, US Navy", story)
        # And the helper itself, because two callers share it.
        self.assertEqual(api_dates.sentence_case("on Monday"), "On Monday")
        self.assertEqual(api_dates.sentence_case(""), "")
        self.assertEqual(api_dates.sentence_case("iMessage"), "IMessage")

    def test_the_upcoming_event_reason_keeps_the_weekday_capitalised(self):
        """The same bug, the other caller: `_when_word` returns "on Monday" and
        the list-event reason ran .capitalize() over it, shipping "On monday"."""
        import api_dates
        import datetime
        soon = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        lid = db.create_list(self.conn, "Paris workshop")
        self.conn.execute("UPDATE lists SET event_date = ?, venue = 'Paris' "
                          "WHERE id = ?", (soon, lid))
        self.conn.commit()
        reasons = [i["reason"] for i in api_dates.handle_week(self.conn, days=14)
                   if i["kind"] == "event"]
        self.assertTrue(reasons, "the event did not turn up in the week view")
        for r in reasons:
            self.assertNotRegex(r, r"^On [a-z]",
                                f"weekday lower-cased by capitalize(): {r!r}")

    def test_chat_history_cards_roundtrip(self):
        db.add_chat_message(self.conn, "user", "who do I know?")
        db.add_chat_message(self.conn, "assistant", "These people:",
                            contacts_json=json.dumps([{"id": 1, "name": "Ada"}]))
        self.conn.commit()
        out = app.handle_chat_history_get(self.conn)["messages"]
        self.assertEqual(out[-1]["contacts"], [{"id": 1, "name": "Ada"}])
        self.assertEqual(out[0]["contacts"], [])

    def test_reconnect_filters_weak_signal_and_dismissed(self):
        old900 = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=900)).strftime("%Y-%m-%d")
        old200 = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=200)).strftime("%Y-%m-%d")

        # one old LinkedIn message and nothing else curated — shouldn't read as "worth a message"
        weak = db.create_contact(self.conn, "Weak Signal")
        db.set_channel(self.conn, weak, "linkedin", 1, old900)

        # a real relationship: described, a note, a real (if old) conversation
        strong = db.create_contact(self.conn, "Strong Signal")
        db.mark_reviewed(self.conn, strong, "described")
        db.add_note(self.conn, strong, "good friend from university")
        db.set_channel(self.conn, strong, "whatsapp", 200, old200)
        self.conn.commit()

        names = {r["name"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertNotIn("Weak Signal", names)
        self.assertIn("Strong Signal", names)

        db.dismiss_reconnect(self.conn, strong)
        self.conn.commit()
        names_after = {r["name"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertNotIn("Strong Signal", names_after)

    def test_reconnect_skip_endpoint_and_draft_needs_key(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        db.mark_reviewed(self.conn, cid, "described")
        self.conn.commit()

        app.handle_reconnect_skip(self.conn, cid)
        self.assertTrue(db.get_contact(self.conn, cid)["reconnect_dismissed_at"])

        had_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(app.ApiError) as ctx:
                app.handle_draft_message(self.conn, cid)
            self.assertEqual(ctx.exception.status, 400)
        finally:
            if had_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = had_key


class TestLookaheadSettingGoverns(Base):
    """NC_DATES_LOOKAHEAD_DAYS is in settings.MANAGED_KEYS and was dead for its
    whole life: it is only the default for handle_week(days=None), and the sole
    caller — build_dashboard — hardcoded days=7. So the field rendered in Settings,
    accepted a value, and governed nothing anywhere. Worse than the read-at-import
    bugs, which at least worked after a restart."""

    def _person_with_date(self, name, days_out):
        import datetime as _dt
        cid = db.create_contact(self.conn, name)
        when = (_dt.date.today() + _dt.timedelta(days=days_out)).isoformat()
        app.handle_set_date(self.conn, cid, {"kind": "meeting", "on_date": when,
                                            "label": "Coffee"})
        self.conn.commit()
        return cid

    def test_the_week_span_follows_the_setting(self):
        """Aimed at handle_week(days=None) directly. It used to go through
        /api/today, which was the only caller that passed days=None — and that is
        now the reason the two Settings fields were REMOVED rather than kept: with
        Today deleted nothing calls it, so a field configuring it would render,
        save, and govern nothing, which is the exact defect this class was written
        about. The default is still honoured, so the fields come back with a
        surface if one is ever built."""
        near = self._person_with_date("Near Person", 3)
        far = self._person_with_date("Far Person", 20)
        with mock.patch.dict(os.environ, {"NC_DATES_LOOKAHEAD_DAYS": "7"}):
            ids = [w.get("contact_id") for w in app.handle_week(self.conn, days=None)]
        self.assertIn(near, ids)
        self.assertNotIn(far, ids, "20 days out is outside a 7-day lookahead")
        # Same data, one setting changed, no restart — the far date appears.
        with mock.patch.dict(os.environ, {"NC_DATES_LOOKAHEAD_DAYS": "30"}):
            ids = [w.get("contact_id") for w in app.handle_week(self.conn, days=None)]
        self.assertIn(near, ids)
        self.assertIn(far, ids)

    def test_the_row_limit_is_passed_and_not_left_to_the_default(self):
        """db.upcoming_dates defaults to limit=50 and handle_week never passed one,
        so a long lookahead stopped at 50 rows in silence. Assert the limit reaching
        the query is the one api_dates chose, not the library default."""
        import api_dates
        seen = {}
        real = db.upcoming_dates

        def spy(conn, **kw):
            seen.update(kw)
            return real(conn, **kw)

        with mock.patch.object(api_dates.db, "upcoming_dates", spy):
            api_dates.handle_week(self.conn, days=90)
        self.assertEqual(seen.get("limit"), api_dates._week_row_limit())
        self.assertNotEqual(seen.get("limit"), 50, "50 is the default this bug rode on")


class TestFeedApi(Base):
    """GET /api/feed: conversation signals derived from a
    seeded temp archive — unanswered, fact, group, note, import — bucketed
    Today / Yesterday / This week. Excluded contacts never surface."""

    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="crm_feed_")
        self._orig_arc = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(self.arc_dir, "archive.db")
        arc.init_archive(arc.ARCHIVE_DB_PATH)

    def tearDown(self):
        arc.ARCHIVE_DB_PATH = self._orig_arc
        super().tearDown()

    def _ago(self, days, hours=0):
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days, hours=hours)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _msg(self, cid, ts, body, direction=0, channel="whatsapp", group="",
             key=None, group_key=""):
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        arc.insert_message(ac, contact_id=cid, channel=channel, direction=direction,
                           ts=ts, body=body, source_key=key or (ts + body),
                           group_name=group, group_key=group_key)
        ac.commit()
        ac.close()

    @staticmethod
    def _rows(out, kind):
        return [r for b in out["buckets"] for r in b["rows"] if r["kind"] == kind]

    def test_unanswered_signal(self):
        ada = db.create_contact(self.conn, "Ada Lovelace")
        bob = db.create_contact(self.conn, "Bob Replied")
        eve = db.create_contact(self.conn, "Eve Excluded")
        db.set_excluded(self.conn, eve, 1)
        old = db.create_contact(self.conn, "Old Olof")
        self.conn.commit()
        # Ada wrote last (inbound, 2 days ago) and you never answered
        self._msg(ada, self._ago(3), "hi!", direction=1, channel="linkedin")
        self._msg(ada, self._ago(2), "are we still on for Paris?", channel="linkedin")
        # Bob wrote, but you answered afterwards
        self._msg(bob, self._ago(2), "ping")
        self._msg(bob, self._ago(1), "pong", direction=1)
        # Eve is excluded — her waiting message must never surface
        self._msg(eve, self._ago(1), "hello?")
        # Old Olof's message is outside the 14-day window
        self._msg(old, self._ago(30), "long ago")

        rows = self._rows(feed.handle_feed(self.conn), "unanswered")
        self.assertEqual([r["name"] for r in rows], ["Ada Lovelace"])
        r = rows[0]
        self.assertEqual(r["contact_id"], ada)
        self.assertEqual(r["channel"], "linkedin")
        self.assertIn("replied on LinkedIn", r["text"])
        self.assertIn("unanswered 2d", r["text"])
        self.assertEqual(r["meta"], "are we still on for Paris?")
        self.assertIsNone(r["group"])

    def test_group_signal_counts_and_skips_excluded(self):
        ada = db.create_contact(self.conn, "Ada")
        eve = db.create_contact(self.conn, "Eve")
        db.set_excluded(self.conn, eve, 1)
        self.conn.commit()
        self._msg(ada, self._ago(1), "who's in?", group="Weekend plans", key="g1")
        self._msg(ada, self._ago(0, hours=2), "dinner sunday", group="Weekend plans", key="g2")
        self._msg(eve, self._ago(1), "me!", group="Weekend plans", key="g3")   # excluded → not counted
        self._msg(ada, self._ago(5), "stale", group="Running club", key="g4")  # outside 3-day window

        rows = self._rows(feed.handle_feed(self.conn), "group")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["group"], "Weekend plans")
        self.assertIn("Weekend plans was active — 2 messages", rows[0]["text"])
        self.assertIsNone(rows[0]["contact_id"])

    def test_two_conversations_sharing_a_label_are_counted_apart(self):
        """NC-110. The tally was keyed on the group's LABEL, and an unnamed chat
        has no label of its own — the importers make one from the head count. So
        two unrelated three-person chats were reported as one conversation with
        their messages added together. Keyed on the conversation, they are two
        rows, each with its own count."""
        ada = db.create_contact(self.conn, "Ada")
        self.conn.commit()
        self._msg(ada, self._ago(1), "one", group="Group of 3",
                  key="a1", group_key="imessage:1")
        self._msg(ada, self._ago(1), "two", group="Group of 3",
                  key="a2", group_key="imessage:1")
        self._msg(ada, self._ago(0, hours=1), "elsewhere", group="Group of 3",
                  key="a3", group_key="imessage:2")

        rows = self._rows(feed.handle_feed(self.conn), "group")
        self.assertEqual(len(rows), 2, "two chats were tallied as one")
        counts = sorted(int(r["text"].split("— ")[1].split(" ")[0]) for r in rows)
        self.assertEqual(counts, [1, 2])
        # Both still SHOW the label, because that is all a human has to go on.
        self.assertEqual({r["group"] for r in rows}, {"Group of 3"})

    def test_rows_with_no_key_yet_fall_back_to_the_label(self):
        """Everything imported before the column exists reads group_key=''.
        Those must still tally — as they did before — rather than collapsing
        into one nameless row."""
        ada = db.create_contact(self.conn, "Ada")
        self.conn.commit()
        self._msg(ada, self._ago(1), "one", group="Weekend plans", key="b1")
        self._msg(ada, self._ago(1), "two", group="Running club", key="b2")
        rows = self._rows(feed.handle_feed(self.conn), "group")
        self.assertEqual({r["group"] for r in rows}, {"Weekend plans", "Running club"})

    def test_note_fact_and_import_rows(self):
        cid = db.create_contact(self.conn, "Anna Svensson")
        db.add_note(self.conn, cid, "Met at the garden fair.")
        # a fresh scraped fact with a city surfaces; stale or city-less ones don't
        paris = db.create_contact(self.conn, "Paris Pierre")
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE contacts SET facts_source='linkedin_scrape', home_city='Paris', "
            "facts_updated_at=? WHERE id=?", (now_iso, paris))
        stale = db.create_contact(self.conn, "Stale Sten")
        self.conn.execute(
            "UPDATE contacts SET facts_source='linkedin_scrape', home_city='Oslo', "
            "facts_updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (stale,))
        nocity = db.create_contact(self.conn, "No City Nils")
        self.conn.execute(
            "UPDATE contacts SET facts_source='linkedin_scrape', "
            "facts_updated_at=? WHERE id=?", (now_iso, nocity))
        db.state_set(self.conn, "import_synced_whatsapp", now_iso)
        db.state_set(self.conn, "import_synced_telegram", "2020-01-01T00:00:00+00:00")
        # An enrich stamp must not read as a sync — "Recompute closeness synced"
        # is nonsense copy, and the row would be indistinguishable from a real one.
        db.state_set(self.conn, "import_synced_engagement", now_iso)
        # And a stamp whose registry entry no longer exists must vanish, not fall
        # through to a title-cased guess. State rows outlive registry entries, so
        # retiring any row on the Data page would otherwise announce it as a sync
        # for a week. The key here belongs to no registered source, deliberately.
        db.state_set(self.conn, "import_synced_a_retired_source", now_iso)
        self.conn.commit()

        out = feed.handle_feed(self.conn)
        notes = self._rows(out, "note")
        self.assertEqual(notes[0]["text"], "You noted how you met Anna Svensson")
        self.assertEqual(notes[0]["meta"], "Met at the garden fair.")
        facts = self._rows(out, "fact")
        self.assertEqual([r["name"] for r in facts], ["Paris Pierre"])
        self.assertEqual(facts[0]["text"], "is now in Paris")
        imports = self._rows(out, "import")
        self.assertEqual([r["text"] for r in imports], ["WhatsApp synced"],
                         "only real, currently-registered import sources may appear")

    def test_buckets_labeled_ordered_and_empty_dropped(self):
        cid = db.create_contact(self.conn, "Bucket Bea")
        self.conn.commit()
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        self._msg(cid, today + " 00:00:01", "morning!", key="b1")   # → Today (unanswered)
        self.conn.execute(                                          # → This week (note)
            "INSERT INTO notes (contact_id, body, created_at) VALUES (?, 'old note', ?)",
            (cid, self._ago(3)))
        self.conn.commit()

        out = feed.handle_feed(self.conn)
        labels = [b["label"] for b in out["buckets"]]
        self.assertEqual(labels, ["Today", "This week"])   # Yesterday empty → dropped
        for b in out["buckets"]:
            self.assertTrue(b["rows"])
            for r in b["rows"]:
                self.assertEqual(
                    set(r.keys()),
                    {"kind", "contact_id", "name", "text", "meta", "ts", "channel", "group"})

    def test_feed_empty_without_archive(self):
        # no archive.db and nothing curated recently → no buckets, valid shape
        os.remove(arc.ARCHIVE_DB_PATH)
        self.assertEqual(feed.handle_feed(self.conn), {"buckets": []})


class TestFirstRunArchiveIsEmptyNotBroken(Base):
    """A fresh install must not 500 on the person page.

    An empty data dir is the state every new user starts in.
    arc.open_if_present() only proves the FILE exists; an archive.db that exists
    with no `messages` table still raises OperationalError from the query, and
    /api/contact/{id}/episodes let it escape as a 500. Opening a person before importing any messages is the most ordinary
    thing a new user can do, so the app looked broken on first contact.

    handle_get_history already degraded to empty for this exact reason. Episodes
    was the one path that did not, which is the tell: the failure mode was known
    and one caller was simply missed.
    """

    def _archive_without_a_messages_table(self):
        """An archive.db that exists and is a valid SQLite file, but is empty —
        precisely what a first run has after something touches the path."""
        path = os.path.join(self.dir, "archive-empty.db")
        import sqlite3
        sqlite3.connect(path).close()
        return path

    def test_episodes_returns_empty_rather_than_500(self):
        path = self._archive_without_a_messages_table()
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", path):
            out = app.handle_get_episodes(1, {})
        # `threads` joined the payload with NC-106. Asserted key by key rather
        # than as one dict so the next field added here does not read as a
        # regression in first-run behaviour, which is what this class is about.
        self.assertEqual(out["episodes"], [])
        self.assertFalse(out["has_more"])

    def test_history_agrees(self):
        """The sibling endpoint, asserted alongside so the two cannot drift back
        apart — they are the same question about the same missing table."""
        path = self._archive_without_a_messages_table()
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", path):
            out = app.handle_get_history(1, {})
        self.assertEqual(out["messages"], [])
        self.assertFalse(out["has_more"])
        self.assertFalse(out["threads"])

    def test_threads_are_ignored_without_a_connection(self):
        """NC-106. Widening a page to a whole conversation means showing other
        people's lines, and naming them needs crm.db. A line nobody can
        attribute is worse than no line, so the two arrive together: no
        connection, no widening.

        Asserted against an archive that really holds the conversation. With the
        empty archive above, the handler would take its exception path and return
        threads=False no matter what the guard did, so removing the guard
        entirely would go unnoticed.
        """
        path = os.path.join(self.dir, "archive-with-a-thread.db")
        arc.init_archive(path)
        ac = arc.get_archive_db(path)
        for cid, ts, body in ((1, "2026-06-01 10:00:00", "mine"),
                              (2, "2026-06-01 10:01:00", "theirs")):
            arc.insert_message(ac, contact_id=cid, channel="imessage",
                               direction=0, ts=ts, body=body,
                               source_key=f"t{cid}", group_name="Weekend plans",
                               group_key="imessage:1")
        ac.commit(); ac.close()
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", path):
            out = app.handle_get_history(1, {"threads": ["1"]})
            self.assertFalse(out["threads"])
            self.assertEqual([m["body"] for m in out["messages"]], ["mine"],
                             "another person's line was served with no way to "
                             "name them")
            # /episodes has no such mode at all, by design — see its docstring.
            self.assertNotIn("threads", app.handle_get_episodes(1, {}))
