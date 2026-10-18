"""crm/linkedin_token.py: LinkedIn's token introspection, and what it must not do.

Three groups of test here, in descending order of how expensive the bug would be.

First, the credential tests. This module handles a client secret and a live access
token, and it writes a file. Nothing it writes may contain either, and no error
path may put either into a log line or an exception message.

Second, the two documented traps in LinkedIn's response, both of which fail
OPEN — that is, the naive reading of each says "your token is fine":
  · a credential/token mismatch returns HTTP 200 with active:false, so
    `r.status_code == 200` proves nothing;
  · expires_at is an absolute Unix timestamp, so reading it as "seconds
    remaining" makes any real token look decades fresh.

Third, that "we have not checked" never renders as "it is fine". That distinction
IS the feature — the bug being guarded against is a panel reporting a dead
token as up to date, and a monitor that guesses optimistically when it has no data
would reproduce it exactly.
"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401 — CRM_DATA_DIR bootstrap
import linkedin_token as T  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "crm", "linkedin_token.py")

TOKEN = "AQVfake-access-token-value-do-not-log-me-0123456789"
SECRET = "WPL_AP1.fake-client-secret-also-never-logged.xyz"


def source():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


class Res:
    """A stand-in for a requests Response, with only what the module reads."""

    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._body


class TokenBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="li_token_")
        self.report = os.path.join(self.tmp, "linkedin-token.json")
        self._p = mock.patch.object(T, "REPORT_PATH", self.report)
        self._p.start()
        self._env = {k: os.environ.get(k) for k in (
            "LINKEDIN_TOKEN", "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET")}
        os.environ["LINKEDIN_TOKEN"] = TOKEN
        os.environ["LINKEDIN_CLIENT_ID"] = "77fakeclientid"
        os.environ["LINKEDIN_CLIENT_SECRET"] = SECRET

    def tearDown(self):
        self._p.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def call(self, res=None, raises=None):
        """Run introspect() against a stubbed requests.post, and hand back both
        the record and the request that was made."""
        seen = {}

        def post(url, headers=None, data=None, timeout=None):
            seen.update(url=url, headers=headers or {}, data=data or {},
                        timeout=timeout)
            if raises is not None:
                raise raises
            return res

        import requests
        with mock.patch.object(requests, "post", post):
            rec = T.introspect()
        return rec, seen

    def record(self, res=None, raises=None):
        """Mock the call AND write the report.

        Using call() and then check_and_record() would quietly make a REAL
        request to LinkedIn on the second step and overwrite the stubbed record
        with a live 400. Anything that needs a report on disk goes through here.
        """
        def post(url, headers=None, data=None, timeout=None):
            if raises is not None:
                raise raises
            return res

        import requests
        with mock.patch.object(requests, "post", post):
            return T.check_and_record()


# ════════════════════════════════════════════════════════════════════════════
class TestTheCredentialsNeverEscape(TokenBase):
    """The module holds a client secret and an access token. Everything it
    writes or returns is a place either could leak to."""

    def test_the_written_report_contains_neither_secret(self):
        exp = int(time.time()) + 40 * 86400
        self.record(Res(200, {"active": True, "status": "active",
                              "expires_at": exp, "scope": "r_fake_scope",
                              "auth_type": "3L"}))
        with open(self.report, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(TOKEN[:16], text)
        self.assertNotIn(SECRET[:16], text)

    def test_a_network_error_message_is_not_passed_through(self):
        """A requests exception can carry the full request — and this request's
        body IS the client secret. So the record keeps the exception's class name
        and nothing else."""
        import requests
        boom = requests.ConnectionError(
            f"failed posting to introspectToken with client_secret={SECRET}")
        rec, _ = self.call(raises=boom)
        self.assertEqual(rec["outcome"], "unreachable")
        blob = json.dumps(rec)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("client_secret", blob)
        self.assertEqual(rec["detail"], "ConnectionError")

    def test_a_response_echoing_a_credential_is_refused_not_recorded(self):
        """Defence against a LinkedIn change nobody controls. If the response
        ever started echoing the token back, writing it to disk would be an
        unrecoverable mistake, so the record is dropped instead."""
        rec, _ = self.call(Res(200, {
            "active": True, "status": "active",
            "expires_at": int(time.time()) + 86400,
            "scope": f"r_fake_scope,{TOKEN}", "auth_type": "3L"}))
        self.assertEqual(rec["outcome"], "unreachable")
        self.assertIn("echoed", rec["detail"])
        self.assertNotIn(TOKEN, json.dumps(rec))

    def test_the_report_file_is_owner_only(self):
        self.record(Res(200, {"active": True, "expires_at": int(time.time()) + 86400}))
        self.assertEqual(oct(os.stat(self.report).st_mode)[-3:], "600")

    def test_the_module_never_logs(self):
        """No logger, no print in the request path. The one place a secret could
        reach a log file is the one place there is no logging call."""
        code = re.sub(r'"""(?:.|\n)*?"""', "", source())
        code = "\n".join(l.split("#")[0] for l in code.splitlines())
        # _cli() is excluded: it prints summary()/the record, both of which are
        # asserted secret-free above, and it only runs when a human types the
        # module name. The rule being enforced is about the REQUEST path.
        code = code.split("def _cli(")[0]
        for banned in ("logging", "logger", "print(", "sys.stderr"):
            self.assertNotIn(banned, code,
                             f"linkedin_token references {banned!r}; a client "
                             f"secret must never reach a log")


# ════════════════════════════════════════════════════════════════════════════
class TestTheRequestMatchesWhatLinkedInDocuments(TokenBase):
    """https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection

    Posting deliberately fake credentials to the endpoint returns HTTP 400,
    exactly as documented, so the URL, method and body shape below are the ones
    the real service expects.
    """

    def test_it_posts_form_encoded_to_the_documented_url(self):
        self.call(Res(200, {"active": True}))
        _, seen = self.call(Res(200, {"active": True}))
        self.assertEqual(seen["url"], "https://www.linkedin.com/oauth/v2/introspectToken")
        self.assertEqual(seen["headers"].get("Content-Type"),
                         "application/x-www-form-urlencoded")

    def test_all_three_required_fields_go_in_the_body(self):
        """Body, not query string: a token in a URL ends up in access logs and
        proxy caches."""
        _, seen = self.call(Res(200, {"active": True}))
        self.assertEqual(set(seen["data"]), {"client_id", "client_secret", "token"})
        self.assertEqual(seen["data"]["token"], TOKEN)
        self.assertNotIn(TOKEN, seen["url"])

    def test_it_carries_a_timeout(self):
        """This runs inside the autosync loop. A hung LinkedIn must not hold the
        loop that keeps iMessage current."""
        _, seen = self.call(Res(200, {"active": True}))
        self.assertIsNotNone(seen["timeout"])
        self.assertLessEqual(seen["timeout"], 60)


# ════════════════════════════════════════════════════════════════════════════
class TestTheTwoTrapsThatFailOpen(TokenBase):
    """Both of these, read naively, say "your token is fine". That is what makes
    them worth a test each rather than a comment."""

    def test_a_200_with_active_false_is_a_dead_token(self):
        """Documented verbatim: "If the credentials are valid but do not match the
        client information in the token, you will receive a successful response
        (status 200 OK), however with `active: false`". So a 200 alone proves
        nothing, and this is the single easiest way to build this wrong."""
        rec, _ = self.call(Res(200, {"active": False}))
        self.assertEqual(rec["outcome"], "expired")
        self.assertFalse(rec["active"])

    def test_a_200_with_no_active_field_is_a_failed_check_not_a_pass(self):
        rec, _ = self.call(Res(200, {"status": "active"}))
        self.assertEqual(rec["outcome"], "unreachable")

    def test_an_unparseable_200_is_a_failed_check_not_a_pass(self):
        rec, _ = self.call(Res(200, raw="<html>gateway error</html>"))
        self.assertEqual(rec["outcome"], "unreachable")

    def test_expires_at_is_read_as_an_absolute_timestamp(self):
        """It is epoch SECONDS, not a countdown. Read as a duration, a token
        expiring in 2027 would compute as ~53,000 days left, i.e. permanently
        healthy — the failure would never surface."""
        exp = int(time.time()) + 40 * 86400
        rec, _ = self.call(Res(200, {"active": True, "status": "active",
                                     "expires_at": exp}))
        self.assertEqual(rec["expires_at"], exp)
        self.assertAlmostEqual(rec["days_left"], 40, delta=0.1)

    def test_an_already_past_expiry_clamps_at_zero(self):
        rec, _ = self.call(Res(200, {"active": True,
                                     "expires_at": int(time.time()) - 99999}))
        self.assertEqual(rec["days_left"], 0)

    def test_revoked_and_expired_are_kept_apart(self):
        for status, want in (("revoked", "revoked"), ("expired", "expired")):
            with self.subTest(status=status):
                rec, _ = self.call(Res(200, {"active": False, "status": status}))
                self.assertEqual(rec["outcome"], want)

    def test_400_and_401_are_rejections_not_outages(self):
        """400 = bad client_id or token, 401 = bad client_secret. Both mean stop
        trusting the token; neither means LinkedIn is down."""
        for code in (400, 401):
            with self.subTest(http=code):
                rec, _ = self.call(Res(code, {}))
                self.assertEqual(rec["outcome"], "rejected")
                self.assertEqual(rec["http"], code)

    def test_a_500_is_an_outage_not_a_rejection(self):
        rec, _ = self.call(Res(503, {}))
        self.assertEqual(rec["outcome"], "unreachable")


# ════════════════════════════════════════════════════════════════════════════
class TestNotCheckedNeverReadsAsFine(TokenBase):
    """The distinction that IS the feature. The bug this replaced was a panel
    saying "LinkedIn is up to date" about a token it had never asked about."""

    def test_no_report_at_all_is_unknown(self):
        self.assertEqual(T.summary()["state"], "unknown")

    def test_missing_client_credentials_is_unchecked_not_ok(self):
        os.environ.pop("LINKEDIN_CLIENT_SECRET")
        rec = T.check_and_record()
        self.assertEqual(rec["outcome"], "unconfigured")
        self.assertIn("client_secret", rec["missing"])
        self.assertEqual(T.summary()["state"], "unchecked")

    def test_unconfigured_costs_no_request(self):
        """It must return before touching the network, or the loop pays a DNS
        lookup twice a day for an install that has not opted in."""
        os.environ.pop("LINKEDIN_CLIENT_ID")

        def boom(*a, **k):
            raise AssertionError("made a request with no credentials")

        import requests
        with mock.patch.object(requests, "post", boom):
            self.assertEqual(T.introspect()["outcome"], "unconfigured")

    def test_a_live_token_with_room_is_ok_and_one_without_is_expiring(self):
        for days, want in ((40, "ok"), (T.WARN_DAYS, "expiring"), (2, "expiring")):
            with self.subTest(days=days):
                self.record(Res(200, {"active": True, "status": "active",
                                      "expires_at": int(time.time()) + days * 86400}))
                self.assertEqual(T.summary()["state"], want)

    def test_expired_revoked_and_rejected_all_read_as_dead(self):
        for body, status in (({"active": False, "status": "expired"}, 200),
                             ({"active": False, "status": "revoked"}, 200),
                             ({}, 400)):
            with self.subTest(body=body, status=status):
                self.record(Res(status, body))
                self.assertEqual(T.summary()["state"], "dead")

    def test_a_briefly_failing_check_is_not_reported_as_a_token_problem(self):
        """A network blip says nothing about the token, and dressing it up as a
        token failure would train people to ignore the panel."""
        import requests
        self.record(raises=requests.ConnectionError("down"))
        self.assertEqual(T.summary()["state"], "ok")

    def test_a_persistently_failing_check_becomes_stale(self):
        import requests
        rec = self.record(raises=requests.ConnectionError("down"))
        rec["checked_at"] = int(time.time()) - T.CHECK_EVERY_HOURS * 3600 * 3
        T._write_report(rec)
        self.assertEqual(T.summary()["state"], "stale")

    def test_a_corrupt_report_is_unknown_not_ok(self):
        with open(self.report, "w", encoding="utf-8") as fh:
            fh.write("{ truncated")
        self.assertEqual(T.summary()["state"], "unknown")

    def test_the_summary_says_there_is_no_renewal(self):
        """`renewable` is False and hardcoded. Renewing a 3-legged token needs a
        member in a browser unless LinkedIn granted the app programmatic refresh
        tokens, which ordinary member authorisation does not. The flag exists so
        the UI cannot grow a Renew button that quietly does nothing."""
        self.assertFalse(T.summary()["renewable"])


# ════════════════════════════════════════════════════════════════════════════
class TestItDoesNotCheckMoreOftenThanItSaid(TokenBase):
    """The endpoint's rate limit is undocumented, so the cadence is the only
    thing protecting it, and it is the only thing that can go wrong quietly."""

    def test_it_is_due_when_nothing_has_been_recorded(self):
        self.assertTrue(T.is_due())

    def test_it_is_not_due_straight_after_a_check(self):
        self.record(Res(200, {"active": True}))
        self.assertFalse(T.is_due())

    def test_it_is_due_again_after_the_interval(self):
        rec = self.record(Res(200, {"active": True}))
        rec["checked_at"] = int(time.time()) - (T.CHECK_EVERY_HOURS * 3600 + 60)
        T._write_report(rec)
        self.assertTrue(T.is_due())

    def test_the_interval_is_at_least_a_few_hours(self):
        """A token that lasts weeks does not need minute-by-minute polling, and the loop
        this runs in can tick every couple of minutes."""
        self.assertGreaterEqual(T.CHECK_EVERY_HOURS, 4)

    def test_the_warning_window_leaves_time_to_act(self):
        self.assertGreaterEqual(T.WARN_DAYS, 3)


# ════════════════════════════════════════════════════════════════════════════
class TestThereIsNoRenewalCodeToMisfire(unittest.TestCase):
    """A refresh implementation written on spec — without the approval LinkedIn
    requires for programmatic refresh tokens — would be dead code that looks like
    a working feature, and that is how an unused path gets mistaken for a working
    one."""

    def test_the_module_does_not_touch_the_access_token_endpoint(self):
        code = re.sub(r'"""(?:.|\n)*?"""', "", source())
        code = "\n".join(l.split("#")[0] for l in code.splitlines())
        self.assertNotIn("grant_type", code)
        self.assertNotIn("refresh_token", code)
        self.assertNotIn("oauth/v2/accessToken", code)

    def test_the_docstring_records_why_and_cites_the_docs(self):
        """So nobody implements it on a guess. Both the eligibility rule and
        where to check it must be written down."""
        # Whitespace-normalised, because the phrase is line-wrapped in the
        # docstring and a raw substring match on wrapped prose is a gate that
        # fails on reflow rather than on meaning.
        doc = " ".join(source().split())
        self.assertIn("programmatic-refresh-tokens", doc)
        self.assertIn("Marketing Developer Platform", doc)


if __name__ == "__main__":
    unittest.main()
