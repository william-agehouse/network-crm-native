"""Ten fixes to Ask, the import sources and the unanswered-messages
read, each pinned by a test that fails without its fix.

NC-346  Ask with no key finds someone by their city when no metro is saved
NC-300  a failed AI call leaves a line in the log, not only in the reply
NC-327  Ask defaults to Opus 5.5 and shows Opus 5.5's prices
NC-328  mail from or to any of your own addresses has the right direction
NC-348  Show in Finder tries every command before it gives up
NC-351  the Data page's three source checks are remembered until something changes
NC-355  choosing the Google file cannot leave the engine waiting on macOS
NC-303  the Google calendar row takes several secret addresses
NC-224  WhatsApp photos are saved again only when their file changed
NC-227  the unanswered-messages read is answered from an index
"""

import json
import mailbox
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from email.mime.text import MIMEText
from http import HTTPStatus
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, app, arc, config, db, ei, feed, importers, settings_mod,
)

import api_gmail  # noqa: E402
import api_import  # noqa: E402
import chat  # noqa: E402
import ics_import as ics  # noqa: E402
import photos as ph  # noqa: E402
import whatsapp_photos as wap  # noqa: E402
from api_core import ApiError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── NC-346 ───────────────────────────────────────────────────────────────────

class TestAskFindsSomeoneByCity(Base):
    """The keyword Ask matched a place on the metro alone."""

    def _person(self, name, city="", metro=""):
        cid = db.create_contact(self.conn, name)
        if city:
            db.set_field(self.conn, cid, "home_city", city)
        if metro:
            db.set_field(self.conn, cid, "home_metro", metro)
        self.conn.commit()
        return cid

    def _who_is_in_london(self):
        out = app.api_ask.handle_ask(self.conn, {"query": "Who is in London?"})
        return {c["id"]: c for c in out["contacts"]}

    def test_a_person_whose_city_is_london_and_metro_is_empty_is_listed(self):
        ada = self._person("Ada Example", city="London")
        found = self._who_is_in_london()
        self.assertIn(ada, found)
        self.assertEqual(found[ada]["reason"].split(" · ")[0], "Based in London")

    def test_a_person_whose_metro_is_london_is_still_listed(self):
        grace = self._person("Grace Example", metro="London")
        self.assertIn(grace, self._who_is_in_london())

    def test_someone_in_another_city_is_not_listed(self):
        alan = self._person("Alan Example", city="Paris", metro="Paris")
        self.assertNotIn(alan, self._who_is_in_london())


# ── NC-300 ───────────────────────────────────────────────────────────────────

class TestAFailedAiCallLeavesALogLine(Base):

    def setUp(self):
        super().setUp()
        self._key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
        self.addCleanup(self._restore)

    def _restore(self):
        if self._key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._key

    def test_the_kind_of_error_and_its_message_reach_the_log(self):
        def down(conn, messages, **_kw):
            try:
                raise OSError("name resolution failed")
            except OSError as inner:
                raise ConnectionError("Connection error.") from inner

        question = "Which founder do I know called Zebediah Quillfeather?"
        with mock.patch.object(app.api_ask.chat, "chat", down), \
                self.assertLogs("networkcrm", level="WARNING") as logged:
            out = app.api_ask.handle_chat(self.conn, {"message": question})
        text = "\n".join(logged.output)
        self.assertIn("ConnectionError", text)
        self.assertIn("Connection error.", text)
        self.assertIn("caused by OSError", text)
        # the reply still degrades exactly as before
        self.assertIn("AI chat was unavailable", out["reply"])

    def test_the_question_is_not_written_to_the_log(self):
        def down(conn, messages, **_kw):
            raise RuntimeError("the service said no")

        question = "Which founder do I know called Zebediah Quillfeather?"
        with mock.patch.object(app.api_ask.chat, "chat", down), \
                self.assertLogs("networkcrm", level="WARNING") as logged:
            app.api_ask.handle_chat(self.conn, {"message": question})
        self.assertNotIn("Zebediah", "\n".join(logged.output))


# ── NC-327 ───────────────────────────────────────────────────────────────────

class TestAskDefaultsToOpusFivePointFive(unittest.TestCase):

    def test_a_fresh_install_runs_opus_5_5_at_its_prices(self):
        """In a clean process, with nothing set: the model the engine will use
        and the prices it will show under the answer."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_MODEL", "NC_PRICE_IN_PER_MTOK",
                            "NC_PRICE_OUT_PER_MTOK")}
        empty = tempfile.mkdtemp(prefix="nc327_")
        env["CRM_DATA_DIR"] = empty
        env["CRM_SECRETS_DIR"] = empty           # no .env, so nothing is set
        code = ("import sys; sys.path.insert(0, %r); import chat; "
                "print(chat.MODEL, chat.price_in(), chat.price_out())"
                % os.path.join(REPO, "crm"))
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                             text=True, env=env, cwd=empty, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["claude-opus-5-5", "4.0", "20.0"])

    def test_the_settings_page_shows_the_same_model_and_prices(self):
        keys = settings_mod.MANAGED_KEYS
        self.assertEqual(keys["ANTHROPIC_MODEL"]["default"], "claude-opus-5-5")
        self.assertEqual(float(keys["NC_PRICE_IN_PER_MTOK"]["default"]),
                         chat.PRICE_IN_DEFAULT)
        self.assertEqual(float(keys["NC_PRICE_OUT_PER_MTOK"]["default"]),
                         chat.PRICE_OUT_DEFAULT)

    def test_a_million_tokens_each_way_costs_what_opus_5_5_costs(self):
        spend = chat._Spend()
        spend.add(mock.Mock(usage=mock.Mock(input_tokens=1_000_000,
                                            output_tokens=1_000_000,
                                            cache_read_input_tokens=0,
                                            cache_creation_input_tokens=0)))
        with mock.patch.dict(os.environ):
            os.environ.pop("NC_PRICE_IN_PER_MTOK", None)
            os.environ.pop("NC_PRICE_OUT_PER_MTOK", None)
            report = spend.report()
        self.assertEqual(report["usd"], 24.0)


# ── NC-328 ───────────────────────────────────────────────────────────────────

class TestSeveralOwnAddresses(unittest.TestCase):
    """Mail from or to a second address of yours has the right direction."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc328_")
        self.mbox_path = os.path.join(self.dir, "mail.mbox")
        self._orig = ei.SELF_EMAIL
        self.addCleanup(setattr, ei, "SELF_EMAIL", self._orig)

    def _msg(self, sender, to, body, msg_id):
        m = MIMEText(body)
        m["From"], m["To"] = sender, to
        m["Date"] = "Mon, 04 May 2026 09:00:00 +0000"
        m["Subject"] = "hello"
        m["Message-ID"] = msg_id
        return m

    def _write(self):
        box = mailbox.mbox(self.mbox_path, create=True)
        # Ada writes to the second address; the second address answers her.
        box.add(self._msg("Ada Lovelace <ada@example.com>", "me.work@example.com",
                          "Sent to my work address", "<in@test>"))
        box.add(self._msg("me.work@example.com", "ada@example.com",
                          "Answered from my work address", "<out@test>"))
        box.flush()
        box.close()

    def test_the_addresses_are_split_on_commas_semicolons_and_spaces(self):
        for written in ("me@example.com, me.work@example.com",
                        "me@example.com;me.work@example.com",
                        "ME@example.com  me.work@example.com"):
            with self.subTest(written=written):
                ei.SELF_EMAIL = written
                self.assertEqual(ei.self_addresses(),
                                 {"me@example.com", "me.work@example.com"})
        ei.SELF_EMAIL = ""
        self.assertEqual(ei.self_addresses(), frozenset())

    def test_mail_to_and_from_the_second_address_counts_in_both_directions(self):
        self._write()
        ei.SELF_EMAIL = "me@example.com, me.work@example.com"
        agg = ei.aggregate_mbox(self.mbox_path)
        self.assertEqual((agg["ada@example.com"].inbound,
                          agg["ada@example.com"].outbound), (1, 1))
        self.assertNotIn("me.work@example.com", agg,
                         "your own second address was counted as a stranger")
        self.assertIn("ada@example.com", ei.qualifying_addresses(agg))

    def test_the_calendar_import_knows_every_address_too(self):
        """An invite sent to your second address is you, not a guest."""
        with mock.patch.dict(os.environ, {"SELF_EMAIL": "me@example.com, Me.Work@example.com",
                                          "SELF_NAME": ""}):
            emails, norm = ics._self_identity()
        self.assertTrue(ics._is_self({"email": "me.work@example.com", "name": "x"}, emails, norm))
        self.assertTrue(ics._is_self({"email": "me@example.com", "name": "x"}, emails, norm))
        self.assertFalse(ics._is_self({"email": "ada@example.com", "name": "Ada"}, emails, norm))
        self.assertFalse(ics._is_self({"email": "", "name": "Ada"}, emails, norm),
                         "an attendee with no address matched an empty own address")

    def test_the_settings_box_takes_a_list(self):
        """It refused anything but one address, so the list could only be typed
        into the settings file by hand."""
        from tests.helpers import js_code_of
        src = js_code_of(os.path.join(REPO, "static", "app", "ledger.js"))
        rule = src[src.index("SELF_EMAIL:"):src.index("ANTHROPIC_API_KEY:")]
        self.assertIn(".split(/[,;\\s]+/)", rule)
        self.assertIn(".every(", rule)

    def test_with_only_the_first_address_the_same_mailbox_misses_the_exchange(self):
        """The fault, kept as the contrast: one address was all it knew."""
        self._write()
        ei.SELF_EMAIL = "me@example.com"
        agg = ei.aggregate_mbox(self.mbox_path)
        self.assertNotIn("ada@example.com", ei.qualifying_addresses(agg))

    def test_the_archived_messages_carry_the_right_direction(self):
        self._write()
        ei.SELF_EMAIL = "me@example.com, me.work@example.com"
        arc_path = os.path.join(self.dir, "archive.db")
        done = ei.archive_messages(self.mbox_path, {"ada@example.com": 99},
                                   archive_path=arc_path)
        self.assertEqual(done["new"], 2)
        conn = arc.get_archive_db(arc_path)
        try:
            by_body = {m["body"].strip(): m["direction"]
                       for m in arc.get_messages(conn, contact_id=99)}
        finally:
            conn.close()
        self.assertEqual(by_body["Answered from my work address"], 1)   # sent
        self.assertEqual(by_body["Sent to my work address"], 0)         # received


# ── NC-348 ───────────────────────────────────────────────────────────────────

class TestShowInFinderTriesEveryCommand(unittest.TestCase):

    def _target(self):
        return {"label": "Open the Messages folder",
                "argv": [["open", "-R", "/x/chat.db"], ["open", "/x"]]}

    def _run(self, first):
        calls = []

        def fake_run(argv, **_kw):
            calls.append(argv)
            if len(calls) == 1:
                if isinstance(first, Exception):
                    raise first
                return subprocess.CompletedProcess(argv, first)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(importers, "resolve_open_target",
                               return_value=self._target()), \
                mock.patch.object(api_import.subprocess, "run", fake_run):
            return api_import.handle_import_open({"source": "imessage"}), calls

    def test_the_folder_opens_when_the_first_command_is_missing(self):
        out, calls = self._run(FileNotFoundError("open"))
        self.assertEqual(out, {"ok": True})
        self.assertEqual(calls[1], ["open", "/x"])

    def test_the_folder_opens_when_the_first_command_hangs(self):
        out, calls = self._run(subprocess.TimeoutExpired("open", 15))
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_the_folder_opens_when_the_first_command_is_refused(self):
        out, calls = self._run(1)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_when_every_command_fails_the_answer_says_so_in_a_sentence(self):
        with mock.patch.object(importers, "resolve_open_target",
                               return_value=self._target()), \
                mock.patch.object(api_import.subprocess, "run",
                                  side_effect=FileNotFoundError("open")):
            out = api_import.handle_import_open({"source": "imessage"})
        self.assertFalse(out["ok"])
        self.assertIn("would not open it", out["message"])


# ── NC-351 ───────────────────────────────────────────────────────────────────

class TestTheSourceChecksAreRemembered(Base):
    """GET /api/import/sources asked the sync helper, LinkedIn and Gmail every time."""

    def setUp(self):
        super().setUp()
        import api_fda
        import api_linkedin
        import gmail_accounts
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("NC_SOURCE_CHECK_CACHE_SEC", "LINKEDIN_TOKEN", "GMAIL_ACCOUNTS"):
            os.environ.pop(key, None)
        api_import.forget_source_checks()
        self.addCleanup(api_import.forget_source_checks)
        self.asked = {}
        for name, owner in (("linkedin", api_linkedin), ("helper", api_fda),
                            ("gmail", gmail_accounts)):
            patcher = mock.patch.object(owner, "collection_blocked",
                                        wraps=owner.collection_blocked)
            self.asked[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def asks(self):
        return {k: m.call_count for k, m in self.asked.items()}

    def test_the_second_call_asks_nothing_and_is_fast(self):
        first = app.handle_import_sources(self.conn)
        self.assertEqual(self.asks(), {"linkedin": 1, "helper": 1, "gmail": 1})
        started = time.perf_counter()
        second = app.handle_import_sources(self.conn)
        took = time.perf_counter() - started
        self.assertEqual(self.asks(), {"linkedin": 1, "helper": 1, "gmail": 1})
        self.assertLess(took, 0.1)
        self.assertEqual([s["blocked"] for s in first["sources"]],
                         [s["blocked"] for s in second["sources"]])

    def test_a_new_linkedin_token_is_noticed_at_once(self):
        before = api_import.source_blockers(self.conn)
        self.assertIn("linkedin", before)             # no token, so blocked
        os.environ["LINKEDIN_TOKEN"] = "AQV-test-token"
        after = api_import.source_blockers(self.conn)
        self.assertNotIn("linkedin", after,
                         "the old answer was served after the token was added")
        self.assertEqual(self.asks()["linkedin"], 2)

    def test_a_new_helper_report_is_noticed_at_once(self):
        api_import.source_blockers(self.conn)
        report = importers.IMSG_HELPER_REPORT
        os.makedirs(os.path.dirname(report), exist_ok=True)
        with open(report, "w", encoding="utf-8") as fh:
            json.dump({"sources": []}, fh)
        self.addCleanup(lambda: os.path.exists(report) and os.remove(report))
        api_import.source_blockers(self.conn)
        self.assertEqual(self.asks()["helper"], 2)

    def test_a_new_gmail_address_is_noticed_at_once(self):
        api_import.source_blockers(self.conn)
        os.environ["GMAIL_ACCOUNTS"] = "you@example.com"
        blockers = api_import.source_blockers(self.conn)
        self.assertEqual(self.asks()["gmail"], 2)
        self.assertIn("gmail", blockers)

    def test_an_old_answer_is_dropped_when_its_lifetime_runs_out(self):
        api_import.source_blockers(self.conn)
        api_import._checks_memory["at"] -= 1000
        api_import.source_blockers(self.conn)
        self.assertEqual(self.asks()["gmail"], 2)

    def test_zero_turns_the_memory_off(self):
        os.environ["NC_SOURCE_CHECK_CACHE_SEC"] = "0"
        api_import.source_blockers(self.conn)
        api_import.source_blockers(self.conn)
        self.assertEqual(self.asks()["gmail"], 2)

    def test_what_a_caller_adds_does_not_stay_in_the_memory(self):
        api_import.source_blockers(self.conn)["gmail"] = "added by a caller"
        self.assertNotEqual(api_import.source_blockers(self.conn).get("gmail"),
                            "added by a caller")


# ── NC-355 ───────────────────────────────────────────────────────────────────

class TestAPickedFileCannotHangTheEngine(Base):
    """A file in Downloads sits behind a macOS question; a read there waits."""

    def setUp(self):
        super().setUp()
        self.secrets = tempfile.mkdtemp(prefix="nc355_")
        self.picked_dir = tempfile.mkdtemp(prefix="nc355_dl_")
        patch = mock.patch.object(config, "SECRETS_DIR", self.secrets)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": "you@example.com",
                                           "NC_PICKED_FILE_WAIT_SEC": "1"})
        env.start()
        self.addCleanup(env.stop)
        api_gmail.reset_for_test()
        self.addCleanup(api_gmail.reset_for_test)
        self.picked = os.path.join(self.picked_dir, "client secret.json")
        with open(self.picked, "w", encoding="utf-8") as fh:
            json.dump({"installed": {"client_id": "x.apps.googleusercontent.com",
                                     "client_secret": "y"}}, fh)

    def _block_reads_of_the_picked_file(self):
        """Every look at the picked path blocks until released, as macOS does
        while its question is unanswered; and it remembers who looked."""
        release = threading.Event()
        looked = []
        real = {name: getattr(os.path, name) for name in ("isfile", "exists", "getsize")}

        def watcher(name):
            def inner(path, *a, **kw):
                if "client" in str(path) and "secret" in str(path):
                    looked.append((name, threading.current_thread()))
                    release.wait(30)
                return real[name](path, *a, **kw)
            return inner

        for name in real:
            patcher = mock.patch.object(os.path, name, watcher(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        return release, looked

    def test_a_blocked_read_gives_up_with_a_sentence_and_saves_nothing(self):
        release, looked = self._block_reads_of_the_picked_file()
        # escaped spaces, as a dragged path arrives, so the path-tidying step
        # (which asks whether the path exists) is part of what must not block
        typed = self.picked.replace(" ", "\\ ")
        started = time.monotonic()
        with self.assertRaises(ApiError) as caught:
            api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                                "path": typed})
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(caught.exception.status, HTTPStatus.REQUEST_TIMEOUT)
        self.assertIn("may open files in that folder", str(caught.exception))
        # the thread answering the request never touched the picked path
        self.assertTrue(looked, "the picked path was never looked at")
        self.assertNotIn(threading.current_thread(), [t for _n, t in looked])
        # a late answer from macOS must not quietly save the file afterwards
        release.set()
        time.sleep(0.3)
        landed = api_gmail.gmail_accounts.accounts_from_env()["you@example.com"]
        self.assertFalse(os.path.exists(landed))

    def test_a_readable_file_is_saved_owner_only_as_before(self):
        os.chmod(self.picked, 0o644)
        out = api_gmail.handle_gmail_credentials({"email": "you@example.com",
                                                  "path": self.picked})
        self.assertTrue(out["ok"])
        landed = api_gmail.gmail_accounts.accounts_from_env()["you@example.com"]
        with open(landed, "rb") as fh, open(self.picked, "rb") as src:
            self.assertEqual(fh.read(), src.read())
        self.assertEqual(os.stat(landed).st_mode & 0o077, 0)

    def test_a_file_that_is_not_there_still_says_so(self):
        with self.assertRaises(ApiError) as caught:
            api_gmail.handle_gmail_credentials(
                {"email": "you@example.com", "path": self.picked + ".gone"})
        self.assertIn("There is no file at", str(caught.exception))
        with self.assertRaises(ApiError) as caught:
            api_gmail.handle_gmail_credentials({"email": "you@example.com", "path": ""})
        self.assertIn("Choose the file first", str(caught.exception))


# ── NC-303 ───────────────────────────────────────────────────────────────────

def _calendar(uid, summary):
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:{uid}\r\n"
            "DTSTART:20270301T090000Z\r\nDTEND:20270301T100000Z\r\n"
            f"SUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode()


class _Response:
    def __init__(self, body):
        self._body, self._at = body, 0
        self.headers = {"Content-Type": "text/calendar",
                        "Content-Length": str(len(body))}

    def read(self, n=-1):
        chunk = self._body[self._at:self._at + n]
        self._at += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Opener:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def open(self, request, timeout=None):
        self.asked.append(request.full_url)
        answer = self.answers[request.full_url]
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)


class TestSeveralGoogleCalendars(Base):

    A = "https://calendar.google.com/calendar/ical/ada%40example.com/private-AAAASECRET/basic.ics"
    B = "https://calendar.google.com/calendar/ical/ada%40work.example.com/private-BBBBSECRET/basic.ics"

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"NC_ICS_FETCH": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.snapshot = importers.calendar_snapshot_path("calendar_google")
        if os.path.exists(self.snapshot):
            os.remove(self.snapshot)

    def _fetch(self, stored, answers):
        os.environ["NC_ICS_GOOGLE_URL"] = stored
        opener = _Opener(answers)
        with mock.patch.object(api_import.urllib.request, "build_opener",
                               return_value=opener):
            return api_import.fetch_calendar_ics("calendar_google"), opener

    def test_addresses_are_split_wherever_a_box_puts_them(self):
        split = api_import.calendar_addresses
        self.assertEqual(split(self.A), [self.A])
        self.assertEqual(split(f"{self.A},{self.B}"), [self.A, self.B])
        self.assertEqual(split(f"{self.A}\n{self.B}\n"), [self.A, self.B])
        self.assertEqual(split(f"{self.A} ; {self.B}"), [self.A, self.B])
        # a one-line box that dropped the line break
        self.assertEqual(split(self.A + self.B.replace("https://", "webcal://")),
                         [self.A, self.B.replace("https://", "webcal://")])
        self.assertEqual(split(""), [])

    def test_two_addresses_make_one_snapshot_holding_both_calendars(self):
        out, opener = self._fetch(
            f"{self.A},{self.B}",
            {self.A: _calendar("one@example.com", "Coffee with Grace"),
             self.B: _calendar("two@example.com", "Lunch with Alan")})
        self.assertEqual(opener.asked, [self.A, self.B])
        self.assertEqual(out["calendars"], 2)
        with open(out["path"], encoding="utf-8") as fh:
            events = ics.parse_events(fh.read())
        self.assertEqual(sorted(e["uid"] for e in events),
                         ["one@example.com", "two@example.com"])
        self.assertEqual(sorted(e["summary"] for e in events),
                         ["Coffee with Grace", "Lunch with Alan"])

    def test_one_address_still_works_and_reads_as_before(self):
        out, opener = self._fetch(self.A, {self.A: _calendar("one@example.com", "Coffee")})
        self.assertEqual(out["calendars"], 1)
        self.assertNotIn("calendars", out["note"])
        self.assertEqual(len(ics.parse_events(open(out["path"], encoding="utf-8").read())), 1)

    def test_a_bad_second_address_stops_everything_and_names_which_one(self):
        before = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        os.makedirs(os.path.dirname(self.snapshot), exist_ok=True)
        with open(self.snapshot, "wb") as fh:
            fh.write(before)
        gone = urllib.error.HTTPError(self.B, 404, "Not Found", {}, None)
        with self.assertRaises(ApiError) as caught:
            self._fetch(f"{self.A},{self.B}",
                        {self.A: _calendar("one@example.com", "Coffee"), self.B: gone})
        said = str(caught.exception)
        self.assertIn("address 2 of 2", said)
        for secret in ("AAAASECRET", "BBBBSECRET"):
            self.assertNotIn(secret, said)
        with open(self.snapshot, "rb") as fh:
            self.assertEqual(fh.read(), before, "a half-downloaded set was saved")

    def test_a_piece_that_is_not_an_address_is_refused_before_anything_is_fetched(self):
        opener = _Opener({})
        os.environ["NC_ICS_GOOGLE_URL"] = f"{self.A},not-an-address"
        with mock.patch.object(api_import.urllib.request, "build_opener",
                               return_value=opener):
            with self.assertRaises(ApiError) as caught:
                api_import.fetch_calendar_ics("calendar_google")
        self.assertEqual(opener.asked, [])
        self.assertIn("comma between them", str(caught.exception))
        self.assertNotIn("AAAASECRET", str(caught.exception))

    def test_the_row_says_so_and_points_at_apple_calendar(self):
        text = importers.REGISTRY["calendar_google"].export_help
        self.assertIn("comma between them", text)
        self.assertIn("Apple Calendar row at the top of this group", text)
        self.assertIn("every calendar in Calendar.app", text)

    def test_the_data_page_help_says_so_too(self):
        """The Data page shows api_ledger's own help, not the registry's, so the
        sentence above never reached the page."""
        import api_ledger
        text = api_ledger.HELP["calendar_google"]
        self.assertIn("comma between them", text)
        self.assertIn("Apple Calendar row", text)
        self.assertIn("comma between them", api_ledger.HELP["calendar_icloud"])

    def test_the_paste_box_lets_a_comma_and_a_space_through(self):
        """It refused any space, so "first, second" read as a mis-paste."""
        from tests.helpers import js_code_of
        src = js_code_of(os.path.join(REPO, "static", "app", "ledger.js"))
        problem = src[src.index("export function secretProblem"):]
        problem = problem[:problem.index("\n}\n")]
        self.assertLess(problem.index("pastedValue(key, raw)"), problem.index("/\\s/.test(v)"),
                        "the space check runs before the addresses are joined")
        self.assertIn("saveSetting(act.key, pastedValue(act.key, value))", src)


# ── NC-224 ───────────────────────────────────────────────────────────────────

class TestWhatsappPhotosAreSavedOnlyWhenTheirFileChanged(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nc224_")
        self.dbpath = os.path.join(self.dir, "crm.db")
        db.init_db(self.dbpath)
        self._db_path = db.DB_PATH
        db.DB_PATH = self.dbpath
        self._photos_dir = ph.PHOTOS_DIR
        ph.PHOTOS_DIR = os.path.join(self.dir, "photos")
        self._exclude = wap.EXCLUDE_FILE
        wap.EXCLUDE_FILE = os.path.join(self.dir, "exclude_list.txt")
        self.addCleanup(setattr, db, "DB_PATH", self._db_path)
        self.addCleanup(setattr, ph, "PHOTOS_DIR", self._photos_dir)
        self.addCleanup(setattr, wap, "EXCLUDE_FILE", self._exclude)
        self.profile = os.path.join(self.dir, "Profile")
        os.makedirs(self.profile)
        self.conn = db.get_db(self.dbpath)
        self.addCleanup(self.conn.close)
        self.people = {}
        for name, digits, letter in (("Ada Example", "46701740605", b"A"),
                                     ("Grace Example", "46701740606", b"G"),
                                     ("Alan Example", "46701740607", b"T")):
            cid = db.create_contact(self.conn, name)
            db.add_identifier(self.conn, cid, "phone", "+" + digits)
            self.people[name] = cid
            self._avatar(digits, letter)
        self.conn.commit()

    def _avatar(self, digits, letter, picture="1", when=None):
        path = os.path.join(self.profile, f"{digits}-{picture}.jpg")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + letter * 300)
        if when is not None:
            os.utime(path, ns=(when, when))
        return path

    def _run(self):
        with mock.patch.object(ph, "save_photo", wraps=ph.save_photo) as save:
            stats = wap.run(self.profile, None, dry_run=False)
        return stats, save.call_count

    def test_a_second_run_saves_nothing(self):
        first, _ = self._run()
        self.assertEqual((first["saved"], first["unchanged"]), (3, 0))
        second, calls = self._run()
        self.assertEqual((second["saved"], second["unchanged"]), (0, 3))
        self.assertEqual(calls, 0, "an unchanged avatar was converted and compared again")

    def test_one_changed_avatar_saves_one_photo(self):
        self._run()
        self._avatar("46701740606", b"Q", picture="2")    # a new picture id, new file
        stats, calls = self._run()
        self.assertEqual((stats["saved"], stats["unchanged"]), (1, 2))
        self.assertEqual(calls, 1)

    def test_a_file_rewritten_under_the_same_name_is_noticed(self):
        self._run()
        old = os.stat(os.path.join(self.profile, "46701740605-1.jpg")).st_mtime_ns
        self._avatar("46701740605", b"Z", when=old + 5_000_000_000)
        stats, _ = self._run()
        self.assertEqual((stats["saved"], stats["unchanged"]), (1, 2))

    def test_a_photo_that_was_removed_is_saved_again(self):
        self._run()
        ada = self.people["Ada Example"]
        ph.delete_photo(self.conn, ada)
        self.conn.commit()
        stats, _ = self._run()
        self.assertEqual((stats["saved"], stats["unchanged"]), (1, 2))

    def test_a_run_with_nothing_new_writes_no_marks(self):
        self._run()
        seen = self.conn.execute(
            "SELECT key, value, updated_at FROM state WHERE key LIKE 'wa_photo_seen_%' "
            "ORDER BY key").fetchall()
        self.assertEqual(len(seen), 3)
        time.sleep(1.1)       # updated_at has one-second resolution
        self._run()
        again = self.conn.execute(
            "SELECT key, value, updated_at FROM state WHERE key LIKE 'wa_photo_seen_%' "
            "ORDER BY key").fetchall()
        self.assertEqual([tuple(r) for r in seen], [tuple(r) for r in again])


# ── NC-227 ───────────────────────────────────────────────────────────────────

class TestTheUnansweredReadUsesAnIndex(Base):
    """feed.owed_reply_candidates read every message to find who is waiting."""

    def setUp(self):
        super().setUp()
        self.arc_path = os.path.join(self.dir, "archive.db")
        arc.init_archive(self.arc_path)
        self.ac = arc.get_archive_db(self.arc_path)
        self.addCleanup(self.ac.close)
        self.waiting = db.create_contact(self.conn, "Ada Example")
        self.answered = db.create_contact(self.conn, "Grace Example")
        self.conn.commit()
        n = 0
        for cid, direction, ts, group in (
                (self.waiting, 0, "2026-10-10 09:00:00", ""),     # they wrote, no answer
                (self.answered, 0, "2026-10-10 09:00:00", ""),
                (self.answered, 1, "2026-10-10 10:00:00", ""),    # and you answered
                (self.waiting, 1, "2026-10-12 09:00:00", "Dinner group")):  # a group line
            n += 1
            arc.insert_message(self.ac, contact_id=cid, channel="whatsapp",
                               direction=direction, ts=ts, body=f"line {n}",
                               source_key=f"k{n}", group_name=group,
                               group_key=group)
        self.ac.commit()

    def _owed(self):
        with mock.patch.object(feed, "_cutoff", return_value="2000-01-01 00:00:00"):
            return [c[2]["id"] for c in feed.owed_reply_candidates(
                self.conn, self.ac, days=36500, cap=20)]

    def _plan(self, needle):
        """The query plan of the statement the read ran, found by what it says."""
        seen = []
        self.ac.set_trace_callback(seen.append)
        try:
            self._owed()
        finally:
            self.ac.set_trace_callback(None)
        sql = next(s for s in seen if needle in s)
        return " | ".join(r[3] for r in self.ac.execute("EXPLAIN QUERY PLAN " + sql))

    def test_the_archive_has_the_index(self):
        names = {r["name"] for r in self.ac.execute("PRAGMA index_list(messages)")}
        self.assertIn("idx_msg_owed", names)

    def test_who_is_waiting_is_read_from_the_index_alone(self):
        plan = self._plan("GROUP BY contact_id")
        self.assertIn("COVERING INDEX idx_msg_owed", plan)

    def test_without_the_index_the_same_read_scans_the_messages(self):
        """The contrast, so the plan check above cannot pass by accident: this is
        the plan before the fix."""
        self.ac.execute("DROP INDEX idx_msg_owed")
        plan = self._plan("GROUP BY contact_id")
        self.assertNotIn("COVERING", plan)
        self.assertIn("SCAN messages USING INDEX idx_msg_contact", plan)

    def test_one_persons_newest_message_comes_straight_off_the_index(self):
        plan = self._plan("ORDER BY ts DESC LIMIT 1")
        self.assertIn("idx_msg_owed (group_name=? AND contact_id=?)", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_the_answer_is_the_same_with_and_without_the_index(self):
        with_index = self._owed()
        self.ac.execute("DROP INDEX idx_msg_owed")
        self.assertEqual(self._owed(), with_index)
        self.assertEqual(with_index, [self.waiting])

    def test_an_archive_from_before_group_chats_still_opens(self):
        """The index names group_name, so it may only be made after that column
        exists on an old archive."""
        old = os.path.join(self.dir, "old-archive.db")
        raw = sqlite3.connect(old)
        raw.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL,
                channel TEXT NOT NULL, direction INTEGER NOT NULL DEFAULT 0,
                ts TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '', source_key TEXT NOT NULL,
                UNIQUE(channel, source_key));""")
        raw.close()
        arc.init_archive(old)
        conn = arc.get_archive_db(old)
        try:
            names = {r["name"] for r in conn.execute("PRAGMA index_list(messages)")}
        finally:
            conn.close()
        self.assertIn("idx_msg_owed", names)


if __name__ == "__main__":
    unittest.main()
