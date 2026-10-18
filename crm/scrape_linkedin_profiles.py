#!/usr/bin/env python3
"""
Ground-truth facts enrichment by visiting each 1st-degree connection's real
LinkedIn profile page — the authoritative complement to enrich_facts.py, whose
passes only *guess* location/topics from indirect signals (phone codes, message
text, LLM inference). Here we read what the person actually states: their
location, their About section, and their top skills.

The account that logs in is 1st-degree-connected to everyone this touches, so it
has permission to read their profiles — but automated browsing violates LinkedIn's
ToS, so this is deliberately slow, conservative, and meant to run on a dedicated,
isolated machine (for example a spare Mac). It never solves CAPTCHAs and stops the
moment LinkedIn shows any security interstitial.

Browser stack — Selenium driving the *system-installed* Firefox (NOT Playwright):
Selenium is pure-Python and drives the real Firefox already on the machine, with
no extra browser download and no native dependency to compile. The login session
is persisted in a dedicated Firefox profile directory.

Ban-avoidance posture (this is a ToS-gray automation — the goal is: never earn a
PERMANENT ban; a temporary throttle is acceptable):
  • Hard stop, no retry, the instant a security/rate-limit page appears — this is
    the main thing that keeps a throttle from escalating to a ban.
  • Randomized 20–50 s gaps between profiles + a 3–6 min break every 8–10 views,
    hard --limit per run, and human-like scrolling on each page.
  • Real logged-in Firefox profile + best-effort hiding of navigator.webdriver.
  CAVEAT: navigator.webdriver hiding via dom.webdriver.enabled is best-effort;
  some geckodriver versions re-assert it, so treat volume/pacing + the immediate
  stop-on-block as the real protection, not fingerprint spoofing. Keep DAILY
  volume modest and run at plausible daytime hours, not 24/7.

Pass D (this script):
  • Reuse a persistent, manually-authenticated Firefox profile (see --login).
  • For each candidate: open their profile ONCE and capture as much as that single
    load gives — name, headline, location, every section (About, Experience,
    Education, Licenses, Volunteering, Projects, Honors, Interests), top skills,
    and the mutual-connection / connection counts shown in the intro card.
  • Archive the full result to data/li_raw/<id>.{html.gz,json} so a profile never
    has to be revisited — re-parse the saved HTML later for any new field.
  • Best-effort profile-photo capture: pull the display-photo URL straight out of
    the captured page HTML and download the JPEG to data/li_raw/<id>.jpg via the
    licdn CDN. Never blocks or fails a contact — a missing photo is fine. Because
    the signed URLs live in the archived HTML (~2 weeks), photos for
    already-scraped profiles can be backfilled later with --backfill-photos, with
    zero new LinkedIn visits.
  • Feed the captured text to the SAME Claude Haiku prompt as enrich_facts Pass C
    and store {city, country, metro, topics, role_type, blurb} with
    facts_source='linkedin_scrape'.
  • Never clobbers a 'manual' edit; re-scrapes 'linkedin_scrape' rows only after
    180 days but overrides weaker sources (offline / message_scan / llm) freely.
  NOTE: the full list of mutual connections is deliberately NOT crawled — it needs
  extra paginated page loads, which raises ban risk. Only the count/preview that
  the main profile page already shows is captured.

ONE MACHINE? You need NONE of merge_scraped_facts.py / import_scraped_photos.py /
enrich_li_extras.py / pull_and_merge.sh / the liscraper LaunchAgent (NC-012). Those
all exist to bridge a SEPARATE scraper machine's crm.db copy back to this one; the
line below writes facts straight into THIS Mac's own db.DB_PATH, so if this script
runs where the app runs, the facts are already in the database the app reads the
moment this process commits — no pull, no merge, no SSH. That whole apparatus is
only for the two-machine setup (a separate, isolated scraper Mac, per the
ban-avoidance posture above) — see merge_scraped_facts.py's docstring for the full story.

First-time setup (once, on the target machine):
  pip install selenium anthropic
  # geckodriver: Selenium 4 auto-downloads it; if that fails, drop a
  # geckodriver binary on PATH or set GECKODRIVER_PATH. Firefox itself must be
  # installed at /Applications/Firefox.app (override with FIREFOX_BIN).

Usage:
  python crm/scrape_linkedin_profiles.py --login          # one-time: log into LinkedIn (opens a visible Firefox)
  python crm/scrape_linkedin_profiles.py                  # scrape next 25 candidates
  python crm/scrape_linkedin_profiles.py --limit 10
  python crm/scrape_linkedin_profiles.py --dry-run        # show candidates, no browser
  python crm/scrape_linkedin_profiles.py --backfill-photos # download photos from already-saved HTML (no browser, no LinkedIn visits)
"""

import argparse
import gzip
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402 — loads .env
import db  # noqa: E402
# Reuse the Pass C prompt and the change-applying helper — do not duplicate them.
from enrich_facts import LLM_SYSTEM, _apply_changes  # noqa: E402


# ── Constants ─────────────────────────────────────────────────────────────────
FF_PROFILE_DIR = config.secret_path("ff_profile")   # persistent login session
RAW_DIR = config.data_path("li_raw")                 # raw HTML + JSON per profile (revisitable)
# NC-095: both of these are db's, not this file's. The token was a second
# literal and the window was hardcoded here while the Data page read
# NC_LI_RESCRAPE_DAYS — so the panel's "N queued" and this run's actual queue
# disagreed by construction the moment anyone set that variable.
FACTS_SOURCE = db.LI_FACTS_SOURCE
RESCRAPE_AFTER_DAYS = db.li_rescrape_days()   # kept for --help and log lines

LLM_MODEL = "claude-haiku-4-5-20251001"

# Exit codes — the dispatcher relies on these to tell a real LinkedIn block (back
# off for hours) apart from a technical hiccup (just retry next run):
#   0 = ok, 1 = technical error (bad launch, crash, config), 3 = LinkedIn checkpoint
EXIT_OK, EXIT_ERROR, EXIT_CHECKPOINT = 0, 1, 3

# Firefox / geckodriver locations (overridable via env for other machines).
FIREFOX_BIN = os.environ.get("FIREFOX_BIN", "/Applications/Firefox.app/Contents/MacOS/firefox")
GECKODRIVER_PATH = os.environ.get("GECKODRIVER_PATH", "")   # "" → let Selenium Manager find it

# Rate limiting — these protect the account from a ban. Tune with care.
MIN_DELAY_SECONDS = 20.0            # random.uniform lower bound between profiles
MAX_DELAY_SECONDS = 50.0           # random.uniform upper bound between profiles
BREAK_EVERY_MIN = 8                # take a long break after this many profiles…
BREAK_EVERY_MAX = 10               # …picked randomly in this range
LONG_BREAK_MIN_SECONDS = 3 * 60    # long break lower bound
LONG_BREAK_MAX_SECONDS = 6 * 60    # long break upper bound

# LinkedIn's SPA leaks memory over a long session and the host may be short on
# memory — so relaunch the browser every 5–8 profiles to flush it. The
# persistent Firefox profile means we stay logged in across the relaunch.
RECYCLE_MIN = 5
RECYCLE_MAX = 8

PAGE_LOAD_TIMEOUT_S = 30
SETTLE_MIN_SECONDS = 2.0           # random settle after load (let lazy content render)
SETTLE_MAX_SECONDS = 4.5
WINDOW_SIZE = (1280, 900)          # a normal desktop viewport (avoid a headless 0x0 fingerprint)

# LinkedIn security-interstitial fingerprints. If any of these show up we stop.
# Fail-safe: it is always better to halt on a false positive than to push through
# a real block — pushing through is what turns a temporary throttle into a ban.
CHECKPOINT_URL_MARKERS = ("checkpoint", "authwall", "uas/login", "/login")
CHECKPOINT_TEXT_MARKERS = (
    # hard security / CAPTCHA interstitials
    "unusual activity", "security check", "verify your identity",
    "let's do a quick security check", "please verify", "captcha",
    "we've noticed some unusual", "help us keep your account safe",
    # soft rate-limit / usage-limit pages — back off BEFORE they escalate
    "commercial use limit", "reached the weekly limit", "reached the monthly limit",
    "you've reached the", "browsing too fast", "viewing profiles too quickly",
    "unusual amount of activity", "account has been restricted",
    "we've restricted", "try again later",
)

# Best-effort CSS selectors. LinkedIn's DOM drifts constantly, so every read is
# wrapped in try/except and we keep whatever partial data we managed to get.
# The real safety net is that we ALSO save the raw HTML (see save_raw): if these
# miss, nothing is lost — re-parse the saved file later without revisiting LinkedIn.
NAME_SELECTORS = ("h1",)
HEADLINE_SELECTORS = (
    "div.text-body-medium.break-words",
    ".pv-text-details__left-panel .text-body-medium",
)
LOCATION_SELECTORS = (
    "div.mt2.relative span.text-body-small.inline.t-black--light.break-words",
    "span.text-body-small.inline.t-black--light.break-words",
    ".pv-text-details__left-panel .text-body-small",
)
SKILL_SELECTORS = (
    "section[data-section='skills'] span[aria-hidden='true']",
    "div#skills ~ div span[aria-hidden='true']",
)
MAX_SKILLS = 10

# Profile sections keyed by their stable anchor <div id="…"> — we grab the whole
# section's visible text (robust to inner-markup drift) and both save it and feed
# it to the LLM. This captures far more than location alone: work history,
# education, certs, volunteering, interests (followed companies/people).
PROFILE_SECTION_ANCHORS = {
    "about": "about",
    "experience": "experience",
    "education": "education",
    "licenses": "licenses_and_certifications",
    "volunteering": "volunteering_experience",
    "projects": "projects",
    "honors": "honors_and_awards",
    "interests": "interests",
}
SECTION_TEXT_CAP = 2500            # per-section char cap when saving / prompting


# ── Candidate selection ───────────────────────────────────────────────────────
def pick_candidates(conn, limit: int) -> list:
    """Contacts worth (re)scraping, MOST-RELEVANT-FIRST.

    Ordering is by the CRM's own relationship-strength (familiarity) score, high
    to low — so the people who matter most to you get real location/interest data
    first, and the long tail fills in over later runs. Ties break to
    never-scraped-first, then least-recently-updated, for stable ordering.

    Eligibility (mirroring enrich_facts' 'never clobber manual' convention):
      • has a linkedin_url, not excluded, NOT SOFT-DELETED, not a manual edit;
      • either never scraped before, or the last scrape is older than the
        rescrape window — so we override weaker sources (offline / message_scan
        / llm / empty) but leave fresh scrapes alone.

    NC-095: the clause is db.li_queue_where(), the same one the Data page counts
    to render "N queued" on the other machine. Keeping a single copy means the
    panel's promise and this query are the same sentence: a second copy would
    drift (a missing `deleted` filter, no null-safety, its own copy of the
    "linkedin_scrape" literal).
    """
    cutoff = db.li_scrape_cutoff()
    where, params = db.li_queue_where(cutoff)
    rows = conn.execute(
        f"SELECT * FROM contacts WHERE {where}", params).fetchall()

    def _rank(r):
        never_scraped = 0 if (r["facts_source"] or "") == FACTS_SOURCE else 1
        return (-db.relationship_strength(r),    # highest familiarity first
                -never_scraped,                   # then never-scraped before re-scrapes
                r["facts_updated_at"] or "",      # then least-recently-updated
                r["id"])

    return sorted(rows, key=_rank)[:limit]


# ── Browser ─────────────────────────────────────────────────────────────────
def _release_profile_lock() -> None:
    """Kill any Firefox still holding our dedicated profile and clear stale lock
    files, so every launch self-heals. Covers three cases that would otherwise
    make Firefox exit instantly ('profile in use') and fail the run:
      • an unclean shutdown (power loss) mid-scrape leaving a stale .parentlock,
      • the browser-recycle relaunch racing its own quit,
      • a leftover login/headed Firefox holding the profile.
    Only touches Firefox processes whose command line references OUR profile dir."""
    try:
        subprocess.run(["pkill", "-f", FF_PROFILE_DIR], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
    except Exception:
        pass
    for lock in (".parentlock", "lock", "MarionetteActivePort"):
        try:
            os.remove(os.path.join(FF_PROFILE_DIR, lock))
        except OSError:
            pass


def make_driver(headless: bool):
    """Build a Firefox WebDriver bound to the system Firefox and the persistent
    login profile. Import selenium lazily so --dry-run needs neither it nor a
    browser."""
    try:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
    except ImportError:
        print("selenium not installed. Run: pip install selenium", file=sys.stderr)
        sys.exit(1)

    os.makedirs(FF_PROFILE_DIR, exist_ok=True)
    _release_profile_lock()      # self-heal stale locks before every launch
    opts = Options()
    if os.path.exists(FIREFOX_BIN):
        opts.binary_location = FIREFOX_BIN
    # Reuse one fixed profile dir so the --login session's cookies persist.
    opts.add_argument("-profile")
    opts.add_argument(FF_PROFILE_DIR)
    if headless:
        opts.add_argument("-headless")

    # Reduce automation fingerprints. dom.webdriver.enabled=false is a best-effort
    # attempt to hide navigator.webdriver — LinkedIn's strongest single bot signal
    # (see caveat in the module notes: newer geckodriver may re-assert it). The
    # rest keep the session looking like an ordinary Firefox and suppress popups.
    opts.set_preference("dom.webdriver.enabled", False)
    opts.set_preference("useAutomationExtension", False)
    opts.set_preference("dom.webnotifications.enabled", False)
    opts.set_preference("media.peerconnection.enabled", False)

    # Cap memory growth: small memory cache, minimal back/forward history, and
    # don't keep rendered old pages around — blunts LinkedIn's SPA leak per session
    # (recycling the whole browser every few profiles is the real safeguard).
    opts.set_preference("browser.cache.memory.capacity", 51200)   # KB (~50 MB)
    opts.set_preference("browser.sessionhistory.max_entries", 5)
    opts.set_preference("browser.sessionhistory.max_total_viewers", 0)

    service = Service(executable_path=GECKODRIVER_PATH) if GECKODRIVER_PATH else Service()
    driver = webdriver.Firefox(options=opts, service=service)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_S)
    try:
        driver.set_window_size(*WINDOW_SIZE)
    except Exception:
        pass
    return driver


def make_driver_resilient(headless: bool, attempts: int = 2):
    """make_driver() with a retry. On a machine under memory pressure, Firefox can
    hang on launch (Selenium times out talking to geckodriver) — that's a local glitch,
    NOT a LinkedIn block, so we clean up and retry rather than treating it as one.
    If it still won't start, exit EXIT_ERROR (technical) so the dispatcher just
    retries next tick instead of triggering a multi-hour cooldown."""
    for n in range(1, attempts + 1):
        try:
            return make_driver(headless=headless)
        except SystemExit:
            raise                       # missing selenium etc. — already fatal
        except Exception as exc:
            print(f"  browser launch attempt {n}/{attempts} failed: {exc}",
                  file=sys.stderr, flush=True)
            _release_profile_lock()
            time.sleep(random.uniform(3.0, 6.0))
    print("Could not start Firefox after retries — technical error (not a block).",
          file=sys.stderr, flush=True)
    sys.exit(EXIT_ERROR)


def run_login() -> None:
    """Open a visible Firefox on LinkedIn's login page, wait for the user to sign
    in by hand, then quit — the persistent profile keeps the cookies.

    Deliberately simple: we never automate credential entry (exactly what trips
    LinkedIn's defenses). The user logs in themselves and presses Enter."""
    driver = make_driver(headless=False)
    try:
        driver.get("https://www.linkedin.com/login")
        print("A Firefox window has opened. Log in to LinkedIn there (solve any")
        print("checkpoint / 2FA yourself), wait until your feed is visible, then")
        print("come back here.")
        try:
            input("Press Enter once you are fully logged in… ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted — session may be incomplete.", file=sys.stderr)
            return
        cur = (driver.current_url or "").lower()
        if "login" in cur or "checkpoint" in cur:
            print("WARNING: still on a login/checkpoint URL — the session may not",
                  file=sys.stderr)
            print("         have saved. If scraping fails, re-run --login.", file=sys.stderr)
        else:
            print("Login looks good — session saved to the Firefox profile.")
    finally:
        driver.quit()


# ── Checkpoint detection ──────────────────────────────────────────────────────
def is_checkpoint(url: str, page_text: str) -> bool:
    """True if the page looks like a LinkedIn security / CAPTCHA interstitial."""
    u = (url or "").lower()
    if any(m in u for m in CHECKPOINT_URL_MARKERS):
        return True
    t = (page_text or "").lower()
    return any(m in t for m in CHECKPOINT_TEXT_MARKERS)


# ── Extraction ────────────────────────────────────────────────────────────────
def _first_text(driver, selectors: tuple) -> str:
    """Return the trimmed text of the first selector that matches, else ''.
    Each read is isolated so a drifted/absent selector never aborts the run."""
    from selenium.webdriver.common.by import By
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                txt = (el.text or "").strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


def _collect_skills(driver, selectors: tuple, limit: int) -> list:
    """Grab the top few visible skill labels without opening any modal."""
    from selenium.webdriver.common.by import By
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if not els:
                continue
            out: list = []
            for el in els:
                try:
                    txt = (el.text or "").strip()
                except Exception:
                    continue
                if txt and txt not in out:
                    out.append(txt)
                if len(out) >= limit:
                    break
            if out:
                return out
        except Exception:
            continue
    return []


def _human_scroll(driver) -> None:
    """Scroll the page in a few uneven steps. Serves two purposes: it triggers
    LinkedIn's lazy-loaded About/Skills sections (which sit below the fold), and
    it makes the visit look less like an instant headless bot hit and more like
    someone actually reading the profile. Best-effort — never fatal."""
    try:
        # Scroll down in growing increments until the page stops getting taller
        # (LinkedIn lazy-loads sections as you approach them, so a fixed-fraction
        # scroll misses them — the height only grows once each section loads).
        # Uneven step sizes + pauses also read like a human skimming.
        last_h = 0
        for step in range(12):
            driver.execute_script("window.scrollBy(0, arguments[0]);",
                                  random.randint(550, 1100))
            time.sleep(random.uniform(0.5, 1.3))
            h = driver.execute_script("return document.body.scrollHeight;") or 0
            if h <= last_h and step >= 4:
                break
            last_h = h
        # drift back up, like re-reading the top
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.3);")
        time.sleep(random.uniform(0.4, 1.0))
    except Exception:
        pass


def _body_text(driver) -> str:
    from selenium.webdriver.common.by import By
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return ""


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """Strip a page's HTML to plain visible text. Used as the primary signal for
    the LLM: robust to LinkedIn's constant DOM/class changes (which break CSS
    selectors) because it just reads whatever text the page rendered."""
    if not html:
        return ""
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = _TAG_RE.sub(" ", t)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"),
                 ("&quot;", '"'), ("&nbsp;", " "), ("&apos;", "'")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _section_text(driver, anchor_id: str) -> str:
    """Whole visible text of the profile section whose stable anchor is
    <… id='anchor_id'>. Robust to inner-markup drift; capped for sanity."""
    from selenium.webdriver.common.by import By
    try:
        el = driver.find_element(
            By.XPATH, f"(//*[@id='{anchor_id}']/ancestor::section)[1]")
        return (el.text or "").strip()[:SECTION_TEXT_CAP]
    except Exception:
        return ""


def _scan_line(body_text: str, needle: str, maxlen: int = 120) -> str:
    """First short line of the page text containing `needle` (case-insensitive).
    Used for free extras that live in the intro card: mutual-connection count,
    connection/follower counts."""
    needle = needle.lower()
    for line in (body_text or "").splitlines():
        s = line.strip()
        if needle in s.lower() and 0 < len(s) <= maxlen:
            return s
    return ""


# ── Profile photo ─────────────────────────────────────────────────────────────
# The CDN 403s a default Python UA but serves this browser UA + LinkedIn referer
# combination. Kept as constants so the scrape path and the
# --backfill-photos path use the exact same request shape.
PHOTO_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15")
PHOTO_REFERER = "https://www.linkedin.com/"
PHOTO_ACCEPT = "image/avif,image/webp,image/png,image/jpeg,*/*"
PHOTO_TIMEOUT_S = 20               # per-download cap — a slow CDN must never hang a run
PHOTO_MAX_BYTES = 5 * 1024 * 1024  # sanity cap (real display photos are tens of KB)

# Largest render first: prefer 400px, fall back to 200 then 100. LinkedIn writes
# the URL into the page HTML with '&' escaped as '&amp;'; the size marker appears
# as profile-displayphoto-(scale|shrink)_<N>_<N>. Char class stops at quotes /
# whitespace / angle brackets / backslash so a match can't run past one URL.
_PHOTO_SIZES = (400, 200, 100)
_PHOTO_URL_RE = {
    size: re.compile(
        r"https://media\.licdn\.com/dms/image/"
        r"[^\s\"'<>\\]*?profile-displayphoto-(?:scale|shrink)_%d_%d"
        r"[^\s\"'<>\\]*" % (size, size))
    for size in _PHOTO_SIZES
}


def _after_global_nav(html: str) -> str:
    """The page with LinkedIn's chrome removed, so a photo search cannot see the
    viewer's own avatar.

    Tries the landmarks in order of how structural they are: the opening <main> tag,
    then the end of the nav element, then the known nav class names. The FIRST one
    found wins and everything before it is discarded. Returns the input unchanged
    when none is present, because a scrape that finds no photo is a much smaller
    problem than one that finds the wrong face.
    """
    low = (html or "").lower()
    for landmark in ("<main", "</nav>", "global-nav__me", "global-nav "):
        cut = low.find(landmark)
        if cut != -1:
            # </nav> and the class names are BEFORE the content, so skip past them;
            # <main> begins the content, so keep it.
            return html[cut:] if landmark == "<main" else html[cut + len(landmark):]
    return html


def extract_photo_url(html: str) -> str:
    """Best-effort: pull the largest LinkedIn profile-photo URL out of a page's
    raw HTML. Prefers the 400px 'profile-displayphoto-(scale|shrink)_400_400'
    render, falling back to 200 then 100. Returns the de-escaped URL ('&amp;' →
    '&') or '' if none is found. Regex-based and never throws — LinkedIn's markup
    drifts, so a miss just means no photo this pass."""
    if not html:
        return ""
    # STRIP THE VIEWER'S OWN AVATAR FIRST. Searching the whole document would also
    # find the logged-in user's photo, which sits in LinkedIn's global nav under the
    # SAME profile-displayphoto-* pattern. So on a profile whose person has no photo, the
    # first match in the page would be the scraping account's own face — saved as
    # that person's portrait, on every contact with no photo of their own.
    #
    # Cutting at the end of the nav is deliberate rather than matching the top card:
    # a "where the subject's photo lives" selector has to track LinkedIn's markup
    # drift, while "everything before the main element is chrome, not the subject" is
    # structural and stays true. If no landmark is found the whole document is
    # searched as before — a miss means no photo, and the guard in photos.save_photo
    # is the second line of defence.
    html = _after_global_nav(html)
    try:
        for size in _PHOTO_SIZES:
            m = _PHOTO_URL_RE[size].search(html)
            if not m:
                continue
            url = m.group(0)
            # A match can overrun the real URL end when it sits inside an embedded
            # JSON blob (delimited by escaped quotes, not raw ones). Trim at the
            # first entity/escape that can't appear in a URL, THEN de-escape the
            # surviving '&amp;' query separators.
            for stop in ("&quot;", "&#34;", "&#39;", "&#039;", "&apos;",
                         "&lt;", "&gt;", "\\"):
                cut = url.find(stop)
                if cut != -1:
                    url = url[:cut]
            return url.replace("&amp;", "&")
    except Exception:
        return ""
    return ""


def download_image(url: str) -> "bytes | None":
    """Fetch image bytes for a licdn URL with the browser UA + LinkedIn referer
    (the CDN 403s a default Python UA but serves this). Returns the bytes only if
    they look like a real JPEG/PNG (magic-byte checked), else None. Never throws —
    any failure (403, timeout, oversize, non-image) yields None, so a missing
    photo can never break the caller."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": PHOTO_UA,
            "Referer": PHOTO_REFERER,
            "Accept": PHOTO_ACCEPT,
        })
        with urllib.request.urlopen(req, timeout=PHOTO_TIMEOUT_S) as resp:
            data = resp.read(PHOTO_MAX_BYTES + 1)
    except Exception:
        return None
    if not data or len(data) > PHOTO_MAX_BYTES:
        return None
    # Validate by magic bytes — a 403 body / redirect / HTML error page must never
    # be saved as if it were a photo.
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG"):
        return data
    return None


def _photo_path(contact_id) -> str:
    """Absolute path to a contact's saved photo, alongside its .html.gz / .json."""
    return os.path.join(RAW_DIR, f"{contact_id}.jpg")


def download_photo_to_file(contact_id, photo_url: str) -> bool:
    """Download `photo_url` and, if it is a valid image, write it to
    data/li_raw/<id>.jpg. Returns True on a saved image, False otherwise. Never
    throws — best-effort, safe to call inline from the scrape path."""
    if not photo_url:
        return False
    data = download_image(photo_url)
    if not data:
        return False
    try:
        os.makedirs(RAW_DIR, exist_ok=True)
        with open(_photo_path(contact_id), "wb") as fh:
            fh.write(data)
        return True
    except Exception:
        return False


def scrape_profile(driver, url: str) -> "dict | None":
    """Navigate to a profile and capture AS MUCH as one page load gives us.

    Returns a rich dict (name/headline/location, every profile section's text,
    skills, mutual-connection & connection counts, plus the full page HTML for
    archival re-parsing), or None if the page is a security checkpoint (caller
    must stop). Every field is best-effort — LinkedIn's markup drifts, but the
    saved HTML means nothing is permanently lost if a selector misses."""
    try:
        driver.get(url)
    except Exception:
        # A page-load timeout still leaves partial DOM — carry on and read it.
        pass
    time.sleep(random.uniform(SETTLE_MIN_SECONDS, SETTLE_MAX_SECONDS))
    _human_scroll(driver)  # load every lazy section AND mimic a human reading

    body_text = _body_text(driver)
    if is_checkpoint(driver.current_url, body_text):
        return None

    sections = {}
    for key, anchor in PROFILE_SECTION_ANCHORS.items():
        txt = _section_text(driver, anchor)
        if txt:
            sections[key] = txt

    try:
        html = driver.page_source or ""
    except Exception:
        html = ""

    # The full rendered text is the primary signal for the LLM. Prefer stripping
    # the page HTML (captures everything the page rendered, regardless of what's
    # in the viewport) and fall back to the live body text.
    profile_text = _html_to_text(html) or body_text

    return {
        "url": url,
        "final_url": (driver.current_url or ""),
        "name_on_page": _first_text(driver, NAME_SELECTORS),
        "headline": _first_text(driver, HEADLINE_SELECTORS),
        "location": _first_text(driver, LOCATION_SELECTORS),
        "sections": sections,                              # about/experience/education/…
        "skills": _collect_skills(driver, SKILL_SELECTORS, MAX_SKILLS),
        "mutuals": _scan_line(body_text, "mutual connection"),
        "connections": _scan_line(body_text, " connection"),
        "followers": _scan_line(body_text, "follower"),
        "profile_text": profile_text,                      # → LLM + saved in JSON
        "photo_url": extract_photo_url(html),              # → JSON sidecar; .jpg downloaded in run_scrape
        "html": html,                                      # archived to .html.gz
        "body_text": body_text,                            # not saved (redundant w/ profile_text)
    }


def save_raw(contact_id: int, name: str, scraped: dict) -> None:
    """Persist the full scrape so a profile never has to be revisited: gzipped
    HTML + a JSON sidecar of everything we extracted (minus the bulky html/body).
    Lives under data/li_raw/ which is gitignored and stays on the machine."""
    try:
        os.makedirs(RAW_DIR, exist_ok=True)
        html = scraped.get("html") or ""
        with gzip.open(os.path.join(RAW_DIR, f"{contact_id}.html.gz"), "wt",
                       encoding="utf-8") as fh:
            fh.write(html)
        meta = {k: v for k, v in scraped.items() if k not in ("html", "body_text")}
        meta["contact_id"] = contact_id
        meta["name"] = name
        meta["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(os.path.join(RAW_DIR, f"{contact_id}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"    (warn) could not save raw for {contact_id}: {exc}", file=sys.stderr)


PROFILE_TEXT_CAP = 6000            # chars of page text fed to the LLM


def has_scraped_content(scraped: dict) -> bool:
    """True if the scrape got anything worth normalizing. The page text is the
    real signal; the structured bits are a bonus."""
    if not scraped:
        return False
    if len((scraped.get("profile_text") or "").strip()) >= 300:
        return True
    return bool(scraped.get("location") or scraped.get("headline")
                or scraped.get("skills") or scraped.get("sections"))


def build_scrape_context(row, scraped: dict) -> str:
    """Assemble the scraped facts into an LLM context string for the shared
    LLM_SYSTEM prompt. The reliable signal is the full rendered profile text —
    LinkedIn's DOM changes constantly, so we feed the text and let the LLM extract
    rather than depending on selectors. Structured extras (mutuals, any sections
    we did grab) are added on top; the raw HTML/JSON keep the complete record."""
    parts = [f"Name: {row['name']}"]
    keys = row.keys()
    if "position" in keys and row["position"]:
        parts.append(f"CRM title: {row['position']}")
    if "company" in keys and row["company"]:
        parts.append(f"CRM company: {row['company']}")
    if scraped.get("mutuals"):
        parts.append(f"Mutual connections: {scraped['mutuals'][:120]}")

    text = (scraped.get("profile_text") or scraped.get("body_text") or "").strip()
    if text:
        parts.append("Full profile page text:\n" + text[:PROFILE_TEXT_CAP])
    else:
        # fall back to whatever structured bits we managed to grab
        if scraped.get("headline"):
            parts.append(f"Headline: {scraped['headline'][:200]}")
        if scraped.get("location"):
            parts.append(f"LinkedIn location: {scraped['location'][:120]}")
        sec = scraped.get("sections") or {}
        for label, key in (("About", "about"), ("Experience", "experience"),
                           ("Education", "education"), ("Interests", "interests")):
            if sec.get(key):
                parts.append(f"{label}: {sec[key][:800]}")
        if scraped.get("skills"):
            parts.append("Skills: " + ", ".join(scraped["skills"][:MAX_SKILLS]))
    return "\n".join(parts)


# ── LLM normalization ─────────────────────────────────────────────────────────
def llm_normalize(client, context: str) -> dict:
    """Send scraped context to Claude Haiku using the SAME call pattern and prompt
    as enrich_facts.enrich_pass_c_one, returning a dict of contact-column changes
    (or {} on empty context / failure). Kept in sync with Pass C's field mapping."""
    if not context.strip():
        return {}
    try:
        msg = client.messages.create(
            model=LLM_MODEL,
            max_tokens=300,
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        data = json.loads(raw)
    except Exception as exc:
        print(f"  LLM error: {exc}", file=sys.stderr)
        return {}

    changes: dict = {}
    if data.get("city"):
        changes["home_city"] = str(data["city"])[:80]
    if data.get("country"):
        changes["home_country"] = str(data["country"])[:80]
    if data.get("metro"):
        changes["home_metro"] = str(data["metro"])[:80]
    if data.get("topics"):
        try:
            changes["topics"] = json.dumps([str(t)[:40] for t in data["topics"][:5]])
        except Exception:
            pass
    if data.get("role_type"):
        changes["role_type"] = str(data["role_type"])[:40]
    if data.get("blurb"):
        changes["blurb"] = str(data["blurb"])[:200]
    return changes


# ── Scrape run ────────────────────────────────────────────────────────────────
def run_scrape(conn, client, limit: int) -> None:
    """Visit up to `limit` candidate profiles, one at a time, with randomized
    delays and periodic long breaks. Commits after each contact so an interrupted
    run keeps its progress. Stops (exit 1) the instant a checkpoint is detected."""
    candidates = pick_candidates(conn, limit)
    if not candidates:
        print("No candidates to scrape (all fresh, manual, or missing a URL).")
        return
    total = len(candidates)
    print(f"Scraping {total} profile(s). Profile dir: {FF_PROFILE_DIR}")

    next_break_at = random.randint(BREAK_EVERY_MIN, BREAK_EVERY_MAX)
    next_recycle_at = random.randint(RECYCLE_MIN, RECYCLE_MAX)
    driver = make_driver_resilient(headless=True)
    try:
        for i, row in enumerate(candidates, start=1):
            cid, name, url = row["id"], row["name"], row["linkedin_url"]

            # Relaunch the browser every few profiles to flush LinkedIn's memory
            # leak. The persistent profile keeps us logged in across the relaunch.
            if i > next_recycle_at:
                print(f"  … recycling browser at profile {i} (flush memory)")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(random.uniform(2.0, 5.0))
                driver = make_driver_resilient(headless=True)
                next_recycle_at = i + random.randint(RECYCLE_MIN, RECYCLE_MAX)

            try:
                scraped = scrape_profile(driver, url)
            except Exception as exc:
                print(f"  [{i}/{total}] {name}: navigation error ({exc}) — skipped",
                      file=sys.stderr)
                scraped = {}

            if scraped is None:  # checkpoint / CAPTCHA
                print("\n" + "!" * 70, file=sys.stderr)
                print("CHECKPOINT / SECURITY PAGE DETECTED — stopping immediately.",
                      file=sys.stderr)
                print("Do NOT re-run for a while. Log in by hand in a normal browser",
                      file=sys.stderr)
                print("first, then re-run --login later if needed.", file=sys.stderr)
                print("!" * 70, file=sys.stderr)
                conn.commit()
                driver.quit()
                sys.exit(EXIT_CHECKPOINT)

            # Always archive the raw scrape FIRST — even a page we can't parse (or
            # that normalizes to nothing) is saved so it never has to be revisited.
            if scraped:
                save_raw(cid, name, scraped)
                # Best-effort profile photo: the URL is already extracted into the
                # sidecar; download the JPEG alongside. A missing/failed photo
                # never affects the text scrape or the contact.
                download_photo_to_file(cid, scraped.get("photo_url", ""))

            changes = {}
            if has_scraped_content(scraped):
                ctx = build_scrape_context(row, scraped)
                changes = llm_normalize(client, ctx)

            if changes:
                _apply_changes(conn, cid, changes, FACTS_SOURCE)
                conn.commit()  # commit per-contact for resumability
                topics = changes.get("topics", "[]")
                print(f"  [{i}/{total}] {name}: city={changes.get('home_city', '?')}, "
                      f"topics={topics}  (raw saved)")
            else:
                tail = " (raw saved)" if scraped else ""
                print(f"  [{i}/{total}] {name}: no normalized data{tail}")

            if i >= total:
                break

            # periodic long break, else a normal randomized inter-profile delay
            if i >= next_break_at:
                brk = random.uniform(LONG_BREAK_MIN_SECONDS, LONG_BREAK_MAX_SECONDS)
                print(f"  … long break {brk / 60:.1f} min (anti-ban)")
                time.sleep(brk)
                next_break_at = i + random.randint(BREAK_EVERY_MIN, BREAK_EVERY_MAX)
            else:
                time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    print("Done.")


# ── Dry run ───────────────────────────────────────────────────────────────────
def run_dry(conn, limit: int) -> None:
    candidates = pick_candidates(conn, limit)
    print(f"[dry-run] {len(candidates)} candidate(s) would be scraped "
          f"(limit={limit}), most-relevant-first, no browser launched:")
    for i, row in enumerate(candidates, start=1):
        score = db.relationship_strength(row)
        src = row["facts_source"] or "(none)"
        upd = row["facts_updated_at"] or "never"
        print(f"  [{i}/{len(candidates)}] score={score:>3}  {row['name']}  "
              f"[source={src}, updated={upd}]")


# ── Backfill photos ─────────────────────────────────────────────────────────────
def run_backfill_photos() -> None:
    """Download profile photos for already-scraped contacts WITHOUT re-visiting
    LinkedIn. Walks the archived data/li_raw/<id>.html.gz files and, for every
    contact that doesn't yet have a <id>.jpg, extracts the display-photo URL from
    the saved HTML and fetches it via the licdn CDN (signed URLs stay valid
    ~2 weeks, so backfilling from saved HTML works). No browser, no profile
    visits — safe to run any time. Prints one line per contact and a final tally."""
    if not os.path.isdir(RAW_DIR):
        print(f"No raw archive at {RAW_DIR} — nothing to backfill.")
        return

    names = sorted(n for n in os.listdir(RAW_DIR) if n.endswith(".html.gz"))
    if not names:
        print(f"No saved HTML in {RAW_DIR} — nothing to backfill.")
        return

    print(f"Backfilling photos from {len(names)} saved profile(s) in {RAW_DIR}")
    saved = no_url = failed = skipped = 0
    for name in names:
        cid = name[:-len(".html.gz")]
        if os.path.exists(_photo_path(cid)):
            skipped += 1
            continue
        try:
            with gzip.open(os.path.join(RAW_DIR, name), "rt", encoding="utf-8") as fh:
                html = fh.read()
        except Exception as exc:
            print(f"  {cid}: read-failed ({exc})")
            failed += 1
            continue

        url = extract_photo_url(html)
        if not url:
            print(f"  {cid}: no-url")
            no_url += 1
        elif download_photo_to_file(cid, url):
            print(f"  {cid}: saved")
            saved += 1
        else:
            print(f"  {cid}: download-failed")
            failed += 1

    print(f"Done. saved={saved}, no-url={no_url}, download-failed={failed}, "
          f"already-had-photo={skipped}, total={len(names)}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", action="store_true",
                        help="one-time: open a visible Firefox to log in and save the session")
    parser.add_argument("--limit", type=int, default=25,
                        help="hard cap on profiles visited in one run (default 25)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the candidates without launching a browser")
    parser.add_argument("--backfill-photos", action="store_true",
                        help="download profile photos from already-saved HTML "
                             "(no browser, no LinkedIn visits)")
    args = parser.parse_args()

    if args.login:
        run_login()
        return

    # Photo backfill works purely off the saved data/li_raw/*.html.gz archive — it
    # needs neither a login session, an API key, nor the DB, so handle it before
    # any of those checks and never touch the browser or LinkedIn.
    if args.backfill_photos:
        run_backfill_photos()
        return

    db.init_db()
    conn = db.get_db()

    if args.dry_run:
        run_dry(conn, args.limit)
        conn.close()
        return

    # Live scrape requires a saved login profile and an API key.
    if not os.path.isdir(FF_PROFILE_DIR) or not os.listdir(FF_PROFILE_DIR):
        print(f"No saved LinkedIn session at {FF_PROFILE_DIR}.", file=sys.stderr)
        print("Run once with --login first:", file=sys.stderr)
        print("  python crm/scrape_linkedin_profiles.py --login", file=sys.stderr)
        conn.close()
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("No ANTHROPIC_API_KEY set — cannot normalize scraped text.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
        conn.close()
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        run_scrape(conn, client, args.limit)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
