"""Importers reading their sources: parsing each export format and matching people to existing contacts (never creating them)."""

import datetime
import io
import json
import mailbox
import os
import sqlite3
import sys
import tempfile
import unittest
from email.message import Message

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    MAY2026, _apple_ns, _coredata_secs, ci, db, ei, im, meta,
    py_block, sc, tg, wa,
)


class TestImessage(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(im.apple_date_to_iso(_apple_ns(MAY2026)), "2026-05-01 00:00:00")
        self.assertEqual(im.normalize_phone("+46 70 174 0605"), im.normalize_phone("070-174 0605"))
        self.assertTrue(im.is_email("a@b.example"))

    def test_aggregate_excludes_tapbacks_and_groups(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "chat.db")
        c = sqlite3.connect(p)
        c.executescript("""
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INTEGER, associated_message_type INTEGER);
        CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, style INTEGER);
        CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);
        CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
        """)
        c.execute("INSERT INTO handle VALUES (1,'+46701740605')")
        c.execute("INSERT INTO handle VALUES (2,'+46701740699')")
        c.execute("INSERT INTO chat VALUES (1,45)")   # 1:1
        c.execute("INSERT INTO chat VALUES (2,43)")   # group
        c.executemany("INSERT INTO chat_handle_join VALUES (?,?)", [(1, 1), (2, 1), (2, 2)])
        raw = _apple_ns(MAY2026)
        rows = [(i + 1, raw, 0) for i in range(4)] + [(i + 5, raw, 2000) for i in range(3)]  # 4 real + 3 tapbacks
        rows.append((8, raw, 0))  # a group message (chat 2) — must be excluded
        c.executemany("INSERT INTO message VALUES (?,?,?)", rows)
        c.executemany("INSERT INTO chat_message_join VALUES (1,?)", [(i + 1,) for i in range(7)])
        c.execute("INSERT INTO chat_message_join VALUES (2,8)")
        c.commit(); c.close()
        agg = im.aggregate_handles(p)
        self.assertEqual(agg["+46701740605"]["count"], 4)  # tapbacks + group excluded
        self.assertNotIn("+46701740699", agg)              # group-only handle excluded


class TestWhatsapp(unittest.TestCase):
    def test_aggregate_sessions_excludes_groups(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ChatStorage.sqlite")
        c = sqlite3.connect(p)
        c.executescript("""
        CREATE TABLE ZWACHATSESSION(Z_PK INTEGER PRIMARY KEY, ZSESSIONTYPE INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
        CREATE TABLE ZWAMESSAGE(Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMESSAGEDATE REAL);
        """)
        c.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?)", [
            (1, 0, "46701740611@s.whatsapp.net", "Anna Svensson"),
            (2, 1, "x@g.us", "Group"),
        ])
        secs = _coredata_secs(MAY2026)
        msgs = [(i + 1, 1, secs) for i in range(9)] + [(i + 10, 2, secs) for i in range(50)]
        c.executemany("INSERT INTO ZWAMESSAGE VALUES (?,?,?)", msgs)
        c.commit(); c.close()
        sess = wa.aggregate_sessions(p)
        self.assertEqual(len(sess), 1)
        self.assertEqual(sess[0]["name"], "Anna Svensson")
        self.assertEqual(sess[0]["count"], 9)
        self.assertTrue(wa.has_alpha("Ture"))
        self.assertFalse(wa.has_alpha("+46 70 174 0611"))


class TestEmailImport(unittest.TestCase):
    """Email .mbox import: enrich existing contacts, two-way only, filter noise.

    Builds a tiny synthetic mailbox and runs the importer against a temp crm.db
    (the real database is never touched — db.DB_PATH and EXCLUDE_FILE are patched
    for the duration of the test and restored afterwards)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_email_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        # apply() calls db.get_db() with no path -> point DB_PATH at the temp db
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        # isolate the exclude list and pretend the importer's env is clean
        self._orig_exclude = ei.EXCLUDE_FILE
        ei.EXCLUDE_FILE = os.path.join(self.dir, "exclude_list.txt")
        self._orig_self = ei.SELF_EMAIL
        ei.SELF_EMAIL = "me@example.com"
        self.mbox_path = os.path.join(self.dir, "archive.mbox")

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        ei.EXCLUDE_FILE = self._orig_exclude
        ei.SELF_EMAIL = self._orig_self

    def _msg(self, frm, to, date, headers=None):
        m = Message()
        m["From"] = frm
        m["To"] = to
        m["Date"] = date
        for k, v in (headers or {}).items():
            m[k] = v
        m.set_payload("body")
        return m

    def _write_mbox(self, messages):
        box = mailbox.mbox(self.mbox_path)
        box.lock()
        for m in messages:
            box.add(m)
        box.flush()
        box.unlock()
        box.close()

    def _seed_ada(self):
        conn = db.get_db(self.dbpath)
        cid = db.create_contact(conn, "Ada Lovelace", source="linkedin")
        db.add_identifier(conn, cid, "email", "ada@example.com")
        conn.commit()
        conn.close()
        return cid

    def test_two_way_exchange_counts(self):
        cid = self._seed_ada()
        self._write_mbox([
            self._msg("Ada Lovelace <ada@example.com>", "me@example.com",
                      "Mon, 04 May 2026 09:00:00 +0000"),
            self._msg("Me <me@example.com>", "ada@example.com",
                      "Mon, 04 May 2026 10:30:00 +0000"),
        ])
        agg = ei.aggregate_mbox(self.mbox_path)
        stats, _ = ei.apply(agg, create_new=False, dry_run=False)
        conn = db.get_db(self.dbpath)
        row = db.get_contact(conn, cid)
        conn.close()
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(row["em_count"], 2)            # inbound + outbound
        self.assertEqual(row["em_last"], "2026-05-04 10:30:00")

    def test_reimport_is_idempotent(self):
        """Running the same import twice must not double the count (set, not add)."""
        cid = self._seed_ada()
        self._write_mbox([
            self._msg("Ada Lovelace <ada@example.com>", "me@example.com", "Mon, 04 May 2026 09:00:00 +0000"),
            self._msg("Me <me@example.com>", "ada@example.com", "Mon, 04 May 2026 10:30:00 +0000"),
        ])
        for _ in range(2):
            ei.apply(ei.aggregate_mbox(self.mbox_path), create_new=False, dry_run=False)
        conn = db.get_db(self.dbpath)
        self.assertEqual(db.get_contact(conn, cid)["em_count"], 2)  # not 4
        conn.close()

    def test_one_sided_newsletter_with_list_unsubscribe_ignored(self):
        cid = self._seed_ada()
        self._write_mbox([
            self._msg("BigCo News <news@bigco.example>", "me@example.com",
                      "Tue, 05 May 2026 08:00:00 +0000",
                      headers={"List-Unsubscribe": "<https://bigco.example/unsub>"}),
        ])
        agg = ei.aggregate_mbox(self.mbox_path)
        self.assertNotIn("news@bigco.example", agg)         # bulk header -> skipped at scan
        stats, _ = ei.apply(agg, create_new=False, dry_run=False)
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(stats["created"], 0)
        conn = db.get_db(self.dbpath)
        # Ada untouched, and no newsletter contact created
        self.assertEqual(db.get_contact(conn, cid)["em_count"], 0)
        self.assertEqual(len(db.find_exact(conn, "BigCo News")), 0)
        conn.close()

    def test_unmatched_address_not_created_without_flag(self):
        self._seed_ada()
        self._write_mbox([
            self._msg("Random Stranger <stranger@nowhere.example>", "me@example.com",
                      "Wed, 06 May 2026 12:00:00 +0000"),
            self._msg("Me <me@example.com>", "stranger@nowhere.example",
                      "Wed, 06 May 2026 13:00:00 +0000"),
        ])
        agg = ei.aggregate_mbox(self.mbox_path)
        stats, _ = ei.apply(agg, create_new=False, dry_run=False)
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["unmatched"], 1)         # two-way but no contact match
        conn = db.get_db(self.dbpath)
        self.assertEqual(len(db.find_exact(conn, "Random Stranger")), 0)
        conn.close()

    def test_subjects_captured_and_summarize_flag(self):
        """Subject lines are collected per address; passing a mock client writes auto_summary."""
        cid = self._seed_ada()
        m1 = self._msg("Ada Lovelace <ada@example.com>", "me@example.com",
                       "Mon, 04 May 2026 09:00:00 +0000")
        m1["Subject"] = "Meeting next week?"
        m2 = self._msg("Me <me@example.com>", "ada@example.com",
                       "Mon, 04 May 2026 10:30:00 +0000")
        m2["Subject"] = "Re: Meeting next week?"
        self._write_mbox([m1, m2])

        agg = ei.aggregate_mbox(self.mbox_path)
        self.assertIn("Meeting next week?", agg["ada@example.com"].subjects)

        class _FakeContent:
            type = "text"
            text = "Ada and I discussed a meeting."

        class _FakeMsg:
            content = [_FakeContent()]

        class _FakeClient:
            def __init__(self):
                self.calls = []

            @property
            def messages(self):
                return self

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return _FakeMsg()

        fake = _FakeClient()
        stats, _ = ei.apply(agg, create_new=False, dry_run=False, client=fake)
        self.assertEqual(stats["summarized"], 1)
        self.assertEqual(len(fake.calls), 1)
        conn = db.get_db(self.dbpath)
        row = db.get_contact(conn, cid)
        conn.close()
        self.assertEqual(row["auto_summary"], "Ada and I discussed a meeting.")


class TestTelegramImport(unittest.TestCase):
    """Telegram Desktop JSON export import: enrich existing contacts, 1:1 only,
    two-way only, ignore groups.

    Builds a synthetic result.json in a temp dir and runs the importer against a
    temp crm.db (the real database is never touched — db.DB_PATH and EXCLUDE_FILE
    are patched for the duration of the test and restored afterwards)."""

    SELF_USER_ID = 999

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_tg_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        # apply() calls db.get_db() with no path -> point DB_PATH at the temp db
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        # isolate the exclude list from the real one
        self._orig_exclude = tg.EXCLUDE_FILE
        tg.EXCLUDE_FILE = os.path.join(self.dir, "exclude_list.txt")
        self.export_path = os.path.join(self.dir, "result.json")

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        tg.EXCLUDE_FILE = self._orig_exclude

    def _msg(self, mid, frm_id, dt):
        return {
            "id": mid, "type": "message",
            "date": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "date_unixtime": str(int(dt.timestamp())),
            "from_id": frm_id, "text": "hi",
        }

    def _write_export(self, chats):
        data = {
            "personal_information": {"user_id": self.SELF_USER_ID,
                                     "first_name": "Me"},
            "chats": {"list": chats},
        }
        with open(self.export_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _seed_anna(self):
        conn = db.get_db(self.dbpath)
        cid = db.create_contact(conn, "Anna Svensson", source="linkedin")
        conn.commit()
        conn.close()
        return cid

    def test_import_one_to_one_two_way_ignores_groups_and_one_sided(self):
        cid = self._seed_anna()
        me = f"user{self.SELF_USER_ID}"
        anna_last = datetime.datetime(2026, 5, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)
        self._write_export([
            # two-way 1:1 with Anna: one from me, one from her -> counts (=2)
            {"name": "Anna Svensson", "type": "personal_chat", "id": 222, "messages": [
                self._msg(1, me, datetime.datetime(2026, 4, 30, 9, 0, 0,
                                                   tzinfo=datetime.timezone.utc)),
                self._msg(2, "user222", anna_last),
            ]},
            # a group with many messages — must be ignored entirely
            {"name": "Some Group", "type": "private_group", "id": 333, "messages": [
                self._msg(10 + i, "user444", anna_last) for i in range(50)
            ]},
            # one-sided 1:1: only the other person sent -> skipped
            {"name": "Carl Cold", "type": "personal_chat", "id": 555, "messages": [
                self._msg(100, "user555", anna_last),
                self._msg(101, "user555", anna_last),
            ]},
            # saved messages — ignored like any non-personal_chat
            {"name": "Saved Messages", "type": "saved_messages", "id": 999,
             "messages": [self._msg(200, me, anna_last)]},
        ])

        chats, group_count = tg.aggregate_chats(tg.load_export(self.export_path))
        stats, _ = tg.apply(chats, group_count, create_new=False, dry_run=False)

        # group + saved_messages both counted as "other", neither enriches anyone
        self.assertEqual(group_count, 2)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["skipped_group"], 2)
        self.assertEqual(stats["skipped_one_sided"], 1)
        self.assertEqual(stats["contacts_touched"], 1)

        conn = db.get_db(self.dbpath)
        anna = db.get_contact(conn, cid)
        # the one-sided chat created/updated nothing
        self.assertEqual(len(db.find_exact(conn, "Carl Cold")), 0)
        conn.close()
        self.assertEqual(anna["tg_count"], 2)                  # both sides counted
        self.assertEqual(anna["tg_last"], "2026-05-01 10:00:00")


class TestMetaImport(unittest.TestCase):
    """Meta 'Download Your Information' import (Facebook Messenger / Instagram):
    enrich existing contacts, 1:1 only, two-way only, ignore groups, undo Meta's
    mojibake so accented names match.

    Builds a synthetic inbox/ tree of message_1.json files in a temp dir and runs
    the importer against a temp crm.db (the real database is never touched —
    db.DB_PATH, EXCLUDE_FILE and SELF_NAME are patched for the test and restored)."""

    SELF = "Me Myself"
    PLATFORM = "messenger"           # exercise the messenger -> msgr_* columns
    ANNA_LAST = "2026-05-01 10:00:00"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_meta_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        # apply() calls db.get_db() with no path -> point DB_PATH at the temp db
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        # isolate the exclude list and pin "you" to a known name
        self._orig_exclude = meta.EXCLUDE_FILE
        meta.EXCLUDE_FILE = os.path.join(self.dir, "exclude_list.txt")
        self._orig_self = meta.SELF_NAME
        meta.SELF_NAME = self.SELF
        self.inbox = os.path.join(self.dir, "inbox")
        os.makedirs(self.inbox)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        meta.EXCLUDE_FILE = self._orig_exclude
        meta.SELF_NAME = self._orig_self

    @staticmethod
    def _ms(dt):
        return int(dt.timestamp() * 1000)

    def _msg(self, sender, dt):
        return {"sender_name": sender, "timestamp_ms": self._ms(dt), "content": "hi"}

    def _write_thread(self, slug, participants, title, messages):
        folder = os.path.join(self.inbox, slug)
        os.makedirs(folder, exist_ok=True)
        data = {
            "participants": [{"name": p} for p in participants],
            "messages": messages,
            "title": title,
            "thread_path": f"inbox/{slug}",
        }
        with open(os.path.join(folder, "message_1.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _run(self, create_new=False, dry_run=False):
        files = meta.find_message_files(self.inbox)
        parsed, group_count = meta.parse_threads(meta.group_files_by_thread(files))
        self_norm = meta.detect_self(parsed)
        threads = meta.aggregate_threads(parsed, self_norm)
        stats, _ = meta.apply(threads, group_count, self.PLATFORM,
                              create_new=create_new, dry_run=dry_run)
        return stats

    def test_one_to_one_two_way_ignores_groups_and_one_sided_and_fixes_mojibake(self):
        # A seeded contact carries the PROPER accents. The export's participant name
        # arrives mojibake'd (UTF-8 bytes mis-read as Latin-1) and must be fixed to
        # match. 'Anna Söderström' -> 'Anna SÃ¶derstrÃ¶m' in the raw export.
        proper = "Anna Söderström"
        mojibake = proper.encode("utf-8").decode("latin-1")
        self.assertNotEqual(db.normalize_name(proper), db.normalize_name(mojibake))

        conn = db.get_db(self.dbpath)
        anna = db.create_contact(conn, proper, source="linkedin")
        conn.commit()
        conn.close()

        early = datetime.datetime(2026, 4, 30, 9, 0, 0, tzinfo=datetime.timezone.utc)
        last = datetime.datetime(2026, 5, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)

        # 1:1 two-way with Anna (each side sends ≥1) -> counts (=2), accented match
        self._write_thread("annasoderstrom_abc", [mojibake, self.SELF], mojibake, [
            self._msg(self.SELF, early),
            self._msg(mojibake, last),
        ])
        # group thread (3 participants) -> ignored entirely
        self._write_thread("groupchat_xyz", ["Bob Boo", "Cara Coo", self.SELF],
                           "Group Chat", [self._msg("Bob Boo", last) for _ in range(20)])
        # one-sided 1:1 (only the other person sends) -> skipped, never created
        self._write_thread("carlcold_def", ["Carl Cold", self.SELF], "Carl Cold", [
            self._msg("Carl Cold", last),
            self._msg("Carl Cold", last),
        ])

        stats = self._run(create_new=False, dry_run=False)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["skipped_group"], 1)
        self.assertEqual(stats["skipped_one_sided"], 1)
        self.assertEqual(stats["contacts_touched"], 1)

        conn = db.get_db(self.dbpath)
        row = db.get_contact(conn, anna)
        # the one-sided chat created/updated nothing
        self.assertEqual(len(db.find_exact(conn, "Carl Cold")), 0)
        conn.close()
        # mojibake fix let the accented name match -> messenger stats landed
        self.assertEqual(row["msgr_count"], 2)              # both sides counted
        self.assertEqual(row["msgr_last"], self.ANNA_LAST)


class TestSeedCsv(unittest.TestCase):
    def test_reader_skips_preamble_and_blanks(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "Connections.csv")
        with open(p, "w") as fh:
            fh.write('Notes:\n"preamble"\n\nFirst Name,Last Name,Company,Position\n'
                     'Ada,Lovelace,Engines,Mathematician\n,,,\nAlan,Turing,GCHQ,Cryptographer\n')
        rows = [r for r in sc._make_reader(p)]
        names = [(r.get("First Name"), r.get("Last Name")) for r in rows]
        self.assertIn(("Ada", "Lovelace"), names)
        self.assertIn(("Alan", "Turing"), names)


class TestContactsImport(unittest.TestCase):
    """Apple Contacts .vcf import: enrich existing contacts and seed new ones from
    the address book (names, phones, emails, company/title).

    Builds a synthetic Contacts.vcf in a temp dir and runs the importer against a
    temp crm.db (the real database is never touched — db.DB_PATH and EXCLUDE_FILE
    are patched for the duration of the test and restored afterwards)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_vcf_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        # apply() calls db.get_db() with no path -> point DB_PATH at the temp db
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        # isolate the exclude list from the real one
        self._orig_exclude = ci.EXCLUDE_FILE
        ci.EXCLUDE_FILE = os.path.join(self.dir, "exclude_list.txt")
        self.vcf_path = os.path.join(self.dir, "Contacts.vcf")

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        ci.EXCLUDE_FILE = self._orig_exclude

    def _write_vcf(self, body):
        with open(self.vcf_path, "w", encoding="utf-8") as fh:
            fh.write(body)

    def _seed_existing(self):
        """Ada is already in the CRM (e.g. a LinkedIn connection); Eve is excluded."""
        conn = db.get_db(self.dbpath)
        ada = db.create_contact(conn, "Ada Lovelace", source="linkedin")
        conn.commit()
        conn.close()
        with open(ci.EXCLUDE_FILE, "w", encoding="utf-8") as fh:
            fh.write("Eve Excluded\n")
        return ada

    def test_enrich_match_create_skip(self):
        ada = self._seed_existing()
        self._write_vcf(
            "BEGIN:VCARD\r\n"
            "VERSION:3.0\r\n"
            "FN:Ada Lovelace\r\n"
            "TEL;TYPE=CELL:+46 70 174 0605\r\n"
            "EMAIL;TYPE=WORK:ada@example.com\r\n"
            "END:VCARD\r\n"
            "BEGIN:VCARD\r\n"
            "VERSION:3.0\r\n"
            "FN:Grace Hopper\r\n"
            "ORG:US Navy;Research\r\n"
            "TITLE:Rear Admiral\r\n"
            "TEL;TYPE=CELL:+1 202 555 0100\r\n"
            "EMAIL:grace@navy.example\r\n"
            "PHOTO;ENCODING=b;TYPE=JPEG:/9j/IGNOREME\r\n"
            "END:VCARD\r\n"
            "BEGIN:VCARD\r\n"
            "VERSION:3.0\r\n"
            "TEL;TYPE=HOME:+46 70 000 0000\r\n"
            "END:VCARD\r\n"
            "BEGIN:VCARD\r\n"
            "VERSION:3.0\r\n"
            "FN:Eve Excluded\r\n"
            "EMAIL:eve@example.com\r\n"
            "END:VCARD\r\n"
        )
        cards = ci.parse_vcards(self.vcf_path)
        self.assertEqual(len(cards), 4)

        before = db.get_db(self.dbpath).execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
        stats = ci.apply(cards, enrich_only=False, dry_run=False)
        conn = db.get_db(self.dbpath)
        after = conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]

        # Ada matched by name -> reported as updated, phone/email identifiers added
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["updated"], 1)
        ada_idents = {(i["kind"], i["value"]) for i in db.get_identifiers(conn, ada)}
        # Stored whole, country code and all. This used to assert the last-9
        # matching key ('701740605') because the importer stored that — which is
        # how one contact ended up with a pill per source for one number.
        self.assertIn(("phone", "+46701740605"), ada_idents)
        self.assertIn(("email", "ada@example.com"), ada_idents)

        # Grace created with her company/title and identifiers
        self.assertEqual(stats["created"], 1)
        grace_rows = db.find_exact(conn, "Grace Hopper")
        self.assertEqual(len(grace_rows), 1)
        grace = grace_rows[0]
        self.assertEqual(grace["company"], "US Navy")     # first ';'-separated ORG part
        self.assertEqual(grace["position"], "Rear Admiral")
        grace_idents = {(i["kind"], i["value"]) for i in db.get_identifiers(conn, grace["id"])}
        self.assertIn(("email", "grace@navy.example"), grace_idents)
        self.assertIn(("phone", "+12025550100"), grace_idents)

        # no-name card and excluded card both skipped
        self.assertEqual(stats["skipped_no_name"], 1)
        self.assertEqual(stats["skipped_excluded"], 1)
        self.assertEqual(len(db.find_exact(conn, "Eve Excluded")), 0)

        # exactly one net-new contact (Grace); Ada existed, Eve/no-name skipped
        self.assertEqual(after - before, 1)
        conn.close()

    def test_unfolding_and_phone_match(self):
        # A folded FN line (continuation begins with a space) must be joined, and a
        # card with no name but a known phone enriches the matching contact.
        conn = db.get_db(self.dbpath)
        bob = db.create_contact(conn, "Bob Smith", source="linkedin")
        db.add_identifier(conn, bob, "phone", "+46 70 174 0622")
        conn.commit()
        conn.close()
        self._write_vcf(
            "BEGIN:VCARD\r\n"
            "FN:Carol\r\n Carlsson\r\n"            # folded continuation line
            "EMAIL:carol@example.com\r\n"
            "END:VCARD\r\n"
            "BEGIN:VCARD\r\n"
            "FN:Bob Smith\r\n"
            "TEL;TYPE=CELL:070-174 0622\r\n"        # different format, same last-9
            "EMAIL:bob@example.com\r\n"
            "END:VCARD\r\n"
        )
        cards = ci.parse_vcards(self.vcf_path)
        self.assertEqual(cards[0].name, "Carol Carlsson")   # unfolded
        stats = ci.apply(cards, enrich_only=False, dry_run=False)
        self.assertEqual(stats["created"], 1)               # Carol is new
        self.assertEqual(stats["matched"], 1)               # Bob matched by name
        conn = db.get_db(self.dbpath)
        bob_idents = {(i["kind"], i["value"]) for i in db.get_identifiers(conn, bob)}
        self.assertIn(("email", "bob@example.com"), bob_idents)
        conn.close()

    def test_enrich_only_skips_creation(self):
        self._seed_existing()
        self._write_vcf(
            "BEGIN:VCARD\r\nFN:Ada Lovelace\r\nEMAIL:ada@example.com\r\nEND:VCARD\r\n"
            "BEGIN:VCARD\r\nFN:Grace Hopper\r\nEMAIL:grace@navy.example\r\nEND:VCARD\r\n"
        )
        cards = ci.parse_vcards(self.vcf_path)
        stats = ci.apply(cards, enrich_only=True, dry_run=False)
        self.assertEqual(stats["matched"], 1)               # Ada updated
        self.assertEqual(stats["created"], 0)               # Grace NOT created
        conn = db.get_db(self.dbpath)
        self.assertEqual(len(db.find_exact(conn, "Grace Hopper")), 0)
        conn.close()


class TestTheContactsStoreIsFoundAndItsAbsenceExplained(unittest.TestCase):
    """NC-029. Two stacked bugs, both silent.

    ADDRESSBOOK_GLOB was the single pattern `Sources/*/AddressBook-v22.abcddb`.
    The address book can also sit in a flat layout, with the store directly in
    `~/Library/Application Support/AddressBook/AddressBook-v22.abcddb` and a
    `Sources/` folder whose listing macOS denies.

    So the store could sit in a layout the pattern never covered, AND the Sources/
    listing could be hard-denied by TCC (the macOS privacy layer) — no dialog,
    no error, just an empty glob. load_addressbook() reported neither: both the
    iMessage and the WhatsApp importer printed "0 phone/email -> name mappings"
    and resolved every handle against an empty mapping, which looks exactly like
    a Mac with no contacts.

    The gate is on the reasons, because a bare 0 was the whole defect."""

    def _store(self, root, people=()):
        os.makedirs(root, exist_ok=True)
        p = os.path.join(root, "AddressBook-v22.abcddb")
        c = sqlite3.connect(p)
        c.executescript("CREATE TABLE ZABCDRECORD(Z_PK,ZFIRSTNAME,ZLASTNAME);"
                        "CREATE TABLE ZABCDPHONENUMBER(ZOWNER,ZFULLNUMBER);"
                        "CREATE TABLE ZABCDEMAILADDRESS(ZOWNER,ZADDRESS);")
        for i, (first, last, phone) in enumerate(people, start=1):
            c.execute("INSERT INTO ZABCDRECORD VALUES (?,?,?)", (i, first, last))
            c.execute("INSERT INTO ZABCDPHONENUMBER VALUES (?,?)", (i, phone))
        c.commit()
        c.close()
        return p

    def test_both_store_layouts_are_found(self):
        d = tempfile.mkdtemp()
        # The flat layout, which the old single pattern missed entirely.
        self._store(d, [("Ada", "Lovelace", "+46 70 174 06 05")])
        self.assertEqual([os.path.relpath(p, d) for p in im.addressbook_paths(d)],
                         ["AddressBook-v22.abcddb"])
        # The per-account layout, which it did cover — both must work at once,
        # because a Mac can have both and load_addressbook merges them.
        self._store(os.path.join(d, "Sources", "ABCD-1234"),
                    [("Grace", "Hopper", "+46 70 174 06 21")])
        self.assertEqual(len(im.addressbook_paths(d)), 2)
        mapping, why = im.load_addressbook_report(d)
        self.assertEqual(why, "")
        self.assertEqual(sorted(mapping.values()), ["Ada Lovelace", "Grace Hopper"])

    def test_an_empty_mapping_always_says_which_of_the_four_reasons_it_is(self):
        d = tempfile.mkdtemp()
        # 1. nothing there at all
        self.assertIn("no macOS Contacts data",
                      im.addressbook_reason(os.path.join(d, "absent")))
        # 2. the folder exists but holds no store
        os.makedirs(os.path.join(d, "bare"))
        self.assertIn("holds no address book file",
                      im.addressbook_reason(os.path.join(d, "bare")))
        # 3. a store we located but could not open — the common case, and the
        #    only one whose message must tell the user what to do about it
        denied = im.addressbook_reason(d, found=1, opened=0)
        self.assertIn("blocked us from opening", denied)
        self.assertIn("sync helper", denied)
        # 4. a store that opened fine and is genuinely empty. NOT a permission
        #    problem, so it must not send the user off to install anything.
        self._store(os.path.join(d, "real"))
        mapping, why = im.load_addressbook_report(os.path.join(d, "real"))
        self.assertEqual(mapping, {})
        self.assertIn("no phone numbers or email addresses", why)
        self.assertNotIn("sync helper", why)

    def test_a_populated_store_reports_no_reason_at_all(self):
        d = tempfile.mkdtemp()
        self._store(d, [("Ada", "Lovelace", "+46 70 174 06 05")])
        mapping, why = im.load_addressbook_report(d)
        self.assertEqual(why, "", "a working store must not carry an explanation")
        self.assertTrue(mapping)

    def test_both_importers_print_the_reason_and_not_a_bare_zero(self):
        """The fix is worthless if the callers keep printing len() alone — that
        was the defect, not the missing function."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("imessage_import.py", "whatsapp_import.py"):
            with self.subTest(module=name):
                with io.open(os.path.join(here, "crm", name), encoding="utf-8") as fh:
                    src = fh.read()
                self.assertIn("load_addressbook_report()", src,
                              f"{name} still calls load_addressbook() and cannot "
                              f"know why the mapping is empty")
                # Anchored on the format string, not on the words: both modules
                # describe the AddressBook step in prose above the code, and a
                # check on the words would match a docstring and pass against
                # a caller that still printed a bare count.
                stmt = 'phone/email \u2192 name mappings"'
                self.assertEqual(src.count(stmt), 1, f"{name}: expected one print")
                i = src.index(stmt)
                self.assertIn("why", src[i:i + 120],
                              f"{name} prints the count without the reason")


class TestTheThreeDroppedRowsAreGoneButNotTheirCode(unittest.TestCase):
    """NC-015. Telegram, Messenger and Instagram each had a row on the Data page
    and none of the three was finished enough to import anything useful.

    All three rows were dropped rather than finishing one.
    Messenger is why it was all three: default end-to-end encryption keeps recent
    chats out of the standard Meta export altogether, and what remains needs a
    separate PIN-protected flow `meta_import.py` does not know.

    Two halves to get right, and the second is the easy one to lose. The rows go,
    including every satellite that existed only to serve them. The CODE stays —
    both scripts still run from the command line, and so do the archive channels,
    the count columns and the identifier kinds, because a person's Instagram
    handle is a real field you type in by hand and anything an earlier CLI run
    archived must keep counting."""

    def setUp(self):
        import importers
        self.importers = importers
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(here, "crm", "importers.py"), encoding="utf-8") as fh:
            self.src = fh.read()
        with io.open(os.path.join(here, "crm", "api_import.py"), encoding="utf-8") as fh:
            self.api = fh.read()

    def test_none_of_the_three_is_a_source_any_more(self):
        for key in ("telegram", "messenger", "instagram"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.importers.REGISTRY)

    def test_nothing_is_left_pointing_at_a_row_that_does_not_exist(self):
        """Each row had satellites: an argv builder, a Downloads/Desktop glob, a
        preview-count branch, a native picker prompt, an "open Meta's export
        page" button. A leftover glob is the worst of them — it would put a
        finding on the page for a source with no spec to render it."""
        for dead in ("_cmd_telegram", "_cmd_messenger", "_cmd_instagram",
                     "Telegram Desktop", "accountscenter.facebook.com",
                     "accountscenter.instagram.com"):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, self.src)
        for dead in ("result.json from Telegram export", "Select Meta export folder"):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, self.api)
        # And no finding for them can be produced at all.
        for key in ("telegram", "messenger", "instagram"):
            self.assertEqual(self.importers._quick_count(key, "/nonexistent"), 0)

    def test_both_importers_still_run_from_the_command_line(self):
        """The capability was not deleted, its row was. A future reader finding
        these files with no row must be told which, or they will delete them."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("meta_import.py", "telegram_import.py"):
            with self.subTest(script=name):
                path = os.path.join(here, "crm", name)
                self.assertTrue(os.path.exists(path), f"{name} was deleted")
                with io.open(path, encoding="utf-8") as fh:
                    head = fh.read(1200)
                self.assertIn("NO LONGER A ROW", head,
                              f"{name} does not say its row was dropped")
                self.assertIn("NC-015", head)

    def test_the_channels_and_columns_outlive_the_rows(self):
        """Deleting these would have been a migration, and a destructive one: a
        contact's Instagram handle is typed in by hand, and messages an earlier
        CLI run archived still render in a timeline and still count toward
        coverage. So the read side must keep reporting all three channels even
        though nothing on the page can import them any more."""
        from tests.helpers import app
        path = os.path.join(tempfile.mkdtemp(), "crm.db")
        db.init_db(path)
        conn = db.get_db(path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
            coverage = app.api_contacts._channel_coverage(conn)
        finally:
            conn.close()
        for col in ("tg_count", "msgr_count", "ig_count"):
            self.assertIn(col, cols, f"{col} was dropped along with the row")
        for channel in ("telegram", "messenger", "instagram"):
            self.assertIn(channel, coverage,
                          f"coverage stopped counting {channel}, so anything "
                          f"an earlier CLI import archived became invisible")


class TestARowThatCannotWorkYetSaysSoInsteadOfFailing(unittest.TestCase):
    """NC-006. `merge_scraped_facts.py` has no built-in default for `IMAC`. The
    "LinkedIn profile facts" row renders on every install, so an unconfigured
    install must show the source as not set up rather than a live button.

    **The absence of a default is what makes that possible**: with one,
    nothing can tell "configured" from "never touched", so the row cannot know to
    show itself as not set up. So the rule is no default, plus a
    general `ImporterSpec.needs` contract — the next source with a prerequisite
    gets this for free rather than inventing it again.

    The row still RENDERS. Hiding it is how a user concludes the capability does
    not exist; `linkedin_facts` is deliberately kept as a row on the new
    Data page for the same reason (NC-236: the old page's RETIRED set is gone)."""

    def setUp(self):
        import importers
        self.importers = importers
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(here, "crm", "merge_scraped_facts.py"),
                     encoding="utf-8") as fh:
            self.script = fh.read()
        import settings as settings_module
        self.settings = settings_module

    def test_the_scraper_host_has_no_default(self):
        self.assertIn('IMAC = os.environ.get("IMAC", "").strip()', self.script,
                      "IMAC has a default, so every install thinks it is "
                      "configured and the button fails naming a machine the "
                      "user never set")
        # Whole file, prose included: a usage example in the docstring that tells
        # the reader to ssh to a real machine is a copy-pasteable instruction,
        # so a check on code alone would pass while that line was there.
        #
        # Shape, not one name (NC-138). Asserting the absence of one literal
        # hostname would miss any OTHER real host.
        import re as _re
        PLACEHOLDER = _re.compile(r"you|your|someone|user|example|host|<|\{")
        for m in _re.finditer(r"[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*",
                              self.script):
            token = m.group(0)
            with self.subTest(token=token):
                self.assertRegex(
                    token, PLACEHOLDER,
                    f"{token!r} looks like a real account on a real machine. "
                    "A name in a usage example is a copy-pasteable instruction "
                    "pointing at somebody's private network")

    def test_it_is_a_setting_so_it_can_actually_be_set(self):
        """No default plus no field is worse than the bug: the row would be off
        forever. Both keys must be in MANAGED_KEYS, and empty."""
        for key in ("IMAC", "KEY"):
            with self.subTest(key=key):
                self.assertIn(key, self.settings.MANAGED_KEYS)
                self.assertEqual(self.settings.MANAGED_KEYS[key]["default"], "")

    def test_the_registry_declares_the_prerequisite_and_whether_it_is_met(self):
        spec = self.importers.REGISTRY["linkedin_facts"]
        self.assertEqual(spec.needs, "IMAC")
        by_key = {s["key"]: s for s in self.importers.list_sources()}
        row = by_key["linkedin_facts"]
        self.assertEqual(row["needs"], "IMAC")
        # Read at call time, so setting it in Settings makes the row live without
        # a restart — the same rule import_timeout_sec() follows.
        old = os.environ.pop("IMAC", None)
        try:
            self.assertFalse(
                {s["key"]: s for s in self.importers.list_sources()}
                ["linkedin_facts"]["configured"])
            os.environ["IMAC"] = "someone@scraper-host"
            self.assertTrue(
                {s["key"]: s for s in self.importers.list_sources()}
                ["linkedin_facts"]["configured"])
            # Whitespace is not configuration.
            os.environ["IMAC"] = "   "
            self.assertFalse(
                {s["key"]: s for s in self.importers.list_sources()}
                ["linkedin_facts"]["configured"])
        finally:
            os.environ.pop("IMAC", None)
            if old is not None:
                os.environ["IMAC"] = old

    def test_a_source_the_user_picks_a_file_for_never_needs_a_setting(self):
        """`needs` must stay opt-in: a blanket prerequisite would switch off the
        whole Data page on a fresh install.

        NC-144. This is a property, not a list of sources: a source the user
        hands a FILE to cannot have a prerequisite — the file is the input — so
        those must always be runnable out of the box. A list of "sources that
        declare no needs" would freeze an accident into a rule, and would block
        a source that does need something (Gmail with GMAIL_ACCOUNTS empty, or
        facts_llm with no key; NC-116). The exact per-source classification
        lives in tests/test_sources_declare_what_they_need.py, which fails when
        a new source is added without deciding."""
        picky = [s for s in self.importers.list_sources()
                 if s["kind"] in ("file", "folder")]
        self.assertTrue(picky, "no file-picking sources found at all")
        for s in picky:
            with self.subTest(key=s["key"]):
                self.assertEqual(
                    s["needs"], "",
                    "a source the user picks a file for has its input in hand; "
                    "a prerequisite here would disable a row that works")
                self.assertTrue(s["configured"])

    def test_a_fresh_install_still_has_plenty_it_can_run(self):
        """The other half of the same worry, stated as the thing actually feared:
        an install with no settings at all must not present a dead page."""
        import os as _os
        from unittest import mock as _mock
        with _mock.patch.dict(_os.environ, {}, clear=True):
            rows = self.importers.list_sources()
            live = [s for s in rows if s["configured"]]
        self.assertGreaterEqual(
            len(live), len(rows) - 6,
            f"only {len(live)} of {len(rows)} sources are usable with no "
            "settings — a prerequisite has been added too broadly")

    def test_the_row_reports_it_and_the_buttons_refuse(self):
        """NC-236: the old page drew an unconfigured row as "Not set up" with Run
        and Preview disabled (and ranked "Not set up" above "Not collecting" above
        "Synced", so the worst true fact won). The new page takes the stronger
        route: the server's row for the LinkedIn profile facts source offers NO
        import button at all until the scraper machine is set, only the two boxes
        that set it. Checked on the answer the page is drawn from."""
        import api_ledger
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="crm_test_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "crm.db")
        from tests.helpers import db as _db
        _db.init_db(path)
        conn = _db.get_db(path)
        self.addCleanup(conn.close)

        def facts_acts():
            out = api_ledger.handle_data_ledger(conn, {})
            row = next(r for r in out["rows"] if r["key"] == "linkedin_facts")
            return [a["kind"] for a in row["detail"]["acts"]]

        old = os.environ.pop("IMAC", None)
        try:
            self.assertNotIn("import", facts_acts(),
                             "an unconfigured source still has a live Import "
                             "button that can only fail naming a machine that "
                             "is not set")
            self.assertIn("text", facts_acts(),
                          "no box to set the machine, so the row is off forever")
            os.environ["IMAC"] = "someone@scraper-host"
            self.assertIn("import", facts_acts(),
                          "setting the machine does not make the row live")
        finally:
            os.environ.pop("IMAC", None)
            if old is not None:
                os.environ["IMAC"] = old

    def test_only_the_pull_needs_the_host_not_the_merge(self):
        """`--source` is a file you already have. Requiring IMAC for it would make
        the merge half unusable on any Mac, which is a bigger bug than the one
        being fixed."""
        self.assertIn("if args.pull and not IMAC:", self.script)
        # NC-170: the `if` suite, bounded by indentation, which is what Python
        # already uses to say where a block ends.
        msg = py_block(self.script, "if args.pull and not IMAC:")
        self.assertIn("--source", msg, "the refusal does not mention the way round it")
        self.assertIn("Settings", msg, "the refusal does not say where to set it")
