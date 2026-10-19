# Changelog

What has actually changed. It exists so that a rebuilt surface is never left
unrecorded (NC-001).

The version the app reports lives in two places that must agree —
`crm/updates.py`'s `CURRENT_VERSION` and `CFBundleShortVersionString` in
`packaging/appify.sh`. A test enforces that; don't bump one alone.

## 1.0.0 — 19 October 2026

The first release, at
[github.com/william-agehouse/network-crm-native/releases](https://github.com/william-agehouse/network-crm-native/releases)
with `NetworkCRM.dmg` attached. Everything below is in it.

### One app, private to its own window (NC-330, 2026-10-17)

- **Network CRM is now the native window**, drawn with Apple's own toolkit. It
  starts its engine itself when it opens and talks to it through a private file
  that only your Mac account can open. The engine opens **no network address**:
  no browser, no web page and no other account on the same Mac can reach your CRM
  while it runs (NC-322). Running it from the source code still uses an address
  on your own Mac, for developers.
- **Quitting stops everything, and so does a force-quit.** The engine notices its
  app has gone and stops, with everything it started. The separate **Quit
  Network CRM** app is gone, because there is nothing left for it to stop.
- **The web page is no longer inside the app**, and nor is the window that showed
  it. Three problems went with that window: export links that saved nothing
  (NC-331), a photo button that opened no file panel (NC-332), and one frame of
  the wrong theme at launch (NC-374). The native window saves exports with a Save
  panel and picks photos with a panel that allows only pictures.
- **The first start of a new version says what it is doing**: "Setting up the
  optional parts (Ask with AI, Gmail, Google photos)", for about a minute, while
  it installs them. Later starts skip it.
- **Every build checks itself before it replaces the last one**: the app starts
  its engine on a throwaway folder and the build fails unless the engine answers,
  opened no network port, keeps its file private, stops when the app vanishes,
  and leaves nothing running after a quit.
- An engine left running by an older version is stopped once, after checking it
  really is Network CRM's.
- **Everything the web page had moved over before it went.** Settings ends with
  **Remove everything** in the native window too (NC-314), so the README, the
  uninstall guide and the disk image's Read Me point at a button that exists. The
  Full Disk Access sheet says the imported messages are stored unencrypted
  (NC-324), and the Anthropic key box, in Settings and in setup, says how to cap
  the bill (NC-327).

### Setup installs only the exact packages it was tested with (NC-316)

- **Every package the first start installs is pinned to one version and to the
  fingerprint of its file.** The installer refuses a file whose fingerprint
  differs, and installs only ready-made packages, never building one from source,
  so a tampered or substituted package cannot run with access to your messages.
  Meaning-based search, installed from Settings, works the same way.

### Paste a list keeps its promises (NC-361, NC-362, NC-365 to NC-368)

- **Names in Hidden contacts are refused**, as every other import already did,
  and the receipt counts them (NC-361).
- **"Founder at Acme" or "CEO" on its own line is a job, not a person.** It is
  shown as a guess to confirm instead of being saved as someone you typed
  (NC-362).
- **Limits say what to do, not which setting to raise**: "Up to 60 people per paste.
  Paste a longer list in parts." None of those settings had a box (NC-365).
- **The page-reading limit counts**: a limit of 0 reads nothing, and each paste
  stops at its limit (NC-366).
- **A person that could only half be saved is taken back out**, and counted once,
  as failed (NC-367).
- **Receipts add up when the AI joins or splits lines**, and a record with no
  name says it needs one (NC-368).

### Imports and settings (NC-364, NC-369 to NC-373, NC-375)

- **The AI spend cap and the two token prices have boxes in Settings**, with
  limits and plain labels (NC-364).
- **LinkedIn's client ID and secret have boxes on the LinkedIn row**, the secret
  hidden as you type (NC-371).
- **Read shared history has a button** on the LinkedIn profile facts row, with
  what it costs, and it stops at the spend cap (NC-372).
- **The writing-style receipt no longer prints a negative count**, and says so
  when the message archive could not be read instead of reporting 0 (NC-369).
- **Calendar events stored in world time land on your local day**, and a
  several-day event that was shortened is counted in the receipt (NC-370).
- **Error messages are one plain sentence**, without addresses, codes or
  "Internal Server Error"; the detail goes to the log (NC-363).
- **The ring around a picture-less person is visible** in both themes (NC-373).
- **The window builds with the toolkit you have** when the macOS 26.5 one is
  missing, and says which (NC-375).

### Counts say what they count, and a delete cannot lose messages (NC-087)

- **"Where this came from" names what each number counts**: messages, phone
  numbers and emails, dates, photos or people, and out of which group. Two meetings
  with one person read as 2 dates, not 2 people.
- **Deleting someone for good removes their messages first, or nothing at all.**
  It used to remove the person and then try their messages, and when an import
  was busy with the message archive it left them behind, counted everywhere and
  shown nowhere. Now the person stays in Deleted and the app says to try again in
  a moment.
- The command-line summary counts the same people as the People page, instead
  of everyone, hidden and deleted included.

### Network CRM Native, a second app drawn with Apple's own toolkit (in progress)

- **A Mac app with no web page inside** (NC-330), built beside the current one in
  `native/`. It uses the same engine and the same data, so both show the same people.
  If no engine is running it starts one from the installed Network CRM, and on quit it
  stops only an engine it started.
- **First four screens:** People (everyone in one native table, with search, sort,
  saved views and filters), a person's page, Ask with its saved chats, and Lists.
- **Then everything else the web page has** (NC-345): Data and Settings as pages at the
  foot of the sidebar (⌘, opens Settings), the Full Disk Access, Gmail and paste-a-list
  sheets, first-run setup, ⌘K, merging two people, and the pieces the first four screens
  lacked (hidden names, Add Person, Coming up, ticking people in an answer, member notes).
- **23 bugs found and fixed before anyone else uses it** (NC-357). The ones you would
  have hit first: opening Network CRM while this app ran was refused; one slow answer from
  the engine wiped the screen and what you were typing (now a banner says so, and goes when
  the engine answers); Ask's Rename and Delete did nothing; adding someone to a list from
  their page could reset them from Going to To invite.
- **Picking someone no longer hides the names** (NC-360). The table beside their page
  slid under the see-through sidebar, because its columns kept the widths they had across
  the whole window. They now shrink to fit what shows, and the page leaves the table room
  for Name, Closeness and Last contact.
- Apple-chip Macs on macOS 26 or later. **Corrected 2026-10-17:** this said "not yet in
  the build anyone else gets". It is now the build: see "One app, private to its own
  window" above.

### Before publishing: a quieter helper, licence texts, and no developer tools (2026-10-17)

- **The copying helper writes less to your disk** (NC-223). It used to rewrite its
  bookkeeping file every two minutes even when nothing had changed, and its log grew
  without limit. Now it writes only when something changed, and its log stays under 1 MB.
  A new helper needs Full Disk Access granted again, so the Data page in both apps says
  "A newer copy came with this version of the app" with a **Set it up again** button.
- **The bundled Python's parts carry their licence texts** (NC-249), not just their names.
- **A short settings template you can see in Finder** (NC-299): `env.example`, 25 lines,
  in place of the hidden `.env.example`.
- **No other product is named as a design model** (NC-290), in code, docs or the disk
  image's read-me.
- **The public copy carries no developer tools** (NC-290): the coding-assistant notes and
  settings, the `tools/` folder and the finished plans are not in the public copy. The
  rules they held now live in `docs/EDITING.md`.
- **Pasting a list saves only on a real yes.** Its review step treated `"false"` sent as
  text as a yes; now only `true` saves.
- **README**: the build steps include the Python download the app carries, it no longer
  claims the app checks for updates once a day (nothing checks by itself), and it says
  what `native/` is and what it needs.

### Ready to hand out: 47 fixes (2026-10-17)

- **Setup asks who you are, then starts your LinkedIn file** (NC-318, NC-319, NC-307).
  Your name and email come first, so every import knows which side is yours. The next
  step opens LinkedIn's data page so the connections file (it arrives by email in a day
  or two) is on its way while you finish. Until it is imported, People says it is
  waiting and where it goes. Before the step that looks for files, setup warns that
  macOS may ask whether "python3" may read a folder, and that this is the app's own
  engine asking (NC-320). Both apps.
- **Settings ends with Remove everything** (NC-314). It turns the copying helper off
  first, then deletes the app's data and logs, after you type a word. README, the disk
  image's Read Me and `docs/UNINSTALL.md` say what else to remove by hand.
- **Several of your own addresses, several calendars.** Your email setting takes a list,
  so mail and invites to your other address read as yours (NC-328). A Google or iCloud
  calendar row takes a second link after a comma, and says the Apple Calendar row reads
  every calendar on the Mac (NC-303).
- **People:** removing a name from the hidden list brings the person back (NC-334); a
  new sort, Most messages (NC-305); emptying a field really empties it instead of
  saying Saved over the old text (NC-333); searching only emoji no longer lists
  everyone, and accents are folded (NC-341); dates months away no longer read "on
  Thursday" (NC-343).
- **Lists:** putting someone back on a list adds them and says so (NC-336); a person who
  does not exist or a date that is not a real day is refused with a sentence (NC-337,
  NC-338).
- **Ask** uses Opus 5.5 by default, at 4 and 20 US dollars per million tokens in and out
  (NC-327), and the key box says how to cap the bill. A failed call is written to the
  log, not only the reply (NC-300). Without a key it finds people by city (NC-346), and
  New Chat starts from your saved settings (NC-356).
- **Handing it out:** the app says it needs an Apple-chip Mac on macOS 26 (NC-312); the
  first launch shows its window at once and says it is setting up, with every package
  at a fixed version (NC-316); the Read Me walks through Open Anyway (NC-313); Settings
  says where new versions are published, and stops claiming a daily check that never
  ran (NC-315). Exports save through a Save panel and the photo picker shows only
  images (NC-331, NC-332).
- **Privacy:** run from the source code, the databases are readable by your account
  only (NC-321); the Full Disk Access card and README say the copied messages are not
  encrypted beyond FileVault (NC-324); the built app no longer carries caches naming
  the build Mac's account (NC-310).
- **Speed:** the unanswered-messages list reads an index, 258 to 16 milliseconds on
  a made-up archive of 300,000 messages (NC-227); WhatsApp photos are saved only
  when one changed (NC-224); a rebuild reuses the prepared Python instead of
  writing about 190 megabytes (NC-225).
- Smaller: a refused request no longer breaks the next one (NC-335); a bad page size or
  an unreadable archive says so instead of "no messages" (NC-340); Show in Finder says
  when the Mac did not open it (NC-348).

### New Data and Settings pages

- **The Data and Settings pages are new.** Both were rebuilt from one design (NC-088,
  NC-114) and replaced the old ones (NC-236). Every source is always listed as one row, the rows you use come
  first, and each row says what it gives you, what you have from it now, and either
  what switching it on takes or when it last imported. What needs you is listed once,
  at the top.
- **Everything on the page comes from one answer from the app,** so two numbers on one
  screen can no longer disagree.
- **Credentials live on their source's row,** not in Settings. Settings holds eight
  things the app can do for you, and This install.
- **Nothing was dropped silently.** Everything the old pages let you see or do is on
  the new ones: merging, finding duplicates and exporting from ⌘K and the menus; the
  import time limit; a warning when the check for new files fails; removing a Gmail
  address. What is still missing is tracked as NC-238 and NC-239.

### Ask can search your messages, and no longer gives up while it is thinking

- **Ask can now answer a question with a date in it.** "Which new contacts did I mention
  Acme to in the last month, and did I send anyone the link?" had no route to an
  answer before: the model was never told what day it is, and the only message search
  read conversations glued into blocks, which mix both sides of a conversation (so
  "did *I* send" is not expressible) and carry no per-message date. Three new tools read
  the message rows directly — one that says what the archive holds and over what dates,
  one that finds messages by phrase, date range and direction, and one that groups the
  same filter into a list of people, so "who is new to this topic" takes three searches.
- **"New" is answered in one query instead of one per person.** A first-match date inside
  a date range is inside the range for everybody, so it cannot tell you who is new — and
  the model, correctly not trusting it, ran an extra search per person to check them one
  at a time. Each person now comes back with their earliest matching message across the
  *whole* archive, so the same answer needs only a few searches and is faster and
  cheaper.
- **The provenance line under an answer told the truth about the new searches.** It read
  "0 matched" beside an answer built from dozens of matches, and a search that matched
  far more people than it returned reported only the number returned — a wrong number,
  not a rounded one.
  It now shows both, and names what the orientation call actually found.
- **A correct answer naming people rendered no clickable cards at all.** The answer
  loop only collected people from the older tools' results. Every name was right and not
  one of them was openable.
- **Ask waited 20 seconds and then said it had failed.** A question needing several
  searches could not finish inside that timeout. It now waits three minutes, and if it
  does run out the message says the answer
  may still be arriving rather than telling you to ask again.
- **And Ask had quietly stopped using the model.** Anthropic accepts at most four cache
  markers per request; the tool loop added one per round, so any question needing four
  searches was rejected outright — and the keyword fallback caught it, answered anyway,
  and nothing said which engine had replied. A good fallback hides a total failure of
  the thing it is falling back from.
- **"Searched N contacts · M conversations" counted conversation blocks** while the
  new tools search the individual messages underneath them. It now says so — and stays
  quiet on an install with no archive rather than reading "· 0 messages".

### Ask says how long it has been thinking

- **"Still working" was a pulsing line and nothing else.** The wording deliberately had
  no "…" because the line pulsed — so anywhere that animation stopped, the screen said
  "Searching your network", perfectly still, and looked exactly like a dead request. It
  now counts the seconds beside it, which needs nothing to be animating. That matters
  more than it used to: an answer is now allowed three minutes.
- The number is worked out from when the question was sent, not counted up, so it is
  right the instant the window comes back however long it was away.
- **With reduced motion turned on there was no signal at all** — one stylesheet switched
  the animation off completely and, in doing so, cancelled the single gentle fade another
  stylesheet had deliberately asked for. Reduced motion gets that fade back.

### Six places locked the database while doing something slow

- **A background import could stop the app writing for up to five minutes.** Only one
  thing can write to the database at a time, and after twenty seconds of waiting the other
  one gives up. Six places did some bookkeeping and then went off to do something slow —
  run an importer, fetch a photo, ask a model — without finishing the bookkeeping first,
  so everything else was locked out for as long as that took. This is what made a question
  vanish with nothing saved.
- **Two folders of WhatsApp exports in one sync meant the second one could not import at
  all.** The sync held the lock its own importer needed. With one folder it happened to
  work — by luck, not design, and a test with one folder would never have caught it.
- **A vCard import held the lock while resizing each photo**, a Gmail import held it while
  fetching every message one at a time, and the contact-enrichment pass held it across
  every model call in the run.
- Three of those fixes are worth having on their own: a Gmail contact now survives a
  failure part way through fetching its history, an email scan keeps the counts it worked
  out even if summarising fails, and the enrichment pass no longer throws away everything
  already paid for when one call near the end fails.
- **A question that failed no longer disappears.** The box was cleared the moment you hit
  send and never refilled, so you had to retype it from the transcript. It comes back now
  — unless you have already started typing a new one.
- No retry was added. The holds are gone, so a failure now means the database really is
  busy, and a retry on top would hide the next mistake of the same kind — which is exactly
  how the cache-marker bug above went unnoticed.

### The Data page says what it can prove

- **The LinkedIn token button opened the wrong page**, and the instructions were one
  sentence. It went to LinkedIn's OAuth tools *index* rather than the generator that
  actually issues a token — both return 200, so nothing would have caught it but reading
  the page. There are now five numbered steps, each preventing a mistake that otherwise
  fails later: the wrong kind of app gives a token that dies in two months instead of a year,
  a missing permission gives one that collects nothing, and a half-copied token comes back
  rejected.
- **You can paste a LinkedIn token into the panel that asks for one.** It used to end
  with "then paste it into Settings → LinkedIn token" — a panel that explains the
  problem, links the token page and names the scope to tick, and then sends you to a
  different screen to actually hand it over. The field is now where the instruction was.
  It writes through the same single writer Settings uses, so it is a second button on one
  path rather than a second path, and the whole page refreshes when a token lands.

- **A source can now be "connected" and "not collecting", and the page says both.**
  The step counter read `N/N sources connected` from `have > 0` — has this source ever
  put a row in the database — so LinkedIn counted as connected after its
  first import, and after its token disappeared. It now reads
  **N/N sources connected · 1 not collecting**. The first number was true; lowering it
  would have traded one wrong number for another.
- **The headline stopped vouching for a number it had not checked.** It used to say
  "*N of them are names you have never actually talked to. Every source is connected,
  so that is the real answer rather than missing data*" — using a completeness claim to
  certify a different figure as trustworthy. It now hedges when anything is not
  collecting, and only certifies when nothing is.
- **"No source needs you now." is the page's answer instead of one component's.** It was
  computed from the scan for new files on the computer, which cannot see a refused macOS
  permission or a missing token — so it rendered directly beneath a panel telling you to
  go and mint one. Every panel that fetches its own status now reports whether it is
  asking for something.
- **A figure names the population it counted.** The LinkedIn panel's coverage bar is
  captioned *profiles filled in by the scraper — not messages*, because it sat directly
  under a headline about messages. "Newest message 22 hours ago" reads "— nothing since"
  when the lane cannot run: it is the date on the newest message body and cannot move
  once collection stops. The download lane says "N people **from the export**",
  which is a different population from the people with a LinkedIn link.
- **Calendar counted birthdays.** Its metric was every row in `contact_dates`, four kinds
  from two sources, so address-book birthdays were evidence a calendar had been
  imported. They are left out now, and an install that has never touched a calendar no
  longer shows Calendar as connected.
- Two units stopped claiming something their query never checks: "people with a current
  role" (no freshness column is read) and "people you've met". The Apple Contacts bar is
  gone — its numerator and denominator were the same query, so it could only ever be full.
- The LinkedIn source row reads **Not collecting** with the reason, instead of "Synced
  today" in the freshest green on the ramp.

### Sync and imports

- iMessage and WhatsApp copies refresh every **2 minutes** instead of every 6 hours,
  and a cycle now writes a few KB to disk instead of megabytes. The copy was always cheap
  (`clonefile` on APFS); the expense was the `wal_checkpoint` that followed it,
  because writing into a clone breaks copy-on-write block by block.
  `imessage_import.snapshot_uri()` reads the write-ahead log instead of folding it in.
- The free offline facts pass now runs after **background** imports too, not only
  clicked ones. It never did before, which would have become "never runs at all" as
  syncing moved to the background.
- The writing-style measurement re-runs weekly on its own. Nothing ever re-ran it, so
  the voice used for AI drafts was frozen at whatever the last manual click produced.
- WhatsApp syncs through the opt-in Full Disk Access helper rather than reaching into
  its own container, so it stops going stale between app launches.
- **Birthdays are imported.** Nothing ever read `BDAY` from a vCard or `ZBIRTHDAY`
  from the macOS address book, so the app held calendar meetings and not one
  birthday. Correcting a date in Apple Contacts now moves the existing row instead
  of leaving two, and a birthday you typed yourself is never overwritten.
- Gmail and LinkedIn sync on a clock, counting attempts rather than successes, so an
  expired token backs off instead of retrying every pass.
- The helper compares each source against **what it last copied from**, not against
  the destination, so an unchanged source costs two `stat()` calls.
- **Apple Contacts imported a tiny fraction of the address book, and reported success.** The flat
  `AddressBook-v22.abcddb` the helper copied is only the *"On My Mac"* store; every
  iCloud, Google or Exchange account keeps its own database under `Sources/<UUID>/`.
  The helper now sweeps those too — copying only what changed, deleting copies whose
  source is gone, and **refusing** rather than silently truncating if it hits a cap.
  Two more defects in the same source: the importer ignored a write-ahead log many times
  larger than the database file beside it, and the auto-sync loop had no fingerprint key for the
  address book, so it re-imported all of it every two minutes, forever.

### Dates

- **"Coming up" above your people.** The next few birthdays, anniversaries and dated
  events, with how long you have. Clicking one opens that person. It is absent — not
  empty, absent — when nothing is due, and it leaves calendar meetings out, because
  your calendar app already shows those.
- One setting controls how far ahead it looks. It is the same field that existed before
  and governed nothing.
- An event three days out reads **"On Monday"**, not "On monday".

### Data page

- Rows for work the app already does itself are gone: recompute closeness, the free
  facts pass, and the writing-style measurement all happen on their own now.
- A source nothing has ever imported says **"Import N cards"**, not "Sync now".
- When the scan for new files on the computer cannot answer, the page says so instead
  of claiming "No source needs you now".
- The three calendar cards no longer say "Nothing is written to your CRM yet" next to
  a button that writes.
- The WhatsApp row no longer credits "the iMessage helper".
- Each source shows what it actually bought you, rather than only that it ran.
- **A source that already updates itself stops asking you to click.** An old calendar
  export could sit under "Needs you" while the calendar link had synced
  by itself that day. A hand-exported file is now suppressed when a sibling source
  imports the same data more recently — and the source moves to "Your sources" rather
  than vanishing off the page.
- A copy the helper is being **refused permission** to refresh no longer says "paste a
  fresh copy into the import folder". There is nothing to paste; it names the one
  grant, in the panel further up the same page.

- **Every source row opens what that source actually brought.** Real counts, per kind —
  "People it first added", "Phone numbers and emails", "Photos still in use", each as
  how many of how many — and, where the schema genuinely cannot answer, a sentence
  saying so instead of a number: a photo a source gave you and then lost is recorded
  nowhere, so the panel says that rather than implying zero.
- **You can paste a list of people in.** A finished, tested importer for pasted text was
  unreachable — no stylesheet link, no route, no menu item. It now reads
  free-form lines into proposals that name what they would write, which fields are
  already filled, and which of your existing contacts each line might be, before
  anything is saved.

### Permissions and privacy

- **A web page can no longer read your CRM.** While the app ran, a page written to target it
  could point its own web address at your Mac and read everything through the export,
  because the app answered any request that reached it. It now answers only requests
  addressed to this Mac (NC-309), and another site can no longer make the browser load
  the app's data as an image or a link either (NC-323). Changing data from another site
  was already refused.
- **The app no longer raises a macOS consent dialog by itself.** Opening the Data page,
  and again once per launch in the background, probed WhatsApp's group container — which
  makes macOS ask whether the program may access data from other apps. Once the helper
  has copied that container the app does not touch it at all, and the question stops
  being asked. It was never going to stop by answering it: macOS does not durably
  remember a grant made to a bare interpreter.
- WhatsApp's **profile-photo cache** moved into the app's own folder with everything
  else, which is what made the above possible — it was the last thing readable only in
  place. **Most avatars exist only as a thumbnail**, so the copy takes both file
  kinds; matching only the full-size ones would quietly collect a handful of faces
  and report success.
- The Full Disk Access flow happens in the app, not in a Terminal, and the app itself
  never holds that permission — only a small separate helper does.
- The installer no longer reports the grant as successful while macOS is still blocking
  it, and the docs no longer tell you to verify it the one way that never works.
- Nobody's messages are imported before they have been agreed to.
- Gmail works with any Google account you connect.
- The LinkedIn scraper no longer saves the signed-in user's own profile picture as other
  people's portraits.
- The app no longer contacts github.com once a day to ask about updates from a
  repository that could only answer 404. With no release repo configured it makes no request
  at all, and the version row says “This copy doesn’t check for updates” rather than
  showing a “Check now” button that cannot work.

### Settings

- **Settings is four kinds of thing, grouped, instead of one undifferentiated list.**
  Identity and credentials at the top, open, because a credential is a decision and not
  a number to tune. **Thirty-four numeric tuning values left the page** — most of them
  deleted outright, since every consumer already read its value from the environment
  with a default. What is left that is a limit you can actually feel (an Anthropic spend
  ceiling, how many days ahead to look for birthdays) stayed.
- **Every on/off setting is now a toggle**, not a text box you type `1` or `0` into.
- Hidden contacts moved out of Settings and into People, where the people are.

### AI answers

- **Ask, the contact briefs and the draft messages all know what you are working on.**
  A box in Settings (the row **Answers that know what you are working on**) takes a
  free-text description of your own goals, and it reaches all three prompts. With it
  set, "who should I talk to this month" stops ranking by closeness and ranks by
  relevance to the stated goal, and says why a close contact who is not part of the
  goal comes lower. Previously the app measured how you *write* and knew
  nothing about what you are *for*. The text lives in the database, not `secrets/.env`,
  so a backup of the data folder keeps it.
- A drafted message is signed by **you**, with your own name.

### Scraper

- The two LinkedIn-extras scripts take **`--source-dir`**, so the scraper runs on one
  Mac. They previously only knew how to `ssh`+`rsync` from a second machine, which meant
  configuring loopback SSH to yourself. And the facts half never needed either — it
  writes straight into the same database the app reads, which was true all along and
  documented nowhere.
- `LINKEDIN_SCRAPE_HANDOFF.md` starts at step one: the terms-of-service warning first,
  then Firefox, the packages, signing in, a three-profile trial run, and what to do when
  LinkedIn shows a checkpoint. It used to begin at step four and the ToS warning existed
  only inside a Python file.
- The background job the helper installs is written for your own home folder.

### Correctness

- A corrupt database moves aside and the app starts fresh, with a notice in the app
  saying where the old file went. It used to raise before the server existed, so the
  app simply never opened.
- LinkedIn portraits outrank address-book and chat avatars; a hand-set photo still wins.
- Phone identifiers dedupe by canonical form.
- The person page no longer 500s on a fresh install, and clicking a person works.
- Calendar imports are idempotent — a re-import cannot duplicate or invent a date.
- Two switches that were read from the environment but had no field in Settings now
  have one. One of them was named in an error message that pointed at Settings.
- A contact's story reads **"Based in Buenos Aires"**, not "Based in Buenos aires".
  `str.capitalize()` lower-cases everything after the first character, so the app was
  destroying a place name the importer had got right, on every contact it applied to.
- **Merging two people keeps where their dates came from.** The merge dropped the
  `source` column, which defaults to `manual` — so it did not lose provenance, it forged
  the highest-trust value, and a merged birthday could never be corrected by a later
  sync again.

### Packaging

- The app icon is an icon. A malformed SVG renders as an error *page*, and `qlmanage`
  thumbnails it and exits 0, so the icon could ship as a rasterized XML parse error. A
  well-formedness check now runs before the icon is built.
- **The window no longer flashes white at every launch.** It painted the system's
  default white for one frame before the web view had anything to show. The window and
  the web view now start on the app's own background in the right theme.
- **The Go menu goes where it says.** It held its own copy of the tab list, which had
  never been updated when a view was deleted — so every item was off by one and ⌘1 went
  nowhere. It is now checked against the app's real tab list by a test, and it can reach
  the paste-a-list surface.
- Stopped shipping 67 MB of Python that never ran. (Reversed in NC-021: the bundled
  Python came back as the first one the launcher tries, so the app needs nothing
  installed on an Apple Silicon Mac.)
- Real icons throughout instead of emoji, and the People list selects rows the way a file list does.
- **The offline shell was missing nine of the app's own files** — `app/menu.js` and
  `app/setup-progress.js` among them. A hand-count missed them; a test now walks the
  real `import` graph from `main.js` instead. A
  hand-kept `SHELL_ASSETS` array stays (an install-time fetch to derive it would make
  SW install itself depend on the network, which the whole file exists to not need),
  but `tests/test_service_worker.py` now parses `index.html`'s links and that import
  graph and fails the moment either disagrees with the array again, in both
  directions — an omission or a stale entry that would reject the whole
  `cache.addAll()`.

### Repo

- `BACKLOG.md` is the durable list of what is wrong. It is private and is not
  published with the code.
- Test data must be invented: no fixture names a real person or carries a real number.
- Deleted: a Procfile launching a bot that no longer exists, and `GET /api/review` — live,
  routed, tested, and called by nothing.
- **An Ask in flight no longer blocks every other write.** Saving anything while Ask was
  mid-answer failed with sqlite3's own `database is locked`, which reads as corruption
  rather than as a database that is briefly busy. The cause was not a timeout — that was
  already 30 seconds — but `handle_chat` holding SQLite's writer lock across the entire
  Anthropic tool-use loop: it wrote the user's turn and did not commit until the answer
  came back, and a 40-round answer outlasts any ceiling worth setting. It now commits
  before calling the model, which also means your question survives a crash mid-answer.
  A `database is locked` that does still happen now says what it means. (NC-052)
- **Every button left on the Data page now says what pressing it costs.** The rows that
  still wait for a click answered "No file needed", which names the absence of a
  requirement rather than the presence of one. They now say the thing you actually need
  to decide with: "Costs money each run", "Needs your other Mac", "Merging cannot be
  undone", "a file, when you ask". The reason also used to disappear once a source was
  configured — the one moment you are about to press the button. (NC-036)
- **The Data page shows one row per real source.** Google Calendar and the hand-exported
  calendar file were two rows for the same events, side by side with the same shape and
  the same stamp. The exported one now folds inside the live link's row rather than
  disappearing — a secret URL can be revoked and a published calendar unpublished, and
  that file is the way back. "Your sources" went from seven rows to six. (NC-032)
- **The app has a boot animation, and you choose which.** The window used to be blank from
  first paint until the app mounted. Five variants now ship together — Dealt, Cold Open,
  Tracking In, Mark Landing, Constellation — and `Settings → Appearance` switches between
  them, with **Play it** to compare them without relaunching. Each is labelled with what it
  costs a fast launch: three are free, two hold the window back by 420 ms and 920 ms, and a
  test fails if a label ever stops matching the real number. Reduced motion collapses all
  five to a short cross-fade and stops the paid ones charging. (NC-049. Later cut to one,
  Constellation, and the picker went with the other four: NC-062.)
- **The app knows who you are.** `SELF_NAME` and `SELF_EMAIL` are set in the app's own
  env, which lives outside the bundle and survives a rebuild. Without them the product
  signs every AI draft "the user", and its importers cannot reliably tell your side of a
  conversation from anyone else's. (NC-054)
- **The "no Python installed" failure is now tested.** Shipping the app means shipping
  its failure path, and that path had never been exercised. The launcher's own version
  gate is now run against stand-in interpreters: Apple's 3.9 Command Line Tools stub is
  refused (it is the last candidate tried, so on a bare Mac it is what gets found), 3.10.0
  exactly is accepted, and both launch paths reach a dialog that names the fix rather than
  dying quietly. (NC-021)
- **A birthday today no longer reads as "tomorrow" after midnight.** The upcoming-dates
  strip asked UTC what day it was while everything you read is a wall-calendar date, so
  between local midnight and UTC midnight — hours every night, depending on your time
  zone — every row was a day out. (NC-055)
- **One blocked source no longer stops the sync helper.** macOS *blocks* rather than
  refuses a read of a folder it has never asked permission for, and the helper ran its
  sources in order — so an unanswered dialog on the second of four meant the third and
  fourth were never reached. Nothing synced at all while the Data page went
  on showing the last good report as current. Each source now runs on its own thread with
  its own ceiling, so a stalled one costs 25 seconds and nothing else.
- **The sync helper reads Apple Calendar, and can no longer be frozen by one source.**
  Calendar was skipped because a check looked in `~/Library/Calendars`, the
  pre-Ventura location, and concluded there was no calendar; modern macOS
  keeps it in a group container. Adding it exposed a worse problem: macOS
  *blocks* rather than refuses a read of a folder it has never asked permission for, so
  one unanswered dialog stalled the whole helper — every source stopped syncing
  and the status report froze silently. A watchdog now reports whatever finished and
  exits. (NC-042)
- **The calendar reads itself off your Mac now.** No secret link to find, no address to
  paste, no switch to turn on: the sync helper copies Apple Calendar's own database
  alongside iMessage and Contacts, and the app reads the copy. It covers every calendar in
  Calendar.app including the Google and iCloud accounts added there, so the older
  secret-link rows fold underneath it instead of sitting beside it. (NC-042)
