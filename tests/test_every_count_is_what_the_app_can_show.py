"""NC-204 and NC-107. iMessage and LinkedIn counts are what the app can show.

The rule for WhatsApp (NC-092) is that a person's count means the messages the
app can actually show, recounted for everyone from the saved messages. NC-204
extended it to iMessage and LinkedIn, the other two channels whose cards
could disagree with their timelines:

* iMessage — the importer zeroed everyone and added Apple's one-to-one totals
  back, which count photos and leave out group chats, while the timeline draws
  the person's lines in groups too. A card could say no iMessage at all over
  saved messages.
* LinkedIn — the importer never let the count go down, so it kept placeholders
  an old scraper counted and never saved, and a card could claim more messages
  than its timeline held.

One writer now for all three, `engagement.recount_channels`. The WhatsApp half
is in test_the_whatsapp_count_is_what_the_app_can_show.py.
"""

import glob
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, arc, code_of, db  # noqa: E402

import api_messages  # noqa: E402
import api_serialize  # noqa: E402
import engagement  # noqa: E402
import imessage_import  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLUMNS = {"imessage": ("imsg_count", "imsg_last"),
           "linkedin": ("msg_count", "last_contact")}


class _Saved(Base):

    def setUp(self):
        super().setUp()
        self.arc_path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.arc_path)

    def save(self, cid, channel, n, start=0, group=""):
        a = arc.get_archive_db(self.arc_path)
        for i in range(n):
            arc.insert_message(a, contact_id=cid, channel=channel, direction=i % 2,
                               ts=f"2026-05-{1 + start + i:02d} 10:00:00",
                               body=f"{channel} {start + i}",
                               source_key=f"{channel}-{cid}-{start + i}",
                               group_name=group,
                               group_key=f"{channel}:{cid}" if group else "")
        a.commit()
        a.close()

    def stored(self, cid, channel):
        count_col, last_col = COLUMNS[channel]
        r = self.conn.execute(f"SELECT {count_col}, {last_col} FROM contacts WHERE id = ?",
                              (cid,)).fetchone()
        return r[0], r[1]

    def refresh(self):
        engagement.refresh(self.conn, archive_path=self.arc_path)


class TestTheRecountCoversBoth(_Saved):

    def test_imessage_counts_group_lines_and_drops_what_was_never_saved(self):
        ada = db.create_contact(self.conn, "Ada")
        bo = db.create_contact(self.conn, "Bo")
        # Apple's one-to-one total with photos in it, and a count with nothing
        # saved behind it at all.
        self.conn.execute("UPDATE contacts SET imsg_count = 9, imsg_last = '2026-06-30 00:00:00' "
                          "WHERE id = ?", (ada,))
        self.conn.execute("UPDATE contacts SET imsg_count = 3 WHERE id = ?", (bo,))
        self.conn.commit()
        self.save(ada, "imessage", 2)
        self.save(ada, "imessage", 3, start=10, group="Book club")
        self.refresh()
        self.assertEqual(self.stored(ada, "imessage"), (5, "2026-05-13 10:00:00"))
        self.assertEqual(self.stored(bo, "imessage"), (0, ""))

    def test_linkedin_goes_down_as_well_as_up(self):
        """The rule this replaces could only go up."""
        cy = db.create_contact(self.conn, "Cy")
        db.set_channel(self.conn, cy, "linkedin", 40, "2026-07-01 00:00:00")
        self.conn.commit()
        self.save(cy, "linkedin", 25)
        self.refresh()
        self.assertEqual(self.stored(cy, "linkedin"), (25, "2026-05-25 10:00:00"))

    def test_a_wrong_date_is_fixed_even_when_the_count_is_right(self):
        """A LinkedIn or iMessage last date could be wrong, even beside a count
        that was right."""
        gus = db.create_contact(self.conn, "Gus")
        db.set_channel(self.conn, gus, "linkedin", 2, "2026-12-31 00:00:00")
        self.conn.commit()
        self.save(gus, "linkedin", 2)
        self.refresh()
        self.assertEqual(self.stored(gus, "linkedin"), (2, "2026-05-02 10:00:00"))

    def test_each_channel_is_counted_on_its_own(self):
        dee = db.create_contact(self.conn, "Dee")
        self.conn.commit()
        self.save(dee, "imessage", 4)
        self.save(dee, "linkedin", 2, start=20)
        self.save(dee, "whatsapp", 1, start=25)
        self.refresh()
        self.assertEqual(self.stored(dee, "imessage")[0], 4)
        self.assertEqual(self.stored(dee, "linkedin")[0], 2)
        self.assertEqual(self.conn.execute("SELECT wa_count FROM contacts WHERE id = ?",
                                           (dee,)).fetchone()[0], 1)

    def test_the_card_counts_what_the_timeline_draws(self):
        eve = db.create_contact(self.conn, "Eve")
        self.conn.execute("UPDATE contacts SET imsg_count = 50, msg_count = 50 WHERE id = ?", (eve,))
        self.conn.commit()
        self.save(eve, "imessage", 3)
        self.save(eve, "imessage", 2, start=10, group="Chess club")
        self.save(eve, "linkedin", 4, start=20)
        self.refresh()
        card = api_serialize._channel_counts(db.get_contact(self.conn, eve))
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", self.arc_path):
            episodes = api_messages.handle_get_episodes(eve, {"limit": ["100"]})["episodes"]
        for channel in ("imessage", "linkedin"):
            with self.subTest(channel=channel):
                drawn = sum(e["count"] for e in episodes if e["channel"] == channel)
                self.assertEqual(card[channel], drawn)


class TestTheImportersWriteNoCount(Base):

    def test_imessage_apply_matches_people_and_counts_nothing(self):
        """apply() still finds the person and records the handle; the count is
        the recount's to write, once the messages are saved."""
        fay = db.create_contact(self.conn, "Fay")
        db.add_identifier(self.conn, fay, "phone", "+46701740605", source="manual")
        self.conn.execute("UPDATE contacts SET imsg_count = 7 WHERE id = ?", (fay,))
        self.conn.commit()
        handles = {"+46701740605": {"count": 99, "last": "2026-09-01 10:00:00"}}
        with mock.patch.object(db, "DB_PATH", self.path):
            stats, handle_to_cid = imessage_import.apply(handles, {}, include_unknown=False,
                                                         dry_run=False)
        self.assertEqual(handle_to_cid, {"+46701740605": fay})
        row = db.get_contact(self.conn, fay)
        self.assertEqual((row["imsg_count"], row["imsg_last"] or ""), (7, ""),
                         "apply() wrote a count: the second measurement is back")

    def test_no_importer_writes_these_columns(self):
        """The old writers were an add in imessage_import.apply and a recount
        with a floor in linkedin_import.archive_messages. Either coming back,
        or a new one anywhere in crm/, puts a second definition in the column."""
        cols = r"(imsg_count|imsg_last|msg_count|last_contact|wa_count|wa_last)"
        writes = re.compile(rf"SET[^;\"']*\b{cols}\s*=|"
                            r"(set_channel|bump_channel|reset_channel)\([^)]*"
                            r"[\"'](imessage|linkedin|whatsapp)[\"']")
        found = [os.path.basename(p) for p in sorted(glob.glob(os.path.join(REPO, "crm", "*.py")))
                 if writes.search(code_of(p))]
        self.assertEqual(found, [])

    def test_every_import_run_by_hand_recounts(self):
        """The app recounts after the imports it runs. From a terminal, the
        importer is the only thing that runs, so each one recounts itself."""
        for name, after in (("imessage_import.py", "def main():"),
                            ("linkedin_import.py", "def main()"),
                            ("whatsapp_import.py", "def main():"),
                            ("linkedin_snapshot_import.py", "def run(")):
            with self.subTest(importer=name):
                code = code_of(os.path.join(REPO, "crm", name))
                self.assertIn("engagement.refresh(", code[code.index(after):])


if __name__ == "__main__":
    unittest.main()
