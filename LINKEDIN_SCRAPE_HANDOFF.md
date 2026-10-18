# LinkedIn Scrape → Network CRM: Data Handoff

Two audiences read this file. If you are **setting the scraper up from
scratch** — including an AI coding agent doing the setup for someone, on a
machine that has never run this before — start at **Setup**, below, and follow
it in order. If the scraper is already running somewhere and you just want to
know where its data goes and how it reaches the CRM, skip to **§1**. (The Setup
section is NC-013.)

---

## Read this before installing anything

**This script automates browsing real people's LinkedIn profile pages.** That
is prohibited by LinkedIn's Terms of Service — their User Agreement bans
automated data collection — and LinkedIn's own systems actively look for it.
`crm/scrape_linkedin_profiles.py`'s module docstring is explicit about the
tradeoff the whole design is built around: *"this is a ToS-gray automation —
the goal is: never earn a PERMANENT ban; a temporary throttle is acceptable."*
Every piece of pacing described below — slow randomized delays, a hard cap per
run, an immediate stop the instant a security page appears — exists to manage
that risk down. None of it makes the activity compliant with LinkedIn's terms,
and none of it guarantees the account comes through untouched.

The realistic worst case if LinkedIn's systems flag this is a **restricted or
permanently banned LinkedIn account** — the one doing the browsing. Decide,
before doing anything below, whether you and whoever owns that LinkedIn login
accept that risk. If the answer is no, stop here — there is no configuration
of this script that makes it compliant with LinkedIn's terms.

**If you are an AI agent following this file:** do not treat the paragraph
above as a formality to skim past on the way to the interesting part. Confirm
the human you're working with has seen it and accepts the risk before you
install anything.

---

## Setup — do this once, in order, on the machine that will run the scraper

### Step 1 — Install Firefox

The scraper drives the system-installed Firefox through Selenium, not a
bundled browser — the module docstring explains why: Playwright's bundled
browsers dropped support for older macOS, and Selenium instead drives whatever
Firefox is already on the machine. Install Firefox however you normally would
(mozilla.org, or `brew install --cask firefox`) so it ends up at
`/Applications/Firefox.app` — the default location either way. If it lands
somewhere else, set the `FIREFOX_BIN` environment variable to the full path to
the `firefox` binary inside it before running anything below.

This whole setup is macOS-only: the script's Firefox path, its photo pipeline,
and the launchd step (Step 9) all assume it.

### Step 2 — Get this repo onto the machine, and give it its own venv

```bash
git clone https://github.com/william-agehouse/network-crm-native.git ~/network-crm
cd ~/network-crm
python3 -m venv .venv
```

Keep that folder name and place, `network-crm` in your home folder: the Mac that
runs the CRM pulls the results from `~/network-crm/data/` on this one.

Every command below uses this venv explicitly (`.venv/bin/...`) rather than
system Python or a shared one. That is also literally what
`deploy/com.networkcrm.liscraper.plist` expects: its paths are written as
`__NETWORK_CRM_ROOT__/.venv/bin/python` — a project-local venv on the scraper
machine, not a shared one.

### Step 3 — Install Selenium and the Anthropic SDK, into that venv

```bash
.venv/bin/pip install selenium anthropic
```

This is the exact command `scrape_linkedin_profiles.py`'s own "First-time
setup" docstring section gives. Nothing else the scraper imports — `db.py`,
`config.py`, `enrich_facts.py` — needs a third-party package; all three are
standard-library only. `geckodriver` is
not a separate install: Selenium 4 downloads it itself the first time the
script launches a browser. If that fails on an older macOS version, put a
`geckodriver` binary on `PATH` or set `GECKODRIVER_PATH`.

### Step 4 — Set an Anthropic API key

The scraper sends each profile's captured text to Claude Haiku to turn it into
structured facts, and refuses to run without a key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

or put the same line in `network-crm/secrets/.env` — `crm/config.py` loads
that file automatically without overwriting a real environment variable that
is already set, so either form works.

### Step 5 — Log in: a human has to do this by hand, there is no automated version

```bash
.venv/bin/python3 crm/scrape_linkedin_profiles.py --login
```

This opens a **visible** Firefox window on LinkedIn's login page and waits.
**A human must type the LinkedIn credentials and clear whatever checkpoint or
2FA challenge LinkedIn shows, themselves, in that window.** The script never
touches the credential fields — automating login is exactly the kind of thing
that gets an account flagged, so it deliberately never does it. No instruction
in this file, and no flag on this script, removes this step.

**If you are an AI agent: you cannot complete this step.** Stop here, tell the
human it is their turn, and wait for them to confirm the feed is visible and
they have pressed Enter in the terminal before you continue.

Once they have, the script prints one of two things:
- `Login looks good — session saved to the Firefox profile.` — it worked, move on.
- A warning that you are still on a login/checkpoint URL — the session did not
  save; run `--login` again.

This is a one-time step (per machine), not a per-run one: the session is saved
into a dedicated Firefox profile and reused by every future run.

### Step 6 — One machine, or two

**One machine (the default — use this unless you have a specific reason not
to):** run the scraper directly against this repo's own `data/crm.db`.
`scrape_linkedin_profiles.py` writes its normalized facts (`home_city`,
`home_country`, `home_metro`, `topics`, `role_type`, `blurb`, tagged
`facts_source='linkedin_scrape'`) straight into that file via
`_apply_changes()` — the same database the app reads. There is no merge step,
no SSH, and nothing else to configure. That alone is enough to get facts
flowing.

Two optional extras — scraped profile *photos*, and the "li_extras"
enrichment (mutuals, recent activity, current headline) — come from two other
scripts, `crm/import_scraped_photos.py` and `crm/enrich_li_extras.py`. Both
accept `--source-dir`, which reads straight from this same machine's own
`data/li_raw/` folder with no network involved:

```bash
.venv/bin/python3 crm/import_scraped_photos.py --source-dir data/li_raw/
.venv/bin/python3 crm/enrich_li_extras.py --source-dir data/li_raw/
```

(`--source-dir` is deliberately not called `--source` — that name already
belongs to `merge_scraped_facts.py`'s flag for a single pulled *database
file*. These two scripts instead take a *directory* of `<id>.jpg`/`<id>.json`
files, a different enough shape that reusing the name would invite
`--source data/li_raw/` to silently fail on the wrong script.)

**Two machines (an option — most of the rest of this document describes
it):** the scraper runs on a second, dedicated Mac against a *copy*
of `crm.db`, and you pull the results back with `deploy/pull_and_merge.sh`. Do
this only if you deliberately want the ToS-gray browsing isolated from the
machine your real data lives on. It needs an SSH alias/key to that second
machine and `IMAC=user@host` set — see "6. Reaching the scraper machine (ops
only)" below for how to wire it; substitute your own second Mac
and SSH alias throughout.

### Step 7 — Test run before anything real

First, a dry run: no browser, no LinkedIn contact at all, just a list of who
would be scraped.

```bash
.venv/bin/python3 crm/scrape_linkedin_profiles.py --dry-run --limit 3
```

Expect output shaped like:

```
[dry-run] 3 candidate(s) would be scraped (limit=3), most-relevant-first, no browser launched:
  [1/3] score=87  Jane Doe  [source=(none), updated=never]
  [2/3] score=81  John Smith  [source=offline, updated=2025-01-01T00:00:00+00:00]
  [3/3] score=76  ...
```

If that list looks right, run the real thing against a small number before
trusting it with anything larger:

```bash
.venv/bin/python3 crm/scrape_linkedin_profiles.py --limit 3
```

**How to tell it worked:**
- each contact prints a line like
  `[1/3] Jane Doe: city=Berlin, topics=["robotics", ...]  (raw saved)`,
  and the run ends with `Done.`;
- `data/li_raw/<contact_id>.json` and `<contact_id>.html.gz` exist for each
  contact just scraped;
- `sqlite3 data/crm.db "SELECT name, home_city, facts_source FROM contacts WHERE facts_source='linkedin_scrape';"`
  shows those rows.

Only increase `--limit` past this once a `--limit 3` run has gone cleanly
end-to-end.

### Step 8 — If you hit a checkpoint: stop. Do not retry.

If LinkedIn shows a security or CAPTCHA interstitial mid-run, the script
detects it, prints:

```
CHECKPOINT / SECURITY PAGE DETECTED — stopping immediately.
Do NOT re-run for a while. Log in by hand in a normal browser
first, then re-run --login later if needed.
```

commits whatever was already scraped, and exits (code 3). **This is the single
most important operational rule in this whole document: do not re-run the
scraper after this.** Log into LinkedIn by hand in an ordinary browser first
to confirm the account is otherwise fine, then leave it alone.

If you run the scraper through the paced dispatcher (`crm/scrape_dispatch.py`,
Step 9) this happens automatically: a checkpoint sets a cooldown of
**24-48 hours** (`crm/scrape_dispatch.py`'s `SCRAPE_COOLDOWN_MIN` / `_MAX`,
24/48 by default) during which every scheduled tick is skipped without
running anything. Respect that same window even when running by hand — a
temporary throttle only stays temporary if you don't press on into it.

### Step 9 — Optional: run it continuously via launchd

`deploy/com.networkcrm.liscraper.plist` is a LaunchAgent template: it wakes
`crm/scrape_dispatch.py` every 30 minutes, and that script decides, itself,
whether to run one small paced session right now (active hours, daily cap,
inter-session gaps and the Step 8 cooldown all live there — see its own
module docstring for the exact tunables).

**Every path in it is the literal token `__NETWORK_CRM_ROOT__`, not a path — so
it will not load until you substitute your own** (NC-014 made it a token rather
than a real-looking path precisely so it cannot be loaded by accident):

```bash
sed 's|__NETWORK_CRM_ROOT__|'"$PWD"'|g' \
  deploy/com.networkcrm.liscraper.plist \
  > ~/Library/LaunchAgents/com.networkcrm.liscraper.plist
# Run this FROM the checkout on the scraper machine, so $PWD is its root.
# Then confirm the python path points at .venv/bin/python from Step 2, not a
# system interpreter.
launchctl load ~/Library/LaunchAgents/com.networkcrm.liscraper.plist
```

This step is optional — a manual `--limit N` run whenever you like works fine
too. The LaunchAgent just automates the pacing that protects the account over
long, continuous use.

---

## TL;DR

The scraper visits each 1st-degree LinkedIn connection's profile and turns it
into structured facts (§2). Run it on the same machine as your `crm.db` (Setup
Step 6) and those facts land straight in the **existing `contacts` columns**
`enrich_facts.py` already uses, tagged `facts_source = 'linkedin_scrape'` — no
merge step needed. A two-machine setup instead runs the scraper on an isolated
second Mac and pulls the results back (§1, §4, §6); richer raw fields
(headline, skills, mutuals, full experience/education text) are archived as
files there and can be lifted into the CRM if you want more than the
normalized set (§5).

---

## 1. Where the data lives

*(On a one-machine setup — Setup Step 6 — these three collapse into one:
there is only ever one `crm.db`, and `data/li_raw/` sits right next to it.
What follows describes a two-machine setup.)*

- **Master DB:** `network-crm/data/crm.db` on the Mac that runs the CRM — the one the
  CRM app already reads. The scraper's output is merged into it (see §4).
- **Scraper's working DB:** a *copy* of `crm.db` on the scraper Mac at
  `~/network-crm/data/crm.db`. The scraper writes here; `pull_and_merge.sh`
  copies the facts back to the master. (The scraper Mac never writes the master
  directly.)
- **Raw archive (scraper Mac only):** `~/network-crm/data/li_raw/` — one `.html.gz` +
  `.json` per contact. **These do NOT come back with `pull_and_merge.sh`** (it
  only syncs the DB). Pull them separately if you want them (see §5).

---

## 2. The normalized fields written to `contacts`

These are existing columns (added by `db.py::ensure_columns`, also used by
`enrich_facts.py`). The scraper populates them via the shared Claude Haiku
prompt (`enrich_facts.LLM_SYSTEM`) and `enrich_facts._apply_changes`:

| Column             | Format                     | Example                                            |
|--------------------|----------------------------|----------------------------------------------------|
| `home_city`        | text                       | `Berlin`                                           |
| `home_country`     | text                       | `Germany`                                          |
| `home_metro`       | text                       | `Berlin`                                           |
| `topics`           | **JSON array string**      | `["robotics", "industrial tech", "venture capital"]` |
| `role_type`        | text (fixed vocab)         | `founder` / `investor` / `research` / `tech` / …   |
| `blurb`            | text (≤200 chars)          | `Co-founder of Acme Robotics, focused on warehouses.` |
| `facts_source`     | text                       | `linkedin_scrape`                                  |
| `facts_updated_at` | ISO-8601 UTC               | `2025-01-15T12:00:00+00:00`                         |

`role_type` vocabulary (from `enrich_facts.LLM_SYSTEM`): `founder investor tech
product design sales marketing finance operations medical legal research recruiter
consultant`.

> **Note:** `topics` is stored as a JSON *string*, not a native list —
> `json.loads(row["topics"] or "[]")` to read it.

---

## 3. How the data is created (pipeline)

`crm/scrape_linkedin_profiles.py`, per contact:

1. Picks candidates: `contacts` with a `linkedin_url`, not `excluded`, not
   `facts_source='manual'`, ordered by **CRM relationship-strength score**
   (`db.relationship_strength`) — most-relevant people first.
2. Opens the profile once in headless Firefox (Selenium), scrolls to load all
   sections, and grabs the **full page text**.
3. Sends that text to **Claude Haiku** (same prompt as `enrich_facts` Pass C) →
   `{city, country, metro, topics, role_type, blurb}`.
4. Writes those to `contacts` via `_apply_changes(..., "linkedin_scrape")`.
5. Archives the raw scrape to `data/li_raw/<contact_id>.{html.gz,json}`.

For continuous/scheduled operation (Setup Step 9), `crm/scrape_dispatch.py` +
a LaunchAgent pace this out: profiles/day, session size, inter-session gaps
and the post-checkpoint cooldown are all tunable there (defaults 40-55/day,
8-15 per session, 75-210 minutes between sessions, 24-48h cooldown after a
block — see that script's own module docstring). Not relevant to consuming
the data — it just keeps the rows flowing safely over time.

**Never-clobber rule:** anything a human set (`facts_source='manual'`, e.g. via
`db.set_field` on `home_city`) is never overwritten by the scraper or the merge.

---

## 4. How it reaches the master `crm.db`

*(Two-machine setups only — Setup Step 6. On one machine, the facts are
already in the master db the moment the scraper writes them; there is
nothing in this section to run.)*

**In the app:** Data → Enrich → **LinkedIn profile facts** — one click runs the
same pull + merge below.

Or from the command line on the Mac that runs the CRM (it reaches the scraper Mac over SSH):

```bash
cd "/path/to/network-crm" && ./deploy/pull_and_merge.sh          # pull + merge
./deploy/pull_and_merge.sh --dry-run                             # preview
# (both are thin wrappers around: python crm/merge_scraped_facts.py --pull)
```

This runs `crm/merge_scraped_facts.py`, which:
- asks the scraper Mac to write a **consistent snapshot** of its `crm.db` to
  `/tmp/ncrm-pull-snapshot.db` (`sqlite3 … ".backup"`, `umask 077`), copies that,
  and deletes it from the scraper Mac afterwards,
- copies the 8 fields in §2 from that snapshot into the master,
- matches rows by **`id` + `name`** (the scraper Mac's DB is a snapshot copy, so ids align),
- **skips** any master row with `facts_source='manual'`.

The snapshot step is not optional politeness. The scraper Mac's `crm.db` is in WAL mode,
so its newest commits sit in a `crm.db-wal` sidecar until SQLite checkpoints them.
Copying only `crm.db` would leave out every profile scraped since the last
checkpoint, silently, because a WAL database without its sidecar reads as a
perfectly valid *older* one. If the scraper Mac cannot produce a snapshot the pull
says so and copies the live db anyway: a lagging sync, not a broken one.

So to get the latest scraped data into the CRM, that one command is all it takes;
the rows are then live in `crm.db` for the app to read.

---

## 5. Richer raw data (optional, on the scraper machine)

Each `data/li_raw/<contact_id>.json` holds MORE than the normalized set — useful if
you want to add structured fields to the CRM:

```json
{
  "url": "...", "final_url": "...",
  "name_on_page": "...", "headline": "...", "location": "...",
  "sections": { "about": "...", "experience": "...", "education": "...",
                "licenses": "...", "interests": "...", ... },
  "skills": ["...", "..."],
  "mutuals": "Ada, Alan and 40 other mutual connections",
  "connections": "500+ connections",
  "followers": "1,234 followers",
  "profile_text": "<full rendered profile text>",
  "contact_id": 42, "name": "...", "scraped_at": "..."
}
```

`<contact_id>.html.gz` is the full page HTML for re-parsing anything else later.

On a two-machine setup these files stay on the scraper machine. To bring them
to the master machine for processing:

```bash
rsync -avz -e "ssh -i ~/.ssh/<your-key>" scraper:network-crm/data/li_raw/ ./data/li_raw/
# ('scraper' is the ssh alias from §6 — substitute your own alias/host/key)
```

On a one-machine setup they are already local: read `data/li_raw/` directly,
or use `--source-dir` on `import_scraped_photos.py` / `enrich_li_extras.py`
(Setup Step 6).

If the CRM wants e.g. `headline`, `skills`, `mutual_count`, or work-history as
columns, parse these JSONs (keyed by `contact_id`, matching `contacts.id`).

---

## 6. Reaching the scraper machine (ops only)

*(Two-machine setups only. Skip this if you followed the one-machine Setup
path — there's only one machine.)*

Reach the scraper Mac over SSH, ideally across a private network such as
Tailscale, which gives it a stable name without opening any port.

Set it up as an alias in `~/.ssh/config` on the machine that runs the CRM, so
one word reaches it:

```
Host scraper
  HostName <its-tailscale-name>
  User <the scraper account>
  IdentityFile ~/.ssh/imac_scraper
```

Then `ssh scraper` works, and `IMAC=<the scraper account>@<its-tailscale-name>`
(plus `KEY=...` if the key is not at the default, `~/.ssh/imac_scraper`) is what
`merge_scraped_facts.py`, `import_scraped_photos.py` and `enrich_li_extras.py`
read in their `--pull` mode.

**Keep your real host name, login and private-network address in your own
`~/.ssh/config`, not in this repo** (NC-138): together they are a map of a
private network, and they are of no use to anyone else.

You generally don't need the machine to *use* the data — `pull_and_merge.sh` is
the only touchpoint. SSH is only for inspecting the scraper itself.

---

## 7. Suggested CRM integration work

The normalized fields are already schema-native, so the main opportunities are
**surfacing** them:

- Show `blurb` + `topics` + `home_city` on contact cards in the desktop app.
- Filter/segment the network by `home_metro` / `home_city` (there's already a
  `location` filter in `db.list_contacts`) and by `role_type`.
- "Who do I know in <city>?" / "founders in robotics" style queries — the data now
  supports these for scraped contacts.
- *Optional:* lift `headline`, `skills`, and a `mutual_count` from the raw JSONs
  (§5) into new `contacts` columns if you want richer profiles/filters.

**Data caveats to respect:**
- `topics` is a JSON string — parse before use.
- Coverage grows over time (at the dispatcher's default pace — see §3), most
  relevant contacts first — not every contact has `facts_source='linkedin_scrape'`
  yet; fall back to existing sources (`offline` / `message_scan` / `llm`) for the
  rest.
- Never overwrite `facts_source='manual'`.

---

## Key files

| File | Role |
|------|------|
| `crm/scrape_linkedin_profiles.py` | the scraper (extraction + LLM + DB write + raw archive); `--login`, `--limit`, `--dry-run`, `--backfill-photos` |
| `crm/scrape_dispatch.py`          | paced scheduler (dispatcher); `--status` prints its decision without scraping |
| `deploy/com.networkcrm.liscraper.plist` | LaunchAgent template that wakes the dispatcher every 30 min — generate/adapt your own (Setup Step 9), never copy in as-is |
| `crm/merge_scraped_facts.py`      | two-machine only: merge scraper-machine facts → master `crm.db`; `--pull` or `--source /path/to/db` |
| `crm/import_scraped_photos.py`    | two-machine `--pull`, or one-machine `--source-dir data/li_raw/`: register scraped photos |
| `crm/enrich_li_extras.py`         | two-machine `--pull`, or one-machine `--source-dir data/li_raw/`: mutuals/recent-activity extras |
| `deploy/pull_and_merge.sh`        | two-machine only: one-command pull + merge of all three above |
| `crm/enrich_facts.py`             | shared LLM prompt + `_apply_changes` (reused, not duplicated) |
| `crm/db.py`                       | `contacts` schema + `relationship_strength` |
| `data/li_raw/`                   | raw per-profile archive (scraper machine on two-machine setups; local on one-machine setups) |
