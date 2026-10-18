"""NC-092. A person's WhatsApp count is the messages the app can show you.

`crm/whatsapp_import.py` used to write `wa_count` twice per import. First it
ADDED WhatsApp's own total for each one-to-one chat, which includes photos,
stickers and deleted messages, none of which the archive keeps. Then it
OVERWROTE the count with an archive recount, but only for people who got a new
group message on that run. So one column held two different measurements,
decided by run history. So a card with WhatsApp history could disagree with its
own timeline: some cards claimed messages that were not saved anywhere, and some
people had saved messages that their cards called zero.

The rule: the count means the messages the app can actually show, recounted for
everyone from the saved messages. One writer now,
`engagement.recount_channels`, and it runs where the rest of the archive-derived
figures already run: at startup and after every import.
"""

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import MAY2026, Base, _coredata_secs, arc, code_of, db, wa  # noqa: E402

import api_messages  # noqa: E402
import api_serialize  # noqa: E402
import engagement  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANNA = "46701@s.whatsapp.net"


def _counts(conn, cid):
    r = conn.execute("SELECT wa_count, wa_last, imsg_count FROM contacts WHERE id = ?",
                     (cid,)).fetchone()
    return r[0], r[1], r[2]


class _WithArchive(Base):

    def setUp(self):
        super().setUp()
        self.arc_path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.arc_path)

    def save(self, cid, n, channel="whatsapp", start=0, group=""):
        a = arc.get_archive_db(self.arc_path)
        for i in range(n):
            arc.insert_message(a, contact_id=cid, channel=channel, direction=i % 2,
                               ts=f"2026-05-{1 + start + i:02d} 10:00:00",
                               body=f"message {start + i}",
                               source_key=f"{channel}-{cid}-{start + i}",
                               group_name=group, group_key=f"wa:{cid}" if group else "")
        a.commit()
        a.close()


class TestTheRecount(_WithArchive):

    def test_every_person_gets_what_the_archive_holds_for_them(self):
        anna = db.create_contact(self.conn, "Anna")
        cleo = db.create_contact(self.conn, "Cleo")
        dan = db.create_contact(self.conn, "Dan")
        # The old writers' leftovers: Anna's WhatsApp total counted her photos,
        # Cleo's count came with no saved message at all, Dan's said none.
        self.conn.execute("UPDATE contacts SET wa_count = 9, wa_last = '2026-06-30 00:00:00' "
                          "WHERE id = ?", (anna,))
        self.conn.execute("UPDATE contacts SET wa_count = 4, wa_last = '2026-01-01 00:00:00' "
                          "WHERE id = ?", (cleo,))
        self.conn.commit()
        self.save(anna, 3)
        self.save(anna, 2, start=10, group="Book club")   # her lines in a group
        self.save(dan, 2)
        engagement.refresh(self.conn, archive_path=self.arc_path)
        self.assertEqual(_counts(self.conn, anna)[:2], (5, "2026-05-12 10:00:00"))
        self.assertEqual(_counts(self.conn, cleo)[:2], (0, ""))
        self.assertEqual(_counts(self.conn, dan)[:2], (2, "2026-05-02 10:00:00"))

    def test_messages_that_belong_to_a_conversation_are_nobodys(self):
        anna = db.create_contact(self.conn, "Anna")
        self.conn.commit()
        self.save(anna, 1)
        self.save(arc.NO_PERSON, 6, group="Book club")    # the owner's own lines
        engagement.refresh(self.conn, archive_path=self.arc_path)
        self.assertEqual(_counts(self.conn, anna)[0], 1)

    def test_other_channels_keep_what_their_importers_wrote(self):
        """Only the channels covered by that rule are recounted. Email's counter still
        means what the email importer counted."""
        anna = db.create_contact(self.conn, "Anna")
        self.conn.execute("UPDATE contacts SET em_count = 40 WHERE id = ?", (anna,))
        self.conn.commit()
        self.save(anna, 2, channel="email")
        engagement.refresh(self.conn, archive_path=self.arc_path)
        self.assertEqual(self.conn.execute("SELECT em_count FROM contacts WHERE id = ?",
                                           (anna,)).fetchone()[0], 40)
        self.assertEqual(set(engagement.COUNTED_FROM_ARCHIVE),
                         {"whatsapp", "imessage", "linkedin"})

    def test_only_what_changed_is_written(self):
        anna = db.create_contact(self.conn, "Anna")
        db.create_contact(self.conn, "Cleo")
        self.conn.commit()
        self.save(anna, 2)
        a = arc.get_archive_db(self.arc_path)
        try:
            self.assertEqual(engagement.recount_channels(self.conn, a), 1)
            self.assertEqual(engagement.recount_channels(self.conn, a), 0)
        finally:
            a.close()

    def test_a_missing_archive_changes_nothing(self):
        """A wrong data folder must not zero every card: with nothing to count
        from, nothing is recounted."""
        anna = db.create_contact(self.conn, "Anna")
        self.conn.execute("UPDATE contacts SET wa_count = 3 WHERE id = ?", (anna,))
        self.conn.commit()
        engagement.refresh(self.conn, archive_path=os.path.join(self.dir, "nowhere.db"))
        self.assertEqual(_counts(self.conn, anna)[0], 3)
        empty = os.path.join(self.dir, "empty.db")
        sqlite3.connect(empty).close()                 # NC-198: a file, no tables
        engagement.refresh(self.conn, archive_path=empty)
        self.assertEqual(_counts(self.conn, anna)[0], 3)

    def test_an_archive_with_no_whatsapp_left_zeroes_the_count(self):
        """The other direction: a readable archive that holds nothing for
        someone is a true zero, not a gap to leave alone."""
        anna = db.create_contact(self.conn, "Anna")
        self.conn.execute("UPDATE contacts SET wa_count = 3 WHERE id = ?", (anna,))
        self.conn.commit()
        engagement.refresh(self.conn, archive_path=self.arc_path)
        self.assertEqual(_counts(self.conn, anna)[:2], (0, ""))


class TestAnImportFromStartToCard(_WithArchive):
    """The whole path on a stand-in WhatsApp database: match, save, recount, and
    then the card and the timeline, read the way the person page reads them."""

    def setUp(self):
        super().setUp()
        self.wa_path = os.path.join(self.dir, "ChatStorage.sqlite")
        c = sqlite3.connect(self.wa_path)
        c.executescript("""
        CREATE TABLE ZWACHATSESSION(Z_PK INTEGER PRIMARY KEY, ZSESSIONTYPE INTEGER,
                                    ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
        CREATE TABLE ZWAGROUPMEMBER(Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                                    ZMEMBERJID TEXT, ZISACTIVE INTEGER);
        CREATE TABLE ZWAMESSAGE(Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                                ZMESSAGEDATE REAL, ZTEXT TEXT, ZISFROMME INTEGER,
                                ZGROUPMEMBER INTEGER);
        """)
        secs = _coredata_secs(MAY2026)
        c.execute("INSERT INTO ZWACHATSESSION VALUES (1, 0, ?, 'Anna')", (ANNA,))
        c.execute("INSERT INTO ZWACHATSESSION VALUES (10, 1, 'g10@g.us', 'Book club')")
        c.execute("INSERT INTO ZWAGROUPMEMBER VALUES (100, 10, ?, 1)", (ANNA,))
        c.execute("INSERT INTO ZWAGROUPMEMBER VALUES (101, 10, 'x@s.whatsapp.net', 1)")
        c.executemany("INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?)", [
            # one-to-one: three things said, two photos with no text
            (1, 1, secs - 500, "Hey, how are you?", 0, None),
            (2, 1, secs - 400, None, 0, None),
            (3, 1, secs - 300, "Doing well", 1, None),
            (4, 1, secs - 200, None, 1, None),
            (5, 1, secs - 100, "See you soon", 0, None),
            # the group: two lines from Anna, one from the owner
            (6, 10, secs - 50, "Chapter four?", 0, 100),
            (7, 10, secs - 40, "Yes, Thursday", 1, None),
            (8, 10, secs - 30, "Great", 0, 100),
        ])
        c.commit()
        c.close()
        self.anna = db.create_contact(self.conn, "Anna")
        # A count left behind by the old importer, to prove it does not survive.
        self.conn.execute("UPDATE contacts SET wa_count = 77 WHERE id = ?", (self.anna,))
        self.conn.commit()

    def _import(self):
        with mock.patch.object(db, "DB_PATH", self.path):
            sessions = wa.aggregate_sessions(self.wa_path)
            self.assertEqual(sessions[0]["count"], 5, "WhatsApp itself holds five")
            _, jid_to_cid = wa.apply(sessions, {}, include_unknown=False, dry_run=False)
            wa.archive_and_chunk(self.wa_path, jid_to_cid, max_members=8,
                                 archive_path=self.arc_path)
        engagement.refresh(self.conn, archive_path=self.arc_path)

    def test_the_card_counts_what_the_timeline_draws(self):
        self._import()
        row = db.get_contact(self.conn, self.anna)
        card = api_serialize._channel_counts(row)["whatsapp"]
        with mock.patch.object(arc, "ARCHIVE_DB_PATH", self.arc_path):
            episodes = api_messages.handle_get_episodes(self.anna, {"limit": ["100"]})["episodes"]
        timeline = sum(e["count"] for e in episodes if e["channel"] == "whatsapp")
        self.assertEqual(timeline, 5, "three one-to-one lines and her two in the group")
        self.assertEqual(card, timeline)
        self.assertEqual(row["wa_last"], max(e["ts_end"] for e in episodes))

    def test_importing_again_changes_nothing(self):
        self._import()
        first = _counts(self.conn, self.anna)
        self._import()
        self.assertEqual(_counts(self.conn, self.anna), first)


class TestThereIsOneWriter(unittest.TestCase):

    def test_the_importer_writes_no_whatsapp_count(self):
        """The two old writers were an add in apply() and a recount of touched
        people in archive_group_messages(). Either one coming back puts the
        second measurement back in the column."""
        code = code_of(os.path.join(REPO, "crm", "whatsapp_import.py"))
        for gone in ("wa_count", "wa_last", "set_channel", "reset_channel", "bump_channel"):
            with self.subTest(name=gone):
                self.assertNotIn(gone, code)

    def test_nothing_else_in_the_app_writes_it(self):
        """db.merge_contacts adds the two people's counts, which equals the
        recount because the merge moves the messages too. Anything else that
        sets the column is a second definition."""
        import glob
        import re
        writes = re.compile(r"SET[^;\"']*\bwa_count\s*=|set_channel\([^)]*whatsapp")
        found = []
        for path in sorted(glob.glob(os.path.join(REPO, "crm", "*.py"))):
            if writes.search(code_of(path)):
                found.append(os.path.basename(path))
        self.assertEqual(found, [])

    def test_an_import_run_by_hand_recounts_too(self):
        """The app recounts after the imports it runs. Run from a terminal, the
        importer is the only thing that runs, so it has to recount itself."""
        code = code_of(os.path.join(REPO, "crm", "whatsapp_import.py"))
        main = code[code.index("def main():"):]
        self.assertIn("engagement.refresh(", main)


if __name__ == "__main__":
    unittest.main()
