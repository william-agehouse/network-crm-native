"""Relationship strength: the 0-100 familiarity score, its buckets, and the reconnect qualification test built on it."""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, db,
)


class TestStrength(Base):
    """Relationship-strength score + the dashboard that surfaces it."""

    def _recent(self, days=5):
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def test_blank_contact_scores_zero(self):
        cid = db.create_contact(self.conn, "Blank Person")
        row = db.get_contact(self.conn, cid)
        self.assertEqual(db.relationship_strength(row), 0)
        self.assertEqual(db.strength_label(0), "faint")

    def test_recent_multichannel_beats_stale_single_channel(self):
        # Recent across 3 channels = strong; one old channel = weak.
        strong = db.create_contact(self.conn, "Close Connie")
        db.set_channel(self.conn, strong, "email", 120, self._recent())
        db.set_channel(self.conn, strong, "imessage", 300, self._recent())
        db.set_channel(self.conn, strong, "whatsapp", 40, self._recent())
        weak = db.create_contact(self.conn, "Distant Dan")
        db.set_channel(self.conn, weak, "email", 3, "2019-01-01 00:00:00")
        self.conn.commit()
        s_strong = db.relationship_strength(db.get_contact(self.conn, strong))
        s_weak = db.relationship_strength(db.get_contact(self.conn, weak))
        self.assertGreater(s_strong, s_weak)
        self.assertGreaterEqual(s_strong, 65)   # "close" tier
        self.assertLess(s_weak, 40)

    def test_recency_decays(self):
        recent = db.create_contact(self.conn, "Recent Rita")
        db.set_channel(self.conn, recent, "email", 50, self._recent(days=2))
        old = db.create_contact(self.conn, "Old Otto")
        db.set_channel(self.conn, old, "email", 50, self._recent(days=400))
        self.conn.commit()
        s_recent = db.relationship_strength(db.get_contact(self.conn, recent))
        s_old = db.relationship_strength(db.get_contact(self.conn, old))
        self.assertGreater(s_recent, s_old)

    def test_breakdown_components(self):
        cid = db.create_contact(self.conn, "Multi Mia")
        db.set_channel(self.conn, cid, "email", 10, self._recent())
        db.set_channel(self.conn, cid, "telegram", 10, self._recent())
        self.conn.commit()
        b = db.strength_breakdown(db.get_contact(self.conn, cid))
        self.assertEqual(b["channels_used"], 2)
        self.assertEqual(b["total_messages"], 20)
        self.assertEqual(b["warmth"], 1.0)        # in rhythm (recent contact)
        self.assertEqual(b["reciprocity"], 1.0)   # no direction data → neutral
        self.assertGreater(b["bond"], 0)

    def _set_eng(self, cid, *, days, sent, recv, first, last):
        self.conn.execute(
            """UPDATE contacts SET eng_active_days=?, eng_sent=?, eng_recv=?,
               eng_first=?, eng_last=? WHERE id=?""",
            (days, sent, recv, first, last, cid))

    def _ago(self, days):
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def test_stranger_with_recent_message_scores_low(self):
        # THE core fix: one message yesterday must not read as a relationship.
        cid = db.create_contact(self.conn, "Stranger Sten")
        db.set_channel(self.conn, cid, "linkedin", 1, self._recent(days=1))
        self._set_eng(cid, days=1, sent=0, recv=1,
                      first=self._ago(1), last=self._ago(1))
        self.conn.commit()
        self.assertLess(db.relationship_strength(db.get_contact(self.conn, cid)), 12)

    def test_old_friend_keeps_most_of_their_score(self):
        # 400 shared days over 3 years, balanced — quiet for 10 months.
        friend = db.create_contact(self.conn, "Quiet Freja")
        db.set_channel(self.conn, friend, "whatsapp", 4000, self._ago(300))
        db.mark_reviewed(self.conn, friend, "described")
        self._set_eng(friend, days=400, sent=2000, recv=2000,
                      first=self._ago(1200), last=self._ago(300))
        self.conn.commit()
        score = db.relationship_strength(db.get_contact(self.conn, friend))
        self.assertGreaterEqual(score, 40)   # silence never erases real history

    def test_one_way_stream_is_crushed(self):
        # Same volume/recency, but all inbound (newsletter/support-bot shape)…
        bot = db.create_contact(self.conn, "Noreply Bot")
        db.set_channel(self.conn, bot, "email", 300, self._recent())
        self._set_eng(bot, days=120, sent=0, recv=300,
                      first=self._ago(700), last=self._ago(3))
        # …versus a balanced human with identical shape
        pal = db.create_contact(self.conn, "Balanced Bea")
        db.set_channel(self.conn, pal, "email", 300, self._recent())
        self._set_eng(pal, days=120, sent=150, recv=150,
                      first=self._ago(700), last=self._ago(3))
        self.conn.commit()
        s_bot = db.relationship_strength(db.get_contact(self.conn, bot))
        s_pal = db.relationship_strength(db.get_contact(self.conn, pal))
        self.assertLess(s_bot, 0.5 * s_pal)

    def test_shared_days_beat_burst_volume(self):
        # 2,000 messages in one week vs 500 spread over two years of real contact.
        burst = db.create_contact(self.conn, "Burst Bo")
        db.set_channel(self.conn, burst, "whatsapp", 2000, self._ago(20))
        self._set_eng(burst, days=7, sent=1000, recv=1000,
                      first=self._ago(27), last=self._ago(20))
        steady = db.create_contact(self.conn, "Steady Siv")
        db.set_channel(self.conn, steady, "whatsapp", 500, self._ago(20))
        self._set_eng(steady, days=200, sent=250, recv=250,
                      first=self._ago(750), last=self._ago(20))
        self.conn.commit()
        s_burst = db.relationship_strength(db.get_contact(self.conn, burst))
        s_steady = db.relationship_strength(db.get_contact(self.conn, steady))
        self.assertGreater(s_steady, s_burst)

    def test_top_by_strength_excludes_hidden_and_zero(self):
        keep = db.create_contact(self.conn, "Keep Kim")
        db.set_channel(self.conn, keep, "imessage", 200, self._recent())
        hidden = db.create_contact(self.conn, "Hidden Hugo")
        db.set_channel(self.conn, hidden, "imessage", 999, self._recent())
        db.set_excluded(self.conn, hidden, 1)
        db.create_contact(self.conn, "Zero Zoe")  # no history -> score 0, dropped
        self.conn.commit()
        top = db.top_by_strength(self.conn, limit=8)
        ids = {r["id"] for _, r in top}
        self.assertIn(keep, ids)
        self.assertNotIn(hidden, ids)         # excluded
        self.assertEqual(len(top), 1)         # zero-score contact dropped

    def test_coverage_and_the_strongest_ties(self):
        # Was test_handle_dashboard_shape. The dashboard route went in NC-264;
        # these are the two reads it composed, which the Data page still uses.
        cid = db.create_contact(self.conn, "Dash Dora")
        db.set_channel(self.conn, cid, "email", 30, self._recent())
        self.conn.commit()
        cov = app.api_contacts._channel_coverage(self.conn)
        self.assertEqual(cov["any_history"], 1)
        self.assertEqual(cov["email"], 1)
        top = db.top_by_strength(self.conn, limit=8)
        self.assertEqual(top[0][1]["id"], cid)
        self.assertGreater(top[0][0], 0)

    def test_semantic_search_degrades_without_libs(self):
        # In CI/dev without sentence-transformers, the endpoint must not raise.
        cid = db.create_contact(self.conn, "Sem Sam")
        self.conn.commit()
        out = app.handle_semantic_search(self.conn, {"q": ["anyone"]})
        self.assertIn("available", out)
        self.assertIsInstance(out["contacts"], list)
        if not out["available"]:
            self.assertIn("message", out)

    def test_semantic_empty_query(self):
        out = app.handle_semantic_search(self.conn, {"q": [""]})
        self.assertEqual(out["contacts"], [])


class TestReconnect(Base):
    def _set_last_contact(self, cid, value):
        self.conn.execute("UPDATE contacts SET last_contact=? WHERE id=?", (value, cid))

    def _old(self):
        """A date well over 6 months before today."""
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _recent(self):
        """A date a few days ago — comfortably inside 6 months."""
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def test_described_long_ago_appears(self):
        cid = db.create_contact(self.conn, "Overdue Olga")
        db.add_note(self.conn, cid, "old friend")
        db.mark_reviewed(self.conn, cid, "described")
        self._set_last_contact(cid, self._old())
        self.conn.commit()
        ids = {r["id"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertIn(cid, ids)

    def test_described_recent_does_not_appear(self):
        cid = db.create_contact(self.conn, "Fresh Fred")
        db.add_note(self.conn, cid, "just talked")
        db.mark_reviewed(self.conn, cid, "described")
        self._set_last_contact(cid, self._recent())
        self.conn.commit()
        ids = {r["id"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertNotIn(cid, ids)

    def test_excluded_never_appears(self):
        cid = db.create_contact(self.conn, "Excluded Eli")
        db.add_note(self.conn, cid, "do not surface")
        db.mark_reviewed(self.conn, cid, "described")
        self._set_last_contact(cid, self._old())
        db.set_excluded(self.conn, cid, 1)
        self.conn.commit()
        ids = {r["id"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertNotIn(cid, ids)

    def test_new_contact_no_signal_does_not_appear(self):
        # 'new' status, no notes, no message history -> not a reconnect candidate
        cid = db.create_contact(self.conn, "Blank Brian")
        self.conn.commit()
        ids = {r["id"] for r in db.find_reconnect(self.conn, months=6)}
        self.assertNotIn(cid, ids)

    def test_overdue_sorted_before_cold_and_oldest_first(self):
        # two overdue (different ages) + one cold (signal via message history, no date)
        older = db.create_contact(self.conn, "Older Contact")
        db.mark_reviewed(self.conn, older, "described")
        self.conn.execute("UPDATE contacts SET last_contact='2020-01-01 00:00:00' WHERE id=?", (older,))
        newer = db.create_contact(self.conn, "Newer Contact")
        db.mark_reviewed(self.conn, newer, "described")
        self.conn.execute("UPDATE contacts SET last_contact='2024-01-01 00:00:00' WHERE id=?", (newer,))
        cold = db.create_contact(self.conn, "Cold Contact")
        self.conn.execute("UPDATE contacts SET msg_count=3 WHERE id=?", (cold,))
        self.conn.commit()
        order = [r["id"] for r in db.find_reconnect(self.conn, months=6)]
        self.assertEqual(order, [older, newer, cold])  # oldest overdue first, cold last

    def test_handle_reconnect_returns_contacts_list(self):
        cid = db.create_contact(self.conn, "Api Annie")
        db.add_note(self.conn, cid, "knows me")
        db.mark_reviewed(self.conn, cid, "described")
        self._set_last_contact(cid, self._old())
        self.conn.commit()
        out = app.handle_reconnect(self.conn, {})
        self.assertEqual(out["months"], 6)
        self.assertIsInstance(out["contacts"], list)
        self.assertIn(cid, {c["id"] for c in out["contacts"]})

    def test_handle_reconnect_clamps_months(self):
        self.assertEqual(app.handle_reconnect(self.conn, {"months": ["0"]})["months"], 1)
        self.assertEqual(app.handle_reconnect(self.conn, {"months": ["999"]})["months"], 120)
        self.assertEqual(app.handle_reconnect(self.conn, {"months": ["junk"]})["months"], 6)
