"""The cached 'story so far' brief and the next-action line derived from it."""

import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, arc, db,
)


class TestBriefMetaAndNextAction(Base):
    """The brief receipt (brief_meta written at generation time, read back with
    newer_messages) and the derived next_action card."""

    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="crm_bm_")
        self._orig_arc = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = os.path.join(self.arc_dir, "archive.db")
        arc.init_archive(arc.ARCHIVE_DB_PATH)

    def tearDown(self):
        arc.ARCHIVE_DB_PATH = self._orig_arc
        super().tearDown()

    def _msg(self, cid, ts, body, direction=0, channel="whatsapp", key=None):
        ac = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        arc.insert_message(ac, contact_id=cid, channel=channel, direction=direction,
                           ts=ts, body=body, source_key=key or (ts + body))
        ac.commit()
        ac.close()

    def _old(self, days=400):
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def test_brief_meta_written_and_read_with_newer_messages(self):
        cid = db.create_contact(self.conn, "Ada Lovelace")
        self.conn.commit()
        self._msg(cid, "2026-06-01 10:00:00", "hello")
        self._msg(cid, "2026-06-01 10:05:00", "hi back", direction=1)
        self._msg(cid, "2026-06-02 09:00:00", "sent you the deck", channel="email")

        # Patch the OWNER module, not app. handle_make_brief lives in
        # api_briefs.py and reads its own module global, so rebinding
        # app._run_claude (which is only a re-export) does not reach it — the
        # stub would be bypassed and a real, billed Anthropic call made. The
        # assertion on out["story"] below is what makes that loud.
        with mock.patch.object(app.api_briefs, "_run_claude",
                               lambda *a, **k: "You met Ada at a maths salon."):
            out = app.handle_make_brief(self.conn, cid)
        # Assert the STUBBED text came back, not just the metadata. Without
        # this, moving _run_claude to another module and forgetting to update
        # the patch target above would bypass the stub, make a real, billed
        # Anthropic call against a configured key — and still pass,
        # because brief_meta's counts do not depend on what the model said.
        self.assertEqual(out["story"], "You met Ada at a maths salon.")
        self.assertEqual(out["brief_meta"]["messages"], 3)
        self.assertEqual(out["brief_meta"]["channels"], 2)

        meta = app.handle_get_contact(self.conn, cid)["brief_meta"]
        self.assertEqual(meta["messages"], 3)
        self.assertEqual(meta["channels"], 2)
        self.assertTrue(meta["built_at"])
        self.assertEqual(meta["newer_messages"], 0)

        # a message newer than built_at flips the stale marker
        self._msg(cid, "2099-01-01 00:00:00", "from the future")
        meta2 = app.handle_get_contact(self.conn, cid)["brief_meta"]
        self.assertEqual(meta2["newer_messages"], 1)

    def test_brief_meta_null_without_brief(self):
        cid = db.create_contact(self.conn, "No Brief Bo")
        self.conn.commit()
        self.assertIsNone(app.handle_get_contact(self.conn, cid)["brief_meta"])

    def test_next_action_present_when_gone_cold(self):
        cid = db.create_contact(self.conn, "Cold Carla")
        db.mark_reviewed(self.conn, cid, "described")
        old = self._old()
        self.conn.execute("UPDATE contacts SET last_contact=? WHERE id=?", (old, cid))
        self.conn.commit()
        self._msg(cid, old, "see you around!")
        na = app.handle_get_contact(self.conn, cid)["next_action"]
        self.assertEqual(na["kind"], "reconnect")
        self.assertGreaterEqual(na["days_silent"], 399)
        self.assertEqual(na["last_snippet"], "see you around!")
        self.assertEqual(na["last_channel"], "whatsapp")

    def test_next_action_null_when_recent_or_snoozed(self):
        fresh = db.create_contact(self.conn, "Fresh Fia")
        db.mark_reviewed(self.conn, fresh, "described")
        recent = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("UPDATE contacts SET last_contact=? WHERE id=?", (recent, fresh))
        self.conn.commit()
        self.assertIsNone(app.handle_get_contact(self.conn, fresh)["next_action"])

        cold = db.create_contact(self.conn, "Snoozed Sam")
        db.mark_reviewed(self.conn, cold, "described")
        self.conn.execute("UPDATE contacts SET last_contact=? WHERE id=?", (self._old(), cold))
        self.conn.commit()
        self.assertIsNotNone(app.handle_get_contact(self.conn, cold)["next_action"])
        db.dismiss_reconnect(self.conn, cold)
        self.conn.commit()
        self.assertIsNone(app.handle_get_contact(self.conn, cold)["next_action"])
