"""Ask LinkedIn how long the token has left, and write the answer down.

WHY THIS EXISTS
NC-064 shipped a LinkedIn panel whose API lane said "Token set" whenever
`LINKEDIN_TOKEN` was a non-empty string. That is a claim about `.env`, not about
LinkedIn: an expired token is still a string, so the panel could read "LinkedIn
is up to date" for a token that had expired while no message had arrived since.

It is an easy mistake to repeat: answering "is this working?" with a signal
that only proves "this is installed". `imsg_helper_installed()` proving a plist exists rather than a permission being
granted was one. `mtime` proving a file changed rather than data being current
was another. `token_present` is this one.

WHAT MAKES IT ANSWERABLE NOW
LinkedIn has a token introspection endpoint. It reports the token's
real status and a real expiry, so "how long have I got" stops being a guess:

    POST https://www.linkedin.com/oauth/v2/introspectToken
    Content-Type: application/x-www-form-urlencoded
    client_id=…&client_secret=…&token=…

    → {"active": true, "status": "active", "expires_at": 1497497620,
       "created_at": …, "authorized_at": …, "scope": "…", "auth_type": "3L"}

    https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection

TWO THINGS ABOUT THAT RESPONSE THAT WILL BITE IF IGNORED
  1. **A credential mismatch returns HTTP 200 with `active: false`.** Documented,
     and it means `r.status_code == 200` proves nothing at all. Only the `active`
     field in the body is a go/no-go, so this module reads the body and treats a
     200 it cannot parse as a failed check rather than as a healthy token.
  2. **`expires_at` is an absolute Unix timestamp in seconds, not a countdown.**
     Reading it as "seconds remaining" would report a token expiring in 2027 as
     comfortably fresh forever.

WHY IT WRITES A FILE INSTEAD OF ANSWERING A REQUEST
Introspection is a network call, and `/api/linkedin/status` runs on every visit
to the Data page. Putting one inside the other would pay a LinkedIn round trip to
render a page, and would make the page's speed depend on LinkedIn being up.

So this follows the pattern the sync helper already established: the thing that
knows writes a small report, and the endpoint reads the report. `api_linkedin`
never calls this module — it reads the JSON. That keeps its "no network on the
page-render path" guarantee literally true, and it means a LinkedIn outage makes
the report stale rather than making the app slow.

WHAT IT CANNOT DO, AND WHY THERE IS NO RENEW FUNCTION HERE
It cannot renew anything. Renewing a 3-legged token without a human means a
refresh token, and LinkedIn issues those only to "approved Marketing Developer
Platform partners" — a different partner programme, enabled per-application. The
Member Data Portability docs that grant `r_dma_portability_self_serve` never
mention refresh tokens at all; they document only the manual Developer Portal
flow where a member clicks Allow.

    https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens
    https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-member/

Not automating consent is the entire point of three-legged OAuth, so the honest
product is not a renewal timer — it is telling the user in good time that they need
to spend thirty seconds in a browser. (For an app granted refresh tokens — the
Developer Portal shows an "OAuth Refresh Token Usage" table for it — a renewer
would belong in this module, using grant_type=refresh_token against the
accessToken endpoint. Deliberately not written on spec.)

HOW LONG A TOKEN LASTS. "Tokens last 60 days" is LinkedIn's BASELINE, not what
every app issues. How long a token lives depends on the products the app has
been approved for:

    an app with no products approved    5,184,000 s =  60 days
    an app with Member Data Portability 31,536,000 s = 365 days

Approving the Member Data Portability product raises the access-token time-to-live
to a full year, which the scope's own consent copy states outright: "Access (up to
1 Year) and use your LinkedIn data". An app that carries
r_dma_portability_self_serve is therefore the annual kind.

So expiry is an ANNUAL event, not a bi-monthly one, and the number this module
reports must come from introspection rather than from any constant here. It
does: days_left is computed from the `expires_at` LinkedIn returns.
"""

import json
import os
import time
from typing import Any, Dict

import config

# requests is imported INSIDE introspect(), not here, and the reason is blast
# radius. api_import imports this module and app.py imports api_import, so a
# module-level `import requests` would put a third-party package on the app's
# startup path: if it were ever missing from the venv the whole app would fail to
# launch, rather than this one check degrading. It is declared in
# requirements.txt and present today — this is about what happens the day it is
# not. Same judgement invariant 8 makes about ANTHROPIC_API_KEY.

INTROSPECT_URL = "https://www.linkedin.com/oauth/v2/introspectToken"

# Where the answer lives. Beside sync-status.json, and for the same reason: it is
# this install's record of what an outside system last said, not app state.
REPORT_PATH = config.data_path("linkedin-token.json")

# ── Tunables ─────────────────────────────────────────────────────────────────
# Twice a day, and NOT because expiry needs watching that closely — the token
# lasts a year (see the correction above). It is this often because REVOCATION has
# no schedule: a secret rotated in the Developer Portal, a withdrawn consent or a
# deleted app kills the token the same afternoon, and that is the case where a day
# of silent no-op syncing is worth avoiding. The endpoint's rate limit is
# undocumented, so err high anyway.
CHECK_EVERY_HOURS = int(os.environ.get("NC_LI_TOKEN_CHECK_HOURS", "12"))
# Warn this far out. Ten days is a fortnight of slack minus a weekend: long
# enough that a busy week cannot swallow it, short enough not to nag. It is
# generous against a 365-day token rather than tight against a 60-day one, which
# is the right direction to be wrong in — this fires once a year.
WARN_DAYS = int(os.environ.get("NC_LI_TOKEN_WARN_DAYS", "10"))
# Short: this runs inside the autosync loop, and a hung LinkedIn must not hold it.
TIMEOUT_SECONDS = int(os.environ.get("NC_LI_TOKEN_TIMEOUT", "20"))


def credentials() -> Dict[str, str]:
    """The three values introspection needs, read at call time.

    Read from the environment on every call rather than captured at import, so
    pasting a client secret into Settings takes effect without a restart — the
    same contract every other managed key has.
    """
    return {
        "client_id": (os.environ.get("LINKEDIN_CLIENT_ID") or "").strip(),
        "client_secret": (os.environ.get("LINKEDIN_CLIENT_SECRET") or "").strip(),
        "token": (os.environ.get("LINKEDIN_TOKEN") or "").strip(),
    }


def configured() -> bool:
    c = credentials()
    return bool(c["client_id"] and c["client_secret"] and c["token"])


def read_report() -> Dict[str, Any]:
    """The last recorded answer, or {} — never raises.

    {} means "nobody has checked", which is a different thing from both "the
    token is fine" and "the token is dead", and the panel has to be able to say
    so. Fabricating a healthy default here is how a monitoring feature becomes
    worse than none.
    """
    try:
        with open(REPORT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_report(data: Dict[str, Any]) -> None:
    """Atomic, and 0600.

    The file holds no secret — see the assertion in `introspect` — but it names
    the account's token state, and every other file this app writes about its own
    credentials is owner-only. Written to a .part and renamed so a reader can
    never catch a half-written JSON document.
    """
    tmp = REPORT_PATH + ".part"
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, REPORT_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def introspect() -> Dict[str, Any]:
    """Ask LinkedIn, and return the record that gets written.

    Every outcome is a dict with an `outcome` key. Nothing raises: this is called
    from the autosync loop, where an exception would take the sources behind it
    down with it, and from a diagnostics route.

    `outcome` is one of:
      unconfigured  no client id / secret / token — we cannot check, and that is
                    not the same as a problem with the token
      active        LinkedIn says it is live; `expires_at` and `days_left` set
      expired       LinkedIn says the TTL has passed
      revoked       LinkedIn says it was revoked
      rejected      HTTP 400/401 — bad client id, bad secret, or bad token
      unreachable   network failure, timeout, or an unparseable body
    """
    c = credentials()
    now = int(time.time())
    if not (c["client_id"] and c["client_secret"] and c["token"]):
        missing = [k for k in ("client_id", "client_secret", "token") if not c[k]]
        return {"outcome": "unconfigured", "checked_at": now, "missing": missing}

    try:
        import requests
    except ImportError:
        return {"outcome": "unreachable", "checked_at": now,
                "detail": "requests is not installed in this venv"}

    try:
        r = requests.post(
            INTROSPECT_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=c,                       # all three go in the BODY, form-encoded
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:  # noqa: F821 — imported just above
        # The class name, never str(e): a requests exception can carry the full
        # request URL, and this one's body holds the client secret.
        return {"outcome": "unreachable", "checked_at": now,
                "detail": type(e).__name__}

    # 400 = bad client_id or bad token, 401 = bad client_secret. Both are real
    # answers and both mean "stop trusting this token", but neither tells us
    # WHICH of the three is wrong, so the record says so rather than guessing.
    if r.status_code in (400, 401):
        return {"outcome": "rejected", "checked_at": now, "http": r.status_code}
    if r.status_code != 200:
        return {"outcome": "unreachable", "checked_at": now, "http": r.status_code}

    try:
        body = r.json()
    except ValueError:
        return {"outcome": "unreachable", "checked_at": now, "detail": "unparseable"}
    if not isinstance(body, dict):
        return {"outcome": "unreachable", "checked_at": now, "detail": "unparseable"}

    # THE documented trap: valid credentials that do not match the token get a
    # 200 with active:false. So the HTTP status proves nothing and `active` is the
    # only go/no-go. A 200 whose body has no `active` at all is treated as a
    # failed check, not as a healthy token.
    if "active" not in body:
        return {"outcome": "unreachable", "checked_at": now, "detail": "no active field"}
    active = bool(body.get("active"))
    status = str(body.get("status") or "")

    expires_at = body.get("expires_at")
    expires_at = int(expires_at) if isinstance(expires_at, (int, float)) else None
    # expires_at is an ABSOLUTE epoch-seconds timestamp, not a countdown.
    days_left = None
    if expires_at is not None:
        days_left = max(0.0, round((expires_at - now) / 86400.0, 2))

    if active:
        outcome = "active"
    elif status in ("expired", "revoked"):
        outcome = status
    else:
        # active:false with no reason: most likely the credential/token mismatch
        # above. "dead, and LinkedIn did not say why" is the honest record.
        outcome = "expired"

    out = {
        "outcome": outcome,
        "checked_at": now,
        "active": active,
        "status": status,
        "expires_at": expires_at,
        "days_left": days_left,
        "created_at": body.get("created_at") if isinstance(
            body.get("created_at"), (int, float)) else None,
        "authorized_at": body.get("authorized_at") if isinstance(
            body.get("authorized_at"), (int, float)) else None,
        # Useful because a token minted with the wrong scope ticked is a real
        # mistake with a confusing symptom: 403 on every domain, token "fine".
        "scopes": [s.strip() for s in str(body.get("scope") or "").split(",")
                   if s.strip()],
        "auth_type": str(body.get("auth_type") or ""),
        "warn_days": WARN_DAYS,
    }
    # A LinkedIn change that started echoing the token back must not get written
    # to disk by us. Cheap, and the failure it prevents is unrecoverable.
    blob = json.dumps(out)
    for secret in (c["token"], c["client_secret"]):
        if secret and secret in blob:
            return {"outcome": "unreachable", "checked_at": now,
                    "detail": "response echoed a credential; not recorded"}
    return out


def check_and_record() -> Dict[str, Any]:
    """Introspect and write the report. Returns the record."""
    rec = introspect()
    _write_report(rec)
    return rec


def is_due() -> bool:
    """Has CHECK_EVERY_HOURS passed since the last check?

    Unconfigured counts as due: it costs no request (introspect() returns before
    touching the network), and it means the panel starts telling the truth the
    moment the client secret is pasted in, rather than up to twelve hours later.
    """
    rec = read_report()
    last = rec.get("checked_at")
    if not isinstance(last, (int, float)):
        return True
    return (time.time() - float(last)) >= CHECK_EVERY_HOURS * 3600


def summary() -> Dict[str, Any]:
    """What the panel needs, derived from the recorded report. No network.

    `state` is decided here rather than in JS for the reason api_fda gives: the
    ordering is a product decision and belongs next to the data it reads.

      unknown   nobody has checked yet
      unchecked no client id/secret, so we cannot check — NOT a problem with the
                token, and it must not be dressed up as one
      stale     the last check is older than two check periods; LinkedIn was
                probably unreachable. Says nothing about the token itself
      dead      expired, revoked or rejected — the import cannot work
      expiring  active, but inside the warning window
      ok        active with room to spare
    """
    # Every return below carries the SAME keys. A short early-return for "no
    # report yet" would make a caller reading summary()["renewable"] get a
    # KeyError in exactly the state that is most common on a fresh install. A
    # status shape that varies by status is a shape every consumer has to guard,
    # so it does not vary.
    out: Dict[str, Any] = {
        "state": "unknown",
        "outcome": "",
        "checked_at": None,
        "checked_age_hours": None,
        "expires_at": None,
        "days_left": None,
        "scopes": [],
        "auth_type": "",
        "warn_days": WARN_DAYS,
        "configured": configured(),
        # There is no renewal path to offer. Stated in the payload so the UI
        # cannot grow a "Renew" button that quietly does nothing: renewing a
        # 3-legged token needs a member in a browser unless LinkedIn has granted
        # this app programmatic refresh tokens, which the DMA product does not.
        "renewable": False,
    }
    rec = read_report()
    if not rec:
        return out

    outcome = str(rec.get("outcome") or "")
    checked = rec.get("checked_at")
    days = rec.get("days_left")
    age_h = None
    if isinstance(checked, (int, float)):
        age_h = max(0.0, (time.time() - float(checked)) / 3600.0)

    if outcome == "unconfigured":
        state = "unchecked"
    elif outcome in ("expired", "revoked", "rejected"):
        state = "dead"
    elif outcome == "unreachable":
        # A failed check is a fact about the network, not about the token. It
        # only becomes worth saying once it has been failing for a while.
        state = "stale" if (age_h is None or age_h > CHECK_EVERY_HOURS * 2) else "ok"
    elif outcome == "active":
        state = "expiring" if (isinstance(days, (int, float))
                               and days <= WARN_DAYS) else "ok"
    else:
        state = "unknown"

    out.update({
        "state": state,
        "outcome": outcome,
        "checked_at": int(checked) if isinstance(checked, (int, float)) else None,
        "checked_age_hours": round(age_h, 2) if age_h is not None else None,
        "expires_at": rec.get("expires_at"),
        "days_left": days,
        "scopes": rec.get("scopes") or [],
        "auth_type": rec.get("auth_type") or "",
    })
    return out


def _cli() -> int:
    """`python3 crm/linkedin_token.py [--check]` — for diagnosing by hand.

    Prints the record with no secret in it. --check makes the call; without it,
    prints what was last recorded.
    """
    import sys
    if "--check" in sys.argv:
        rec = check_and_record()
        print(json.dumps(rec, indent=2, sort_keys=True))
        print(f"\nrecorded to {REPORT_PATH}")
    else:
        print(json.dumps(summary(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
