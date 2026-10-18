# Network CRM

A private memory of **everyone you know** — built from the conversations you
already have, searchable in plain English, and yours alone.

**Built for macOS, and tested only there.** The iMessage, WhatsApp, Apple Contacts
and Calendar importers, the sync helper and the packaged app are Mac-only. **The
Mac app needs a Mac with an Apple chip (M1 or later) and macOS 26 or later**; the
copy built for handing out does not run on a Mac with an Intel processor, and
building one yourself on an Intel Mac is untested. Running from source needs
Python 3.10 or newer.

It pulls your contacts and message history from your address book, LinkedIn,
iMessage, WhatsApp, email and your calendar, then gives you two ways to use them:

- **A local app** to browse your network, see every conversation with a person on
  one page, and jot notes in your own words.
- **An "Ask" chatbot** — "who do I know in London?", "which designers should I
  ask to the workshop next month?" — grounded in your real contacts, not made up.

> **Privacy first.** Everything runs on your machine, and nothing leaves it until
> you turn something on: your contacts, notes, messages, emails and search index
> all live on this Mac. Four things send data out, and only once you turn them on:
>
> - **Your own Anthropic key.** Ask and the other AI features send Anthropic what
>   they work from, each time you use them: your question, and the contact
>   details, notes and **message excerpts** that Ask's searches find.
> - **Reading links in a pasted list of people** (off until you turn on “Let Intake read a web page” in Settings).
>   Each click fetches one address from what you pasted.
> - **Connecting LinkedIn, Google or a calendar feed.** The app asks that service
>   for your own data from it.
> - **Exporting.** What you export goes wherever you put it.
>
> The app never checks for new versions by itself. A copy built for handing out
> shows this project's releases page in Settings; looking there is up to you.
>
> [ARCHITECTURE.md §7](ARCHITECTURE.md#7-deployment-shapes--trust-boundaries) says
> exactly what each AI feature sends.

---

## Quick start

**If you already have the Mac app, skip this section.** On a Mac with an Apple chip
and macOS 26 or later it needs no Terminal and nothing installed; see
[Install as a Mac app](#install-as-a-mac-app).
There is no ready-made download yet, so getting the app means building it once,
in Terminal, with the two commands in that section. What follows is for running
from source.

You need **Python 3.10 or newer**. A Mac does not come with one: `/usr/bin/python3`
is Python 3.9.6, which is too old, and `xcode-select --install` installs that same
3.9.6 — so that is not the fix, despite being the obvious guess. Get the macOS
64-bit universal2 installer from
[python.org/downloads](https://www.python.org/downloads/).

You also need `git`. If the first command below makes macOS offer to install the
"command line developer tools", accept it — or download the repo as a ZIP and
unpack it instead, which needs nothing.

No account, no cloud, no build step.

```bash
git clone https://github.com/william-agehouse/network-crm-native.git
cd network-crm-native
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt -c requirements-lock.txt
```

`requirements-lock.txt` holds the exact version of every package, including the
ones the listed packages pull in, so your copy installs the same code the app
does rather than whatever is newest today.

The virtual environment (`.venv`) is a private folder of packages belonging to
this app alone. It is worth the one extra line: it keeps these packages out of
the rest of your Mac, and some Python installations refuse to install
packages at all outside one.

Then launch the app:

- **Double-click `start.command`** in Finder — it finds `.venv` on its own — **or**
  run `.venv/bin/python3 crm/app.py`. From a ZIP download, macOS may refuse to
  open `start.command` the first time, because it came from the internet: open
  **System Settings → Privacy & Security**, scroll to Security, and click **Open
  Anyway**. A `git clone` does not have this.

It opens in your browser at `http://127.0.0.1:8765`. That's it — you can start
adding people immediately, or head to **Data** to bring in your existing data.

The app is dependency-free at its core (Python standard library + SQLite). AI
features (the Ask chatbot, AI enrichment) are optional and only switch on once you
add a key from Anthropic in Settings — see [Optional AI features](#optional-ai-features).

### Install as a Mac app

`packaging/` builds **Network CRM.app**: an ordinary Mac app with its own native
window. To build it, in Terminal, from the repo folder:

```bash
xcode-select --install          # Apple's command line tools: they compile the app (not a fix for Python)
mkdir -p packaging/.cache       # the Python the app carries inside it, downloaded once:
curl -L -o packaging/.cache/cpython-3.13.14+20260728-aarch64-apple-darwin-install_only.tar.gz \
  "https://github.com/astral-sh/python-build-standalone/releases/download/20260728/cpython-3.13.14%2B20260728-aarch64-apple-darwin-install_only.tar.gz"
packaging/appify.sh --embed
```

The command line tools are only for compiling the app's window and its sync
helper. They do not give you a Python new enough to run it from source; the built
app does not need one. The download (about 25 MB) is the Python the app carries
inside it, so the app runs on a Mac with nothing installed. The build looks for
it in `packaging/.cache/` and never downloads it itself; without it the build
stops and says where to put it, because the app cannot start without it.

The app lands in `packaging/dist/`; drag **Network CRM.app** to Applications.
Opening it starts its engine (the Python part that keeps your data); quitting it
stops everything. Nothing else on the Mac can reach the engine: it has no network
address that a browser or a web page could open, only a private file that this
app and your account can open. The build itself needs no Python, and before it
finishes it opens the engine from inside the new app on a throwaway folder to
check all of this. [`packaging/README.md`](packaging/README.md) has every option.

To build the copy you give to other people, use `packaging/appify.sh --handout`
and then `packaging/make_dmg.sh`: the first refuses to finish unless the app
carries its own Python and its helper, and the second puts it in a disk image
with a **Read Me First** that walks the recipient through the first launch.

Four things to know:

- **It needs a Mac with an Apple chip (M1 or later) and macOS 26 or later.** On
  an older macOS, or a Mac with an **Intel** processor, macOS itself says the app
  cannot open there.
- **A finished `.app` needs nothing installed.** The bundle carries its own
  Python inside it, as many Mac apps carry their own runtime, so you drag it to
  Applications and open it. No interpreter to install, no admin password, no
  Terminal. That is most of the app's size. The first time a new version opens it
  takes about a minute and needs the internet, to fetch the parts the AI features
  use, at exact versions checked against their published fingerprints; the window
  says so while it happens. Without the internet the app still opens, and tries
  again next time.
- **The Python download is the build's only manual step.** Any newer `install_only`
  `aarch64-apple-darwin` file from
  [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases)
  works too; the release page lists each file's checksum. `packaging/.cache/` is
  ignored by git, so a fresh clone never has it. The building Mac has to have an
  Apple chip too.
- **A build you make yourself is unsigned**, because signing needs an Apple
  Developer ID certificate that you would have to bring.
  So macOS Gatekeeper stops the *first* launch, in one of two ways depending on
  how the app reached the Mac. Usually it says it could not check the app: click
  **Done**, then open **System Settings → Privacy & Security**, scroll to
  Security, click **Open Anyway** (offered for about an hour after the blocked
  attempt), and confirm with your Mac's password. Right-click → **Open** no
  longer does this since macOS 15. If you instead see *"Network CRM is damaged
  and can't be opened"* it is not damaged — that is the message for a
  quarantined **unsigned** app, and one `xattr` command clears it. Both outcomes
  and their fixes are in
  [`packaging/README.md`](packaging/README.md#first-launch-the-app-is-not-signed-with-a-developer-id), and a DMG built by
  `packaging/make_dmg.sh` carries them as **Read Me First.txt** beside the app,
  because a `.app` user never opens a repo.

### The window's source, `native/`

The app's window is written with Apple's own interface toolkit (SwiftUI); its
source is in `native/Sources`, and `packaging/appify.sh` compiles it into the app.
`native/build.sh` builds a quick developer copy, **Network CRM Native.app**, that
runs this checkout's engine code on the checkout's `.venv`, against your real data
folder, so quit Network CRM before opening it. Both builds compile with the
macOS 26.5 toolkit from the command line tools when it is there, and otherwise
with whichever toolkit `xcrun` finds, and say which they used.
[`docs/EDITING.md`](docs/EDITING.md#network-crm-nativeapp) has the details.

**Updating.** New versions are published at
[github.com/william-agehouse/network-crm-native/releases](https://github.com/william-agehouse/network-crm-native/releases).
To update, replace the app; your data stays. Quit Network CRM, drag the new
version into Applications and choose Replace: your contacts, notes and messages
live in your own Library folder, not inside the app.

---

## The app — four tabs

A single window, warm and editorial, with an **omnibox** (⌘K) that takes anything:
a name jumps to the person, a question goes to Ask, a command runs.

| | |
|---|---|
| **People** | The browsable list (photos, strength rings, filters) and the person page: **"the story so far"** up top, then a unified timeline where your notes and **conversation episodes from every channel** interleave chronologically. |
| **Ask** | A real chat over your whole network. Ask in plain English; get a grounded reply plus tappable contact cards. See [Ask](#ask--your-network-in-plain-english). |
| **Lists** | Named groups of people, such as a guest list or a shortlist, each exportable as CSV. |
| **Data** | The occasional tasks: every source and what it takes to switch on, the passes that fill in context, possible duplicates, and export. **Settings** is the gear in the top bar. See [Importing your data](#importing-your-data). |

Every contact gets a 0–100 **relationship-strength** score from how recently and
often you talk (across all channels), how many channels you share, and what
you've written — shown as a ring around their photo. Dark mode included.

---

## Ask — your network in plain English

The **Ask** page is a conversation, not a form:

> **You:** who are good people to talk to in Boston?
> **CRM:** Here are your Boston contacts: **Maya Exempeldotter** (CEO & Founder,
> Exempel Works) — your strongest Boston connection… **Tomas Fiktivsson**
> (Co-Founder, Fiktiv Labs)… *(+ tappable contact cards)*

Follow-ups keep context ("only founders", "who do I know best there?"). It's
grounded in your real data through tool calls — it will tell you when it finds
nobody rather than inventing names.

- **With an `ANTHROPIC_API_KEY`:** full LLM chat with tool use over your CRM.
- **Without one:** the same box falls back to local semantic + keyword search, so
  it still answers — just without the conversational phrasing.

The same brain answers from the omnibox and from the Ask view — one brain, two doors.

---

## Importing your data

Open **Data**. **Needs you**, at the top, lists only what the app cannot do
without you; an export already sitting in Downloads or on the Desktop turns up
there on its own. **Your sources**, below it, lists every source. Click a row to
see what it holds, what it takes to switch on, and how: an **Open…** shortcut to
the right page or folder, then a file picker where a file is needed. No
Terminal, no typing paths. The first run walks through the same sources in order.

| Source | You provide |
|---|---|
| **Apple Contacts** | Nothing, with the sync helper installed — it copies your address book and this reads the copy. Without it, a `.vcf` export you make yourself (embedded photos come too) |
| **LinkedIn** | For your connections, LinkedIn's own export, which works for anyone: ask LinkedIn for a copy of your data, choose Connections, and pick the `Connections.csv` it emails you, usually within a day or two. Your **messages** need LinkedIn's API: a LinkedIn developer app of your own that LinkedIn has approved for its copy-of-your-data product (Member Data Portability), and a token with the `r_dma_portability_self_serve` permission, pasted on the **LinkedIn messages** row. LinkedIn decides who can get that product. LinkedIn shows the app only your recent messages, so it adds to what it saved before rather than replacing it |
| **iMessage** | A copy of `~/Library/Messages/chat.db` you make yourself |
| **WhatsApp** | A copy of WhatsApp Desktop's `ChatStorage.sqlite`. Profile photos come with it — they are not a separate step |
| **Email** | An exported `.mbox` mailbox |
| **Gmail** | A sign-in file you make once at Google Cloud Console — about five minutes, and the Data page walks you through it step by step. After that it reads your mail directly and stays current with no export to make. Google will not let one app read many people's mail without a yearly security audit, so everyone makes their own key |
| **Calendar** | Nothing, with the sync helper installed — it copies Calendar's own store, which holds **every** calendar on this Mac (macOS offers nothing narrower), and this reads the copy. Without it, a published Google or iCloud link pasted on the Calendar row of the Data page and fetched again daily, or a `.ics` you export by hand |

**The app itself never gets Full Disk Access.** For
iMessage and WhatsApp you copy the database file yourself and point the app at
*your copy*. (If Finder asks permission to copy, that's Finder, not this app.)

There is now an **opt-in** way to skip the copying, and it does need Full Disk
Access — granted to a separate compiled helper of about 1,200 lines, never to the app. The
helper copies four things into the app's own folder every couple of minutes: the
iMessage and WhatsApp databases, your address book from every account, and
Calendar's store with every calendar on this Mac. It does nothing else: no network of any kind, no
subprocesses, and the only files it writes are those copies and one status file.
It exists as its own binary precisely so the permission is scoped to one small
program you can read end to end; the app carries on with no permission
at all if you decline. See
[`packaging/IMESSAGE_SYNC.md`](packaging/IMESSAGE_SYNC.md).

Three rows on the Data page then fill in context:

- **Worked out on this Mac** runs by itself after every import: how close you
  are to each person, and cities and roles read from phone numbers, email
  addresses and job titles. Free, and nothing leaves the Mac.
- **LinkedIn profile facts** pulls each person's city, topics, role and a
  one-line summary from your own scraper machine (see
  `LINKEDIN_SCRAPE_HANDOFF.md`).
- **Facts written by AI**, with an Anthropic key, has the model write a city,
  topics and a short summary for people.

> **Before you run the LinkedIn scraper:** it breaks LinkedIn's User Agreement,
> and the account it browses with can be restricted or permanently banned. The
> guide explains the risk and how the pacing lowers it; nothing makes it
> compliant. The rest of the app works without it.

Re-run any import anytime to sync new activity; it refreshes the numbers and never
touches your notes, categories, or descriptions.

---

## Filling in what you know

Imported contacts are a pile of names until you say something about them. The
first run's **Remember three people** step starts that: it picks three people you
have never described and asks *"how do you know them?"*, and whatever you type is
saved as a note on that person. After that, every person's page has a **How you
met** field and a timeline of your notes.

That is deliberately in the app rather than in a chat bot you have to set up,
keep a token for, and keep running: every user would pay the setup cost, and
almost nobody would get as far as using one.

Telegram, Messenger and Instagram have no import rows in the app: Messenger's
end-to-end encryption keeps recent chats out of Meta's standard export altogether,
and all three need a hand-made download in an app whose rule is that nothing should.
`crm/telegram_import.py` and `crm/meta_import.py` still work from the command line
if you have an export.

---

## Where to run it — two shapes

Imports, the app, and search are all local, and **no GPU is needed anywhere** —
semantic search is a small CPU model. So the only reason to run this anywhere but
your own Mac is reaching it from another device.

| Shape | Cost | Privacy | Best for |
|---|---|---|---|
| **A. All-local** — run everything on your Mac | $0 | Max (nothing leaves the Mac unless you turn it on; see *Privacy first*) | The zero-setup default |
| **B. Home server** — a spare, always-on Mac runs everything | $0 | Max (all data stays home) | **Recommended** if you have the hardware |

**A (local):** just launch the app on your Mac. Done.

**B (home server / LAN):** run the app on the always-on machine bound to the LAN,
behind a token, and browse to it from your laptop:

```bash
APP_HOST=0.0.0.0 APP_TOKEN=$(openssl rand -hex 16) .venv/bin/python3 crm/app.py
# then on your laptop, open:  http://<that-machine-ip>:8765/?token=<the-token>
```

Bound to the LAN, every `/api` route requires the token; the loopback default
(`.venv/bin/python3 crm/app.py`) enforces nothing and stays zero-friction.

**The token is not optional here.** Set `APP_HOST` without `APP_TOKEN` and the app
refuses to start and tells you so, rather than serving your contacts to everyone
on the network.

---

## Optional AI features

Everything works with no keys. To turn the AI features on, open **Settings** (the
gear in the top bar), find the row **Ask, drafts and facts written by AI**, and
click it, paste a key from Anthropic into **Key from Anthropic**, and press
**Connect**. Once a key is in, the same row picks the model; its setting is
`ANTHROPIC_MODEL` (default `claude-opus-5-5`), which is Opus 5.5. The app saves
the key in its own `secrets/` folder and uses it straight away.

- An Anthropic key (`ANTHROPIC_API_KEY`) turns on the **Ask** chatbot (the model
  looking things up in your contacts and messages), AI enrichment
  (`enrich_facts.py --llm`), and AI email and relationship summaries.
- **Finding people by meaning**, so "who works in robotics" also finds people
  who never used the word. No key, and the search runs on this Mac and sends
  nothing. Turn it on from the Settings row **Find people by meaning, not just
  words**, or from source with
  `.venv/bin/python3 -m pip install sentence-transformers numpy`. Either way it is
  a large download: about 2 GB of packages (they bring in PyTorch), then a 90 MB
  model from Hugging Face the first time it runs. It is used by the People
  search and by Ask when there is no key.

Running from source, you can write the file by hand instead: `mkdir -m 700 secrets`,
copy `env.example` to `secrets/.env`, fill in only what you want, and restart the
app. The Mac app keeps its copy in `~/Library/Application Support/NetworkCRM/secrets`.

---

## Privacy & what's shareable

- `data/` and `secrets/` are gitignored, as are `*.db`, `*.npz`, `*.mbox`,
  `Connections*.csv`, and the Gmail credential files. Your data and keys are never
  committed.
- **Check before you share a fork publicly.** Gitignoring `data/` keeps your
  contacts out of it, but names and numbers can still get in by hand, in test
  fixtures, examples, comments and commit messages. Search the files **and**
  `git log -p` for the names and numbers of people you know before you push.
- **What protects the copies on your Mac.** The message text the app keeps
  (`archive.db`) and the copies of your iMessage, WhatsApp, contacts and calendar
  databases are stored unencrypted in its data folder, whichever way they got
  there. Other accounts on the Mac cannot open the Mac app's folder, but any
  program you run yourself can read it, the same as your other documents.
  FileVault (macOS's own disk encryption, in System Settings → Privacy &
  Security) is what protects them if the Mac is lost or stolen.
- **Back up the whole data folder**, not one file in it. The Mac app keeps it at
  `~/Library/Application Support/NetworkCRM/data`; running from source, it is
  `data/` in the checkout. `crm.db` holds everything you typed. `archive.db`
  holds the message text, and some of it cannot be imported again: LinkedIn
  shows the app only its most recent messages, so older ones exist nowhere else.
  `photos/` holds the pictures you set by hand. **Export** (Markdown / CSV /
  JSON) is a readable copy of your contacts and notes, not a backup: it leaves
  out the messages and the photos.

---

## Removing it

Putting the app in the Trash is not enough: your data stays in your Library
folder, and the optional copying helper keeps running with Full Disk Access.
**Settings → Remove everything** turns the helper off, then deletes the app's data
and logs. After that, drag the app to the Trash and remove the helper's entry
under System Settings → Privacy & Security → Full Disk Access. Every step, and a
Terminal block that does the same, is in [`docs/UNINSTALL.md`](docs/UNINSTALL.md).

---

## Reporting a problem

- **Something broken or missing:** open an issue at
  [github.com/william-agehouse/network-crm-native/issues](https://github.com/william-agehouse/network-crm-native/issues).
  Say what you did, what you expected and what happened, and which macOS you
  are on. Never paste your own contacts or messages into an issue: issues are
  public.
- **A security problem** (anything that could expose someone's data or let
  another program act through the app): do not open a public issue. Report it
  privately with GitHub's private vulnerability reporting: the repository's
  **Security** tab, then **Report a vulnerability**. Only the maintainer sees it.

---

## For developers (and their AIs)

[`ARCHITECTURE.md`](ARCHITECTURE.md) is a full written description of the data
model, the importer contract, the Ask tool-use design, the two deployment
shapes and their trust boundaries, and a recipe for adding a new import source —
written so you (or an AI model) can understand and extend the codebase.
[`docs/EDITING.md`](docs/EDITING.md) is the practical half: how to run it from the
source code, where to make a change, and how to check it before you rebuild the app.

**The NC numbers.** Comments, tests and the changelog carry tags like `NC-028`.
Each one refers to an entry in a list of bugs and decisions that is not in this
repository. The comment beside a tag says what you need to know; the tag only
says where the reasoning came from.

### CLI (advanced)

The app covers everything below with buttons, but each step also has a CLI:

```bash
.venv/bin/python3 crm/linkedin_import.py                                     # LinkedIn via API (connections+DMs)
.venv/bin/python3 crm/seed_from_csv.py ~/Downloads/Connections.csv           # …or seed from a CSV export
.venv/bin/python3 crm/merge_scraped_facts.py --pull                          # pull LinkedIn profile facts (scraper)
.venv/bin/python3 crm/imessage_import.py --chatdb ~/Desktop/chat.db          # import iMessage
.venv/bin/python3 crm/whatsapp_import.py --db ~/Desktop/ChatStorage.sqlite   # import WhatsApp
.venv/bin/python3 crm/enrich_facts.py                                        # tag locations/roles (free)
.venv/bin/python3 crm/enrich_facts.py --llm                                  # + AI enrichment
.venv/bin/python3 crm/search_cli.py "acme"                                   # keyword search
.venv/bin/python3 crm/search_cli.py --semantic "designers to reconnect with"  # local semantic search
.venv/bin/python3 crm/export_data.py --format md                             # export (md|csv|json)
.venv/bin/python3 -m unittest discover -s tests -t .                         # run the test suite (install requirements.txt first)
```

Each of these prints its options with `--help`, and stops without running if
given one it does not know. `--dry-run` counts what would change and writes
nothing on the LinkedIn, iMessage and WhatsApp imports and on
`merge_scraped_facts.py`. `seed_from_csv.py` and `enrich_facts.py` have no
preview: they write on every run.

## Layout

```
crm/                    all application code (stdlib + SQLite, no web framework)
  app.py                the HTTP layer: server, route tables, static files
  api_*.py              one module per domain behind those routes — contacts,
                        serialize, views, import, ask, messages, dates, notes,
                        lists, briefs, duplicates, identifiers, instance, guards
  chat.py               the Ask chatbot's LLM tool-use brain
  db.py                 SQLite schema + data access for crm.db
  archive.py            full message text + conversation chunks for search
  photos.py             local contact photos (data/photos/, source-priority guard)
  importers.py          registry of every import/enrich source (label, help, argv)
  search_cli.py         local keyword + semantic search
  *_import.py           one importer per source (contacts, imessage, whatsapp, …)
  whatsapp_photos.py    profile-photo importer (WhatsApp Desktop's Media/Profile)
  enrich_facts.py       enrichment passes (locations, roles, topics — rules + LLM)
  merge_scraped_facts.py  pull + merge LinkedIn profile facts from the scraper machine
static/                 the front-end (no build step): index.html shell
  app/                  Preact ES modules — main/people/ask/lists/data/settings/intake/ui/api, and small helpers
  vendor/               preact+htm standalone (~13 KB, vendored — no npm, no CDN)
  css/ fonts/           design tokens (light+dark) + self-hosted Fraunces (OFL)
tests/                  stdlib unittest suite, one file per area (install requirements.txt first: one test checks for it)
                        helpers.py must be imported before any crm module
packaging/              builds Network CRM.app, its disk image and the copying helper
native/                 the app's window (SwiftUI), compiled into Network CRM.app
docs/                   how to change the code, how to remove the app, the design brief
deploy/                 the optional second Mac that reads LinkedIn profiles
data/      (gitignored) crm.db, archive.db, photos/, *.npz, exclude_list.txt
secrets/   (gitignored) .env, gmail_credentials_*.json, gmail_token_*.json (chmod 700)
```

## License

The app's own code is MIT; see [LICENSE](LICENSE). It includes four things other
people wrote: Preact (MIT) and htm (Apache-2.0), which draw the interface, the
Fraunces typeface (SIL Open Font License), and seven icons redrawn from Feather
Icons (MIT). Their notices are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
