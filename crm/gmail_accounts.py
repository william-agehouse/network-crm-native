"""Which Gmail accounts are configured, and whether each one can actually run.

NC-180. Gmail needs two things and only one of them is a field on a screen: the
address goes in `GMAIL_ACCOUNTS` under Settings, and the OAuth client file — the
small JSON you download from Google Cloud Console, which is what lets this app
ask Google anything at all — has to be fetched by hand and dropped into the
secrets folder. Filling in the half that is on the screen and not the half that
is not is the obvious way to end up half set up.

In that state the 12-hour background run used to fail with `SystemExit("Missing
<path>…")`, exit 1, which `run_autosync` discards like every other non-zero. So
it failed twice a day forever, said nothing anywhere, and the Data page went on
reporting Gmail as configured — because "configured" only ever asked whether
`GMAIL_ACCOUNTS` was set.

**This module exists so the server can ask without importing Google.**
`crm/gmail_import.py` pulls in `google.auth`, `google_auth_oauthlib` and
`googleapiclient` at import time; the server must not, and `_source_blockers()`
in crm/api_import.py runs on every visit to the Data page. Everything here is
`os` and `re` — the two functions gmail_import already had, moved, plus the
readiness question asked of them. gmail_import imports them back, so there is
still exactly one parser for `GMAIL_ACCOUNTS` and one spelling of where a token
file lives.
"""

import os
import re
from typing import Dict

import config  # resolves the secrets dir (packaged .app vs repo checkout)


def accounts_from_env() -> Dict[str, str]:
    """Which Gmail accounts to import, from the GMAIL_ACCOUNTS setting.

    The accounts come from this setting: each user enters their own addresses in
    the app.

    Format is comma-separated `email` or `email:credentials_file`. A bare email
    derives `gmail_credentials_<slug>.json`; the explicit form exists because
    real credential files are rarely named after the address. A relative
    filename resolves inside the secrets dir; an absolute path is taken as given.
    """
    out: Dict[str, str] = {}
    for entry in os.environ.get("GMAIL_ACCOUNTS", "").split(","):
        entry = entry.strip()
        if not entry or "@" not in entry:
            continue
        email, _, cred = entry.partition(":")
        email, cred = email.strip().lower(), cred.strip()
        if not cred:
            cred = f"gmail_credentials_{re.sub(r'[^a-z0-9]', '', email)}.json"
        out[email] = cred if os.path.isabs(cred) else config.secret_path(cred)
    return out


def token_path(account_email: str) -> str:
    """Where this account's saved Google permission lives once it has one."""
    slug = re.sub(r"[^a-z0-9]", "", account_email.lower())
    return config.secret_path(f"gmail_token_{slug}.json")


def missing_credentials() -> Dict[str, str]:
    """{address: the file it wants} for every configured account with no client
    file on disk. Empty when every account has one, and empty when none are
    configured — "not set up" is not the same as "set up wrongly", and the Data
    page already has its own words for the first."""
    out: Dict[str, str] = {}
    for email, creds in accounts_from_env().items():
        if not os.path.exists(creds):
            out[email] = creds
    return out


def never_signed_in() -> Dict[str, str]:
    """{address: the token file it would write} for every configured account
    that HAS its client file and has never been signed in.

    NC-117. The third state, and the one nothing could see. NC-180 taught the
    page about an address with no file; this is an address with a file and no
    permission — the state every new install passes through between downloading
    the JSON and pressing Sign in, and the state it stays in forever if nobody
    presses it. The background loop cannot fix it (a run nobody started may not
    open a Google tab, NC-177), so left unsaid it is silent forever.

    Accounts with no client file are NOT listed here. They are the other
    blocker's business, and a person who has not got a file yet cannot sign in
    with it — saying both at once would be two instructions for one step.
    """
    out: Dict[str, str] = {}
    for email, creds in accounts_from_env().items():
        if os.path.exists(creds) and not os.path.exists(token_path(email)):
            out[email] = token_path(email)
    return out


def collection_blocked() -> str:
    """One sentence for the Data page, or "" when nothing is wrong.

    Same shape and same contract as api_linkedin.collection_blocked() and
    api_fda.collection_blocked(): the module that knows exports the sentence,
    and nobody reimplements the rule. Never raises — see _source_blockers().

    It names the file, because the whole difficulty of this state is that the
    missing thing is invisible: nothing on any screen has ever mentioned it.

    Missing file outranks missing sign-in, and the order is the point rather
    than a tidiness preference: you cannot sign in with a file you have not
    downloaded, so naming both at once would be two instructions for one step.
    """
    missing = missing_credentials()
    if missing:
        first_file = os.path.basename(next(iter(missing.values())))
        who = ("This account has" if len(missing) == 1
               else f"{len(missing)} of these accounts have")
        return (f"Gmail cannot run yet. {who} no sign-in file from Google, so "
                f"there is nothing to sign in with and the automatic sync is not "
                f"attempting it. Open this row — the steps are there, and the "
                f"button files the download for you. By hand it is Google Cloud "
                f"Console → Clients → your client → download the JSON, saved in "
                f"your secrets folder as {first_file}.")

    unsigned = never_signed_in()
    if unsigned:
        # NC-117. Says which address, for NC-181's reason: GMAIL_ACCOUNTS is a
        # list and everything else on this row is keyed by source, so "sign in"
        # with no name sends a person with several addresses to Google once
        # per address.
        who = (f"{next(iter(unsigned))} has" if len(unsigned) == 1
               else f"{len(unsigned)} of these accounts have")
        return (f"Gmail is set up but not signed in. {who} a sign-in file and no "
                f"permission yet, so there is nothing to collect with and the "
                f"automatic sync is not attempting it. Open this row and press "
                f"Sign in — that is when the Google tab opens, and not before.")
    return ""
