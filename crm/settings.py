"""
In-app Settings: the whitelist of env vars the app is allowed to write
(MANAGED_KEYS), plus the mask/read logic and the secrets/.env read-modify-write.

Every box on the Settings and Data pages saves through write_key below, the
only code in the app that writes secrets/.env, so every change goes through one
code path that (a) preserves unrelated lines and comments, (b) never echoes a
full secret back to the browser, (c) refuses a value outside its bounds with a
sentence (VALIDATORS), and (d) updates os.environ immediately so features that
read it (Ask, drafts, the paid fill-in runs) work without a restart.

Not every key has a box. The ones without one are listed, with what each does,
in UNMANAGED_KEYS (and the unrendered MANAGED_KEYS are marked where they sit);
those are set by adding a KEY=value line to secrets/.env by hand, and this
module keeps such lines exactly as they are.
"""

import os
import re
from typing import Any, Dict, List, Tuple

import config

MASK_PREFIX = "…"  # secrets read back as "…last4", never the full value

# key -> metadata. `default` is what a plain (non-secret) field shows when unset.
# NC-062. BOOT_ANIMATIONS, BOOT_ANIMATION_DEFAULT and boot_animation() were
# removed together with the setting that chose between boot animations. With one
# animation there is no value to validate, nothing to stamp on <html>, and no
# CSS-selector injection surface, so the whole mechanism went rather than being
# left as a constant nobody can change.


MANAGED_KEYS: Dict[str, Dict[str, Any]] = {
    "ANTHROPIC_API_KEY":  {"secret": True,  "default": ""},
    "LINKEDIN_TOKEN":     {"secret": True,  "default": ""},
    # NC-068. Token introspection needs the app's OWN credentials, not just
    # the token — LinkedIn's introspectToken takes client_id + client_secret +
    # token, and there is no bearer-only variant. Empty by default, and empty
    # means "we cannot check the expiry", which the panel says in as many
    # words rather than reporting a token it has never looked at as fine.
    # NC-371: both have a box on the LinkedIn messages row of the Data page, shown
    # once a token is connected (the secret masked like the token). Before that
    # the row told people to edit this file, the one registered secret with no box.
    "LINKEDIN_CLIENT_ID":     {"secret": False, "default": ""},
    "LINKEDIN_CLIENT_SECRET": {"secret": True,  "default": ""},
    "ANTHROPIC_MODEL":    {"secret": False, "default": "claude-opus-5-5"},
    "SELF_NAME":          {"secret": False, "default": ""},
    "SELF_EMAIL":         {"secret": False, "default": ""},
    # NC_BOOT_ANIMATION was here from NC-049 until NC-062. It survived NC-003's
    # settings cull because the animation was meant to be a personal choice; once
    # there was one animation, the choice went too.
    # TELEGRAM_BOT_TOKEN, ADMIN_USERNAME and OPENAI_API_KEY were here until the
    # phone bots were removed. Nothing read them afterwards, and a
    # settings field for a feature that no longer exists is worse than no field:
    # you fill it in and nothing happens. OPENAI_API_KEY was only ever the bot's
    # voice-note transcription — the app's AI is Anthropic throughout.
    #
    # ── NC-003: four kinds of thing, one list ───────────────────────────────────
    # Settings had grown messy, with 3-4 different types of setting mixed together.
    # This table used to render ~54 fields, and two thirds of them were raw numeric
    # tuning knobs — "longest answer, in tokens", "people one meaning search
    # returns", "days back to read" — none of which is a decision a user of a
    # personal CRM should be asked to make. The rule below applies to every key
    # already here and every key added since:
    #
    #   • identity (name/email) and credentials/connections (keys, tokens,
    #     addresses, the machine or account that unlocks a whole source) stay,
    #     open near the top of Settings.
    #   • an on/off switch stays IF a person would ever flip it. This said
    #     "every on/off switch stays, full stop" and cited NC-004 — "a switch
    #     nobody can see is a switch nobody can turn off". NC-074 found nine
    #     counter-examples a few dozen lines below this very comment, so the rule
    #     is restated rather than left to be re-derived: NC-004's bug was
    #     SCATTERING, switches buried among the numbers so nobody could find
    #     them, and it is still fixed. What NC-074 removed was DUPLICATION —
    #     seven switches asking "should this happen without me?" once per source,
    #     under a master switch that already stopped all seven, plus two
    #     configuring a pass whose row does not render. A control that only
    #     repeats a decision the user already made above it is not a control; it
    #     is a way to make the real one ambiguous.
    #     The test of "would a person flip it": does it decide something the
    #     switches around it do not already decide? If not, it leaves the screen
    #     and stays in this dict — the key must survive, or a tidy-up has
    #     silently become a feature deletion.
    #   • a plain numeric tuning value leaves the rendered Settings UI entirely.
    #     The test: expose what a user can *feel* — money, disk, wait time, a
    #     cadence you set up on purpose — and just decide the rest. "People one
    #     meaning search returns" is plumbing; it leaves. Its owning module
    #     already reads it live via `os.environ.get(KEY, default)`, so the
    #     environment path is unaffected — only the field, and usually this
    #     dict entry, goes. A dropped key is still settable by hand-editing
    #     secrets/.env; it just stops being a decision the app puts in front of
    #     anyone.
    #   • a few of those numbers are kept registered anyway, because each
    #     already failed to be invisible once and re-hiding it would relearn
    #     that lesson too. NC_ASK_FALLBACK_CAP is the page size of the one
    #     path that must work with zero API keys; it stays registered but
    #     UNrendered, the same "documented constant" shape NC_DATES_MAX_ROWS
    #     already used before NC-003. NC_DATES_LOOKAHEAD_DAYS stays RENDERED —
    #     NC-040 made it a visible field because it drives the real, visible
    #     strip above the People table (tests/test_birthdays.py asserts both
    #     the registry entry AND the rendered field). NC-364 rendered three
    #     more, all money: ENRICH_LLM_MAX (the most people one paid run may
    #     send) and the two prices below, on the AI row of the Settings page,
    #     beside the key and the model. NC_GMAIL_EVERY_HOURS and
    #     NC_LINKEDIN_EVERY_HOURS also stay registered, unrendered:
    #     tests/test_api_import.py already asserted both "must be tunable"
    #     before NC-003 existed, and dropping the registry entry would break
    #     that guarantee for no UI gain. (NC_CALENDAR_EVERY_HOURS and
    #     NC_AUTOSYNC_MINUTES — the same *kind* of "how often" knob — have no
    #     such test and do leave the registry; that asymmetry predates NC-003
    #     and was left as it was.)
    #
    # Every removed key is noted, tersely, near where it used to sit, so a search
    # for the env var name still finds why it isn't a field and where it lives.
    #
    # Which Gmail accounts to import, as `email` or `email:credentials_file`,
    # comma-separated. It is a setting rather than a table inside gmail_import.py,
    # so each user enters their own addresses in the app and nobody has to edit
    # Python inside a signed .app bundle.
    "GMAIL_ACCOUNTS":     {"secret": False, "default": ""},
    "NC_AUTOSYNC":        {"secret": False, "default": "1"},
    # auto-import a chat.db pasted into the iMessage import folder ("0" = no)
    "NC_IMSG_AUTOIMPORT": {"secret": False, "default": "1"},
    # The same switch for WhatsApp's copy. Read at api_import.py:939 in the same
    # loop as the iMessage one, but it was missing from this table (NC-004), so
    # one of two identical toggles was a field in Settings and the other could
    # only be set by editing the environment. A switch nobody can see is a switch
    # nobody can turn off.
    "NC_WA_AUTOIMPORT":   {"secret": False, "default": "1"},
    # NC-003 removed IMSG_GROUP_MAX and WA_GROUP_MAX (largest group chat, by
    # member count, that each import still reads — defaults 8 and 15). A
    # correctness knob almost nobody needs to touch, not a decision. Still read
    # live in imessage_import.group_max_members() and whatsapp_import's own
    # reader; raise either by hand-editing secrets/.env.
    #
    # The third of the three (NC-029). Apple Contacts moved only when a human
    # exported a .vcf and clicked; the FDA helper now copies the address book
    # itself, and this is the off switch for importing that copy.
    "NC_CONTACTS_AUTOIMPORT": {"secret": False, "default": "1"},
    # And the fourth (NC-185). The helper started copying Apple Calendar in
    # NC-042, after NC-004 had made the iMessage and WhatsApp switches a pair, and
    # this one was never registered — so the only one of the four automatic
    # imports you could not turn off from here.
    "NC_CALENDAR_LOCAL_AUTOIMPORT": {"secret": False, "default": "1"},
    # Fetch the link-fed calendars on a clock (NC-031). NC_ICS_FETCH is still the
    # master switch and still defaults to "0", so this does nothing at all until
    # calendar downloading is turned on and a link is pasted.
    "NC_CALENDAR_AUTO": {"secret": False, "default": "1"},
    # NC-003 removed NC_CALENDAR_EVERY_HOURS (hours between calendar-fetch
    # attempts, default 24) — grouped with the other "how often" knobs below.
    # Still read live via api_import.API_SOURCES[...]["hours_env"].
    #
    # The machine running the LinkedIn profile scraper, as user@host, and the SSH
    # key to reach it with (NC-006). No default on purpose: a built-in machine name
    # would fail to resolve on most installs, so the Data page's button would be
    # live and its error would name a computer the user had never heard of. Empty
    # is what lets the row show itself as not set up — see ImporterSpec.needs.
    "IMAC": {"secret": False, "default": ""},
    # NC-119. KEY stays UNMASKED, deliberately. It holds a path, and _check_key_path
    # below refuses anything that is not one — a value with a newline, a value that
    # is not an existing file, a value starting with "-". So there is nothing secret
    # in it to hide, and hiding it would cost the one thing this field is for: a
    # wrong path is the likeliest reason the scraper row fails, and a masked path
    # cannot be checked at a glance. Masking would also say "this is a secret" about
    # something that is not, which is the same lie the old label told.
    # If KEY ever leaves VALIDATORS, this decision has to be taken again.
    "KEY":  {"secret": False, "default": ""},
    # NC-003 removed NC_AUTOSYNC_MINUTES (minutes between background-sync
    # checks, default 2) — the same "how often" plumbing as the *_EVERY_HOURS
    # keys around it. Still read live in api_import.py's autosync loop.
    #
    # Gmail and LinkedIn need no file — they hold their own credentials, so they
    # sync on a clock. Hours between *attempts*, so an expired token backs off
    # instead of retrying every pass. Both are hitting someone else's
    # rate-limited API, which is why this isn't "every launch".
    "NC_GMAIL_AUTO":        {"secret": False, "default": "1"},
    # Kept registered, unrendered (see the NC-003 comment above):
    # tests/test_api_import.py already required this to be "tunable" — i.e.
    # in this dict — before NC-003 existed.
    "NC_GMAIL_EVERY_HOURS": {"secret": False, "default": "12"},
    "NC_LINKEDIN_AUTO":        {"secret": False, "default": "1"},
    # Same as NC_GMAIL_EVERY_HOURS just above.
    "NC_LINKEDIN_EVERY_HOURS": {"secret": False, "default": "24"},
    # NC-003 removed NC_IMPORT_TIMEOUT_SEC (seconds before one import
    # subprocess is killed, default 300) from the registry and the UI — still
    # read live by api_import.import_timeout_sec(); raise it by hand-editing
    # secrets/.env if a first import of a huge chat.db needs longer than that.
    #
    # NC_GMAIL_CONNECT_TIMEOUT_SEC (NC-184, added by NC-117): seconds a Gmail
    # sign-in may stay unfinished before the app stops waiting, default 600,
    # clamped 60s–1h, read live by api_gmail.connect_timeout_sec(). It is the
    # length of one person's trip through Google's consent screens, not a
    # machine limit — ten minutes covers a first visit to Google Cloud Console
    # with room to spare, and the panel prints the number it is using, so
    # raising it here is for somebody who wants longer, not somebody debugging.
    # Never registered above on purpose: a box for it would be a box you set
    # once and never look at, and it is not money, disk or a cadence.
    #
    # Ask's ceilings. A roster question ("50 people for the team offsite") needs a
    # much longer answer and more searching than "who do I know in London?", and
    # these were silent constants tuned for the short case: the answer was cut off
    # mid-list and returned as if finished. Raise MAX_TOKENS if your rosters still
    # get clipped; lower it to spend less per answer.
    #
    # NC-003 removed all seven of these from the registry and the UI —
    # NC_CHAT_MAX_TOKENS, NC_CHAT_TOOL_ROUNDS, NC_CHAT_SEARCH_LIMIT,
    # NC_CHAT_SEMANTIC_LIMIT, NC_CHAT_MAX_CARDS, NC_CHAT_HISTORY_CONTEXT and
    # NC_CHAT_TRANSCRIPT_MAX. None passes the test above (none is money,
    # disk, wait time, or a cadence the user set up); each is still read live,
    # clamped, in chat.py / api_ask.py exactly as before — raise or lower any of
    # them by hand-editing secrets/.env.
    #
    # People one ZERO-KEY answer returns. api_ask.ask_fallback_cap() has read this
    # at call time (clamped 1–500, default ASK_FALLBACK_CAP = 30) since the
    # rule-based engine got a People control, but the key was never registered
    # here — so the number the user chooses against could only be changed by
    # hand-editing secrets/.env. This is the path that runs with NO API key and
    # must keep working, so it is the one path that must not have an invisible
    # ceiling. NC-003 kept
    # this registered for exactly that reason, unlike its Ask siblings just
    # above — but it is not rendered either: raising it is still a hand-edit of
    # secrets/.env, the difference is only that GET/POST /api/settings still see
    # the key.
    "NC_ASK_FALLBACK_CAP":     {"secret": False, "default": "30"},
    # NC-003 removed NC_SEMANTIC_INSTALL_TIMEOUT_SEC (seconds the one-time
    # ~2 GB pip install may run, default 1800). Not rendered as a field in the
    # app. Still read live by api_instance.semantic_install_timeout_sec(); raise
    # it for a slow connection by hand-editing secrets/.env.
    #
    # Hard cap on people sent to a PAID fill-in run per press, so a single
    # click can never run away with spend: one paid request per person. Two
    # runs read it — Facts written by AI (enrich_facts.py --llm) and, since
    # NC-372, the shared history read from saved LinkedIn profiles
    # (enrich_li_extras.py). ENRICH_LLM_MAX is read first, ENRICH_LIMIT is the
    # fallback, then 20 (spend_cap() below, the same rule enrich_facts.py
    # follows). The free offline pass has no cap.
    #
    # They once did nothing from the UI: importers._cmd_facts_llm passed
    # `--limit 20` on the argv, which beat the env-derived default. The flag is
    # gone; see the note on that function.
    #
    # NC-364: it has a box now, on the same row as the Anthropic key (the AI
    # row, api_abilities._money_boxes), from SPEND_CAP_MIN to SPEND_CAP_MAX;
    # _check_spend_cap refuses anything else with a sentence.
    # NC-003 had kept it registered but unrendered, judging a box for a spend
    # cap nobody touches often to be clutter of its own; the rule since is
    # that money a person can feel gets a box. ENRICH_LIMIT, its legacy
    # fallback name, left the registry entirely — set ENRICH_LLM_MAX instead.
    "ENRICH_LLM_MAX":     {"secret": False, "default": "20"},
    # NC-195. What the app assumes Anthropic charges, in US dollars per million
    # tokens sent and received, when it shows what an Ask answer or an intake
    # proposal cost (chat.price_in / price_out, read at call time). If Anthropic's
    # prices change, or another model is chosen, every cost the app shows is
    # wrong until these are updated — which is why Ask prints the prices it
    # assumed beside the cost. They change only what the app SAYS a call cost,
    # never what Anthropic bills. NC-364: both have a box on the AI row of the
    # Settings page, from 0 to PRICE_MAX (_check_price).
    "NC_PRICE_IN_PER_MTOK":  {"secret": False, "default": "4"},
    "NC_PRICE_OUT_PER_MTOK": {"secret": False, "default": "20"},
    # run the free offline facts pass automatically after an import? ("0" = no)
    "NC_ENRICH_AUTO":     {"secret": False, "default": "1"},
    # May Intake fetch a web page? Off by default — it is one of the two things
    # in this app that reach the internet. It was absent from this table while
    # api_intake.py's own refusal message said "Set NC_INTAKE_WEB to 1 under
    # Settings", sending the user to look for a field that did not exist (NC-004).
    "NC_INTAKE_WEB":      {"secret": False, "default": "0"},
    # how many days ahead "This week" looks for birthdays/meetings. NC-003
    # left this one RENDERED, unlike every other numeric field — see the
    # NC-003 comment above for why (NC-040, tests/test_birthdays.py).
    # api_dates._dates_lookahead() reads it live.
    "NC_DATES_LOOKAHEAD_DAYS": {"secret": False, "default": "14"},
    # Rows one "upcoming dates" answer may carry. api_dates._week_row_limit()
    # reads it at call time (clamped 1–5000, default WEEK_ROW_LIMIT_DEFAULT = 400)
    # and it was never registered here, so it could not be set from Settings.
    # It matters because it interacts with the key above: db.upcoming_dates used
    # to stop at 50 rows with nothing on screen to say so, so a 365-day lookahead
    # or a calendar import that adds meetings in bulk hit a ceiling that looked
    # like missing data. Not rendered — a truncation ceiling with no visible
    # effect at 400 rows is a documented constant, not a Settings field. (This
    # was already the shape NC-003 later applied to the whole registry.)
    "NC_DATES_MAX_ROWS":       {"secret": False, "default": "400"},
    # may the scheduled LinkedIn scrape overwrite a field you hand-edited?
    # "0" (default) = your edit always wins; "1" = a newer scraped value wins
    "SCRAPE_OVERRIDES_MANUAL": {"secret": False, "default": "0"},
    # ── Writing-style profile (crm/style_profile.py) ─────────────────────────
    # A fully offline measurement of how the user writes, taken from the messages
    # they SENT, saved as one row in crm.db's `state` table. No API call, no
    # network, no write to any other table.
    #
    # NC-003 removed the five numbers below the two switches — NC_STYLE_MAX_
    # MESSAGES, NC_STYLE_MIN_CHARS, NC_STYLE_MIN_BUCKET, NC_STYLE_LANG_MARGIN,
    # NC_STYLE_TOP_N — from the registry and the UI: five numbers deciding which
    # of the user's own sent messages a purely offline measurement reads, none of
    # them a call a personal-CRM user should be asked to make. Each is still
    # read live, clamped, by its own named function in style_profile.py; change
    # one by hand-editing secrets/.env. The two ON/OFF switches immediately
    # below are NOT part of this cut — a switch stays visible regardless of
    # what numeric plumbing used to sit next to it.
    #
    # Include the LinkedIn corpus? Off, and this default is a correctness
    # decision rather than a preference: linkedin_import.py infers direction as
    # "the sender is not a known contact, therefore I sent it"
    # (linkedin_import.py:198-206), so recruiter mail would be measured as
    # the user's own writing. On is allowed, is recorded in the profile, and makes
    # the report say the numbers are contaminated by an unknown amount.
    "NC_STYLE_INCLUDE_LINKEDIN": {"secret": False, "default": "0"},
    # Include group-chat messages? Off for a register reason, not a trust one:
    # writing to a group is one-to-many and a bucket is about one person. On
    # WhatsApp, group rows are hardcoded inbound, so there this can only ever
    # admit other people's messages.
    "NC_STYLE_INCLUDE_GROUPS": {"secret": False, "default": "0"},
    # ── Calendar (.ics) import ───────────────────────────────────────────────
    # Google Calendar and Apple/iCloud both speak RFC 5545, so one parser
    # (crm/ics_import.py) serves both, fed three ways: these two URLs, or a file
    # you export by hand. Nothing here reads the OS calendar: these are the
    # routes that need no permission, which invariant 6 says every source keeps.
    # The calendar database itself is macOS-protected, and reading it is the sync
    # helper's job (the `calendar_local` row).
    #
    # BOTH URLS ARE SECRETS. A Google "Secret address in iCal format" and an
    # iCloud published-calendar link are bearer credentials: anyone holding one
    # can read that entire calendar, indefinitely, with no sign-in and no way to
    # tell it happened. So they are masked on read-back like ANTHROPIC_API_KEY
    # (read_all() never echoes a full secret) and must never reach a log line or
    # a URL query string. One wrinkle to know about: mask() shows the last 4
    # characters, and for a Google address those are always ".ics" — safe, but it
    # tells you nothing about *which* calendar. The "set" flag is what confirms
    # one is stored.
    "NC_ICS_GOOGLE_URL":   {"secret": True,  "default": ""},
    "NC_ICS_ICLOUD_URL":   {"secret": True,  "default": ""},
    # Master switch for downloading either of those. "0" (the default) means this
    # app makes no outbound request at all except Ask — see the egress note on
    # api_import.fetch_calendar_ics. A URL pasted above does precisely nothing
    # until this is "1"; the .ics FILE import ignores this switch, because a file
    # you exported yourself needs no network.
    "NC_ICS_FETCH":        {"secret": False, "default": "0"},
    # NC-003 removed the seven pure-tuning ICS numbers — NC_ICS_FETCH_TIMEOUT_SEC,
    # NC_ICS_MAX_KB, NC_ICS_MAX_ATTENDEES, NC_ICS_BACK_DAYS, NC_ICS_FORWARD_DAYS,
    # NC_ICS_LABEL_MAX, NC_ICS_TZ — from the registry and the UI. None is money,
    # disk, wait time or a cadence the user set up; each is a correctness detail
    # of the RFC 5545 parse. Every one is still read live, clamped, by its own
    # named function in ics_import.py; change one by hand-editing secrets/.env.
    # NC-096 removed the three cloud-sync keys — NC_SYNC_SERVER, NC_SYNC_TOKEN,
    # NC_SYNC_AUTO — with the sync subsystem they configured. Nothing read them
    # once cloud/ and crm/sync.py were deleted.
    # NC_BOT_USERNAME (the cloud bot's Telegram @username) was here until NC-196,
    # after the bot it named was removed, with nothing reading it. Same reason as
    # the bot's other keys above.
}


# ── NC-185: every other key the app reads ────────────────────────────────────
# MANAGED_KEYS above is what the Settings page may write. This is the rest: every
# key read anywhere in crm/, and what it does, so that the Settings page's own
# sentence — this file is the list of what each setting does — is true of all of
# them and not only the ones with a box. tests/test_every_setting_is_accounted_for.py
# fails on a key read in crm/ that is in neither table.
#
#   plumbing  decided, not exposed: where a file lives, how the server listens,
#             a flag one part of the app sets for another. Not a person's choice.
#   tunable   a limit someone can feel — money, waiting, how often, how much —
#             with no box on the Settings page. Set it by adding KEY=value to
#             secrets/.env. "Read at start" means a change needs a restart;
#             everything else applies on the next use.
#
# Each sentence names the function or module that reads the key, which is where
# its default and its allowed range live. Defaults are deliberately not copied
# here: a number written in two places is a number that goes stale in one.
UNMANAGED_KEYS: Dict[str, Tuple[str, str]] = {
    # ── Money: what one answer or one import can cost ─────────────────────────
    "SUMMARY_MODEL": ("tunable",
        "Which Claude model the email importer uses to summarise threads; each "
        "summary is a paid call (email_import.SUMMARY_MODEL, read at start)."),
    "ENRICH_LIMIT": ("tunable",
        "The older name for ENRICH_LLM_MAX, still honoured when that one is unset "
        "(enrich_facts.py)."),
    "NC_CHAT_MAX_TOKENS": ("tunable",
        "The longest answer Ask may write, in tokens, which caps what one answer "
        "can cost (chat.max_tokens)."),
    "NC_CHAT_BRIEF_MAX_TOKENS": ("tunable",
        "The same ceiling when you ask for a brief answer (chat.brief_max_tokens)."),
    "NC_CHAT_TOOL_ROUNDS": ("tunable",
        "How many lookups Ask may make before it has to answer: more finds more "
        "people and costs more (chat.max_tool_rounds)."),
    "NC_CHAT_QUICK_TOOL_ROUNDS": ("tunable",
        "The same for a quick search (chat.quick_tool_rounds)."),
    "NC_CHAT_SEARCH_LIMIT": ("tunable",
        "How many people one name-and-keyword search inside Ask returns "
        "(chat.search_people_limit)."),
    "NC_CHAT_QUICK_SEARCH_LIMIT": ("tunable",
        "The same for a quick search (chat.quick_search_limit)."),
    "NC_CHAT_SEMANTIC_LIMIT": ("tunable",
        "How many people one search-by-meaning inside Ask returns "
        "(chat.semantic_limit)."),
    "NC_CHAT_QUICK_SEMANTIC_LIMIT": ("tunable",
        "The same for a quick search (chat.quick_semantic_limit)."),
    "NC_CHAT_MESSAGE_LIMIT": ("tunable",
        "How many messages one search of your conversations inside Ask returns "
        "(chat.message_limit)."),
    "NC_CHAT_QUICK_MESSAGE_LIMIT": ("tunable",
        "The same for a quick search (chat.quick_message_limit)."),
    "NC_CHAT_MESSAGE_PEOPLE_LIMIT": ("tunable",
        "How many different people those messages may come from "
        "(chat.message_people_limit)."),
    "NC_CHAT_QUICK_MESSAGE_PEOPLE_LIMIT": ("tunable",
        "The same for a quick search (chat.quick_message_people_limit)."),
    "NC_CHAT_HISTORY_CONTEXT": ("tunable",
        "How many earlier turns of a conversation are sent with each new question; "
        "more remembers more and costs more (api_ask.py)."),
    "NC_CHAT_TRANSCRIPT_MAX": ("tunable",
        "How many turns of one Ask conversation are shown when you open it "
        "(api_ask.chat_transcript_max)."),
    "NC_CHAT_MAX_CARDS": ("tunable",
        "How many person cards can appear under one answer (api_ask.chat_max_cards)."),
    "NC_INTAKE_MAX_TOKENS": ("tunable",
        "The longest proposal the add-people-from-text feature may write, in tokens "
        "(intake.model_max_tokens)."),
    "NC_INTAKE_MAX_PEOPLE": ("tunable",
        "How many people one paste may propose (intake.batch_max_people)."),
    "NC_INTAKE_PASTE_MAX_CHARS": ("tunable",
        "The longest text you can paste into it, in characters "
        "(intake.paste_max_chars)."),
    "NC_INTAKE_WEB_MAX_LOOKUPS": ("tunable",
        "How many web pages it may look up per paste, when web lookup is on "
        "(intake.web_max_lookups)."),
    "NC_INTAKE_WEB_MAX_KB": ("tunable",
        "The most it reads of one web page, in kilobytes (intake.web_max_bytes)."),
    # ── Waiting: how long something may take before the app gives up ──────────
    "NC_IMPORT_TIMEOUT_SEC": ("tunable",
        "Seconds one import may run before it is stopped (api_import.import_timeout_sec)."),
    "NC_GMAIL_CONNECT_TIMEOUT_SEC": ("tunable",
        "Seconds the Gmail sign-in waits for you to finish in the browser "
        "(api_gmail.py)."),
    "NC_ICS_FETCH_TIMEOUT_SEC": ("tunable",
        "Seconds one calendar download may take (api_import.py)."),
    "NC_PICKED_FILE_WAIT_SEC": ("tunable",
        "Seconds the app waits for macOS to let it read a file you picked, such "
        "as a sign-in file in Downloads, before asking you to try again "
        "(api_gmail.picked_file_wait_sec)."),
    "NC_SOURCE_CHECK_CACHE_SEC": ("tunable",
        "Seconds the Data page may reuse its last answer to \"can each source "
        "collect right now?\" while nothing it depends on has changed; 0 asks "
        "every time (api_import.source_check_cache_sec)."),
    "NC_SEMANTIC_INSTALL_TIMEOUT_SEC": ("tunable",
        "Seconds the search-by-meaning install may take (api_instance.py)."),
    "NC_FDA_INSTALL_TIMEOUT": ("tunable",
        "Seconds the sync helper's installer may take (api_fda.py, read at start)."),
    "NC_FDA_RECHECK_TIMEOUT": ("tunable",
        "Seconds \"Check now\" waits for the helper to report back "
        "(api_fda.py, read at start)."),
    "NC_FDA_RECHECK_POLL": ("tunable",
        "Seconds between looks at the helper's report while waiting "
        "(api_fda.py, read at start)."),
    "NC_LI_TOKEN_TIMEOUT": ("tunable",
        "Seconds the LinkedIn token check may take (linkedin_token.py, read at start)."),
    "ENRICH_AFTER_IMPORT_SEC": ("tunable",
        "Seconds the free offline facts pass may run after an import "
        "(api_import.py, read at start)."),
    "NC_INTAKE_WEB_TIMEOUT_SEC": ("tunable",
        "Seconds one web lookup may take (intake.web_timeout_sec)."),
    # ── How often: cadences and "is this still working?" thresholds ───────────
    "NC_AUTOSYNC_MINUTES": ("tunable",
        "Minutes between the background checks for new data "
        "(api_import.autosync_interval_seconds)."),
    "NC_CALENDAR_EVERY_HOURS": ("tunable",
        "Hours between calendar downloads (api_import.API_SOURCES)."),
    "NC_FACTS_CHAIN_MIN_MINUTES": ("tunable",
        "The least time between two automatic facts passes after imports "
        "(api_import.py, read at start)."),
    "NC_STYLE_CHAIN_MIN_MINUTES": ("tunable",
        "The least time between two automatic rebuilds of your writing-style "
        "profile (api_import.py, read at start)."),
    "NC_LI_RESCRAPE_DAYS": ("tunable",
        "Days before a scraped LinkedIn profile is due to be scraped again. Here it "
        "changes what the Data page predicts; the scraper's own machine reads its "
        "own copy (db.py)."),
    "NC_LI_TOKEN_CHECK_HOURS": ("tunable",
        "Hours between checks that the LinkedIn token still works "
        "(linkedin_token.py, read at start)."),
    "NC_LI_TOKEN_WARN_DAYS": ("tunable",
        "Days before the LinkedIn token expires that the app starts warning "
        "(linkedin_token.py, read at start)."),
    "NC_LI_API_SILENT_DAYS": ("tunable",
        "Days without new LinkedIn messages before the app says collection may "
        "have stopped (api_linkedin.py, read at start)."),
    "NC_LI_SCRAPER_QUIET_HOURS": ("tunable",
        "Hours without a scraped profile before the app says the scraper is down "
        "(api_linkedin.py, read at start)."),
    "NC_FDA_STALE_HOURS": ("tunable",
        "Hours without a report before the app says the sync helper has gone quiet "
        "(api_fda.py, read at start)."),
    # The LinkedIn scraper's pacing, on the machine that runs it. Read each time
    # scrape_dispatch.py runs. These set how fast the scraper goes.
    "SCRAPE_ACTIVE_START": ("tunable", "The hour scraping may start each day (scrape_dispatch.py)."),
    "SCRAPE_ACTIVE_END": ("tunable", "The hour scraping stops each day (scrape_dispatch.py)."),
    "SCRAPE_DAILY_MIN": ("tunable", "The fewest profiles to aim for in a day (scrape_dispatch.py)."),
    "SCRAPE_DAILY_MAX": ("tunable", "The most profiles in a day (scrape_dispatch.py)."),
    "SCRAPE_SESSION_MIN": ("tunable", "The fewest profiles in one sitting (scrape_dispatch.py)."),
    "SCRAPE_SESSION_MAX": ("tunable", "The most profiles in one sitting (scrape_dispatch.py)."),
    "SCRAPE_GAP_MIN": ("tunable", "The shortest pause between two profiles, in seconds (scrape_dispatch.py)."),
    "SCRAPE_GAP_MAX": ("tunable", "The longest pause between two profiles, in seconds (scrape_dispatch.py)."),
    "SCRAPE_COOLDOWN_MIN": ("tunable",
        "The shortest pause after LinkedIn blocks the scraper with a check, in hours "
        "(scrape_dispatch.py)."),
    "SCRAPE_COOLDOWN_MAX": ("tunable",
        "The longest such pause, in hours (scrape_dispatch.py)."),
    # ── How much: what an import reads, and what the table sends ──────────────
    "IMSG_GROUP_MAX": ("tunable",
        "The largest iMessage group chat, in members other than you, that is still "
        "imported; 0 imports none (imessage_import.group_max_members)."),
    "WA_GROUP_MAX": ("tunable",
        "The same for WhatsApp (whatsapp_import.py)."),
    "NC_CONTACTS_PAGE": ("tunable",
        "How many people the People table loads before offering the rest "
        "(api_contacts.py, read at start)."),
    "NC_CONTACTS_MAX": ("tunable",
        "The most people it will send at once when you ask for all of them "
        "(api_contacts.py, read at start)."),
    "NC_FDA_OUTPUT_LINES": ("tunable",
        "How many lines of the helper installer's output the Data page keeps "
        "(api_fda.py, read at start)."),
    "NC_ICS_BACK_DAYS": ("tunable",
        "How far back a calendar event may be and still be imported, in days "
        "(ics_import.lookback_days)."),
    "NC_ICS_FORWARD_DAYS": ("tunable",
        "How far ahead, in days (ics_import.lookahead_days)."),
    "NC_ICS_MAX_ATTENDEES": ("tunable",
        "The most other people an event may have and still count as meeting them "
        "(ics_import.py)."),
    "NC_ICS_LABEL_MAX": ("tunable",
        "How many characters of an event's title are kept (ics_import.label_max)."),
    "NC_ICS_MAX_KB": ("tunable",
        "The largest calendar download accepted, in kilobytes (api_import.py)."),
    "NC_ICS_TZ": ("tunable",
        "The time zone for calendar events that name none, and the one that decides "
        "which day a time written in UTC falls on; empty means this Mac's own "
        "(ics_import.py)."),
    "NC_STYLE_MAX_MESSAGES": ("tunable",
        "How many of your messages the writing-style profile reads "
        "(style_profile.py)."),
    "NC_STYLE_MIN_CHARS": ("tunable",
        "The shortest message it counts, in characters (style_profile.py)."),
    "NC_STYLE_MIN_BUCKET": ("tunable",
        "The fewest messages a group needs before its own numbers are used; below "
        "it the profile falls back to all your messages (style_profile.min_bucket)."),
    "NC_STYLE_LANG_MARGIN": ("tunable",
        "How sure the language guess must be before a message is filed under a "
        "language, from 0 to 1 (style_profile.py)."),
    "NC_STYLE_TOP_N": ("tunable",
        "How many greetings, sign-offs, emoji and favourite words each group keeps "
        "(style_profile.top_n)."),
    # ── Plumbing ───────────────────────────────────────────────────────────────
    "CRM_DATA_DIR": ("plumbing",
        "The data folder. Set by the app; secrets/.env lives inside it, so it "
        "cannot be set from there (config.py)."),
    "CRM_SECRETS_DIR": ("plumbing",
        "The folder holding secrets/.env, set by the app for the same reason "
        "(config.py)."),
    "CRM_DB_PATH": ("plumbing", "Where crm.db lives, for tests; default the data folder (db.py)."),
    "ARCHIVE_DB_PATH": ("plumbing", "Where archive.db lives, for tests; default the data folder (archive.py)."),
    "CRM_PHOTOS_DIR": ("plumbing", "Where contact photos are kept, for tests (photos.py)."),
    "EXCLUDE_FILE": ("plumbing",
        "Where the list of people you excluded is kept, for tests; every importer "
        "reads the same file."),
    "CHAT_DB": ("plumbing",
        "Where the iMessage importer looks for chat.db when run by hand from a "
        "terminal (imessage_import.py)."),
    "NC_LI_SNAPSHOT": ("plumbing",
        "Where the LinkedIn message snapshot is read from, for tests and terminal "
        "runs (linkedin_snapshot_import.py)."),
    "APP_HOST": ("plumbing",
        "Which network address the server listens on. This Mac only by default; "
        "anything else is a deployment decision and needs APP_TOKEN (app.py)."),
    "APP_TOKEN": ("plumbing",
        "The password every request must carry when APP_HOST is not this Mac "
        "(app.py)."),
    "APP_PORT": ("plumbing", "The port the server listens on (app.py)."),
    "NC_INSTANCE": ("plumbing",
        "Set by the app when it starts its engine: a fingerprint of the code and "
        "data folders, reported back by /api/status (NC-202)."),
    "NC_BUILD": ("plumbing",
        "Set by the app when it starts its engine: which build of the bundle it "
        "is, reported by /api/status and shown in Settings (NC-203)."),
    "NC_NO_BROWSER": ("plumbing",
        "Stops the server opening a browser tab when it starts; the packaged app "
        "has its own window (app.py)."),
    "NC_UNATTENDED": ("plumbing",
        "Set by the app on the Gmail import it starts in the background, so that "
        "import never opens a sign-in window nobody asked for (gmail_import.py)."),
    "NC_IMESSAGE_SYNC_INTERVAL": ("plumbing",
        "Not read by this app: it is handed to the sync helper's installer, from "
        "the install request's own interval (api_fda.py)."),
    "NC_UPDATE_REPO": ("plumbing",
        "Which GitHub repository the update check reads. Empty means the update "
        "check never makes a request; it is set when a release is built "
        "(updates.py)."),
    "EMBED_MODEL": ("plumbing",
        "The on-device model that turns text into search-by-meaning vectors. "
        "Changing it makes every stored vector useless (search_cli.py)."),
    "FIREFOX_BIN": ("plumbing",
        "Where the LinkedIn scraper finds Firefox, on the machine that runs it "
        "(scrape_linkedin_profiles.py)."),
    "GECKODRIVER_PATH": ("plumbing",
        "Where it finds Firefox's driver; empty lets Selenium find it itself "
        "(scrape_linkedin_profiles.py)."),
}

def mask(value: str) -> str:
    """Last 4 chars only, prefixed — or "" if there's nothing to show."""
    value = value or ""
    return (MASK_PREFIX + value[-4:]) if value else ""


def env_path() -> str:
    return os.path.join(config.SECRETS_DIR, ".env")


def read_all() -> List[Dict[str, Any]]:
    """Current value of every managed key, read from os.environ (the live
    process state — seeded from secrets/.env at startup, kept in sync by
    write_key() on every save). Secrets are masked; unset plain fields fall
    back to their documented default so the UI shows something sensible."""
    out = []
    for key, meta in MANAGED_KEYS.items():
        raw = os.environ.get(key, "") or ""
        is_set = bool(raw)
        if meta["secret"]:
            value = mask(raw)
        else:
            value = raw if is_set else meta["default"]
        out.append({"key": key, "value": value, "secret": meta["secret"], "set": is_set})
    return out


# ── What a value is allowed to be ───────────────────────────────────────────
# NC-149. Two of these settings are not data: they become arguments to ssh, scp
# and rsync. `write_key` already refused embedded newlines, which stops a value
# smuggling a second KEY=... line into .env — but a single line is plenty to
# turn "can change Settings" into "can run commands as whoever runs the app",
# because a value starting with "-" is read by ssh as an OPTION, and
# `-oProxyCommand=...` runs through /bin/sh: running
# `ssh -oProxyCommand='touch /tmp/x' true` creates /tmp/x.
#
# The call sites now pass `--` before the host, which is the real fix and holds
# even for a value that never came through here. This is the second wall, and
# the one that gives a person a sentence instead of a mystery.

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
# Everything a shell or rsync's own -e splitter would read as syntax. A SPACE is
# deliberately NOT here: "~/Library/Application Support/…" is an ordinary place
# to keep a key, and refusing it would be refusing the commonest real path on
# this platform. The call site quotes it instead.
_KEY_FORBIDDEN = set("'\"\\`$;&|<>*?()[]{}!\n\r\t")


def _check_host(value: str) -> None:
    if not _HOST_RE.match(value):
        raise ValueError(
            "The scraper machine is written as user@host or just host — letters, "
            "digits, dots, dashes and underscores. A name that resolves from "
            "any network, like 'you@your-mac', is the usual form. A value "
            "starting with '-' is refused because ssh would read it as an "
            "option, not a computer.")


def _check_key_path(value: str) -> None:
    if value.startswith("-"):
        raise ValueError(
            "An SSH key is a path to a file, and a path starting with '-' is "
            "read by ssh as an option instead. Give the full path, for example "
            "~/.ssh/id_ed25519.")
    bad = sorted(_KEY_FORBIDDEN & set(value))
    if bad:
        raise ValueError(
            "The key path contains " + " ".join(repr(c) for c in bad) +
            ", which rsync and ssh read as syntax rather than as part of a "
            "filename. Spaces are fine; quotes and shell punctuation are not. "
            "Move the key somewhere with a plainer name.")
    if not os.path.exists(os.path.expanduser(value)):
        raise ValueError(
            f"There is no file at {value}. Point this at the private key that "
            "reaches the scraper machine — the one whose .pub is in that "
            "machine's ~/.ssh/authorized_keys.")


# NC-364. The bounds the AI row of the Settings page states beside each box, kept
# here with the checks that enforce them so the sentence and the refusal cannot
# disagree. PRICE_MAX matches chat.PRICE_MAX, the ceiling the cost display
# itself ignores a price above.
PRICE_MAX = 1000
SPEND_CAP_DEFAULT = int(MANAGED_KEYS["ENRICH_LLM_MAX"]["default"])
SPEND_CAP_MIN = 1
SPEND_CAP_MAX = 1000


def _check_price(value: str) -> None:
    """NC-195: a price is a number of US dollars, and chat._price ignores anything
    else — so refuse it here rather than store a value that silently does nothing."""
    try:
        ok = 0 <= float(value) <= PRICE_MAX
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"A price is US dollars per million tokens, as a number "
                         f"from 0 to {PRICE_MAX:,}. For example 4 or 3.75.")


def _check_spend_cap(value: str) -> None:
    """NC-364: the most people one paid run may send to Anthropic. A whole number
    inside the bounds, or a sentence saying what is wanted, never a stored value
    the runs would quietly read as the default instead."""
    ok = value.isdigit() and SPEND_CAP_MIN <= int(value) <= SPEND_CAP_MAX
    if not ok:
        raise ValueError(f"Type a whole number of people from {SPEND_CAP_MIN} to "
                         f"{SPEND_CAP_MAX:,}. For example {SPEND_CAP_DEFAULT}.")


def spend_cap() -> int:
    """The most people one paid fill-in run may send to Anthropic, read at call
    time: ENRICH_LLM_MAX, then its older name ENRICH_LIMIT, then the default.

    The same reading enrich_facts.main() does for its --limit default (a whole
    number above zero wins), so the number a page prints is the number a run
    uses. A hand-edited value above SPEND_CAP_MAX is honoured as written there,
    so it is honoured here too: the bounds are what the box accepts."""
    for raw in (os.environ.get("ENRICH_LLM_MAX"), os.environ.get("ENRICH_LIMIT")):
        raw = (raw or "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    return SPEND_CAP_DEFAULT


VALIDATORS = {"IMAC": _check_host, "KEY": _check_key_path,
              "NC_PRICE_IN_PER_MTOK": _check_price,
              "NC_PRICE_OUT_PER_MTOK": _check_price,
              "ENRICH_LLM_MAX": _check_spend_cap}


def _read_lines(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return fh.readlines()


def write_key(key: str, value: str) -> bool:
    """Read-modify-write secrets/.env for a single key, preserving every other
    line (including comments) verbatim. An empty value removes the line
    entirely (and clears it from os.environ) rather than writing `KEY=`.

    Returns whether the file ended up owner-only. The setting is saved either
    way — losing a key over a permission bit would be the worse failure — but a
    False here is something the person is TOLD, not something written to a log
    nobody reads (NC-150).
    """
    if key not in MANAGED_KEYS:
        raise ValueError(f"'{key}' is not a managed setting")
    # one value = one line: strip whitespace and refuse embedded newlines so a
    # crafted value can never smuggle extra KEY=... lines into the file
    value = (value or "").strip()
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    # Empty means "remove this setting", which is always allowed — a value that
    # is not there cannot be misread by anything.
    if value and key in VALIDATORS:
        VALIDATORS[key](value)

    path = env_path()
    os.makedirs(config.SECRETS_DIR, mode=0o700, exist_ok=True)
    lines = _read_lines(path)

    prefix = key + "="
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(prefix):
            found = True
            if value:
                new_lines.append(f"{key}={value}\n")
            # else: drop the line (empty value = remove)
            continue
        new_lines.append(line)

    if not found and value:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(new_lines)
    # 0600, because this file holds API keys and open() leaves it 0644 —
    # world-readable, with only the 0700 directory standing between a key and
    # any other account on the Mac. One rule, one place: config.harden owns it
    # and carries the reasoning (NC-147). A read-only or exotic filesystem must
    # not break saving a setting, so the result is REPORTED rather than raised.
    secured = config.harden(path)

    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)
    return secured
