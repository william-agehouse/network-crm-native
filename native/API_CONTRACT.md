# Network CRM engine: API contract for the SwiftUI client (milestone 1)

Written 2026-10-14 for the native app (NC-330). Read from the Python source (`crm/app.py`,
`crm/api_*.py`, `crm/chat.py`, `crm/db.py`) and the web page (`static/app/*.js`), then **checked
against live engines**:

- An engine on a real database: GET only, shapes only (nothing copied).
- Port **8768**: empty scratch engine (its own temporary data folder), every write below was run there with
  made-up people (Ada, Grace, Alan). AI is off there (`/api/status.ai == false`), so Ask ran on the keyword engine.
  The AI-path shapes (`spend`, `trail`, `question`, `shape.caps`) come from `crm/chat.py` plus real saved
  answers read through `GET /api/chat/N/history` on the real-database engine.
- Line numbers are from the code on 2026-10-14 and can drift by a few lines as the files change.

**Updated 2026-10-17** for the engine changes listed in section 0 below. The sections they touch carry the
NC number beside the new rule, and the old rule is kept (marked fixed) where a client written against it would
otherwise be confused. That update was written from the engine changes as they were specified, then checked
only by reading this app's code and building it: no engine with the new behaviour was running, so none of the
new answers below was observed live. Where an exact status or sentence was not specified it says so.

Notation: `T?` = key may be missing. `T | null` = key present, value may be null. `""` is the engine's
"nothing" for strings (it almost never sends null). All JSON is UTF-8 with non-ASCII escaped (`\uXXXX`), ints
are plain, floats may print as `1.0`.

---

## 0. Engine changes since the 2026-10-14 mapping

| NC | What the engine does now | What this app does about it |
|---|---|---|
| NC-333 | The engine's rule is unchanged: emptying a filled person field saves nothing (`{ok:true, unchanged:true}`) unless the request carries `clear:true`. The fix is the web page's half: send `clear:true` on a deliberate empty, never say "Saved" over `unchanged`. | Already did both. `PersonStore.fieldBody` adds `clear:true` when the trimmed new text is empty and the old text was not; `fieldAnswerProblem` turns `unchanged` into a sentence (the field keeps what was typed and never says "Saved"). Pinned by `SelfTest+Refusals.swift` (no engine needed) and one write check. |
| NC-336 | `POST /api/list/{id}/add` brings back a member who was removed from that list and counts them in `added`. | `ListsAddTo.add` still reads the list when `added` falls short of what was sent and restores through `/member` anyone still missing, so an older engine also works; with this engine it finds nobody to restore. Not simplified away on purpose. |
| NC-337 | An id that is not a person answers **400 naming it** from `POST /api/lists` and `POST /api/list/{id}/add` (it was a 500 `FOREIGN KEY constraint failed`). | The engine's sentence is shown as it is ("Nobody was added to “X”. …", the add popover's red line, the Lists alert, which no longer adds "Try again in a moment" to a refusal). |
| NC-338 | A date must be `YYYY-MM-DD` or the engine answers **400**: a person's `on_date` (`POST …/dates`) and a list's `event_date` (`POST …/meta`). | Only real days were ever sent already (calendar pickers, `ListsDates.isValidDay`). `PersonStore.addDate` now checks again and says why in words before asking. `""` still clears an event date (the web page's own Clear sends it; not re-checked against the new engine). |
| NC-339 | `PUT` and `DELETE /api/note/{id}` for a note that does not exist answer **404** (they answered `{ok:true}`). | A 404 is shown as the engine's sentence, and the person's page is refreshed so the stale note disappears (`PersonStore.editNote`, `deleteNote`). |
| NC-340 | `GET …/episodes` and `GET …/history`: a bad `limit` is **400**, an unknown person is **404**, and a message archive that cannot be read is a **real error** (they gave a 500, or an empty 200). The exact status for an unreadable archive was not specified. | An unreadable archive now reads as the engine's sentence in the Messages tab, in an opened conversation and under Channels (it used to read as "no messages" or a dash). The app only ever sends a whole `limit` from 1 to 200 (20 for episodes, 50, 200 or 1 for history). |
| NC-342 | `POST /api/db-recovery-notice/dismiss` replaces `GET` (a GET changes nothing). `GET /api/onboarding` no longer writes. | "I've read this" posts through `AppModel.dismissRecoveryNotice` and shows the engine's sentence if it fails. The first-run check still asks `GET /api/settings/ledger` (`OnboardingGate`), which answers the same on any engine. `native/tools/standin.py` takes the POST, and a GET of that address changes nothing there, as in the engine. |
| NC-334 | `POST /api/exclude-list/remove` un-hides every person whose name matches, mirroring add (it only edited the file). | Nothing to change in the app: the Hidden popover reloads the list and People after a remove. `SelfTest+People.swift` now expects the person shown again. |
| NC-319 | `GET /api/data/linkedin-wait` → `{ waiting, asked_at, says, row_says?, seconds?, late? }`; `POST /api/data/linkedin-asked {asked}` starts or ends the wait and answers the same shape. The wait ends by itself once a contact from LinkedIn's connections file exists. | Setup has a LinkedIn step (`OnbLinkedInStep`) that opens LinkedIn's data page and posts `asked:true`; People shows `says` in one line above the table, with **Open the LinkedIn row** and **Stop reminding me** (`asked:false`). |
| NC-318 | Nothing new in the engine: `SELF_NAME` and `SELF_EMAIL` are ordinary settings. `SELF_EMAIL` may now hold several addresses separated by commas (NC-328). | Setup asks for both on its second step (`OnbYouStep`), and the Settings box accepts a comma-separated list (`LedgerFormat.check`). |
| NC-303 | A calendar link setting may hold several addresses, separated by commas. | The paste box joins whatever separates them into one comma before saving (`LedgerFormat.pasted`). |
| NC-314 | `GET /api/remove/plan` and `POST /api/remove/everything {confirm}` (the web Settings page's Remove everything). | Not offered here yet; `docs/UNINSTALL.md` explains the by-hand route. |
| NC-305 | `GET /api/contacts` accepts `sort=messages` (the person with the most messages across every channel first). | Nothing sends it: the People table's Messages column sorts the rows it already holds (it loads everyone with `limit=20000`), and every `sort` this app sends (`closeness`, `quiet`, `recent`) means what it did. |

---

## 1. Transport, headers, and what the engine refuses

Base URL `http://127.0.0.1:<port>` (real engine 8765, `NC_NATIVE_PORT` in development). Use the literal
`127.0.0.1`, not `localhost` (resolves to `::1` first; the engine binds IPv4 only). No token on loopback.
Server: Python `ThreadingHTTPServer`, `HTTP/1.1` keep-alive, every answer has `Content-Length`
(`crm/app.py:391`, `:398`). Requests run in parallel threads, so Stop works while Ask is open.

### Request rules a URLSession client must meet (all verified on 8768)

| Rule | Detail | Source |
|---|---|---|
| `Host` must carry the engine's exact port | `127.0.0.1:8768`, `localhost:8768`, `[::1]:8768`, `LOCALHOST:8768` pass. `127.0.0.1` (no port), another port, `0.0.0.0:8768`, `evil.example:8768` get **403** `{"error":"this app only answers requests addressed to this Mac"}`. No Host at all (HTTP/1.0) passes. URLSession to `http://127.0.0.1:PORT` sends the right Host. Applies to every path including `/` and `/api/photo`. | `app.py:273-298`, `:592-599` |
| Writes: no `Origin` is fine | Absent Origin passes (URLSession sends none). If you send one it must be `http(s)://127.0.0.1:PORT` or `http(s)://localhost:PORT`. Anything else, `null`, `file://`, `app://localhost`, `http://127.0.0.1` (no port) and even `http://[::1]:PORT` get **403** `cross-origin writes are not allowed`. Do not set Origin. | `app.py:659-674` |
| `Sec-Fetch-Site` | URLSession sends none: passes. `same-origin` and `none` pass; **any other value** (`same-site`, `cross-site`, junk) is 403 on every write (`cross-site writes are not allowed`) **and on every GET under `/api/`** including `/api/photo/N` (`other sites cannot load this app's data`). The page itself (`/`) is not checked. | `app.py:601-613`, `:668-673` |
| `Content-Type` on POST/PUT | Absent or empty is accepted. If present, the part before `;` must equal `application/json` (case-insensitive), else **415** `Content-Type must be application/json`. `text/plain`, `application/x-www-form-urlencoded`, `text/json`, `application/jsonx` all 415. **Set it explicitly** (a body-less POST with no type also works, treated as `{}`). | `app.py:424-452` |
| Body must be one JSON object | `[]`, `null`, a bare string: 400 `JSON body must be an object`. Invalid JSON/UTF-8: 400 `invalid JSON: ...`. Empty or whitespace-only body: treated as `{}` (so the handler's own "x is required" error appears). DELETE and GET bodies are never read. | `app.py:444-451` |
| **Send `Content-Length`, never chunked** | The engine does not decode `Transfer-Encoding: chunked`; a chunked body is read as empty `{}` and the call does the wrong thing silently (`POST /api/onboarding` would set `done:false`, `POST /api/contacts` says `name is required`). Use `URLRequest.httpBody = Data`, not `httpBodyStream`. | verified on 8768 |
| Body limit 10,000,000 bytes | Declared `Content-Length` above that: **400** `{"error":"request body too large"}` (not 413). A photo is base64 in JSON, so raw image limit is about 7.4 MB. | `app.py:253`, `:440` |
| Query strings on writes are ignored | `POST /api/contacts?x=1` works. Trailing slash, upper-case path: 404 `{"error":"not found"}`. | verified |
| Only GET, POST, PUT, DELETE exist | `HEAD`, `PATCH`, `OPTIONS`, `TRACE` answer **501 with an HTML body** and `Connection: close`. `GET` of a write-only path and `POST` of a read-only path answer 404 JSON. | verified |
| Timeouts | Web page: 20 s for everything (`api.js:56`), **180 s for `POST /api/chat`** (`ask.js:96`). A long Ask answer legitimately takes well over 20 s: give that one call its own `timeoutIntervalForRequest` of at least 180 s. Timings with a few thousand people: list about 0.1 s (about 2 MB), person 2 ms. First semantic search after engine start: 8 s (loads the model). | `api.js`, `ask.js` |

### Keep-alive trap (verified): a rejected write poisons the next request on the same connection

If the engine rejects a write **before reading its body** (415 wrong Content-Type, 403 Host/Origin/Sec-Fetch,
and also any `DELETE` that carried a body), the unread body bytes stay on the socket. The **next request on
that connection** then fails with `400` (or `501`) **and an HTML body**, and the server closes the connection.
The one after that works. Seen with both a 415 and a 403. A 404 on an unknown path, a 400 from a handler and
the "body too large" 400 do read/skip correctly and do not poison. So: after any non-2xx on a write, expect
that the next call may come back as an HTML 400/501, and **retry it once**. Never `JSONDecoder` an error
body without checking `Content-Type: application/json` first.

### Error shape

Every engine-made error is `{"error": "<sentence>"}` with the HTTP status. The web page throws
`data.detail || data.error || "<status> <statusText>"` (`api.js:118`); the engine never sends `detail`.
Statuses used: 200, 400 (validation), 403 (host/origin), 404 (unknown id or path), 415, 500 (uncaught:
`{"error": "<python exception text>"}`; list create/add no longer produce `FOREIGN KEY constraint failed`, NC-337), 501 (unsupported method, HTML).
A 400 or 404 from a handler is a finished sentence: show it as it is, and do not offer "try again" (the same request is refused again).
Success is always 200, even for creates (no 201).

---

## 2. Shared types

All timestamps are **naive UTC strings** `"YYYY-MM-DD HH:MM:SS"` (notes, messages, chats, lists, `last`,
`first_contact`). Date-only fields (`connected_on`, `on_date`, `event_date`, `when`, `next_occurrence`) are
`"YYYY-MM-DD"`. `last` on real data always has the 19-character form; the web slices `[:10]` and so should you.

```ts
type Channel = "linkedin"|"imessage"|"whatsapp"|"email"|"telegram"|"messenger"|"instagram";
type Counts  = { [K in Channel]: number }              // all 7 keys always present, ints >= 0
type Tier    = "close"|"active"|"loose"|"faint"        // db.strength_label: >=65 close, >=40 active, >=18 loose, else faint (db.py:1838)

// _contact_summary  (api_serialize.py:49)  — People table rows, list members, semantic hits
type PersonSummary = {
  id: number; photo: string /* "" or "/api/photo/{id}?v={mtime}" (relative!) */;
  name: string; company: string; position: string;
  home_city: string; home_country: string; home_metro: string; role_type: string; blurb: string;
  topics: string[];                                    // 0..5 in practice, parsed from JSON, never the raw string
  category: ""|"personal"|"professional"|"both"|"unknown";   // "" for almost everyone in practice
  status: "new"|"described"|"skipped"|"never_met"|"no_context";
  excluded: boolean;                                   // NO `deleted` key in summaries (see traps)
  counts: Counts; last: string /* "" = never */; strength: number /* int 0..100 */; strength_label: Tier;
}

// _contact_detail  (api_serialize.py:214) — everything the person page shows
type PersonDetail = {
  id: number; photo: string; name: string; company: string; position: string; category: string; // as above
  how_we_met: string;
  connected_on: string;         // "YYYY-MM-DD" or ""
  first_contact: string;        // "YYYY-MM-DD HH:MM:SS" or ""  (note the different format)
  linkedin_url: string;
  status: string; excluded: boolean; deleted: boolean;
  source: "linkedin"|"contacts"|"whatsapp"|"manual"|string;
  counts: Counts; last: string; strength: number; strength_label: Tier;
  notes: Note[];                // newest first (created_at DESC, id DESC)
  identifiers: Identifier[];    // sorted by kind then value; usually 0..3
  history_preview: ArchivedMessage[];  // newest first, max 5. NOT read by the web page.
  home_city: string; home_country: string; home_metro: string; topics: string[]; role_type: string; blurb: string;
  facts_source: ""|"linkedin_scrape"|"llm"|"offline"|string;   // NOT read by the web page
  mutuals: { mutual_names: string[]; shared_history: string[]; recent_activity: string };
  dates: DateRow[];             // can run to dozens on one person (calendar meetings)
  story: string;                // never empty-by-design but "" happens
  story_source: "ai"|"auto";
  next_action: NextAction | null;    // null for most people; also null for excluded/deleted people
  brief_meta: { messages: number; channels: number; built_at: string; newer_messages: number } | null;
  strength_parts: { days: number; tenure: number; volume: number; depth: number; reciprocity: number;
                    bond: number; warmth: number;            // floats 0..1, 3 decimals
                    active_days: number; total_messages: number; channels_used: number;   // ints
                    days_since: number | null };              // null when never in touch
}
type Note        = { id: number; body: string; kind: "note"|"how_we_met"|"voice"|string; created_at: string }
type Identifier  = { kind: "phone"|"email"; value: string }   // phones are stored normalized ("+46701740605")
type NextAction  = { kind: "reconnect"; days_silent: number; last_snippet: string /* <=120 chars */; last_channel: string /* Channel or "" */ }
type DateRow     = { id: number; kind: "birthday"|"anniversary"|"meeting"|"custom"; label: string; on_date: string;
                     year_known: boolean; recurring: boolean;
                     next_occurrence: string;   // "" (empty string, not null) for a one-off in the past
                     days_until: number;        // 0 when next_occurrence is ""
                     reason: string;            // finished sentence, see section 6
                     source: "manual"|"calendar"|string }
type ArchivedMessage = { id: number; channel: Channel; direction: 0|1 /* 1 = you sent it */; ts: string; subject: string; body: string }
type List        = { id: number; name: string; created_at: string; event_date: string; venue: string;
                     members: number; invited: number; replied: number; attending: number }   // counts, see section 5
type ChatRow     = { id: number; title: string; created_at: string; updated_at: string; shape: Shape;
                     turns?: number; last_message_at?: string }            // turns/last_message_at absent on single-chat answers
type Shape       = { people: "10"|"25"|"all"; detail: "brief"|"full"; search: "quick"|"thorough" }
```

---

## 3. People

### GET /api/contacts — the table of everyone
Handler `api_contacts.py:81-105`, SQL `db.py:1469-1568`. Used `people.js:649` (also `ui.js:94`, `lists.js:372`, `main.js:388` for lookups).

| Query param | Type, default | Allowed / behaviour |
|---|---|---|
| `q` | string, `""` | Trimmed. Matches `name_norm LIKE %normalize(q)%` OR `lower(company) LIKE %q.lower()%` OR `lower(position) LIKE %q.lower()%`. `normalize_name` = NFC, lowercase, every non-word char becomes a space (`db.py:541`). Consequences: **a query of only emoji/punctuation or only spaces normalizes to `""` and matches everyone** (checked: `q=%20%20` returns the full total); multi-word queries must appear in that order in the name (`king ada` does not find "Ada King"); `zoe` does not find "Zoë" (accents kept, not folded); a `%` or `_` typed by the user is a LIKE wildcard in the company/position half. Not matched: topics, blurb, role_type, notes. |
| `filter` | `"all"` | `all` (active: not hidden, not deleted), `todo` (status `new`, not hidden), `described` (status `described`, not hidden), `excluded` (hidden but not deleted), `deleted`. **Any other value silently becomes `all`.** |
| `location` | `""` | Exact match on `home_metro` OR `home_city` (case-sensitive, `london` finds nothing for `London`). |
| `role` | `""` | Exact, case-sensitive match on `role_type` (the roles in use are all lower-case). |
| `topic` | `""` | Case-insensitive **substring of the raw topics JSON string** (`LOWER(topics) LIKE %t%`): `topic=ai` also matches "maintain"; a quote character matches every person who has topics. |
| `sort` | `""` | `closeness` = every match ranked by strength desc then `name.lower()` asc (Python code-point order, not locale), then cut to `limit`; `quiet` = people with any contact first, oldest `last` first, then name; `recent` = people with contact first, newest first, then name; `messages` (**NC-305, new**) = highest total across every channel first (this app does not send it: its Messages column sorts the rows it holds). **`""` and any other value = LinkedIn `last_contact` desc only (ignores the other six channels): not closeness, and not what the web page shows.** The page always sends `sort`. |
| `limit` | int, `500` | `int(limit or 500)` clamped to 1..20000 (`NC_CONTACTS_MAX`). `0` and negatives become **1**. Non-numeric becomes 500. |

Response `{ contacts: PersonSummary[]; total: number; limit: number }`. `total` = matches before the cut (same
WHERE as the list), `limit` = the clamped value. Checked: no params returns the first 500; `limit=20000` returns all
in about 0.1 s. Hidden and deleted people never appear under `all`.
`Used` (Row, `people.js:428-543`): `id, name, photo, position, company` (via roleStr), `home_metro, home_city, topics,
last, strength, strength_label, counts` (sum only, for "No messages"). Filters use `counts[channel]`, `role_type`,
`home_*`, `topics`. Not drawn in the table: `category, status, excluded, blurb, home_country`.

### GET /api/search/semantic?q=
`api_contacts.py:173-202`, used `people.js:654`, `main.js:409`. Meaning search: **top 30 by cosine score, no relevance
floor** (the scratch engine with 7 people returned all 7 for `engineer`), hidden/deleted people removed.
`q` empty returns `{available:true, contacts:[], query:""}`. Response
`{ available: boolean; contacts: (PersonSummary & {score: number /*3 decimals*/})[]; query: string; message?: string }`.
`available:false` carries `message` ("Semantic search needs a one-time install: ..." or "Semantic search failed: ...") and
the page shows it in a note line above the table (`people.js:871`) instead of an error. First call after engine start is slow (about 8 s).
No other filters apply server-side: the page re-applies location/role/topic filters to these rows itself (`people.js:660-664`).

### GET /api/facets, GET /api/locations
`api_contacts.py:120-146`, `:108-117`. Used `people.js:698-699`, `main.js:364`.
`/api/facets` → `{ roles: string[]; topics: string[] }`: roles = every distinct non-empty `role_type` of visible people
(alphabetical); topics = the **20 most common** topics, most common first (hard cap, not configurable).
`/api/locations` → `{ locations: string[] }`: distinct `home_metro` (falling back to `home_city`), alphabetical, no duplicates.
Both exclude hidden people only (`visible`), not deleted-but-unhidden ones.

### POST /api/contacts — add someone
`api_contacts.py:253-259`. Body `{ name: string }` (required, trimmed; a number is turned into a string, `null`/blank is 400 `name is required`).
Extra keys are ignored. Response `{ id: number }`. **No de-duplication** (the page checks for an exact same name itself, `people.js:945-954`).
Web then optionally POSTs `/field` for company and opens the person.

### GET /api/upcoming?days=
`api_dates.py:319-339`, used `people.js:565` ("Coming up" strip, hidden while a person is open and when `shown` is empty).
`days` int, default **14** (`NC_DATES_LOOKAHEAD_DAYS`), junk or <=0 falls back to the default, clamped 1..365.
Response `{ days: number; shown: UpcomingItem[]; more: number }`, `shown` max **3**, `more` = how many did not fit.
Kinds shown: birthday, anniversary, custom, event (never `meeting`). Two shapes:
```ts
type UpcomingItem =
 | { when: string; days_until: number; kind: "birthday"|"anniversary"|"custom"; title: string /* person's name */;
     contact_id: number; date_id: number; reason: string }
 | { when: string; days_until: number; kind: "event"; title: string /* list name */;
     list_id: number; venue: string; people: number; attending: number; reason: string }   // no contact_id, no date_id
```
Verified on real data (birthday) and on 8768 (event, from a list with `event_date`). Sorted by `(when, title)`.
`reason` examples: `"Birthday tomorrow (turns 40)"`, `"On Tuesday · Café Ada"`.

### GET /api/exclude-list, POST /api/exclude-list/add, POST /api/exclude-list/remove
`api_contacts.py:334-379`, used `people.js:631`, `:304`, `:315`. The "Hidden" pill: the names the importers skip.
- GET → `{ entries: { name: string; in_db: boolean }[] }` (file order; `in_db` false = a name with no matching person).
- add: `{ name: string }` (required, 400 `name required`). Writes the name to `exclude_list.txt` **and hides every contact whose normalized name matches, immediately**. Returns the refreshed list `{ entries, exclude_file_saved: boolean, warning?: string }`.
- remove: `{ name: string }` (400 `name required`) → `{ ok: true, exclude_file_saved: boolean, warning?: string }`. Takes the name off the file **and un-hides every contact whose normalized name matches**, mirroring add (NC-334; before that it only edited the file and Grace stayed `excluded:true`). One person can still be shown again with `POST /api/contact/{id}/exclude {excluded:false}`.
- `warning` (when `exclude_file_saved` is false) is a sentence the page shows instead of its tick (`people.js:307`, `:317`).

---

## 4. A person's page

All `{id}` are positive ints in the path (`\d+`); a non-numeric id is 404 `{"error":"not found"}`. Unknown id is 404 `contact N not found`
(`/history` and `/episodes` too since NC-340; before that they answered an empty 200). Dispatcher `app.py:849-876`.

### GET /api/contact/{id}
`api_contacts.py:239`. Response `PersonDetail` (section 2). Works for hidden and deleted people (`deleted:true`, `next_action:null`). Used by `people.js:1031` and the omnibox chip `main.js:377`.
Fields the page **uses** and where: `name` (1340), `position` (1343), `company` (1346), `blurb` (1349, 1831), `category` (1356, 1481), `excluded` (1358, 1498), `deleted` (1359, 1491),
`photo` (`ui.js:231`), `counts` (1226), `strength`+`strength_label` (1228, 1257), `strength_parts` (1104), `last` (1233, 1272), `connected_on`+`first_contact` (1234, 1275), `linkedin_url` (1205, 1839),
`next_action` (1515), `story`+`story_source`+`brief_meta` (1684-1693), `how_we_met` (1818), `topics` (1810), `home_city/home_metro/home_country/role_type` (1827-1830), `identifiers` (1142, 1202),
`dates` (1570, 2031), `mutuals` (1641), `notes` (2098), `id`. **Never read by the page:** `history_preview`, `facts_source`, `source`, `status` (detail), `first_contact` is read only as a fallback.
Business effects: adding a note flips `status` to `described` and bumps `times_reviewed` (`api_notes.py:62`).

### Edits (the detail pane's editable fields)

| Route | Body | Response / behaviour (verified on 8768) | Source / page use |
|---|---|---|---|
| `POST /api/contact/{id}/field` | `{ field, value, clear? }` | `field` ∈ `name, company, position, home_city, home_country, home_metro, role_type, blurb` else 400 `field must be one of [...]`. `value` is `str()`-ed and trimmed (`42` becomes `"42"`, `null` becomes `""`). `name` empty: 400 `name cannot be empty`. **Anti-clobber: an empty `value` on a field that already has text returns `{ok:true, unchanged:true}` and saves nothing, unless `clear:true` is sent.** Normal: `{ok:true}`. | `api_contacts.py:472-490`. Page: `people.js:1090` (header name/role/company 1341-1346; Details City/Metro/Country/Role type/Blurb 1827-1831). **The page never sent `clear`, and showed "Saved" over an `unchanged` answer**, so emptying a field in the web page silently did nothing: that is NC-333, the web page's half (the engine's rule stands). The Swift client sends `clear:true` when the user deliberately empties a field and treats `unchanged` as "nothing saved" (`PersonStore.fieldBody`, `fieldAnswerProblem`). |
| `POST /api/contact/{id}/howwemet` | `{ text: string }` | `{ok:true}`. Empty text **does** clear it (no anti-clobber on this route); missing `text` also clears. | `:382`; page `people.js:1806` |
| `POST /api/contact/{id}/category` | `{ category }` | Lower-cased then must be `personal|professional|both|unknown` (so `"PROFESSIONAL"` works) else 400 `category must be one of ['both', 'personal', 'professional', 'unknown']`. **`""` is rejected: a category can never be cleared back to none.** | `:262`; page `people.js:1395` |
| `POST /api/contact/{id}/note` | `{ body: string }` | `{ note: Note }` (kind `"note"`). Empty/whitespace: 400 `body is required`. Sets status `described`. | `api_notes.py:53`; page `people.js:2077` ("Noted") |
| `PUT /api/note/{noteId}` | `{ body }` | `{ok:true}`; empty 400 `body is required`. **A nonexistent note id is 404 with a sentence since NC-339** (it used to answer 200 ok). | `:72`; page `people.js:1975` |
| `DELETE /api/note/{noteId}` | none | `{ok:true}` (soft delete). **A nonexistent id is 404 since NC-339** (it used to be 200). | `:81`; page `people.js:1980` |
| `POST /api/contact/{id}/identifier` | `{ kind:"phone"|"email", value, remove? }` | Add or (with `remove:true`) delete. Returns `{ok:true, identifiers: Identifier[]}` (the full new list). Bad kind: 400 `kind must be 'phone' or 'email'`; empty value 400 `value is required`. Phones are normalized (`"+46 70 174 06 05"` is stored `"+46701740605"`; removal accepts either spelling). Emails are kept as typed and **case-variants are stored as two entries**. | `api_identifiers.py:19`; page `people.js:1157` |
| `POST /api/contact/{id}/dates` | add: `{ kind, on_date, label?, year_known?, recurring? }`; remove: `{ remove:true, date_id:int }` | Returns `{ok:true, dates: DateRow[]}` (whole list). `kind` ∈ `birthday|anniversary|meeting|custom` (400 `kind must be one of [...]`); `on_date` required (400 `on_date is required (YYYY-MM-DD)`). `year_known:false` stores unknown year, anything else true. `recurring` defaults by kind: birthday/anniversary yes, meeting/custom no. `date_id` must be a JSON int (400 `date_id must be an integer`). **`on_date` must be `YYYY-MM-DD` since NC-338, else 400** (it used to accept `"tomorrow"`, which then showed as `Date · tomorrow`). Validate in the client anyway, so the person is told in words before anything is sent. | `api_dates.py:224`; page `people.js:1582` |
| `POST /api/contact/{id}/exclude` | `{ excluded: boolean }` (required, 400 `excluded is required`) | `{ok:true, exclude_file_saved:boolean, warning?:string}`. Hides/unhides and mirrors into `exclude_list.txt`. **Truthiness, not type:** `"false"` hides. Send real booleans. | `:318`; page `people.js:1406` (toasts: `d.warning \|\| "Excluded — hidden from review and imports"` / `"Back in your network"`) |
| `POST /api/contact/{id}/delete` | `{ deleted?: boolean }` (default **true**) | `{ok:true, deleted:boolean, exclude_file_saved:boolean, warning?:string}`. Reversible soft delete; deleting also sets `excluded:true` and restoring clears it. | `:404`; page `people.js:1414` (`"Deleted — restore from the Deleted filter"` + go to People; `"Restored"`) |
| `POST /api/contact/{id}/purge` | `{}` | **Irreversible.** Only for already-deleted people else 400 `Delete this contact first. Permanent deletion is only offered for an already-deleted contact, because deleting is what tells the importers to stop collecting them.` Returns `{ok:true, purged:true, messages_removed:number, exclude_file_saved, warning?}`. The name stays on the skip list afterwards (verified). | `:421`; page `people.js:1424` (`Permanently deleted · N messages removed`) |
| `POST /api/contact/{id}/photo` | `{ data: string }` base64, raw or `data:image/...;base64,...` | `{ok:true, photo:"/api/photo/{id}?v={mtime}"}`. Errors 400: `data (base64 image) is required`, `invalid base64: ...`, `not a usable JPEG/PNG/HEIC image` (this one also covers **images under 120 bytes**, which a tiny test image trips). Stored as manual photo, re-encoded by `sips` to JPEG max 512 px. | `:493`, `photos.py:48-56`, `MIN_BYTES=120`; page `people.js:1069` |
| `DELETE /api/contact/{id}/photo` | none | `{ok:true}`; unknown contact 404. | `:513`; page `people.js:1502` |
| `POST /api/contact/{id}/reconnectskip` | `{}` | `{ok:true}`: snoozes the "Going cold" card for 60 days. | `:244`; page `people.js:1524` ("Not now") |
| `POST /api/contact/{id}/skip` · `/nevermet` · `/nocontext` | `{}` | `{ok:true}`, sets status `skipped` / `never_met` / `no_context`. Only the first-run wizard uses `skip`. Not needed in milestone 1. | `:390`, `REVIEW_STATUS_ACTIONS` |
| `POST /api/contact/{id}/brief` | `{}` | Writes an AI story (costs money). No key: 400 `ANTHROPIC_API_KEY is not set`. OK: `{ok:true, story, source:"ai", brief_meta:{messages,channels,built_at,newer_messages:0}}`. Page: `people.js:1674`; `message.includes("ANTHROPIC")` shows "AI stories need an ANTHROPIC_API_KEY — add one in Settings". | `api_briefs.py:74` |
| `POST /api/contact/{id}/draftmessage` | `{}` | Costs money. No key: 400 same text. OK: `{ message: string; meta: {messages, channels, built_at, newer_messages?} }`. Page `ui.js:435`: no key shows "Drafts need an API key. Open Settings." | `api_briefs.py:164-207` |
| `POST /api/merge` | `{ keep_id:int, dup_id:int }` | **Irreversible.** Both must be JSON ints (strings: 400 `keep_id and dup_id must be integers`), must differ (400), must exist (404). `{ok:true, kept_id}`. | `api_duplicates.py:89`; page `ui.js:159` |

### GET /api/photo/{id}
`app.py:637`, `_serve_photo :453`. **Not JSON**: raw `image/jpeg` bytes (always JPEG on disk; `photos.sniff_content_type` can also report png/heic for old files), headers `Content-Type`, `Content-Length`,
`Cache-Control: public, max-age=604800, immutable`; no ETag, **HEAD is 501**. No photo: 404 `{"error":"no photo"}` (`Content-Type: application/json`). Works with or without `?v=`.
The URL in `photo` is **relative** and carries `v` = the file's mtime in whole seconds: prefix the origin, and key your image cache on the full URL. Two replacements inside one second produce the same URL and an `immutable` cache will keep the old picture, so after your own upload append your own nonce.
Photos are typically a few tens of KB. Subject to the Host and Sec-Fetch-Site checks.

### GET /api/contact/{id}/history
`api_messages.py:125-166`, used `people.js:1911`. Query: `channel` (a Channel), `before` (timestamp, **strict `ts < before`**), `since` (`ts >= since`), `limit` (default 50, **clamped to 200**), `threads` (`1` widens to whole group conversations; `0`/`false`/empty off).
Response `{ messages: HistoryMsg[]; has_more: boolean; threads: boolean }`, newest first.
```ts
type HistoryMsg = { id: number; channel: Channel; direction: 0|1; ts: string; subject: string; body: string;
                    group: string /* group chat name or "" */; who: string /* speaker label; "" unless threads=1 and it is a group line */ }
```
With `threads=1`, `who` is a name, or `"Someone you hid"`, `"Someone you deleted"`, `"Someone not in your contacts"`; your own lines have `who:""` and `direction:1`.
**Before NC-340 any error became `{messages:[],has_more:false,threads:false}` with 200**: unknown person, `limit=abc`, locked archive. **Since NC-340: a bad `limit` is 400, an unknown person is 404, and an archive that cannot be read is a real error with a sentence** (exact status for that last case not specified: check when the new engine runs). A person who simply has no messages is still an ordinary empty 200.
The web page passes `before = ts_end + "z"` to make the bound inclusive (a space-separated timestamp followed by `z` sorts after itself) — see section 6.

### GET /api/contact/{id}/episodes
`api_messages.py:169-273`, used `people.js:2064`. Query `limit` (default 25, clamp 100; **a non-numeric value is a 400 since NC-340**, it was a 500 `invalid literal for int()`), `before` (strict `ts <`, applied to the person's 600 newest messages).
Conversation slices: same channel and same group, gaps under **4 hours**, at most 60 messages each, newest episode first.
```ts
{ episodes: Episode[]; has_more: boolean; next_before?: string | null }   // next_before missing/null on empty answers
type Episode = { channel: Channel; ts_start: string; ts_end: string; count: number; sent: number /* of those, yours */;
                 snippet: string /* <=140 chars, "(media)" if no text; prefixed "[Group name] " for groups */;
                 group: string;
                 messages?: {channel; direction; ts; subject; body; group}[];   // only when count <= 6, oldest first, this person's lines only
                 thread_since?: string; thread_before?: string }                 // only for group episodes: episode window +/- 4 h
```
Unknown person: **404 since NC-340** (it was an empty answer). A person with no messages: `{episodes:[], has_more:false}` (and `next_before` absent); an archive that cannot be read is a real error (NC-340). A Mac with no archive file yet is expected to stay an empty answer, not an error (not re-checked). Pagination: pass the previous answer's `next_before` as `before`; page stops when `has_more` is false or `episodes` is empty.
In a sample, most episodes carry `messages` (always with count <= 6), and group episodes are common.

---

## 5. Ask

All AI work happens in `POST /api/chat`. **The scratch engine cannot spend money, but a real one, such as the app's own on 8765, can** (`status.ai == true`): never send a chat to a real engine from a test.

### POST /api/chat
`api_ask.py:909-1151`. Body:
```ts
{ message: string;                    // required, trimmed, else 400 "message is required"
  conversation_id?: number|string;    // int or numeric string. Omitted/null/"" = newest chat (or creates the first one). 0 is NOT "none": it 404s. Non-numeric: 400 "conversation_id must be an integer". Deleted/unknown: 404 "chat N not found".
  run_id?: string;                    // /^[A-Za-z0-9_-]{8,64}$/ — a UUID string works. Invalid or already live = answer still runs but cannot be stopped
  shape?: Partial<Shape> }            // clamped silently; unknown words fall back to the default, never a 400. numbers accepted (25 -> "25")
```
Response (verified on 8768 for the rules engine; AI-engine differences marked):
```ts
type ChatAnswer = {
  reply: string;                                   // markdown-lite; may start with a canned filler line the page rewrites (section 6)
  contacts: AskCard[];                             // already narrowed to people the prose names, max 80 (NC_CHAT_MAX_CARDS)
  trail: TrailEntry[];                             // [] on the keyword engine
  truncated: boolean;                              // answer stopped early
  stopped?: boolean;                               // only present (true) when Stop worked
  spend: Spend | null;                             // null on the keyword engine
  question: { question: string; options: string[] /* 2..4 */; multi: boolean } | null;   // the answer asked something back
  searched: { contacts: number; conversations: number; messages: number };   // "Searched ..." line; messages 0 if no archive
  engine: "llm"|"rules";
  shape: ShapeStatus;
  context: { limit: number; turns: number; total: number; trimmed: number; fed_to_llm: boolean; scope: "conversation" };
  conversation: ChatRow;                           // the chat this turn landed in (has turns, title may be freshly derived)
  conversation_resolved: "requested"|"newest"|"created";
  cards_matched: number; cards_named: number; cards_shown: number;
  cards_filter: "names"|"kept-all"; cards_filter_reasons: ("answer_truncated"|"people_shape")[];
  total_matched?: number;                          // keyword engine only: matches before the people cap
}
type Spend = { rounds: number; input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_write_tokens: number;
               usd: number; price_in_per_mtok: number; price_out_per_mtok: number; model: string }   // usd rounded to 4 decimals; model is the engine's configured model name
type TrailEntry = { tool: string /* find_person|get_contact|search_messages|semantic_search|people_in_messages|search_conversations|mutual_connections|message_stats|ask_the_user|... */;
                    detail: string; matched: number; total: number; label?: string }   // label only for message_stats and ask_the_user
type ShapeStatus = { requested: Shape;
                     effective: { people: "10"|"25"|"all"; detail: "brief"|"full"|null; search: "quick"|"thorough"|null };   // null = control did nothing on this answer
                     options: { people: string[]; detail: string[]; search: string[] };
                     controls: { [k in "people"|"detail"|"search"]: { available: boolean; layer: string; reason: string } };
                     people_cap: number;           // 0 on the AI engine, 10/25/30 on keyword
                     caps: { tool_rounds; max_tokens; search_people_limit; semantic_limit; message_limit; message_people_limit } | null }  // null on keyword
```
- **HTTP 200 does not mean the AI answered.** With a key set but the AI call failing, the engine answers from the keyword engine, sets `engine:"rules"` and **appends `\n\n(AI chat was unavailable: <python error text>)` to `reply`** (`api_ask.py:1019-1026`). The page parses that suffix (`AI_DOWN_RE`, `ask.js:121`) to show a notice. Without a key, `shape.controls.detail/search.available` are false with the reason `"Ask answered from keyword matching, not from the AI engine. This control needs an API key."`
- Keyword engine canned replies (`api_ask.py:439-448`): `"I couldn't find anyone matching that in your network yet."` or `"Here's who I found[ in <Metro>][ around <themes>]:"`. It understands only 30 metro names and 14 theme words (`_METRO_ALIASES`, `_THEME_ROLES`).
- Stopped answer: `reply` = `"Stopped."` or `"Stopped. The N person|people found so far are below."`, `stopped:true`, `truncated:true`, cards kept unfiltered, `spend` still reported (`chat.py:2782-2809`).
- Other canned AI replies: `"I didn't find anything for that."`, `"That question needed too many steps — try asking something narrower.\n\nI stopped after N search rounds. The people found so far are below."`, `"I kept trying to ask you a question instead of answering, so I stopped.\n\nThe people found so far are below."` (`chat.py:2899`, `:3040`, `:3014`).
- `question`: when set, `reply` ends with the question text, `contacts` is `[]`, and the buttons are `options` (multi-select if `multi`; replying = sending the chosen text(s) joined with `", "` as the next `message`). Never two question turns in a row (server rule, `api_ask.py:999`). Limits: question 240 chars, option 60, 2..4 options (`chat.py:1030-1037`).

**`AskCard` is not one shape** (checked on real saved answers): AI-path cards come straight from tool results. Guaranteed on every card: `id, name, company, position, home_city, home_metro, role_type, strength, strength_label`.
Everything else is optional: `category?, topics?, blurb?, reason?, score?, last?, matched?, matching_messages?, channels?, first?, first_ever?, new_in_window?, in_group_threads?, one_to_one?, name_match?, messages_total?, messages_one_to_one?, known_since?, years_known?, last_message?`.
Keyword-engine cards have `topics, blurb, last, reason, score` and **no `category`**. **No card has `photo`, `counts`, `status`, `excluded`**: the web page's Ask rows show initials only. If the Swift Ask rows want a photo, `GET /api/photo/{id}` (404 if none) or look the person up.
Persisted turns keep the same card objects, minus anyone hidden since (below).

### POST /api/chat/stop
`api_ask.py:1154-1174` (registered through `ROUTES`). Body `{ run_id }`. Response `{ ok:true, status:"stopping"|"unknown", live:number }`. `unknown` is normal (the run just finished) and 200; no body and a bad id also answer `unknown`.
Design (page, `ask.js:813-824`): send Stop on a **second connection** while the first request stays open; the first request then returns normally with `stopped:true` and the real bill. Do not cancel the first request's task.
The engine finishes the round it is in before returning ("Stopping" can last seconds).

### GET /api/chats
`api_ask.py:823-854`, used `ask.js:889`.
```ts
{ chats: ChatRow[];          // live chats only, most recently active first; each has turns and last_message_at
  total: number; newest_id: number /* 0 when none */;
  adoption: { adopted: number; conversation_id: number; title: string; created_at: string; updated_at: string } | null;   // one-time upgrade receipt
  limits: { context: number /* 19 */; transcript: number /* 500 */; title_max: number /* 48 */ } }
```
`turns` counts **stored messages** (one question + one answer = 2), and the page labels it "messages". Empty engine: `{chats:[], total:0, newest_id:0, adoption:null, limits:{...}}`.

### POST /api/chats — new empty chat
`api_ask.py:857`. Body `{ title?: string; shape?: Partial<Shape> }` (the page sends `{shape}`). Response `{ conversation: ChatRow /* turns:0 */; created: true }`.
Title is normalised like every title (whitespace collapsed, trailing `?` dropped, cut at 48 chars on a word boundary with `…`, so up to 49 chars). No title = `"New chat"`; the first question names it.

### GET /api/chat/{id}/history  (and legacy GET /api/chat/history = newest chat)
`api_ask.py:1243-1273`, used `ask.js:857`. Response:
```ts
{ messages: ChatMessage[];                       // oldest first
  conversation: ChatRow | null;                  // null only on the legacy route when no chat exists
  conversation_id: number;                       // 0 when none
  total: number; shown: number; trimmed: number; limit: number /* 500 */ }
type ChatMessage = { role: "user"|"assistant"; content: string; created_at: string; contacts: AskCard[]; truncated: boolean;
                     contacts_withheld?: number;                  // only when > 0: cards removed because that person is hidden now
                     searched?: {contacts;conversations;messages}; trail?: TrailEntry[]; spend?: Spend | null;   // only when a receipt was stored: keys absent, not null (spend can be an explicit null inside a receipt)
                     stopped?: true; question?: {question; options; multi} }                                    // only when true / present
```
User turns have `contacts: []` and `truncated: false`. Unknown or deleted chat: 404 `chat N not found`; non-numeric: 404 `not found`.
A restored turn carries **no** `engine`, `shape`, `context`, `cards_*`, `total_matched`.
`trimmed` = turns older than the newest 500; the page says `Ask does not show the N earliest message(s) in this chat.`

### POST /api/chat/{id}/rename
Body `{ title: string }`. Response `{ conversation: ChatRow (with turns), title: string }` where `title` is what was **stored**: normalised as above (300 chars become 48 plus `…`); **an empty title is accepted and becomes `"New chat"`**. Unknown chat 404. Page toasts `Saved as “<stored>”` when it differs from what was typed (`ask.js:1126`). `api_ask.py:874`.

### DELETE /api/chat/{id}
Soft delete. Response `{ ok:true, id, title, turns, purged:false }` (turns stay on disk). Second delete: 404. `api_ask.py:891`. Page: `Chat deleted. N message(s) stay on disk.` / `Chat deleted` (`ask.js:1139`).

### DELETE /api/chat/{id}/history  (and legacy DELETE /api/chat/history = newest chat only)
Empties one chat but keeps the thread. `{ ok:true, cleared:number, conversation_id:number, scope:"conversation"|"all" }`. The web page no longer calls it. `api_ask.py:1276`.

### POST /api/ask  (not used by the page; the keyword engine on its own)
Body `{ query, location?, themes?, shape? }` → `{ contacts: (KeywordCard)[]; parsed:{metro,themes}; query; total_matched; shape: ShapeStatus }`. Documented because `/api/chat` falls back to it. `api_ask.py:215`.

---

## 6. Lists

All writes `{ok:true}` unless noted. `{id}` unknown or deleted: 404 `list N not found`. Source `api_lists.py`, dispatcher `app.py:812-846`.

### GET /api/lists
`{ lists: List[] }`, newest first (id desc). **`members` is a number here.** `replied` is cumulative (counts `replied` + `attending`), `invited` is exclusive, `declined` counts in none of the three (`db.py:2327-2334`). Used `lists.js:295`, `ui.js:377`.

### GET /api/list/{id}
`{ id; name; created_at; event_date: string; venue: string; members: (PersonSummary & { list_status: ""|"invited"|"replied"|"attending"|"declined" })[]; counts: { members; invited; replied; attending } }`.
**Here `members` is an array**, and `counts` is the number source (the page's header text builds from `members.length` plus `counts.invited/replied/attending`, `lists.js:530-533`). Order: undecided (`""`) first, then invited, replied, attending, declined; newest-added first inside each. Hidden-then-deleted people are left out of members and of counts. `GET /api/list/{id}/export` returns a CSV download (`name,role,company,location,status,phones,emails`), not JSON.

### POST /api/lists — create
Body `{ name: string; contact_ids?: number[] }`. Response `{ id; added: number }`. Blank name: 400 `name is required`. `contact_ids` not a list: 400 `contact_ids must be a list`. **An id that does not exist answers 400 naming it since NC-337** (it was a 500 `FOREIGN KEY constraint failed`, and a non-integer item a 500 `invalid literal for int()`; the NC-337 fix is "check the ids first and answer 400 naming the bad ones"); nothing is created. Duplicate names are allowed. Page: `lists.js:307`, `ui.js:388`.

### POST /api/list/{id}/add
Body `{ contact_ids: number[] }` (non-empty, else 400 `contact_ids (non-empty list) required`; numeric strings are coerced). Response `{ ok:true, added:number }` — only people newly inserted. Unknown contact id: **400 naming it since NC-337** (it was a 500 FOREIGN KEY). **Since NC-336 a person who was removed from this list is brought back by `/add` and counted in `added`.** (Fixed. Before that `added` was 0 and they stayed removed, so the web page's reading of `added === 0` as "already holds everyone you selected", `ui.js:391`, was wrong for them; `/member` with a status also still brings them back.)

### POST /api/list/{id}/member
Body `{ contact_id: number /* JSON int, a numeric string is 400 */; status?: string; remove?: boolean }`.
`remove` truthy (also `"false"`!) removes the person (soft). Otherwise `status` ∈ `"" | invited | replied | attending | declined` (else 400 `status must be one of ['', 'attending', 'declined', 'invited', 'replied']`); **a missing `status` means `""`** ("To invite"). Setting a status for someone who is not in the list changes nothing and still answers 200; setting one for a **removed** person brings them back. Page `lists.js:410`, `:416`.

### POST /api/list/{id}/rename
`{ name }` (blank: 400 `name is required`). Page `lists.js:467` (only sends when changed and non-blank).

### POST /api/list/{id}/meta
Body `{ event_date?: string; venue?: string }` — each key optional; **a key that is absent or `null` leaves the field alone**, `""` clears it. The page sends one key at a time (`lists.js:496`). `event_date` must be `YYYY-MM-DD` or `""` (clears) since NC-338; anything else is 400 (it used to store `"soon"`, which the calendar logic then ignored). Dated lists feed `/api/upcoming` as kind `event`.

### DELETE /api/list/{id}
Soft delete, `{ok:true}`; second call 404. People stay. Page: confirm text `Delete this list? The N person|people stay in People.`, toast `List deleted` (`lists.js:555-562`, `:489`).

---

## 7. Status, setup, settings

| Route | Response | Notes |
|---|---|---|
| `GET /api/status` (`app.py:347`) | `{ running: true; mode:"local"; private: boolean; db_path: string; host: string; port: number; instance: string; build: string; ai: boolean; db_recovery: string; counts: { total; new; described; skipped; excluded } }` | Page polls every **15 s while the window is visible** (`main.js:637-647`); a failed call turns the dot to "Server unreachable". It reads only `ai` (Ask, `ask.js:903`) and `db_recovery` (`main.js:642`). `instance`/`build` identify the engine build for the app. `private` is true when the engine answers only on the app's private socket file (NC-330); then `host` is `""` and `port` is `0`. **`counts.total` is every row including hidden and deleted people, and `new` includes hidden ones (so it is a few above `filter=todo`); `excluded` = hidden + deleted. It is not the People count: use `contacts.total`.** |
| `POST /api/db-recovery-notice/dismiss` (`app.py:697`, `:339`) | `{ok:true}` | Clears the "your database was moved aside" notice. **A POST since NC-342** (it was a GET that wrote, so a prefetch could clear the notice unread; a GET of it now changes nothing). Shown as a banner when `status.db_recovery` is non-empty, button "I've read this" (`main.js:706-714`; this app: `AppModel.dismissRecoveryNotice`). Body `{}`. |
| `GET /api/onboarding` (`api_instance.py:609`) | `{ done: boolean }` | **Used to write on first call** (NC-342, fixed): if the flag was never set and the person already had more contacts than the grandfather threshold, it stored `done=1`. It is now a plain read. Empty engine: `{done:false}`. Page shows the setup wizard when `done === false` (`main.js:657-719`); a failure counts as done. This app still asks `GET /api/settings/ledger` (`install.setup_again.done`) instead, which says the same on any engine. |
| `POST /api/onboarding` (`:622`) | `{ done: boolean }` | Body `{ done: boolean }`; truthiness (`"false"` is true; `{}` is false). Used to finish or replay the wizard. |
| `GET /api/settings` (`api_instance.py:300`) | `{ settings: { key:string; value:string; secret:boolean; set:boolean }[]; diagnostics: object }` | Milestone 1 needs only the row `key == "SELF_NAME"` for the Ask greeting (`ask.js:897-902`). **The payload also lists secret keys (masked); never log or store it.** |

---

## 8. Display rules the web page computes itself

Copy these so the Swift client looks and reads the same. Quoted strings are exact (curly quotes and em-dashes included).

### 8.1 Dates and "how long ago"
- **`relDate(s)`** (`api.js:221-231`) for a past timestamp: `d = Date(s[:10] + "T12:00:00Z")`; `days = round((now − d) / 86400 s)`. `s` empty gives `""`; unparseable gives `s[:10]`.
  `days <= 0` → `today` (future dates too) · `1` → `yesterday` · `< 30` → `{n}d ago` · `< 365` → `{round(days/30)}mo ago` · else `{days/365}y ago` with **one decimal under 730 days, none above** (`1.4y ago`, `3y ago`). Never "weeks", never "in N weeks": **the web page has no future phrasing of that kind.**
- **`daysSince`** (`people.js:63-68`): the same formula, clamped at 0, and `null` for empty/unparseable (null and 0 are different: null = never).
- **`whenShort(days)`** (`people.js:596-600`, the "Coming up" chips): `days <= 0` → `today` · `1` → `tomorrow` · else `in {days} days` (no singular/weeks).
- **Server sentences** (`reason`, shown as tooltips and as date chips): `_when_word` (`api_dates.py:48-56`): 0 → `today`, 1 → `tomorrow`, **any other number of days → `on <Weekday>` even 300 days away** (English weekday of the date, e.g. `on Thursday`). Birthday `Birthday {when}` + ` (turns N)` only when `year_known`; anniversary `{label or "Anniversary"} {when}` + ` — N year(s)` when year known; meeting `Seeing them {when}` + ` · {label}`; other `{label or "Date"} {when}`; a one-off already past: meeting `Met {Mon D, YYYY}` + ` · {label}`, birthday `Birthday {Mon D, YYYY}`, anniversary `{label or "Anniversary"} · {Mon D, YYYY}`, other `{label or "Date"} · {Mon D, YYYY}` (`api_dates.py:114-154`). Event rows: sentence-cased `on Tuesday` + ` · {venue}` → `On Tuesday · Café Ada`. Server counts days by the **local** calendar date (`api_dates.py:80`).
- **`fmtDay`** (`api.js:240`): `Mon D, YYYY` in the user's locale from the date at 12:00 UTC (`Oct 14, 2026`); format in UTC to avoid day shifts. Used for note dates, episode dates, event lines, `built <date>`.
- **`fmtMonthYear`** (`api.js:233`): `Oct 2026` ("Connected"/"First contact" tile). Takes `connected_on || first_contact`. The web parses a date-only string as UTC midnight (so a west-of-UTC machine can show the previous month on the 1st): use the month as written.
- **Raw timestamps shown as text**: message bubbles show `ts[:16]` (`2026-10-14 16:08`, UTC, not converted) (`people.js:1861`). **Timestamps from the engine are UTC.**
- **Greeting** (`ask.js:60-66`): local hour `< 12` morning, `< 18` afternoon, else evening → `Good morning, {first word of SELF_NAME}` or `Good morning` with no name.

### 8.2 Staleness colours (days since last contact) — `people.js:73-80`
| Tier | Rule | Light | Dark | Used for |
|---|---|---|---|---|
| fresh | `d < 14` | `#2b7d53` | `#4fb583` | "Last contact" cell, "Last touch" tile text colour |
| warm | `14 <= d < 90` | `#866b15` | `#c9a94e` | same |
| cool | `90 <= d < 365` | `#9e5f28` | `#d08a4e` | same |
| cold | `d >= 365` **or never (null)** | `#6c6f73` | `#868890` | same; "No contact yet" is drawn in the muted tone, not in the colour |
(`tokens.css:154-161`, dark `:261-264`.) Empty-cell text colours: `--muted #6d6e73` (dark `#9a9ca3`), `--faint #9b9da1` (dark `#6e7078`).

### 8.3 Closeness (0-100) — `db.py:1827-1846`, `people.js:37`
- Bands: `>= 65` **close**, `>= 40` **active**, `>= 18` **loose**, else **faint**. The engine sends the lower-case word as `strength_label`; labels shown: `Close`, `Active`, `Loose`, `Faint` (Ask rows show the raw lower-case word).
- Table cell (`people.js:533-540`): a dot (tier colour), the label, the number. **If the person has zero messages on every channel the cell reads `No messages` and the score is hidden**, even though `strength` can be above 0 (it happens: depth from notes/how-we-met earns points). The person tile always shows the number and label and adds a `No messages` sub-line when the message total is 0.
- Tier colours (`tokens.css:181-184`): close `#2f8a5b` (dark `#4fb583`), active `#3b6fe0` (`#5b8cf0`), loose `#9a7b18` (`#c9a94e`), faint `#d7d9dd` (`#3d3e4a`); ring track `--line #e8e9eb` (dark `#2e2e38`).
- Avatar ring (`api.js:247-252`, `ui.css:426-429`): conic gradient, fill = `clamp(strength, 6, 100)` percent in the tier colour, remainder `--line`, ring padding 2.5 px; missing tier counts as faint. Ring tooltip (`ui.js:203-207`): `Closeness {n} of 100 — {tier}` or `No closeness score yet` (when strength is 0 and no label).
- "Why this score" popover (`people.js:1103-1129`): title `Why {strength} — bond × warmth`; bars (value clamped 0..1, percent): `Shared days` (`days`, hint `{active_days} days`), `Tenure` (`tenure`), `Volume` (`volume`, hint `{total_messages} msgs`), `Curation` (`depth`), `Reciprocity` (`reciprocity`, hint `one-way` when `<= 0.36`, else percent), `Warmth` (`warmth`, hint: `no touchpoint yet` when `days_since` null, `in touch today` at 0, else `quiet {n} day(s)`). Footnote: `Bond = shared days · tenure · volume · curation, scaled by reciprocity and {channels_used} channel(s). Warmth is recency against your own rhythm — it cools a score but never erases history.`

### 8.4 Initials and avatar colours — `api.js:199-216`, `ui.css:430-441`, `tokens.css:191-194`
- Initials: trim, split on whitespace, take the first **two** words, first character of each, upper-case, `?` if empty. (JS takes the first UTF-16 unit, which breaks for names that start with an emoji or other astral character: real networks hold such names. In Swift take the first `Character`.)
- Hue: `h = 0; for each Unicode scalar cp: h = (h*31 + cp) % 360` (iterate **unicode scalars**, not UTF-16 units). Pair `[h, (h + 40 + (h % 31)) % 360]`.
- Fill: `linear-gradient(135deg, hsl(h1, 22%, 92%), hsl(h2, 22%, 87%))`, initials colour `hsl(h1, 22%, 38%)`, weight 650, font size 38% of the diameter, 2 px border in the panel colour. Dark: saturation 20%, lightness 26% → 20%, ink 80%.
- A photo (`photo` non-empty) covers the initials; if the image fails to load it is removed and the initials show again.
- Sizes used: table row 22 px (ring only in the narrow split view, font 9), person page hero (`--pd-av`, ring), Ask rows 24 px (ring when `strength` truthy), list members 32 px with ring (`--row-h` is 40 px, md = 0.8x).

### 8.5 Channels — `api.js:180-184`, `icons.js:139-142`, `tokens.css:165-171`
Order and labels: `linkedin` LinkedIn · `imessage` iMessage · `whatsapp` WhatsApp · `email` Email · `telegram` Telegram · `messenger` Messenger · `instagram` Instagram. Icon names in `icons.js` are the channel keys (vector glyphs); the LinkedIn quick-action is the text `in`. Colours light/dark: linkedin `#2f6fb4`/`#5b9be0`, imessage `#2f8a5b`/`#4fb583`, whatsapp `#1f8a70`/`#3fb59b`, email `#6b5bd2`/`#9184e8`, telegram `#2b8ccc`/`#5bb6e8`, messenger `#7a5bd2`/`#a184e8`, instagram `#c0447e`/`#e0709e`. Note: imessage/whatsapp and email/linkedin are indistinguishable by luminance: always print the channel name beside the dot.
Date icons (`icons.js:161`): birthday cake, anniversary heart, meeting people, custom star, event calendar. Category colours (`tokens.css:174-178`): personal `#2f8a5b`, professional `#3b6fe0`, both `#7a5bd2`, unknown `#9b9da1`, none `#d7d9dd`. Category labels (`api.js:188-190`): `Personal`, `Professional`, `Both`, `Not sure` (for `unknown`); the tag on the person is **not drawn for `""` or `unknown`** (`people.js:1356`).

### 8.6 People: sort, filters, views, columns, counts
- **Sorts** (`people.js:96`), request param → label: `closeness` **Closest first** (default), `quiet` **Longest quiet first**, `recent` **Recently active**. The page **always sends `sort`** (`people.js:647`).
- **Client-side re-sort**: after the fetch the page re-sorts: for `closeness` by strength desc keeping the server's name order for ties (`people.js:667-670`); for `quiet`/`recent` it re-sorts by the `last` string (people with a `last` first; ascending for quiet, descending for recent; ties keep the strength-desc order, which differs from the server's name tiebreak) (`people.js:718-726`). With a search query, rows that also scored in semantic search sort first by `score` desc, then strength; **semantic-only hits are appended after all keyword hits** and carry a `~` hint titled `Matched by meaning, not by keyword` (`people.js:510`, `:667-670`).
- **Server filters** (params): `filter`, `location`, `role`, `topic`, `q`, `limit`, `sort` (section 3). The page's pills: Location (`Anywhere`, options from `/api/locations`, searchable), Role (`Any role`, `/api/facets.roles`), Topic (`Any topic`, `/api/facets.topics`, 20 only).
- **Client-side filters** (applied to the loaded rows, **so they force `limit=20000`**, `pageFor` `people.js:708`):
  Last contact (`any time` = `Any time`): `m1` **In the last month** (`d != null && d <= 30`), `q3` **Quiet 3+ months** (`d >= 90`), `q6` **Quiet 6+ months** (`d >= 180`), `y1` **Quiet 1+ year** (`d >= 365`), `never` **Never contacted** (`d == null`) (`people.js:86-94`; `d` = daysSince of `last`).
  Closeness (`Any closeness`): `strength_label` equals Close/Active/Loose/Faint. Channel (`Any channel`): `counts[channel] > 0`.
- **Saved views** (`people.js:107-117`, name — note → params): **Everyone** — "the whole network, closest first" · **Going cold** — "quiet 6 months or more, longest first" (`sort=quiet`, last `q6`) · **Never contacted** — "no message on any channel" (last `never`) · **Recently active** — "last exchange first" (`sort=recent`) · **Inner circle** — "closeness reads as close" (tier close) · **To describe** — "no description written yet" (`filter=todo`) · **Described** — "you have written them up" (`filter=described`) · **Excluded** — "hidden from review and imports" (`filter=excluded`) · **Deleted** — "reversible — restore from here" (`filter=deleted`) · then `Founders`/`Investors`/`Engineers` (note `role is {role}`, param `role`) **only if** `/api/facets.roles` contains `founder`/`investor`/`engineer` (case-insensitive). A state that matches none shows `Custom view`. Applying a view resets every other filter and clears the search (`people.js:815-821`).
- **Default columns** (`people.js:52-59`, widths `view-network.css:266`): Name (2.2fr, min 150) · Role (1.7fr, min 110) · Location (1.1fr, min 80) · Topics (1.3fr, min 100) · Last contact (128 px) · Closeness (132 px). **With a person open (split view)** the table narrows to a rail with only Name + Last contact (128 px) and closeness shows on the avatar ring (`view-network.css:411-415`). Rows are 40 px (`--row-h`).
- **Cells**: Name; Role = `position · company` joined with ` · ` (either may be missing; `roleStr`), else `No role yet`; Location = `home_metro || home_city` else `No location`; Topics = first topic as a tag plus `+N` (tooltip lists the rest) else `No topics`; Last contact = `relDate(last)` in the staleness colour else `No contact yet`; Closeness as 8.3.
- **Count line** (`countState`, `people.js:152-175`), N is `total`: shown == total → `N people` (`1 person`); shown < total → `{shown} of {N} people`; server capped (loaded < total) → `First {loaded} of {N}` plus a `Load all {N}` button; numbers use thousands separators (`toLocaleString`). Bulk button under a narrowed list: `Add these {n} to a list` / `Add this person to a list`; shown only when `q`, a filter, a view or a facet is active.
- **Empty and loading**: skeleton rows while loading; `No one matches. Clear a filter, or add someone.` Search placeholder `Search or describe someone…`; the semantic-unavailable hint is the server's `message`. Right-click menu items: `Open`, `Add a note`, `Draft a message`, `Merge with…`; header facts `{relDate} · {Tier} {n} · {k} msgs`.
- **Hidden pill** (`people.js:862-864`): badge = number of exclude-list entries; panel `Hidden contacts` / `Hidden everywhere. Every import obeys this list.` / input `Full name to hide` / `This name is already hidden.` (case-insensitive check) / tag `not in your contacts` / `No hidden names.` / button `Remove`. Toasts `Hidden` / `Removed`, or the server `warning`.
- **Add person** (`people.js:925-981`): fields `Their name`, `Company (optional)`; before creating, GETs `/api/contacts?q=<name>` and if an exact (case-insensitive) same name exists shows `“{name}” is already in your network.` with `Open them` / `Add anyway`; toast `Added {name}`.

### 8.7 The person page (order, strings, states) — `people.js:2129-2157`
Main column: header (photo, name, Role · Company inline fields, blurb, tags, actions, 4 vital tiles) → next-action card → Story → Timeline. Side column: Details, Mutual connections.
- Header tags: category tag unless empty/unknown; `excluded`; `deleted`. Placeholders `Name`, `Role`, `Company`. Name field: an empty name just reloads (no save). Saves toast `Saved`.
- Actions: `Draft message` (the one filled button), `List` (add to list), `⋯` menu `More actions` with groups **Duplicates** (`Merge with…` / "fold another record into this one"), **Category** (Personal, Professional, Both, Not sure, with check), **Danger**: for a deleted person `Restore` ("bring this deleted contact back"), `Delete permanently…` ("cannot be undone") then `Yes — erase permanently` ("notes, photo and archived messages too"); otherwise `Exclude` ("hide everywhere") / `Unexclude`, `Delete` ("reversible — restore from the Deleted filter"); plus `Remove photo` (toast `Photo removed`). Photo is set by clicking the avatar (`Set a photo for {name}`, toast `Photo saved`).
  Quick links: LinkedIn (`in`, from `linkedin_url`), mail (first email identifier, `Write to {email}`), phone (first phone, `Call {phone}`).
- Vital tiles (`people.js:1225-1303`): **Closeness** (dot, number, label; button `Why this score?`), **Last touch** (`relDate(last)` or `No contact yet`, staleness colour), **Connected** (when `connected_on` set) or **First contact**, value `fmtMonthYear(connected_on || first_contact)` or `Not known`, **Messages** (`total.toLocaleString()` plus per-channel `Name count` dots in channel order, only channels with a count).
- **Next-action card** (`people.js:1512-1553`), only when `next_action` is non-null: tag `Going cold`; line `Last exchange {days_silent} day(s) ago.`; second line `“{last_snippet}” · {ChannelLabel}` (each part optional); buttons `Draft a message`, `Not now` (POST reconnectskip, toast `Snoozed — off the list for a while`, card replaced by `Snoozed. This person is off the list for a while.`). Only one `kind` exists: `reconnect`.
- **Story**: label `Story`, source tag `AI written` (`story_source=="ai"`) else `From your data`, button `rewrite` (or `rebuild` when `brief_meta.newer_messages > 0`, `writing…` while busy). Empty story: `No story yet. Import your conversations in Data. Or write how you met in Details.` Receipt line (`ui.js:333-347`): `From {n} message(s) · {c} channel(s) · built {fmtDay}` and, when `newer_messages > 0`, `{n} new message(s) since`.
- **Timeline** (`people.js:2044-2127`): composer placeholder `Add a note about {FirstName}… (Enter to save)`, button `Note`, toast `Noted`. Entries = notes + episodes + derived events, merged **newest first** by a key that pads date-only stamps to `23:59:59` (so a same-day milestone sorts above that day's first conversation). Page size for episodes **20** (`limit=20`), button `Earlier…` when `has_more` and the last page was non-empty, using `next_before`.
  Note entry: label `how you met` (kind `how_we_met`), `voice note` (`voice`), else `note`; date `fmtDay(created_at)`; actions `edit`/`cancel`, `delete`; edit box label `Note text`, `Save`.
  Episode entry: channel icon + name, `fmtDay(ts_end)`, `{count} message(s)` (+ ` from {FirstName}` for a group), snippet, expand arrow. Opening an episode fetches `GET history?channel=&before=&limit=` — for a **normal** episode `before = ts_end + "z"`, `limit = min(count, 200)`, then keeps only `ts >= ts_start`; for a **group** episode (has `thread_since`/`thread_before`) `before = thread_before + "z"`, `limit=200`, `threads=1`, `since=thread_since` and shows the whole conversation. Small non-group episodes use the inline `messages`; **group episodes never use inline messages** (they would show one voice). Bubbles: sent = `You`, received = `who || FirstName`; empty body `(media)`; bodies over 280 characters clamp with `Show more`/`Show less`; subject in bold. Group note: `The whole conversation — {First}'s {count} message(s) and {others} line(s) from everyone else in {group}.`; truncation: `The last {n} messages of a longer conversation.` when `has_more`.
  Derived events (`people.js:2027-2042`): `Connected on LinkedIn` (source `LinkedIn`, date `connected_on`); every date row that is **not recurring, not birthday/anniversary, and has an `on_date`** becomes an event labelled `label || ("Met in person" if meeting else "Marked date")`, source `Calendar` when `source=="calendar"` else `added`. Birthdays and anniversaries stay as chips only.
  Empty: `No history yet. Import WhatsApp, iMessage or email in Data and this becomes a story.`
- **Details** (`people.js:1790-1845`): rows in order `How you met` (2-line, ghost `Where you met {FirstName}`), `Topics` (read-only chips, ghost `Topics`), `City`, `Metro`, `Country`, `Role type`, `Blurb` (2-line), `Phone and email` (chips with `×`, add via `+ number / email` with a `phone|email` picker, placeholders `+46…` / `name@…`), `Dates` (chips showing `reason`, tooltip `{on_date}` + ` · every year`, add via `+ birthday / meeting` with kinds birthday/anniversary/meeting/`date`, a date input, label (placeholder `where / what` for meeting, `label` otherwise, hidden for birthday), `year unknown` checkbox for birthday/anniversary), `LinkedIn` (link text = URL without scheme and `www.`). An empty value shows the field's own name as ghost text. Click to edit; saving needs a real change; Enter saves single-line fields, Esc cancels; a failed save keeps the draft.
- **Mutual connections** (hidden entirely if all three parts are empty): caption `Mutual connections` + `LinkedIn · unverified`; names joined with `, `; shared history as bullets; `Lately: {recent_activity}`.
- Load failure: `Could not load` + the error text. Loading: skeleton blocks. The prev/next stepper (`‹ 3 / 12 ›`) walks the **currently visible list order**.

### 8.8 Ask page — `ask.js`
- **Empty chat**: greeting (above), text `Find who to invite, who to introduce, or who to contact. Every answer comes from your own contacts.`, intent chips (prefill the composer, never send): `Find introductions` → `Who could introduce me to `, `Overdue contacts` → `Who should I contact this month?`, `Prepare a meeting` → `What do I know about `, `Find expertise` → `Who in my network knows about ` (`ask.js:53-58`). When `status.ai == false`: notice `Ask has no API key. Answers use keyword search.` with action `Open Settings`.
- **Composer**: placeholder `Type a question about a person or a topic`; Enter sends, Shift+Enter or Option+Enter inserts a newline, ignore Enter while an input method is composing; grows to 8 rows. Button `Ask`; while this chat is answering it becomes `Stop` (`Stopping…` once pressed; tooltip `Stop this answer and keep what it has found`); while **another** chat answers it is disabled with `Another chat is still answering. Wait for it to finish.` A failed request puts the question back in the empty composer.
- **Pending line**: `Searching your network` (or `Stopping`) and an elapsed `{n}s` counter computed from the start time, not incremented.
- **Answer text**: a list of rewrites run at render time on every stored reply (`ask.js:104-183`) — remove `^\s*here.?s who i found[^\n]*` entirely; `^\s*i couldn.?t find anyone matching that[^\n]*` and `^\s*i didn.?t find anything for that\.?` → `No contact matches that question. Use other words.`; `^\s*that question needed too many steps[^\n]*` → `This question needs too many steps. Split it into two questions.`; the trailing `\s*\(AI chat was unavailable:\s*([\s\S]*?)\)\s*$` is cut off and becomes a **notice** (below). With no text, no cards, no notice, no failure: `No contact matches that question.` Reply is rendered as markdown-lite (**bold**, *italic*, backticks, `- ` bullets, bare links, blank-line paragraphs). Buttons `Copy answer` (toast `Copied`) and `Open People` (only with cards).
- **Notices** (text, optional action `Open Settings`, optional `Show detail`/`Hide detail` raw text), chosen by regex on the error text (`ask.js:131-168`): `The anthropic package is not installed. Ask used keyword search for this answer.` (+ Open Settings) · `The Anthropic account is out of credit. Ask used keyword search for this answer. Top up at console.anthropic.com and ask again.` · `The Anthropic API rejected the key. Ask used keyword search for this answer.` (+ Open Settings) · `The Anthropic API is busy. Ask used keyword search for this answer. Send the question again in one minute.` · fallback `Ask cannot reach the Anthropic API. Ask used keyword search for this answer.` Request failure turn: timed out → `Ask stopped waiting after 3 minutes. The answer can still be running on this Mac. Reopen this chat in a moment to see if it finished.` with `Stop the answer`; after Stop `You stopped the answer. Reopen this chat to see how far it got.`; any other error → `Ask hit a problem and used keyword search instead. Sending the same question again will hit the same problem.`
- **Provenance line** above each answer (`ask.js:398-479`), shown if any of `searched`/`spend`/`trail` exists: `Searched {contacts} contacts · {conversations} conversations` + ` · {messages} messages` **only when messages > 0** + ` · {n} tool call(s)` (when trail non-empty) + ` · ${cost}` where cost is `usd` with 4 decimals if `< 0.01` else 2 decimals, and, when there is no spend and no trail, a **`Keyword search`** tag. With neither trail nor spend it is plain text; otherwise a toggle that expands rows `{tool} · {detail} · {label or "{matched} matched" or "{matched} of {total} matched" when total > matched}` and a last row `${usd 4dp} · priced at ${price_in} per million tokens in, ${price_out} out · {rounds} round(s) · {model} · {input_tokens} tokens in · {output_tokens} out` + ` · {cache_read_tokens} from cache` when > 0 (prices and thousands separators as in the web page; older answers without prices show none).
- **Quiet lines under an answer** (`ask.js:1315-1355`): `The answer stopped early.` + ` The list below is incomplete.` (cards present) when `truncated` · `The answer lists {shown}. The searches found {cards_matched}.` when `cards_matched > shown` · `Keyword search matched {total_matched} people. The {shown} closest are below.` when `total_matched > shown` · `Ask read the last {context.turns} messages of this chat. It did not read {context.trimmed} earlier messages.` when `fed_to_llm && trimmed > 0` · `One person in this answer is hidden now, so their card is not shown.` / `{n} people in this answer are hidden now, so their cards are not shown.` (restored turns).
- **People the answer names** (`ContactCards`, `ask.js:222-313`): first **8** rows, then `Show all {total}` / `Show fewer`; each row has a tick, a 24 px avatar (ring when `strength` truthy), the name, meta = `roleStr(card) || card.reason || "No role recorded"`, up to three small tags (`home_metro||home_city`, `role_type`, and `+{topics.length}` when topics exist: with `max=1` the topics are never listed), and the lower-case tier word with a 6 px dot in the tier colour. Click the row = open that person. Selection bar (only if more than 1 row or something ticked): `{n} selected`, `Select all` (also expands), `Clear`, `Add to list` (opens the list picker), shift-click extends a range (only adds). Order = answer order.
- **Question buttons** (`AskOptions`): one chip per option; single-select sends on click; `multi` shows a `Send` button that sends the picked options joined by `, `. Once a later user turn exists the options stay visible and inert with the chosen ones highlighted (matched whole, case-insensitive, splitting the reply on commas). A question is "answered" only when the **next** turn is a user turn.
- **Shape chips** (`ask.js:495-510`): three chips, each showing the current value; options (value, label, note): People `10` **10 people** "A short answer" · `25` **25 people** "A longer answer" · `all` **All people** "Name everyone found"; Detail `brief` **Brief** "One or two sentences" · `full` **Full** "A reason for each person"; Search `quick` **Quick** "Fewer searches. It can miss people" · `thorough` **Thorough** "More searches. It takes longer". Defaults `{people:"all", detail:"full", search:"thorough"}`. A chip is disabled when `controls[k].available == false` from the **last answer** (tooltip = its `reason`), or, before any answer, when `status.ai == false` for Detail and Search. The chosen shape is the preference for the next **new** chat; opening an old chat restores that chat's own `conversation.shape` without overwriting the preference.
- **Chats rail** (`ask.js:666-725`): `New chat` button; count `{n} chat(s)`; each row = `title` and `chatMeta`: `No messages yet` or `{turns} message(s)` + ` · {relDate(last_message_at || updated_at)}`; empty `No chats yet` / `A chat takes its name from your first question.` / `Send a question below. It starts your first chat.`; adoption note `Ask put {n} earlier message(s) in one chat. It is in the list below.` (dismissible, only when `adopted > 0`). New chat on an already-empty chat does nothing (toast `This chat is empty already`). Footer actions: `Rename` (field `Chat name`, max length = `limits.title_max`), `Delete chat` → confirm `Delete this chat? The {n} message(s) stay on disk.` / `Delete this chat? It has no messages.` with `Confirm`/`Cancel`. A named chat that 404s shows `Ask cannot open this chat. It may be deleted.` with `Start a new chat`. A cut transcript shows `Ask does not show the {n} earliest message(s) in this chat.` If an answer lands after you switched chats: toast `The answer went to “{title}”`.
- The current chat is the one in the route: no id = newest. Never rewrite "newest" into an id.

### 8.9 Lists page — `lists.js`
Status labels (`lists.js:16-22`): `""` **To invite**, `invited` **Invited**, `replied` **Replied**, `attending` **Going**, `declined` **Declined**. Row second line: `{n} person|people` + ` · {invited} invited` + ` · {replied} replied` + ` · {attending} going` (each only when > 0). List icon rule (`lists.js:46-57`): a **leading emoji (or flag) followed by whitespace and a non-empty name** becomes the icon and the displayed title drops it; otherwise a neutral diamond glyph and the whole name. Create row: field `New list — e.g. Team dinner`, button `Create`, count `{n} list(s)`, empty `No lists yet` + hint `Start a name with an emoji and it becomes the list icon.` + `Name a list above to create it. You can also save an Ask answer as a list.` Detail: editable name, `{n} person|people · ...`, `Event date` (date input) and `Venue`, `Delete list`; add-member field `Add a person to this list` (searches `/api/contacts?q=`, shows 5 not already members, each `add`); member row: avatar with ring, name, `roleStr · metro/city` or `No role yet`, five status chips, a write-message button, a remove button; empty list `This list is empty` / `A list is a subset of your people. It never changes the records themselves.` / `Add a person in the field above. You can also save an Ask answer into this list.` No selection: `Select a list` / `A list is a group you curate: an event roster, a shortlist, a segment. Your people stay in People — a list only points at them.`
**Add to list popover** (`ui.js:373-421`): heading `Add {n} person|people to a list`, existing lists each with `{members} person|people` and `add`, a field `Name a new list` + `Create`. Toasts: `Added {added} person|people to “{list}”` + ` · {skipped} already in the list` (skipped = ids sent − `added`), or `“{list}” already holds everyone you selected` when `added == 0` (a removed member counts as added since NC-336; before that this line was wrong for them). Creating a new list sends `{name, contact_ids}`.

### 8.10 Shell and global
Tabs `People`, `Ask`, `Lists`, `Data` (⌘1-⌘4) and a Settings gear (`main.js:17-22`); status dot tooltips `Runs on this Mac. Your data stays here.` / `Server unreachable`. Database recovery banner: the `db_recovery` sentence, button `I've read this` (a POST since NC-342). Toast = one line, 2.6 s, announced to the screen reader. Dark mode follows the same token set (hex values above).

---

## 9. Live checks that differed from what the code suggested

1. **Ask cards are heterogeneous** (section 5): the code in `api_ask.py` only shows the keyword engine's card; real AI answers have 4+ distinct card shapes, all lacking `photo`/`counts`/`status`.
2. *(Fixed by NC-340.)* `GET /api/contact/{id}/history` and `/episodes` for a **missing person** returned 200 empty, not 404 (the docstrings did not say). `episodes?limit=abc` was a **500**; `history?limit=abc` a silent empty 200. Now 404 / 400 / 400.
3. *(Fixed by NC-339.)* `PUT`/`DELETE /api/note/{id}` for a **missing note** answered `{ok:true}`. Now 404.
4. *(Fixed by NC-338.)* `POST …/dates` **accepted a nonsense `on_date`** (`"tomorrow"` stored); the handler's `ValueError` branch only fired for kind/emptiness. Same for list `event_date`. Now both are 400.
5. *(Fixed by NC-337.)* `POST /api/lists` with an **unknown contact id** was a **500** (`FOREIGN KEY constraint failed`), not a 400. Now 400 naming it.
6. *(Fixed by NC-336.)* `POST /api/list/{id}/add` **could not re-add a removed member** (`added:0`), although the docs and the page treat 0 as "already in". Now it restores them and counts them.
7. `GET /api/status.counts.total` is **not** the People total (it includes hidden and deleted rows).
8. `app.py:369` says `DELETE /api/db-recovery-notice`; the route was `GET /api/db-recovery-notice/dismiss` and is `POST` of that address since NC-342.
9. *(Fixed by NC-342.)* `GET /api/onboarding` and `GET /api/db-recovery-notice/dismiss` **wrote**. Neither does now.
10. The Host check accepts `[::1]:PORT` but the Origin check rejects `http://[::1]:PORT` (its allow-list is built as `http://::1:PORT`).
11. A rejected write leaves its body unread and breaks the next request on a kept-alive connection (section 1).
12. Photo upload: a valid PNG under 120 bytes returns "not a usable JPEG/PNG/HEIC image" (size, not format).
13. `limit=0` returns 1 row, not 0 and not the default.
14. With **no `sort`**, `/api/contacts` orders by the LinkedIn column only; the "recent" in the code comment is not the same as `sort=recent` (all seven channels).
15. `/api/search/semantic` always returns 30 rows with no relevance cut-off.
16. Deleted/hidden people are `excluded:true` in `PersonSummary` but only the **detail** payload has `deleted`; the `filter=deleted` list rows cannot tell you which view you are in except by the filter you sent.

---

## 10. Traps for the Swift author (short list)

1. Always send `Content-Type: application/json` and a `Content-Length` body (`httpBody`, never a stream); send **no** Origin or Sec-Fetch-Site; use `127.0.0.1:PORT`.
2. Retry once after a failed write: the next call on that connection may come back as an HTML 400/501.
3. Never decode an error body as JSON without checking the content type (501 and the poisoned 400 are HTML; photos are image bytes).
4. `photo` is a relative URL with an mtime query; cache by full URL; 404 means none.
5. People sort: send `sort=closeness` explicitly. Omitted = LinkedIn-only recency.
6. Send `limit=20000` to load everyone (default is 500; `0` means 1). Client-side filters need everyone loaded.
7. `status.counts.total` is not the People count; use `contacts.total`.
8. `strength > 0` with no messages: the table shows `No messages`, not the score.
9. `members` is an int in `/api/lists` and an array in `/api/list/{id}`; `replied` is cumulative.
10. Empty-string fields: to clear a person field send `clear:true` (still true; NC-333 is the web page learning it) and treat `{unchanged:true}` as "nothing saved", never "Saved"; `howwemet` clears with `""`; a category can never be cleared; `exclude`/`delete`/`remove`/`done` use truthiness, send real booleans.
11. *(Fixed by NC-334.)* `exclude-list/remove` used to leave the person hidden. It un-hides every matching person now, mirroring add.
12. *(Fixed by NC-336.)* Removed list members used to come back only through `/member`, not `/add`. `/add` restores them now and counts them; this app still falls back to `/member` for an older engine.
13. Ask: `engine:"rules"` plus a `(AI chat was unavailable: ...)` suffix is a degraded answer inside a 200. `spend`/`trail`/`searched` are absent (not null) on old stored turns, `spend` is `null` on live keyword answers. Cards are optional-everything beyond 9 keys.
14. Stop = a second request with the same `run_id`; leave the first request running.
15. Timestamps are UTC naive strings; `before` is strict, append `"z"` for inclusive; the person page's two date formats differ (`connected_on` date-only vs `first_contact` with time).
16. `dates[].next_occurrence` is `""` (not null) for past one-offs; `strength_parts.days_since`, `next_action`, `brief_meta` are the real nulls.
17. Searching with only emoji or punctuation matches **everyone**; accents are not folded.
18. Initials and avatar hue must iterate Unicode scalars/Characters, not UTF-16 units, or names starting with an emoji break.
19. *(Fixed by NC-342.)* `GET /api/onboarding` and the recovery-dismiss GET used to change state. The first is a plain read now; dismissing the notice is `POST /api/db-recovery-notice/dismiss`, and a GET of it does nothing.
20. Date phrasing differs by surface: `relDate` (`3d ago`, `2mo ago`, `1.4y ago`), `whenShort` (`in 5 days`), server sentences (`on Thursday`). There is no "in 2 weeks" anywhere.
21. A date is `YYYY-MM-DD` or a 400 (NC-338): a person's `on_date`, a list's `event_date` (`""` clears the latter). Check it in the client first and say why in words.
22. A 400 or 404 from the engine is a finished sentence (NC-337 to NC-340: unknown people on lists, bad dates, a missing note, a bad page size, an unknown person on history or episodes, an unreadable message archive). Show it as it is; do not decode a blank or offer "try again". After a 404 on a note, refresh the person: the page was showing something that is gone.
