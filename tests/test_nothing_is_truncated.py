"""NC-142 / NC-143: what arrives is what is stored, and Ask can read all of it.

THE BUG these guard: imported text had a length limit for exactly one channel,
and it was silent:

    Stored emails stopped at a 5,000-character ceiling. They stopped
    mid-sentence and nothing, anywhere, said so.

Both email importers had their own copy of the same extractor, and each threw
away four different things without recording any of it: everything past 5,000
characters; every line beginning with ">" (which is not only the duplicated
thread below a reply, but anyone answering *inside* the quote); everything after
a "--" signature separator; and the entire message when it had no text/plain
part, so an HTML-only mail stored its subject and nothing else.

The decision: everything should always be read.

The second half is the reading end. Every tool in chat.py returns a window — 220
characters around a match, 150 of a neighbour — which is right for iMessage
(a message is rarely longer than the window) and wrong for email (the average
mail is far longer). Ask could see that a mail existed and not what it
said, with no way to ask for more. read_message is that way.

WHAT THESE GUARD

  * no cap survives in either importer, at any length
  * a quoted line, and text below a signature, are kept — these are content,
    not formatting
  * an HTML-only email yields its words instead of nothing
  * a re-import REPAIRS a body an older run truncated, and can never shorten
    one: longer wins, so a regressed importer cannot destroy history
  * an unchanged body writes nothing at all, which is what keeps a second run
    free of a full-text re-index
  * read_message returns the whole thing, pages through a very long one, and
    refuses a hidden contact
  * a search result carries its id only when there is genuinely more to read —
    an id on a complete row is wasted tokens (NC-127)
"""
import os
import shutil
import sys
import tempfile
import unittest
from email.message import EmailMessage
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, arc, chat_mod as chat, db, ei  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import email_text                      # noqa: E402
import gmail_import as gi              # noqa: E402


# A body longer than any cap that ever existed here, and longer than the window.
LONG = ("Sentence number %d, with enough words in it to be worth reading. " % 0)
BULK = "".join(f"Paragraph {i}. The quick brown fox jumps over the lazy dog. "
               for i in range(400))          # ~22,000 characters


def _b64(text: str) -> str:
    import base64
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


class TestTheImportersKeepWhatArrived(unittest.TestCase):
    """Both extractors, held to the same contract, because the bug was that
    each had its own copy of it."""

    def gmail(self, text, mime="text/plain"):
        return gi._extract_body({"mimeType": mime, "body": {"data": _b64(text)}})

    def mbox(self, text, subtype="plain"):
        m = EmailMessage()
        m["From"] = "someone@example.com"
        m.set_content(text, subtype=subtype)
        return ei._extract_body(m)

    # ── the cap ────────────────────────────────────────────────────────────
    def test_gmail_keeps_a_very_long_body(self):
        got = self.gmail(BULK)
        self.assertGreater(len(got), 20000,
                           "the 5,000-character cap is back; long real "
                           "emails were cut by it")
        self.assertIn("Paragraph 399.", got, "the end of the message is missing")

    def test_mbox_keeps_a_very_long_body(self):
        got = self.mbox(BULK)
        self.assertGreater(len(got), 20000)
        self.assertIn("Paragraph 399.", got)

    # ── the three silent drops ─────────────────────────────────────────────
    def test_a_quoted_line_is_content_not_formatting(self):
        """The case that makes this more than tidiness: answering inline."""
        raw = ("> Are you free on Tuesday?\n"
               "Yes, Tuesday works.\n"
               "> And can you bring the contract?\n"
               "I will print it tonight.\n")
        for name, got in (("gmail", self.gmail(raw)), ("mbox", self.mbox(raw))):
            self.assertIn("Are you free on Tuesday?", got,
                          f"{name} dropped the question that was answered")
            self.assertIn("bring the contract", got, f"{name} dropped a question")
            self.assertIn("I will print it tonight", got)

    def test_text_below_a_signature_survives(self):
        raw = "The report is attached.\n--\nAnna\n\nPS: the deadline moved to the 9th."
        for name, got in (("gmail", self.gmail(raw)), ("mbox", self.mbox(raw))):
            self.assertIn("deadline moved", got,
                          f"{name} stopped at the signature separator and lost "
                          "the postscript")

    def test_an_html_only_email_yields_its_words(self):
        html = ("<html><head><style>.x{color:red}</style></head><body>"
                "<p>Hello Anna,</p><p>The invoice is <b>overdue</b>.</p>"
                "<script>track()</script></body></html>")
        got = self.gmail(html, mime="text/html")
        self.assertIn("Hello Anna", got, "an HTML-only email stored nothing")
        self.assertIn("overdue", got)
        self.assertNotIn("color:red", got, "the stylesheet became message text")
        self.assertNotIn("track()", got, "the script became message text")

    def test_plain_text_still_wins_over_html(self):
        """A multipart/alternative carries both; the plain part is the readable
        one and must be preferred, or every mail arrives as converted markup."""
        got = gi._extract_body({
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/html",
                       "body": {"data": _b64("<p>html version</p>")}},
                      {"mimeType": "text/plain",
                       "body": {"data": _b64("plain version")}}]})
        self.assertEqual(got, "plain version")

    def test_an_attached_text_file_is_not_the_message(self):
        got = gi._extract_body({
            "mimeType": "multipart/mixed",
            "parts": [{"mimeType": "text/plain", "filename": "notes.txt",
                       "body": {"data": _b64("ATTACHMENT CONTENT")}},
                      {"mimeType": "text/plain",
                       "body": {"data": _b64("the actual message")}}]})
        self.assertEqual(got, "the actual message")

    def test_malformed_html_does_not_raise(self):
        """An importer that dies on one bad message imports nothing."""
        for junk in ("<p>unclosed <b>bold", "<<>><p", "", "<!-- -->"):
            email_text.html_to_text(junk)   # must not raise


class TestWhitespaceIsTidiedAndNothingElse(unittest.TestCase):
    def test_runs_of_blank_lines_collapse(self):
        self.assertEqual(email_text.tidy("a\n\n\n\n\nb"), "a\n\nb")

    def test_a_line_is_never_dropped_for_what_it_says(self):
        for line in ("> quoted", "--", "—", "  ", "PS: later"):
            kept = email_text.tidy(f"before\n{line}\nafter")
            self.assertIn("after", kept,
                          f"tidy() stopped at {line!r} — it normalises "
                          "formatting and must never judge content")


class RepairBase(Base):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp(prefix="ncrm-trunc-")
        self.p = os.path.join(self.d, "archive.db")
        arc.init_archive(self.p)
        self.ac = arc.get_archive_db(self.p)
        self._patch = mock.patch.object(arc, "ARCHIVE_DB_PATH", self.p)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.ac.close()
        shutil.rmtree(self.d, True)
        super().tearDown()

    def put(self, cid, body, key="k1", ts="2026-01-01 10:00:00", channel="email",
            subject=""):
        out = arc.insert_message(self.ac, contact_id=cid, channel=channel,
                                 direction=0, ts=ts, body=body,
                                 subject=subject, source_key=key)
        self.ac.commit()      # read_message opens its own connection
        return out

    def stored(self, key="k1"):
        return self.ac.execute(
            "SELECT body FROM messages WHERE source_key = ?", (key,)
        ).fetchone()["body"]


class TestAReImportRepairsATruncatedBody(RepairBase):
    """The migration path for the rows already cut. INSERT OR IGNORE alone
    would leave every one of them truncated forever, because the row exists."""

    def setUp(self):
        super().setUp()
        self.cid = db.create_contact(self.conn, "Anna Andersson")

    def test_a_longer_body_replaces_the_truncated_one(self):
        self.put(self.cid, "This email was cut off right he")
        self.put(self.cid, "This email was cut off right here — and here is the rest.")
        self.assertIn("here is the rest", self.stored(),
                      "a re-import left the truncated body in place, so "
                      "already-cut emails could never be repaired")

    def test_a_shorter_body_can_never_shorten_history(self):
        """Longer-wins, not newest-wins. A regressed importer, or a source that
        only offers a preview of something it once gave in full, must not be
        able to destroy text that cannot be fetched again."""
        self.put(self.cid, "The full message, every word of it, preserved.")
        self.put(self.cid, "The full message")
        self.assertEqual(self.stored(), "The full message, every word of it, preserved.")

    def test_an_unchanged_body_writes_nothing(self):
        """What keeps a second import free: no UPDATE, so no full-text re-index
        across a large archive."""
        self.put(self.cid, "Unchanged.")
        before = self.ac.total_changes
        self.put(self.cid, "Unchanged.")
        self.assertEqual(self.ac.total_changes - before, 0,
                         "re-importing an identical message wrote to the "
                         "database, which re-indexes it for nothing")

    def test_an_empty_body_never_wipes_a_stored_one(self):
        self.put(self.cid, "Real content.")
        self.put(self.cid, "")
        self.assertEqual(self.stored(), "Real content.")

    def test_the_first_insert_still_reports_itself_as_new(self):
        self.assertTrue(self.put(self.cid, "first", key="a"))
        self.assertFalse(self.put(self.cid, "first", key="a"))


class TestAskCanReadAWholeMessage(RepairBase):
    """NC-143. The window is a cost control, not a limit on what is knowable."""

    def setUp(self):
        super().setUp()
        self.cid = db.create_contact(self.conn, "Anna Andersson")
        self.put(self.cid, BULK, key="long")
        self.mid = self.ac.execute(
            "SELECT id FROM messages WHERE source_key = 'long'").fetchone()["id"]

    def test_it_returns_the_whole_message(self):
        got = chat._read_message(self.conn, self.mid)
        self.assertNotIn("error", got)
        self.assertEqual(got["total_chars"], len(BULK))
        self.assertIn("Paragraph 0.", got["text"])
        self.assertGreater(len(got["text"]), chat._MSG_SNIPPET * 10,
                           "read_message returned a window, which is the thing "
                           "it exists to escape")

    def test_a_message_longer_than_one_call_says_where_to_continue(self):
        """Bounded, and honest about it — the opposite of the importer's cap,
        which cut silently and offered no way to the rest."""
        got = chat._read_message(self.conn, self.mid)
        if got["total_chars"] > chat._READ_MAX:
            self.assertIn("continues_at_offset", got)
            rest = chat._read_message(self.conn, self.mid,
                                      got["continues_at_offset"])
            self.assertIn("Paragraph 399.", rest["text"],
                          "paging to the offset did not reach the end")
        else:
            self.assertNotIn("continues_at_offset", got)

    def test_a_short_message_never_claims_to_continue(self):
        self.put(self.cid, "Short one.", key="s")
        sid = self.ac.execute(
            "SELECT id FROM messages WHERE source_key = 's'").fetchone()["id"]
        self.assertNotIn("continues_at_offset", chat._read_message(self.conn, sid))

    def test_a_hidden_contact_is_refused(self):
        """Every other tool honours the hidden list; a tool that reads by raw id
        would otherwise be the way around it."""
        hid = db.create_contact(self.conn, "Hidden Person")
        db.set_excluded(self.conn, hid, 1)
        self.put(hid, "private", key="h")
        h = self.ac.execute(
            "SELECT id FROM messages WHERE source_key = 'h'").fetchone()["id"]
        self.assertIn("error", chat._read_message(self.conn, h))

    def test_a_nonsense_id_explains_itself(self):
        for bad in ("abc", None, 999999):
            out = chat._read_message(self.conn, bad)
            self.assertIn("error", out)
            self.assertTrue(out["error"].strip())

    def test_the_tool_is_reachable_through_the_dispatch(self):
        """A function nothing routes to is not a feature — the NC-100 trap."""
        out = chat.run_tool(self.conn, "read_message", {"id": self.mid})
        self.assertEqual(out.get("total_chars"), len(BULK))

    def test_it_is_declared_to_the_model(self):
        names = {t["name"] for t in chat.TOOLS}
        self.assertIn("read_message", names,
                      "the implementation exists and the model is never told")
        spec = next(t for t in chat.TOOLS if t["name"] == "read_message")
        self.assertIn("id", spec["input_schema"]["properties"])
        self.assertEqual(spec["input_schema"]["required"], ["id"])


class TestTheIdRidesAlongOnlyWhenThereIsMoreToRead(RepairBase):
    def setUp(self):
        super().setUp()
        self.cid = db.create_contact(self.conn, "Anna Andersson")

    def _search(self, **kw):
        return chat._search_messages(self.conn, **kw)

    def test_a_cut_result_carries_its_id_and_its_length(self):
        self.put(self.cid, "findme " + BULK, key="big", channel="email")
        out = self._search(query="findme", context=0)
        hit = out["messages"][0]
        self.assertIn("id", hit, "the model is shown a window with no way to "
                                 "ask for the rest")
        self.assertGreater(hit["full_chars"], chat._MSG_SNIPPET)
        self.assertEqual(chat._read_message(self.conn, hit["id"])["total_chars"],
                         hit["full_chars"])

    def test_a_complete_result_carries_neither(self):
        """NC-127: field names on rows nobody needs to follow up are wasted
        space in a result."""
        self.put(self.cid, "findme and that is the whole message", key="small")
        hit = self._search(query="findme", context=0)["messages"][0]
        self.assertNotIn("id", hit)
        self.assertNotIn("full_chars", hit)


class TestAQuotaPauseIsNotALostMessage(unittest.TestCase):
    """NC-142's repair pass, and the rate limiting that nearly hid from it.

    A full scan could lose message bodies to Gmail's per-minute quota, print a
    warning for each, and carry on — text that did not arrive, with nothing
    remembering that it had not. That is NC-142's own defect wearing a different
    coat, so the retry is part of the same fix rather than a nicety.
    """

    REAL_403 = ("<HttpError 403 ... returned \"Quota exceeded for quota metric "
                "'Total Query Cost' and limit 'Units per minute per user' of "
                "service 'gmail.googleapis.com'\". Details: \"[{'message': ..., "
                "'reason': 'rateLimitExceeded'}]\">")

    def test_the_real_quota_error_is_recognised(self):
        self.assertTrue(gi._is_rate_limited(Exception(self.REAL_403)))
        self.assertTrue(gi._is_rate_limited(Exception("429 Too Many Requests")))
        self.assertTrue(gi._is_rate_limited(Exception("userRateLimitExceeded")))

    def test_a_real_failure_is_not_mistaken_for_one(self):
        """A retry loop that swallows a genuine error is worse than no loop."""
        for other in ("404 Not Found", "invalid_grant", "permission denied",
                      "insufficient authentication scopes"):
            self.assertFalse(gi._is_rate_limited(Exception(other)), other)

    def test_it_waits_and_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception(self.REAL_403)
            return "the body"

        with mock.patch.object(gi.time, "sleep") as slept:
            self.assertEqual(gi._with_retry(flaky), "the body")
        self.assertEqual(calls["n"], 3, "it gave up instead of waiting")
        self.assertEqual(slept.call_count, 2)
        # Full jitter: identical backoffs make a hundred callers return together
        # and be refused together.
        self.assertTrue(all(0 <= c.args[0] for c in slept.call_args_list))

    def test_a_non_quota_error_is_raised_immediately(self):
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise ValueError("404 Not Found")

        with mock.patch.object(gi.time, "sleep"):
            with self.assertRaises(ValueError):
                gi._with_retry(broken)
        self.assertEqual(calls["n"], 1, "it retried something that will never "
                                        "succeed, delaying a real error")

    def test_it_eventually_gives_up_rather_than_looping_forever(self):
        def always():
            raise Exception(self.REAL_403)

        with mock.patch.object(gi.time, "sleep"):
            with self.assertRaises(Exception):
                gi._with_retry(always)


class TestTheRepairPassFindsItsOwnVictims(RepairBase):
    """The candidate query, which is the part that decides what gets re-fetched.

    The ordinary import can only ask Gmail for mail matching a known address or
    a known display name. Mail left after a full scan can belong to a contact
    whose mail arrives from an address the CRM does not hold, so no amount of
    re-running the normal import would ever reach it. Every stored row already
    carries its Gmail id, so the repair asks for exactly those and needs no
    matching at all.
    """

    def setUp(self):
        super().setUp()
        self.cid = db.create_contact(self.conn, "Anna Andersson")

    def _candidates(self):
        """Calls the real selector rather than re-running its SQL by hand, so a
        broken query fails the test."""
        return {r["source_key"] for r in gi.truncated_rows(self.ac)}

    def test_a_message_at_the_old_ceiling_is_a_candidate(self):
        self.put(self.cid, "x" * gi.OLD_CAP, key="gmail_aaa")
        self.assertIn("gmail_aaa", self._candidates())

    def test_the_window_covers_a_subject_prepended_body(self):
        """Email bodies are stored as subject + blank line + body, so a cut one
        lands slightly ABOVE the cap, as it does in practice."""
        self.put(self.cid, "x" * (gi.OLD_CAP + 150), key="gmail_bbb")
        self.assertIn("gmail_bbb", self._candidates())

    def test_a_short_message_is_left_alone(self):
        self.put(self.cid, "short", key="gmail_ccc")
        self.assertNotIn("gmail_ccc", self._candidates())

    def test_an_already_repaired_message_is_left_alone(self):
        """Idempotence. A second pass that fetches all the remaining ones and
        repairs none is what proves they are genuinely about five thousand
        characters long rather than cut."""
        self.put(self.cid, "x" * 20000, key="gmail_ddd")
        self.assertNotIn("gmail_ddd", self._candidates())

    def test_a_message_from_another_importer_is_never_touched(self):
        """A .mbox row has no Gmail id, so asking Gmail for it is meaningless."""
        self.put(self.cid, "x" * gi.OLD_CAP, key="a" * 32)
        self.assertNotIn("a" * 32, self._candidates())


if __name__ == "__main__":
    unittest.main()
