# Architecture

A written map of how Network CRM is built — the data model, the importer
contract, the Ask chatbot's tool-use design, the deployment shapes and their
trust boundaries, and how to extend it. Written so a developer (or an AI model
helping one) can understand and safely change the codebase without reverse-
engineering it first.

---

## 1. Shape of the system

Two SQLite databases, one small web server, and a set of one-shot importer
scripts. No web framework, no ORM, no build step — the backend
is Python standard library (`http.server`) over SQLite in WAL mode. The front-end
is Preact + htm loaded as native ES modules (`static/vendor/preact-htm.module.js`,
~13 KB vendored — no npm, no CDN, still zero toolchain), organized as one module
per surface under `static/app/`: four nav tabs — **People** (list + person page
with story-so-far and an episode timeline; the landing page),
**Ask** (LLM chat), **Lists** (named groups, with CSV export) and **Data**
(imports/enrich/export/duplicates) — plus **Settings** (keys, diagnostics, opt-in
installs) behind its own icon and a first-run **Onboarding** overlay. A hash router
and an omnibox (names jump, questions ask, commands run) bind them together; design
tokens in `static/css/tokens.css` define the warm light theme and a dark theme.

```
                 ┌───────────────────────── your machine ─────────────────────────┐
  importers  →   │  archive.db  (raw messages + FTS + embedding chunks — local only)│
 (one-shot)  →   │  crm.db      (contacts, notes, identifiers, chat log)            │
                 │      ▲            ▲
                 │      │            │
   app.py  ──────┼──────┘   search_cli.py (keyword + semantic)
 (HTTP transport)│          chat.py (Ask: LLM tool-use)
   └─ api_*.py   │
  (all the logic)│
                 └── nothing leaves this Mac unless you turn it on (§7)
```

**Why two databases.** `crm.db` is the durable, curated layer: who you know and
what you've written. It is tiny (< a few MB) and is the only one `export_data.py`
copies. `archive.db` is the bulk layer: every message body, an FTS5 index, and
embedding chunks. It can be tens of MB to gigabytes and **never leaves this Mac**
(invariant 7). Keeping them separate is what makes that boundary a fact about
which file gets copied rather than a rule someone has to remember.

### The backend's one internal seam: transport vs. `api_*` modules

`crm/app.py` is **transport only** (~1,050 lines): the `CrmHandler` request class,
the three route tables (`_route_get`, `_route_write`, `_dispatch_contact_post`),
`handle_status`, `main()`, and a block that re-exports every handler it routes to.
It used to be ~3,170 lines holding all of the above *plus* every handler; the
business logic now lives in 25 flat sibling modules, `crm/api_<domain>.py`
(~11,400 lines in total). The line counts below are rounded, and
`tests/test_the_architecture_is_current.py` fails when one drifts by more than a
quarter:

| Module | ~Lines | Owns |
|---|---|---|
| `api_core.py` | 20 | `ApiError`, and nothing else. A leaf — it imports nothing from `crm/`, so it can never join an import cycle. |
| `api_guards.py` | 50 | `_require_contact` / `_require_list`: the lookups that 404 instead of returning `None`. |
| `api_serialize.py` | 300 | The shared spine — `_contact_summary` (the card shape) and `_contact_detail` (the person page), plus `_story_text`, `_next_action`, `_brief_meta`. |
| `api_contacts.py` | 650 | The aggregate root: listing, filters, facets, every edit. Sole owner of `EXCLUDE_FILE`, the same file the importers honour. |
| `api_import.py` | 1,800 | The import/enrich endpoints and the background auto-sync loop. Owns `PICK_SCRIPTS`. |
| `api_instance.py` | 650 | This install rather than the network it holds: settings, diagnostics, onboarding, update check, `deps_status`. |
| `api_ask.py` | 1,300 | Both Ask halves — the rule-based engine *and* the LLM chat path (see §5). |
| `api_messages.py` | 450 | Every archive.db read for the UI: history, episodes, conversation search. Takes no crm.db connection. |
| `api_views.py` | 85 | View compositions only — the describe queue for onboarding. The churn zone (see below). |
| `api_dates.py` | 380 | Contact dates (birthdays, anniversaries, meetings), recurrence resolution and the phrasing around them. |
| `api_lists.py` | 180 | The list handlers and `_list_export_csv`, which returns `(csv_text, list_name)` rather than an HTTP response so `app.py` keeps the header-writing. |
| `api_briefs.py` | 200 | The AI brief and the draft message. Optional: 400s without a key, and the detail page still works. |
| `api_notes.py` | 100 | The append-only note timeline and the writing streak. |
| `api_duplicates.py` | 60 | Detection and merge, with its own `_dup_member` shape. |
| `api_identifiers.py` | 40 | Phones/emails/socials — the keys importers match people on. |
| `api_ledger.py` | 1,750 | The Data page in one answer: every source row, the **Needs you** list above them, and the figures for **This install**. |
| `api_abilities.py` | 350 | The Settings page in one answer: the things the app can do once you give it something, and what this install is. |
| `api_remove.py` | 240 | Settings' **Remove everything**: turns the copying helper off first, then deletes only the app's own data and log folders, and refuses anything else (NC-314). |
| `api_sources.py` | 320 | One row per import source: how it runs, whether it is connected, when it last ran. |
| `api_provenance.py` | 600 | What one import source actually put in the database. |
| `api_setup.py` | 400 | Setup progress: what each first-run step has given the user so far. |
| `api_intake.py` | 750 | The paste reader's endpoints (§10): its two ways out to the internet, and review before anything is saved. |
| `api_fda.py` | 450 | The Full Disk Access flow for the sync helper, explained in the app's own window. |
| `api_gmail.py` | 560 | Gmail's setup, done from the screen. |
| `api_linkedin.py` | 550 | LinkedIn's three routes (the export, the API, the scraper) as one status. |

Three constraints hold this shape together. Each is load-bearing rather than
stylistic, and each has already cost a real bug.

**Flat siblings, never a `crm/api/` subpackage.** There is no `crm/__init__.py`,
and the modules import each other flat (`import db`, `from api_core import
ApiError`). `python3 crm/app.py` puts `crm/` on `sys.path[0]`, so every module is
top-level and a sibling's `import db` resolves to the *same* module object
`app.py` holds — which is what makes the tests' `app.db` / `app.arc` /
`app.subprocess` patching work at all. The packaged app starts the engine exactly
the same way (`python crm/app.py --socket …`), so the flat form is what ships. Converting to
`from crm import db` would give importers a second copy of each module and break
both. Related: when a symbol leaves `app.py` it must be **re-exported** there —
the route tables call handlers by bare name and the tests reach them as `app.X`.

**`api_views.py` is deliberately disposable.** The UI has been redesigned more
than once, and each redesign rewrites what a screen *shows* without changing what
a contact *is*. So that module holds compositions only: no SQL, no serialization,
no archive opens, no JSON parsing — every read goes through `api_contacts` /
`api_notes` / `api_messages` / `api_dates` / `api_serialize`. The next redesign
should be able to delete the file and write a new one with nothing underneath it
moving. The gate that keeps it honest should match nothing except the docstring
line that states the rule:

```bash
grep -nE 'conn\.execute|SELECT |photos\.photo_url|json\.loads|arc\.get_archive_db|open_if_present' crm/api_views.py
```

If you need a query in there, push it down into the module that owns that table —
`api_contacts.describe_candidates` and `api_notes.recent_happenings` are the
examples to copy.

**`db.py` was deliberately *not* split**, even though at ~2,800 lines it is the
largest file in `crm/` apart from `chat.py`. `db.get_db` reads the `DB_PATH` module global at call
time, and ~20 tests rebind `db.DB_PATH` to a temp file. Behind a package
re-export those rebinds would silently miss, and the suite would keep passing
while writing synthetic contacts into the real `data/crm.db`. The
same reasoning kills any future "tidy this into a package" idea for `db.py`.

---

## 2. Data model

### `crm.db` (see `crm/db.py`)

**`contacts`** — one row per person. Beyond `name`/`company`/`position`/`category`
it carries:

- **Per-channel activity**, as a `(count, last-seen)` pair per channel:
  `msg_count`/`last_contact` (LinkedIn), `imsg_count`/`imsg_last`,
  `wa_count`/`wa_last`, `em_*`, `tg_*`, `msgr_*`, `ig_*`. The first three pairs
  are recounted from `archive.db` for everyone (`engagement.recount_channels`),
  so they are the messages the app can show; the other channels' importers
  write theirs. The strength score and dashboard read them.
- **Facts layer** (enrichment): `home_city`, `home_country`, `home_metro`,
  `topics` (JSON array), `role_type`, `blurb`, and `facts_source`
  (`''` | `offline` | `message_scan` | `linkedin_csv` | `linkedin_scrape` |
  `llm` | `manual`) — `facts_source` guards higher-quality values (manual, LLM,
  profile-scrape) from being overwritten by later rule-based passes; `manual`
  outranks everything, including the scraper merge.
- **Review state**: `status` (`new`/`described`/`skipped`/…), `excluded`,
  `times_reviewed`, `source`.

**`notes`** — append-only, timestamped notes per contact (`kind` =
`note`/`how_we_met`/`voice`/`auto`). A profile is a *timeline*, not one field that
goes stale.

**`identifiers`** — phones/emails/socials per contact, used for cross-source
matching and duplicate detection.

**`chat_history`** — the Ask view's conversation log (`role`, `content`,
`created_at`, `contacts_json`); persisted so the transcript — including the
contact cards under each assistant reply — survives a reload.

**Photos** — `photo` + `photo_source` columns point at `data/photos/<id>.jpg`
(gitignored, ≤512px). `crm/photos.py` enforces a source-priority guard:
`manual > linkedin > contacts (vCard) > whatsapp/google` — an import never
clobbers a photo you set yourself, and below that the LinkedIn portrait wins,
because it is the picture the person chose to be recognised by. Sources: vCard `PHOTO` data
(contacts_import), WhatsApp Desktop's cached avatars (`crm/whatsapp_photos.py`,
matched via ChatStorage chat names or phone identifiers; groups and unmatched
anonymized `@lid` ids are skipped), and manual upload (`POST
/api/contact/{id}/photo`). Served at `GET /api/photo/{id}?v=<mtime>`.

**`brief` / `brief_updated_at`** — the cached "story so far" paragraph. Every
contact detail includes a `story`: the saved brief when present, else an honest
template composed from real fields (`_story_text` in `crm/api_serialize.py`). `POST
/api/contact/{id}/brief` regenerates it with Claude (needs `ANTHROPIC_API_KEY`;
returns 400 without one, and the template keeps working).

**`state`**, **`allowlist`** — a key/value scratchpad (import watermarks, the
onboarding-done flag) and per-user allowlisting.

New columns are added idempotently by `db.ensure_columns()` (a lightweight
migration run on every `init_db`), so upgrading never requires a manual migration.

### `archive.db` (see `crm/archive.py`)

- **`messages`** — one row per archived message: `contact_id`, `channel`,
  `direction` (1 = sent), `ts`, `subject`, `body`. A dedupe key makes inserts
  idempotent, so re-importing a source never doubles rows.
- **`messages_fts`** — FTS5 virtual table over message bodies for keyword search.
- **`chunks`** — conversation slices with a content hash, embedded for semantic
  search over what people actually *said* (not just their contact card).

### Familiarity / relationship strength (`db.relationship_strength`)

A 0–100 score with a multiplicative shape:

```
familiarity = 100 · bond · (0.55 + 0.45 · warmth)
```

**bond** (slow-moving, "how much relationship exists"): weighted blend of
*shared days* (distinct calendar days with ≥1 message — the dominant signal,
log-scaled to a year's worth), *tenure* (first→last touchpoint, 3y max),
*volume* (log-scaled) and *depth* (described / how-we-met / summary) — then
multiplied by *reciprocity* (min/max of sent vs received; a one-way stream is
crushed to 0.35×; unknown stays neutral) and a small multi-channel bonus.

**warmth** (current temperature): recency measured against the pair's own
rhythm — expected gap ≈ 4× their average interval between conversation-days
(clamped 21–240d), halving every 2.5 expected-gaps beyond it. Warmth only
modulates bond (floor 0.55): silence cools a relationship but never erases
history, and a stranger who messaged yesterday can't ride recency to a high
score.

The interaction features (`eng_active_days`, `eng_sent/recv`, `eng_first/last`,
`eng_recent90`) are aggregated from archive.db by **`crm/engagement.py`** in one
GROUP BY pass — refreshed at app startup, after every import, and on demand via
the "Recompute closeness" enrich card. Contacts with channel counts but no
archived text fall back to count-based estimates. `strength_label` buckets:
close ≥65, active ≥40, loose ≥18, faint below.

---

## 3. The importer contract

Every source is one entry in **`crm/importers.py`**'s `REGISTRY`, an
`ImporterSpec`:

```python
@dataclass
class ImporterSpec:
    key: str          # "imessage", "linkedin", "gmail", "facts", …
    label: str        # human label shown on the Import card
    category: str     # "import" | "enrich"
    kind: str         # "file" | "folder" | "none"  (what the picker needs)
    pick_kind: str | None   # native-picker prompt key, or None
    export_help: str  # the step-by-step wizard copy (markdown-lite)
    build_cmd: Callable[[dict], list[str]]   # (form) -> argv for the underlying script
```

Nothing in the API layer hardcodes a source list. `GET /api/import/sources`
returns the registry, the Import wizard renders from it, and `POST
/api/import/run` looks up
`REGISTRY[source].build_cmd(body)` and runs that argv as a subprocess, streaming
stdout+stderr back to the UI. Adding a source to the registry makes it appear in
the UI automatically.

**Each underlying importer** (`crm/*_import.py`) follows the same discipline:

1. Read a file *you* exported/copied (no privileged OS access), or an API you
   authorized (Gmail).
2. Match people to existing `crm.db` contacts by name-norm, phone (last-9-digits),
   or email — **only enrich people already in your CRM** unless `--create-new`.
3. Count only genuine **two-way** conversations; skip `exclude_list.txt`
   names. Small group chats are archived for people already in the CRM.
4. Write message bodies to `archive.db` (idempotent via the dedupe key). For
   WhatsApp, iMessage and LinkedIn that is all: each person's `(count, last)` is
   recounted from `archive.db` afterwards (`engagement.recount_channels`), so it
   is the messages the app can show. The other channels still write their own
   `(count, last)` to `crm.db`.
5. Support `--dry-run`.

`imessage_import.py` is the reference implementation. iMessage/WhatsApp also
demonstrate the **no-Full-Disk-Access** pattern: the user copies the sqlite file
themselves and passes the copy, so the app never gets blanket access to protected
folders. `imessage_import.decode_attributed_body` additionally decodes both the
legacy bplist and the macOS-13+ `streamtyped` (NSArchiver) `attributedBody`
formats so modern iMessage text is captured.

### Location integration contract

The facts columns `home_city`/`home_metro`/`home_country` are written through one
function: `db.set_field(conn, contact_id, "home_metro", value)`, which stamps
`facts_source='manual'`. The UI's contact editor uses it, and any future location
scraper should too — that's the entire integration surface, no schema change
needed.

---

## 4. Search

`crm/search_cli.py` provides both modes, and both degrade gracefully if
`sentence-transformers`/`numpy` aren't installed (semantic simply returns
"unavailable"):

- **Keyword** — SQL `LIKE` over contacts, FTS5 over `archive.db` message bodies.
- **Semantic** — `all-MiniLM-L6-v2` embeddings over contact profile text
  (`semantic_rank`) and over conversation chunks (`chunk_semantic_rank`). Vectors
  are cached to `.npz` next to the DB and invalidated by a signature
  (`MAX(updated_at) || COUNT(*)`) / content hash, so only changed rows re-embed.

The People view calls keyword and semantic in parallel and merges by contact id
(keyword hits first, semantic score breaks ties, semantic-only hits tagged) — the
user sees one search box, not a mode toggle.

Two endpoints feed the person-level views. `GET /api/describe-queue` returns a
few undescribed people ranked by relationship strength × recency, each with their
last archived messages as the memory trigger (§6). `GET /api/contact/{id}/episodes` groups archived messages into
conversation episodes (same channel, gaps under
4h), newest first, paginated via `next_before`; small episodes inline their
messages, larger ones expand lazily through `/history`.

---

## 5. The Ask chatbot (LLM tool-use)

`crm/chat.py` implements a standard Anthropic tool-use loop:

- **`TOOLS`** — eleven tools the model can call:
  - finding people: `find_person` (by name, ranked by how sure the match is and
    then by how much history there is), `search_people` (location / role / topic /
    minimum-strength filters), `semantic_search` (by description, reusing
    `search_cli.semantic_rank`), `get_contact` (notes and how you met), and
    `mutual_connections` (LinkedIn's shared connections for one person);
  - reading messages: `message_stats` (counts first, so the model knows what
    exists), `search_conversations` (conversation pieces), `search_messages` (single
    messages by phrase, date and direction, with the ones either side),
    `people_in_messages` (the same search grouped by person), and `read_message`
    (one message in full);
  - `ask_the_user`, which ends the turn with a question when the one asked has
    more than one real reading.
- **`run_tool(conn, name, input)`** — dispatches a tool call to real SQL /
  embedding queries and returns JSON.
- **`chat(conn, messages)`** — runs `messages.create` with the tools and a system
  prompt describing the CRM, executes each `tool_use` against `run_tool`, and
  loops until the model returns a final answer. Returns `{reply, contacts}` where
  `contacts` are collected from the tool results so the UI can render cards.
  Prompt caching (`cache_control`) is set on the system prompt + tools block,
  which render before the volatile message history.

`handle_chat` in **`crm/api_ask.py`** serves `POST /api/chat`: it loads recent
`chat_history`, calls `chat.chat`, and persists both turns. **Fallback:** if
`ANTHROPIC_API_KEY` is unset (or the API errors), it falls back to the rule-based
`handle_ask` engine in the same `{reply, contacts}` shape. The app therefore
answers with zero API keys; the LLM is a pure upgrade. Both halves live in
`api_ask.py` on purpose, so the whole fallback chain is readable in one file.

The default model is `claude-opus-5-5` (Opus 5.5), overridable via `ANTHROPIC_MODEL`.

---

## 6. The describe queue — how the CRM fills itself in

An import gives you names. What makes the network usable is one sentence per
person in your own words, and nobody sits down to write thousands of them. So the app
asks about a few people at a time.

The one place that asks is onboarding's **Remember three people** step (People is
the landing page, so there is no daily prompt screen). It reads
`GET /api/describe-queue`, which `crm/api_views.py` composes:
`api_contacts.describe_candidates` returns every visible contact with
`status = 'new'`, ranked by `db.relationship_strength` then recency, because the
people you talk to most are both the ones worth remembering and the easiest to
say something about. If that pool has fewer than `DESCRIBE_QUEUE_SIZE` (3),
`db.pick_review_batch` tops it up with the classic rotation. Each entry carries
the person page's fields and their last few messages as a memory trigger.

Answering posts a note (`POST /api/contact/{id}/note`), which also flips `status`
to `described`, so the person is not asked about again. After onboarding, each
person's page has an editable **How you met** field and the note timeline.

> **Why there are no phone bots.** An earlier design had a Telegram bot, a WhatsApp
> bridge and a shared core DM this same question and save the reply as a note,
> with a hosted tier carrying the multi-tenant webhook and pairing-code half. All of
> it is gone. It cost every user a bot token, a running process and a pairing step
> before they saw any value, so the question is asked inside the app instead — the
> describe queue above. **Importing** a Telegram export is unaffected:
> `crm/telegram_import.py` is stdlib-only.

---

## 7. Deployment shapes & trust boundaries

**The packaged app opens no network address at all (NC-330, 2026-10-17).** It
starts the engine with `--socket <file>`, and the engine answers only on that
Unix socket file (`PrivateSocketServer` in `app.py`): permissions 600, in a folder
with permissions 700, in the app's own Application Support. So no browser, no
web page and no other account on the Mac can reach it; `lsof -iTCP` shows nothing
from Network CRM. The rest of this section is about running the engine from
source, where it does listen on a port.

From source, `app.py` binds to `APP_HOST` (default `127.0.0.1`). Off-loopback, every `/api`
route requires `APP_TOKEN` (`Authorization: Bearer` header or `?token=` query
param); the page and static assets stay open so the browser can load and then
supply the token. Loopback mode enforces nothing, and needs no token.

**Off-loopback with no token set is refused, not permitted** (NC-140). It used to
serve anyway and print a warning, which put the only notice on a terminal the
packaged `.app` does not have — so following the home-server recipe below and
missing the token line published the whole CRM to the Wi-Fi, silently.
`_open_lan_refusal` in `app.py` now stops `main()` before it touches the
database, starts the autosync thread, or opens the socket, and says which of the
two settings to change.

| Shape | Bind | What leaves the machine |
|---|---|---|
| **The app** | a private socket file, no port | Nothing, until you turn one of the five things below on. |
| **A. Local, from source** | `127.0.0.1` | The same as the app. |
| **B. Home server** | `0.0.0.0` + token, on your LAN | The CRM itself is reachable only on your home network. The same five opt-ins still reach their services from the server. |

What leaves the machine, and only once you turn it on:

- **An Anthropic key.** Every AI feature sends Anthropic what it works from, each
  time it runs:
  - **Ask** (`chat.py`): your question and whatever its tools return, which is contact fields
    and notes, and, from `search_messages` and `people_in_messages`, the text of
    matching messages and the ones either side (NC-241).
  - **The AI summary and the draft message** (`api_briefs.py`): that person's
    details, up to six of your notes on them, and a snippet of each recent
    conversation.
  - **The paste reader's Propose** (`api_intake.py`): the pasted text, plus any
    page text you read for it.
  - Passes that run only once you start or set them up: **Fill facts with AI**
    (`enrich_facts.py --llm`, a Data page card) sends each person's profile
    fields and up to five recent messages they sent you, each cut to 120
    characters. `email_import.py --summarize`, from the command line, sends the
    subject lines of your emails with each person.
    The LinkedIn scraper (`scrape_linkedin_profiles.py`, from the machine it
    runs on), the extras step of `deploy/pull_and_merge.sh`, and the Data page's
    **Read shared history** button on the LinkedIn profile facts row (all three
    run `enrich_li_extras.py`) send the scraped profile text: from the button, at
    most the Settings spend cap of profiles per press (NC-372).
- **Link reading in the paste reader.** Off unless `NC_INTAKE_WEB=1`. Then each
  click on **Read this page** fetches one address that appears in your paste.
- **Connecting LinkedIn, Google or a calendar feed.** The app asks that service
  for your own data from it.
- **Exporting.** What you export goes wherever you put it.
- **The update check** (`updates.py`). Off while `NC_UPDATE_REPO` is empty, which
  it is unless a release build sets it. Then it asks GitHub's releases address
  for that repository once a day. The request carries no data of yours, only
  what any web request carries: the Mac's network address.

Semantic search is local, but installing it and its first run download the
packages (about 2 GB, since they bring in PyTorch) and then the model (about
90 MB, from Hugging Face). No data of yours goes with either download.
Everything else — imports, enrichment, embeddings, search,
the archive — is local under every shape.

### The packaged Mac app — shape A, and the actual product

Shape A is not a development convenience; it is what ships. **`Network CRM.app`
is the product** — it is built for people who have no checkout, no Terminal, and
no Python. `packaging/appify.sh` builds it and
`packaging/README.md` is the full reference; the parts that constrain the
architecture are these:

- **The window is the app, and it owns its engine (NC-330).** `Contents/MacOS/NetworkCRM`
  is the SwiftUI program built from `native/Sources`. It starts `crm/app.py
  --socket …/run/engine.sock --watch-stdin` as its own child, on the bundled
  Python, and talks to it with a small HTTP client over that socket
  (`native/Sources/PrivateConnection.swift`). The engine watches a pipe only the
  app holds, so when the app goes — even force-quit — the engine sees the end of
  its input and stops, and it stops everything it started (it leads its own
  process group). There is no launcher script, no web page in the window and no
  Quit app any more. `native/Sources/Engine.swift` has the details.
- **The app carries its own Python, and uses no other.** `packaging/appify.sh
  --embed` puts a relocatable
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
  CPython in `Contents/Resources/python` (NC-021), and refuses to build without
  the tarball cached in `packaging/.cache/`. Apple chip and macOS 26 or later only
  (NC-312).
- **The data home belongs to the app, not the repo.** Real data lives in
  `~/Library/Application Support/NetworkCRM/{data,secrets}` — a real directory, so
  the bundle survives deleting the checkout. A developer may make the repo's
  `data/` a **symlink** to it, so a dev instance sees the same data; the test
  suite never does, because `tests/helpers.py` points it at a temporary folder. In
  a fresh clone `data/` is an ordinary folder. The packaged app never points at a
  path inside a checkout: a recipient has no checkout.
- **Dependencies are installed on first run, into the app's own venv.**
  `crm/python_env.py`, which the app runs before starting the engine, builds
  `~/Library/Application Support/NetworkCRM/venv` from the bundled Python and
  `pip install`s **`requirements.txt`** into it, at exactly the versions and file
  fingerprints in `requirements-lock.txt` (`--require-hashes
  --only-binary=:all:`, NC-316), keyed by the sha256 of both files so a new bundle
  reinstalls. This is the reason `requirements.txt` is a contract and
  not a note: anything a shipped feature imports at runtime must be listed there,
  uncommented. A dependency left in a commented-out "optional" block would make the
  Ask view silently answer from the rule-based engine on every install while
  every test passed. `tests/test_packaging.py` guards the *static*
  contract — that `requirements.txt` covers every non-stdlib import in `crm/` —
  and `tests/test_dev_parity.py` guards the *live* one: that the shipped
  interpreter can import what `requirements.txt` promises, that a built bundle's
  `crm/` matches the repo's, and that the interpreter running the suite isn't
  itself missing a production dependency. Both skip cleanly when there's no
  bundle. **Verifying a shipped feature against a development venv proves nothing
  about the product.**
- **A failed install is visible instead of silent.** The core is stdlib-only, so
  pip failing never blocks startup — but `crm/python_env.py` logs the error to
  `~/Library/Logs/NetworkCRM/server.log` and writes a marker at
  `$CRM_DATA_DIR/deps_status.json`. `api_instance.deps_status()` reads that
  marker into the diagnostics, and Settings surfaces it. A successful later install
  deletes the marker.
- **Semantic search is deliberately not shipped.** `sentence-transformers` +
  `numpy` pull PyTorch (~2 GB), which would make a first launch look like a hang
  on every machine it's sent to. They are excluded from `requirements.txt` on
  purpose and are opt-in from the Settings view (`SEMANTIC_PACKAGES` in
  `crm/api_instance.py`), which installs them only at the versions and
  fingerprints in `requirements-semantic.txt`; keyword + FTS5 search work fully
  without them, and §4's graceful degradation is what makes that honest.

The trust boundary is narrower than shape A: the bundle opens no port at all,
and nothing leaves the machine except the five opt-ins in §7.

---

## 8. Recipe: add a new import source

1. **Write the importer** `crm/foo_import.py` following the contract in §3 (match
   to existing contacts, count two-way only, honor `exclude_list.txt`, write both
   DBs idempotently, support `--dry-run`). Copy `imessage_import.py`'s shape.
2. **Register it** in `crm/importers.py`: add an `ImporterSpec` to `REGISTRY` with
   its `label`, `kind`/`pick_kind`, `export_help` (the wizard steps), and a
   `build_cmd` that returns the argv. If it needs a new picker prompt, add it to
   `PICK_SCRIPTS` in **`crm/api_import.py`** (which also owns the
   `/api/import/sources` and `/api/import/run` handlers).
3. That's it — the Import wizard renders the new card, the picker works, and
   `POST /api/import/run` executes it. No frontend edit required.
4. **Test it** with a synthetic fixture: parsing and contact-matching go in
   `tests/test_import_sources.py`, the archive.db side in
   `tests/test_import_archive.py` (see the existing `TestIMessageArchive` /
   `TestWhatsappArchive` classes as templates), and the registry/argv behaviour in
   `tests/test_api_import.py`.

---

## 9. Testing

A stdlib `unittest` suite — **~2,700 tests across ~115 files**, about forty seconds
end to end. One test checks that `requirements.txt` is installed, so install it
first. It used to be one 4,858-line
`tests/test_smoke.py`; it is now one file per area, `tests/test_<area>.py`, plus
`tests/helpers.py` for the shared fixtures.

```bash
python3 -m unittest discover -s tests -t .     # the whole suite
python3 -m unittest tests.test_db_core         # one area
python3 -m pytest tests/ -q                    # pytest works too
```

Roughly: `test_db_core` / `test_db_strength` (the data layer and the 0–100 score),
`test_archive` (archive.db + the engagement roll-up), `test_import_sources` /
`test_import_archive` (every importer against synthetic copies of the real
schemas, both halves of each write, and iMessage `attributedBody` decoding in both
formats), `test_enrich` (invariant 4's `facts_source` precedence), `test_photos`
(invariant 5), `test_api_ask` / `test_api_briefs` / `test_api_contacts` /
`test_api_import` / `test_api_instance` / `test_api_lists` / `test_api_views` (the
API layer, one file per busy module), `test_http` (LAN token auth and
cross-origin write rejection), `test_search` (semantic ranking against a
deterministic stub model, and the graceful-degrade path), `test_export_format`,
and `test_packaging` / `test_dev_parity` (the §7 guards on what the `.app`
actually ships). The rest are named for the one claim each defends, such as
`test_the_privacy_claims_are_true` or `test_an_import_rebuilds_only_whom_it_changed`.
Tests that need `sentence-transformers` skip themselves if it isn't installed.

Two rules about the suite are non-obvious and expensive to get wrong:

**Every test file must import `tests.helpers` before any `crm` module.** Several
crm modules capture paths under `data/` into module-level constants *at import
time* (`config.DATA_DIR`, and `search_cli`'s `crm_vectors.npz` /
`chunk_vectors.npz`). `helpers` points `CRM_DATA_DIR` at a throwaway temp dir
before importing any of them; a file that imports `db` first makes the whole suite
read and write the **real** `data/` directory — and everything still passes while
it happens. The result is a `crm_vectors.npz` holding one synthetic
contact's embedding, costing a full re-embed of every real contact.
`tests/test_bootstrap.py` is the tripwire for a regression, and the fix is the
same three-line shim at the top of every test file:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # must be the FIRST crm-touching import
```

**Patch the owning module, never rebind `app.X`.** `app.py` only *re-exports* the
handlers (§1), so `app.X = stub` rebinds the name in `app`'s namespace while every
caller inside the module that owns `X` keeps using the original. Use
`mock.patch.object(app.api_contacts, "EXCLUDE_FILE", p)` — reach through the
module object, which `app.py` also imports for exactly this reason. This is not
theoretical: patching `_run_claude` (in `test_api_briefs.py`) or `EXCLUDE_FILE` (in
`test_db_core.py`) the wrong way means a real billed Anthropic call or a write to
the configured exclude list while the test still passes. Both patch the owner, and
each keeps an assertion on the *stubbed result* (not just the metadata around it)
that fails loudly if the symbol moves again — don't delete those.

---

## 10. Intake — paste people in (`#intake`)

The one surface that adds people from a block of text: paste a signature, a bio,
a list of LinkedIn addresses → **review** → save. It is documented here in its
own section because it is easy to overlook (NC-016): thousands of lines across
`crm/intake.py` + `crm/api_intake.py`, 127 tests in `tests/test_api_intake.py`
and eight live routes are only useful with an entry point in the front end. If you are reading this because you cannot find the
feature, this is it.

| Piece | ~Lines | Role |
|---|---|---|
| `crm/intake.py` | 1,800 | **Pure.** Text → records → matcher → proposals → report, plus `commit_batch`, the one audited writer. No network, no model, no key. |
| `crm/api_intake.py` | 750 | The eight endpoints, and **the only two egress points**: `fetch_page()` (one https GET of a pasted URL) and `_call_model()` (one Anthropic call, **never with `tools`**). |
| `static/app/intake.js` | 680 | The client for all eight. Paste screen, review screen, receipt screen. |
| `static/css/view-intake.css` | 280 | `.ik-*`, linked from `static/index.html`. |
| `tests/test_intake_ui.py` | — | The gates: **every route in `api_intake.ROUTES` must be called by the client**, the surface is in the router and the omnibox, the stylesheet is linked, and this section names the routes. |

**Reached from two places, on purpose.** The hash `#intake` (registered in
`parseHash`, `SECTION_LABEL` and the view map in `static/app/main.js`) and the
omnibox command *"Paste a list of people"* (⌘K → `paste` / `intake` / `bio`). It
is **not** in `TABS`: that array fixes ⌘1…⌘n in the web page. (The window that
once showed this page hard-coded its Go menu against those digits; since NC-330
the app is native and draws its own paste screen.) Whether it earns a top tab is
open.

### The eight routes, and what the client does with each

| Route | Screen | What it is for |
|---|---|---|
| `GET /api/intake/status` | Paste | What works, what is off, and every cap. Renders the capability sentences and decides whether the model button exists at all. |
| `POST /api/intake/parse` | Paste → Review | The offline read. No key, no network. The default path, not a fallback. |
| `POST /api/intake/propose` | Paste → Review | The model read, identical response shape. Sent with any pages already fetched. Degrades **visibly** into the offline parser (`report.degraded`). |
| `POST /api/intake/lookup` | Review | Fetch ONE address out of the paste as text. Opt-in (`NC_INTAKE_WEB`), one URL per call. The server works out which addresses may be fetched from the pasted `text` itself (`parse_block`, then `allowed_lookup_urls`), so only an address that is in the paste can be fetched. `allowed_urls` in the body is ignored, and a request with no `text` is refused (NC-148). Writes nothing and calls no model. |
| `POST /api/intake/commit` | Review | **The only writer.** Body is `{batch, confirm: true}` — the JSON boolean, because the gate is `bool()` on whatever arrives. |
| `GET /api/intake/batch` | Paste | The parked draft, offered rather than restored. |
| `POST /api/intake/batch` | Review | Park the draft in the existing `state` table (`intake_pending_batch`). No new table, no schema change. |
| `DELETE /api/intake/batch` | Both | Discard it. Touches no contact. |

### The four properties the design is built on

1. **Review before commit.** `parse` / `propose` / `lookup` write nothing. Only
   `commit` writes, and only with `confirm: true`. The review screen is the
   product, not a confirmation dialog.
2. **The model holds no tool.** `_call_model` passes no `tools` argument, ever.
   Its whole output is text, re-parsed by `intake.records_from_model_output` into
   the *same* records the offline parser produces — so create-vs-enrich is
   decided afterwards by `intake.match_one`, from the database, and a fetched
   page cannot choose an action.
3. **The client cannot name a row.** `commit_batch` re-runs the matcher and
   ignores any `contact_id`, `verdict` or `ref` a caller sends; the body carries
   only `intake.COMMIT_KEYS`. A batch is a description of people.
4. **Identity only, never facts.** `intake.WRITABLE` is the whole write surface:
   `name` (a new row only, and only a *confirmed* name), `company`, `position`,
   `how_we_met`, `linkedin_url` (each only when currently empty), identifiers,
   and an append-only note. Every fact field is declined and listed, because a
   web-derived guess entering through `db.set_field` would be stamped `manual` —
   the top of invariant 4's ladder — and sterilize that field forever.

Refusals are first-class: a match on somebody `excluded` or soft-deleted, an
ambiguous match, a repeat of an earlier line, and an unconfirmed name each stop
the write, keep their place in the list, and say why. `tests/test_api_intake.py`'s
`TestKnownBugs` holds no `expectedFailure` tests: the defects it pinned were fixed
on 2026-10-17 (NC-361, NC-362, NC-365 to NC-368) and now run as ordinary gates. Read
it before changing either module.
