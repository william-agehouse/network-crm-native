"""Contact photos: local storage, the source-priority guard (invariant 5), vCard PHOTO import, WhatsApp avatars and Google contact photos."""

import base64
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, ci, db, importers,
)
# AFTER tests.helpers, never before: crm modules capture data/ paths at import
# time, so importing one first makes the whole suite read and write the real
# data dir while still passing (tests/helpers.py; tests/test_bootstrap.py).
import photos  # noqa: E402


class TestPhotos(unittest.TestCase):
    """Local photo storage: source-priority guard, vCard PHOTO import, the
    WhatsApp Media/Profile importer's name/phone/lid matching, merge handoff,
    and the manual upload/delete API handlers. All against temp dirs/DBs."""

    JPEG = b"\xff\xd8\xff\xe0" + b"J" * 300     # valid magic, junk body (>MIN_BYTES)
    JPEG2 = b"\xff\xd8\xff\xe1" + b"K" * 300
    PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 300

    def setUp(self):
        import photos as ph
        import whatsapp_photos as wap
        self.ph, self.wap = ph, wap
        self.dir = tempfile.mkdtemp(prefix="crm_photos_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        self._orig_photos_dir = ph.PHOTOS_DIR
        ph.PHOTOS_DIR = os.path.join(self.dir, "photos")
        self._orig_exclude = wap.EXCLUDE_FILE
        wap.EXCLUDE_FILE = os.path.join(self.dir, "exclude_list.txt")
        self.conn = db.get_db(self.dbpath)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        self.ph.PHOTOS_DIR = self._orig_photos_dir
        self.wap.EXCLUDE_FILE = self._orig_exclude
        self.conn.close()

    def _contact(self, name, phone=None):
        cid = db.create_contact(self.conn, name)
        if phone:
            db.add_identifier(self.conn, cid, "phone", phone)
        self.conn.commit()
        return cid

    def test_priority_guard_and_url(self):
        cid = self._contact("Ada Lovelace")
        row = db.get_contact(self.conn, cid)
        self.assertEqual(self.ph.photo_url(row), "")

        self.assertTrue(self.ph.save_photo(self.conn, cid, self.JPEG, "whatsapp"))
        self.assertTrue(self.ph.save_photo(self.conn, cid, self.JPEG2, "contacts"))   # upgrade
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.JPEG, "whatsapp"))   # downgrade blocked
        self.assertTrue(self.ph.save_photo(self.conn, cid, self.PNG, "contacts"))     # equal rank refreshes
        self.assertTrue(self.ph.save_photo(self.conn, cid, self.JPEG, "manual"))      # manual beats all
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.JPEG2, "contacts"))  # nothing beats manual

        row = db.get_contact(self.conn, cid)
        self.assertEqual(row["photo_source"], "manual")
        self.assertIn(f"/api/photo/{cid}?v=", self.ph.photo_url(row))
        self.assertEqual(self.ph.read_photo(cid)[:3], b"\xff\xd8\xff")

        # junk bytes / tiny blobs are rejected outright
        self.assertFalse(self.ph.save_photo(self.conn, cid, b"not an image" * 20, "manual"))
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.JPEG[:50], "manual"))

    def test_linkedin_outranks_the_address_book_and_the_chat_avatars(self):
        """The photo-priority reordering, in the direction it actually matters.

        WHY THIS IS ITS OWN TEST. The old order was
        manual > contacts > whatsapp/google > linkedin, and the reason it was wrong
        is not visible from the code: a LinkedIn photo is the portrait someone chose
        to be recognised by professionally, while a chat avatar is a holiday snap or
        their dog, and an address-book photo is often a picture you took of them at
        a party. In a tool you open to remember who someone is before you talk to
        them, the headshot is the one that identifies them, so it ranks highest.

        A one-line dict is very easy to "tidy" back into alphabetical or
        historical order, and nothing else in the suite would notice — the two
        existing photo tests only assert that a source is RECORDED, never that one
        beats another in this direction.
        """
        cid = self._contact("Ada Lovelace")
        # A chat avatar arrives first, then the address book, then LinkedIn.
        self.assertTrue(self.ph.save_photo(self.conn, cid, self.JPEG, "whatsapp"))
        self.assertTrue(self.ph.save_photo(self.conn, cid, self.JPEG2, "contacts"))
        self.assertTrue(self.ph.save_photo(self.conn, cid, self.PNG, "linkedin"))
        self.assertEqual(db.get_contact(self.conn, cid)["photo_source"], "linkedin")

        # Now nothing automatic may take it back.
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.JPEG, "contacts"),
                         "the address book must not overwrite a LinkedIn portrait")
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.JPEG, "whatsapp"),
                         "a chat avatar must not overwrite a LinkedIn portrait")
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.JPEG, "google"),
                         "google must not overwrite a LinkedIn portrait")
        self.assertEqual(db.get_contact(self.conn, cid)["photo_source"], "linkedin")

        # But a hand-set photo still wins — invariant 5 is unchanged.
        self.assertTrue(self.ph.save_photo(self.conn, cid, self.JPEG, "manual"),
                        "manual must still beat every import, including LinkedIn")
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.JPEG2, "linkedin"),
                         "LinkedIn must not overrule a photo you set by hand")
        self.assertEqual(db.get_contact(self.conn, cid)["photo_source"], "manual")

        # And the ranks themselves, so a reorder fails here and not somewhere subtle.
        r = self.ph.source_rank
        self.assertGreater(r("manual"), r("linkedin"))
        self.assertGreater(r("linkedin"), r("contacts"))
        self.assertGreater(r("contacts"), r("whatsapp"))
        self.assertEqual(r("whatsapp"), r("google"))

    def test_delete_photo(self):
        cid = self._contact("Grace Hopper")
        self.ph.save_photo(self.conn, cid, self.JPEG, "manual")
        self.ph.delete_photo(self.conn, cid)
        row = db.get_contact(self.conn, cid)
        self.assertEqual((row["photo"], row["photo_source"]), ("", ""))
        self.assertIsNone(self.ph.read_photo(cid))

    def test_vcard_photo_import(self):
        orig_ci_exclude = ci.EXCLUDE_FILE
        ci.EXCLUDE_FILE = self.wap.EXCLUDE_FILE
        try:
            vcf = os.path.join(self.dir, "Contacts.vcf")
            b64 = base64.b64encode(self.JPEG).decode()
            # fold the base64 like Apple does (continuation lines with a leading space)
            folded = b64[:40] + "\r\n " + b64[40:]
            with open(vcf, "w", encoding="utf-8") as fh:
                fh.write(
                    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Mary Shelley\r\n"
                    f"PHOTO;ENCODING=b;TYPE=JPEG:{folded}\r\n"
                    "END:VCARD\r\n"
                )
            cards = ci.parse_vcards(vcf)
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].photo[:3], b"\xff\xd8\xff")
            stats = ci.apply(cards, enrich_only=False, dry_run=False)
            self.assertEqual(stats["photos"], 1)
            conn = db.get_db(self.dbpath)
            row = conn.execute("SELECT * FROM contacts WHERE name='Mary Shelley'").fetchone()
            conn.close()
            self.assertEqual(row["photo_source"], "contacts")
            self.assertIsNotNone(self.ph.read_photo(row["id"]))
        finally:
            ci.EXCLUDE_FILE = orig_ci_exclude

    def _make_profile_dir(self):
        """Synthetic Media/Profile + ChatStorage.sqlite with the real schema subset."""
        profile = os.path.join(self.dir, "Profile")
        os.makedirs(profile)
        cs = os.path.join(self.dir, "ChatStorage.sqlite")
        conn = sqlite3.connect(cs)
        conn.execute("CREATE TABLE ZWACHATSESSION (ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER)")
        conn.executemany(
            "INSERT INTO ZWACHATSESSION VALUES (?, ?, ?)",
            [
                ("100000000000001@lid", "Ada Lovelace", 0),        # lid → name match
                ("120000000000000001@g.us", "Some Group", 1),      # group → skipped
                ("46701740699@s.whatsapp.net", "+46 70 174 06 99", 0),  # placeholder name → phone path
            ],
        )
        conn.commit()
        conn.close()
        def put(name, data):
            with open(os.path.join(profile, name), "wb") as fh:
                fh.write(data)
        put("100000000000001-111.thumb", self.JPEG)     # Ada via session name (lid)
        put("100000000000001-111.jpg", self.JPEG2)      # .jpg beats .thumb
        put("46701740605-222.jpg", self.JPEG)           # phone → identifier match, no session
        put("120000000000000001-333.jpg", self.JPEG)    # group avatar → skipped
        put("100000000000002-444.jpg", self.JPEG)       # unknown lid → skipped
        return profile, cs

    def test_whatsapp_photos_matching(self):
        ada = self._contact("Ada Lovelace")
        bob = self._contact("Bob Byphone", phone="46701740605")
        profile, cs = self._make_profile_dir()

        stats = self.wap.run(profile, cs, dry_run=False)
        self.assertEqual(stats["matched_name"], 1)
        self.assertEqual(stats["matched_phone"], 1)
        self.assertEqual(stats["saved"], 2)
        self.assertEqual(stats["unmatched"], 2)  # group + unknown lid

        # Ada got the .jpg (not the .thumb) — check the distinctive 4th byte
        self.assertEqual(self.ph.read_photo(ada)[:4], self.JPEG2[:4])
        self.assertIsNotNone(self.ph.read_photo(bob))

        # lids are never phone-matched: a 15-digit prefix must not hit find_by_handle
        self.assertIsNone(db.find_by_handle(db.get_db(self.dbpath), "100000000000001"))

    def test_whatsapp_photos_respects_exclude_and_priority(self):
        self._contact("Ada Lovelace")
        with open(self.wap.EXCLUDE_FILE, "w", encoding="utf-8") as fh:
            fh.write("Ada Lovelace\n")
        profile, cs = self._make_profile_dir()
        stats = self.wap.run(profile, cs, dry_run=False)
        self.assertEqual(stats["matched_name"], 0)
        self.assertEqual(stats["excluded"], 1)

    def test_merge_moves_photo(self):
        keep = self._contact("Ada Lovelace")
        dup = self._contact("Ada Lovelace ⟢")
        self.ph.save_photo(self.conn, dup, self.JPEG, "contacts")
        self.conn.commit()
        db.merge_contacts(self.conn, keep, dup)
        self.conn.commit()
        row = db.get_contact(self.conn, keep)
        self.assertEqual(row["photo_source"], "contacts")
        self.assertIsNotNone(self.ph.read_photo(keep))

    def test_api_upload_and_delete(self):
        cid = self._contact("Grace Hopper")
        result = app.handle_set_photo(self.conn, cid, {"data": base64.b64encode(self.JPEG).decode()})
        self.assertIn(f"/api/photo/{cid}?v=", result["photo"])
        summary = app._contact_summary(db.get_contact(self.conn, cid))
        self.assertTrue(summary["photo"])
        # data-URI form is accepted too, and manual outranks an import that follows
        app.handle_set_photo(self.conn, cid, {"data": "data:image/jpeg;base64," + base64.b64encode(self.JPEG2).decode()})
        self.assertFalse(self.ph.save_photo(self.conn, cid, self.PNG, "contacts"))
        app.handle_delete_photo(self.conn, cid)
        self.assertEqual(app._contact_summary(db.get_contact(self.conn, cid))["photo"], "")
        with self.assertRaises(app.ApiError):
            app.handle_set_photo(self.conn, cid, {"data": "!!!not-base64"})

    def test_registry_has_whatsapp_photos(self):
        spec = importers.REGISTRY["whatsapp_photos"]
        argv = spec.build_cmd({"path": "/tmp/Profile"})
        self.assertIn("whatsapp_photos.py", argv[1])
        self.assertIn("/tmp/Profile", argv)

    def test_linkedin_location_sync_removed(self):
        # LinkedIn's portability API (domain=CONNECTIONS) does not expose a
        # per-connection Location field at all -- this feature could never
        # work, so it was removed rather than left silently writing zero
        # locations forever. Guard against it quietly coming back.
        self.assertNotIn("linkedin_location", importers.REGISTRY)
        self.assertFalse(
            os.path.exists(os.path.join(os.path.dirname(importers.__file__),
                                        "linkedin_location_sync.py")))

    def test_linkedin_registry_consolidated(self):
        # ONE import card ("LinkedIn": API connections + DMs in a single run,
        # CSV fallback when a detected Connections.csv path is passed) and ONE
        # enrich card ("LinkedIn profile facts": pull the scraper's output).
        # The old linkedin_dms / linkedin_stats / linkedin_summaries cards are
        # folded in or retired -- guard against the sprawl coming back.
        li_keys = sorted(k for k in importers.REGISTRY if k.startswith("linkedin"))
        self.assertEqual(li_keys, ["linkedin", "linkedin_facts"])

        spec = importers.REGISTRY["linkedin"]
        self.assertEqual(spec.category, "import")
        self.assertEqual(spec.kind, "none")           # one click, no file picker
        argv = spec.build_cmd({})
        self.assertIn("linkedin_import.py", argv[1])  # no flags = connections+messages
        self.assertEqual(len(argv), 2)
        csv_argv = spec.build_cmd({"path": "/tmp/Connections.csv"})
        self.assertIn("seed_from_csv.py", csv_argv[1])
        self.assertIn("/tmp/Connections.csv", csv_argv)

        facts = importers.REGISTRY["linkedin_facts"]
        self.assertEqual(facts.category, "enrich")
        facts_argv = facts.build_cmd({})
        self.assertIn("merge_scraped_facts.py", facts_argv[1])
        self.assertIn("--pull", facts_argv)

    def test_legacy_linkedin_scripts_removed(self):
        # enrich_local.py / seed_from_linkedin.py read the OLD scraper's
        # linkedin_messages.db snapshot; that history now lives in archive.db
        # (via linkedin_import.py), and each person's count is recounted from
        # it (NC-107, which also removed the never-shrink guard this used to
        # cite), so the scripts were retired with their registry cards.
        crm_dir = os.path.dirname(importers.__file__)
        for legacy in ("enrich_local.py", "seed_from_linkedin.py"):
            self.assertFalse(os.path.exists(os.path.join(crm_dir, legacy)), legacy)


class TestGooglePhotosHelpers(unittest.TestCase):
    """Pure helpers of google_photos_import — no API calls."""

    def test_real_photo_url_skips_defaults_and_sizes(self):
        import google_photos_import as gp
        self.assertEqual(gp.real_photo_url({"photos": [{"default": True, "url": "https://x/=s100"}]}), "")
        self.assertEqual(gp.real_photo_url({"photos": [{"url": "https://x/photo=s100"}]}),
                         "https://x/photo=s512")
        self.assertEqual(gp.real_photo_url({"photos": [{"url": "https://x/photo"}]}),
                         "https://x/photo=s512")
        self.assertEqual(gp.real_photo_url({}), "")

    def test_person_identity(self):
        import google_photos_import as gp
        name, emails, phones = gp.person_identity({
            "names": [{"displayName": "Ada Lovelace"}],
            "emailAddresses": [{"value": "Ada@Example.com"}],
            "phoneNumbers": [{"value": "+46 70 174 06 05"}],
        })
        self.assertEqual(name, "Ada Lovelace")
        self.assertEqual(emails, ["ada@example.com"])
        self.assertEqual(phones, ["+46 70 174 06 05"])

    def test_match_contact_priority(self):
        import google_photos_import as gp
        dirpath = tempfile.mkdtemp(prefix="crm_gp_")
        dbpath = os.path.join(dirpath, "crm.db")
        db.init_db(dbpath)
        conn = db.get_db(dbpath)
        by_mail = db.create_contact(conn, "Mail Person")
        db.add_identifier(conn, by_mail, "email", "mail@example.com")
        by_name = db.create_contact(conn, "Name Person")
        conn.commit()
        self.assertEqual(gp.match_contact(conn, "Wrong Name", ["mail@example.com"], []), by_mail)
        self.assertEqual(gp.match_contact(conn, "Name Person", [], []), by_name)
        self.assertIsNone(gp.match_contact(conn, "Nobody", ["no@example.com"], []))
        conn.close()


class TestOneFaceOnePerson(Base):
    """The logged-in viewer's own LinkedIn portrait turned up on other people's cards.

    Cause: scrape_linkedin_profiles.extract_photo_url regex-searched the WHOLE
    profile page for 'profile-displayphoto-*', and LinkedIn puts the logged-in
    viewer's avatar in the global nav under that same pattern. Every profile whose
    subject had no photo therefore matched the viewer's face first and stored it as
    that person's portrait.

    Two fixes, both tested here: the scraper no longer looks at the page chrome, and
    photos.save_photo refuses an image that already belongs to somebody else — so
    any OTHER importer making the same mistake is caught too.
    """

    # 3-byte JPEG magic, padded past photos.MIN_BYTES, with an EOI marker.
    IMG_A = b"\xff\xd8\xff" + b"\x11" * 6000 + b"\xff\xd9"
    IMG_B = b"\xff\xd8\xff" + b"\x22" * 6000 + b"\xff\xd9"

    def test_an_automatic_source_cannot_put_one_image_on_two_people(self):
        a = db.create_contact(self.conn, "Person A")
        b = db.create_contact(self.conn, "Person B")
        self.conn.commit()
        self.assertTrue(photos.save_photo(self.conn, a, self.IMG_A, "linkedin"))
        self.assertFalse(photos.save_photo(self.conn, b, self.IMG_A, "linkedin"),
                         "the same face must not land on a second contact")
        self.conn.commit()
        rows = {r["id"]: r["photo"] for r in
                self.conn.execute("SELECT id, photo FROM contacts")}
        self.assertTrue(rows[a])
        self.assertFalse(rows[b], "B must be left with no photo, not the wrong one")

    def test_a_different_image_is_still_accepted(self):
        """The guard must not become a one-photo-per-database rule."""
        a = db.create_contact(self.conn, "Person A")
        b = db.create_contact(self.conn, "Person B")
        self.conn.commit()
        self.assertTrue(photos.save_photo(self.conn, a, self.IMG_A, "linkedin"))
        self.assertTrue(photos.save_photo(self.conn, b, self.IMG_B, "linkedin"))

    def test_a_manual_photo_is_exempt(self):
        """If the owner deliberately sets one picture on two people, that is their call."""
        a = db.create_contact(self.conn, "Person A")
        b = db.create_contact(self.conn, "Person B")
        self.conn.commit()
        self.assertTrue(photos.save_photo(self.conn, a, self.IMG_A, "manual"))
        self.assertTrue(photos.save_photo(self.conn, b, self.IMG_A, "manual"))

    def test_re_saving_the_same_image_to_the_SAME_contact_still_works(self):
        """An avatar refresh must not be mistaken for a collision with itself."""
        a = db.create_contact(self.conn, "Person A")
        self.conn.commit()
        self.assertTrue(photos.save_photo(self.conn, a, self.IMG_A, "linkedin"))
        self.assertTrue(photos.save_photo(self.conn, a, self.IMG_A, "linkedin"))


class TestScraperIgnoresTheViewersOwnAvatar(Base):
    """The root cause, at the source."""

    MINE = ("https://media.licdn.com/dms/image/v2/AAA/"
            "profile-displayphoto-shrink_400_400/MY-OWN-FACE?e=1")
    HERS = ("https://media.licdn.com/dms/image/v2/BBB/"
            "profile-displayphoto-shrink_400_400/HER-FACE?e=1")
    NAV = '<nav class="global-nav"><img src="%s"></nav>' % MINE

    def test_the_subjects_photo_is_still_found(self):
        import scrape_linkedin_profiles as s
        got = s.extract_photo_url(self.NAV + '<main><img src="%s"></main>' % self.HERS)
        self.assertIn("HER-FACE", got)
        self.assertNotIn("MY-OWN-FACE", got)

    def test_a_profile_with_no_photo_returns_nothing_not_the_viewers_face(self):
        """This is the exact input that produced the bug: profiles like this."""
        import scrape_linkedin_profiles as s
        got = s.extract_photo_url(self.NAV + "<main>this person has no photo</main>")
        self.assertEqual(got, "", "a missing photo must stay missing")

    def test_a_page_with_no_landmark_still_parses(self):
        """No <main> and no nav: search the whole document, as before. A miss is a
        much smaller problem than the wrong face, and save_photo guards the rest."""
        import scrape_linkedin_profiles as s
        self.assertIn("HER-FACE", s.extract_photo_url('<img src="%s">' % self.HERS))
