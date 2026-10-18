"""Apple Contacts importing itself, with no button (NC-029).

The address book is the one source that only ever moved when a human exported a
`.vcf` and clicked. Every source is meant to sync on its own, with nothing manual.
That has four separable parts and each can be wrong on its own:

  1. the FDA helper copies AddressBook-v22.abcddb into our own container;
  2. addressbook_import.py reads THAT COPY and never the protected original;
  3. the registry's one Apple Contacts row dispatches on what the path is;
  4. the autosync loop and the page both know the copy exists.

Nothing here can prove part 1: that needs the helper installed and Full Disk
Access granted on a real Mac, and again after every change to the binary, since
changing it changes its code hash. What is gated here is that the app and the
helper agree on the path, so a working copy lands exactly where the importer
looks.
"""
import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "crm"))

import addressbook_import as ab  # noqa: E402
import importers  # noqa: E402


def _read_crm(name):
    """One crm/ source file as text, for the gates that assert on code rather
    than behaviour — the Swift/Python contracts have no runtime to test."""
    with io.open(os.path.join(REPO, "crm", name), encoding="utf-8") as fh:
        return fh.read()


def make_store(root, people, cols=("ZFIRSTNAME", "ZLASTNAME", "ZORGANIZATION", "ZJOBTITLE")):
    """An AddressBook-v22.abcddb shaped like the real one.

    Synthetic because the real file cannot be opened from a test — reading it
    needs the very permission this whole design exists to avoid.
    """
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "AddressBook-v22.abcddb")
    c = sqlite3.connect(path)
    c.execute(f"CREATE TABLE ZABCDRECORD(Z_PK INTEGER PRIMARY KEY, {', '.join(cols)})")
    c.execute("CREATE TABLE ZABCDPHONENUMBER(ZOWNER, ZFULLNUMBER)")
    c.execute("CREATE TABLE ZABCDEMAILADDRESS(ZOWNER, ZADDRESS)")
    for pk, rec in enumerate(people, start=1):
        c.execute(f"INSERT INTO ZABCDRECORD VALUES (?, {', '.join('?' * len(cols))})",
                  [pk] + [rec.get(k) for k in cols])
        for phone in rec.get("phones", []):
            c.execute("INSERT INTO ZABCDPHONENUMBER VALUES (?,?)", (pk, phone))
        for email in rec.get("emails", []):
            c.execute("INSERT INTO ZABCDEMAILADDRESS VALUES (?,?)", (pk, email))
    c.commit()
    c.close()
    return path


class TestReadingTheAddressBook(unittest.TestCase):
    def test_a_person_becomes_the_same_record_a_vcard_would(self):
        """Reuse, not reimplementation: read_store returns contacts_import.VCard
        objects, so every matching, exclusion and photo rule stays in one place.
        A phone must arrive canonicalised and an email lower-cased for exactly
        that reason — an identifier spelled differently here than by the .vcf
        path would become a second row on the same person."""
        d = tempfile.mkdtemp()
        make_store(d, [{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace",
                        "ZORGANIZATION": "Analytical Engines", "ZJOBTITLE": "Mathematician",
                        "phones": ["+46 70 174 06 05"], "emails": ["Ada@Example.COM"]}])
        cards = ab.read_store(os.path.join(d, "AddressBook-v22.abcddb"))
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.name, "Ada Lovelace")
        self.assertEqual(card.company, "Analytical Engines")
        self.assertEqual(card.title, "Mathematician")
        self.assertEqual(card.phones, ["+46701740605"])
        self.assertEqual(card.emails, ["ada@example.com"])

    def test_a_company_card_keeps_its_name_instead_of_being_dropped(self):
        """Contacts stores a business with no first or last name — the
        organisation IS the name. Requiring a person name would silently drop
        every one of them, and the .vcf path never did (Contacts writes FN)."""
        d = tempfile.mkdtemp()
        make_store(d, [
            {"ZFIRSTNAME": None, "ZLASTNAME": None, "ZORGANIZATION": "Acme AB",
             "ZJOBTITLE": None, "emails": ["hello@acme.example"]},
            {"ZFIRSTNAME": None, "ZLASTNAME": None, "ZORGANIZATION": None, "ZJOBTITLE": None},
        ])
        cards = ab.read_store(os.path.join(d, "AddressBook-v22.abcddb"))
        self.assertEqual([c.name for c in cards], ["Acme AB"],
                         "a company card was dropped, or a truly nameless row was kept")
        # …and the organisation must not also be repeated as the company, which
        # would show "Acme AB · Acme AB" on the row.
        self.assertEqual(cards[0].company, "")

    def test_a_renamed_schema_degrades_instead_of_failing(self):
        """Column names vary by macOS version and the real file cannot be
        inspected from here to check. A release without ZORGANIZATION must lose
        the company back-fill and keep the person, not fail the import."""
        d = tempfile.mkdtemp()
        make_store(d, [{"ZFIRSTNAME": "Grace", "ZLASTNAME": "Hopper"}],
                   cols=("ZFIRSTNAME", "ZLASTNAME"))
        cards = ab.read_store(os.path.join(d, "AddressBook-v22.abcddb"))
        self.assertEqual([(c.name, c.company, c.title) for c in cards],
                         [("Grace Hopper", "", "")])

    def test_two_stores_merge_rather_than_one_winning(self):
        """A Mac can hold the flat store AND one per account, with the same
        person in both. Keeping only the first would throw away a phone number
        that exists in only one of them."""
        d = tempfile.mkdtemp()
        make_store(d, [{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace",
                        "ZORGANIZATION": "Analytical Engines", "ZJOBTITLE": None,
                        "phones": ["+46 70 174 06 05"]}])
        make_store(os.path.join(d, "Sources", "UUID-1"),
                   [{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace", "ZORGANIZATION": None,
                     "ZJOBTITLE": None, "phones": ["+46 70 174 06 77"]}])
        cards, why = ab.read_all(d)
        self.assertEqual(why, "")
        self.assertEqual(len(cards), 1, "the same person became two contacts")
        self.assertEqual(sorted(cards[0].phones), ["+46701740605", "+46701740677"])
        self.assertEqual(cards[0].company, "Analytical Engines",
                         "the store without a company blanked the one with it")

    def test_an_absent_copy_is_reported_not_raised(self):
        cards, why = ab.read_all(os.path.join(tempfile.mkdtemp(), "nothing"))
        self.assertEqual(cards, [])
        self.assertIn("no macOS Contacts data", why)


class TestItReadsTheCopyAndNeverTheProtectedOriginal(unittest.TestCase):
    """The default root decides whether this can run on a timer at all.

    Pointing it at ~/Library/Application Support/AddressBook would recreate the
    bug detect_unprotected_sources() exists to prevent: a background loop touching
    TCC-protected app data raises a consent dialog attributed to "python3", every
    cycle, for the life of the process."""

    def setUp(self):
        with io.open(os.path.join(REPO, "crm", "addressbook_import.py"),
                     encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_default_root_is_our_own_container(self):
        self.assertIn("AB_COPY_DIR = os.path.join(config.APP_SUPPORT_DIR", self.src,
                      "the default root is not inside the app's own folder")
        self.assertTrue(ab.AB_COPY_DIR.endswith(os.path.join("NetworkCRM", "addressbook")))

    def test_the_protected_path_is_never_named_in_code(self):
        """Comments and the docstring discuss it at length — that is the point of
        them. Code must not reach for it."""
        code = "\n".join(ln for ln in self.src.splitlines()
                         if not ln.lstrip().startswith("#"))
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        self.assertNotIn("Library/Application Support/AddressBook", code,
                         "the importer resolves the protected original in code, so a "
                         "background run can raise a consent dialog")


class TestTheAppAndTheHelperAgreeOnWhereTheCopyGoes(unittest.TestCase):
    """Two files, two languages, one path. They can desync: an autosync loop
    that filters for container paths the timer's own scan can never return leaves
    a source that quietly stops updating."""

    def setUp(self):
        with io.open(os.path.join(REPO, "packaging", "src", "imessage_sync.swift"),
                     encoding="utf-8") as fh:
            self.swift = fh.read()

    def source_block(self, label):
        """One `Source(...)` literal from the helper's source table.

        NC-170. A window from the label (`self.swift[i:i + 700]` or `+ 900`) is
        not a block. Two things about it are wrong and one is dangerous: it
        reaches into the NEXT source once this one grows past the number, so a
        gate on the Contacts source could start passing because of a line in the
        Calendar source below it; and it stops short of the end of this one when
        a field is added, so the gate quietly checks less than it claims. A
        Swift literal ends at its closing paren, which is what this returns.
        """
        at = self.swift.index('label: "%s"' % label)
        start = self.swift.rindex("Source(", 0, at)
        end = self.swift.index("\n    ),", start)
        return self.swift[start:end]

    def test_the_helper_copies_the_file_the_importer_looks_for(self):
        self.assertIn('label: "Contacts"', self.swift,
                      "the helper has no Contacts source, so no copy is ever made")
        self.assertIn('appendingPathComponent("addressbook"', self.swift,
                      "the helper writes its Contacts copy somewhere other than "
                      "the directory addressbook_import.py reads")
        self.assertEqual(os.path.basename(ab.AB_COPY_DIR), "addressbook")

    def test_the_helper_copies_the_wal_and_not_only_the_main_file(self):
        """The -wal can be several times the size of the store itself, and it
        holds the most recent writes. The main file alone is a snapshot missing
        most of the recent address book."""
        block = self.source_block("Contacts")
        for part in ("AddressBook-v22.abcddb-wal", "AddressBook-v22.abcddb-shm"):
            self.assertIn(part, block, f"the helper does not copy {part}")

    def test_every_source_has_a_destination_override(self):
        """`overrides` is indexed by source position, so a short array traps on
        the last source rather than failing a test.

        Counted, not matched against a literal. Asserting the exact text
        "[imessageDest, whatsappDest, contactsDest]" would break on a refactor
        that kept the property intact and would say nothing at all about a
        fourth source being added to a three-entry list — the failure this
        exists to catch."""
        self.assertIn("precondition(overrides.count == sources.count", self.swift)

        def _entries(opening):
            body = self.swift[self.swift.index(opening) + len(opening):]
            body = body[:body.index("\n]")]
            return [x.strip() for x in body.split(",") if x.strip()]

        overrides = _entries("let overrides: [URL?] = [")
        # The sources array's entries are the `Source(` constructor calls in it.
        # `let fake = Source(...)` in the self-test block is outside this slice.
        sources_body = self.swift[self.swift.index("let sources: [Source] = ["):]
        sources_body = sources_body[:sources_body.index("\n]")]
        self.assertEqual(len(overrides), sources_body.count("Source("),
                         "one destination override per source, in source order")
        self.assertGreaterEqual(len(overrides), 3, "three sources are expected")

    def test_the_contacts_source_sweeps_the_per_account_stores(self):
        """NC-043, and the reason the sweep exists at all. The helper used to copy
        only the flat "On My Mac" store. On a Mac that syncs contacts that file
        holds almost nothing while the full address book sits in the per-account
        stores, and the app reported the source healthy and current."""
        block = self.source_block("Contacts")
        self.assertIn("sweep: Sweep(", block, "the Contacts source has no sweep")
        self.assertIn('subpath: "Sources"', block)
        self.assertIn('.prefix("AddressBook-v22.abcddb")', block,
                      "the sweep must bring the -wal and -shm along too, which is "
                      "what a prefix match does and an exact name would not")

    def test_the_python_side_looks_where_the_sweep_copies_to(self):
        """The Swift and the importer are a contract. The importer already globbed
        Sources/*/ before the helper ever wrote there, which is exactly how this
        went unnoticed: the glob was right and always empty."""
        self.assertIn("Sources/*/AddressBook-v22.abcddb",
                      _read_crm("imessage_import.py"))
        # and the preview count must read the same set, or the row's number and
        # the import's number disagree
        self.assertIn("addressbook_import.store_paths", _read_crm("importers.py"))

    def test_reading_a_store_replays_the_write_ahead_log(self):
        """immutable=1 ignores the -wal outright. imessage_import.snapshot_uri
        already documents that trap (new rows go unseen); this module reproduced
        it instead of calling that function."""
        src = _read_crm("addressbook_import.py")
        self.assertIn("snapshot_uri(path)", src)
        self.assertNotIn('?immutable=1', src,
                         "read_store is back to ignoring the -wal")


class TestOneRowTwoRoutes(unittest.TestCase):
    """The registry has ONE Apple Contacts row that dispatches on the path. Two
    rows called Apple Contacts is the duplication NC-030 just removed for
    WhatsApp's photos."""

    def test_an_abcddb_path_runs_the_automatic_importer(self):
        cmd = importers.REGISTRY["contacts"].build_cmd(
            {"path": "/x/addressbook/AddressBook-v22.abcddb"})
        self.assertIn("addressbook_import.py", cmd)
        self.assertNotIn("contacts_import.py", cmd)
        # --dir, not the file: the importer merges every store under a root.
        self.assertEqual(cmd[cmd.index("--dir") + 1], "/x/addressbook")

    def test_a_vcf_path_still_runs_the_hand_export_importer(self):
        cmd = importers.REGISTRY["contacts"].build_cmd({"path": "/x/Contacts.vcf"})
        self.assertIn("contacts_import.py", cmd)
        self.assertNotIn("addressbook_import.py", cmd)

    def test_dry_run_survives_the_dispatch(self):
        cmd = importers.REGISTRY["contacts"].build_cmd(
            {"path": "/x/addressbook/AddressBook-v22.abcddb", "dry_run": True})
        self.assertIn("--dry-run", cmd)

    def test_there_is_still_exactly_one_apple_contacts_row(self):
        labels = [s.label for s in importers.REGISTRY.values() if s.category == "import"]
        self.assertEqual(labels.count("Apple Contacts"), 1)


class TestTheTimerAndThePageBothSeeTheCopy(unittest.TestCase):
    """A source present in only one of the two scans either never appears on the
    page or never runs on the timer. Both, or it is broken."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.copy = make_store(os.path.join(self.d, "addressbook"),
                               [{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace",
                                 "ZORGANIZATION": None, "ZJOBTITLE": None}])
        self._orig = importers.AB_AUTO_DB
        importers.AB_AUTO_DB = self.copy

    def tearDown(self):
        importers.AB_AUTO_DB = self._orig

    def test_the_unprotected_scan_returns_it(self):
        found = importers.detect_unprotected_sources()
        contacts = [f for f in found if f["source"] == "contacts"]
        self.assertEqual(len(contacts), 1,
                         "the timer's own scan cannot see the Contacts copy, so it "
                         "will never be imported without a click")
        self.assertEqual(contacts[0]["path"], self.copy)

    def test_the_preview_count_reads_the_address_book_not_vcard_markers(self):
        """_quick_count's contacts branch counted BEGIN:VCARD in the first 256 KB.
        Run against a SQLite file that finds zero, and the row would report an
        empty address book."""
        self.assertEqual(importers._quick_count("contacts", self.copy), 1)


class TestBirthdaysActuallyArrive(unittest.TestCase):
    """NC-041. `contact_dates` existed, the recurrence maths existed, a person's
    page rendered birthdays — and **nothing ever imported one**. Neither the
    vCard path nor this one read BDAY/ZBIRTHDAY, so the table
    held only calendar meetings and "when is X's birthday" had no answer anywhere.
    A feature whose input is never written is indistinguishable from one that
    was never built.

    The two conversions here are the parts that fail silently rather than
    loudly: a Unix-epoch reading of a Core Data timestamp shifts every birthday
    by 31 years, and losing Apple's 1604 no-year sentinel turns a day-only
    birthday into a 421-year-old contact."""

    @staticmethod
    def _coredata(year, month, day):
        """The value Contacts would actually store for that date."""
        import datetime as _dt
        when = _dt.datetime(year, month, day, tzinfo=_dt.timezone.utc)
        return (when - _dt.datetime(2001, 1, 1, tzinfo=_dt.timezone.utc)).total_seconds()

    def _read(self, people):
        d = tempfile.mkdtemp()
        make_store(d, people,
                   cols=("ZFIRSTNAME", "ZLASTNAME", "ZORGANIZATION", "ZJOBTITLE",
                         "ZBIRTHDAY"))
        return ab.read_store(os.path.join(d, "AddressBook-v22.abcddb"))

    def test_a_core_data_timestamp_becomes_the_day_contacts_shows(self):
        cards = self._read([{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace",
                             "ZBIRTHDAY": self._coredata(1985, 3, 15)}])
        self.assertEqual(cards[0].birthday, "1985-03-15")
        self.assertEqual(cards[0].birth_year_known, 1)
        # the 31-year trap: read as a Unix timestamp this would be 2016-something
        self.assertTrue(cards[0].birthday.startswith("1985-"))

    def test_a_date_before_the_2001_epoch_goes_backwards_not_forwards(self):
        # Negative seconds. A conversion that clamps at zero or raises would put
        # every pre-2001 birthday — i.e. almost all of them — on 2001-01-01.
        cards = self._read([{"ZFIRSTNAME": "Grace", "ZLASTNAME": "Hopper",
                             "ZBIRTHDAY": self._coredata(1906, 12, 9)}])
        self.assertEqual(cards[0].birthday, "1906-12-09")

    def test_apples_no_year_sentinel_survives_as_year_unknown(self):
        cards = self._read([{"ZFIRSTNAME": "No", "ZLASTNAME": "Year",
                             "ZBIRTHDAY": self._coredata(1604, 6, 2)}])
        self.assertEqual(cards[0].birthday, "1604-06-02")
        self.assertEqual(cards[0].birth_year_known, 0,
                         "1604 is Apple's no-year sentinel; treating it as a real "
                         "year makes this contact 421 years old")

    def test_a_missing_or_junk_birthday_is_simply_absent(self):
        cards = self._read([
            {"ZFIRSTNAME": "Empty", "ZLASTNAME": "One", "ZBIRTHDAY": None},
            {"ZFIRSTNAME": "Junk", "ZLASTNAME": "Two", "ZBIRTHDAY": "not a number"},
            {"ZFIRSTNAME": "Huge", "ZLASTNAME": "Three", "ZBIRTHDAY": 1e30},
        ])
        self.assertEqual([c.birthday for c in cards], ["", "", ""])

    def test_an_old_macos_without_the_column_still_imports_everything_else(self):
        """The whole point of runtime schema discovery: a store with no
        ZBIRTHDAY must not make the import fail, it must import the people."""
        d = tempfile.mkdtemp()
        make_store(d, [{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace"}],
                   cols=("ZFIRSTNAME", "ZLASTNAME"))
        cards = ab.read_store(os.path.join(d, "AddressBook-v22.abcddb"))
        self.assertEqual(cards[0].name, "Ada Lovelace")
        self.assertEqual(cards[0].birthday, "")

    def test_a_real_year_from_one_store_beats_a_no_year_from_another(self):
        """A Mac holds the flat store and one per account. Which one read_all
        happened to open first must not decide whether an age is knowable."""
        root = tempfile.mkdtemp()
        cols = ("ZFIRSTNAME", "ZLASTNAME", "ZORGANIZATION", "ZJOBTITLE", "ZBIRTHDAY")
        make_store(root, [{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace",
                           "ZBIRTHDAY": self._coredata(1604, 12, 10)}], cols=cols)
        make_store(os.path.join(root, "Sources", "ABC"),
                   [{"ZFIRSTNAME": "Ada", "ZLASTNAME": "Lovelace",
                     "ZBIRTHDAY": self._coredata(1815, 12, 10)}], cols=cols)
        cards, why = ab.read_all(root)
        self.assertEqual(why, "")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].birthday, "1815-12-10")
        self.assertEqual(cards[0].birth_year_known, 1)
