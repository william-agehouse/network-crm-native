#!/usr/bin/env python3
"""Measure how the owner actually writes, from the messages THEY sent, and store it.

Flat sibling of app.py (see crm/api_guards.py for why the imports look like this).

WHAT THIS IS FOR
----------------
`api_briefs.handle_draft_message` writes a reconnect message in a generic
assistant voice. This module produces the missing half: a short, plain-language
instruction block, derived from the owner's own outbound messages, that can be
appended to that system prompt. One call:

    prof = style_profile.load(conn)
    directive = style_profile.style_directive(prof, bucket_key)
    _run_claude(BASE_SYSTEM + "\\n\\n" + directive, profile, 200, "message draft")

`api_briefs.py` appends the directive; this module only produces it.

FULLY OFFLINE, AND THAT IS THE POINT
------------------------------------
Zero API calls, zero network. The obvious version of this feature — "send a
sample of greetings and sign-offs to an LLM and ask it to describe the voice" —
is rejected because harvested message text carries other people's names and
words off the machine, and invariant 7 says archive.db never leaves this Mac. So every number here is computed with the standard library, and the
output is designed so that sending it later would be safe:

    `style_directive()` contains NO message text and NO third-party names.

That is not a promise about intent, it is a property of the construction. The
only strings it can emit are (a) numbers, (b) sentences written in this file,
(c) tokens from the closed allowlists defined below — `GREETINGS`, `SIGNOFFS`,
`OPENER_WORDS`, `STYLE_LEXICON` — and (d) emoji characters. A closed list
defined in source cannot contain a name that came out of the archive, and an
emoji cannot encode one. Free-text harvesting of openers, closers or
distinctive words is refused everywhere in this module for exactly that reason,
and the refusals are reported as exclusions rather than being silently absent.

THE CORPUS IS NOT UNIFORM GROUND TRUTH, AND THE PROFILE SAYS SO
---------------------------------------------------------------
`archive.db.messages.direction` is 1 for "sent by me". How trustworthy that bit
is depends entirely on which importer wrote the row. Importer by importer (line
numbers are approximate):

  imessage      GROUND TRUTH.   `direction=int(row["from_me"])` — the Messages
                                database's own `is_from_me`. imessage_import.py:518
  whatsapp 1:1  GROUND TRUTH,   `COALESCE(m.ZISFROMME, 0)`; when the column is
                BUT DEGRADES.   absent the expression becomes the literal "0",
                                so the whole 1:1 corpus reads as inbound.
                                whatsapp_import.py:248
  whatsapp grp  HARDCODED 0.    Group rows are inserted with `direction=0` and
                                are selected with `ZGROUPMEMBER IS NOT NULL`, so
                                they are inbound by construction — the user's own
                                group messages are not in the archive at all.
                                whatsapp_import.py:429
  telegram      INFERRED.       `str(msg["from_id"]) == self_id(data)` — a string
                                compare, but against the export's OWN self id.
                                telegram_import.py:279
  messenger /   INFERRED.       `db.normalize_name(sender_name) == self_norm` —
  instagram                     a name compare. meta_import.py:349
  email (mbox)  GROUND TRUTH    `int(from_addr == SELF_EMAIL)`, and SELF_EMAIL is
                WHEN SET.       read into a module global at import time
                                (email_import.py:54). Unset at process start ⇒
                                every mbox email row is direction=0.
  email (Gmail  GROUND TRUTH    `self_email in from_addrs`, self_email being the
  API)          MOSTLY.         authenticated account (gmail_import.py:215/268).
                                `scan_all_messages` adds `or from_norm ==
                                self_norm` (gmail_import.py:452) — a name
                                compare, so a correspondent whose display name
                                normalises to the owner's own would be mislabelled.
                                Narrow, but real. The create-new backfill inserts
                                `direction=0` (gmail_import.py:574), correct by
                                construction — those are the sender's messages.
  linkedin      INFERRED, AND   See below. Excluded by default.
                UNSAFE.

Two things follow, and both are in the profile rather than in a comment nobody
reads. First, the failure modes are NOT symmetric: telegram, meta, whatsapp and
imessage fail toward false-INBOUND, which loses the owner's writing but never puts
someone else's words in their mouth. LinkedIn fails toward false-OUTBOUND, which
does. Only the second kind can corrupt a voice model. Second, "the email channel"
is two importers writing the same `channel` value with different direction logic,
and a row does not record which one wrote it — so the channel's provenance is the
weaker of the two.

THE LINKEDIN DECISION — EXCLUDED BY DEFAULT
-------------------------------------------
`linkedin_import.archive_messages` (linkedin_import.py:199-206) decides direction
like this:

    if sender_url in url_to_cid:   direction = 0   # a known contact sent it
    else:                          direction = 1   # ...therefore I sent it

`url_to_cid` maps LinkedIn profile URLs to contacts already in crm.db. So the
`else` branch means "the sender is not someone I have", and it is reached by:

  • every cold InMail, recruiter pitch and sales sequence from a stranger — which
    is the majority of LinkedIn inbound for anyone with a public profile;
  • any real contact whose `linkedin_url` is empty, differently spelled, or
    locale-prefixed, so the URL never matched;
  • any thread where a third party posts and the recipient list happens to hold
    a contact — the row is then filed under that contact as well.

Every one of those is stored as the owner's own writing. Averaging recruiter
boilerplate into a personal voice model is the exact failure this feature exists
to avoid, so:

    LinkedIn is EXCLUDED from the corpus by default, behind
    NC_STYLE_INCLUDE_LINKEDIN (default "0").

Not deleted, not silently dropped: the census counts LinkedIn rows, the exclusion
list names the channel with its row count and the reason, and `report()` says it
in a sentence. Turning the setting on is allowed and is recorded in the profile,
so a directive built from a contaminated corpus can be identified after the fact.
The alternative — a heuristic that tries to spot boilerplate — would be this
module guessing about the same rows twice.

WHAT WAS DROPPED AS UNCOMPUTABLE OR UNSAFE
------------------------------------------
  • **Free-text openers and closers.** Harvesting the literal first line would
    collect "Hej Anna" and put a third party's name one string-join away from a
    prompt. Only allowlisted greeting/sign-off tokens are counted; everything
    else increments an `other` counter. The counter is reported.
  • **Distinctive content words.** A log-odds ratio over full vocabulary is the
    textbook measure and it cannot be emitted: its top terms are names, employers
    and project code-words. The distinctiveness measure here runs over a CLOSED
    style lexicon (hedges, intensifiers, discourse markers) instead, so it still
    answers "which of these does the owner over-use", and it is computed from BOTH the
    outbound and the inbound distributions in two bounded passes, because a
    log-odds ratio needs the other side's denominator and one streaming scan
    cannot produce it.
  • **Means.** Nothing here averages a length. One 4,000-word message must not
    move the number a drafter is told to hit, so every length is an order
    statistic (median, p10/p25/p75/p90) taken from an exact frequency table.
  • **Tone, warmth, formality as scores.** Not computable offline without a model,
    and a made-up number would be worse than nothing. Register is proxied by
    relationship strength, which is a real stored quantity.
  • **Sentence splitting is approximate.** Abbreviations and decimals are guarded
    against with a small closed list; other cases over-split. Stated, not hidden.

BOUNDED
-------
A large archive can hold hundreds of thousands of messages. Nothing here loads
the table: both passes iterate a cursor with a
`LIMIT`, and the quantiles come from `_Dist`, a frequency table whose memory is
O(distinct values) — a few thousand ints — not O(messages).

Timings (synthetic archive of realistic message lengths on a laptop, best of two
runs, and linear at both sizes):

    100,000 rows  (50k outbound + 50k inbound)   4.0s
    300,000 rows (150k outbound + 150k inbound)  12.3s
    ≈ 12,000 outbound messages/second, ≈ 24,000 rows/second across both passes

So the default 50,000-message cap costs about four seconds, and an uncapped
500,000-message archive would take roughly twenty. That is fine for an explicit
action — a CLI run or a button — and is NOT fine inside a request handler, which
is why nothing here is wired into one: `measure()` is called deliberately and the
result is read back from `state` by anything that needs it. Re-measure rather
than trust these numbers if the per-message work changes.

SETTINGS (all read at call time — the `api_import.import_timeout_sec` idiom)
----------------------------------------------------------------------------
These are the seven keys and their defaults (settings.py says which of them have
a box on the Settings page):

    NC_STYLE_MAX_MESSAGES      50000  outbound rows read per pass (and the same
                                      cap on the inbound pass)
    NC_STYLE_MIN_CHARS            12  a message shorter than this is counted but
                                      kept out of the style statistics
    NC_STYLE_MIN_BUCKET           30  messages a bucket needs before its numbers
                                      are used rather than reported as too thin
    NC_STYLE_LANG_MARGIN        0.30  confidence below which detect_language
                                      returns "" instead of guessing
    NC_STYLE_TOP_N                 8  how many greetings / sign-offs / emoji /
                                      lexicon terms are kept per bucket
    NC_STYLE_INCLUDE_LINKEDIN      0  include the LinkedIn corpus (see above)
    NC_STYLE_INCLUDE_GROUPS        0  include group-chat messages

Every one is a cap or a filter, so none of them may be a silent constant: each
default lives in a named constant next to a reader function that re-reads
`os.environ` on every call, so an edit made in the app applies to the next run
instead of the next restart.

STORAGE
-------
One key in crm.db's `state` table, JSON: `STATE_KEY = "writing_style_profile"`.
Nothing copies `state` off this Mac, which is why it is the right home for
something derived from archive.db, and why this feature needs no schema change.
The stored blob carries `version`; `load()` reports a version mismatch as stale rather than
raising or silently returning modern-shaped defaults.
"""

import json
import logging
import math
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config  # noqa: F401 — loads secrets/.env before anything reads os.environ
import archive as arc
import db
import figures  # NC-115: the one spelling of "the people you hid"

log = logging.getLogger(__name__)

# ── Identity of the stored artefact ─────────────────────────────────────────
PROFILE_VERSION = 1
STATE_KEY = "writing_style_profile"

THROUGHPUT_NOTE = (
    "About 12,000 outbound messages/second (~24,000 rows/second across both "
    "passes) on a synthetic archive of realistic message lengths: 100,000 rows in "
    "4.0s, 300,000 in 12.3s, linear in between. The default 50,000-message cap "
    "therefore costs about four seconds, and an uncapped 500,000-message archive "
    "about twenty — deliberate-action work, not request-handler work."
)

# ── Setting defaults, each next to its call-time reader ─────────────────────
MAX_MESSAGES_DEFAULT = 50000
MIN_CHARS_DEFAULT = 12
MIN_BUCKET_DEFAULT = 30
LANG_MARGIN_DEFAULT = 0.30
TOP_N_DEFAULT = 8
INCLUDE_LINKEDIN_DEFAULT = False
INCLUDE_GROUPS_DEFAULT = False


def _int_env(key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(key, default) or default)))
    except (TypeError, ValueError):
        return default


def _float_env(key: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.environ.get(key, default) or default)))
    except (TypeError, ValueError):
        return default


def _bool_env(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "no", "off", "false")


def max_messages() -> int:
    """Rows read per pass (NC_STYLE_MAX_MESSAGES).

    Default 50,000: enough that every bucket clears its minimum many times over
    on a real archive, small enough that the pass is imperceptible. It is a cap
    on rows READ, not on rows used, and `report()` states both — a cap that
    silently became a sample would be a hidden limit.
    """
    return _int_env("NC_STYLE_MAX_MESSAGES", MAX_MESSAGES_DEFAULT, 1, 5_000_000)


def min_chars_setting() -> int:
    """Shortest message that may enter the style statistics (NC_STYLE_MIN_CHARS).

    Default 12. "ok", "👍" and "haha" are real messages and are counted in the
    census, but they carry no sentence, no paragraph and no sign-off, and letting
    them into the length medians would tell a drafter to write four characters.
    0 admits everything.
    """
    return _int_env("NC_STYLE_MIN_CHARS", MIN_CHARS_DEFAULT, 0, 10000)


def min_bucket() -> int:
    """Messages a bucket needs before its numbers are used (NC_STYLE_MIN_BUCKET).

    Default 30. Below it the bucket is REPORTED as skipped for lack of data, with
    its count and this threshold, and `style_directive` falls back to the whole
    corpus and says so. A bucket that is quietly absent is indistinguishable from
    a bucket that does not exist.
    """
    return _int_env("NC_STYLE_MIN_BUCKET", MIN_BUCKET_DEFAULT, 1, 100000)


def lang_margin() -> float:
    """Confidence floor for detect_language (NC_STYLE_LANG_MARGIN).

    Default 0.30. Below it the language comes back "" — an honest unknown beats a
    coin flip, because a mislabelled message pollutes two buckets at once (it is
    missing from the right one and wrong in the other).
    """
    return _float_env("NC_STYLE_LANG_MARGIN", LANG_MARGIN_DEFAULT, 0.0, 1.0)


def top_n() -> int:
    """How many greetings / sign-offs / emoji / lexicon terms are kept per bucket
    (NC_STYLE_TOP_N). Default 8 — a directive longer than that stops being read.
    Items outside the top N are not lost: their combined count stays in the
    `other` tally for that measure."""
    return _int_env("NC_STYLE_TOP_N", TOP_N_DEFAULT, 1, 100)


def include_linkedin() -> bool:
    """Include the LinkedIn corpus (NC_STYLE_INCLUDE_LINKEDIN, default off).

    Off because linkedin_import.py infers direction as "the sender is not a known
    contact, therefore I sent it", which labels cold inbound as the owner's own
    writing. See the module docstring. On is allowed and is recorded in the
    profile, so a directive built from it can be identified later.
    """
    return _bool_env("NC_STYLE_INCLUDE_LINKEDIN", INCLUDE_LINKEDIN_DEFAULT)


def include_groups() -> bool:
    """Include group-chat messages (NC_STYLE_INCLUDE_GROUPS, default off).

    Off for a register reason, not a trust one: writing to a group is one-to-many
    and the register axis of a bucket is about one person. WhatsApp group rows are
    hardcoded inbound anyway, so on WhatsApp this setting can only ever admit
    other people's messages — which is why it defaults off rather than on.
    """
    return _bool_env("NC_STYLE_INCLUDE_GROUPS", INCLUDE_GROUPS_DEFAULT)


# ── Per-channel direction provenance ────────────────────────────────────────
# `false_outbound_risk` is the only field that matters for a voice model: it is
# the risk of someone else's words being labelled as the owner's.
CHANNEL_PROVENANCE: Dict[str, Dict[str, Any]] = {
    "imessage": {
        "direction": "ground_truth",
        "how": "int(row['from_me']) — the Messages database's own is_from_me flag",
        "where": "imessage_import.py:518",
        "false_outbound_risk": "none",
        "notes": "Group rows carry a real from_me too, unlike WhatsApp.",
    },
    "whatsapp": {
        "direction": "ground_truth_degrading",
        "how": "COALESCE(m.ZISFROMME, 0); the expression becomes the literal '0' "
               "when the column is missing, and group rows are inserted with "
               "direction=0 unconditionally",
        "where": "whatsapp_import.py:248 (1:1), whatsapp_import.py:429 (group)",
        "false_outbound_risk": "none",
        "notes": "Fails toward inbound: the corpus loses your messages "
                 "rather than gaining someone else's. A WhatsApp row count with "
                 "zero outbound is the visible symptom.",
    },
    "telegram": {
        "direction": "inferred",
        "how": "str(msg['from_id']) == self_id(data) — a string compare, but "
               "against the export's own self id",
        "where": "telegram_import.py:279",
        "false_outbound_risk": "none",
        "notes": "A failed compare marks messages inbound, not outbound.",
    },
    "messenger": {
        "direction": "inferred",
        "how": "db.normalize_name(sender_name) == self_norm — a name compare",
        "where": "meta_import.py:349",
        "false_outbound_risk": "low",
        "notes": "Needs a correspondent whose normalised name equals SELF_NAME "
                 "to mislabel. Otherwise fails toward inbound.",
    },
    "instagram": {
        "direction": "inferred",
        "how": "db.normalize_name(sender_name) == self_norm — a name compare",
        "where": "meta_import.py:349",
        "false_outbound_risk": "low",
        "notes": "Same importer and same compare as messenger.",
    },
    "email": {
        "direction": "ground_truth_when_configured",
        "how": "mbox: int(from_addr == SELF_EMAIL), with SELF_EMAIL captured into "
               "a module global at import time. Gmail API: self_email in "
               "from_addrs, plus a from_norm == self_norm name compare in "
               "scan_all_messages",
        "where": "email_import.py:54/426, gmail_import.py:215/268/452/574",
        "false_outbound_risk": "low",
        "notes": "Two importers write this one channel value and a row does not "
                 "say which, so the channel inherits the weaker of the two. "
                 "SELF_EMAIL unset at process start makes the whole mbox corpus "
                 "inbound. Gmail bodies have the subject prepended; mbox bodies "
                 "do not, so opener and paragraph statistics for this channel "
                 "mix two shapes.",
    },
    "linkedin": {
        "direction": "inferred_unsafe",
        "how": "sender_url in url_to_cid ? inbound : OUTBOUND — 'the sender is "
               "not a known contact, therefore I sent it'",
        "where": "linkedin_import.py:199-206",
        "false_outbound_risk": "structural",
        "notes": "Every cold InMail from a stranger is stored as your own "
                 "writing. Excluded by default (NC_STYLE_INCLUDE_LINKEDIN).",
    },
}

# Channels whose direction bit cannot be trusted to mean "the owner wrote this".
UNSAFE_CHANNELS = frozenset(
    ch for ch, p in CHANNEL_PROVENANCE.items()
    if p["false_outbound_risk"] == "structural"
)

# A letter is a letter and everything else is a chat. LinkedIn messages are
# chat-shaped even when they read like letters, and they are excluded by default
# anyway; recording the mapping here keeps `medium_for` from growing opinions.
LETTER_CHANNELS = frozenset({"email"})


# ── Language detection ─────────────────────────────────────────────────────
# Closed-class function words. Deliberately NOT content words: function words
# survive short messages, resist topic drift, and cannot be a name.
#
# Tokens that exist in both languages are in neither list — see AMBIGUOUS. That
# is what stops "man", "in", "is", "under", "vi" and "hall" from voting.
SV_MARKERS = frozenset("""
och att det som en är på för med av den till inte har jag den de vi ni han hon
men om så kan ska var vad när där här från vid över efter före mellan utan
mycket lite bara redan igen kanske faktiskt liksom alltså typ sen nån nåt något
någon dom dem deras vår vårt våra din ditt dina min mitt mina sitt sina hans
hennes vilken vilket vilka hur varför därför eftersom medan innan tills genom
mot mycket väl ju nog visst gärna ännu aldrig alltid ibland ofta sällan
hej hejsan tjena tack tackar förlåt kram kramar hälsningar mvh
ett tvåa jo japp nej okej säkert absolut självklart precis exakt
skulle skulle kunde ville måste borde brukar tänkte trodde
""".split())

EN_MARKERS = frozenset("""
the and that this these those there their they them then than with from what
when where which while would could should have has had been being does did
about after before between without because although though however therefore
just really actually maybe perhaps probably definitely absolutely quite rather
pretty very much some any every each both either neither
you your yours our ours mine his hers its
thanks thank cheers regards sincerely sorry hello hey please
going gonna wanna let lets make made take took give gave get got know knew
think thought said say says tell told see saw look looking need needed want
wanted
""".split())

# Words that spell the same in both languages, or that a naive list would put in
# both. Left out of both marker sets on purpose. Written down rather than merely
# omitted, because the next person to "improve" the lists needs to know why
# "man" is missing.
AMBIGUOUS = frozenset("""
man in is under over hall fall all so ok no me my we vi du er sin son son
art park plan program problem person telefon information
""".split())

# Characters that only Swedish (of these two) uses. Weak evidence on their own —
# an English message can quote a Swedish name — so they are worth a fraction of
# a marker hit, never a decision.
SV_CHARS = frozenset("åäöÅÄÖ")
SV_CHAR_WEIGHT = 0.5
SV_CHAR_MAX_CREDIT = 3.0   # a wall of å's is not three hundred markers

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
MIN_LANG_TOKENS = 2        # below this there is nothing to count
MIN_LANG_EVIDENCE = 1.0    # total marker weight needed before a verdict


def _tokens(text: str) -> List[str]:
    """Lowercased alphabetic tokens. Digits, punctuation and emoji are not words."""
    return [t.lower() for t in _WORD_RE.findall(text or "")]


def _word_count(text: str) -> int:
    """How many alphabetic tokens, without building any of them. Used where only
    the count is wanted (per-sentence length, called once per sentence per
    message), because `len(_tokens(s))` lowercases a whole message for nothing."""
    return sum(1 for _ in _WORD_RE.finditer(text or ""))


def detect_language(text: str) -> Tuple[str, float]:
    """(lang, confidence) where lang is "sv", "en" or "" for an honest unknown.

    Standard library only, and deliberately not a general language detector: it
    answers one question, "Swedish or English", by counting closed-class function
    words plus a small credit for å/ä/ö. Confidence is the normalised margin
    between the two sides,

        confidence = |sv - en| / (sv + en)

    so 1.0 means every marker pointed one way and 0.0 means a tie. The verdict is
    "" whenever any of these holds — all three are stated rather than tuned away:

      • fewer than MIN_LANG_TOKENS (2) alphabetic tokens. "ok", "👍" and "haha"
        have no language, and pretending otherwise fills a bucket with noise.
      • total marker weight below MIN_LANG_EVIDENCE (1.0). A message of content
        words only ("Invoice 4471 paid") genuinely does not say.
      • confidence below `lang_margin()` (NC_STYLE_LANG_MARGIN, default 0.30) —
        read at call time, so the threshold is tunable without a restart.

    KNOWN LIMITS, none of them hidden:
      • Only these two languages exist to it. A Spanish message scores near zero
        on both sides and comes back "", which is correct behaviour for the wrong
        reason — it is unknown, not "not Swedish".
      • Code, URLs and quoted material vote. `_clean_body` strips quoted replies
        before this ever runs, which removes the biggest case.
      • Mixed-language messages (Swedish sentence, English technical clause) land
        wherever the function words are denser, and bilingual writers produce a
        lot of these. The confidence value is the honest signal there; the profile
        keeps the distribution of confidences so the effect is visible.
      • Marker lists are frequency-ordered by hand, not corpus-derived. They are
        deliberately un-weighted: "och" counts the same as "eftersom".
    """
    toks = _tokens(text)
    if len(toks) < MIN_LANG_TOKENS:
        return "", 0.0

    sv = float(sum(1 for t in toks if t in SV_MARKERS))
    en = float(sum(1 for t in toks if t in EN_MARKERS))

    diacritics = sum(1 for ch in (text or "") if ch in SV_CHARS)
    sv += min(SV_CHAR_MAX_CREDIT, diacritics * SV_CHAR_WEIGHT)

    total = sv + en
    if total < MIN_LANG_EVIDENCE:
        return "", 0.0
    conf = abs(sv - en) / total
    if conf < lang_margin():
        return "", round(conf, 3)
    return ("sv" if sv > en else "en"), round(conf, 3)


# ── Buckets: language × register × medium ──────────────────────────────────
REGISTER_SPLIT = 40        # db.strength_label's "active" boundary
LANG_VALUES = ("sv", "en", "unknown")
REGISTER_VALUES = ("close", "new", "unknown")
MEDIUM_VALUES = ("letter", "chat")


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a key from a sqlite3.Row, a dict, or an object with attributes.

    Exists so `bucket_for` accepts a real contacts Row in the app and a plain
    dict in a test without two code paths that can disagree.
    """
    if row is None:
        return default
    try:
        keys = row.keys()          # sqlite3.Row and dict both have this
    except AttributeError:
        return getattr(row, key, default)
    if key in keys:
        value = row[key]
        return default if value is None else value
    return default


def register_for(contact_row: Any) -> str:
    """"close" (strength >= 40), "new" (below it), or "unknown".

    40 is `db.strength_label`'s boundary between "loose" and "active" — reused
    rather than re-invented, so the register a drafter is told about is the same
    tier the People table shows. A row that already carries a computed
    `strength` is trusted (that is how a caller avoids recomputing thousands of scores);
    otherwise `db.relationship_strength` is asked.

    "unknown" is a real answer, not a fallback to "new": a message to a contact
    that is no longer in crm.db has no register, and guessing "new" would tell a
    drafter to write formally to an old friend.
    """
    if contact_row is None:
        return "unknown"
    score = _row_get(contact_row, "strength", None)
    if score is None:
        score = _row_get(contact_row, "relationship_strength", None)
    if score is None:
        try:
            score = db.relationship_strength(contact_row)
        except Exception:  # noqa: BLE001 — a partial row must not break bucketing
            log.debug("relationship_strength failed for a contact row", exc_info=True)
            return "unknown"
    try:
        score = int(score)
    except (TypeError, ValueError):
        return "unknown"
    return "close" if score >= REGISTER_SPLIT else "new"


def medium_for(channel: str) -> str:
    """"letter" for email, "chat" for everything else.

    The axis is real and it is the one that changes the writing most: a letter
    gets a greeting line, paragraphs and a sign-off; a chat gets none of the
    three and is much shorter.
    """
    return "letter" if (channel or "").strip().lower() in LETTER_CHANNELS else "chat"


def bucket_key(lang: str, register: str, medium: str) -> str:
    """The canonical "lang.register.medium" string. One place, so nothing can
    build a key that the bucket table cannot find."""
    return f"{lang or 'unknown'}.{register or 'unknown'}.{medium or 'chat'}"


def bucket_for(contact_row: Any, message_row: Any) -> str:
    """Which bucket one message belongs in: "sv.close.chat", "en.new.letter", …

    Three axes, each chosen because it changes the writing and is computable
    offline:
      language  from the message body via `detect_language`; "unknown" when the
                detector refuses, which is a bucket of its own rather than a
                silent drop.
      register  from the contact's relationship strength, split at 40
                (`db.strength_label`'s boundary): "close" or "new".
      medium    email is a letter, everything else is a chat.

    `message_row` may be a sqlite3.Row from archive.db or a dict with `body` /
    `channel`. The body is cleaned (quoted replies and signature blocks removed)
    before the language vote, so a Swedish reply quoting an English mail is not
    filed as English.
    """
    body = _row_get(message_row, "body", "") or ""
    channel = _row_get(message_row, "channel", "") or ""
    clean, _ = _clean_body(body)
    lang, _conf = detect_language(clean)
    return bucket_key(lang or "unknown", register_for(contact_row), medium_for(channel))


def all_bucket_keys() -> List[str]:
    """Every key the bucket space can produce, in a stable order. Used by
    `report()` so a bucket with no messages at all is still an enumerable zero
    rather than an absence."""
    return [bucket_key(lang, reg, med)
            for lang in LANG_VALUES
            for reg in REGISTER_VALUES
            for med in MEDIUM_VALUES]


# ── Body cleaning: strip what the owner did not write ────────────────────────
_QUOTE_LINE = re.compile(r"^\s*>")
_REPLY_HEADER = re.compile(
    r"^\s*(on\s.+\swrote:|den\s.+\sskrev:|from:\s|från:\s|-{2,}\s*original message"
    r"|_{5,}|sent from my )", re.IGNORECASE)
_SIG_SEP = re.compile(r"^--\s*$")


def _clean_body(body: str) -> Tuple[str, Dict[str, bool]]:
    """(text the owner wrote, {what was removed}).

    Email bodies carry the other person's words. A reply quotes the thread with
    ">" prefixes or under an "On … wrote:" / "Den … skrev:" header, and a
    signature block follows a bare "--" line. Measuring those as the owner's style
    is not a rounding error: a long quoted thread would dominate the length
    medians and drag the language vote toward whatever the correspondent wrote.

    Removal stops at the first reply header — everything after it is the thread,
    not the message. `flags` records what happened per message so the counts end
    up in the profile instead of being invisible.
    """
    flags = {"quoted": False, "reply_header": False, "signature": False}
    if not body:
        return "", flags
    kept: List[str] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _REPLY_HEADER.match(line):
            flags["reply_header"] = True
            break
        if _SIG_SEP.match(line):
            flags["signature"] = True
            break
        if _QUOTE_LINE.match(line):
            flags["quoted"] = True
            continue
        kept.append(line)
    return "\n".join(kept).strip(), flags


# ── Openers and closers: closed allowlists, never free text ────────────────
# Longest match wins, so "good morning" is not counted as "good". Every entry is
# a greeting or a sign-off in Swedish or English and nothing else — this list is
# the reason `style_directive` cannot leak a name. Adding a non-greeting word
# here would weaken that property, so don't.
GREETINGS: Tuple[str, ...] = (
    # Swedish
    "god morgon", "godmorgon", "god kväll", "god dag", "hej hej", "hejhej",
    "hej", "hejsan", "hallå", "tjena", "tjenare", "tja", "morrn", "läget",
    "hörru", "hörrudu", "gomorron",
    # English
    "good morning", "good afternoon", "good evening", "hi there", "hey there",
    "hi", "hey", "hello", "hiya", "yo", "morning", "dear",
)

SIGNOFFS: Tuple[str, ...] = (
    # Swedish
    "med vänliga hälsningar", "vänliga hälsningar", "hälsningar", "mvh",
    "m.v.h", "vi hörs", "hörs", "vi ses", "ses", "ha det bra", "ha det så bra",
    "ha en bra dag", "kramar", "kram", "puss", "allt gott", "bästa", "tack igen",
    # English
    "best regards", "kind regards", "warm regards", "regards", "best",
    "all the best", "take care", "talk soon", "speak soon", "see you soon",
    "see you", "cheers", "sincerely", "warmly", "yours", "ttyl", "later",
    "thanks again", "many thanks",
)

# Message-initial words that are not greetings but do characterise an opening
# move. Same closed-list discipline: a first token outside this set increments
# `other` and is never stored.
OPENER_WORDS: Tuple[str, ...] = (
    "tack", "tackar", "förlåt", "grattis", "oj", "aha", "jaha", "japp", "jo",
    "ja", "nej", "okej", "precis", "absolut", "kanske", "ska", "kan", "har",
    "vill", "skulle", "kul", "vad", "hur", "när", "var", "varför", "du", "men",
    "så", "och", "jag",
    "thanks", "thank", "sorry", "congrats", "congratulations", "oh", "ah",
    "yes", "no", "ok", "okay", "sure", "just", "quick", "hope", "great",
    "would", "could", "can", "do", "did", "are", "is", "what", "how", "when",
    "where", "why", "i", "we", "you", "and", "but", "so",
)

_GREETINGS_SORTED = tuple(sorted(GREETINGS, key=len, reverse=True))
_SIGNOFFS_SORTED = tuple(sorted(SIGNOFFS, key=len, reverse=True))
_OPENER_SET = frozenset(OPENER_WORDS)


def _normalise_edge(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace — enough to compare a
    line's opening against an allowlist. Bounded to 48 characters because a
    greeting is never longer and because nothing longer should be examined."""
    folded = unicodedata.normalize("NFC", (text or "").strip().lower())
    folded = re.sub(r"[^\w\s'’-]+", " ", folded, flags=re.UNICODE)
    return re.sub(r"\s+", " ", folded).strip()[:48]


def _match_allowlist(text: str, allowlist: Iterable[str]) -> str:
    """The longest allowlist entry `text` starts with, or "".

    The comparison is on a word boundary, so "hint" does not match "hi" and
    "session" does not match "ses".
    """
    norm = _normalise_edge(text)
    if not norm:
        return ""
    for entry in allowlist:
        if norm == entry or norm.startswith(entry + " "):
            return entry
    return ""


def greeting_of(body: str) -> str:
    """The allowlisted greeting this message opens with, or "" for none."""
    first = next((ln for ln in (body or "").split("\n") if ln.strip()), "")
    return _match_allowlist(first, _GREETINGS_SORTED)


def signoff_of(body: str) -> str:
    """The allowlisted sign-off the last line opens with, or "" for none.

    Matched against the START of the last non-empty line, so "Mvh Erik" and
    "Kram Anna" both resolve to the allowlisted token alone — the name is
    examined and discarded, never stored or returned.
    """
    lines = [ln for ln in (body or "").split("\n") if ln.strip()]
    if not lines:
        return ""
    hit = _match_allowlist(lines[-1], _SIGNOFFS_SORTED)
    if hit or len(lines) < 2:
        return hit
    # A sign-off followed by a name on its own line ("Cheers" / "Erik").
    return _match_allowlist(lines[-2], _SIGNOFFS_SORTED)


def opener_word_of(body: str) -> str:
    """The message's first token when it is on OPENER_WORDS, else ""."""
    toks = _tokens(_normalise_edge(next(
        (ln for ln in (body or "").split("\n") if ln.strip()), "")))
    if not toks:
        return ""
    return toks[0] if toks[0] in _OPENER_SET else ""


# ── Contractions and spoken forms ──────────────────────────────────────────
# Both sides of a real ratio. A raw contraction count has no denominator: 40
# apostrophes says nothing without knowing how many chances there were.
EN_CONTRACTION_RE = re.compile(r"\b[\w]+[’'](s|t|re|ve|ll|d|m)\b", re.UNICODE)
EN_EXPANDED = (
    "do not", "does not", "did not", "cannot", "can not", "will not",
    "would not", "should not", "could not", "is not", "are not", "was not",
    "were not", "have not", "has not", "had not", "it is", "that is",
    "there is", "i am", "you are", "we are", "they are", "i will", "we will",
    "you will", "i have", "we have", "you have", "i would", "let us",
)
# Swedish has no apostrophe contractions; the equivalent axis is spoken form vs
# written form, which is the single biggest formality tell in Swedish writing.
SV_SPOKEN = ("nån", "nåt", "nånting", "nånstans", "dom", "sen", "iaf", "asså",
             "ba", "va", "sån", "sånt", "såna", "mej", "dej", "sej", "nåra")
SV_WRITTEN = ("någon", "något", "någonting", "någonstans", "de", "dem",
              "sedan", "i alla fall", "alltså", "bara", "vad", "sådan",
              "sådant", "sådana", "mig", "dig", "sig", "några")



def _phrase_re(phrases: Iterable[str]) -> "re.Pattern":
    """One alternation over `phrases`, longest first so "i am" cannot shadow
    "i am not". Built once at import: the naive version ran one `re.findall` per
    phrase, which is ~30 full scans of every message body and was measurably the
    hottest line in the whole pass."""
    ordered = sorted(phrases, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in ordered) + r")\b")


_EN_EXPANDED_RE = _phrase_re(EN_EXPANDED)
_SV_SPOKEN_RE = _phrase_re(SV_SPOKEN)
_SV_WRITTEN_RE = _phrase_re(SV_WRITTEN)


def _count_phrases(low: str, pattern: "re.Pattern") -> int:
    """Occurrences of any phrase in one scan."""
    return sum(1 for _ in pattern.finditer(low))


# ── The closed style lexicon for vocabulary distinctiveness ────────────────
# Hedges, intensifiers, discourse markers and stance adverbs. Closed and defined
# here, which is what makes emitting the winners safe: no name, employer or
# project word can enter this vocabulary, because the vocabulary is this tuple.
STYLE_LEXICON: Dict[str, Tuple[str, ...]] = {
    "sv": ("kanske", "faktiskt", "liksom", "alltså", "typ", "nog", "ju",
           "verkligen", "absolut", "precis", "väl", "gärna", "ganska",
           "ändå", "iallafall", "dock", "därför", "eftersom", "fast", "bara",
           "jättebra", "grymt", "skitbra", "helt", "riktigt", "väldigt",
           "lite", "mycket", "snabbt", "gulligt", "kul", "tack", "förlåt",
           "spännande", "grymt", "toppen", "perfekt", "självklart", "säkert"),
    "en": ("maybe", "perhaps", "probably", "actually", "basically", "really",
           "quite", "rather", "pretty", "very", "super", "totally",
           "definitely", "absolutely", "honestly", "obviously", "certainly",
           "just", "sort", "kind", "somewhat", "slightly", "fairly",
           "anyway", "though", "however", "therefore", "because", "since",
           "great", "awesome", "amazing", "lovely", "brilliant", "nice",
           "thanks", "sorry", "keen", "happy", "excited"),
}
LEXICON_TERMS: Tuple[str, ...] = tuple(sorted(
    set(STYLE_LEXICON["sv"]) | set(STYLE_LEXICON["en"])))
_LEXICON_SET = frozenset(LEXICON_TERMS)
_SV_LEXICON = frozenset(STYLE_LEXICON["sv"])
_EN_LEXICON = frozenset(STYLE_LEXICON["en"])
LEXICON_PRIOR = 0.5       # Jeffreys prior per term, so a zero count is finite


def _term_lang(term: str) -> str:
    """"sv", "en" or "both" for a lexicon term. A word that is in neither list
    cannot reach this function, because the lexicon is the universe."""
    in_sv, in_en = term in _SV_LEXICON, term in _EN_LEXICON
    if in_sv and in_en:
        return "both"
    return "sv" if in_sv else "en"


# ── Emoji ──────────────────────────────────────────────────────────────────
# Ranges rather than a library. This is deliberately approximate and the
# approximation is stated: a ZWJ sequence (👨‍👩‍👧) counts as one emoji because the
# joiner and the following base are skipped, but an unusual sequence may split.
# Precision here buys nothing — the statistic used is "share of messages that
# carry any emoji", which is insensitive to the counting rule.
_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF), (0x1F000, 0x1F2FF), (0x2600, 0x27BF),
    (0x2B00, 0x2BFF), (0x1F1E6, 0x1F1FF), (0xFE0F, 0xFE0F),
    (0x2190, 0x21FF), (0x2700, 0x27BF),
)
_EMOJI_SKIP = {0x200D, 0xFE0F, 0xFE0E} | set(range(0x1F3FB, 0x1F400))


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    if cp in _EMOJI_SKIP:
        return False
    return any(low <= cp <= high for low, high in _EMOJI_RANGES)


def emoji_in(text: str) -> List[str]:
    """Emoji characters in `text`, joiners and skin-tone modifiers folded away."""
    out: List[str] = []
    skip_next_base = False
    for ch in text or "":
        cp = ord(ch)
        if cp in _EMOJI_SKIP:
            skip_next_base = (cp == 0x200D)
            continue
        if _is_emoji(ch):
            if skip_next_base:
                skip_next_base = False
                continue
            out.append(ch)
        else:
            skip_next_base = False
    return out


# ── Sentences ──────────────────────────────────────────────────────────────
# Abbreviations that end in a period and do not end a sentence. Closed and
# short on purpose: a long list is a maintenance burden that goes stale, and the
# residual over-splitting is reported rather than fixed by guesswork.
ABBREVIATIONS = frozenset({
    "t.ex", "bl.a", "dvs", "osv", "mm", "kl", "nr", "ca", "fr.o.m", "t.o.m",
    "e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "prof", "st", "jr",
    "inc", "ltd", "approx", "no",
})
_SENT_SPLIT = re.compile(r"([.!?…]+)(\s+|$)")


def sentences_of(text: str) -> List[str]:
    """Split into sentences. Approximate — see ABBREVIATIONS and the docstring.

    Guards two cases that would otherwise inflate the sentence count badly: a
    period between digits ("1.5", "kl 14.30") and a period after a known
    abbreviation. A newline also ends a sentence, because chat messages routinely
    use one instead of a full stop.
    """
    text = (text or "").strip()
    if not text:
        return []
    out: List[str] = []
    for block in re.split(r"\n+", text):
        buf = ""
        pos = 0
        for m in _SENT_SPLIT.finditer(block):
            head = block[pos:m.start()]
            mark = m.group(1)
            before = (buf + head).rstrip()
            tail = re.split(r"[\s(]", before)[-1].lower() if before else ""
            after = block[m.end():m.end() + 1]
            digit_run = (mark == "." and before[-1:].isdigit() and after.isdigit())
            if digit_run or tail.rstrip(".") in ABBREVIATIONS:
                buf += head + mark + m.group(2)
                pos = m.end()
                continue
            piece = (buf + head + mark).strip()
            if piece:
                out.append(piece)
            buf = ""
            pos = m.end()
        rest = (buf + block[pos:]).strip()
        if rest:
            out.append(rest)
    return out


# ── Accumulators: exact order statistics in bounded memory ─────────────────
class _Dist:
    """A frequency table over integers. Exact quantiles, memory O(distinct values).

    Why not a list of every observation: an archive can hold hundreds of
    thousands of messages and there are several length statistics per bucket, so
    the list version is tens of megabytes that grow with the corpus. A Counter
    keyed by the value grows with the RANGE of the value instead — a few thousand
    entries for a message length, whatever the message count. Values above `cap`
    fold into one overflow key, so a pathological 2MB message costs one entry.

    There is no `mean()` and there will not be one. A single 4,000-word message
    must not move the number a drafter is told to hit.
    """

    __slots__ = ("counts", "cap", "n", "over")

    def __init__(self, cap: int = 100000) -> None:
        self.counts: Counter = Counter()
        self.cap = cap
        self.n = 0
        self.over = 0

    def add(self, value: Any) -> None:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return
        if v < 0:
            return
        if v > self.cap:
            self.over += 1
            v = self.cap
        self.counts[v] += 1
        self.n += 1

    def quantile(self, q: float) -> Optional[int]:
        """The q-th order statistic (nearest-rank, no interpolation), or None."""
        if not self.n:
            return None
        rank = max(1, min(self.n, math.ceil(q * self.n)))
        seen = 0
        for value in sorted(self.counts):
            seen += self.counts[value]
            if seen >= rank:
                return value
        return None

    def summary(self) -> Dict[str, Any]:
        """Order statistics plus the denominator. `n` is in here because a median
        without its count is the kind of number this codebase has shipped before."""
        p25, p75 = self.quantile(0.25), self.quantile(0.75)
        return {
            "n": self.n,
            "min": self.quantile(0.0),
            "p10": self.quantile(0.10),
            "p25": p25,
            "median": self.quantile(0.50),
            "p75": p75,
            "p90": self.quantile(0.90),
            "max": max(self.counts) if self.counts else None,
            "iqr": (p75 - p25) if (p25 is not None and p75 is not None) else None,
            "over_cap": self.over,
            "cap": self.cap,
        }


class _Rate:
    """hits out of n. Serialises with both, always — a percentage on its own is
    the shape that let "30 of 400 matches" ship without the 400."""

    __slots__ = ("hits", "n")

    def __init__(self) -> None:
        self.hits = 0
        self.n = 0

    def add(self, hit: bool, weight: int = 1) -> None:
        self.n += weight
        if hit:
            self.hits += weight

    def bump(self, hits: int, n: int) -> None:
        self.hits += hits
        self.n += n

    def summary(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "n": self.n,
            "share": round(self.hits / self.n, 4) if self.n else None,
            "per_100": round(100.0 * self.hits / self.n, 2) if self.n else None,
        }


class _Bucket:
    """Every statistic for one bucket. One class, so a bucket and the overall
    aggregate cannot drift apart in shape."""

    def __init__(self) -> None:
        self.messages = 0
        self.chars = _Dist(cap=200000)
        self.words = _Dist(cap=50000)
        self.sentences_per_msg = _Dist(cap=2000)
        self.words_per_sentence = _Dist(cap=2000)
        self.paragraphs = _Dist(cap=1000)
        self.lines = _Dist(cap=5000)
        self.multi_paragraph = _Rate()
        self.terminal: Counter = Counter()
        self.commas = _Rate()          # commas per 100 words
        self.dash = _Rate()
        self.ellipsis = _Rate()
        self.exclaim_msgs = _Rate()
        self.exclaim_marks = _Rate()   # marks per 100 words
        self.question_msgs = _Rate()
        self.emoji_msgs = _Rate()
        self.emoji_counts: Counter = Counter()
        self.lower_start = _Rate()
        self.allcaps_words = _Rate()   # per 100 words
        self.greeting = _Rate()
        self.greetings: Counter = Counter()
        self.signoff = _Rate()
        self.signoffs: Counter = Counter()
        self.opener_words: Counter = Counter()
        self.opener_other = 0
        self.contraction = _Rate()     # contracted / (contracted + expanded)
        self.spoken = _Rate()          # spoken form / (spoken + written), sv
        self.lang_conf = _Dist(cap=1000)   # confidence × 100

    # -- ingestion ---------------------------------------------------------
    def add(self, clean: str, lang: str, conf: float,
            toks: Optional[List[str]] = None) -> None:
        """Fold one message in. `toks` may be passed when the caller has already
        tokenised the body, which `measure` has (it needs the same tokens for the
        lexicon counts) — tokenising a message three times was pure waste."""
        self.messages += 1
        low = clean.lower()
        if toks is None:
            toks = _tokens(clean)
        nwords = len(toks)

        self.chars.add(len(clean))
        self.words.add(nwords)
        self.lang_conf.add(round(conf * 100))

        sents = sentences_of(clean)
        self.sentences_per_msg.add(len(sents))
        for s in sents:
            self.words_per_sentence.add(_word_count(s))

        paras = [p for p in re.split(r"\n\s*\n", clean) if p.strip()]
        self.paragraphs.add(len(paras))
        self.lines.add(len([ln for ln in clean.split("\n") if ln.strip()]))
        self.multi_paragraph.add(len(paras) > 1)

        self.terminal[_terminal_class(clean)] += 1
        if nwords:
            self.commas.bump(clean.count(","), nwords)
            self.exclaim_marks.bump(clean.count("!"), nwords)
            self.allcaps_words.bump(
                sum(1 for t in _WORD_RE.findall(clean)
                    if len(t) > 1 and t.isupper()), nwords)
        self.dash.add(bool(re.search(r"(\s[-–—]\s|—)", clean)))
        self.ellipsis.add("..." in clean or "…" in clean)
        self.exclaim_msgs.add("!" in clean)
        self.question_msgs.add("?" in clean)

        found = emoji_in(clean)
        self.emoji_msgs.add(bool(found))
        for e in found:
            self.emoji_counts[e] += 1

        first_alpha = next((ch for ch in clean if ch.isalpha()), "")
        if first_alpha:
            self.lower_start.add(first_alpha.islower())

        hit = greeting_of(clean)
        self.greeting.add(bool(hit))
        if hit:
            self.greetings[hit] += 1
        sign = signoff_of(clean)
        self.signoff.add(bool(sign))
        if sign:
            self.signoffs[sign] += 1
        opener = opener_word_of(clean)
        if opener:
            self.opener_words[opener] += 1
        elif not hit:
            self.opener_other += 1

        if lang == "en":
            contracted = len(EN_CONTRACTION_RE.findall(clean))
            expanded = _count_phrases(low, _EN_EXPANDED_RE)
            if contracted or expanded:
                self.contraction.bump(contracted, contracted + expanded)
        elif lang == "sv":
            spoken = _count_phrases(low, _SV_SPOKEN_RE)
            written = _count_phrases(low, _SV_WRITTEN_RE)
            if spoken or written:
                self.spoken.bump(spoken, spoken + written)

    # -- aggregation -------------------------------------------------------
    def merge(self, other: "_Bucket") -> None:
        """Fold another bucket into this one, exactly.

        This is how the whole-corpus `overall` figures are built. The obvious
        alternative — calling `add()` a second time with the same message — did
        every regex, every sentence split and every allowlist lookup twice, which
        was half the runtime of the pass for a number that is just a sum. Every
        field here is additive: frequency tables merge as Counters, rates as
        (hits, n) pairs, so a merged bucket is bit-identical to one measured
        directly. The tests assert that equality.
        """
        self.messages += other.messages
        self.opener_other += other.opener_other
        for name in ("chars", "words", "sentences_per_msg", "words_per_sentence",
                     "paragraphs", "lines", "lang_conf"):
            mine, theirs = getattr(self, name), getattr(other, name)
            mine.counts.update(theirs.counts)
            mine.n += theirs.n
            mine.over += theirs.over
        for name in ("multi_paragraph", "commas", "dash", "ellipsis",
                     "exclaim_msgs", "exclaim_marks", "question_msgs",
                     "emoji_msgs", "lower_start", "allcaps_words", "greeting",
                     "signoff", "contraction", "spoken"):
            mine, theirs = getattr(self, name), getattr(other, name)
            mine.hits += theirs.hits
            mine.n += theirs.n
        for name in ("terminal", "emoji_counts", "greetings", "signoffs",
                     "opener_words"):
            getattr(self, name).update(getattr(other, name))

    # -- serialisation -----------------------------------------------------
    def summary(self, keep: int) -> Dict[str, Any]:
        term_total = sum(self.terminal.values())
        return {
            "messages": self.messages,
            "length_chars": self.chars.summary(),
            "length_words": self.words.summary(),
            "sentences_per_message": self.sentences_per_msg.summary(),
            "words_per_sentence": self.words_per_sentence.summary(),
            "paragraphs_per_message": self.paragraphs.summary(),
            "lines_per_message": self.lines.summary(),
            "multi_paragraph": self.multi_paragraph.summary(),
            "terminal_punctuation": {
                "n": term_total,
                "counts": dict(self.terminal),
                "shares": {k: round(v / term_total, 4)
                           for k, v in self.terminal.items()} if term_total else {},
            },
            "commas_per_100_words": self.commas.summary(),
            "exclamation_marks_per_100_words": self.exclaim_marks.summary(),
            "messages_with_exclamation": self.exclaim_msgs.summary(),
            "messages_with_question": self.question_msgs.summary(),
            "messages_with_emoji": self.emoji_msgs.summary(),
            "top_emoji": _top(self.emoji_counts, keep),
            "messages_with_dash": self.dash.summary(),
            "messages_with_ellipsis": self.ellipsis.summary(),
            "lowercase_start": self.lower_start.summary(),
            "allcaps_words_per_100_words": self.allcaps_words.summary(),
            "greeting_rate": self.greeting.summary(),
            "top_greetings": _top(self.greetings, keep),
            "signoff_rate": self.signoff.summary(),
            "top_signoffs": _top(self.signoffs, keep),
            "top_opener_words": _top(self.opener_words, keep),
            "opener_not_allowlisted": self.opener_other,
            "contraction_rate_en": self.contraction.summary(),
            "spoken_form_rate_sv": self.spoken.summary(),
            "language_confidence_x100": self.lang_conf.summary(),
        }


def _terminal_class(text: str) -> str:
    """How the message ends: "period", "question", "exclamation", "ellipsis",
    "emoji", "none". Chat writing mostly ends in "none", and that is exactly the
    habit a drafter needs to be told about."""
    stripped = (text or "").rstrip()
    if not stripped:
        return "none"
    if stripped.endswith("…") or stripped.endswith("..."):
        return "ellipsis"
    last = stripped[-1]
    if last == "?":
        return "question"
    if last == "!":
        return "exclamation"
    if last == ".":
        return "period"
    if _is_emoji(last):
        return "emoji"
    return "none"


def _top(counter: Counter, keep: int) -> Dict[str, Any]:
    """Top `keep` items with their counts, plus the tail's total and the whole
    denominator. The tail count is the difference between this list and the truth,
    which is the number a UI needs in order not to lie about "most used".

    Ties break alphabetically, not by insertion order. `Counter.most_common`
    preserves first-seen order among equal counts, which made "your usual
    greetings" depend on the order rows came back from SQLite — the same corpus
    produced a different directive depending on how it was traversed, and two
    runs could not be compared. Sorting by (-count, term) makes the output a
    function of the data alone.
    """
    total = sum(counter.values())
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:keep]
    shown = sum(c for _, c in items)
    return {
        "items": [{"term": t, "count": c} for t, c in items],
        "shown": shown,
        "other": total - shown,
        "distinct": len(counter),
        "n": total,
    }


# ── The streaming pass ─────────────────────────────────────────────────────
def _channel_filter() -> Tuple[List[str], List[str]]:
    """(channels excluded by policy, the reasons) — read at call time."""
    excluded: List[str] = []
    if not include_linkedin():
        excluded.extend(sorted(UNSAFE_CHANNELS))
    return excluded, [f"{ch}: {CHANNEL_PROVENANCE[ch]['notes']}" for ch in excluded]


def _where(direction: int, skip_channels: List[str],
           groups_ok: bool) -> Tuple[str, List[Any]]:
    """The WHERE clause both passes use, and that the eligibility COUNT reuses.

    One builder, because the "how many were eligible" number MUST be the count of
    exactly what the pass would have read. Reconstructing it by subtracting policy
    rows and group rows from the channel totals would double-subtract any row that
    was both (a group message on an excluded channel) and need a `max(0, …)` to
    hide the error. Asking the database with the same predicate cannot drift from
    the pass.
    """
    clauses = ["direction = ?"]
    params: List[Any] = [1 if direction else 0]
    if skip_channels:
        clauses.append(f"channel NOT IN ({','.join('?' * len(skip_channels))})")
        params.extend(skip_channels)
    if not groups_ok:
        clauses.append("group_name = ''")
    return " AND ".join(clauses), params


def _eligible(arc_conn: Any, direction: int, skip_channels: List[str],
              groups_ok: bool) -> int:
    """How many rows the pass would read if there were no cap. Exact, by
    construction: same predicate as `_where`."""
    sql, params = _where(direction, skip_channels, groups_ok)
    try:
        row = arc_conn.execute(
            f"SELECT COUNT(*) AS n FROM messages WHERE {sql}", params).fetchone()
        return int(row["n"] or 0) if row else 0
    except Exception:  # noqa: BLE001 — an unreadable archive degrades to 0
        log.warning("style_profile: eligibility count failed", exc_info=True)
        return 0


def _census(arc_conn: Any) -> Dict[str, Any]:
    """Per-channel inbound/outbound/group counts for the WHOLE archive.

    Separate from the capped passes on purpose: this is the denominator the caps
    are reported against, so "50,000 of 500,000" is sayable. One GROUP BY over
    the table, no bodies read.
    """
    out: Dict[str, Any] = {"messages_total": 0, "outbound_total": 0,
                           "inbound_total": 0, "by_channel": {},
                           "channels_with_no_outbound": [], "group_rows": 0,
                           "readable": True, "reason": ""}
    try:
        rows = arc_conn.execute(
            "SELECT channel, direction, COUNT(*) AS n, "
            "       SUM(CASE WHEN group_name != '' THEN 1 ELSE 0 END) AS g "
            "FROM messages GROUP BY channel, direction"
        ).fetchall()
    except Exception:  # noqa: BLE001 — a locked or old archive degrades to empty
        log.warning("style_profile: archive census failed", exc_info=True)
        out["readable"] = False
        # NC-369. Plain words, and a whole sentence: this text is shown to the
        # person in the receipt and is also pasted after "No style profile is
        # available:" in the drafting instruction, where a bare fragment with no
        # full stop ran straight into the next sentence.
        out["reason"] = ("Your message archive could not be read, so nothing was "
                         "measured.")
        return out
    for r in rows:
        ch = r["channel"] or ""
        direction = 1 if int(r["direction"] or 0) == 1 else 0
        entry = out["by_channel"].setdefault(
            ch, {"inbound": 0, "outbound": 0, "total": 0, "group_rows": 0})
        entry["outbound" if direction else "inbound"] += r["n"]
        entry["total"] += r["n"]
        entry["group_rows"] += (r["g"] or 0)
        out["messages_total"] += r["n"]
        out["group_rows"] += (r["g"] or 0)
        out["outbound_total" if direction else "inbound_total"] += r["n"]
    out["channels_with_no_outbound"] = sorted(
        ch for ch, e in out["by_channel"].items()
        if e["outbound"] == 0 and e["inbound"] > 0)
    return out


def _registers(conn: Any) -> Dict[int, str]:
    """{contact_id: register} for every contact, computed once.

    thousands of rows, so this is bounded and cheap, and it avoids a strength
    computation per message. `register_for` is the single implementation, shared
    with `bucket_for`, so the fast path here cannot disagree with the public API.
    """
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT * FROM contacts").fetchall()
    except Exception:  # noqa: BLE001 — no crm.db means no register, not a 500
        log.warning("style_profile: contacts read failed", exc_info=True)
        return {}
    return {r["id"]: register_for(r) for r in rows}


def _excluded_contacts(conn: Any) -> "Tuple[set, bool]":
    """(contact ids the CRM says never to index, whether the list could be read).

    Invariant 2, and not a setting. `excluded` and the soft-delete flag are
    the user's own decisions about people, and a style profile has no business
    overriding them. The count of messages dropped for this reason is reported.

    NC-150. This used to return a bare set, and an empty one both when there was
    no connection and on ANY exception — a locked crm.db during an import being
    the realistic case. The receipt then read "0 excluded", which is exactly what
    it reads when nobody is excluded. The two are now distinguishable, because
    the consequence of confusing them is a style profile built from the messages
    of someone the user removed.
    """
    if conn is None:
        return set(), False
    try:
        rows = conn.execute("SELECT * FROM contacts LIMIT 1").fetchall()
        has_deleted = bool(rows) and "deleted" in rows[0].keys()
        # Hidden or deleted; hidden alone on a file too old to have the column.
        pop = "active" if has_deleted else "visible"
        sql = f"SELECT id FROM contacts WHERE {figures.population_not_where(pop)}"
        return {r["id"] for r in conn.execute(sql)}, True
    except Exception:  # noqa: BLE001
        log.warning("style_profile: exclude read failed", exc_info=True)
        return set(), False


def _inbound_lexicon(arc_conn: Any, skip_channels: List[str],
                     cap: int, groups_ok: bool) -> Dict[str, Any]:
    """Closed-lexicon token counts over INBOUND messages — the other half of the
    log-odds ratio.

    A second bounded pass, not a second scan of everything: it accumulates two
    integers and a Counter over `LEXICON_TERMS` and reads nothing else. This pass
    is the reason the distinctiveness measure is honest. One streaming scan of
    outbound messages cannot produce a log-odds ratio, because a ratio needs the
    denominator from the side you did not scan, so a single scan would ship a
    number with no comparison in it.
    """
    counts: Counter = Counter()
    tokens = 0
    scanned = 0
    sql, params = _where(0, skip_channels, groups_ok)
    params = list(params) + [cap]
    try:
        cur = arc_conn.execute(
            f"SELECT body FROM messages WHERE {sql} ORDER BY id LIMIT ?", params)
        for row in cur:                       # iterate: never fetchall()
            scanned += 1
            clean, _ = _clean_body(row["body"] or "")
            for tok in _tokens(clean):
                tokens += 1
                if tok in _LEXICON_SET:
                    counts[tok] += 1
    except Exception:  # noqa: BLE001 — degrade to "not computable", never 500
        log.warning("style_profile: inbound pass failed", exc_info=True)
        return {"scanned": scanned, "tokens": tokens, "counts": counts,
                "ok": False,
                "reason": "reading your received messages stopped part way"}
    return {"scanned": scanned, "tokens": tokens, "counts": counts,
            "ok": True, "reason": ""}


def _log_odds(out_counts: Counter, out_tokens: int,
              in_counts: Counter, in_tokens: int, keep: int) -> Dict[str, Any]:
    """Weighted log-odds with a Jeffreys prior, over the closed style lexicon.

    For a term w, with n_out / n_in the lexicon-token totals on each side and
    a = LEXICON_PRIOR:

        delta = log((y_out + a) / (n_out + a0 - y_out - a))
              - log((y_in  + a) / (n_in  + a0 - y_in  - a))
        var   ~ 1/(y_out + a) + 1/(y_in + a)
        z     = delta / sqrt(var)

    The universe is the lexicon, NOT all vocabulary — so `z` ranks style words
    against each other, and a positive z means "the owner reaches for this more than their
    correspondents do". That restriction is what makes the result safe to emit
    (see the module docstring); it is also why the numbers must not be read as
    general keyness. Both denominators travel with the result.

    `computable` is False when either side has too few lexicon tokens to compare.
    It is never quietly zero.
    """
    a = LEXICON_PRIOR
    a0 = a * len(LEXICON_TERMS)
    n_out = sum(out_counts.values())
    n_in = sum(in_counts.values())
    if n_out < 1 or n_in < 1:
        return {"computable": False,
                "reason": "one side has none of the style words, so there is "
                          "nothing to compare against",
                "outbound_lexicon_tokens": n_out, "inbound_lexicon_tokens": n_in,
                "outbound_tokens": out_tokens, "inbound_tokens": in_tokens,
                "lexicon_size": len(LEXICON_TERMS), "items": []}
    scored: List[Dict[str, Any]] = []
    for term in LEXICON_TERMS:
        y_out, y_in = out_counts.get(term, 0), in_counts.get(term, 0)
        if y_out + y_in == 0:
            continue
        num_o = y_out + a
        den_o = max(1e-9, n_out + a0 - y_out - a)
        num_i = y_in + a
        den_i = max(1e-9, n_in + a0 - y_in - a)
        delta = math.log(num_o / den_o) - math.log(num_i / den_i)
        var = 1.0 / num_o + 1.0 / num_i
        scored.append({
            "term": term,
            # Which language's lexicon this term belongs to. Carried per item so
            # `style_directive` can filter to the bucket's language: the counts
            # are corpus-wide, and without this a Swedish directive told the
            # drafter to reach for "honestly" and "great".
            "lang": _term_lang(term),
            "outbound_count": y_out,
            "inbound_count": y_in,
            "log_odds": round(delta, 4),
            "z": round(delta / math.sqrt(var), 3),
        })
    scored.sort(key=lambda d: -d["z"])
    return {
        "computable": True,
        "reason": "",
        "method": "weighted log-odds, Jeffreys prior, universe = the closed "
                  "STYLE_LEXICON only",
        "lexicon_size": len(LEXICON_TERMS),
        "outbound_tokens": out_tokens,
        "inbound_tokens": in_tokens,
        "outbound_lexicon_tokens": n_out,
        "inbound_lexicon_tokens": n_in,
        "terms_compared": len(scored),
        "items": scored[:keep],
        "under_used": scored[-keep:][::-1] if len(scored) > keep else [],
    }


def measure(conn: Any, limit: Optional[int] = None,
            min_chars: Optional[int] = None) -> Dict[str, Any]:
    """The streaming pass. `conn` is the **crm.db** connection.

    archive.db is opened here, and only through `arc.open_if_present()`, so a
    missing, locked or corrupt archive yields an empty-but-shaped profile rather
    than a 500 — the `enrich_facts.py` / `api_messages.py` precedent. `conn` is
    crm.db because the register axis needs relationship strength and the exclude
    flags. Pass None and the pass refuses (NC-150): without the exclude list it
    cannot promise it did not learn from someone you removed, and the profile says
    so. A message whose person has no row in crm.db is kept with the "unknown"
    register and counted as kept (`kept_register_unknown`), never as dropped.

    `limit` and `min_chars` default to `max_messages()` and `min_chars_setting()`,
    read HERE rather than captured at import, so a Settings edit applies to the
    next run. Both appear in the returned `caps` block with the totals they were
    applied against.

    Two bounded passes, both iterating a cursor (never `fetchall`):
      1. outbound (`direction = 1`) — every style statistic, bucketed;
      2. inbound (`direction = 0`) — closed-lexicon counts only, which is the
         comparison side the distinctiveness measure cannot do without.

    Nothing is dropped silently. Every message read but not used increments a
    named counter in `corpus.dropped`, and `report()` turns those into sentences.
    """
    cap = max_messages() if limit is None else max(1, int(limit))
    floor = min_chars_setting() if min_chars is None else max(0, int(min_chars))
    keep = top_n()
    groups_ok = include_groups()
    skip_channels, skip_reasons = _channel_filter()

    result: Dict[str, Any] = {
        "archive_present": False,
        "census": {"messages_total": 0, "outbound_total": 0, "inbound_total": 0,
                   "by_channel": {}, "channels_with_no_outbound": [],
                   "group_rows": 0, "readable": False,
                   "reason": "archive.db is not present"},
        "corpus": {"outbound_scanned": 0, "outbound_available": 0,
                   "outbound_capped": False, "inbound_scanned": 0,
                   "inbound_available": 0, "inbound_capped": False,
                   "used_for_style": 0, "dropped": {},
                   # NC-369. False until the outbound pass has actually run, so
                   # a receipt can tell "nothing was measured" from "zero".
                   "pass_ran": False},
        "buckets": {}, "skipped_buckets": [], "overall": _Bucket().summary(keep),
        "vocabulary": {"computable": False,
                       "reason": "archive.db is not present, so there is no "
                                 "corpus to compare",
                       "items": []},
        "caps": {"max_messages": cap, "min_chars": floor, "min_bucket": min_bucket(),
                 "top_n": keep, "lang_margin": lang_margin(),
                 "include_linkedin": include_linkedin(),
                 "include_groups": groups_ok},
        "exclusions": [],
        "degraded": True,
        "degraded_reason": "Your message archive is missing or could not be "
                           "opened, so the profile is empty. Drafting messages "
                           "keeps working without a writing style.",
    }

    with arc.open_if_present() as arc_conn:
        if arc_conn is None:
            return result
        result["archive_present"] = True
        result["degraded"] = False
        result["degraded_reason"] = ""
        # NC-369. The archive IS there. The starting reason ("archive.db is not
        # present") would be false about it, and it was what a receipt printed
        # when the archive could not be read. A pass that completes replaces the
        # whole block below.
        result["vocabulary"]["reason"] = ("no measurement was made, so there was "
                                          "nothing to compare")
        census = _census(arc_conn)
        result["census"] = census
        if not census["readable"]:
            result["degraded"] = True
            result["degraded_reason"] = census["reason"]
            return result

        registers = _registers(conn)
        excluded_ids, exclusions_readable = _excluded_contacts(conn)
        if not exclusions_readable:
            # NC-150. Refusing is the only honest answer here. Invariant 2 says
            # the exclude list is not a tunable, so a profile built without it is
            # not a slightly-less-accurate profile — it is one that may have been
            # learned from the messages of someone the user removed, and nothing
            # downstream could tell.
            result["degraded"] = True
            result["degraded_reason"] = (
                "The list of people you have hidden or deleted could not be read, "
                "so no profile was built: one built without it could learn from "
                "their messages. An import is probably still running. Try again "
                "when it finishes.")
            return result

        buckets: Dict[str, _Bucket] = {}
        out_lex: Counter = Counter()
        out_tokens = 0
        dropped: Counter = Counter()
        # NC-369. Messages that were USED but whose person is no longer in the
        # contacts list. They used to be added to `dropped` as well, so the same
        # message was counted as kept and as set aside and the receipt's
        # arithmetic went negative. They get their own count, and are reported
        # as kept.
        kept_unknown_register = 0
        stopped_early = False
        clean_flags: Counter = Counter()
        scanned = 0

        eligible_out = _eligible(arc_conn, 1, skip_channels, groups_ok)
        eligible_in = _eligible(arc_conn, 0, skip_channels, groups_ok)

        sql, params = _where(1, skip_channels, groups_ok)
        params = list(params) + [cap]

        try:
            cur = arc_conn.execute(
                "SELECT contact_id, channel, body FROM messages "
                f"WHERE {sql} ORDER BY id LIMIT ?", params)
            for row in cur:                   # iterate: never fetchall()
                scanned += 1
                cid = row["contact_id"]
                if cid in excluded_ids:
                    dropped["contact_excluded_in_crm"] += 1
                    continue
                clean, flags = _clean_body(row["body"] or "")
                for name, hit in flags.items():
                    if hit:
                        clean_flags[name] += 1
                if not clean:
                    dropped["empty_after_quote_removal"] += 1
                    continue
                if len(clean) < floor:
                    dropped["shorter_than_min_chars"] += 1
                    continue
                lang, conf = detect_language(clean)
                register = registers.get(cid, "unknown")
                key = bucket_key(lang or "unknown", register,
                                 medium_for(row["channel"]))
                toks = _tokens(clean)
                buckets.setdefault(key, _Bucket()).add(clean, lang, conf, toks)
                if cid not in registers:
                    # After the add, so a failure inside it cannot leave this count
                    # one ahead of the messages actually used.
                    kept_unknown_register += 1
                out_tokens += len(toks)
                for tok in toks:
                    if tok in _LEXICON_SET:
                        out_lex[tok] += 1
        except Exception:  # noqa: BLE001 — degrade, never 500; detail to the log
            log.warning("style_profile: outbound pass failed", exc_info=True)
            stopped_early = True
            # The message being worked on when it failed was read but never
            # sorted into used or set aside. Count it as a set-aside of its own,
            # named, instead of leaving a one-message hole in the arithmetic that
            # the receipt would then blame on a bug.
            in_flight = (scanned - sum(dropped.values())
                         - sum(b.messages for b in buckets.values()))
            if in_flight > 0:
                dropped["stopped_on_this_message"] += in_flight
            result["degraded"] = True
            # NC-369. A whole sentence, because the receipt now shows it. It used
            # to be a lowercase fragment nothing ever printed, so numbers from a
            # pass that stopped part way looked like a finished measurement.
            result["degraded_reason"] = (
                "Reading your sent messages stopped part way, so the profile "
                f"covers only {_n(scanned, 'message')}, read before it stopped.")

        inbound = _inbound_lexicon(arc_conn, skip_channels, cap, groups_ok)

    # ── assemble ──
    # `overall` is the exact sum of the buckets, merged rather than measured a
    # second time — see _Bucket.merge for why that halves the pass.
    overall = _Bucket()
    for bucket in buckets.values():
        overall.merge(bucket)

    threshold = min_bucket()
    usable: Dict[str, Any] = {}
    skipped: List[Dict[str, Any]] = []
    for key, bucket in sorted(buckets.items()):
        summary = bucket.summary(keep)
        summary["usable"] = bucket.messages >= threshold
        summary["min_required"] = threshold
        usable[key] = summary
        if bucket.messages < threshold:
            skipped.append({
                "bucket": key, "messages": bucket.messages,
                "min_required": threshold,
                "reason": f"{bucket.messages} messages is below the "
                          f"{threshold}-message minimum (NC_STYLE_MIN_BUCKET), so "
                          "its numbers are reported but not used for drafting",
            })

    out_available = result["census"]["outbound_total"]
    in_available = result["census"]["inbound_total"]
    result["corpus"] = {
        "outbound_scanned": scanned,
        "outbound_available": out_available,
        "outbound_eligible": eligible_out,
        # NC-369. "Capped" means the cap stopped the pass. A pass that stopped
        # because reading failed also leaves messages unread, and calling that a
        # cap would send the person to raise a limit that was never the problem.
        "outbound_capped": eligible_out > scanned and not stopped_early,
        "outbound_stopped_early": stopped_early,
        "outbound_not_read": max(0, eligible_out - scanned),
        "outbound_excluded_by_policy": max(0, out_available - eligible_out),
        "inbound_scanned": inbound["scanned"],
        "inbound_available": in_available,
        "inbound_eligible": eligible_in,
        "inbound_capped": eligible_in > inbound["scanned"],
        "used_for_style": overall.messages,
        # NC-369. A subset of `used_for_style`, not of `dropped`.
        "kept_register_unknown": kept_unknown_register,
        "dropped": dict(sorted(dropped.items())),
        "dropped_total": sum(dropped.values()),
        "pass_ran": True,
        "cleaned": dict(sorted(clean_flags.items())),
    }
    result["buckets"] = usable
    result["skipped_buckets"] = skipped
    result["overall"] = overall.summary(keep)
    result["vocabulary"] = _log_odds(out_lex, out_tokens,
                                     inbound["counts"], inbound["tokens"], keep)
    if not inbound["ok"]:
        result["vocabulary"]["computable"] = False
        result["vocabulary"]["reason"] = inbound["reason"]
    result["exclusions"] = _exclusions(result["census"], skip_channels,
                                       skip_reasons, groups_ok, floor, dropped,
                                       kept_unknown_register)
    return result


def _exclusions(census: Dict[str, Any], skip_channels: List[str],
                skip_reasons: List[str], groups_ok: bool, floor: int,
                dropped: Counter,
                kept_unknown_register: int = 0) -> List[Dict[str, Any]]:
    """Every refusal, each with a count, a reason and the setting that changes it.

    This is the list that keeps the module honest. A filter with no entry here is
    a silent drop, which is a hidden limit.
    """
    out: List[Dict[str, Any]] = []
    for ch in skip_channels:
        rows = (census["by_channel"].get(ch) or {})
        out.append({
            "kind": "channel_policy",
            "what": f"channel {ch}",
            "count": rows.get("outbound", 0),
            "of": rows.get("total", 0),
            "why": CHANNEL_PROVENANCE.get(ch, {}).get("notes", "")
                   or "excluded by policy",
            "setting": "NC_STYLE_INCLUDE_LINKEDIN" if ch == "linkedin" else "",
        })
    if not groups_ok and census.get("group_rows"):
        out.append({
            "kind": "group_policy",
            "what": "group-chat messages",
            "count": census["group_rows"],
            "of": census["messages_total"],
            "why": "writing to a group is one-to-many, so it does not belong to a "
                   "one-person register. WhatsApp group rows are hardcoded inbound, "
                   "so including them can only add other people's messages.",
            "setting": "NC_STYLE_INCLUDE_GROUPS",
        })
    if dropped.get("shorter_than_min_chars"):
        out.append({
            "kind": "too_short",
            "what": f"messages shorter than {floor} characters",
            "count": dropped["shorter_than_min_chars"],
            "of": census["outbound_total"],
            "why": "too short to carry a sentence, a paragraph or a sign-off; "
                   "counted in the census, kept out of the style statistics",
            "setting": "NC_STYLE_MIN_CHARS",
        })
    if dropped.get("contact_excluded_in_crm"):
        out.append({
            "kind": "contact_excluded",
            "what": "messages to contacts excluded in the CRM",
            "count": dropped["contact_excluded_in_crm"],
            "of": census["outbound_total"],
            "why": "invariant 2 — the exclude list and soft delete are your "
                   "decisions about people, not a tunable for this module",
            "setting": "",
        })
    if dropped.get("empty_after_quote_removal"):
        out.append({
            "kind": "empty_after_clean",
            "what": "messages that were only quoted text or a signature",
            "count": dropped["empty_after_quote_removal"],
            "of": census["outbound_total"],
            "why": "nothing you wrote remained after quoted replies and "
                   "signature blocks were removed",
            "setting": "",
        })
    if dropped.get("stopped_on_this_message"):
        out.append({
            "kind": "stopped_midway",
            "what": "the message being read when reading stopped",
            "count": dropped["stopped_on_this_message"],
            "of": census["outbound_total"],
            "why": "it was read but could not be counted, because reading "
                   "stopped on it",
            "setting": "",
        })
    if kept_unknown_register:
        out.append({
            "kind": "register_unknown",
            "what": "messages to people who are no longer in your contacts",
            "count": kept_unknown_register,
            "of": census["outbound_total"],
            "why": "kept, but filed as 'closeness unknown': guessing 'barely "
                   "know' would tell a drafter to write formally to an old friend",
            "setting": "",
        })
    for ch in sorted(census.get("channels_with_no_outbound") or []):
        rows = (census["by_channel"].get(ch) or {})
        out.append({
            "kind": "no_outbound",
            "what": f"channel {ch} contributed no outbound messages",
            "count": 0,
            "of": rows.get("inbound", 0),
            "why": _no_outbound_reason(ch),
            "setting": "",
        })
    return out


def _no_outbound_reason(channel: str) -> str:
    """Why a channel with inbound messages has no outbound ones. This is the
    observable symptom of the two degradations described in the module
    docstring, so it is worth naming precisely instead of shrugging.

    Shown to a person, so it names what to do in the app's own words: the import
    that wrote the rows, and the "Your email addresses" box in Settings."""
    if channel == "whatsapp":
        return ("None of your WhatsApp messages are marked as sent by you. That "
                "usually means the import could not read which messages were "
                "yours, so every one was stored as received. Import WhatsApp "
                "again.")
    if channel == "email":
        if not (os.environ.get("SELF_EMAIL") or "").strip():
            return ("None of your emails are marked as sent by you, because the "
                    "app does not know your email address, so every email was "
                    "stored as received. Add your email addresses in Settings, "
                    "then import your email again.")
        return ("Your email address is set now, but it may not have been when "
                "your emails were imported, so they may all be stored as "
                "received. Import your email again.")
    return ("None of its messages are marked as sent by you, so it adds nothing "
            "to your writing style.")


# ── The profile ────────────────────────────────────────────────────────────
def profile(conn: Any, limit: Optional[int] = None,
            min_chars: Optional[int] = None) -> Dict[str, Any]:
    """The whole offline pipeline in one call: measure → annotate → version.

    Returns everything a caller or a UI needs to decide whether to trust the
    result: the buckets and their statistics, the corpus census, the exclusions
    with counts, the per-channel direction provenance, the settings that were in
    force, and a `version`.

    Writes nothing. `save()` is a separate, explicit call.
    """
    result = measure(conn, limit=limit, min_chars=min_chars)
    result["version"] = PROFILE_VERSION
    result["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    result["state_key"] = STATE_KEY
    result["offline"] = True
    result["network_calls"] = 0
    result["provenance"] = {
        "channels": CHANNEL_PROVENANCE,
        "unsafe_channels": sorted(UNSAFE_CHANNELS),
        "self_email_set": bool((os.environ.get("SELF_EMAIL") or "").strip()),
        "self_name_set": bool((os.environ.get("SELF_NAME") or "").strip()),
        "note": "direction=1 means 'sent by me', but how much that is worth "
                "depends on the importer that wrote the row. See channels.",
    }
    result["safety"] = {
        "directive_contains_message_text": False,
        "directive_contains_names": False,
        "how": "style_directive() emits only numbers, sentences written in "
               "crm/style_profile.py, tokens from the closed GREETINGS / "
               "SIGNOFFS / OPENER_WORDS / STYLE_LEXICON allowlists, and emoji "
               "characters. Free-text harvesting is refused everywhere.",
    }
    result["throughput_note"] = THROUGHPUT_NOTE
    result["best_bucket"] = _best_bucket(result)
    return result


def _best_bucket(prof: Dict[str, Any]) -> str:
    """The largest usable bucket, or "" when none clears the minimum. Used as the
    fallback target so a directive for a thin bucket has somewhere honest to go."""
    usable = [(v.get("messages", 0), k) for k, v in (prof.get("buckets") or {}).items()
              if v.get("usable")]
    return max(usable)[1] if usable else ""


# ── Storage ────────────────────────────────────────────────────────────────
def save(conn: Any, prof: Dict[str, Any]) -> Dict[str, Any]:
    """Store the profile as JSON under `STATE_KEY` in crm.db's `state` table.

    Nothing copies `state` off this Mac, which is why a profile derived from
    archive.db may live there without breaking invariant 7 — and why this
    feature needs no schema change. Returns a receipt: what was written,
    how big it was, and under which key.
    """
    prof = dict(prof or {})
    prof.setdefault("version", PROFILE_VERSION)
    prof["saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    blob = json.dumps(prof, ensure_ascii=False, sort_keys=True, default=str)
    db.state_set(conn, STATE_KEY, blob)
    conn.commit()
    return {"ok": True, "key": STATE_KEY, "bytes": len(blob.encode("utf-8")),
            "version": prof["version"], "saved_at": prof["saved_at"],
            "buckets": len(prof.get("buckets") or {})}


def load(conn: Any) -> Dict[str, Any]:
    """Read the stored profile back. Always a dict, always with `present`.

    Four outcomes, and none of them raises or returns modern-shaped defaults that
    pretend to be measured data:

      absent    {"present": False, "reason": …} — nothing has been measured yet
      unreadable  same shape, different reason — the blob is not JSON
      stale     the profile, plus present=True and stale=True, when its `version`
                is not PROFILE_VERSION. The numbers are still real; the shape may
                have moved, so the caller is told rather than surprised.
      current   the profile with present=True, stale=False
    """
    raw = db.state_get(conn, STATE_KEY)
    if not raw:
        return {"present": False, "stale": False, "version": None,
                "reason": "no writing style profile has been measured yet",
                "state_key": STATE_KEY}
    try:
        prof = json.loads(raw)
    except (TypeError, ValueError):
        # The blob is not echoed anywhere: a corrupt value could be anything.
        log.warning("style_profile: stored profile is not readable JSON")
        return {"present": False, "stale": False, "version": None,
                "reason": "the saved profile could not be read; measure again",
                "state_key": STATE_KEY}
    if not isinstance(prof, dict):
        return {"present": False, "stale": False, "version": None,
                "reason": "the saved profile is not an object; measure again",
                "state_key": STATE_KEY}
    version = prof.get("version")
    prof["present"] = True
    prof["stale"] = version != PROFILE_VERSION
    prof["reason"] = ("" if not prof["stale"] else
                      f"saved by version {version}, this build expects "
                      f"{PROFILE_VERSION} — measure again to refresh")
    return prof


# ── The directive: the point of the feature ────────────────────────────────
def _pct(rate: Optional[Dict[str, Any]]) -> Optional[int]:
    """A share as a whole percent, or None when the denominator is zero."""
    if not rate or not rate.get("n"):
        return None
    return int(round(100.0 * rate["hits"] / rate["n"]))


def _terms(block: Optional[Dict[str, Any]], keep: int = 3) -> List[str]:
    """The top terms of a `_top()` block. Every term came from a closed
    allowlist defined in this file, which is what makes returning them safe."""
    return [i["term"] for i in ((block or {}).get("items") or [])[:keep]]


DIRECTIVE_HEADER = "WRITING STYLE — derived statistics only, no message text."
DIRECTIVE_FOOTER = ("These are measurements, not examples. Do not reproduce "
                    "phrasing from any archive; write fresh text that matches "
                    "these habits.")
LANG_WORD = {"sv": "Swedish", "en": "English", "unknown": "the language the "
             "contact last used"}


def measured_languages() -> List[str]:
    """The languages `detect_language` can tell apart, in words: ["Swedish",
    "English"]. NC-326: the Data page names them from here rather than from a
    sentence of its own, so a language added to LANG_VALUES is named there too.
    Everything else is filed under "unknown" and measured as one group."""
    return [LANG_WORD[k] for k in LANG_VALUES if k != "unknown" and k in LANG_WORD]
REGISTER_WORD = {"close": "someone you know well", "new": "someone you barely "
                 "know", "unknown": "someone whose closeness is not recorded"}
MEDIUM_WORD = {"letter": "an email", "chat": "a chat message"}


def style_directive(prof: Dict[str, Any], bucket_key_: str) -> str:
    """A short plain-language instruction block for a draft-message system prompt.

    THE SAFETY PROPERTY, which a test should try to break: the returned string
    contains no message text and no third-party name. It is assembled from
    numbers, from sentences written in this file, from tokens that came out of
    the closed GREETINGS / SIGNOFFS / OPENER_WORDS / STYLE_LEXICON allowlists,
    and from emoji characters. There is no code path here that copies a substring
    of a message body, because no such substring is stored in the profile.

    Degraded paths, all visible and all stated in the returned text (invariant 8):
      • no profile measured yet → a one-line block saying so, and the drafter
        proceeds with its own default voice;
      • archive absent → the same;
      • the requested bucket is below `NC_STYLE_MIN_BUCKET` → the largest usable
        bucket's numbers are used and the substitution is named, with both counts;
      • no bucket clears the minimum → the whole-corpus numbers are used and that
        is named too.
    """
    if not prof or not prof.get("present", True):
        return (f"{DIRECTIVE_HEADER}\nNo style profile has been measured yet "
                f"({(prof or {}).get('reason', 'nothing stored')}). Write in your "
                "own default voice.")
    if prof.get("degraded"):
        return (f"{DIRECTIVE_HEADER}\nNo style profile is available: "
                f"{prof.get('degraded_reason', 'the archive could not be read')} "
                "Write in your own default voice.")

    buckets = prof.get("buckets") or {}
    threshold = (prof.get("caps") or {}).get("min_bucket", MIN_BUCKET_DEFAULT)
    requested = bucket_key_ or ""
    chosen = requested
    notes: List[str] = []

    entry = buckets.get(requested)
    if entry and entry.get("usable"):
        stats = entry
    else:
        fallback = prof.get("best_bucket") or _best_bucket(prof)
        have = (entry or {}).get("messages", 0)
        if fallback and fallback != requested:
            stats = buckets[fallback]
            chosen = fallback
            notes.append(
                f"Asked for {_bucket_words(requested)} but only {have} measured "
                f"messages match it, below the {threshold}-message minimum. Using "
                f"{_bucket_words(fallback)} instead ({stats.get('messages', 0)} "
                "messages).")
        else:
            stats = prof.get("overall") or {}
            chosen = "overall"
            notes.append(
                f"No bucket reached the {threshold}-message minimum "
                f"({_bucket_words(requested)} has {have}). Using the whole "
                f"measured corpus ({stats.get('messages', 0)} messages).")

    lines: List[str] = [DIRECTIVE_HEADER]
    total = stats.get("messages", 0)
    if chosen == "overall":
        lines.append(f"Basis: all measured outbound messages (n={total}).")
    else:
        lines.append(f"Basis: {_bucket_words(chosen)} (n={total}).")
    lines.extend(notes)

    lang = chosen.split(".")[0] if chosen != "overall" else "unknown"
    if lang in ("sv", "en"):
        lines.append(f"Language: write in {LANG_WORD[lang]}.")

    words = stats.get("length_words") or {}
    if words.get("median") is not None:
        lines.append(
            f"Length: about {words['median']} words; typical range "
            f"{words.get('p25')}–{words.get('p75')}, and 9 in 10 are under "
            f"{words.get('p90')} (median of n={words.get('n')}, not an average).")
    sent = stats.get("words_per_sentence") or {}
    if sent.get("median") is not None:
        lines.append(f"Sentences: about {sent['median']} words each "
                     f"({sent.get('p25')}–{sent.get('p75')}, n={sent.get('n')}).")
    multi = _pct(stats.get("multi_paragraph"))
    if multi is not None:
        n_multi = stats["multi_paragraph"]["n"]
        lines.append(
            f"Paragraphs: one block ({100 - multi}% of n={n_multi})."
            if multi < 20 else
            f"Paragraphs: more than one block {multi}% of the time (n={n_multi}).")

    term = stats.get("terminal_punctuation") or {}
    shares = term.get("shares") or {}
    if term.get("n"):
        none_pct = int(round(100 * shares.get("none", 0)))
        period = int(round(100 * shares.get("period", 0)))
        lines.append(f"Endings: no final punctuation {none_pct}% of the time, a "
                     f"full stop {period}% (n={term['n']}).")
    for label, key in (("exclamation marks", "messages_with_exclamation"),
                       ("questions", "messages_with_question")):
        pct = _pct(stats.get(key))
        if pct is not None:
            lines.append(f"{label.capitalize()}: in {pct}% of messages "
                         f"(n={stats[key]['n']}).")
    lower = _pct(stats.get("lowercase_start"))
    if lower is not None:
        lines.append(f"Capitalisation: starts lowercase {lower}% of the time "
                     f"(n={stats['lowercase_start']['n']}).")

    emoji_pct = _pct(stats.get("messages_with_emoji"))
    if emoji_pct is not None:
        top_e = _terms(stats.get("top_emoji"), 4)
        tail = f" Most used: {' '.join(top_e)}." if top_e else ""
        lines.append(f"Emoji: {emoji_pct}% of messages carry one "
                     f"(n={stats['messages_with_emoji']['n']}).{tail}")

    greet_pct = _pct(stats.get("greeting_rate"))
    if greet_pct is not None:
        top_g = _terms(stats.get("top_greetings"))
        tail = f" Usual openings: {', '.join(top_g)}." if top_g else ""
        lines.append(f"Greeting: opens with one {greet_pct}% of the time "
                     f"(n={stats['greeting_rate']['n']}).{tail}")
    sign_pct = _pct(stats.get("signoff_rate"))
    if sign_pct is not None:
        top_s = _terms(stats.get("top_signoffs"))
        tail = f" Usual sign-offs: {', '.join(top_s)}." if top_s else ""
        lines.append(f"Sign-off: uses one {sign_pct}% of the time "
                     f"(n={stats['signoff_rate']['n']}).{tail}")

    contraction = _pct(stats.get("contraction_rate_en"))
    if lang == "en" and contraction is not None:
        lines.append(f"Contractions: contracted form {contraction}% of the time "
                     f"(n={stats['contraction_rate_en']['n']} chances).")
    spoken = _pct(stats.get("spoken_form_rate_sv"))
    if lang == "sv" and spoken is not None:
        lines.append(f"Spoken forms: the spoken spelling {spoken}% of the time — "
                     f"e.g. nån over någon, dom over de "
                     f"(n={stats['spoken_form_rate_sv']['n']} chances).")

    vocab = prof.get("vocabulary") or {}
    if vocab.get("computable") and vocab.get("items"):
        # Filter to the bucket's language. The counts are corpus-wide, so an
        # unfiltered list told a Swedish drafter to reach for "honestly".
        items = vocab["items"]
        if lang in ("sv", "en"):
            items = [i for i in items
                     if i.get("lang", lang) in (lang, "both")]
        over = [i["term"] for i in items[:5]]
        if over:
            lines.append("Reaches for these more than their correspondents do: "
                         + ", ".join(over) + ".")
    elif vocab.get("reason"):
        lines.append(f"Vocabulary comparison not available: {vocab['reason']}.")

    lines.append(DIRECTIVE_FOOTER)
    return "\n".join(lines)


def _bucket_words(key: str) -> str:
    """"sv.close.chat" → "Swedish, to someone you know well, as a chat message"."""
    parts = (key or "").split(".")
    if len(parts) != 3:
        return key or "an unnamed bucket"
    lang, register, medium = parts
    return (f"{LANG_WORD.get(lang, lang)}, to "
            f"{REGISTER_WORD.get(register, register)}, as "
            f"{MEDIUM_WORD.get(medium, medium)}")


# ── The honesty surface ────────────────────────────────────────────────────
# The last line of every receipt. "Nothing left this machine" is the part the
# tests and the page look for; the rest says it in words a person can check.
NOTHING_LEFT = ("Nothing left this machine: no message or number was sent "
                "anywhere, and nothing went over the internet.")

# How a channel is named in a sentence. The stored key is lowercase and internal.
CHANNEL_NAME = {"imessage": "iMessage", "whatsapp": "WhatsApp", "email": "Email",
                "linkedin": "LinkedIn", "telegram": "Telegram",
                "messenger": "Messenger", "instagram": "Instagram"}


def _n(count: int, singular: str, plural: str = "") -> str:
    """"1 message" / "3 messages" — these sentences are shown to a person, and
    "1 messages were skipped" reads like a bug even when the number is right."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def report(prof: Dict[str, Any]) -> Dict[str, Any]:
    """Everything that happened, as numbers a caller can render — and a
    `sentences` list a human can read. Same standard as `ics_import.report()`.

    Nothing may leave this pipeline without appearing here. The contract:

        outbound_scanned == used_for_style + dropped_total

    where `dropped_total` counts only messages that LEFT the pipeline. A message
    kept with an unknown register is in `used_for_style` and in
    `kept_register_unknown`, and never in `dropped_total` (NC-369: it used to be
    in both, which made this arithmetic go negative). `accounting.unexplained`
    is that arithmetic, exposed. If a future change
    starts dropping messages, this number goes non-zero and the receipt says so
    instead of the count quietly shrinking. A second identity covers the caps:

        outbound_available == outbound_scanned + excluded_by_policy + not_read

    where `not_read` is what the cap left on the table.
    """
    prof = prof or {}
    census = prof.get("census") or {}
    corpus = prof.get("corpus") or {}
    caps = prof.get("caps") or {}
    buckets = prof.get("buckets") or {}
    skipped = prof.get("skipped_buckets") or []
    exclusions = prof.get("exclusions") or []
    vocab = prof.get("vocabulary") or {}

    scanned = corpus.get("outbound_scanned", 0)
    used = corpus.get("used_for_style", 0)
    dropped_total = corpus.get("dropped_total", 0)
    kept_unknown = corpus.get("kept_register_unknown", 0)
    available = corpus.get("outbound_available", 0)
    eligible = corpus.get("outbound_eligible", 0)
    not_read = corpus.get("outbound_not_read", 0)
    # Taken from the corpus block, which got it from a COUNT with the pass's own
    # predicate. It used to be re-derived here by scanning `exclusions` for
    # entries whose prose began "channel " — a number that depended on the
    # wording of a sentence.
    policy_rows = corpus.get("outbound_excluded_by_policy", 0)
    usable_buckets = {k: v for k, v in buckets.items() if v.get("usable")}

    out: Dict[str, Any] = {
        "version": prof.get("version", PROFILE_VERSION),
        "generated_at": prof.get("generated_at", ""),
        "archive_present": bool(prof.get("archive_present")),
        "degraded": bool(prof.get("degraded")),
        "degraded_reason": prof.get("degraded_reason", ""),
        "offline": True,
        "network_calls": 0,
        "messages_in_archive": census.get("messages_total", 0),
        "outbound_in_archive": census.get("outbound_total", 0),
        "inbound_in_archive": census.get("inbound_total", 0),
        "outbound_scanned": scanned,
        "outbound_eligible": eligible,
        "outbound_cap": caps.get("max_messages"),
        "outbound_capped": bool(corpus.get("outbound_capped")),
        # NC-369. Reading stopped because it failed, which is not the cap.
        "outbound_stopped_early": bool(corpus.get("outbound_stopped_early")),
        "outbound_not_read": not_read,
        "inbound_scanned": corpus.get("inbound_scanned", 0),
        "inbound_eligible": corpus.get("inbound_eligible", 0),
        "inbound_capped": bool(corpus.get("inbound_capped")),
        "messages_used": used,
        # NC-369. Part of messages_used, not of messages_dropped.
        "messages_kept_register_unknown": kept_unknown,
        "messages_dropped": dropped_total,
        "dropped_by_reason": corpus.get("dropped", {}),
        "bodies_cleaned": corpus.get("cleaned", {}),
        "buckets_seen": len(buckets),
        "buckets_usable": len(usable_buckets),
        "buckets_possible": len(all_bucket_keys()),
        "buckets_skipped_for_lack_of_data": len(skipped),
        "skipped_buckets": skipped,
        "bucket_counts": {k: v.get("messages", 0) for k, v in sorted(buckets.items())},
        "min_bucket": caps.get("min_bucket"),
        "best_bucket": prof.get("best_bucket", ""),
        "exclusions": exclusions,
        "excluded_by_policy": policy_rows,
        "channels_with_no_outbound": census.get("channels_with_no_outbound", []),
        "linkedin_included": bool(caps.get("include_linkedin")),
        "groups_included": bool(caps.get("include_groups")),
        # NC-369. False when the pass never ran (the archive could not be read,
        # or the list of hidden people could not), so a caller cannot mistake the
        # zeros below for a measured empty result.
        "measured": bool(corpus.get("pass_ran", "outbound_eligible" in corpus)),
        "vocabulary_computable": bool(vocab.get("computable")),
        "vocabulary_reason": vocab.get("reason", ""),
        "vocabulary_terms_compared": vocab.get("terms_compared", 0),
        "provenance": (prof.get("provenance") or {}).get("channels", {}),
        "settings": caps,
        "accounting": {
            # Identity 1: everything read was either used or set aside.
            "outbound_scanned": scanned,
            "explained": used + dropped_total,
            "unexplained": scanned - used - dropped_total,
            # Not part of the sum: how many of the `used` messages went to people
            # who are no longer in the contacts list.
            "kept_register_unknown": kept_unknown,
            # Identity 2: everything eligible was read or left by the cap.
            "outbound_eligible": eligible,
            "eligible_unexplained": eligible - scanned - not_read,
            # Identity 3: every outbound row in the archive is eligible or was
            # refused by policy.
            "available": available,
            "available_unexplained": available - eligible - policy_rows,
        },
        "wrote_anything": False,   # measuring never writes; save() does
    }

    s: List[str] = []
    if not out["archive_present"]:
        s.append("Your message archive is not there yet, so there is nothing to "
                 "measure. Drafting messages keeps working; it uses a plain "
                 "default voice.")
        out["sentences"] = s
        out["summary_text"] = " ".join(s)
        return out

    # NC-369. A measurement that failed must not read like a measurement that
    # found nothing. The reason comes first, ahead of any number, and when the
    # pass never ran there are no numbers at all: "0 archived messages in total"
    # for an archive that holds thousands is a false statement, not a count.
    if out["degraded"]:
        s.append(out["degraded_reason"]
                 or "Your message archive could not be fully read.")
    if not out["measured"]:
        if not census.get("readable", True):
            s.append("An import may still be running, or the archive may come "
                     "from an older version of the app. Wait a minute and "
                     "measure again. If it keeps failing, run any message "
                     "import once.")
        s.append("Nothing was measured, so there are no numbers to show. "
                 "Drafting messages still works; it uses a plain default voice "
                 "until this is fixed.")
        s.append(NOTHING_LEFT)
        out["sentences"] = s
        out["summary_text"] = " ".join(s)
        return out

    s.append(f"{_n(out['messages_in_archive'], 'archived message')} in total: "
             f"{out['outbound_in_archive']} marked as sent by you, "
             f"{out['inbound_in_archive']} received.")
    s.append(f"{_n(scanned, 'outbound message')} read, {used} used for the "
             f"statistics and {dropped_total} set aside — every reason is listed "
             "with a count.")
    if kept_unknown:
        s.append(f"{kept_unknown} of the {_n(used, 'message')} used went to "
                 "people who are no longer in your contacts. They still count, "
                 "but the app cannot tell how well you know them, so they are "
                 "filed as 'closeness unknown'.")
    if out["outbound_capped"]:
        s.append(f"Reading stopped at its limit of {caps.get('max_messages')} "
                 f"messages, so {_n(out['outbound_not_read'], 'more message')} "
                 "you sent "
                 f"{'was' if out['outbound_not_read'] == 1 else 'were'} not read.")
    if out["linkedin_included"]:
        s.append("LinkedIn messages are included, because that switch is on in "
                 "Settings. LinkedIn does not say who sent a message, so the app "
                 "guesses that anyone who is not in your contacts is you. "
                 "Messages from strangers can therefore be counted as your own "
                 "writing, and by how much is unknown.")
    else:
        li = next((e for e in exclusions if e.get("what") == "channel linkedin"), None)
        if li and li.get("of"):
            s.append(f"LinkedIn messages are left out: {li['count']} of its "
                     f"{li['of']} messages are marked as sent by you only by a "
                     "guess (anyone not in your contacts is assumed to be you), "
                     "which would count recruiter mail as your writing. A switch "
                     "in Settings turns them on.")
    for ch in out["channels_with_no_outbound"]:
        s.append(f"{CHANNEL_NAME.get(ch, ch.capitalize())} has messages you "
                 "received but none you sent. " + _no_outbound_reason(ch))
    if out["buckets_usable"]:
        s.append(f"{out['buckets_usable']} of the {_n(out['buckets_seen'], 'message group')} "
                 f"found ({out['buckets_possible']} are possible) hold at least "
                 f"{caps.get('min_bucket')} messages, enough to describe how you "
                 "write in that group. A group is one language, one level of "
                 "closeness to the person, and chat or email.")
    else:
        s.append(f"No group of messages holds the {caps.get('min_bucket')} messages "
                 "needed to describe your style on its own, so the numbers for "
                 "all your messages together are used instead.")
    if skipped:
        thin = ", ".join(f"{_bucket_words(b['bucket'])} ({b['messages']})"
                         for b in skipped[:6])
        s.append(f"{_n(len(skipped), 'group')} left out for having too few "
                 f"messages: {thin}" + ("…" if len(skipped) > 6 else "") + ".")
    if out["vocabulary_computable"]:
        s.append(f"The words you reach for more than other people do were found "
                 f"among {vocab.get('terms_compared', 0)} everyday style words "
                 "(greetings, sign-offs and common fillers), counting "
                 f"{vocab.get('outbound_lexicon_tokens', 0)} in messages you sent "
                 f"and {vocab.get('inbound_lexicon_tokens', 0)} in messages you "
                 "received. Only that fixed list is used, never your other words, "
                 "because the most common of those would be people's names.")
    else:
        s.append("The words you reach for were not compared with other people's: "
                 f"{out['vocabulary_reason'] or 'there was not enough data on one side'}.")
    gap = out["accounting"]["unexplained"]
    if gap > 0:
        s.append(f"WARNING: the counts do not add up. {_n(gap, 'message')} read "
                 "but neither used nor set aside. That is a fault in this app, "
                 "not something about your messages.")
    elif gap < 0:
        # NC-369. Negative means the same message was counted twice. Say that,
        # not "-16 unaccounted for", which reads as nonsense.
        s.append("WARNING: the counts do not add up. The app used or set aside "
                 f"{_n(-gap, 'message')} more than it read. That is a fault in "
                 "this app, not something about your messages.")
    s.append(NOTHING_LEFT)
    out["sentences"] = s
    out["summary_text"] = " ".join(s)
    return out


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> None:
    """`python3 crm/style_profile.py [--limit N] [--dry-run] [--json]`

    Measures and saves by default. `--dry-run` measures and prints and writes
    NOTHING — the same split as every importer in this repo.
    """
    import argparse
    import sqlite3
    import time

    ap = argparse.ArgumentParser(
        description="Measure your writing style from archive.db (offline)")
    ap.add_argument("--limit", type=int, default=None,
                    help=f"outbound messages to read (default "
                         f"{max_messages()}, NC_STYLE_MAX_MESSAGES)")
    ap.add_argument("--min-chars", type=int, default=None,
                    help=f"shortest message that enters the statistics (default "
                         f"{min_chars_setting()}, NC_STYLE_MIN_CHARS)")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and print, write nothing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--bucket", default=None,
                    help="print the style directive for this bucket key")
    args = ap.parse_args()

    conn = db.get_db()
    started = time.monotonic()
    try:
        prof = profile(conn, limit=args.limit, min_chars=args.min_chars)
    except sqlite3.OperationalError as exc:
        conn.close()
        raise SystemExit(f"Can't read crm.db ({exc}). Launch the app once so the "
                         "database exists, then re-run this.")
    elapsed = time.monotonic() - started
    rep = report(prof)
    rep["elapsed_sec"] = round(elapsed, 3)

    receipt: Dict[str, Any] = {"ok": False, "skipped": "dry run"}
    if not args.dry_run:
        receipt = save(conn, prof)
    conn.close()

    if args.json:
        print(json.dumps({"report": rep, "saved": receipt,
                          "directive": style_directive(
                              prof, args.bucket or prof.get("best_bucket", ""))},
                         indent=2, default=str))
        return

    for line in rep["sentences"]:
        print(f"  {line}")
    print()
    for key, count in rep["bucket_counts"].items():
        mark = "[x]" if (prof.get("buckets") or {}).get(key, {}).get("usable") else "[ ]"
        print(f"  {mark} {key:24s} {count:>7d}")
    print()
    for e in rep["exclusions"]:
        setting = f" [{e['setting']}]" if e.get("setting") else ""
        print(f"  - {e['what']}: {e['count']} of {e.get('of', 0)}{setting}")
    print(f"\n  Pass took {rep['elapsed_sec']}s. {THROUGHPUT_NOTE}")
    print(f"\n{style_directive(prof, args.bucket or prof.get('best_bucket', ''))}\n")
    if args.dry_run:
        print("  Nothing was written (--dry-run).")
    else:
        print(f"  Saved {receipt.get('bytes', 0)} bytes under state key "
              f"'{receipt.get('key')}'.")


if __name__ == "__main__":
    main()
