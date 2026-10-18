"""Writing-style profile: the offline measurement, the honesty surface, and the
invariant-7 guard on `style_directive()`.

`crm/style_profile.py` is a large module (about 2,300 lines). Every fixture here
is synthetic and inline; no real archive, no real contact and no network call is
involved, which is the whole point of the module (invariant 7: archive.db never
leaves the machine; invariant 8: zero API keys and every degraded path visible).

WHAT THESE TESTS ESTABLISH
--------------------------
The module works. The two bounded passes, the bucket space, the order
statistics, the closed-allowlist harvesting, the census, the exclusions, the
save/load contract, the call-time settings and the CLI all behave as their
docstrings claim, and — the thing that mattered most — `style_directive()` really
does contain no message text and no contact name. `TestDirectiveLeaksNothing`
seeds a distinctive name and sentence *inside the measured outbound corpus*, the
hardest case, and scans every directive the profile can produce plus the whole
stored blob and the whole report.

Two defects were known, both in `report()`'s accounting, and both were pinned as
expected failures until 2026-10-17 (NC-369). Both are fixed; their tests now live
in `TestTheReceiptTellsTheTruth`, with the old failure written into each docstring.

TESTING NOTES
-------------
  • `ArcBase` rebinds `arc.ARCHIVE_DB_PATH` (the module global
    `arc.open_if_present()` reads at call time) into a temp dir and restores it,
    the convention `test_api_briefs.py` and `test_api_ask.py` already use. With
    `CREATE_ARCHIVE = False` the path still points inside the temp dir, so the
    "archive is absent" case takes its `os.path.exists()` miss instead of finding
    the real archive.
  • `ts` auto-increments in `msg()`. Ties in a timestamp could hide an
    iteration-order bug behind equal sort keys.
  • `NC_STYLE_*`, `SELF_EMAIL` and `SELF_NAME` are cleared in `setUp` and
    restored in `tearDown`. `config` loads `secrets/.env` at import, so without
    that a real `SELF_EMAIL` in the environment silently changes
    `_no_outbound_reason("email")` and `provenance.self_email_set`.
  • `canon()` sorts lists AND sets recursively. `json.dumps(sort_keys=True)`
    does not touch list order, so a set that leaked into the profile would
    serialise differently on two runs and compare equal under sort_keys alone.
"""

import os, sys  # noqa: E401 — the bootstrap shim must run before any crm import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db, arc  # noqa: E402

import ast  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import sys as _sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

import style_profile as sp  # noqa: E402


# ── Corpus ───────────────────────────────────────────────────────────────────
# Swedish, carrying the three Swedish-only vowels between them.
SV = [
    "Hej! Hur är det med dig? Vi hörs snart.",
    "Tack för idag, det var kul att träffas igen.",
    "Jag tror att vi borde ta ett möte om det där på måndag.",
    "Kan du skicka över filen när du har tid? Ingen brådska.",
    "Låter bra, jag fixar det imorgon så är det ur världen.",
    "Vad gör du i helgen? Jag är i Stockholm hela veckan.",
    "Det känns som att vi behöver prata igenom detta ordentligt.",
    "Absolut, jag hör av mig så fort jag vet något mer.",
]

# English, no Swedish diacritics anywhere.
EN = [
    "Hi! How are you doing? Let's catch up soon.",
    "Thanks for today, it was really great to meet again.",
    "I think we should set up a meeting about that on Monday.",
    "Could you send over the file when you have time? No rush.",
    "Sounds good, I will sort it tomorrow so it is off the list.",
    "What are you up to this weekend? I am in London all week.",
    "It feels like we need to talk this through properly.",
    "Absolutely, I will get back to you as soon as I know more.",
]

# A Swedish frame with an English clause inside it. Whatever the heuristic
# decides it must not claim high confidence, and if it cannot decide this belongs
# in the stated unknown bucket rather than silently in sv or en.
CODE_SWITCHED = [
    "Hej! Jag tror att vi borde ta ett möte, but let's keep it short and focused.",
    "Tack för att du kollade — quick question though, is the deadline still Friday?",
]

# No function word from either list, so the denominator for a language score is
# zero. Must land in the unknown bucket (and mostly below NC_STYLE_MIN_CHARS).
TOO_SHORT = ["ok", "Ok!", "👍", "Deal", "Perfect", "+46701740605", "Zoom kl 14?"]

# Must not crash the tokeniser or the length measures.
EMOJI = "👍👍👍 🎉 så kul!! 🙌🏽 ❤️‍🔥 👨‍👩‍👧‍👦"          # incl. ZWJ + skin-tone sequences
CJK = "了解しました。来週の月曜日にお会いしましょう。 我们下周一见面吧。"  # no spaces at all
RTL = "شكرا جزيلا، سأراك الأسبوع القادم"                      # right-to-left
MIXED_SCRIPT = "Hej! 你好 مرحبا 👋 see you kl 14"

# The median-robustness outlier: 4,000 words in ONE message.
LONG_MESSAGE = " ".join(["ord"] * 4000)


# ── The invariant-7 leak fixture ─────────────────────────────────────────────
# Deliberately unmistakable. LEAK_NAME goes in crm.db as a contact name AND
# inside outbound bodies next to an allowlisted greeting and an allowlisted
# sign-off — the exact shape a free-text opener/closer harvester would quote.
# LEAK_SENTENCE is a whole outbound body, so it sits in the measured first-person
# corpus rather than safely on the inbound side.
LEAK_NAME = "Zebulon Qvarnström-Rutherford"
LEAK_SENTENCE = (
    "The pangolin quarterly reconciliation spreadsheet is stored in the vault "
    "under codename Marzipan Vandersloot."
)
LEAK_SUBJECT = "Fwd: Blodwynne Thistlethwaite audit"
LEAK_TOKENS = [
    "Zebulon", "Qvarnström", "Rutherford",
    "pangolin", "reconciliation", "Marzipan", "Vandersloot", "codename", "vault",
    "Blodwynne", "Thistlethwaite", "audit",
]


def assert_no_leak(case, text, where=""):
    """Fail if any fixture-specific name, sentence or distinctive token appears.

    Case-insensitive and substring-based on purpose: "zebulon" lowercased by a
    tokeniser, or "Qvarnström" inside a longer word, are both still leaks.
    """
    hay = (text or "").lower()
    for tok in LEAK_TOKENS:
        case.assertNotIn(
            tok.lower(), hay,
            f"invariant 7: {tok!r} from the archive fixture appears in {where or 'the output'}. "
            f"Output was:\n{text}")
    case.assertNotIn(LEAK_NAME.lower(), hay)
    case.assertNotIn(LEAK_SENTENCE.lower(), hay)
    case.assertNotIn(LEAK_SUBJECT.lower(), hay)


# ── Determinism helper ───────────────────────────────────────────────────────
_WS = re.compile(r"\s+")


def canon(obj):
    """An order-insensitive rendering. Sorts lists and sets recursively, because
    `json.dumps(sort_keys=True)` leaves list order alone — so a set that leaked
    into the profile would serialise in an arbitrary order inside a list and two
    runs would still compare equal."""
    if isinstance(obj, dict):
        return {k: canon(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return sorted((canon(v) for v in obj), key=repr)
    if isinstance(obj, (set, frozenset)):
        return sorted((canon(v) for v in obj), key=repr)
    if isinstance(obj, str):
        return _WS.sub(" ", obj).strip()
    return obj


def blob(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


# ── Fixture base ─────────────────────────────────────────────────────────────
class ArcBase(Base):
    """Base + a synthetic archive.db that `arc.open_if_present()` will find."""

    #: set False for the "archive.db is absent" case
    CREATE_ARCHIVE = True
    #: settings that must not be inherited from the machine's secrets/.env
    ENV_KEYS = ("SELF_EMAIL", "SELF_NAME")

    def setUp(self):
        super().setUp()
        self.arc_dir = tempfile.mkdtemp(prefix="crm_style_arc_")
        self.arc_path = os.path.join(self.arc_dir, "archive.db")
        self._orig_arc_path = arc.ARCHIVE_DB_PATH
        arc.ARCHIVE_DB_PATH = self.arc_path
        self._env = {k: os.environ.get(k) for k in
                     list(self.ENV_KEYS) + [k for k in os.environ
                                            if k.startswith("NC_STYLE_")]}
        for k in self._env:
            os.environ.pop(k, None)
        if self.CREATE_ARCHIVE:
            arc.init_archive(self.arc_path)
        self._seq = 0

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("NC_STYLE_")]:
            os.environ.pop(k, None)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        arc.ARCHIVE_DB_PATH = self._orig_arc_path
        shutil.rmtree(self.arc_dir, ignore_errors=True)
        super().tearDown()

    # -- writers ------------------------------------------------------------
    def contact(self, name, **fields):
        cid = db.create_contact(self.conn, name)
        for k, v in fields.items():
            db.set_field(self.conn, cid, k, v)
        self.conn.commit()
        return cid

    def msg(self, body, *, contact_id=1, channel="imessage", direction=1,
            ts=None, subject="", group_name="", conn=None):
        """One archive row. direction defaults to 1 — the first-person corpus.

        `ts` auto-increments so a dict/set iteration-order bug in the module
        under test cannot hide behind fixture rows sharing a timestamp.

        Given `conn`, the row goes into that open connection and the caller
        commits. NC-211: opening, committing and closing the archive for each
        row wrote far more to the SSD than the row itself (the close copies the
        log back into the file), which added up to a great deal of wasted disk
        writing on every run.
        """
        self._seq += 1
        if ts is None:
            ts = (f"2026-0{1 + self._seq % 9}-{1 + self._seq % 28:02d} "
                  f"12:{self._seq % 60:02d}:00")
        ac = conn or arc.get_archive_db(self.arc_path)
        try:
            arc.insert_message(
                ac, contact_id=contact_id, channel=channel, direction=direction,
                ts=ts, body=body, source_key=f"synth_{channel}_{self._seq}",
                subject=subject, group_name=group_name)
            if conn is None:
                ac.commit()
        finally:
            if conn is None:
                ac.close()

    def seed(self, bodies, *, direction=1, channel="imessage", contact_id=1,
             group_name=""):
        ac = arc.get_archive_db(self.arc_path)
        try:
            for b in bodies:
                self.msg(b, direction=direction, channel=channel,
                         contact_id=contact_id, group_name=group_name, conn=ac)
            ac.commit()
        finally:
            ac.close()

    def dump(self, path=None):
        """A full textual dump of a sqlite file, for "this wrote nothing" checks."""
        conn = sqlite3.connect(path) if path else self.conn
        try:
            return "\n".join(conn.iterdump())
        finally:
            if path:
                conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# THE MOST IMPORTANT TEST IN THIS FILE
# ═══════════════════════════════════════════════════════════════════════════
class TestDirectiveLeaksNothing(ArcBase):
    """`style_directive()` must contain no message text and no contact name.

    That is the reason the whole feature is offline: a version that sent
    harvested greetings to an LLM would carry other people's names off the
    machine (invariant 7). The property is
    supposed to hold by construction — the only strings the directive can emit
    are numbers, sentences written in `crm/style_profile.py`, tokens from the
    closed GREETINGS / SIGNOFFS / OPENER_WORDS / STYLE_LEXICON allowlists, and
    emoji. This class tries to break it.
    """

    def seed_leak_fixture(self):
        cid = self.contact(LEAK_NAME)
        # The distinctive sentence as a whole outbound body.
        self.msg(LEAK_SENTENCE, contact_id=cid, direction=1)
        # The name straight after an allowlisted greeting — what a free-text
        # opener harvester would grab.
        self.msg(f"Hej {LEAK_NAME}, tack för igår och för att du fixade det så snabbt.",
                 contact_id=cid, direction=1)
        # The name on its own line under an allowlisted sign-off — the branch
        # `signoff_of` deliberately examines and discards.
        self.msg(f"Tack så mycket för hjälpen igår, det gjorde stor skillnad.\n\nKram\n{LEAK_NAME}",
                 contact_id=cid, direction=1)
        # An email subject, which the pass must never read at all.
        self.msg(f"Hej, jag bifogar underlaget som vi pratade om igår kväll.\n\nMvh\n{LEAK_NAME}",
                 contact_id=cid, direction=1, channel="email", subject=LEAK_SUBJECT)
        # And the same distinctive words on the inbound side, so the vocabulary
        # comparison has a populated other half too.
        self.msg(LEAK_SENTENCE, contact_id=cid, direction=0)
        # Enough ordinary volume that buckets clear a lowered minimum.
        self.seed(SV + EN + CODE_SWITCHED, contact_id=cid, direction=1)
        self.seed(SV + EN, contact_id=cid, direction=0)
        return cid

    def test_the_leak_scanner_itself_fires(self):
        """A leak scanner that can never fail is worse than no scanner. This
        pins that `assert_no_leak` catches a verbatim leak, a lowercased one and
        a single distinctive token."""
        for probe in (LEAK_SENTENCE, LEAK_NAME.lower(), "…pangolin…",
                      f"Usual openings: hej, {LEAK_NAME}."):
            with self.assertRaises(AssertionError):
                assert_no_leak(self, probe)
        assert_no_leak(self, "Length: about 11 words; typical range 10–12.")

    def test_directive_contains_no_message_text_and_no_names(self):
        self.seed_leak_fixture()
        os.environ["NC_STYLE_MIN_BUCKET"] = "2"     # make real buckets usable
        prof = sp.profile(self.conn)
        self.assertTrue(prof["buckets"], "fixture produced no buckets to scan")
        self.assertTrue(any(v["usable"] for v in prof["buckets"].values()),
                        "no usable bucket, so the statistics path was never exercised")
        # The test is only meaningful if the seeded text was actually measured:
        # every outbound row read was used, and 22 is the fixture's outbound count.
        self.assertEqual(prof["corpus"]["outbound_scanned"], 22)
        self.assertEqual(prof["corpus"]["used_for_style"], 22)
        self.assertEqual(prof["corpus"]["dropped"], {})

        keys = (list(prof["buckets"]) + sp.all_bucket_keys()
                + [prof["best_bucket"], "", "overall", "sv.close.chat"])
        for key in keys:
            directive = sp.style_directive(prof, key)
            assert_no_leak(self, directive, f"the directive for bucket {key!r}")

        # The same guarantee for the stored blob and the receipt. crm.db is the
        # exportable database (crm/export_data.py copies it wholesale), so the
        # blob itself must be clean too.
        assert_no_leak(self, blob(prof), "the profile that save() writes to crm.db")
        assert_no_leak(self, blob(sp.report(prof)), "report()")

    def test_directive_default_threshold_fallback_also_leaks_nothing(self):
        """The whole-corpus fallback path builds its numbers from `overall`,
        which is a different code path from a named bucket."""
        self.seed_leak_fixture()
        prof = sp.profile(self.conn)          # default min_bucket 30 → nothing usable
        self.assertEqual(prof["best_bucket"], "")
        directive = sp.style_directive(prof, "sv.close.chat")
        self.assertIn("Using the whole measured corpus", directive)
        assert_no_leak(self, directive, "the whole-corpus fallback directive")

    def test_only_allowlisted_tokens_reach_the_directive(self):
        """Stronger than a leak scan: every alphabetic word in the emitted
        greeting / sign-off / vocabulary lists must be a member of a closed list
        defined in the source."""
        self.seed_leak_fixture()
        os.environ["NC_STYLE_MIN_BUCKET"] = "2"
        prof = sp.profile(self.conn)
        allowed = (set(sp.GREETINGS) | set(sp.SIGNOFFS) | set(sp.OPENER_WORDS)
                   | set(sp.LEXICON_TERMS))
        for key, entry in prof["buckets"].items():
            for block in ("top_greetings", "top_signoffs", "top_opener_words"):
                for item in entry[block]["items"]:
                    self.assertIn(item["term"], allowed,
                                  f"{key}.{block} emitted {item['term']!r}, which is "
                                  "not on any closed allowlist in crm/style_profile.py")
        for item in (prof["vocabulary"].get("items") or []):
            self.assertIn(item["term"], set(sp.LEXICON_TERMS))

    def test_top_emoji_are_emoji_and_nothing_else(self):
        cid = self.contact(LEAK_NAME)
        self.seed([EMOJI + " " + b for b in SV], contact_id=cid)
        prof = sp.profile(self.conn)
        for item in prof["overall"]["top_emoji"]["items"]:
            self.assertEqual(len(item["term"]), 1)
            self.assertTrue(sp._is_emoji(item["term"]),
                            f"{item['term']!r} was emitted as an emoji but is not one")
            self.assertFalse(item["term"].isalnum())


# ═══════════════════════════════════════════════════════════════════════════
# Offline by construction
# ═══════════════════════════════════════════════════════════════════════════
class TestOfflineByConstruction(unittest.TestCase):
    """Invariant 8 and invariant 7 at the source level: standard library only,
    no network, no `anthropic`."""

    SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "crm", "style_profile.py")

    @classmethod
    def setUpClass(cls):
        with open(cls.SRC, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)

    def _imported(self):
        """Every module name imported anywhere in the file, including inside
        functions — `main()` imports three of its own."""
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
        return names

    def test_imports_are_stdlib_or_crm_siblings(self):
        # figures (NC-115) is the vocabulary of who counts. It imports nothing
        # but the standard library, and db already imported it.
        siblings = {"config", "archive", "db", "figures"}
        stdlib = set(getattr(_sys, "stdlib_module_names", ()))
        self.assertTrue(stdlib, "need Python 3.10+ for sys.stdlib_module_names")
        for name in sorted(self._imported() - siblings):
            self.assertIn(name, stdlib,
                          f"crm/style_profile.py imports {name!r}, which is neither "
                          "standard library nor a crm sibling")

    def test_no_network_and_no_llm_client(self):
        forbidden = {"urllib", "http", "socket", "ssl", "requests", "httpx",
                     "anthropic", "openai", "subprocess", "smtplib", "ftplib",
                     "telnetlib", "asyncio"}
        self.assertEqual(self._imported() & forbidden, set())
        self.assertNotIn("ANTHROPIC_API_KEY", self.source)
        self.assertNotIn("api.anthropic.com", self.source)

    def test_no_mean_of_a_length_is_ever_published(self):
        """"Nothing here averages a length. One 4,000-word message must not move
        the number a drafter is told to hit." Checked against the serialised
        shape, not the prose."""
        self.assertFalse(hasattr(sp._Dist, "mean"))
        keys = set(sp._Dist().summary()) | set(sp._Rate().summary())
        for key in keys:
            self.assertNotIn("mean", key)
            self.assertNotIn("avg", key)

    def test_linkedin_is_the_channel_excluded_by_default(self):
        """The importer decides direction by "the sender is not a known contact,
        therefore I sent it" (crm/linkedin_import.py:198-206), so any LinkedIn
        message from a stranger is recorded as the owner's own writing."""
        self.assertEqual(sp.UNSAFE_CHANNELS, frozenset({"linkedin"}))
        self.assertFalse(sp.INCLUDE_LINKEDIN_DEFAULT)
        self.assertFalse(sp.include_linkedin())
        self.assertEqual(sp.CHANNEL_PROVENANCE["linkedin"]["false_outbound_risk"],
                         "structural")
        excluded, reasons = sp._channel_filter()
        self.assertEqual(excluded, ["linkedin"])
        self.assertTrue(reasons[0])


class TestWritesNothingButState(ArcBase):
    """`measure()`/`profile()`/`report()`/`style_directive()` write nothing at
    all, and `save()` touches only the `state` row it says it does."""

    def test_measuring_writes_to_neither_database(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid)
        self.seed(SV, contact_id=cid, direction=0)
        before_crm, before_arc = self.dump(), self.dump(self.arc_path)
        prof = sp.profile(self.conn)
        sp.report(prof)
        sp.style_directive(prof, prof["best_bucket"])
        self.assertEqual(self.dump(), before_crm, "profile() wrote to crm.db")
        self.assertEqual(self.dump(self.arc_path), before_arc,
                         "profile() wrote to archive.db")
        self.assertFalse(sp.report(prof)["wrote_anything"])

    def test_save_touches_only_the_state_table(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid)
        counts = lambda: {  # noqa: E731
            t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("contacts", "notes", "identifiers", "lists", "list_members")}
        before = counts()
        receipt = sp.save(self.conn, sp.profile(self.conn))
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["key"], sp.STATE_KEY)
        self.assertEqual(counts(), before)
        rows = self.conn.execute("SELECT key FROM state").fetchall()
        self.assertEqual([r["key"] for r in rows], [sp.STATE_KEY])


# ═══════════════════════════════════════════════════════════════════════════
# Degraded paths — all four must be visible, none may raise
# ═══════════════════════════════════════════════════════════════════════════
class TestMissingArchive(ArcBase):
    CREATE_ARCHIVE = False

    def test_missing_archive_file_returns_a_shaped_empty_profile(self):
        prof = sp.profile(self.conn)
        self.assertFalse(prof["archive_present"])
        self.assertTrue(prof["degraded"])
        self.assertIn("message archive is missing", prof["degraded_reason"])
        # Shaped, not partial: everything a UI reads is present and zero.
        self.assertEqual(prof["buckets"], {})
        self.assertEqual(prof["overall"]["messages"], 0)
        self.assertIsNone(prof["overall"]["length_words"]["median"])
        self.assertFalse(prof["vocabulary"]["computable"])
        self.assertTrue(prof["vocabulary"]["reason"])
        self.assertEqual(prof["version"], sp.PROFILE_VERSION)

    def test_report_says_the_archive_is_absent(self):
        rep = sp.report(sp.profile(self.conn))
        self.assertFalse(rep["archive_present"])
        self.assertTrue(rep["sentences"])
        self.assertIn("message archive is not there yet", rep["summary_text"])
        self.assertNotIn("archive.db", rep["summary_text"])
        self.assertEqual(rep["messages_in_archive"], 0)

    def test_directive_says_so_and_tells_the_drafter_to_carry_on(self):
        directive = sp.style_directive(sp.profile(self.conn), "sv.close.chat")
        self.assertIn(sp.DIRECTIVE_HEADER, directive)
        self.assertIn("own default voice", directive)


class TestEmptyArchive(ArcBase):
    def test_empty_archive_measures_zero_without_inventing_statistics(self):
        prof = sp.profile(self.conn)
        self.assertTrue(prof["archive_present"])
        self.assertFalse(prof["degraded"], prof["degraded_reason"])
        self.assertEqual(prof["census"]["messages_total"], 0)
        self.assertEqual(prof["corpus"]["outbound_scanned"], 0)
        self.assertEqual(prof["overall"]["messages"], 0)
        for stat in ("length_words", "length_chars", "words_per_sentence"):
            self.assertIsNone(prof["overall"][stat]["median"])
            self.assertEqual(prof["overall"][stat]["n"], 0)
        for rate in ("greeting_rate", "signoff_rate", "messages_with_emoji"):
            self.assertIsNone(prof["overall"][rate]["share"])
            self.assertEqual(prof["overall"][rate]["n"], 0)

    def test_report_and_directive_are_honest_about_zero(self):
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        self.assertEqual(rep["buckets_usable"], 0)
        self.assertIn("No group of messages holds", rep["summary_text"])
        self.assertFalse(rep["vocabulary_computable"])
        directive = sp.style_directive(prof, "sv.close.chat")
        self.assertIn("n=0", directive)
        # No number a drafter could act on was invented from nothing.
        self.assertNotIn("Length:", directive)
        self.assertNotIn("Sentences:", directive)


class TestOnlyInboundMessages(ArcBase):
    """Only-inbound must report an EMPTY CORPUS, never statistics computed from
    nothing. Two real importers make this the normal state of a channel: WhatsApp
    group rows are inserted `direction=0` unconditionally, and every mbox email
    is inbound when SELF_EMAIL was unset at import time."""

    def test_no_outbound_means_no_statistics_at_all(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid, direction=0)
        prof = sp.profile(self.conn)
        self.assertEqual(prof["census"]["inbound_total"], 16)
        self.assertEqual(prof["census"]["outbound_total"], 0)
        self.assertEqual(prof["corpus"]["outbound_scanned"], 0)
        self.assertEqual(prof["corpus"]["used_for_style"], 0)
        self.assertEqual(prof["buckets"], {})
        self.assertEqual(prof["overall"]["messages"], 0)
        self.assertIsNone(prof["overall"]["length_words"]["median"])
        self.assertEqual(prof["best_bucket"], "")

    def test_the_channel_is_named_with_the_reason_it_has_no_outbound(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid, direction=0, channel="whatsapp")
        self.seed(EN, contact_id=cid, direction=0, channel="email")
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual(rep["channels_with_no_outbound"], ["email", "whatsapp"])
        kinds = [e for e in prof["exclusions"] if e["kind"] == "no_outbound"]
        self.assertEqual({e["what"] for e in kinds},
                         {"channel email contributed no outbound messages",
                          "channel whatsapp contributed no outbound messages"})
        for e in kinds:
            self.assertTrue(e["why"], "a refusal with no reason is a silent drop")
            self.assertEqual(e["of"], 8)
        joined = " ".join(rep["sentences"])
        self.assertIn("could not read which messages were yours", joined)
        self.assertIn("does not know your email address", joined)
        self.assertIn("WhatsApp has messages you received but none you sent", joined)
        # "outbound messages read" stays in the line above: the Data page's chips
        # are read from exactly those words (importers.RECEIPT_PATTERNS).
        for jargon in ("COALESCE", "SELF_EMAIL", "email_import.py", "inbound"):
            self.assertNotIn(jargon, joined)

    def test_vocabulary_refuses_rather_than_returning_zero(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid, direction=0)
        prof = sp.profile(self.conn)
        self.assertFalse(prof["vocabulary"]["computable"])
        self.assertIn("nothing to compare", prof["vocabulary"]["reason"])
        self.assertEqual(prof["vocabulary"]["items"], [])
        self.assertEqual(prof["vocabulary"]["outbound_lexicon_tokens"], 0)
        self.assertGreater(prof["vocabulary"]["inbound_lexicon_tokens"], 0)


class TestMigrationOnAnOldArchiveSchema(ArcBase):
    """A migration must be tested against an OLD schema.

    Every other fixture here builds a fresh archive.db through
    `arc.init_archive`, which is how an index naming a column that has not been
    added yet can pass every test and then stop the app booting. Both of
    `style_profile`'s SQL paths reference `group_name`, which archives created
    before group-chat support do not have.
    """

    CREATE_ARCHIVE = False

    OLD_DDL = """
    CREATE TABLE messages (
        id          INTEGER PRIMARY KEY,
        contact_id  INTEGER NOT NULL,
        channel     TEXT    NOT NULL,
        direction   INTEGER NOT NULL DEFAULT 0,
        ts          TEXT    NOT NULL,
        subject     TEXT    NOT NULL DEFAULT '',
        body        TEXT    NOT NULL DEFAULT '',
        source_key  TEXT    NOT NULL,
        UNIQUE(channel, source_key)
    );
    """

    def build_old_archive(self):
        conn = sqlite3.connect(self.arc_path)
        conn.executescript(self.OLD_DDL)
        for i, body in enumerate(SV + EN):
            conn.execute(
                "INSERT INTO messages (contact_id, channel, direction, ts, body, "
                "source_key) VALUES (1, 'imessage', 1, ?, ?, ?)",
                (f"2026-01-{1 + i:02d} 09:00:00", body, f"old_{i}"))
        conn.commit()
        conn.close()

    def test_pre_group_name_archive_does_not_raise(self):
        self.build_old_archive()
        self.contact("Anna Nilsson")
        # assertLogs both proves the degradation is logged and keeps the module's
        # exc_info traceback out of the suite's output.
        with self.assertLogs(sp.log, "WARNING") as logged:
            prof = sp.profile(self.conn)     # must not raise
        self.assertIn("census failed", " ".join(logged.output))
        self.assertTrue(prof["archive_present"])
        self.assertTrue(prof["degraded"])
        self.assertTrue(prof["degraded_reason"])
        self.assertFalse(prof["census"]["readable"])
        # And the directive still degrades in words rather than crashing.
        self.assertIn("own default voice",
                      sp.style_directive(prof, "sv.close.chat"))

    def test_the_archive_migration_makes_it_measurable_again(self):
        self.build_old_archive()
        self.contact("Anna Nilsson")
        arc.init_archive(self.arc_path)       # the idempotent migration
        prof = sp.profile(self.conn)
        self.assertFalse(prof["degraded"], prof["degraded_reason"])
        self.assertEqual(prof["census"]["messages_total"], 16)
        self.assertEqual(prof["corpus"]["outbound_scanned"], 16)
        self.assertEqual(sp.report(prof)["accounting"]["unexplained"], 0)


# ═══════════════════════════════════════════════════════════════════════════
# Buckets, thresholds and the honesty surface
# ═══════════════════════════════════════════════════════════════════════════
class TestBuckets(ArcBase):
    def test_language_split_has_sv_en_and_an_honest_unknown(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid)
        self.seed(EN, contact_id=cid)
        self.seed([CJK, RTL], contact_id=cid)      # neither language
        prof = sp.profile(self.conn)
        counts = {k: v["messages"] for k, v in prof["buckets"].items()}
        self.assertEqual(counts, {"sv.new.chat": 8, "en.new.chat": 8,
                                  "unknown.new.chat": 2})
        self.assertIn("unknown", sp.LANG_VALUES)

    def test_code_switched_messages_never_claim_high_confidence(self):
        """A Swedish frame with an English clause inside it must either land in
        the stated unknown bucket or come back with a visibly low confidence.
        Silently filing it as sv or en at 1.0 would pollute two buckets at once."""
        results = [sp.detect_language(body) for body in CODE_SWITCHED]
        for (lang, conf), body in zip(results, CODE_SWITCHED):
            self.assertIn(lang, ("sv", "en", ""))
            self.assertLess(conf, 0.75,
                            f"{body[:30]!r} got confidence {conf}, which is not the "
                            "honest signal a mixed-language message deserves")
        # One of the two is genuinely undecidable at the default margin and says
        # so rather than guessing.
        self.assertIn("", [lang for lang, _ in results])
        # A lifted margin pushes both into the unknown bucket, and leaves
        # unambiguous text alone.
        os.environ["NC_STYLE_LANG_MARGIN"] = "0.9"
        for body in CODE_SWITCHED:
            self.assertEqual(sp.detect_language(body)[0], "")
        for body in SV[:3] + EN[:3]:
            self.assertNotEqual(sp.detect_language(body)[0], "",
                                "a lifted margin must not swallow unambiguous text")

    def test_a_bucket_under_the_threshold_is_reported_as_skipped(self):
        cid = self.contact("Anna Nilsson")
        for i in range(4):
            self.seed([f"{b} Nummer {i}." for b in EN], contact_id=cid)   # 32 en
        self.seed(SV[:2], contact_id=cid)                                 # 2 sv
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual(rep["buckets_seen"], 2)
        self.assertEqual(rep["buckets_usable"], 1)
        self.assertEqual(rep["buckets_possible"], len(sp.all_bucket_keys()))
        self.assertEqual(len(sp.all_bucket_keys()), 18)
        self.assertEqual(rep["buckets_skipped_for_lack_of_data"], 1)
        (thin,) = rep["skipped_buckets"]
        self.assertEqual(thin["bucket"], "sv.new.chat")
        self.assertEqual(thin["messages"], 2)
        self.assertEqual(thin["min_required"], sp.MIN_BUCKET_DEFAULT)
        self.assertIn("NC_STYLE_MIN_BUCKET", thin["reason"])
        # Reported, not absent: the thin bucket is still in the profile.
        self.assertIn("sv.new.chat", prof["buckets"])
        self.assertFalse(prof["buckets"]["sv.new.chat"]["usable"])
        self.assertIn("sv.new.chat", rep["bucket_counts"])
        words = " ".join(rep["sentences"])
        self.assertIn("Swedish, to someone you barely know, as a chat message (2)", words)
        self.assertNotIn("sv.new.chat", words, "an internal key in a sentence a person reads")

    def test_threshold_is_inclusive_at_the_boundary(self):
        cid = self.contact("Anna Nilsson")
        self.seed([f"{EN[0]} Nummer {i}." for i in range(3)], contact_id=cid)
        os.environ["NC_STYLE_MIN_BUCKET"] = "3"
        prof = sp.profile(self.conn)
        self.assertTrue(prof["buckets"]["en.new.chat"]["usable"])
        self.assertEqual(prof["skipped_buckets"], [])
        os.environ["NC_STYLE_MIN_BUCKET"] = "4"
        prof = sp.profile(self.conn)
        self.assertFalse(prof["buckets"]["en.new.chat"]["usable"])
        self.assertEqual(len(prof["skipped_buckets"]), 1)

    def test_thin_bucket_substitution_is_named_with_both_counts(self):
        cid = self.contact("Anna Nilsson")
        for i in range(4):
            self.seed([f"{b} Nummer {i}." for b in EN], contact_id=cid)
        self.seed(SV[:2], contact_id=cid)
        prof = sp.profile(self.conn)
        directive = sp.style_directive(prof, "sv.new.chat")
        self.assertIn("Asked for Swedish", directive)
        self.assertIn("only 2 measured messages", directive)
        self.assertIn(f"below the {sp.MIN_BUCKET_DEFAULT}-message minimum", directive)
        self.assertIn("Using English", directive)
        self.assertIn("(32 messages)", directive)
        self.assertIn("Basis: English", directive)

    def test_best_bucket_is_always_one_of_the_measured_buckets(self):
        cid = self.contact("Anna Nilsson")
        for i in range(4):
            self.seed([f"{b} Nummer {i}." for b in EN + SV], contact_id=cid)
        prof = sp.profile(self.conn)
        self.assertIn(prof["best_bucket"], prof["buckets"])
        self.assertTrue(prof["buckets"][prof["best_bucket"]]["usable"])
        self.assertEqual(prof["best_bucket"], sp._best_bucket(prof))

    def test_register_and_medium_axes(self):
        row = self.conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (self.contact("Anna"),)).fetchone()
        self.assertEqual(sp.register_for(row), "new")
        self.assertEqual(sp.register_for({"strength": sp.REGISTER_SPLIT}), "close")
        self.assertEqual(sp.register_for({"strength": sp.REGISTER_SPLIT - 1}), "new")
        self.assertEqual(sp.register_for(None), "unknown")
        self.assertEqual(sp.register_for({"strength": "not a number"}), "unknown")
        self.assertEqual(sp.medium_for("email"), "letter")
        self.assertEqual(sp.medium_for(" EMAIL "), "letter")
        self.assertEqual(sp.medium_for("imessage"), "chat")
        self.assertEqual(sp.medium_for(""), "chat")
        self.assertEqual(sp.bucket_for(row, {"body": SV[0], "channel": "email"}),
                         "sv.new.letter")
        self.assertEqual(sp.bucket_for(None, None), "unknown.unknown.chat")
        self.assertEqual(sp.bucket_key("", "", ""), "unknown.unknown.chat")


class TestDenominators(ArcBase):
    """Every published statistic carries the count it was computed from. "30 of
    400 matches" shipping without the 400 is the shape this codebase has had."""

    def setUp(self):
        super().setUp()
        self.cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=self.cid)
        self.seed(SV, contact_id=self.cid, direction=0)
        self.prof = sp.profile(self.conn)

    def test_three_statistics_carry_their_denominator(self):
        overall = self.prof["overall"]
        words = overall["length_words"]                     # an order statistic
        self.assertEqual(words["n"], 16)
        self.assertIsNotNone(words["median"])
        greeting = overall["greeting_rate"]                 # a rate
        self.assertEqual(greeting["n"], 16)
        self.assertEqual(greeting["hits"], 2)               # "Hej!" and "Hi!"
        self.assertAlmostEqual(greeting["share"], 2 / 16)
        commas = overall["commas_per_100_words"]            # a per-100-words rate
        self.assertEqual(commas["n"], overall["length_words"]["n"] and commas["n"])
        self.assertGreater(commas["n"], 0)
        self.assertEqual(commas["per_100"], round(100.0 * commas["hits"] / commas["n"], 2))

    def test_no_rate_or_distribution_block_omits_n(self):
        for scope, stats in [("overall", self.prof["overall"])] + list(
                self.prof["buckets"].items()):
            for key, value in stats.items():
                if isinstance(value, dict) and ("share" in value or "median" in value):
                    self.assertIn("n", value, f"{scope}.{key} has no denominator")

    def test_the_vocabulary_comparison_carries_both_denominators(self):
        vocab = self.prof["vocabulary"]
        self.assertTrue(vocab["computable"])
        for key in ("outbound_tokens", "inbound_tokens", "outbound_lexicon_tokens",
                    "inbound_lexicon_tokens", "lexicon_size", "terms_compared"):
            self.assertGreater(vocab[key], 0, key)
        for item in vocab["items"]:
            self.assertIn("outbound_count", item)
            self.assertIn("inbound_count", item)
        self.assertIn("closed", vocab["method"])

    def test_every_exclusion_carries_a_count_a_total_and_a_reason(self):
        for entry in self.prof["exclusions"]:
            self.assertIn("count", entry)
            self.assertIn("of", entry)
            self.assertTrue(entry["why"], entry)
            self.assertTrue(entry["what"], entry)
            self.assertIsInstance(entry["count"], int)


class TestOrderStatisticsAreRobust(ArcBase):
    def test_one_4000_word_message_does_not_move_the_median(self):
        cid = self.contact("Anna Nilsson")
        corpus = (SV + EN)[:15]           # odd count, so the median rank is stable
        self.seed(corpus, contact_id=cid)
        before = sp.profile(self.conn)["overall"]["length_words"]
        self.msg(LONG_MESSAGE, contact_id=cid)
        after = sp.profile(self.conn)["overall"]["length_words"]

        self.assertEqual(before["n"], 15)
        self.assertEqual(after["n"], 16)
        self.assertEqual(after["median"], before["median"])
        self.assertEqual(after["p25"], before["p25"])
        self.assertEqual(after["p75"], before["p75"])
        self.assertEqual(after["p90"], before["p90"])
        # It IS visible — the outlier is not hidden, only kept out of the median.
        self.assertEqual(after["max"], 4000)
        self.assertGreater(after["max"], before["max"])
        # A mean would have moved by hundreds of words.
        self.assertLess(before["median"], 30)

    def test_dist_folds_pathological_values_into_a_reported_overflow(self):
        dist = sp._Dist(cap=10)
        for value in (1, 2, 3, 500):
            dist.add(value)
        for junk in ("not a number", None, -5):
            dist.add(junk)
        summary = dist.summary()
        self.assertEqual(summary["n"], 4)
        self.assertEqual(summary["over_cap"], 1)
        self.assertEqual(summary["cap"], 10)
        self.assertEqual(summary["median"], 2)
        self.assertIsNone(sp._Dist().quantile(0.5))

    def test_overall_is_the_exact_merge_of_the_buckets(self):
        """`overall` is built by merging bucket accumulators instead of measuring
        every message twice. A merged bucket must be bit-identical to a measured
        one, or the whole-corpus fallback quietly disagrees with its parts."""
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid)          # one bucket only
        prof = sp.profile(self.conn)
        (only,) = prof["buckets"].values()
        stripped = {k: v for k, v in only.items()
                    if k not in ("usable", "min_required")}
        self.assertEqual(blob(stripped), blob(prof["overall"]))


class TestTokeniserRobustness(ArcBase):
    def test_emoji_cjk_rtl_and_mixed_script_do_not_crash_anything(self):
        cid = self.contact("Anna Nilsson")
        self.seed([EMOJI, CJK, RTL, MIXED_SCRIPT], contact_id=cid)
        self.seed([EMOJI, CJK], contact_id=cid, direction=0)
        prof = sp.profile(self.conn)                         # must not raise
        rep = sp.report(prof)
        self.assertEqual(prof["corpus"]["outbound_scanned"], 4)
        self.assertEqual(prof["overall"]["messages"], 4)
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        for key in prof["buckets"]:
            self.assertIsInstance(sp.style_directive(prof, key), str)
        self.assertGreater(prof["overall"]["messages_with_emoji"]["hits"], 0)

    def test_zwj_and_skin_tone_sequences_count_as_one_emoji_each(self):
        found = sp.emoji_in(EMOJI)
        self.assertEqual(found, ["👍", "👍", "👍", "🎉", "🙌", "❤", "👨"])
        self.assertEqual(sp.emoji_in(CJK), [])
        self.assertEqual(sp.emoji_in(RTL), [])
        self.assertEqual(sp.emoji_in(""), [])
        self.assertEqual(sp.emoji_in(None), [])

    def test_spaceless_and_rtl_text_still_produces_finite_measures(self):
        for text in (CJK, RTL, MIXED_SCRIPT, EMOJI):
            self.assertIsInstance(sp.sentences_of(text), list)
            self.assertIsInstance(sp._tokens(text), list)
            self.assertEqual(sp._word_count(text), len(sp._tokens(text)))
            lang, conf = sp.detect_language(text)
            self.assertIn(lang, ("sv", "en", ""))
            self.assertGreaterEqual(conf, 0.0)
        self.assertEqual(sp.detect_language(CJK)[0], "",
                         "a language the detector does not know must come back unknown")
        self.assertEqual(sp.detect_language(RTL)[0], "")

    def test_short_messages_have_no_language_and_are_counted_not_guessed(self):
        for text in TOO_SHORT:
            self.assertEqual(sp.detect_language(text)[0], "", repr(text))
        cid = self.contact("Anna Nilsson")
        self.seed(TOO_SHORT, contact_id=cid)
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        short = prof["corpus"]["dropped"].get("shorter_than_min_chars", 0)
        self.assertEqual(short + prof["corpus"]["used_for_style"],
                         prof["corpus"]["outbound_scanned"])
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        (entry,) = [e for e in prof["exclusions"] if e["kind"] == "too_short"]
        self.assertEqual(entry["count"], short)
        self.assertEqual(entry["setting"], "NC_STYLE_MIN_CHARS")

    def test_sentence_splitting_guards_abbreviations_and_decimals(self):
        self.assertEqual(len(sp.sentences_of("Vi ses kl 14.30 imorgon.")), 1)
        self.assertEqual(len(sp.sentences_of("Ta med t.ex. kaffe. Tack!")), 2)
        self.assertEqual(len(sp.sentences_of("Hi! How are you? Fine.")), 3)
        self.assertEqual(sp.sentences_of("  "), [])
        self.assertEqual(len(sp.sentences_of("rad ett\nrad två")), 2)


class TestClosedAllowlists(ArcBase):
    def test_greeting_matching_is_on_a_word_boundary(self):
        self.assertEqual(sp.greeting_of("Hej Anna, hur är det?"), "hej")
        self.assertEqual(sp.greeting_of("Good morning Sam"), "good morning")
        self.assertEqual(sp.greeting_of("Hint of trouble ahead"), "")
        self.assertEqual(sp.greeting_of("Nothing to see here"), "")
        self.assertEqual(sp.greeting_of(""), "")

    def test_signoff_matching_discards_the_name_beside_it(self):
        self.assertEqual(sp.signoff_of("Tack för allt\nMvh Sam"), "mvh")
        self.assertEqual(sp.signoff_of("Tack för allt\nCheers\nSam"), "cheers")
        self.assertEqual(sp.signoff_of("Tack för allt\nSession over"), "")
        self.assertEqual(sp.signoff_of(""), "")

    def test_an_opener_outside_the_allowlist_increments_other_and_is_not_stored(self):
        cid = self.contact("Anna Nilsson")
        self.msg("Zebulon ringde precis och undrade om mötet på tisdag.",
                 contact_id=cid)
        self.seed(["Tack för allt du gjorde igår, det var verkligen snyggt."] * 2,
                  contact_id=cid)
        prof = sp.profile(self.conn)
        overall = prof["overall"]
        self.assertEqual(overall["opener_not_allowlisted"], 1)
        terms = {i["term"] for i in overall["top_opener_words"]["items"]}
        self.assertEqual(terms, {"tack"})
        assert_no_leak(self, blob(overall), "the opener statistics")

    def test_top_n_keeps_the_tail_count_rather_than_losing_it(self):
        cid = self.contact("Anna Nilsson")
        self.seed(["Hej Anna, hur mår du idag? Allt bra hoppas jag.",
                   "Hallå där, hur går det med projektet på jobbet?",
                   "Tjena, ska vi ta en fika nu på fredag eftermiddag?",
                   "Hi Anna, how are you doing today? All well I hope."],
                  contact_id=cid)
        os.environ["NC_STYLE_TOP_N"] = "1"
        prof = sp.profile(self.conn)
        block = prof["overall"]["top_greetings"]
        self.assertEqual(len(block["items"]), 1)
        self.assertEqual(block["distinct"], 4)
        self.assertEqual(block["shown"] + block["other"], block["n"])
        self.assertEqual(block["other"], 3)

    def test_ties_break_alphabetically_not_by_row_order(self):
        counter = sp.Counter({"hej": 3, "hallå": 3, "tjena": 3, "hi": 1})
        first = sp._top(counter, 2)
        second = sp._top(sp.Counter(dict(reversed(list(counter.items())))), 2)
        self.assertEqual(first, second)
        self.assertEqual([i["term"] for i in first["items"]], ["hallå", "hej"])


class TestBodyCleaning(ArcBase):
    QUOTED = ("Hej Anna,\n\nTack för mötet igår. Jag skickar filen imorgon.\n\n"
              "Mvh\nSam\n\n> Det här skrev Anna och det ska inte mätas\n"
              "On Mon, 1 Jan 2026, Anna wrote:\n> gammalt citat")

    def test_quoted_replies_and_signatures_are_removed_and_counted(self):
        clean, flags = sp._clean_body(self.QUOTED)
        self.assertNotIn("skrev Anna", clean)
        self.assertNotIn("gammalt citat", clean)
        self.assertIn("Tack för mötet", clean)
        self.assertTrue(flags["quoted"])
        self.assertTrue(flags["reply_header"])

    def test_the_cleaning_counts_reach_the_report(self):
        cid = self.contact("Anna Nilsson")
        self.seed([self.QUOTED] * 3, contact_id=cid, channel="email")
        self.msg("> bara ett citat och inget annat alls", contact_id=cid,
                 channel="email")
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual(rep["bodies_cleaned"]["quoted"], 4)
        self.assertEqual(rep["bodies_cleaned"]["reply_header"], 3)
        self.assertEqual(prof["corpus"]["dropped"]["empty_after_quote_removal"], 1)
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        (entry,) = [e for e in prof["exclusions"] if e["kind"] == "empty_after_clean"]
        self.assertEqual(entry["count"], 1)
        self.assertEqual(list(prof["buckets"]), ["sv.new.letter"])


# ═══════════════════════════════════════════════════════════════════════════
# Channel and contact policy
# ═══════════════════════════════════════════════════════════════════════════
class TestChannelPolicy(ArcBase):
    def test_linkedin_is_excluded_counted_and_explained(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid, channel="linkedin")
        self.seed(EN, contact_id=cid, channel="imessage")
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual(list(prof["buckets"]), ["en.new.chat"])
        self.assertFalse(rep["linkedin_included"])
        (entry,) = [e for e in prof["exclusions"]
                    if e["what"] == "channel linkedin"]
        self.assertEqual(entry["count"], 8)
        self.assertEqual(entry["of"], 8)
        self.assertEqual(entry["setting"], "NC_STYLE_INCLUDE_LINKEDIN")
        self.assertIn("InMail", entry["why"])
        self.assertEqual(rep["excluded_by_policy"], 8)
        said = " ".join(rep["sentences"])
        self.assertIn("anyone not in your contacts is assumed to be you", said)
        self.assertNotIn("NC_STYLE", said, "a setting name in a sentence a person reads")
        self.assertEqual(rep["accounting"]["available_unexplained"], 0)

    def test_turning_linkedin_on_is_allowed_and_flagged_as_contaminated(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid, channel="linkedin")
        self.seed(EN, contact_id=cid, channel="imessage")
        os.environ["NC_STYLE_INCLUDE_LINKEDIN"] = "1"
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual({k: v["messages"] for k, v in prof["buckets"].items()},
                         {"sv.new.chat": 8, "en.new.chat": 8})
        self.assertTrue(prof["caps"]["include_linkedin"])
        self.assertTrue(rep["linkedin_included"])
        self.assertIn("counted as your own writing", " ".join(rep["sentences"]))
        self.assertEqual(prof["exclusions"], [])

    def test_group_messages_are_excluded_counted_and_switchable(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid, group_name="Book club")
        self.seed(EN, contact_id=cid)
        prof = sp.profile(self.conn)
        self.assertEqual(list(prof["buckets"]), ["en.new.chat"])
        (entry,) = [e for e in prof["exclusions"] if e["kind"] == "group_policy"]
        self.assertEqual(entry["count"], 8)
        self.assertEqual(entry["of"], 16)
        self.assertEqual(entry["setting"], "NC_STYLE_INCLUDE_GROUPS")
        os.environ["NC_STYLE_INCLUDE_GROUPS"] = "1"
        prof = sp.profile(self.conn)
        self.assertEqual({k: v["messages"] for k, v in prof["buckets"].items()},
                         {"sv.new.chat": 8, "en.new.chat": 8})
        self.assertEqual([e for e in prof["exclusions"]
                          if e["kind"] == "group_policy"], [])

    def test_excluded_contacts_are_refused_and_the_refusal_is_counted(self):
        """Invariant 2: the exclude list and the soft delete are the owner's
        decisions about people, not a tunable for this module."""
        cid = self.contact("Anna Nilsson")
        other = self.contact("Bertil Berg")
        db.set_excluded(self.conn, cid, 1)
        self.conn.commit()
        self.seed(SV, contact_id=cid)
        self.seed(EN, contact_id=other)
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual(list(prof["buckets"]), ["en.new.chat"])
        self.assertEqual(prof["corpus"]["dropped"]["contact_excluded_in_crm"], 8)
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        (entry,) = [e for e in prof["exclusions"] if e["kind"] == "contact_excluded"]
        self.assertEqual(entry["count"], 8)
        self.assertIn("invariant 2", entry["why"])

    def test_the_census_counts_every_channel_including_excluded_ones(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid, channel="linkedin")
        self.seed(EN, contact_id=cid, channel="imessage")
        self.seed(SV, contact_id=cid, channel="whatsapp", direction=0)
        census = sp.profile(self.conn)["census"]
        self.assertTrue(census["readable"])
        self.assertEqual(census["messages_total"], 24)
        self.assertEqual(census["outbound_total"], 16)
        self.assertEqual(census["inbound_total"], 8)
        self.assertEqual(set(census["by_channel"]),
                         {"linkedin", "imessage", "whatsapp"})
        self.assertEqual(census["by_channel"]["linkedin"],
                         {"inbound": 0, "outbound": 8, "total": 8, "group_rows": 0})

    def test_provenance_is_published_for_every_channel_the_census_can_see(self):
        prof = sp.profile(self.conn)
        channels = prof["provenance"]["channels"]
        self.assertEqual(set(channels), set(sp.CHANNEL_PROVENANCE))
        for name, entry in channels.items():
            for key in ("direction", "how", "where", "false_outbound_risk", "notes"):
                self.assertTrue(entry[key], f"{name}.{key}")
        self.assertEqual(prof["provenance"]["unsafe_channels"], ["linkedin"])
        self.assertIs(prof["provenance"]["self_email_set"], False)
        self.assertIs(prof["provenance"]["self_name_set"], False)
        self.assertEqual(sp.report(prof)["provenance"], channels)


class TestCapSelectsTheOldestRows(ArcBase):
    """Pins observed behaviour so a change to it is deliberate: both passes are
    `ORDER BY id LIMIT ?` (crm/style_profile.py:1571-1572 and :1396), so the cap
    keeps the OLDEST eligible messages and leaves the newest unread. On a capped
    archive the profile therefore describes how the owner used to write."""

    def test_the_cap_keeps_the_lowest_ids(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid)      # inserted first → lowest ids
        self.seed(EN, contact_id=cid)      # newest
        os.environ["NC_STYLE_MAX_MESSAGES"] = "8"
        prof = sp.profile(self.conn)
        self.assertEqual(list(prof["buckets"]), ["sv.new.chat"])
        self.assertEqual(prof["corpus"]["outbound_scanned"], 8)
        self.assertEqual(prof["corpus"]["outbound_eligible"], 16)
        self.assertTrue(prof["corpus"]["outbound_capped"])
        self.assertEqual(prof["corpus"]["outbound_not_read"], 8)
        rep = sp.report(prof)
        said = " ".join(rep["sentences"])
        self.assertIn("Reading stopped at its limit of 8 messages", said)
        self.assertIn("8 more messages you sent were not read", said)
        self.assertNotIn("NC_STYLE", said, "a setting name in a sentence a person reads")
        self.assertEqual(rep["accounting"]["eligible_unexplained"], 0)


# ═══════════════════════════════════════════════════════════════════════════
# Determinism, storage, settings
# ═══════════════════════════════════════════════════════════════════════════
class TestDeterminism(ArcBase):
    def test_two_runs_over_the_same_corpus_are_identical(self):
        cid = self.contact("Anna Nilsson")
        other = self.contact("Bertil Berg")
        self.seed(SV + CODE_SWITCHED, contact_id=cid)
        self.seed(EN + [EMOJI, CJK, RTL], contact_id=other)
        self.seed(SV + EN, contact_id=cid, direction=0)
        first, second = sp.measure(self.conn), sp.measure(self.conn)
        # canon() first, so an ordering difference is reported as an ordering
        # difference rather than as a wall of unequal JSON.
        self.assertEqual(blob(canon(first)), blob(canon(second)))
        # Then raw: equal canonical forms with unequal raw forms is exactly the
        # iteration-order bug canon() exists to expose.
        self.assertEqual(blob(first), blob(second),
                         "the two runs agree on content but not on list order — "
                         "something in the profile iterates a set or a dict")
        d1 = sp.style_directive(sp.profile(self.conn), "sv.new.chat")
        d2 = sp.style_directive(sp.profile(self.conn), "sv.new.chat")
        self.assertEqual(d1, d2)

    def test_insertion_order_does_not_change_the_profile(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid)
        forward = sp.measure(self.conn)
        for key in ("generated_at",):
            forward.pop(key, None)

        # Rebuild the same corpus in the opposite order in a second archive.
        arc.ARCHIVE_DB_PATH = os.path.join(self.arc_dir, "reversed.db")
        arc.init_archive(arc.ARCHIVE_DB_PATH)
        conn = arc.get_archive_db(arc.ARCHIVE_DB_PATH)
        for i, body in enumerate(reversed(SV + EN)):
            arc.insert_message(conn, contact_id=cid, channel="imessage",
                               direction=1, ts=f"2026-02-{1 + i:02d} 08:00:00",
                               body=body, source_key=f"rev_{i}")
        conn.commit()
        conn.close()
        backward = sp.measure(self.conn)
        self.assertEqual(blob(canon(forward)), blob(canon(backward)))


class TestStorage(ArcBase):
    def test_load_on_a_fresh_db_returns_empty_rather_than_raising(self):
        result = sp.load(self.conn)
        self.assertFalse(result["present"])
        self.assertFalse(result["stale"])
        self.assertIsNone(result["version"])
        self.assertIn("no writing style profile", result["reason"])
        self.assertEqual(result["state_key"], sp.STATE_KEY)
        self.assertIn("own default voice", sp.style_directive(result, "sv.close.chat"))

    def test_save_load_roundtrip(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid)
        self.seed(SV, contact_id=cid, direction=0)
        prof = sp.profile(self.conn)
        receipt = sp.save(self.conn, prof)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["version"], sp.PROFILE_VERSION)
        self.assertEqual(receipt["buckets"], len(prof["buckets"]))
        self.assertGreater(receipt["bytes"], 0)

        back = sp.load(self.conn)
        self.assertTrue(back["present"])
        self.assertFalse(back["stale"])
        for key, value in prof.items():
            self.assertEqual(blob(back[key]), blob(value), f"{key} did not survive")
        # A directive built from the reloaded profile equals one built in memory.
        self.assertEqual(sp.style_directive(back, "sv.new.chat"),
                         sp.style_directive(prof, "sv.new.chat"))

    def test_a_corrupt_blob_is_reported_and_never_echoed(self):
        db.state_set(self.conn, sp.STATE_KEY, "{not json at all")
        self.conn.commit()
        with self.assertLogs(sp.log, "WARNING") as logged:
            result = sp.load(self.conn)
        self.assertNotIn("not json", " ".join(logged.output),
                         "the corrupt value must not be echoed into the log either")
        self.assertFalse(result["present"])
        self.assertIn("could not be read", result["reason"])
        self.assertNotIn("not json", blob(result))

    def test_a_non_object_blob_is_reported(self):
        db.state_set(self.conn, sp.STATE_KEY, json.dumps([1, 2, 3]))
        self.conn.commit()
        result = sp.load(self.conn)
        self.assertFalse(result["present"])
        self.assertIn("not an object", result["reason"])

    def test_a_version_mismatch_is_stale_not_silently_modern(self):
        db.state_set(self.conn, sp.STATE_KEY,
                     json.dumps({"version": sp.PROFILE_VERSION + 99, "buckets": {}}))
        self.conn.commit()
        result = sp.load(self.conn)
        self.assertTrue(result["present"])
        self.assertTrue(result["stale"])
        self.assertIn(str(sp.PROFILE_VERSION + 99), result["reason"])
        self.assertIn("measure again", result["reason"])


class TestSettingsAreReadAtCallTime(ArcBase):
    """The `api_import.import_timeout_sec` idiom: every tunable re-reads
    os.environ on every call, so a Settings edit applies to the next run instead
    of the next restart. No module is reimported anywhere in this class."""

    def setUp(self):
        super().setUp()
        self.cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=self.cid)
        self.seed(SV, contact_id=self.cid, direction=0)

    def test_each_reader_returns_its_named_default_when_unset(self):
        self.assertEqual(sp.max_messages(), sp.MAX_MESSAGES_DEFAULT)
        self.assertEqual(sp.min_chars_setting(), sp.MIN_CHARS_DEFAULT)
        self.assertEqual(sp.min_bucket(), sp.MIN_BUCKET_DEFAULT)
        self.assertEqual(sp.lang_margin(), sp.LANG_MARGIN_DEFAULT)
        self.assertEqual(sp.top_n(), sp.TOP_N_DEFAULT)
        self.assertEqual(sp.include_linkedin(), sp.INCLUDE_LINKEDIN_DEFAULT)
        self.assertEqual(sp.include_groups(), sp.INCLUDE_GROUPS_DEFAULT)

    def test_every_setting_changes_behaviour_between_two_calls(self):
        base = sp.profile(self.conn)
        self.assertEqual(base["caps"]["min_bucket"], sp.MIN_BUCKET_DEFAULT)
        self.assertEqual([k for k, v in base["buckets"].items() if v["usable"]], [])

        os.environ["NC_STYLE_MIN_BUCKET"] = "4"
        after = sp.profile(self.conn)
        self.assertEqual(after["caps"]["min_bucket"], 4)
        self.assertEqual(sorted(k for k, v in after["buckets"].items() if v["usable"]),
                         ["en.new.chat", "sv.new.chat"])

        os.environ["NC_STYLE_MAX_MESSAGES"] = "5"
        capped = sp.profile(self.conn)
        self.assertEqual(capped["caps"]["max_messages"], 5)
        self.assertEqual(capped["corpus"]["outbound_scanned"], 5)
        self.assertTrue(capped["corpus"]["outbound_capped"])
        os.environ.pop("NC_STYLE_MAX_MESSAGES")

        os.environ["NC_STYLE_MIN_CHARS"] = "500"
        floored = sp.profile(self.conn)
        self.assertEqual(floored["caps"]["min_chars"], 500)
        self.assertEqual(floored["corpus"]["used_for_style"], 0)
        self.assertEqual(floored["corpus"]["dropped"]["shorter_than_min_chars"], 16)
        os.environ.pop("NC_STYLE_MIN_CHARS")

        os.environ["NC_STYLE_TOP_N"] = "1"
        trimmed = sp.profile(self.conn)
        self.assertEqual(trimmed["caps"]["top_n"], 1)
        self.assertLessEqual(len(trimmed["overall"]["top_greetings"]["items"]), 1)
        os.environ.pop("NC_STYLE_TOP_N")

        os.environ["NC_STYLE_LANG_MARGIN"] = "0.99"
        strict = sp.profile(self.conn)
        self.assertEqual(strict["caps"]["lang_margin"], 0.99)

    def test_limit_and_min_chars_arguments_override_the_settings(self):
        os.environ["NC_STYLE_MAX_MESSAGES"] = "16"
        prof = sp.profile(self.conn, limit=3, min_chars=0)
        self.assertEqual(prof["caps"]["max_messages"], 3)
        self.assertEqual(prof["caps"]["min_chars"], 0)
        self.assertEqual(prof["corpus"]["outbound_scanned"], 3)

    def test_out_of_range_and_unparseable_values_clamp_to_the_default(self):
        for raw in ("abc", "", "not a number"):
            os.environ["NC_STYLE_MAX_MESSAGES"] = raw
            self.assertEqual(sp.max_messages(), sp.MAX_MESSAGES_DEFAULT, raw)
        os.environ["NC_STYLE_MAX_MESSAGES"] = "-5"
        self.assertEqual(sp.max_messages(), 1)
        os.environ["NC_STYLE_MAX_MESSAGES"] = "999999999"
        self.assertEqual(sp.max_messages(), 5_000_000)
        os.environ.pop("NC_STYLE_MAX_MESSAGES")
        for raw, expected in (("2.0", 1.0), ("-1", 0.0), ("abc", sp.LANG_MARGIN_DEFAULT)):
            os.environ["NC_STYLE_LANG_MARGIN"] = raw
            self.assertEqual(sp.lang_margin(), expected, raw)

    def test_the_boolean_settings_accept_the_usual_off_words(self):
        for raw in ("0", "no", "off", "false", "OFF", "False"):
            os.environ["NC_STYLE_INCLUDE_LINKEDIN"] = raw
            self.assertFalse(sp.include_linkedin(), raw)
        for raw in ("1", "yes", "on", "true"):
            os.environ["NC_STYLE_INCLUDE_LINKEDIN"] = raw
            self.assertTrue(sp.include_linkedin(), raw)

    def test_the_settings_in_force_travel_with_the_profile(self):
        os.environ["NC_STYLE_MIN_BUCKET"] = "4"
        os.environ["NC_STYLE_INCLUDE_GROUPS"] = "1"
        rep = sp.report(sp.profile(self.conn))
        self.assertEqual(rep["settings"]["min_bucket"], 4)
        self.assertTrue(rep["settings"]["include_groups"])
        self.assertTrue(rep["groups_included"])
        self.assertFalse(rep["linkedin_included"])


class TestReportIsTheHonestySurface(ArcBase):
    """`crm/ics_import.py report()` is the standard: everything that happened,
    as numbers a caller can render plus sentences a human can read."""

    def setUp(self):
        super().setUp()
        self.cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=self.cid)
        self.seed(SV, contact_id=self.cid, direction=0)
        self.prof = sp.profile(self.conn)
        self.rep = sp.report(self.prof)

    def test_the_three_accounting_identities_all_balance(self):
        acc = self.rep["accounting"]
        self.assertEqual(acc["unexplained"], 0,
                         "outbound_scanned != used + dropped")
        self.assertEqual(acc["eligible_unexplained"], 0,
                         "eligible != scanned + left by the cap")
        self.assertEqual(acc["available_unexplained"], 0,
                         "available != eligible + refused by policy")
        self.assertEqual(acc["explained"], acc["outbound_scanned"])

    def test_it_never_claims_a_network_call(self):
        self.assertTrue(self.rep["offline"])
        self.assertEqual(self.rep["network_calls"], 0)
        self.assertFalse(self.rep["wrote_anything"])
        self.assertIn("Nothing left this machine", self.rep["summary_text"])
        self.assertTrue(self.prof["offline"])
        self.assertEqual(self.prof["network_calls"], 0)

    def test_the_sentences_render_the_counts_with_correct_grammar(self):
        self.assertEqual(sp._n(1, "message"), "1 message")
        self.assertEqual(sp._n(0, "message"), "0 messages")
        self.assertEqual(sp._n(3, "bucket"), "3 buckets")
        self.assertTrue(all(isinstance(s, str) and s for s in self.rep["sentences"]))
        self.assertEqual(self.rep["summary_text"], " ".join(self.rep["sentences"]))

    def test_a_warning_sentence_appears_only_when_the_arithmetic_fails(self):
        self.assertNotIn("WARNING", self.rep["summary_text"])

    def test_the_safety_claim_travels_with_the_profile(self):
        safety = self.prof["safety"]
        self.assertFalse(safety["directive_contains_message_text"])
        self.assertFalse(safety["directive_contains_names"])
        self.assertIn("closed", safety["how"])
        self.assertTrue(self.prof["throughput_note"])
        self.assertEqual(self.prof["state_key"], sp.STATE_KEY)


class TestCli(ArcBase):
    """`--dry-run` measures, prints and writes NOTHING — the same split as every
    importer in this repo."""

    def _run(self, argv):
        """Drive `main()` against the test databases.

        `db.get_db` is patched on the module that OWNS it, never rebound as
        `sp.db = stub` — the repo's rule. It hands out a SECOND
        connection to the same file, because `main()` closes what it is given and
        tearDown still needs `self.conn`. The original is captured before the
        patch: `sp.db` IS `db`, so a lambda that called `db.get_db` would recurse
        into its own mock.
        """
        from unittest import mock
        import io
        import contextlib
        real_get_db = db.get_db
        out = io.StringIO()
        self.conn.commit()
        with mock.patch.object(sp.db, "get_db",
                              side_effect=lambda *a, **k: real_get_db(self.path)), \
             mock.patch.object(_sys, "argv", ["style_profile.py"] + argv), \
             contextlib.redirect_stdout(out):
            sp.main()
        return out.getvalue()

    def test_dry_run_prints_a_receipt_and_writes_nothing(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid)
        text = self._run(["--dry-run"])
        self.assertIn("Nothing was written", text)
        self.assertIn(sp.DIRECTIVE_HEADER, text)
        self.assertIsNone(db.state_get(self.conn, sp.STATE_KEY))

    def test_a_real_run_saves_and_says_where(self):
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid)
        text = self._run([])
        self.assertIn(sp.STATE_KEY, text)
        self.assertTrue(db.state_get(self.conn, sp.STATE_KEY))
        self.assertTrue(sp.load(self.conn)["present"])

    def test_json_output_is_parseable_and_leaks_nothing(self):
        cid = self.contact(LEAK_NAME)
        self.msg(LEAK_SENTENCE, contact_id=cid)
        self.seed(SV + EN, contact_id=cid)
        payload = json.loads(self._run(["--dry-run", "--json"]))
        self.assertEqual(set(payload), {"report", "saved", "directive"})
        self.assertEqual(payload["saved"], {"ok": False, "skipped": "dry run"})
        assert_no_leak(self, payload["directive"], "the CLI's --json directive")
        assert_no_leak(self, blob(payload["report"]), "the CLI's --json report")


# ═══════════════════════════════════════════════════════════════════════════
# NC-369 — the receipt must not say something false about what it measured
# ═══════════════════════════════════════════════════════════════════════════
class TestTheReceiptTellsTheTruth(ArcBase):
    """The two defects that used to be pinned here as expected failures, fixed on
    2026-10-17 (NC-369), and the cases next to them that the fix had to cover.

    A receipt that counts one message twice prints a negative number of
    "unaccounted for" messages, and a receipt for an archive that could not be
    read prints "0 archived messages", which reads as a clean empty result. Both
    are a number that says something the data does not.
    """

    def _old_archive(self):
        """A pre-group_name archive at a fresh path, so ArcBase's migrated one
        cannot mask it. Holds 16 outbound messages."""
        arc.ARCHIVE_DB_PATH = self.arc_path = os.path.join(self.arc_dir, "old.db")
        conn = sqlite3.connect(self.arc_path)
        conn.executescript(TestMigrationOnAnOldArchiveSchema.OLD_DDL)
        for i, body in enumerate(SV + EN):
            conn.execute(
                "INSERT INTO messages (contact_id, channel, direction, ts, body, "
                "source_key) VALUES (1, 'imessage', 1, ?, ?, ?)",
                (f"2026-01-{1 + i:02d} 09:00:00", body, f"old_{i}"))
        conn.commit()
        conn.close()

    # -- the message from someone with no row ---------------------------------
    def test_register_unknown_is_counted_as_dropped_while_being_kept(self):
        """WAS an expected failure; fixed 2026-10-17 (NC-369).

        It was: a message from someone with no row in crm.db was counted as
        dropped AND kept (register "unknown"), so `scanned == used + dropped`
        went negative and an ordinary archive printed "WARNING: -16 messages are
        unaccounted for. That is a bug in this module". It now has its own count,
        `kept_register_unknown`, that is part of `used` and not of `dropped`, so
        the arithmetic balances without clamping anything at zero.

        INPUT   any outbound archive row whose `contact_id` has no row in
                crm.db's `contacts` — an orphan left by `db.purge_contact`
                (which leaves archive.db to the caller).
        """
        self.seed(SV + EN, contact_id=999)      # 999 is not a contact
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertEqual(prof["corpus"]["used_for_style"], 16)
        self.assertEqual(prof["corpus"]["kept_register_unknown"], 16)
        self.assertNotIn("contact_not_in_crm_register_unknown",
                         prof["corpus"]["dropped"],
                         "a message that was kept is listed as set aside")
        self.assertEqual(prof["corpus"]["dropped_total"], 0)

        self.assertEqual(rep["accounting"]["unexplained"], 0,
                         "16 kept messages are also counted as dropped, so the "
                         "receipt reports a negative number of unaccounted-for "
                         "messages")
        self.assertEqual(rep["messages_dropped"], 0)
        self.assertEqual(rep["messages_kept_register_unknown"], 16)
        self.assertEqual(rep["accounting"]["kept_register_unknown"], 16)
        self.assertNotIn("WARNING", rep["summary_text"])
        self.assertIn("16 used for the statistics and 0 set aside",
                      rep["summary_text"])
        self.assertIn("16 of the 16 messages used went to people who are no "
                      "longer in your contacts", rep["summary_text"])

    def test_a_kept_unknown_message_and_a_set_aside_one_are_each_counted_once(self):
        """NC-369. The two counts must stay apart when both happen in one run:
        8 messages to someone who is gone are kept, 3 too-short ones to a real
        contact are set aside, and 8 ordinary ones are plainly used."""
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid)             # 8 used
        self.seed(EN, contact_id=999)             # 8 used, person not in crm.db
        self.seed(["ok", "ja", "k"], contact_id=cid)   # 3 below the minimum
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        corpus = prof["corpus"]
        self.assertEqual(corpus["outbound_scanned"], 19)
        self.assertEqual(corpus["used_for_style"], 16)
        self.assertEqual(corpus["kept_register_unknown"], 8)
        self.assertEqual(corpus["dropped_total"], 3)
        self.assertEqual(corpus["dropped"], {"shorter_than_min_chars": 3})
        self.assertEqual(rep["accounting"]["unexplained"], 0)
        kinds = {e["kind"]: e["count"] for e in rep["exclusions"]}
        self.assertEqual(kinds["register_unknown"], 8)
        self.assertEqual(kinds["too_short"], 3)

    def test_a_real_gap_in_the_arithmetic_is_still_shown_as_one(self):
        """NC-369. The fix balances the sum; it must not hide a real mismatch. A
        profile whose counts do not add up still says so, in plain words, and the
        number is not clamped to zero."""
        prof = sp.profile(self.conn)
        prof["corpus"]["outbound_scanned"] = 10
        prof["corpus"]["used_for_style"] = 7
        prof["corpus"]["dropped_total"] = 1
        rep = sp.report(prof)
        self.assertEqual(rep["accounting"]["unexplained"], 2)
        self.assertIn("2 messages read but neither used nor set aside",
                      rep["summary_text"])
        prof["corpus"]["used_for_style"] = 12
        rep = sp.report(prof)
        self.assertEqual(rep["accounting"]["unexplained"], -3,
                         "a double count must show as a negative, not as zero")
        self.assertIn("3 messages more than it read", rep["summary_text"])
        self.assertNotIn("-3", rep["summary_text"])

    # -- an archive that could not be read -------------------------------------
    def test_an_unreadable_archive_is_not_named_in_the_report_sentences(self):
        """WAS an expected failure; fixed 2026-10-17 (NC-369).

        It was: when the archive could not be read, the receipt said "0 archived
        messages in total: 0 marked as sent by you, 0 received" and "Vocabulary
        distinctiveness was not computed: archive.db is not present", for an
        archive that IS there and holds 16 messages, because nothing in the
        sentences ever rendered `degraded_reason`. A failed measurement read
        exactly like an empty one. The receipt now leads with the reason, shows no
        numbers at all, and `measured` is False.

        INPUT   an archive.db predating the `group_name` column, holding 16
                outbound messages. Reachable because `style_profile.main()` never
                calls `arc.init_archive`.
        """
        self._old_archive()
        with self.assertLogs(sp.log, "WARNING"):
            prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertTrue(prof["degraded"])
        self.assertTrue(rep["archive_present"])
        self.assertFalse(rep["measured"])

        text = rep["summary_text"]
        self.assertIn(prof["degraded_reason"], text,
                      "the receipt never tells the user the archive could not be "
                      f"read. It says: {text}")
        self.assertEqual(rep["sentences"][0], prof["degraded_reason"],
                         "the reason has to come before anything else")
        self.assertNotIn("archive.db is not present", text,
                         "the receipt claims the archive is absent when it is "
                         "present but unreadable")
        self.assertNotIn("archived message", text,
                         "a count of archived messages was printed for an archive "
                         "that could not be counted")
        self.assertNotIn("outbound message", text)
        self.assertIn("Nothing was measured", text)
        self.assertNotIn("archive.db", prof["vocabulary"]["reason"])

    def test_an_unreadable_archive_gives_the_data_page_no_chips_to_show(self):
        """NC-369. The Data page turns "N outbound messages read" and "N used for
        the statistics" in this receipt into "Read" and "Measured" chips. An
        archive that could not be read must produce neither: "Read 0" is a
        measurement that did not happen."""
        import importers
        self._old_archive()
        with self.assertLogs(sp.log, "WARNING"):
            rep = sp.report(sp.profile(self.conn))
        self.assertEqual(importers.parse_receipts(rep["summary_text"]), [])

    def test_the_drafting_instruction_for_an_unreadable_archive_is_whole_sentences(self):
        """NC-369. The reason is pasted after "No style profile is available:" in
        the instruction that goes to the drafter. It used to be a lowercase
        fragment with no full stop, so the next sentence ran into it."""
        self._old_archive()
        with self.assertLogs(sp.log, "WARNING"):
            prof = sp.profile(self.conn)
        directive = sp.style_directive(prof, "sv.close.chat")
        self.assertIn("nothing was measured. Write in your own default voice.",
                      directive)

    def test_an_unreadable_archive_on_the_command_line_says_so_first(self):
        """NC-369. The same receipt, through `main()`: the first line printed is
        the reason, and no "0 archived messages" line is printed."""
        self._old_archive()
        # Bound to the instance, so `TestCli`'s driver can be borrowed unchanged.
        with self.assertLogs(sp.log, "WARNING"):
            out = TestCli._run(self, ["--dry-run"])
        first = [ln.strip() for ln in out.splitlines() if ln.strip()][0]
        self.assertIn("could not be read", first)
        self.assertNotIn("archived message", out)

    def test_a_locked_exclude_list_is_named_not_reported_as_zero(self):
        """NC-369. The other way a profile refuses to build (NC-150): the list of
        hidden and deleted people could not be read. It used to print the same
        "0 archived messages" lines and a vocabulary reason that said the archive
        was absent, for an archive that was there."""
        self.seed(SV + EN, contact_id=1)

        class Locked:
            def execute(self, *a, **k):
                raise RuntimeError("database is locked")

        with self.assertLogs(sp.log, "WARNING"):
            prof = sp.profile(Locked())
        rep = sp.report(prof)
        self.assertTrue(prof["degraded"])
        self.assertFalse(rep["measured"])
        self.assertIn("hidden or deleted", rep["sentences"][0])
        self.assertIn("could not be read", rep["sentences"][0])
        self.assertNotIn("crm.db", rep["summary_text"])
        self.assertNotIn("archived message", rep["summary_text"])
        self.assertNotIn("archive.db", prof["vocabulary"]["reason"])

    # -- the words a person reads ----------------------------------------------
    def test_the_receipt_uses_no_setting_names_file_names_or_internal_words(self):
        """NC-369. One run that makes the receipt print every kind of sentence it
        has. None of them may name an environment variable, a file, or a word the
        code uses for itself ("bucket", "lexicon", "register"): the person cannot
        act on `NC_STYLE_MAX_MESSAGES` (it has no box) and does not know what a
        bucket is."""
        cid = self.contact("Anna Nilsson")
        self.seed(SV, contact_id=cid, channel="linkedin")     # left out by policy
        self.seed(SV + EN, contact_id=cid)
        self.seed(EN, contact_id=999)                         # a person who is gone
        self.seed(SV, contact_id=cid, channel="whatsapp", direction=0)  # nothing sent
        os.environ["NC_STYLE_MAX_MESSAGES"] = "20"            # so the limit bites
        rep = sp.report(sp.profile(self.conn))
        text = rep["summary_text"]
        for phrase in ("Reading stopped at its limit of 20 messages",
                       "LinkedIn messages are left out",
                       "no longer in your contacts",
                       "No group of messages holds",
                       "left out for having too few messages",
                       "WhatsApp has messages you received but none you sent",
                       "Nothing left this machine"):
            self.assertIn(phrase, text)
        words = ("NC_", "archive.db", "crm.db", "bucket", "lexicon", "register",
                 "COALESCE", "direction bit", "inbound", " API", "pass ")
        for word in words:
            self.assertNotIn(word, text)
        # The other branch: with a lower minimum the groups ARE big enough, and
        # the sentence that says so, and the one about the style words, are read.
        os.environ.pop("NC_STYLE_MAX_MESSAGES")
        os.environ["NC_STYLE_MIN_BUCKET"] = "2"
        text = sp.report(sp.profile(self.conn))["summary_text"]
        self.assertIn("3 of the 3 message groups found", text)
        self.assertIn("A group is one language, one level of closeness", text)
        self.assertIn("The words you reach for", text)
        for word in words:
            self.assertNotIn(word, text)

    def test_the_data_pages_chips_still_read_the_counts_from_the_receipt(self):
        """NC-369. The words were reworked; the two sentences the page reads its
        "Read" and "Measured" chips from were not. Pinned here so the next edit
        notices before the chips go blank."""
        import importers
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid)
        rep = sp.report(sp.profile(self.conn))
        self.assertEqual(importers.parse_receipts(rep["summary_text"]),
                         [{"verb": "Read", "count": 16},
                          {"verb": "Measured", "count": 16}])

    # -- a pass that stopped part way ------------------------------------------
    def test_a_pass_that_stopped_part_way_says_so_and_blames_no_cap(self):
        """NC-369. Numbers from a pass that stopped early were shown with no
        sentence saying so, and the receipt then blamed the message cap for the
        missing messages. It now says reading stopped, says how many messages the
        numbers cover, and the cap sentence stays out of it. The message being
        read when it failed is counted as set aside, so the arithmetic still
        balances and no "bug" warning appears."""
        from unittest import mock
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid)
        real = sp._clean_body
        calls = {"n": 0}

        def flaky(body):
            calls["n"] += 1
            if calls["n"] == 5:                 # the 5th outbound message
                raise RuntimeError("disk error")
            return real(body)

        with mock.patch.object(sp, "_clean_body", side_effect=flaky), \
                self.assertLogs(sp.log, "WARNING"):
            prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertTrue(prof["degraded"])
        self.assertTrue(rep["measured"])
        self.assertEqual(rep["sentences"][0], prof["degraded_reason"])
        self.assertIn("stopped part way", rep["sentences"][0])
        self.assertIn("covers only 5 messages", rep["sentences"][0])
        self.assertTrue(rep["outbound_stopped_early"])
        self.assertFalse(rep["outbound_capped"])
        self.assertNotIn("-message cap", rep["summary_text"])
        self.assertEqual(rep["accounting"]["unexplained"], 0,
                         "the message being read when it stopped is missing from "
                         "the arithmetic")
        self.assertNotIn("WARNING", rep["summary_text"])
        self.assertEqual(prof["corpus"]["dropped"], {"stopped_on_this_message": 1})
        kinds = [e["kind"] for e in rep["exclusions"]]
        self.assertIn("stopped_midway", kinds)

    def test_a_finished_pass_is_still_reported_as_measured(self):
        """NC-369. And the happy path is unchanged: a normal archive is measured,
        carries no reason sentence, and an older saved profile that has no
        `pass_ran` marker is still read as measured."""
        cid = self.contact("Anna Nilsson")
        self.seed(SV + EN, contact_id=cid)
        prof = sp.profile(self.conn)
        rep = sp.report(prof)
        self.assertTrue(rep["measured"])
        self.assertFalse(prof["degraded"])
        self.assertTrue(rep["sentences"][0].startswith("16 archived messages"))
        del prof["corpus"]["pass_ran"]           # a profile saved before NC-369
        self.assertTrue(sp.report(prof)["measured"])


if __name__ == "__main__":
    unittest.main()
