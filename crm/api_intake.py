#!/usr/bin/env python3
"""AI intake endpoints: the two egress points, and the review-before-commit flow.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).
Reach every external module THROUGH its module name (db.get_db, intake.plan) so
the tests' `app.db` / `app.intake` patching is not bound past.

WHAT LIVES HERE AND WHAT LIVES IN intake.py
-------------------------------------------
`crm/intake.py` is pure: it takes text, reads crm.db with SELECTs, and returns
proposals. It makes no network call and calls no model, which is what makes the
containment logic (`intake.records_from_model_output`) unit-testable with no key
and no network. Everything that leaves this machine is in THIS file, in exactly
two functions, and nowhere else:

  • `fetch_page()`   — one https GET of a URL the user pasted.  NEW EGRESS.
  • `_call_model()`  — one Anthropic messages.create with NO tools.  Egress.

Both are off unless explicitly configured, and both refuse loudly rather than
degrading quietly.

THE ENDPOINTS
-------------
  GET    /api/intake/status   what is available, what is off, and every cap
  POST   /api/intake/parse    offline parse → proposals            (no key)
  POST   /api/intake/propose  model-assisted parse → proposals     (needs a key)
  POST   /api/intake/lookup   fetch ONE pasted URL as text         (opt-in)
  POST   /api/intake/commit   write a confirmed batch              (no model)
  GET    /api/intake/batch    load the parked draft
  POST   /api/intake/batch    park the draft
  DELETE /api/intake/batch    discard the draft

`/parse` and `/propose` return the identical shape, so the UI has one renderer
and the no-key path is not a second-class citizen. `/propose` without a key does
not fail: it runs the offline parser and says so in `report.degraded`, because
invariant 8 wants a *visible* degraded path, never a silent one.

THE REVIEW STEP IS THE PRODUCT
------------------------------
Nothing reaches crm.db from `/parse`, `/propose` or `/lookup`. Only
`/api/intake/commit` writes, it requires `confirm: true` in the body, and it
re-runs the matcher server-side — so the batch the browser posts is a
*description of people*, never an instruction naming a row. See
`intake.commit_batch`.

WHY THE WEB LOOKUP IS SHAPED LIKE THIS
--------------------------------------
A fetched page is untrusted input on its way into a model that proposes database
writes. `intake.py`'s docstring lists the six structural reasons that is
contained; this file adds the three that are about the request itself:

  • It only fetches an address that appeared in the owner's OWN pasted text, and
    the server works out which those are — it re-parses the paste with
    `intake.parse_block` and `intake.allowed_lookup_urls` on every lookup. A URL
    a model returned, or one lifted out of a fetched page, is not in that list
    and is refused, so there is no way to chain one fetch into another. An empty
    list refuses everything: the paste naming no addresses is the case where
    nothing may be fetched, not the case where anything may. Until NC-148 both
    halves of that sentence were false — the list arrived in the request body,
    and empty meant allow-everything.
  • https only, never downgraded across a redirect, and never to a loopback,
    private or link-local address. That last one matters here specifically: this
    app serves an unauthenticated API on 127.0.0.1, so "fetch this URL" must
    never be a way to reach it.
  • The URL never appears in a response or a log. A pasted link can itself be a
    bearer credential — a Google secret calendar address is the example — so every
    message names only the host. The lesson: `http.client.InvalidURL` subclasses
    `HTTPException`, which is NOT `OSError` and NOT `URLError`, so it escapes
    every except clause and the full secret URL would be returned to the browser
    as `str(exc)`. Hence the bare `except Exception` at the end of `fetch_page`:
    it exists to STOP the exception text reaching the browser, not to diagnose.
"""

import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from typing import Any, Callable, Dict, List, Optional

import config  # noqa: F401 — loads secrets/.env before anything reads os.environ
import db
import intake
from api_core import ApiError

# Where the real reason for a failed fetch or model call goes. It must NOT go
# into the HTTP response: a pasted URL can be a credential, and an SDK exception
# can carry the request it was made with.
log = logging.getLogger("networkcrm.intake")

# Identifies us honestly to whatever is fetched; carries nothing private.
INTAKE_USER_AGENT = "NetworkCRM/1 (local contact intake; stdlib urllib)"
READ_CHUNK = 65536
# Characters of extracted page text handed to the model per page. A profile page
# is a few thousand; the cap bounds both the bill and how much untrusted text is
# in one prompt. Structural, not a setting — see intake.FIELD_MAX_CHARS.
PAGE_TEXT_MAX_CHARS = 12000
# Hostnames that must never be fetched even over https. Literal IPs are checked
# separately (and after DNS resolution) by `_refuse_private_host`.
BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost",
                           "metadata", "metadata.google.internal"})
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|template)\b.*?</\1>",
                        re.IGNORECASE | re.DOTALL)

# NC-366. Page fetches already made for each pasted list. The ceiling
# (intake.web_max_lookups) used to be read only for display and floored at one
# page, so 0 still fetched and 30 links were 30 requests. It is counted here, on
# the server, because `/lookup` holds no database connection and the browser
# cannot be trusted to count its own requests. A list is keyed by a hash of the
# pasted text, never the text itself (a pasted link can be a password), and the
# table forgets the oldest lists past a fixed size so a long-running app cannot
# grow it without end. Forgetting only ever resets a count, which is the same
# as pasting the list again.
LOOKUP_LISTS_REMEMBERED = 200
_lookups_used: Dict[str, int] = {}
_lookups_lock = threading.Lock()


def _paste_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def _lookup_ceiling_message(ceiling: int) -> str:
    """NC-366 / NC-365. What a refused fetch says. Names no setting: this limit
    has no box in Settings, so the sentence says what the person can do."""
    if ceiling <= 0:
        return ("Opening web pages for a pasted list is switched off on this Mac, "
                "so this page was not opened and nothing left this machine. Paste "
                "what you know about the person instead.")
    pages = f"{ceiling} web {'page' if ceiling == 1 else 'pages'}"
    return (f"One pasted list may open at most {pages}, and this list has opened "
            f"{'it' if ceiling == 1 else 'them all'}. This page was not opened and "
            "nothing left this machine. Paste what you know about the person "
            "instead.")


def _spend_lookup(key: str) -> None:
    """Count one page fetch against this pasted list, or refuse it (NC-366).

    Called by `fetch_page` right before the request goes out, so a fetch that
    was refused for another reason (lookup off, a foreign or private address)
    costs nothing, and every request that does leave is counted.
    """
    ceiling = intake.web_max_lookups()
    with _lookups_lock:
        used = _lookups_used.pop(key, 0)
        if used >= ceiling:
            _lookups_used[key] = used
            raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, _lookup_ceiling_message(ceiling))
        _lookups_used[key] = used + 1       # re-inserted last: oldest list goes first
        while len(_lookups_used) > LOOKUP_LISTS_REMEMBERED:
            _lookups_used.pop(next(iter(_lookups_used)))


# ── Egress 1: one page fetch ────────────────────────────────────────────────
class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while it stays on https, and only to a public host.

    urllib's default handler replays the request at whatever Location says,
    including plain http and including 127.0.0.1. Both are refused here: the
    first because a pasted link can be a credential and this would send it in
    clear text, the second because a redirect is otherwise a way to turn "read
    this profile" into "read the unauthenticated API on loopback".
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        parts = urllib.parse.urlsplit(newurl)
        scheme = (parts.scheme or "").lower()
        # urllib's http_error_302 only drains and closes fp on the paths where
        # redirect_request RETURNS, so refusing has to close it here or a
        # long-lived server process leaks a socket per refusal.
        if scheme != "https":
            try:
                fp.close()
            except OSError:
                pass
            raise ApiError(
                HTTPStatus.BAD_GATEWAY,
                f"That address redirected to a non-https ({scheme or 'unknown'}) "
                "location. Refused: a link you paste can be a password, and this "
                "would have sent it in clear text. Nothing was downloaded.")
        try:
            _refuse_private_host(parts.hostname or "")
        except ApiError:
            try:
                fp.close()
            except OSError:
                pass
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _refuse_private_host(hostname: str) -> None:
    """Refuse a hostname that points anywhere on this machine or this network.

    Checked for the pasted URL and again for every redirect hop. DNS is resolved
    here on purpose — a public name can point at 127.0.0.1, and https alone does
    not prevent that. A resolution failure is NOT treated as a refusal: it is let
    through so the real connection error is what the user sees, rather than this
    guard inventing a misleading reason.
    """
    host = (hostname or "").strip().strip("[]").lower()
    if not host:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "That address has no hostname, so nothing was fetched.")
    if host in BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ApiError(
            HTTPStatus.FORBIDDEN,
            f"'{host}' is this machine, so it was not fetched. Intake reads public "
            "pages only — this app's own API answers on loopback with no password.")
    if host.endswith(".local"):
        raise ApiError(
            HTTPStatus.FORBIDDEN,
            f"'{host}' is a name on your own network, so it was not fetched. "
            "Intake reads public pages only.")

    addresses: List[str] = [host]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = [info[4][0] for info in socket.getaddrinfo(host, 443)]
        except OSError:
            return   # let the real connection error speak instead of guessing
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            continue
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                f"'{host}' points to a private or local address, so it was not "
                "fetched. Intake reads public pages only.")


def _https_url(raw: str) -> str:
    """Normalize a pasted address to https, or refuse it.

    No transformation except refusing: silently upgrading http would hide that
    the wrong thing was pasted, and file:// would turn a network setting into a
    local file read.
    """
    url = (raw or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "That address is not an https:// URL, so it was not fetched. Paste the "
            "full link, starting with https://.")
    return url


def page_to_text(body: bytes, content_type: str = "") -> Dict[str, Any]:
    """Fetched bytes → plain text, plus what was dropped getting there.

    Scripts, styles and tags go before anything else does, so no markup and no
    inline JavaScript reaches the model. Pure text handling — no network — so the
    extraction is testable on a fixture. The returned text is UNTRUSTED and the
    caller must label it as such (see `intake.UNTRUSTED_PREAMBLE`).
    """
    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if m:
        charset = m.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = body.decode("utf-8", errors="replace")
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n", text).strip()
    kept = text[:PAGE_TEXT_MAX_CHARS]
    return {"text": kept, "chars_extracted": len(text),
            "chars_dropped": max(0, len(text) - len(kept)),
            "chars_cap": PAGE_TEXT_MAX_CHARS}


def fetch_page(url: str, allowed: Optional[List[str]] = None, *,
               on_request: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """Download one pasted URL as text. NEW EGRESS — see the module docstring.

    Returns {host, text, bytes, chars_extracted, chars_dropped, note}. Every
    refusal raises ApiError with a sentence saying what was refused and why: what
    a lookup declined has to be as visible as what it accepted.

    `on_request` runs after every refusal check and right before the request
    leaves; it may raise to stop it. That is where the per-list ceiling is
    counted (NC-366), so only a request that really goes out is counted.

    The URL never appears in a return value, a message or a log line — only its
    host, which tells an onlooker nothing.
    """
    if not intake.web_lookup_enabled():
        # NC-365. This named NC_INTAKE_WEB. The switch has a box, so the
        # sentence names the box.
        raise ApiError(
            HTTPStatus.FORBIDDEN,
            "Web lookup is turned off, so nothing was fetched and nothing left "
            "this machine. Turn on “Let Intake read a web page” in Settings to "
            "allow it. Pasting names, LinkedIn addresses and emails works fully "
            "without it.")
    candidate = (url or "").strip()
    if not candidate:
        raise ApiError(HTTPStatus.BAD_REQUEST, "url is required")
    allow = {u.strip() for u in (allowed or []) if u}
    if candidate not in allow:
        # Containment: only an address from the user's own paste. A URL a model
        # returned, or one found inside a page we already fetched, lands here.
        #
        # NC-148. This used to read `if allow and candidate not in allow`, so an
        # EMPTY allowlist meant "allow everything" — the one input an attacker
        # controls completely, since it is the absence of a field. A paste with
        # no URL in it, or a caller that simply left the key out, turned intake
        # into a general-purpose https fetcher. Empty now means refuse
        # everything, which is the only reading under which the sentence above
        # this function about containment is true.
        raise ApiError(
            HTTPStatus.FORBIDDEN,
            "That address was not in the text you pasted, so it was not fetched. "
            "Intake only ever opens a link you provided yourself — it never "
            "follows a link it found on a page.")

    full = _https_url(candidate)
    host = urllib.parse.urlsplit(full).hostname or ""
    _refuse_private_host(host)
    limit = intake.web_max_bytes()
    timeout = intake.web_timeout_sec()

    request = urllib.request.Request(full, method="GET", headers={
        "User-Agent": INTAKE_USER_AGENT,
        "Accept": "text/html, text/plain;q=0.8",
        "Accept-Language": "en",
    })
    # A local opener, never install_opener(): installing ours globally would
    # change the redirect policy of every other urllib caller in the process.
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    if on_request is not None:
        on_request()   # NC-366: may refuse; nothing has left the machine yet
    body = b""
    content_type = ""
    # NC-365. The messages below named NC_INTAKE_WEB_MAX_KB, showed the site's
    # status number and the raw error text. None of that has a box or a meaning
    # for the person; each now says what happened and what they can do.
    try:
        with opener.open(request, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").strip()
            declared = resp.headers.get("Content-Length") or ""
            if declared.isdigit() and int(declared) > limit:
                raise ApiError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"{host} offered {int(declared) // 1024} KB, more than the "
                    f"{limit // 1024} KB this app reads from one page. Nothing "
                    "was read. Paste what you know about the person instead.")
            # Read to the cap plus one byte: that extra byte is how a chunked or
            # unbounded response is caught when Content-Length lied or was
            # absent, instead of streaming forever into memory.
            while len(body) <= limit:
                chunk = resp.read(READ_CHUNK)
                if not chunk:
                    break
                body += chunk
    except ApiError:
        raise
    except urllib.error.HTTPError as exc:
        log.info("intake fetch refused by host=%s: status %s", host or "?", exc.code)
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            f"{host} turned the request away, so nothing was read. Many sites, "
            "LinkedIn among them, do not let an app read their pages. Paste what "
            "you know about the person instead.") from exc
    except urllib.error.URLError as exc:
        log.info("intake fetch could not reach host=%s: %s", host or "?", exc.reason)
        what = (f"{host or 'That site'} did not answer within {timeout} seconds"
                if isinstance(exc.reason, TimeoutError)
                else f"Could not reach {host or 'that address'}")
        raise ApiError(HTTPStatus.BAD_GATEWAY,
                       f"{what}. Nothing was read. Try again later, or paste what "
                       "you know about the person instead.") from exc
    except OSError as exc:   # socket.timeout, TLS failures, a dropped connection
        log.info("intake fetch failed for host=%s: %s: %s",
                 host or "?", type(exc).__name__, exc)
        what = (f"{host} stopped answering within {timeout} seconds"
                if isinstance(exc, TimeoutError)
                else f"The connection to {host} failed")
        raise ApiError(HTTPStatus.BAD_GATEWAY,
                       f"{what}, so nothing was read. Try again later, or paste "
                       "what you know about the person instead.") from exc
    except Exception as exc:  # noqa: BLE001 — this clause exists to STOP the
        # exception text reaching the browser, not to diagnose. A pasted link can
        # be a bearer credential, and http.client.InvalidURL — raised for a URL
        # with a stray space, i.e. exactly what a bad paste produces — subclasses
        # HTTPException, which is neither OSError nor URLError. In this repo's
        # calendar fetch it escaped all three clauses above and the full secret
        # URL was returned to the browser as str(exc). The type NAME is safe to
        # show (it is a class name, never a value); the message is not.
        # The type name only: InvalidURL's message quotes the path, which is
        # the part of a link that can be a password ("never ... a log line").
        log.warning("intake fetch failed for host=%s: %s",
                    host or "?", type(exc).__name__)
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            f"The address for {host or 'that link'} could not be used. Check it "
            "for a stray space, and that it starts with https://. Nothing was "
            "read.") from exc

    if len(body) > limit:
        raise ApiError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"{host} kept sending past the {limit // 1024} KB this app reads "
            "from one page, so the read was stopped and nothing was kept. Paste "
            "what you know about the person instead.")

    extracted = page_to_text(body, content_type)
    return {"host": host, "bytes": len(body), "content_type": content_type,
            **extracted,
            "trusted": False,
            "note": (f"Read {len(body)} bytes of text from {host}. This text is "
                     "untrusted: it is treated as information about people and "
                     "never as an instruction. Nothing was uploaded.")}


# ── Egress 2: one model call, with no tools ────────────────────────────────
# Opus 5.5's list prices in US dollars per million tokens, the same as chat.py's
# for the same default model (NC-327).
PRICE_IN_DEFAULT = 4.0
PRICE_OUT_DEFAULT = 20.0


def _price(key: str, default: float) -> float:
    """A per-million-token list price, read at call time.

    Same env keys as `crm/chat.py` so there is one place to correct them, but
    read here rather than captured at import — chat.py's `PRICE_IN` / `PRICE_OUT`
    and its `MODEL` are module-level, so a changed setting would not reach them
    until a restart. Nothing here reads them.
    """
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _spend(resp: Any) -> Dict[str, Any]:
    """What one intake proposal cost. A model call whose price is invisible is a
    model call nobody can budget for."""
    usage = getattr(resp, "usage", None)
    ins = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    billed_in = ins + cache_write * 1.25 + cache_read * 0.1
    usd = (billed_in * _price("NC_PRICE_IN_PER_MTOK", PRICE_IN_DEFAULT)
           + out * _price("NC_PRICE_OUT_PER_MTOK", PRICE_OUT_DEFAULT)) / 1_000_000
    return {"input_tokens": ins, "output_tokens": out,
            "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
            "usd": round(usd, 4), "model": intake.anthropic_model()}


def _user_content(text: str, pages: Optional[List[Dict[str, Any]]] = None) -> str:
    """The one user turn: the pasted text and any fetched pages, each fenced and
    labelled untrusted.

    The markers are a hint to the model, not a security boundary — the boundary
    is that the model has no tools and its output is re-parsed by
    `intake.records_from_model_output`. Labelling still earns its place: it makes
    a page's "ignore previous instructions" read as quoted data rather than as
    the latest turn in the conversation.
    """
    parts = ["=== BEGIN PASTED TEXT (untrusted data) ===",
             text or "", "=== END PASTED TEXT ==="]
    for page in pages or []:
        host = page.get("host") or "a web page"
        parts += [f"=== BEGIN PAGE FETCHED FROM {host} (untrusted data) ===",
                  str(page.get("text") or ""),
                  f"=== END PAGE FETCHED FROM {host} ==="]
    parts.append("Return the JSON array now, and nothing else.")
    return "\n".join(parts)


def _call_model(text: str, pages: Optional[List[Dict[str, Any]]] = None
                ) -> Dict[str, Any]:
    """One Anthropic call. EGRESS. Returns {raw, spend, stop_reason, truncated}.

    **No `tools` argument, by design and permanently.** The model has no
    mechanism to act; its whole output is text that
    `intake.records_from_model_output` then re-parses. Adding a tool here would
    undo the single most important structural decision in this feature, so if a
    future change wants one, it belongs behind a separate confirm-then-write path
    like `intake.commit_batch`, not on this call.

    Raises rather than degrading: the caller (`handle_propose`) owns invariant 8
    and falls back to the offline parser with a visible reason.
    """
    import anthropic  # imported lazily so the app boots fine without the package

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=intake.anthropic_model(),
        max_tokens=intake.model_max_tokens(),
        system=[{"type": "text", "text": intake.SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _user_content(text, pages)}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return {"raw": raw, "spend": _spend(resp),
            "stop_reason": getattr(resp, "stop_reason", ""),
            "truncated": getattr(resp, "stop_reason", "") == "max_tokens"}


# ── Endpoints ───────────────────────────────────────────────────────────────
def _text_arg(body: Dict[str, Any]) -> str:
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "text is required")
    return text


def handle_intake_status() -> Dict[str, Any]:
    """GET /api/intake/status — what works, what is off, and every cap.

    Rendered by the UI so a missing key or a disabled lookup is a visible state
    with a sentence, not a feature that silently does less than it looks like it
    does. Reads no database and makes no request.
    """
    key = intake.llm_available()
    web = intake.web_lookup_enabled()
    return {
        "llm": key,
        "web_lookup": web,
        "model": intake.anthropic_model() if key else "",
        "settings": {
            "NC_INTAKE_PASTE_MAX_CHARS": intake.paste_max_chars(),
            "NC_INTAKE_MAX_PEOPLE": intake.batch_max_people(),
            "NC_INTAKE_WEB": "1" if web else "0",
            "NC_INTAKE_WEB_TIMEOUT_SEC": intake.web_timeout_sec(),
            "NC_INTAKE_WEB_MAX_KB": intake.web_max_bytes() // 1024,
            "NC_INTAKE_WEB_MAX_LOOKUPS": intake.web_max_lookups(),
            "NC_INTAKE_MAX_TOKENS": intake.model_max_tokens(),
        },
        "writes": list(intake.WRITABLE),
        "facts_declined": sorted(intake.FACT_FIELDS_DECLINED),
        "match_keys": list(intake.MATCH_KEYS),
        "linkedin_url_note": intake.WRITE_LINKEDIN_URL_NOTE,
        "sentences": [
            "Paste names, LinkedIn addresses, emails and phone numbers. One "
            "person per line.",
            # NC-269. This said "A model reads the paste" and then "Nothing
            # leaves this machine" with a key set: the model read sends the
            # paste to Anthropic, so the second sentence was false for it.
            ("Read it with the model sends the pasted text to Anthropic, and the "
             "model suggests who is in it. The plain read stays on this Mac." if key else
             "No API key is set, so the built-in reader reads the paste. This "
             "works fully offline."),
            ("Web lookup is on. Intake can open a link you pasted." if web else
             "Web lookup is off, so no link you paste is opened."),
            *(["Nothing leaves this machine."] if not key and not web else []),
            # NC-365. Both apps printed `writes`, `match_keys` and every cap's
            # setting name raw ("It matches on name_norm, phone_last9 ..."). These
            # two sentences say the same in words, from the one place both read.
            "It saves a name, company, job title, how you met, LinkedIn address, "
            "emails, phone numbers and a note. It never saves a city, topics or a "
            "description.",
            "It knows someone is already in your list by their name, email, phone "
            "number or LinkedIn address, and it never adds anyone in Hidden "
            "contacts.",
            "Nothing is saved until you review the list and confirm it.",
        ],
    }


def handle_intake_parse(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/intake/parse — the offline parse: no key, no network.

    Body: {text}. Returns {proposals, report, allowed_lookup_urls}. Writes
    nothing. This is the endpoint the feature is built on, not a fallback for
    when something else fails.
    """
    text = _text_arg(body)
    planned = intake.plan(conn, text)
    return {"proposals": planned["proposals"],
            "report": planned["report"],
            "allowed_lookup_urls": intake.allowed_lookup_urls(planned["records"]),
            "wrote_anything": False}


def handle_intake_propose(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/intake/propose — the model-assisted parse.

    Body: {text, pages?}. `pages` is what a previous `/api/intake/lookup` call
    returned; this endpoint never fetches anything itself, so one click cannot
    both read the web and call a model without two separate acts.

    Degrades VISIBLY, per invariant 8. With no key — or when the call fails, or
    when the model returns nothing usable — it runs the offline parser and sets
    `report.degraded` to a sentence saying which path ran and why. The return
    shape is identical either way, so the UI never has a second code path that
    could rot.
    """
    text = _text_arg(body)
    pages = [p for p in (body.get("pages") or []) if isinstance(p, dict)]
    # Only page text this server produced is accepted, and it is used as prompt
    # material only — never as a source of records on its own.
    #
    # NC-366. This was `[:max(1, …)]`, so a ceiling of 0 still used a page. No
    # more pages than the ceiling allows reach the model, and the rest are
    # counted so the report can say they were not used.
    pages = [{"host": str(p.get("host") or "")[:200],
              "text": str(p.get("text") or "")[:PAGE_TEXT_MAX_CHARS]}
             for p in pages]
    page_cap = intake.web_max_lookups()
    pages_unused = max(0, len(pages) - page_cap)
    pages = pages[:page_cap]

    if not intake.llm_available():
        planned = intake.plan(conn, text)
        planned["report"]["degraded"] = (
            "No API key is set, so the built-in reader read your paste instead of "
            "a model. Nothing was lost: this path needs no key. Add your key from "
            "Anthropic in Settings to use the model.")
        planned["report"]["path"] = "offline_parser"
        return {"proposals": planned["proposals"], "report": planned["report"],
                "allowed_lookup_urls": intake.allowed_lookup_urls(planned["records"]),
                "wrote_anything": False}

    offline = intake.parse_block(text)
    allowed = intake.allowed_lookup_urls(offline["records"])
    try:
        # NC-368. Only the part of the paste the report says was read goes to
        # the model. The whole text used to, so people past the character
        # limit could be proposed while the report said they were not read.
        called = _call_model(text[:intake.paste_max_chars()], pages)
    except Exception as exc:  # noqa: BLE001 — a model hiccup degrades, never 500s
        # The message goes to the log, not the response: an SDK exception can
        # quote the request, and the request contains the pasted text. The type
        # name stays in the log too: "(RuntimeError)" on screen helped nobody.
        log.warning("intake model call failed: %s: %s", type(exc).__name__, exc)
        planned = intake.plan(conn, text)
        planned["report"]["degraded"] = (
            "The model could not be reached, so the built-in reader read your "
            "paste instead. Nothing was lost and nothing was written.")
        planned["report"]["path"] = "offline_parser"
        return {"proposals": planned["proposals"], "report": planned["report"],
                "allowed_lookup_urls": allowed, "wrote_anything": False}

    # THE BOUNDARY. Untrusted model text, re-parsed into the same records the
    # offline parser produces, with every dropped key counted.
    contained = intake.records_from_model_output(
        called["raw"], origin="llm" if not pages else "web",
        allowed_urls=allowed)
    meta = {k: v for k, v in contained.items() if k != "records"}
    meta["spend"] = called["spend"]
    meta["truncated"] = called["truncated"]
    meta["pages_used"] = len(pages)
    meta["pages_unused"] = pages_unused

    if not contained["records"]:
        planned = intake.plan(conn, text, model_meta=meta)
        planned["report"]["degraded"] = (
            "The model returned nothing usable, so the built-in reader read your "
            "paste instead. Its reply is accounted for in the report.")
        planned["report"]["path"] = "offline_parser"
        return {"proposals": planned["proposals"], "report": planned["report"],
                "allowed_lookup_urls": allowed, "wrote_anything": False}

    planned = intake.plan(conn, text, model_records=contained["records"],
                          model_meta=meta)
    planned["report"]["path"] = "model"
    planned["report"]["degraded"] = ""
    extra: List[str] = []
    if called["truncated"]:
        # NC-365. Said "Raise NC_INTAKE_MAX_TOKENS", which has no box.
        extra.append("The model ran out of room for its answer, so the list may "
                     "be incomplete. Paste fewer people at a time.")
    if pages_unused:
        extra.append(f"{pages_unused} web {'page' if pages_unused == 1 else 'pages'} "
                     f"you opened {'was' if pages_unused == 1 else 'were'} not used, "
                     + (f"because one list may use at most {page_cap}." if page_cap
                        else "because using web pages for a pasted list is "
                             "switched off on this Mac."))
    if extra:
        planned["report"]["sentences"] += extra
        planned["report"]["summary_text"] = " ".join(planned["report"]["sentences"])
    return {"proposals": planned["proposals"], "report": planned["report"],
            "allowed_lookup_urls": allowed, "wrote_anything": False}


def handle_intake_lookup(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/intake/lookup — fetch ONE pasted address as text. Opt-in.

    Body: {url, text}. Off unless NC_INTAKE_WEB is "1", one URL per call, and
    only an address THIS SERVER found in the pasted text. Writes nothing and
    calls no model: the fetched text goes back to the browser, and the owner
    decides whether to spend a model call on it. That separation is deliberate —
    one click must not be able to both leave the machine and run up a bill.

    NC-148. The allowlist used to arrive as `allowed_urls` in the request body,
    which meant the containment this endpoint is built on was enforced with a
    list supplied by whoever was calling it. It is now re-derived here from the
    paste, with `parse_block` — the same function `/parse` and `/propose` use,
    no database and no network — so the answer to "did this address come from
    the owner's own text" is computed from the text rather than asserted by the
    caller. `allowed_urls` in the body is ignored; a request that sends it and
    no `text` is refused, which is the right way round to fail.
    """
    url = str(body.get("url") or "").strip()
    text = str(body.get("text") or "")
    allowed = intake.allowed_lookup_urls(intake.parse_block(text)["records"])
    # NC-366. The ceiling is per pasted list: this paste's fetches are counted,
    # and the one past `intake.web_max_lookups()` is refused before it leaves.
    key = _paste_key(text)
    page = fetch_page(url, allowed, on_request=lambda: _spend_lookup(key))
    return {"page": page, "wrote_anything": False,
            # The button both apps show is "Read it with the model"; there is
            # no "Propose" anywhere a person can see.
            "sentences": [page["note"],
                          "This text has not been sent to a model. Press “Read "
                          "it with the model” to use it."]}


def handle_intake_commit(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/intake/commit — write a reviewed batch. THE ONLY WRITER.

    Body: {batch: [...], confirm: true}. No model runs and no request leaves the
    machine on this path. `intake.commit_batch` re-runs the matcher and ignores
    any contact_id, verdict or ref the caller sent, so this endpoint cannot be
    used to write to a row the caller names — only to describe people and let the
    server decide who they are.

    A body without `confirm: true` is refused with a receipt rather than a 400,
    so the UI renders the same component for "nothing was written" as for a real
    result.
    """
    batch = body.get("batch")
    if not isinstance(batch, list):
        raise ApiError(HTTPStatus.BAD_REQUEST, "batch must be a list")
    items = [b for b in batch if isinstance(b, dict)]
    dropped = len(batch) - len(items)
    if len(items) > intake.batch_max_people():
        # NC-365. Named NC_INTAKE_MAX_PEOPLE and said "raise the setting",
        # which has no box in Settings.
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{len(items)} people were sent at once, and one save takes at most "
            f"{intake.batch_max_people()}. Nothing was written. Skip some of them, "
            "save, then save the rest.")
    receipt = intake.commit_batch(conn, items,
                                  # Only the JSON value true confirms: bool("false")
                                  # is True in Python, so a string must never pass.
                                  confirmed=body.get("confirm") is True)
    if dropped:
        receipt["malformed_items_dropped"] = dropped
        receipt["sentences"].append(
            f"{dropped} submitted entries were not readable and were ignored.")
        receipt["summary_text"] = " ".join(receipt["sentences"])
    if receipt.get("wrote_anything"):
        # The reviewed draft is spent. Leaving it parked would make a reload
        # re-offer people who already exist.
        intake.clear_pending(conn)
    return receipt


def handle_intake_batch_get(conn: Any) -> Dict[str, Any]:
    """GET /api/intake/batch — the parked draft, or {}.

    Held in the existing `state` key/value table (`intake.STATE_BATCH_KEY`), the
    same store api_import uses for its per-source sync stamps. No new table and
    no schema change: this feature adds neither.
    """
    return {"batch": intake.load_pending(conn)}


def handle_intake_batch_post(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/intake/batch — park the draft so a reload does not lose it.

    A draft, not a queue: parking it writes nothing to `contacts`. Size is capped
    against the same person limit, and the stored byte count comes back so a
    caller can see what it cost.
    """
    payload = body.get("batch")
    if not isinstance(payload, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "batch must be an object")
    proposals = payload.get("proposals")
    if isinstance(proposals, list) and len(proposals) > intake.batch_max_people():
        # NC-365. Same: no setting name, and a thing the person can do.
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"A draft holds at most {intake.batch_max_people()} people. Nothing "
            "was saved. Paste a shorter list, then save the draft again.")
    size = intake.save_pending(conn, payload)
    return {"ok": True, "bytes": size, "wrote_anything": False,
            "sentences": ["The draft was saved. Nobody has been added yet."]}


def handle_intake_batch_delete(conn: Any) -> Dict[str, Any]:
    """DELETE /api/intake/batch — discard the draft. Touches no contact."""
    intake.clear_pending(conn)
    return {"ok": True, "wrote_anything": False,
            "sentences": ["The draft was discarded. No contact was changed."]}


# ── Route wiring ─────────────────────────────────────────────────────────
# The route registry reads ROUTES below, so app.py's route tables stay untouched.
# `/api/intake/lookup` deliberately takes no connection: the fetch path must not
# hold an open crm.db handle while it waits on a socket.
ROUTES = {
    "GET": {"/api/intake/status": "handle_intake_status",
            "/api/intake/batch": "handle_intake_batch_get"},
    "POST": {"/api/intake/parse": "handle_intake_parse",
             "/api/intake/propose": "handle_intake_propose",
             "/api/intake/lookup": "handle_intake_lookup",
             "/api/intake/commit": "handle_intake_commit",
             "/api/intake/batch": "handle_intake_batch_post"},
    "DELETE": {"/api/intake/batch": "handle_intake_batch_delete"},
}


def main() -> None:
    """`python3 api_intake.py '<pasted text>'` — prints what /api/intake/parse
    would return, so the endpoint shape can be checked without the server."""
    import sys
    text = " ".join(sys.argv[1:]) or sys.stdin.read()
    db.init_db()
    conn = db.get_db()
    try:
        print(json.dumps(handle_intake_parse(conn, {"text": text}),
                         indent=2, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
