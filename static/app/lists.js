// Lists — curated groups of people, each with a lightweight event pipeline per
// member (to invite → invited → replied → going / declined).
//
// The split between records and lists is the mental model here:
// People is the canonical object — every record, one row each. A list is a
// curated subset with a purpose: "Team dinner", "Workshop invitees". So this
// view never tries to be a second People table. It is an index of subsets on
// the left and one roster on the right. Lists are born here or from "Save as
// list" under an Ask answer.
import { html, useState, useEffect, useRef, useCallback } from "../vendor/preact-htm.module.js";
import { api, toast, oneShot, debounce, roleStr, fmtDay } from "./api.js";
import { Avatar, Empty, SkelRows, DraftModal, AV } from "./ui.js";
import { Icon } from "./icons.js";
import { useDrag, DragGhost, DragLive } from "./dnd.js";

const STATUSES = [
  ["", "To invite"],
  ["invited", "Invited"],
  ["replied", "Replied"],
  ["attending", "Going"],
  ["declined", "Declined"],
];
const STATUS_LABEL = Object.fromEntries(STATUSES);

/* ── List icons ───────────────────────────────────────────────────────────────
   Each list gets an emoji icon — free personalisation, and the fastest
   way to tell six rosters apart in a sidebar. The lists API carries no icon
   field (api_lists.handle_lists_get returns id/name/created_at/event_date/
   venue/members/invited/replied/attending), and a response shape is not changed
   for an icon, so the icon comes from the name itself: type "🎉 Workshop
   invitees" and the emoji becomes the row icon instead of being read as part of
   the title. User-chosen, no schema, and it survives a rename.

   Everything else gets one neutral vector glyph rather than a guessed emoji —
   an icon the app invented would be decoration, and this palette spends colour
   and shape on meaning only.

   This is the one place an emoji still reaches the screen, and deliberately so:
   it is the USER'S OWN character, typed into a name they chose, which is emoji
   as content rather than as chrome. The app no longer suggests it in the
   placeholder, and draws nothing pictographic itself. */
// A leading pictograph or flag, plus any modifiers/ZWJ parts that belong to it,
// then whitespace, then a non-empty name. Escapes rather than literals for the
// invisible joiners (U+FE0F variation selector, U+200D ZWJ) so the source stays
// readable and survives a copy-paste.
const LEAD_EMOJI = new RegExp(
  "^((?:\\p{Extended_Pictographic}|\\p{Regional_Indicator}{2})"
  + "(?:\\uFE0F|\\u200D\\p{Extended_Pictographic}|\\p{Emoji_Modifier})*)\\s+(.*)$",
  "u",
);
const LIST_GLYPH = "diamond";

function listIcon(name) {
  const m = LEAD_EMOJI.exec(String(name || "").trim());
  if (m && m[2].trim()) return { icon: m[1], title: m[2].trim() };
  return { icon: LIST_GLYPH, title: String(name || "").trim() };
}

/** The row/heading icon. Sized off the type scale, not px, so it tracks the
 *  density of whatever row it sits in. */
/* Two different things can land in the icon slot, and they render differently:
 * an emoji the USER typed at the front of the list's name (their character,
 * drawn as text), or the app's own fallback (a vector, since the app draws no
 * pictographs of its own). `plain` is which. */
function ListIcon({ name, size = "var(--fs-title)" }) {
  const { icon } = listIcon(name);
  const plain = icon === LIST_GLYPH;
  return html`<span aria-hidden="true"
    style=${{
      flex: "none", width: "1.5em", display: "inline-flex",
      alignItems: "center", justifyContent: "center", fontSize: size,
      lineHeight: 1, color: plain ? "var(--faint)" : "inherit",
    }}>${plain ? html`<${Icon} name=${LIST_GLYPH} />` : icon}</span>`;
}

/* ── Getting a list out of here ───────────────────────────────────────────────
   There is no server anyone else can reach, so "share this list" honestly means
   the clipboard: text for a message, Markdown for a doc, CSV for a sheet. All of
   it is built from the members already on the page, so it works with the network
   off and needs no key.

   Status is never dropped on the floor. A roster that quietly folds the people
   who already said no into an invite list is worse than no export at all, so
   every export either groups under status headings, or names in its header the
   one status it was narrowed to. */

// The pipeline chips are clipped for the row; an export is prose, so it spells
// the same states out in words.
const STATUS_PROSE = {
  "": "to invite", invited: "invited", replied: "replied",
  attending: "attending", declined: "declined",
};
const prose = (s) => STATUS_PROSE[s] || s || "no status";
const proseCap = (s) => { const p = prose(s); return p[0].toUpperCase() + p.slice(1); };
const nPeople = (n) => `${n} ${n === 1 ? "person" : "people"}`;

/** One member, one line. position falls back to role_type the way the CSV export
 *  does, so a scraped contact carrying only "investor" still says something. */
function memberLine(m) {
  const role = [m.position || m.role_type, m.company].filter(Boolean).join(", ");
  return [m.name, role, m.home_metro || m.home_city].filter(Boolean).join(" — ");
}

function statusGroups(members) {
  const buckets = new Map();
  for (const m of members) {
    const k = m.list_status || "";
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(m);
  }
  const known = STATUSES.map(([v]) => v);
  const order = [...known.filter(k => buckets.has(k)),
                 ...[...buckets.keys()].filter(k => !known.includes(k))];
  return order.map(k => ({ status: k, rows: buckets.get(k) }));
}

/** Title, when/where, and then the line that admits what this export is: how
 *  many of how many, and what happened to status. Never omitted. */
function exportHead(list, shown, total, note) {
  const when = [list.event_date ? fmtDay(list.event_date) : "", list.venue].filter(Boolean).join(" · ");
  const count = (shown === total ? nPeople(shown) : `${shown} of ${total} people`)
    + (note ? ` · ${note}` : "");
  return [list.name || "List", when, count].filter(Boolean);
}

/** Everything the two text formats need to agree on: which rows, and what the
 *  header has to say about them. */
function scoped(list, scope) {
  const all = list.members || [];
  const groups = statusGroups(all).filter(g => scope === "all" || g.status === scope);
  const shown = groups.reduce((n, g) => n + g.rows.length, 0);
  const note = scope !== "all" ? `${prose(scope)} only`
    : groups.length > 1 ? "grouped by invite status"
    : groups.length === 1 ? `all ${prose(groups[0].status)}` : "";
  return { groups, shown, total: all.length, note, head: exportHead(list, shown, all.length, note) };
}

function buildText(list, scope) {
  const { groups, head } = scoped(list, scope);
  const blocks = [head.join("\n")];
  for (const g of groups) {
    // A heading per group only when there is more than one — otherwise the
    // header line already said "all to invite" and a heading just repeats it.
    const title = groups.length > 1 ? `${proseCap(g.status)} (${g.rows.length})` : "";
    blocks.push([title, ...g.rows.map(memberLine)].filter(Boolean).join("\n"));
  }
  return blocks.join("\n\n") + "\n";
}

const mdCell = (v) => String(v || "").replace(/\|/g, "\\|").replace(/\s+/g, " ").trim();

function buildMarkdown(list, scope) {
  const { groups, head } = scoped(list, scope);
  // One table beats a table per group in a doc — so status becomes a column,
  // which keeps it stated per row rather than implied by where the row sits.
  const withStatus = groups.length > 1;
  const cols = ["Name", "Role", "Company", "City", ...(withStatus ? ["Status"] : [])];
  const rows = [];
  for (const g of groups) {
    for (const m of g.rows) {
      rows.push("| " + [
        m.name, m.position || m.role_type, m.company, m.home_metro || m.home_city,
        ...(withStatus ? [proseCap(g.status)] : []),
      ].map(mdCell).join(" | ") + " |");
    }
  }
  const meta = head.slice(1).join(" · ");
  // Blank lines between the blocks are not cosmetic: GFM only parses a table
  // that starts its own block, so a caption butted up against it turns the whole
  // roster into one paragraph of pipes wherever it gets pasted.
  return [
    `## ${head[0]}`,
    meta ? `*${meta}*` : "",
    [`| ${cols.join(" | ")} |`, `| ${cols.map(() => "---").join(" | ")} |`, ...rows].join("\n"),
  ].filter(Boolean).join("\n\n") + "\n";
}

/** Copy-out for one list. Deliberately not "Share" with a link behind it: what
 *  this can actually do is hand you the list in a form you can paste, so the
 *  preview shows the exact characters that will land on the clipboard. */
function ExportPop({ list }) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState("all");
  const [manual, setManual] = useState("");
  const wrap = useRef(null);
  const manualRef = useRef(null);

  // The popover covers the rows you were reading, so it needs both ways out —
  // otherwise it is a lid you have to find the same small button to lift.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    const onDown = (e) => { if (!wrap.current?.contains(e.target)) setOpen(false); };
    window.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
    };
  }, [open]);

  // A refused copy leaves the text on screen and nothing else to do; pre-select
  // it so the ⌘C the toast asks for actually takes the whole thing.
  useEffect(() => {
    if (!manual) return;
    manualRef.current?.focus();
    manualRef.current?.select();
  }, [manual]);

  const members = list.members || [];
  const groups = statusGroups(members);
  // A status can disappear under you (someone replies while this is open); fall
  // back to everyone rather than silently exporting an empty scope.
  const live = scope === "all" || groups.some(g => g.status === scope) ? scope : "all";
  const { shown } = scoped(list, live);
  const text = buildText(list, live);

  const copy = async (body, what) => {
    try {
      await navigator.clipboard.writeText(body);
      setManual("");
      toast(`Copied ${nPeople(shown)} as ${what}`);
    } catch {
      // Clipboard writes get refused (denied permission, insecure origin) and a
      // blind "Copied" is the one failure nobody notices until they paste
      // nothing into the email. Say it plainly and hand over the text instead.
      setManual(body);
      toast("Could not copy. Take the text from the box.");
    }
  };

  const pick = (s) => { setScope(s); setManual(""); };

  return html`
    <div class="exp-wrap" ref=${wrap}>
      <button class="btn" aria-expanded=${open}
        onClick=${() => { setManual(""); setOpen(o => !o); }}>Export</button>
      ${open && html`
        <div class="omni-pop exp-pop">
          <div class="omni-head">Copy this list out</div>
          <div class="exp-note">The text comes from this Mac. The app uploads nothing.</div>

          ${groups.length > 1 && html`
            <div class="exp-scope">
              <button class=${"chip mini" + (live === "all" ? " active" : "")} onClick=${() => pick("all")}>
                Everyone <span class="exp-n">${members.length}</span>
              </button>
              ${groups.map(g => html`
                <button key=${g.status} class=${"chip mini" + (live === g.status ? " active" : "")}
                  onClick=${() => pick(g.status)}>
                  ${STATUS_LABEL[g.status] || prose(g.status)} <span class="exp-n">${g.rows.length}</span>
                </button>`)}
            </div>`}

          <div class=${"exp-cap" + (manual ? " warn" : "")}>
            ${manual ? "Not copied. Select the text and press ⌘C." : "Preview — plain text"}
          </div>
          ${manual
            ? html`<textarea class="input exp-manual" rows="7" readOnly=${true} ref=${manualRef}
                     aria-label="List text to copy"
                     value=${manual} onFocus=${e => e.target.select()} />`
            : html`<pre class="exp-preview">${text.trimEnd()}</pre>`}

          <div class="exp-acts">
            <button class="btn primary mini" onClick=${() => copy(text, "text")}>Copy as text</button>
            <button class="btn mini" onClick=${() => copy(buildMarkdown(list, live), "Markdown")}>Copy as Markdown</button>
            ${/* The CSV comes from the server, which can reach the phone numbers
                   and emails this page never loaded — so it stays the server's,
                   and it is the one button here that ignores the status scope.
                   That difference belongs ON it, not in a note underneath: three
                   buttons in a row read as three ways to export the same thing. */ ""}
            <a class="btn mini" href=${`/api/list/${list.id}/export`} download>
              <${Icon} name="download" /> CSV${live !== "all" ? ` — all ${members.length}` : ""}
            </a>
          </div>
          <div class="exp-note">
            ${live !== "all"
              ? html`Text and Markdown use the ${prose(live)} filter above. The CSV always holds
                     the whole list — ${nPeople(members.length)}, every status.`
              : html`The CSV adds one column for phone numbers and one for emails.`}
          </div>
        </div>`}
    </div>`;
}

/** The index of lists. Dense --row-h rows: icon, name, then the pipeline in one
 *  muted line. This is the "curated subsets" half of Objects vs Lists, so it
 *  states counts and nothing else — the roster lives on the right. */
function ListsColumn({ selectedId, go, refreshKey, focusNew, dropOver, dragging, onNames }) {
  const [lists, setLists] = useState(null);
  const [name, setName] = useState("");
  const nameRef = useRef(null);

  const load = useCallback(() => {
    api("/api/lists").then(d => setLists(d.lists || [])).catch(() => setLists([]));
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);

  // ⌘K → "Start a new list" lands on lists?new=1 and expects the cursor in the
  // name field. Creates nothing on its own: create() bails on an empty name, so
  // a reflexive second Enter does nothing at all.
  useEffect(() => { if (focusNew) nameRef.current?.focus(); }, [focusNew]);

  const create = async () => {
    if (!name.trim()) return;
    try {
      const d = await api("/api/lists", { method: "POST", body: JSON.stringify({ name: name.trim() }) });
      setName("");
      go(`lists/${d.id}`);
    } catch (e) { toast(e.message); }
  };

  const rows = lists || [];
  // Keyed by the same string the drop target carries, so a lookup cannot miss on
  // number-vs-string. Titles, so an emoji a user put in a name is not read out.
  useEffect(() => {
    onNames?.(Object.fromEntries(rows.map(l => [String(l.id), listIcon(l.name).title])));
  }, [lists]);
  return html`
    <aside class="plist">
      <div class="plist-tools">
        <div style="display:flex;gap:var(--gap-sm)">
          <input class="input" ref=${nameRef} aria-label="New list name"
            placeholder="New list — e.g. Team dinner" value=${name}
            onInput=${e => setName(e.target.value)}
            onKeyDown=${e => { if (e.key === "Enter") create(); }} />
          <button class="btn primary" disabled=${!name.trim()} onClick=${create}>Create</button>
        </div>
        ${lists !== null && rows.length > 0 && html`
          <div class="section-label" style="margin:0">${rows.length} ${rows.length === 1 ? "list" : "lists"}</div>`}
      </div>
      <div class="plist-scroll">
        ${lists === null && html`<${SkelRows} n=${6} />`}
        ${lists !== null && rows.length === 0 && html`
          <${Empty} title="No lists yet"
            hint="Start a name with an emoji and it becomes the list icon.">
            Name a list above to create it. You can also save an Ask answer as a list.
          </${Empty}>`}
        ${rows.map(l => html`
          <button key=${l.id}
            class=${"prow"
              + (l.id === selectedId ? " active" : "")
              + (dropOver === String(l.id) ? " drop-target" : "")}
            style="min-height:var(--row-h)"
            aria-current=${l.id === selectedId ? "true" : "false"}
            ${/* The list you are already looking at is not a target: everyone you
                  could drag is already in it, so a drop there is a no-op dressed
                  up as an action. Every other list takes the drop. */ ""}
            data-drop=${dragging && l.id !== selectedId ? String(l.id) : null}
            onClick=${() => go(`lists/${l.id}`)}>
            <${ListIcon} name=${l.name} />
            <div class="grow">
              <div class="p-name"><span class="txt">${listIcon(l.name).title}</span></div>
              <div class="p-meta">
                ${nPeople(l.members)}
                ${l.invited > 0 ? ` · ${l.invited} invited` : ""}
                ${l.replied > 0 ? ` · ${l.replied} replied` : ""}
                ${l.attending > 0 ? ` · ${l.attending} going` : ""}
              </div>
            </div>
          </button>`)}
      </div>
    </aside>`;
}

function AddMember({ listId, existingIds, onAdded }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const search = useCallback(debounce(async (query) => {
    if (!query.trim()) { setResults([]); return; }
    try {
      const d = await api("/api/contacts?" + new URLSearchParams({ q: query }));
      setResults((d.contacts || []).filter(c => !existingIds.has(c.id)).slice(0, 5));
    } catch { setResults([]); }
  }, 200), [existingIds]);

  const add = async (c) => {
    try {
      const d = await api(`/api/list/${listId}/add`, { method: "POST", body: JSON.stringify({ contact_ids: [c.id] }) });
      // The engine says how many it ADDED. A person who was taken off this list and is
      // put back counts as added; only someone already on it adds nothing.
      if (d && d.added === 0) toast(`${c.name.split(" ")[0]} is already in this list`);
      setQ(""); setResults([]);
      onAdded();
    } catch (e) { toast(e.message); }
  };

  return html`
    <div style="position:relative;max-width:min(22rem,100%)">
      <input class="input" aria-label="Add a person to this list"
        placeholder="Add a person to this list" value=${q}
        onInput=${e => { setQ(e.target.value); search(e.target.value); }} />
      ${results.length > 0 && html`
        <div class="omni-pop">
          ${results.map(c => html`
            <button key=${c.id} class="omni-row" style="min-height:var(--row-h)" onClick=${() => add(c)}>
              <${Avatar} c=${c} size=${AV.xs} />
              <div class="grow">
                <div class="r-name">${c.name}</div>
                <div class="r-meta">${roleStr(c) || c.home_metro || "No role yet"}</div>
              </div>
              <span class="r-kind">add</span>
            </button>`)}
        </div>`}
    </div>`;
}

/** One member of a roster: who they are, where they are in the pipeline, and the
 *  two per-person actions. Dense — a roster is scanned, not read. */
function MemberRow({ listId, m, go, onChanged, onDraft, onDragStart }) {
  const setStatus = async (status) => {
    try {
      await api(`/api/list/${listId}/member`, { method: "POST", body: JSON.stringify({ contact_id: m.id, status }) });
      onChanged();
    } catch (e) { toast(e.message); }
  };
  const remove = async () => {
    try {
      await api(`/api/list/${listId}/member`, { method: "POST", body: JSON.stringify({ contact_id: m.id, remove: true }) });
      onChanged();
    } catch (e) { toast(e.message); }
  };
  const where = [roleStr(m), m.home_metro || m.home_city].filter(Boolean).join(" · ");
  return html`
    <div class="member-row drag-row" style="min-height:var(--row-h)"
         onPointerDown=${e => {
           // Only a press on the person half starts a drag. The pipeline chips
           // and the two action buttons keep their own presses, or every attempt
           // to mark someone "Invited" would be read as the start of a drag.
           if (e.target.closest(".member-pipeline, .member-acts")) return;
           onDragStart?.(e, m);
         }}>
      <button class="member-id" onClick=${() => go(`people/${m.id}`)}
        aria-label=${`Open ${m.name}`}>
        <${Avatar} c=${m} size=${AV.md} ring />
        <div class="grow">
          <div class="p-name"><span class="txt">${m.name}</span></div>
          <div class="p-meta">${where || "No role yet"}</div>
        </div>
      </button>
      <div class="member-pipeline" role="group" aria-label=${`Invite status for ${m.name}`}>
        ${STATUSES.map(([val, label]) => html`
          <button key=${val} class=${"chip mini" + (m.list_status === val ? " active" : "")}
            aria-pressed=${m.list_status === val} onClick=${() => setStatus(val)}>${label}</button>`)}
      </div>
      <div class="member-acts">
        <button class="btn quiet mini" title="Write a message" aria-label=${`Write a message to ${m.name}`}
          onClick=${() => onDraft(m)}><${Icon} name="pencil" /></button>
        <button class="btn quiet mini" title="Remove from this list" aria-label=${`Remove ${m.name} from this list`}
          onClick=${remove}><${Icon} name="close" /></button>
      </div>
    </div>`;
}

function ListDetail({ id, go, onListsChanged, onDragStart }) {
  const [list, setList] = useState(null);
  const [err, setErr] = useState("");
  const [draftPerson, setDraftPerson] = useState(null);

  const load = useCallback(async () => {
    try { setList(await api("/api/list/" + id)); setErr(""); }
    catch (e) { setErr(e.message); }
  }, [id]);
  useEffect(() => { setList(null); load(); }, [id, load]);

  const reload = () => { load(); onListsChanged(); };

  const rename = async (name) => {
    if (!name.trim() || name === list.name) return;
    try { await api(`/api/list/${id}/rename`, { method: "POST", body: JSON.stringify({ name: name.trim() }) }); reload(); }
    catch (e) { toast(e.message); }
  };

  // Deleting a list is irreversible: handle_list_delete commits db.delete_list,
  // which drops the list, every membership row and every pipeline status. There
  // is no undo. So it takes two explicit steps, the way MergeModal does — and
  // the prose states the consequence before the second click, because the one
  // thing a reader needs to know is that the PEOPLE survive.
  const [delPending, setDelPending] = useState(false);

  useEffect(() => {
    if (!delPending) return;
    const onKey = (e) => { if (e.key === "Escape") setDelPending(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [delPending]);

  const del = async () => {
    setDelPending(false);
    try {
      await api("/api/list/" + id, { method: "DELETE" });
      toast("List deleted");
      onListsChanged();
      go("lists");
    } catch (e) { toast(e.message); }
  };

  const saveMeta = async (field, v) => {
    try { await api(`/api/list/${id}/meta`, { method: "POST", body: JSON.stringify({ [field]: v }) }); reload(); }
    catch (e) { toast(e.message); }
  };

  if (err) return html`
    <div class="person"><div class="person-empty">
      <div class="big">Could not load this list</div>${err}
    </div></div>`;
  if (!list) return html`<div class="person"><${SkelRows} n=${6} /></div>`;

  const members = list.members || [];
  // The server counts these, and this pane does not. It used to add them up from
  // `members` — but the rail beside it reads db.get_lists(), whose `replied` is
  // cumulative (attending implies replied) where this loop was exclusive. So one
  // list showed "1 replied · 1 going" in the rail and "1 going" here, at the same
  // moment, in the same four words, with no scroll between them. NC-089.
  const counts = list.counts || { members: 0, invited: 0, replied: 0, attending: 0 };
  const existingIds = new Set(members.map(m => m.id));

  return html`
    <div class="person">
      <div style="display:flex;align-items:flex-start;gap:var(--gap)">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:var(--gap-sm)">
            <${ListIcon} name=${list.name} size="var(--fs-page)" />
            ${/* One editable field, one meaning: the whole name including any
                  leading emoji, so renaming the icon is renaming the name. Sans
                  at the page size — the serif is reserved for a person's own
                  page (DESIGN_DIRECTION §2). */ ""}
            <input class="person-name-input" aria-label="List name"
              style="flex:1;min-width:0;font-family:var(--sans);font-size:var(--fs-page);font-weight:650"
              value=${list.name} onChange=${e => rename(e.target.value)} />
          </div>
          <div style="font-size:var(--fs-ui);color:var(--muted);margin-top:var(--gap-sm)">
            ${nPeople(members.length)}
            ${counts.invited ? ` · ${counts.invited} invited` : ""}
            ${counts.replied ? ` · ${counts.replied} replied` : ""}
            ${counts.attending ? ` · ${counts.attending} going` : ""}
          </div>
          <div style="display:flex;gap:var(--gap-sm);margin-top:var(--gap-sm);flex-wrap:wrap">
            <input class="input" type="date" aria-label="Event date"
              style="width:auto;flex:none;font-size:var(--fs-ui)"
              value=${list.event_date || ""} onChange=${e => saveMeta("event_date", e.target.value)} />
            <input class="input" placeholder="Venue" aria-label="Venue"
              style="flex:1 1 16ch;max-width:28ch;font-size:var(--fs-ui)"
              value=${list.venue || ""} onChange=${e => saveMeta("venue", e.target.value)} />
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:var(--gap-sm);align-items:flex-end;flex:none">
          ${members.length > 0 && html`<${ExportPop} list=${list} />`}
          ${!delPending
            ? html`<button class="btn danger" onClick=${() => setDelPending(true)}>Delete list</button>`
            : html`
              <div class="del-confirm" role="group" aria-label="Confirm deleting this list"
                onBlur=${(e) => {
                  // Leaving the group cancels. relatedTarget is null when focus
                  // goes to the page itself, so treat that as leaving too.
                  if (!e.currentTarget.contains(e.relatedTarget)) setDelPending(false);
                }}>
                <span class="del-confirm-q">
                  Delete this list?${ ""}
                  ${members.length > 0
                    ? html` The ${members.length} ${members.length === 1 ? "person" : "people"} stay in People.`
                    : ""}
                </span>
                <button class="btn danger" autofocus onClick=${del}>Confirm</button>
                <button class="btn quiet" onClick=${() => setDelPending(false)}>Cancel</button>
              </div>`}
        </div>
      </div>

      <div style="margin:var(--pad-view) 0 var(--gap)">
        <${AddMember} listId=${id} existingIds=${existingIds} onAdded=${reload} />
      </div>

      ${members.length === 0
        ? html`<${Empty} title="This list is empty"
            hint="A list is a subset of your people. It never changes the records themselves.">
            Add a person in the field above. You can also save an Ask answer into this list.
          </${Empty}>`
        : members.map(m => html`
          <${MemberRow} key=${m.id} listId=${id} m=${m} go=${go}
            onChanged=${reload} onDraft=${setDraftPerson} onDragStart=${onDragStart} />`)}

      ${draftPerson && html`<${DraftModal} person=${draftPerson} onClose=${() => setDraftPerson(null)} />`}
    </div>`;
}

export function Lists({ id, go, params }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const bump = () => setRefreshKey(k => k + 1);

  /* Drag lives up here because its two ends are siblings: the person comes from
     ListDetail on the right, the list they land on is in ListsColumn on the
     left. Neither could own it alone.
     Dropping ADDS rather than moves. A list is a pointer at people, not a
     container that holds them, so a person can be in six lists at once — taking
     them out of this one because you put them in another would be a deletion
     nobody asked for. */
  // Names, not ids, for the live region — "over 4" tells a screen-reader user
  // nothing, and the drop key has to stay the id because that is what the POST
  // needs. Held in a ref so the announcement can look one up without ListsColumn
  // having to lift its whole loaded state.
  const listNames = useRef({});
  const { drag, over, startDrag, dragging } = useDrag({
    onDrop: async (listId, person) => {
      try {
        const d = await api(`/api/list/${listId}/add`, {
          method: "POST", body: JSON.stringify({ contact_ids: [person.id] }),
        });
        // Say what the engine did, not what was dragged: "Added" over a list that
        // already held them was the small lie. A removed member put back counts as added.
        const first = person.name.split(" ")[0];
        toast(d && d.added === 0
          ? `${first} is already in “${listNames.current[listId] || "that list"}”`
          : `Added ${first}`);
        bump();
      } catch (e) { toast(e.message); }
    },
  });
  const onDragStart = (e, m) => startDrag(e, m, m.name);

  return html`
    <div class="people-layout">
      <${ListsColumn} selectedId=${id} go=${go} refreshKey=${refreshKey}
                      focusNew=${oneShot(params, "new")}
                      dropOver=${over} dragging=${dragging} onNames=${m => { listNames.current = m; }} />
      ${id
        ? html`<${ListDetail} key=${id} id=${id} go=${go} onListsChanged=${bump}
                              onDragStart=${onDragStart} />`
        : html`<div class="person"><div class="person-empty">
            <div class="big">Select a list</div>
            A list is a group you curate: an event roster, a shortlist, a segment.
            Your people stay in People — a list only points at them.
          </div></div>`}
      <${DragGhost} drag=${drag} />
      ${/* Dragging is a pointer gesture and reaches no keyboard or screen-reader
            user, so it is an accelerator and never the only route: the "Add to
            list" control on the person page does the same job, unchanged. This
            at least narrates the gesture for anyone watching it happen. */ ""}
      <${DragLive} drag=${drag} over=${over} overLabel=${over && listNames.current[over]} />
    </div>`;
}
