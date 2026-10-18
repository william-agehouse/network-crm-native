// People — a record table over the whole network, with a person's story beside it.
//
// Density is the feature. A network can run to thousands of people and tens of
// thousands of conversations, and most of those people have no history at all — so
// this surface has to show many rows at once, and it has to show WHICH rows are
// empty. Hence a 40px-row record table: hierarchy by weight, hairline dividers,
// alignment carrying the eye, and the two relationship-intelligence columns the
// old card list hid — LAST CONTACT, coloured by the staleness ramp, and CLOSENESS
// as a dot plus a humanised label.
//
// Empty cells say what is missing, in words. That is the most important detail
// here: a blank cell hid the one thing this app exists to tell you.
//
// The API contract is unchanged — keyword rows from /api/contacts?…, meaning rows
// from /api/search/semantic?q=, merged on the client, and a missing semantic index
// still degrades to keyword-only.
import { html, useState, useEffect, useRef, useCallback, useMemo } from "../vendor/preact-htm.module.js";
import { api, toast, oneShot, debounce, roleStr, relDate, fmtDay, fmtMonthYear, CHANNEL_LABEL, CHANNEL_ORDER, CATS, CAT_LABEL, CAT_COLOR } from "./api.js";
import { Icon, CHANNEL_ICON, DATE_ICON } from "./icons.js";
import { useRowSelection, Marquee, claimFocus } from "./select.js";
import { useRowWindow } from "./windowing.js";
import { useContextMenu, ContextMenu } from "./menu.js";
// Tags left this import with the redesign: location, role type and topics are now
// explicit rows in the person page's Details block rather than a run of pills in the
// identity band, and the table half never used it.
import { Avatar, CatDot, Empty, SkelRows, Md, Meter, DraftModal, Receipt, Stepper, MergeModal, AddToList, messageTotal } from "./ui.js";

// The list is paged. PAGE paints at once; ALL is "everyone who matches" — the
// server clamps it to its own ceiling, and even a network of thousands is only a
// few MB of JSON over loopback, so asking for it is cheap. It matters twice: the count can
// only be honest if the server also reports the total, and the client-side
// predicates (closeness, channel, last contact) can only be right when the client
// is holding every matching row instead of the first page.
const PAGE = 500;
const ALL = 20000;

const STRENGTH_TIERS = [["close", "Close"], ["active", "Active"], ["loose", "Loose"], ["faint", "Faint"]];
const STRENGTH_LABEL = Object.fromEntries(STRENGTH_TIERS);

/* ── Column-type icons ────────────────────────────────────────
 * The table's column glyphs are the shared vector set now — ICON_PATHS and
 * ColIcon used to live here, holding nine of the ninety paths in icons.js, and
 * a second copy of the same <svg> wrapper. ColIcon survives only as the thin
 * binding that keeps the .nx-ic class (12px, .7 opacity — the column header is
 * the one place the icon is deliberately smaller and quieter than --ic). */
function ColIcon({ name }) {
  return html`<${Icon} name=${name} className="nx-ic" />`;
}

// The table's columns, in order. `key` is also the CSS class suffix, so the
// narrow (split-pane) variant can drop columns from one place in the stylesheet.
const COLS = [
  { key: "person", icon: "person", label: "Name" },
  { key: "role", icon: "work", label: "Role" },
  { key: "loc", icon: "pin", label: "Location" },
  { key: "topics", icon: "tag", label: "Topics" },
  { key: "last", icon: "clock", label: "Last contact" },
  { key: "strength", icon: "bolt", label: "Closeness" },
];

/** Days since the most recent touchpoint, or null when there has never been one.
 *  null and 0 mean opposite things here, so they must not collapse. */
function daysSince(iso) {
  if (!iso) return null;
  const d = new Date(String(iso).slice(0, 10) + "T12:00:00Z");
  if (isNaN(d)) return null;
  return Math.max(0, Math.round((Date.now() - d.getTime()) / 86400000));
}

/** Which step of the --stale-* ramp a row sits on. Silence has to have a colour
 *  (tokens.css §staleness), and this is the only place that decides the edges:
 *  days → fresh, weeks → warm, months → cool, over a year or never → cold. */
function staleTier(iso) {
  const d = daysSince(iso);
  if (d === null) return "cold";
  if (d < 14) return "fresh";
  if (d < 90) return "warm";
  if (d < 365) return "cool";
  return "cold";
}

// Last-contact filter. Thresholds, not buckets: for a neglect tool "quiet 6+
// months" is the useful question, and 6 months is the app's own definition of
// going cold (api_contacts.RECONNECT_MONTHS). Client-side, like closeness and
// channel, so it forces an unpaged fetch — see pageFor.
const LAST_FILTERS = [
  ["m1", "In the last month", (d) => d !== null && d <= 30],
  ["q3", "Quiet 3+ months", (d) => d !== null && d >= 90],
  ["q6", "Quiet 6+ months", (d) => d !== null && d >= 180],
  ["y1", "Quiet 1+ year", (d) => d !== null && d >= 365],
  ["never", "Never contacted", (d) => d === null],
];
const lastPred = (k) => (LAST_FILTERS.find(([v]) => v === k) || [])[2];
const lastLabel = (k) => ((LAST_FILTERS.find(([v]) => v === k) || [])[1] || k);

const SORTS = [["closeness", "Closest first"], ["quiet", "Longest quiet first"], ["recent", "Recently active"],
               ["messages", "Most messages"]];
const sortLabel = (k) => ((SORTS.find(([v]) => v === k) || [])[1] || k);

/** The order the table shows. The server ranks every sort first (db.list_contacts),
 *  so the loaded page is the top of the whole list, not of whoever was loaded; this
 *  only makes the order on screen match what each row shows. Closeness is left as
 *  the server ranked it, never-contacted rows sink under quiet and recent (they are
 *  strangers, not neglected friends), and "messages" puts the most messages across
 *  every channel first. Array.sort is stable, so people with the same total keep the
 *  server's order. */
function orderRows(visible, effSort) {
  if (effSort === "closeness") return visible;
  if (effSort === "messages") return [...visible].sort((a, b) => messageTotal(b) - messageTotal(a));
  return [...visible].sort((a, b) => {
    const an = a.last ? 0 : 1, bn = b.last ? 0 : 1;
    if (an !== bn) return an - bn;
    if (!a.last) return 0;
    if (a.last === b.last) return 0;
    const asc = a.last < b.last ? -1 : 1;
    return effSort === "quiet" ? asc : -asc;
  });
}

/* ── Saved views ──────────────────────────────────────────────
 * Named parameter bundles over what the API already supports: `filter` and
 * `sort` on /api/contacts, plus the client-side predicates. Nothing here invents
 * data. "Going cold" is sort=quiet with the 6-month threshold — near, but not
 * identical, to the reconnect list, which is snooze-aware and lives server-side
 * (db.reconnect_due); a view for exactly that needs a backend filter value.
 * These are fixed, not user-editable: persisting a view the user names needs a
 * table and two endpoints. */
const VIEWS = [
  { key: "everyone", name: "Everyone", note: "the whole network, closest first", p: {} },
  { key: "cold", name: "Going cold", note: "quiet 6 months or more, longest first", p: { sort: "quiet", last: "q6" } },
  { key: "never", name: "Never contacted", note: "no message on any channel", p: { last: "never" } },
  { key: "recent", name: "Recently active", note: "last exchange first", p: { sort: "recent" } },
  { key: "close", name: "Inner circle", note: "closeness reads as close", p: { tier: "close" } },
  { key: "todo", name: "To describe", note: "no description written yet", p: { filter: "todo" } },
  { key: "described", name: "Described", note: "you have written them up", p: { filter: "described" } },
  { key: "excluded", name: "Excluded", note: "hidden from review and imports", p: { filter: "excluded" } },
  { key: "deleted", name: "Deleted", note: "reversible — restore from here", p: { filter: "deleted" } },
];

/** Role views built from the roles the network actually has (/api/facets), so
 *  "Founders" appears when there are founders and not otherwise. */
function roleViews(roles) {
  const want = ["founder", "investor", "engineer"];
  const out = [];
  for (const w of want) {
    const hit = (roles || []).find((r) => String(r).toLowerCase() === w);
    if (hit) out.push({ key: "role:" + hit, name: hit[0].toUpperCase() + hit.slice(1) + "s", note: "role is " + hit, p: { role: hit } });
  }
  return out;
}

// The full parameter shape of a view, so matching and applying use one default set.
const VIEW_DEFAULTS = { filter: "all", sort: "closeness", location: "", role: "", topic: "", last: "", tier: "", chan: "" };
const viewParams = (v) => ({ ...VIEW_DEFAULTS, ...(v.p || {}) });

/* ── The count ────────────────────────────────────────────────────────────────
 * ONE owner. Everything that needs a number reads this object; nothing counts
 * again.
 *
 * The bug it replaces: the header ran its own count over the fetched page while
 * the bulk-add button counted the merged keyword+semantic rows, so one screen
 * said "First 500 of N" above "+ Add these 12 matches to a list". Neither
 * number was wrong. There were two owners.
 *
 * The honest presentation, decided here and stated once (N is the total):
 *   shown === total        → "N people"
 *   shown  <  total        → "42 of N people"
 *   server capped the page → "First 500 of N"  + a Load-all action
 * `shown` is always the number of rows the table renders — the same number the
 * bulk action, the stepper and the row aria labels use. The difference between
 * "capped by the server" and "narrowed by a filter" is not a second answer to
 * "how many?": it is answered by whether the Load-all action is there. */
function countState(shown, loaded, total) {
  const n = (x) => Number(x || 0).toLocaleString();
  const known = typeof total === "number";
  const capped = known && loaded < total;
  const unit = shown === 1 ? "person" : "people";
  let lead = n(shown), tail = unit;
  if (capped && shown >= loaded && loaded >= PAGE) {
    // The page cap is the reason, and paging is the honest word for it.
    lead = `First ${n(loaded)}`;
    tail = `of ${n(total)}`;
  } else if (capped && shown < loaded) {
    // A client-side predicate over a capped page. pageFor keeps this unreachable
    // by refetching unpaged, so if it ever shows up, it says so rather than
    // claiming these are all the matches in the network.
    tail = `of the first ${n(loaded)} loaded · ${n(total)} in the network`;
  } else if (known && shown < total) {
    tail = `of ${n(total)} ${unit}`;
  }
  return {
    shown, loaded, total, capped, lead, tail,
    bulk: shown === 1 ? "Add this person to a list" : `Add these ${n(shown)} to a list`,
    loadAll: known ? `Load all ${n(total)}` : "Load all",
  };
}

/* ── Ghost pill with an active-count badge ───────────────────────────────────
 * Count badges on the Filter and Sort buttons say when something is active. One
 * component for the view picker, Sort and Filter, so all three behave the same under the
 * mouse, the keyboard and Escape. `pop` renders the popover body and is handed a
 * close callback. */
function Pill({ label, icon, badge = 0, on = false, look = "", title, pop, onOpen }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const away = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const esc = (e) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      ref.current?.querySelector(".nx-pill")?.focus();
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  const close = () => setOpen(false);
  return html`
    <div class="nx-pw" ref=${ref}>
      <button class=${"nx-pill" + (look ? " " + look : "") + (on ? " on" : "")}
              aria-expanded=${open} aria-haspopup="true" title=${title || label}
              onClick=${() => setOpen((o) => { if (!o) onOpen?.(); return !o; })}>
        ${icon && html`<${ColIcon} name=${icon} />`}
        <span class="nx-pill-l">${label}</span>
        ${badge > 0 && html`<span class="nx-badge">${badge}</span>`}
        <span class="nx-cv"><${Icon} name="chevronDown" /></span>
      </button>
      ${open && html`<div class="omni-pop nx-pop">${pop(close)}</div>`}
    </div>`;
}

/** One row of a popover list. */
/* ── The right-click menu ─────────────────────────────────────────────────────
 * A context menu on a person answers two questions at once: who is this, and
 * what can I do with them. The head is the first half — the facts you'd
 * otherwise have to open the person to see. It costs no extra request: every
 * field here is already on the row the table rendered. */
function PersonPeek({ c, n }) {
  const tier = staleTier(c.last);
  const msgs = Object.values(c.counts || {}).reduce((a, b) => a + (b || 0), 0);
  const tag = STRENGTH_LABEL[c.strength_label] || c.strength_label || "";
  return html`
    <div class="ctx-peek">
      <${Avatar} c=${c} size=${28} ring />
      <div class="grow">
        <div class="ctx-peek-name">${c.name}</div>
        <div class="ctx-peek-meta">${roleStr(c) || "No role yet"}</div>
        <div class="ctx-peek-facts">
          <span class=${"s-" + tier}>${c.last ? relDate(c.last) : "No contact yet"}</span>
          ${msgs > 0 && html`<span class="ctx-peek-sep">·</span><span>${tag} ${c.strength || 0}</span>`}
          ${msgs > 0 && html`<span class="ctx-peek-sep">·</span><span>${msgs.toLocaleString()} msgs</span>`}
        </div>
      </div>
      ${/* When the right-click lands inside a multi-selection, say so — the
            actions below will act on all of them, and a menu headed by one
            person's face that quietly touches twelve records is a trap. */ ""}
      ${n > 1 && html`<span class="ctx-peek-n">${n}</span>`}
    </div>`;
}

/* `targets` is what the actions act on: the marked rows when the right-click
 * landed inside the selection, otherwise just the row clicked. */
function personMenuItems(c, targets, go, clearSel) {
  const many = targets.length > 1;
  return [
    { key: "open", icon: "person", label: many ? `Open ${c.name}` : "Open",
      run: () => go(`people/${c.id}`) },
    { key: "note", icon: "pencil", label: "Add a note",
      run: () => go(`people/${c.id}?note=1`) },
    { key: "draft", icon: "chat", label: "Draft a message",
      run: () => go(`people/${c.id}?draft=1`) },   // openDraft, wired in Header
    { sep: true },
    // No "add to a list" item here. With several rows marked the selection bar
    // is already on screen carrying exactly that control, and with one row the
    // person page it opens has it in the header — a third copy that navigates
    // away first would be the worst of the three.
    { key: "merge", icon: "layers", label: "Merge with…", disabled: many,
      hint: many ? "one at a time" : "",
      run: () => go(`people/${c.id}?merge=1`) },
    // No "copy email / copy phone" here, though they are the obvious next items.
    // The table renders _contact_summary, which carries no identifiers — so both
    // would sit permanently greyed out, and a menu with two dead rows in it
    // teaches you to stop reading the menu. Getting them would cost a request
    // per right-click; ⌘K already offers "Copy <name>'s email" for the open
    // person, which is where the identifiers are actually loaded.
    ...(many ? [{ sep: true }, { key: "clear", icon: "close", label: "Clear selection", run: clearSel }] : []),
  ];
}

function PopRow({ name, note, sel, onClick }) {
  return html`
    <button class=${"omni-row" + (sel ? " sel" : "")} onClick=${onClick}>
      <div class="grow">
        <div class="r-name">${name}</div>
        ${note && html`<div class="r-meta">${note}</div>`}
      </div>
      ${sel && html`<span class="r-kind"><${Icon} name="check" /></span>`}
    </button>`;
}

/** Hidden contacts — names the app excludes everywhere (spam, bots, anyone you
 * never want to see). Moved here from Settings (NC-003): hiding a name is a
 * data-management action on the network itself, not a decision about how the
 * app is configured, so it belongs on the surface that already IS the
 * network, not on the page that configures it.
 *
 * Reached via the "Hidden" pill in the People toolbar, next to Add. Backed by
 * the same /api/exclude-list endpoints Settings used to call — every importer
 * already honors this list regardless of which screen edits it, so moving the
 * UI changed nothing underneath. */
function HiddenNamesPanel({ entries, onEntries }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const typed = name.trim().toLowerCase();
  const note = typed && (entries || []).some(e => e.name.toLowerCase() === typed)
    ? "This name is already hidden." : "";

  const add = async () => {
    const n = name.trim();
    if (!n || busy) return;
    setBusy(true);
    try {
      const d = await api("/api/exclude-list/add", { method: "POST", body: JSON.stringify({ name: n }) });
      onEntries(d.entries || []);
      setName("");
      toast(d.warning || "Hidden");   // NC-150
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  const remove = async (n) => {
    setBusy(true);
    try {
      const d = await api("/api/exclude-list/remove", { method: "POST", body: JSON.stringify({ name: n }) });
      onEntries((entries || []).filter(e => e.name !== n));
      toast(d.warning || "Removed");   // NC-150
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  return html`
    <div class="hidden-pop">
      <div class="omni-head">Hidden contacts</div>
      <div class="nx-pop-note">Hidden everywhere. Every import obeys this list.</div>
      <div class="hidden-add-row">
        <input
          class=${"input" + (note ? " soft-invalid" : "")}
          value=${name}
          aria-label="Full name to hide"
          placeholder="Full name to hide"
          onInput=${e => setName(e.target.value)}
          onKeyDown=${e => { if (e.key === "Enter") add(); }}
        />
        ${name.trim() && html`<button class="btn primary mini" disabled=${busy} onClick=${add}>Add</button>`}
      </div>
      ${note && html`<div class="field-note" role="status">${note}</div>`}
      ${entries === null
        ? html`<div class="exclude-list"><${SkelRows} n=${2} h=${32} /></div>`
        : entries.length === 0
          ? html`<div class="nx-pop-note">No hidden names.</div>`
          : html`
            <div class="exclude-list">
              ${entries.map(e => html`
                <div class="exclude-row" key=${e.name}>
                  <span class="exclude-name">
                    ${e.name}
                    ${!e.in_db && html`<span class="exclude-tag">not in your contacts</span>`}
                  </span>
                  <button class="btn quiet mini" disabled=${busy}
                    aria-label=${"Stop hiding " + e.name}
                    onClick=${() => remove(e.name)}>Remove</button>
                </div>`)}
            </div>`}
    </div>`;
}

/* ── Filter: one pill, a nested popover ──────────────────────────────────────
 * Replaces the six-chip wall. Level one lists the dimensions with their current
 * value; picking one drills into its values with a back chevron and a search box
 * on the long lists. The badge counts the dimensions that are
 * set — which is also what the removable chips below the toolbar spell out. */
function FilterPill({ dims }) {
  const [pane, setPane] = useState(null);
  const [q, setQ] = useState("");
  const active = dims.filter((d) => d.value);
  const cur = dims.find((d) => d.key === pane);
  const needle = q.trim().toLowerCase();
  const options = cur
    ? (needle ? cur.options.filter((o) => (o.label || "").toLowerCase().includes(needle)) : cur.options)
    : [];
  return html`
    <${Pill} label="Filter" icon="filter" badge=${active.length} on=${active.length > 0}
      title=${active.length ? active.map((d) => `${d.label}: ${d.text(d.value)}`).join(" · ") : "Filter the table"}
      onOpen=${() => { setPane(null); setQ(""); }}
      pop=${(close) => (!cur
        ? html`
          <div class="omni-head">Filter by</div>
          ${dims.map((d) => html`
            <button key=${d.key} class="omni-row" onClick=${() => { setPane(d.key); setQ(""); }}>
              <span class="nx-pop-ic"><${ColIcon} name=${d.icon} /></span>
              <div class="grow">
                <div class="r-name">${d.label}</div>
                <div class="r-meta">${d.value ? d.text(d.value) : d.any}</div>
              </div>
              <span class="r-kind"><${Icon} name="chevronRight" /></span>
            </button>`)}
          ${active.length > 1 && html`
            <button class="omni-row" onClick=${() => { dims.forEach((d) => d.set("")); close(); }}>
              <div class="grow"><div class="r-name">Clear all filters</div></div>
            </button>`}`
        : html`
          <button class="omni-row nx-back" onClick=${() => { setPane(null); setQ(""); }}>
            <span class="r-kind"><${Icon} name="chevronLeft" /></span>
            <div class="grow"><div class="r-name">${cur.label}</div></div>
          </button>
          ${cur.searchable && html`
            <input class="input nx-pop-search" autofocus placeholder=${"Search " + cur.label.toLowerCase() + "…"}
              value=${q} onInput=${(e) => setQ(e.target.value)} />`}
          <${PopRow} name=${cur.any} sel=${!cur.value} onClick=${() => { cur.set(""); setPane(null); close(); }} />
          ${options.map((o) => html`
            <${PopRow} key=${o.value} name=${o.label} sel=${o.value === cur.value}
              onClick=${() => { cur.set(o.value); setPane(null); close(); }} />`)}
          ${options.length === 0 && html`<div class="nx-pop-note">No matches.</div>`}`)} />`;
}

/** The applied filters, spelled out and removable. The badge on
 *  the Filter pill says how many; these say which. */
function AppliedChips({ dims }) {
  const on = dims.filter((d) => d.value);
  if (!on.length) return null;
  return html`
    <div class="nx-chips">
      ${on.map((d) => html`
        <span class="nx-chip" key=${d.key}>
          <span class="nx-chip-ic"><${ColIcon} name=${d.icon} /></span>
          <span class="nx-chip-k">${d.label}</span>
          <b>${d.text(d.value)}</b>
          <button class="nx-chip-x" aria-label=${"Remove the " + d.label.toLowerCase() + " filter"}
                  onClick=${() => d.set("")}><${Icon} name="close" /></button>
        </span>`)}
      ${on.length > 1 && html`
        <button class="btn quiet mini" onClick=${() => on.forEach((d) => d.set(""))}>Clear all</button>`}
    </div>`;
}

/* ── One record row ─────────────────────────────────────────────────────────── */
function Row({ c, active, compact, go, marked, cursor, onToggle, onRowPointerDown, onMenu }) {
  // Set on pointerdown when a modifier consumed the press, read on the click
  // that follows. A ref, not state: it must survive to the click in the SAME
  // gesture, and a setState here would re-render the row between the two.
  const consumed = useRef(false);
  // A row that the keyboard jumped to may not have existed when the key was
  // pressed — only the rows in view are drawn. This hands the focus over at the
  // moment the row mounts, if this is the row that was being waited for. Memoised
  // on the id so the ref identity is stable and preact does not detach and
  // reattach it on every render. NC-084.
  const claim = useCallback((el) => claimFocus(el, c.id), [c.id]);
  const msgs = Object.values(c.counts || {}).reduce((a, b) => a + (b || 0), 0);
  const place = c.home_metro || c.home_city;
  const topics = (c.topics || []).filter(Boolean);
  const tier = staleTier(c.last);
  const tag = STRENGTH_LABEL[c.strength_label] || c.strength_label || "";
  // One sentence for a screen reader, in the reading order of the columns. The
  // header row is decoration (aria-hidden), so the row has to carry its own
  // labels — including the empty ones, which are the point.
  const aria = [
    c.name, roleStr(c), place,
    c.last ? `last contact ${relDate(c.last)}` : "no contact yet",
    msgs ? `closeness ${tag}, ${c.strength}` : "no messages",
    // The row is a toggle, so the open row has to say what a second press does.
    // aria-current="true" announces "selected" but nothing about closing.
    active ? "selected, activate to close and show the full table" : "",
  ].filter(Boolean).join(", ");
  // The row toggles rather than only opening. Opening a person narrows the table
  // to a 340px rail and hides four of its columns, and there was NO way back to
  // the full table: the rail row you'd naturally click was already the open one,
  // clicking it re-navigated to the hash it was on (so hashchange never fired),
  // and clicking the People tab restored TAB_HASH["people"] — which is that same
  // person. Every route out led back in. So the open row closes, and main.js does
  // the same for a click on the already-active tab.
  // The mark control is a SIBLING of the row button, absolutely positioned over
  // the avatar — not a child. A <button> inside a <button> is invalid HTML, and
  // Safari resolves it by making the inner one unclickable, so the checkbox
  // would render perfectly and do nothing.
  return html`
    <div class=${"nx-rowwrap" + (marked ? " is-marked" : "")}
         data-row-id=${c.id}
         ref=${claim}
         onContextMenu=${e => onMenu?.(e, c)}>
      <button class=${"nx-mark" + (marked ? " on" : "")}
              aria-label=${(marked ? "Unmark " : "Mark ") + c.name}
              aria-pressed=${marked}
              onClick=${e => { e.stopPropagation(); onToggle?.(c.id); }}>
        <${Icon} name="check" />
      </button>
    <button class=${"nx-row" + (active ? " active" : "") + (marked ? " marked" : "") + (cursor ? " cursor" : "")}
            aria-label=${aria}
            aria-current=${active ? "true" : null}
            aria-selected=${marked ? "true" : null}
            title=${active ? "Click again to close and show the full table" : null}
            onPointerDown=${e => { if (onRowPointerDown?.(e, c.id)) consumed.current = true; }}
            onClick=${() => {
              // A ⇧ or ⌘ click already did its work on pointerdown; letting the
              // click through as well would open the person you just marked.
              if (consumed.current) { consumed.current = false; return; }
              go(active ? "people" : `people/${c.id}`);
            }}>
      <span class="nx-c nx-c-person">
        <${Avatar} c=${c} size=${22} ring=${compact} fontSize=${9} />
        ${/* No CatDot is drawn here. A dot for EVERY row would resolve, for a row with
              no category, to --cat-none (#d7d9dd) — 1.41:1 against the panel, where
              WCAG asks 3:1 of a graphic that carries meaning. It would read as "one
              person has a dot and nobody else does" when in truth everyone had one and
              only that person's was visible. In a typical network almost every contact
              has no category at all, so nearly all of these dots would be an invisible
              circle whose tooltip said "No category".

              It would also collide: --cat-personal and --ring-close are both #2f8a5b,
              so the dot and the avatar ring beside it would be the same green for two
              unrelated facts (personal contact / we talk a lot).

              There is no other signal to put here, because there isn't a spare one:
              status is "new" for almost everyone, and strength / role / topics / metro /
              last-contact are each already their own column or the ring. A dot here
              needs a fact the row doesn't already show — an owed reply, or an upcoming
              date — and both need a backend join into the contacts list.
              Category still has its picker on the person page, where a label carries it. */ ""}
        <span class="nx-name">${c.name}</span>
        ${c.semanticOnly && html`<span class="nx-hint" title="Matched by meaning, not by keyword">~</span>`}
      </span>
      <span class="nx-c nx-c-role">${roleStr(c) || html`<span class="nx-none">No role yet</span>`}</span>
      <span class="nx-c nx-c-loc">${place || html`<span class="nx-none">No location</span>`}</span>
      <span class="nx-c nx-c-topics">
        ${topics.length
          ? [
              // One topic, whole, plus a count — not two mutilated ones. At the
              // ~185px this column gets, two pills and a +N means flexbox
              // shrinks both proportionally and 'logistics' renders as 'lo…',
              // which carries less information than 'logistics +3' does. The
              // count's tooltip lists the rest, and the person's own page
              // shows every topic.
              html`<span class="tag topic" key=${topics[0]}>${topics[0]}</span>`,
              topics.length > 1
                ? html`<span class="tag tag-more" key="more" title=${topics.slice(1).join(", ")}>+${topics.length - 1}</span>`
                : null,
            ]
          : html`<span class="nx-none">No topics</span>`}
      </span>
      <span class=${"nx-c nx-c-last s-" + tier}>
        ${c.last ? relDate(c.last) : html`<span class="nx-none">No contact yet</span>`}
      </span>
      <span class="nx-c nx-c-strength">
        ${msgs
          ? html`
            <span class="nx-dot" style=${{ background: `var(--ring-${c.strength_label || "faint"})` }} />
            <span class="nx-str">${tag}</span>
            <span class="nx-score">${c.strength || 0}</span>`
          : html`<span class="nx-none">No messages</span>`}
      </span>
    </button>
    </div>`;
}

/* ── The table ──────────────────────────────────────────────────────────────── */
/* The next few personal dates, on the page you are already on (NC-040).
 *
 * Removing Today (NC-009) took the only aggregated view of upcoming dates with
 * it, so a birthday tomorrow surfaced nowhere — you had to already be looking at
 * that person. This is the narrow replacement, and the constraint that keeps it
 * from becoming Today again is that it renders NOTHING when nothing is coming
 * up: at rest it costs zero pixels and zero attention, same bargain as the
 * update banner in Settings.
 *
 * The server decides what counts (api_dates.UPCOMING_KINDS excludes calendar
 * meetings, which the calendar app already shows) and how far ahead to look.
 * This only renders. `more` exists so a truncated list says so rather than
 * silently looking complete. */
/** NC-319. Setup can start LinkedIn's data request, and the file arrives by
 *  email a day or two later. Until it is imported, one quiet line above the
 *  table says the app is waiting for it and where it goes, so someone who
 *  finished setup with few people knows more are coming. The engine ends the
 *  wait itself once a LinkedIn connection is in; Stop reminding me ends it now. */
function LinkedInWait({ go, hidden }) {
  const [wait, setWait] = useState(null);
  useEffect(() => {
    if (hidden) return;
    // Same rule as SoonStrip: a line that cannot load stays absent.
    api("/api/data/linkedin-wait").then(setWait).catch(() => setWait(null));
  }, [hidden]);
  if (hidden || !wait || !wait.waiting) return null;
  const stop = () => api("/api/data/linkedin-asked", { method: "POST", body: JSON.stringify({ asked: false }) })
    .then(setWait).catch(e => toast(e.message));
  return html`
    <div class="nx-note nx-wait" role="status">
      <span>${wait.says}</span>
      <button class="btn quiet mini" onClick=${() => go("data?row=linkedin_connections")}>Open the LinkedIn row</button>
      <button class="btn quiet mini" onClick=${stop}>Stop reminding me</button>
    </div>`;
}

function SoonStrip({ go, hidden }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (hidden) return;
    // No error branch: a strip that cannot load has nothing useful to say and
    // must not push the table down with an apology. It stays absent.
    api("/api/upcoming").then(setData).catch(() => setData(null));
  }, [hidden]);
  if (hidden || !data || !(data.shown || []).length) return null;
  return html`
    <div class="nx-soon">
      <span class="nx-soon-l">Coming up</span>
      ${/* Every chip goes somewhere: a date opens the person, a dated list opens
            the list. A chip with neither would be a button that does nothing,
            which is the defect NC-006 was about, so it renders as plain text
            instead of pretending to be pressable. */ ""}
      ${data.shown.map(d => {
        const to = d.contact_id ? `people/${d.contact_id}`
          : d.list_id ? `lists/${d.list_id}` : "";
        const body = html`
          <${Icon} name=${DATE_ICON[d.kind] || "star"} />
          <span class="nx-soon-n">${d.title}</span>
          <span class="nx-soon-w">${whenShort(d.days_until)}</span>`;
        const key = d.date_id ? "d" + d.date_id : "l" + d.list_id;
        return to
          ? html`<button class="nx-soon-i" key=${key} title=${d.reason}
                   onClick=${() => go(to)}>${body}</button>`
          : html`<span class="nx-soon-i flat" key=${key} title=${d.reason}>${body}</span>`;
      })}
      ${data.more > 0 && html`
        <span class="nx-soon-m">+${data.more} more in ${data.days} days</span>`}
    </div>`;
}

/* "today" / "tomorrow" / "in 5 days" — the strip's own short phrasing. The server
 * sends a full sentence in `reason`, which is the tooltip; the visible chip has
 * room for two words. */
function whenShort(days) {
  if (days <= 0) return "today";
  if (days === 1) return "tomorrow";
  return `in ${days} days`;
}


function PeopleTable({ selectedId, go, onVisible, preset, addNow, reloadToken }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState(null);
  const [hint, setHint] = useState("");  // semantic-search "not installed" note
  const [locations, setLocations] = useState([]);
  const [facetRoles, setFacetRoles] = useState([]);
  const [facetTopics, setFacetTopics] = useState([]);
  // Seeded from the quick command that brought us here, NOT applied afterwards
  // in an effect. Doing it in an effect meant the mount load (unfiltered) and the
  // preset load raced, and the loser won whenever it answered second — landing on
  // ?location=Berlin could show every capped row instead of the handful of people
  // in Berlin.
  const [loc, setLoc] = useState(preset?.location || "");
  const [role, setRole] = useState(preset?.role || "");
  const [topic, setTopic] = useState(preset?.topic || "");
  const [tier, setTier] = useState("");
  const [chan, setChan] = useState("");
  const [lastF, setLastF] = useState("");
  const [flt, setFlt] = useState(preset?.filter || "all");
  // "" = whatever the default sort is (closeness); the picker also offers
  // "quiet" (longest-neglected first), "recent" and "messages" (most exchanged)
  const [sort, setSort] = useState(preset?.sort || "");
  const [total, setTotal] = useState(null);   // how many match, before the page
  // Hidden contacts (NC-003: moved here from Settings — see HiddenNamesPanel).
  // Loaded once, independent of the search/filter state above, since it never
  // changes with a query — it is the same list whatever you're looking at.
  const [excl, setExcl] = useState(null);
  useEffect(() => {
    api("/api/exclude-list").then(d => setExcl(d.entries || [])).catch(() => setExcl([]));
  }, []);
  // How many rows to ask for. Lives in a ref rather than the argument list so
  // the existing load() call sites don't each have to thread it.
  const pageRef = useRef(PAGE);
  const compact = !!selectedId;

  const load = useCallback(async (query, location, filter, roleF, topicF, sortF, limit) => {
    const params = { q: query, filter, limit: String(limit || pageRef.current) };
    if (location) params.location = location;
    if (roleF) params.role = roleF;
    if (topicF) params.topic = topicF;
    // Closeness first by default: the list should open on the people who matter
    // most, not on whoever happens to be most recent. Whichever sort is active,
    // the server ranks it (see db.list_contacts) — re-sorting the fetched slice
    // here could never surface the genuinely quiet people "quiet" exists to show.
    const effSortF = sortF || "closeness";
    params.sort = effSortF;
    const kw = api("/api/contacts?" + new URLSearchParams(params))
      .then(d => { setTotal(typeof d.total === "number" ? d.total : null); return d.contacts || []; })
      .catch(() => []);
    if (!query) setHint("");
    const sem = query
      ? api("/api/search/semantic?" + new URLSearchParams({ q: query }))
          .then(d => { setHint(d.available === false ? (d.message || "") : ""); return d.available === false ? [] : (d.contacts || []); })
          .catch(() => [])
      : Promise.resolve([]);
    const [k, s] = await Promise.all([kw, sem]);
    const byId = new Map(k.map(c => [c.id, c]));
    for (const c of s) {
      if (location && c.home_metro !== location && c.home_city !== location) continue;
      if (roleF && c.role_type !== roleF) continue;
      if (topicF && !(c.topics || []).some(t => String(t).toLowerCase().includes(topicF.toLowerCase()))) continue;
      if (byId.has(c.id)) byId.get(c.id).score = c.score;
      else byId.set(c.id, { ...c, semanticOnly: true });
    }
    const merged = [...byId.values()].sort((a, b) => {
      if (!!a.semanticOnly !== !!b.semanticOnly) return a.semanticOnly ? 1 : -1;
      return (b.score || 0) - (a.score || 0) || (b.strength || 0) - (a.strength || 0);
    });
    setRows(merged);
  }, []);

  const debouncedLoad = useCallback(debounce((query, location, filter, roleF, topicF, sortF) => load(query, location, filter, roleF, topicF, sortF), 220), [load]);

  useEffect(() => { load(q, loc, flt, role, topic, sort); }, []);

  // A NEW instruction while we're already mounted — running ⌘K → "People in
  // Paris" from a People page you're already on. The mount state above already
  // reflects whatever the hash said at mount, so this ref starts there and the
  // effect no-ops on the first pass instead of firing a duplicate load.
  //
  // Never cleared when a param goes away: opening a person rewrites the hash to
  // people/<id> with no query at all, and that must not silently undo the filter
  // you just asked for.
  const presetSig = [preset?.filter, preset?.sort, preset?.location, preset?.role, preset?.topic].join("|");
  const applied = useRef(presetSig);
  useEffect(() => {
    if (presetSig === applied.current) return;
    applied.current = presetSig;
    const f = preset.filter || flt, s = preset.sort || sort;
    const l = preset.location || loc, r = preset.role || role, t = preset.topic || topic;
    setFlt(f); setSort(s); setLoc(l); setRole(r); setTopic(t);
    load(q, l, f, r, t, s);
  }, [presetSig]);

  useEffect(() => {
    api("/api/locations").then(d => setLocations(d.locations || [])).catch(() => {});
    api("/api/facets").then(d => { setFacetRoles(d.roles || []); setFacetTopics(d.topics || []); }).catch(() => {});
  }, []);

  // Closeness (strength_label), Channel (per-channel counts) and Last contact are
  // all derived per row rather than stored, so none of them can be a SQL
  // predicate — they have to be applied here. Which means they are only truthful
  // over the whole matching set: applied to the first page they answered "who
  // among these 500" while looking exactly like an answer about everyone.
  // Turning any of them on refetches unpaged (see pageFor).
  const pageFor = (t, c, l) => (t || c || l ? ALL : PAGE);
  const pred = lastPred(lastF);
  const visible = (rows || []).filter(c =>
    (!tier || c.strength_label === tier) &&
    (!chan || (c.counts && c.counts[chan] > 0)) &&
    (!pred || pred(daysSince(c.last))));

  // Closeness first by default (the server already ranked it). The quiet/recent
  // orders stay available in the picker; never-contacted rows sink in those,
  // since they're strangers rather than neglected friends.
  const effSort = sort || "closeness";
  const sorted = orderRows(visible, effSort);

  // Share the visible order with the person page's prev/next stepper.
  const idsKey = sorted.map(c => c.id).join(",");
  useEffect(() => { onVisible?.(sorted.map(c => c.id)); }, [idsKey]);

  // ── Marking ─────────────────────────────────────────────────────────────────
  // Keyed off idsKey rather than `sorted` so the identity only changes when the
  // visible SET changes. Rebuilding this array every render would re-run the
  // selection hook's effects (including the marquee's pointer listeners) on
  // every keystroke in the search box.
  const bodyRef = useRef(null);
  // Which rows to actually draw. `sorted` stays the WHOLE list everywhere else in
  // this component — only the map that produces <Row> elements is windowed. That
  // line matters: idsKey, onVisible, the selection hook's `ids` below and the
  // "add these N to a list" footer all read the full array, and windowing any of
  // them would silently shrink select-all, shift-click ranges, the person page's
  // previous/next stepper and bulk list-adding to one screenful. NC-084.
  const win = useRowWindow({ count: sorted.length, ref: bodyRef });
  const ids = useMemo(() => sorted.map(c => c.id), [idsKey]);
  const sel = useRowSelection({
    ids, bodyRef,
    onOpen: (id) => go(`people/${id}`),
  });
  const { menu, openMenu, closeMenu } = useContextMenu();
  // Right-clicking a row that is NOT part of the current selection should act on
  // that row alone — matching every file manager. Right-clicking inside the
  // selection keeps it, so "add these 12 to a list" survives the right-click.
  const menuTargets = (c) => (sel.isSelected(c.id) ? ids.filter(i => sel.isSelected(i)) : [c.id]);

  // ONE owner for the count (see countState). Every number on this screen — the
  // toolbar, the Load-all button, the bulk-add label — comes out of this object.
  const count = countState(sorted.length, (rows || []).length, total);

  // Bulk add-to-list only makes sense on a list you've actually narrowed. On the
  // default view the count is just the server's page cap (the first 500 of a
  // much larger network), so "add these 500" both means nothing and invites a mistake.
  const narrowed = !!(q.trim() || loc || role || topic || tier || chan || lastF || flt !== "all");

  // The filterable dimensions, in one array: the Filter popover, the badge and
  // the applied chips all read it, so they can never disagree about what is on.
  const reload = (over = {}) => {
    const s = { loc, role, topic, tier, chan, last: lastF, flt, sort, ...over };
    pageRef.current = pageFor(s.tier, s.chan, s.last);
    load(q, s.loc, s.flt, s.role, s.topic, s.sort);
  };

  // Re-run the CURRENT query when the person page saves an edit, so the rail picks
  // up a new name or role. PersonPage's reloadAll() asks for this through the
  // reloadList prop People passes down; without it the rail would keep the old name
  // and the old role after a rename.
  //
  // A nonce rather than handing reload() upward through a ref: reload closes over
  // the live filter state, so calling it from this render is what keeps the
  // refetch on the same query the user is looking at. The mount load has its own
  // effect, hence the skip — otherwise every open of the People view fetched twice.
  const firstToken = useRef(true);
  useEffect(() => {
    if (firstToken.current) { firstToken.current = false; return; }
    reload();
  }, [reloadToken]);
  const dims = [
    { key: "location", label: "Location", icon: "pin", any: "Anywhere", value: loc,
      options: locations.map(l => ({ value: l, label: l })), searchable: true, text: (v) => v,
      set: (v) => { setLoc(v); reload({ loc: v }); } },
    { key: "role", label: "Role", icon: "work", any: "Any role", value: role,
      options: facetRoles.map(r => ({ value: r, label: r })), searchable: true, text: (v) => v,
      set: (v) => { setRole(v); reload({ role: v }); } },
    { key: "topic", label: "Topic", icon: "tag", any: "Any topic", value: topic,
      options: facetTopics.map(t => ({ value: t, label: t })), searchable: true, text: (v) => v,
      set: (v) => { setTopic(v); reload({ topic: v }); } },
    { key: "last", label: "Last contact", icon: "clock", any: "Any time", value: lastF,
      options: LAST_FILTERS.map(([v, l]) => ({ value: v, label: l })), text: lastLabel,
      set: (v) => { setLastF(v); reload({ last: v }); } },
    { key: "closeness", label: "Closeness", icon: "bolt", any: "Any closeness", value: tier,
      options: STRENGTH_TIERS.map(([v, l]) => ({ value: v, label: l })), text: (v) => STRENGTH_LABEL[v] || v,
      set: (v) => { setTier(v); reload({ tier: v }); } },
    { key: "channel", label: "Channel", icon: "chat", any: "Any channel", value: chan,
      options: CHANNEL_ORDER.map(c => ({ value: c, label: CHANNEL_LABEL[c] })), text: (v) => CHANNEL_LABEL[v] || v,
      set: (v) => { setChan(v); reload({ chan: v }); } },
  ];

  // Saved views: the current state either matches one by name, or it is Custom.
  const views = useMemo(() => VIEWS.concat(roleViews(facetRoles)), [facetRoles]);
  const state = { filter: flt, sort: effSort, location: loc, role, topic, last: lastF, tier, chan };
  const activeView = views.find((v) => {
    const p = viewParams(v);
    return Object.keys(VIEW_DEFAULTS).every((k) => p[k] === state[k]);
  });
  const applyView = (v) => {
    const p = viewParams(v);
    setFlt(p.filter); setSort(p.sort); setLoc(p.location); setRole(p.role); setTopic(p.topic);
    setLastF(p.last); setTier(p.tier); setChan(p.chan); setQ("");
    pageRef.current = pageFor(p.tier, p.chan, p.last);
    load("", p.location, p.filter, p.role, p.topic, p.sort);
  };

  // Arrow keys walk the rows; Enter and Tab keep working because every row is a
  // real button.
  // keyNav used to live here: eight lines that moved DOM focus between rows on
  // ↑/↓, and only once a row already had focus. useRowSelection replaces it with
  // the full contract — roving cursor, ⇧ to extend while moving, space to mark,
  // Home/End, ⌘A, Esc — and keeps the focus-moving behaviour this had.

  return html`
    <section class="nx-list">
      <div class="nx-tools">
        <${Pill} look="view" label=${activeView ? activeView.name : "Custom view"}
          title="Saved views"
          pop=${(close) => html`
            <div class="omni-head">Views</div>
            ${views.map((v) => html`
              <${PopRow} key=${v.key} name=${v.name} note=${v.note} sel=${activeView?.key === v.key}
                onClick=${() => { applyView(v); close(); }} />`)}`} />
        <input class="input nx-search" placeholder="Search or describe someone…" value=${q}
          aria-label="Search people"
          onInput=${e => { setQ(e.target.value); debouncedLoad(e.target.value, loc, flt, role, topic, sort); }} />
        ${/* The rail has ~324px of toolbar: the order name would wrap the row and
              cost a table row, so there it is just "Sort" with its badge. */ ""}
        <${Pill} label=${compact ? "Sort" : sortLabel(effSort)} icon="sort" on=${effSort !== "closeness"}
          badge=${effSort !== "closeness" ? 1 : 0} title="Sort the table"
          pop=${(close) => html`
            <div class="omni-head">Sort by</div>
            ${SORTS.map(([v, l]) => html`
              <${PopRow} key=${v} name=${l} sel=${v === effSort}
                onClick=${() => { setSort(v); reload({ sort: v }); close(); }} />`)}`} />
        <${FilterPill} dims=${dims} />
        <span class="nx-spacer"></span>
        ${rows !== null && html`
          <span class="nx-count" aria-live="polite"><b>${count.lead}</b> ${count.tail}</span>`}
        ${count.capped && html`
          <button class="btn quiet mini" title="Load every matching person, then filter over the whole network"
            onClick=${() => { pageRef.current = ALL; load(q, loc, flt, role, topic, sort); }}>${count.loadAll}</button>`}
        ${/* Hidden in the rail for the same reason SoonStrip is below: 324px
              cannot hold a fourth control, and hiding a name can wait until
              the full table is back on screen. */ !compact && html`
          <${Pill} look="ghost" label="Hidden" icon="ban" badge=${(excl || []).length}
            title="Hidden from every view, search and AI answer. Their rows stay in data/ — hiding is a filter, not a delete."
            pop=${() => html`<${HiddenNamesPanel} entries=${excl} onEntries=${setExcl} />`} />`}
        <${AddPerson} go=${go} openNow=${addNow} />
      </div>
      <${AppliedChips} dims=${dims} />
      ${/* hidden in the rail: 324px of width cannot hold three names and the
            table rows are what that space is for. */ ""}
      <${SoonStrip} go=${go} hidden=${compact} />
      <${LinkedInWait} go=${go} hidden=${compact} />
      ${hint && html`<div class="nx-note">${hint}</div>`}
      <div class="nx-table">
        <div class="nx-head" aria-hidden="true">
          ${COLS.map(col => html`
            <span class=${"nx-c nx-h nx-c-" + col.key} key=${col.key}>
              <${ColIcon} name=${col.icon} /><span>${col.label}</span>
            </span>`)}
        </div>
        <div class=${"nx-body" + (sel.marquee ? " banding" : "")} ref=${bodyRef} onKeyDown=${sel.onKeyDown}>
          ${/* A token expression, not 26: SkelRows takes any CSS length and a bare
                number ships style="height:26px" — a literal px past every grep that
                looks for `font-size:` or for px inside a style string. Multiples of
                --row-h are the same idiom ui.js's AV scale uses. */ ""}
          ${rows === null && html`<${SkelRows} n=${12} h="calc(var(--row-h) * .65)" />`}
          ${rows !== null && sorted.length === 0 && html`
            <${Empty}>No one matches. Clear a filter, or add someone.</${Empty}>`}
          ${/* Two empty spacers stand in for the rows that are not drawn, so the
                scrollbar is still the length of the whole list and row n still sits
                at exactly n x --row-h. Their heights are computed pixels and so
                have to be inline — there is no CSS expression for "a few thousand rows tall".
                aria-hidden because they are geometry, not content. */ ""}
          ${win.padTop > 0 && html`
            <div class="nx-pad" style=${"height:" + win.padTop + "px"} aria-hidden="true"></div>`}
          ${sorted.slice(win.start, win.end).map(c => html`
            <${Row} key=${c.id} c=${c} active=${c.id === selectedId} compact=${compact} go=${go}
                    marked=${sel.isSelected(c.id)} cursor=${sel.focusId === c.id}
                    onToggle=${sel.toggle} onRowPointerDown=${sel.onRowPointerDown}
                    onMenu=${openMenu} />`)}
          ${win.padBottom > 0 && html`
            <div class="nx-pad" style=${"height:" + win.padBottom + "px"} aria-hidden="true"></div>`}
        </div>
      </div>
      ${narrowed && sorted.length > 0 && html`
        <div class="nx-foot">
          <${AddToList} contactIds=${sorted.map(c => c.id)} dropUp=${true} alignLeft=${true}
            btnClass="btn quiet mini" trigger=${count.bulk} />
        </div>`}
      <${Marquee} rect=${sel.marquee} />
      ${sel.count > 0 && html`
        <div class="sel-bar" role="status">
          <span class="sel-bar-count">${sel.count} selected</span>
          <${AddToList} contactIds=${[...sel.selected]} dropUp=${true} alignLeft=${true}
            btnClass="btn mini" trigger=${html`<${Icon} name="plus" /> Add to list`}
            onDone=${sel.clear} />
          <button class="btn mini" onClick=${sel.clear}>Clear</button>
          <span class="sel-bar-hint">esc</span>
        </div>`}
      ${menu && html`
        <${ContextMenu} x=${menu.x} y=${menu.y} onClose=${closeMenu}
          head=${html`<${PersonPeek} c=${menu.payload} n=${menuTargets(menu.payload).length} />`}
          items=${personMenuItems(menu.payload, menuTargets(menu.payload), go, sel.clear)} />`}
    </section>`;
}

function AddPerson({ go, openNow }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [match, setMatch] = useState(null);  // existing same-name contact (duplicate check)

  // ⌘K → "Add someone new" lands on people?new=1 and expects the form open,
  // with the cursor already in it (the autofocus below does that half).
  useEffect(() => { if (openNow) setOpen(true); }, [openNow]);

  const create = async () => {
    const n = name.trim();
    try {
      const d = await api("/api/contacts", { method: "POST", body: JSON.stringify({ name: n }) });
      if (company.trim()) await saveField(d.id, "company", company.trim()).catch(() => {});
      toast(`Added ${n}`);
      setName(""); setCompany(""); setMatch(null); setOpen(false);
      go(`people/${d.id}`);
    } catch (e) { toast(e.message); }
  };
  const add = async () => {
    const n = name.trim();
    if (!n) return;
    try {  // warn before creating a second contact with the exact same name
      const d0 = await api("/api/contacts?" + new URLSearchParams({ q: n }));
      const m = (d0.contacts || []).find(c => (c.name || "").trim().toLowerCase() === n.toLowerCase());
      if (m) { setMatch(m); return; }
    } catch { /* if the check fails, don't block adding */ }
    create();
  };
  const keys = e => { if (e.key === "Enter") add(); if (e.key === "Escape") setOpen(false); };
  return html`
    <div class="nx-pw">
      <button class="nx-pill" aria-expanded=${open} onClick=${() => setOpen(o => !o)}>
        <span class="nx-pill-l"><${Icon} name="plus" /> Add</span>
      </button>
      ${open && html`
        <div class="add-pop">
          <div class="microlabel">New person</div>
          <input class="input" autofocus placeholder="Their name" value=${name}
            onInput=${e => { setName(e.target.value); setMatch(null); }} onKeyDown=${keys} />
          <input class="input" placeholder="Company (optional)" value=${company}
            onInput=${e => setCompany(e.target.value)} onKeyDown=${keys} />
          ${match && html`
            <div class="add-dup">
              “${match.name}” is already in your network.
              <div class="add-dup-acts">
                <button class="btn quiet mini" onClick=${() => { setMatch(null); setName(""); setOpen(false); go(`people/${match.id}`); }}>Open them</button>
                <button class="btn primary mini" onClick=${create}>Add anyway</button>
              </div>
            </div>`}
          <div class="add-pop-foot">
            <button class="btn quiet mini" onClick=${() => { setOpen(false); setMatch(null); }}>Cancel</button>
            <button class="btn primary mini" disabled=${!name.trim()} onClick=${add}>Add</button>
          </div>
        </div>`}
    </div>`;
}

/* ── Person page ──────────────────────────────────────────────────────────────
 * The reasoning and every value live in the "Person record" block of static/css/ui.css. The pane
 * answers, in this order:
 *
 *   1. "who is this again?"       identity band + vital-signs strip
 *   2. "should I do something?"   action cluster + next-action card
 *   3. "what has happened?"       story, then timeline
 *   4. "let me correct something" details, dates, mutual connections
 *
 * The principle is ACTION BEFORE DATA, so the reminder card sits above anything
 * merely readable. Curation comes last because it is a different register from
 * consumption, and the record page used to open as five focused-looking inputs and
 * two textareas — which reads as data entry, not as a record.
 *
 * HOW THIS REGION IS BUILT. The endpoints and the request shapes are the ones the
 * page has always used, and it fetches nothing beyond them. What matters in the
 * structure:
 *
 *   · Structure. The taxonomy row, the six field editors and the date chips sit in
 *     .pd-props, the key/value block.
 *   · Handlers. saveOne, the name onChange, locSave and saveHowWeMet reload after a
 *     write, so no stale second copy of the field stays on screen. locSave is
 *     curried, so PropRow gets a plain (value) => Promise.
 *   · State. PropRow holds per-row editing/draft state; the chip composers and the two
 *     popovers hold a focus-return flag; NextCard holds its snoozed line.
 *   · Listeners. useDismiss registers a document mousedown + keydown while either
 *     popover is open.
 *   · Sorting. tsKey pads a date-only stamp to 23:59:59, so a same-day milestone sorts
 *     to the head of its day instead of under that day's first conversation.
 *   · Actions that leave the app: ChannelActions' mailto:/tel: anchors hand off to an
 *     external app, and EpisodeEntry shows a truncation line.
 *
 * Inline styles: none are string literals — every px font-size, padding, gap, margin
 * and width is a token in a .pd-* class in ui.css. Two survive, and only of the
 * dynamic kind: a computed token NAME interpolated into a CSS
 * custom property (the closeness ring tier, the category colour). They hardcode
 * nothing, and they are the same mechanism api.strengthRing already uses for the avatar
 * ring, so keeping them leaves one idiom for one job instead of two. Each is marked
 * KEPT INLINE at its call site with the reason. Every SkelRows height in this file is
 * a token expression too: `h` accepts any CSS length, so a bare number would
 * put `style="height:30px"` past a grep for `font-size:` and `style="…px`.
 */
function useContact(id) {
  const [c, setC] = useState(null);
  const [err, setErr] = useState("");
  const reload = useCallback(async () => {
    if (!id) return;
    try { setC(await api("/api/contact/" + id)); setErr(""); }
    catch (e) { setErr(e.message); }
  }, [id]);
  useEffect(() => { setC(null); reload(); }, [id, reload]);
  return [c, reload, err];
}

/** Outside-click and Escape dismissal for this pane's two popovers, with focus
 *  handed back to the trigger.
 *
 *  A popover that closes only when its own trigger is clicked again leaves the
 *  keyboard and the mouse with no other way out. Pill in the table half of this file
 *  closes on Escape and on an outside click, and every popover behaves the same under
 *  the mouse, the keyboard and Escape. One hook, both call sites. */
function useDismiss(open, close, ref) {
  useEffect(() => {
    if (!open) return;
    const away = (e) => { if (ref.current && !ref.current.contains(e.target)) close(); };
    const esc = (e) => {
      if (e.key !== "Escape") return;
      close();
      const el = ref.current;
      (el?.tagName === "BUTTON" ? el : el?.querySelector("button"))?.focus();
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
}

function PhotoUpload({ c, onDone }) {
  const fileRef = useRef(null);
  const pick = () => fileRef.current?.click();
  const upload = (file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        await api(`/api/contact/${c.id}/photo`, { method: "POST", body: JSON.stringify({ data: reader.result }) });
        toast("Photo saved");
        onDone();
      } catch (e) { toast(e.message); }
    };
    reader.readAsDataURL(file);
  };
  // The avatar sits in a real button that names what it does, so a keyboard or
  // screen-reader user can set a photo. The file input behind it is hidden and out of
  // the tab order; the button proxies it.
  return html`
    <div class="pd-photo">
      <button type="button" class="pd-photo-btn" title="Set a photo"
              aria-label=${`Set a photo for ${c.name}`} onClick=${pick}>
        <${Avatar} c=${c} size="var(--pd-av)" ring />
      </button>
      <input class="pd-file" ref=${fileRef} type="file" accept="image/*" tabindex="-1" aria-hidden="true"
        onChange=${e => upload(e.target.files[0])} />
    </div>`;
}

/** Save one text field on a person. Every caller is somebody editing that box on
 *  purpose, so an empty value is sent with `clear: true`: without it the engine
 *  refuses to blank a field that already holds text (so a stray blur cannot wipe
 *  it) and answers `unchanged`, and the box looked saved while the old text stayed.
 *  If the engine still answers `unchanged`, the old text is still there, so this
 *  rejects and the caller's own error toast says so instead of "Saved". */
function saveField(id, field, value) {
  const text = String(value ?? "");
  return api(`/api/contact/${id}/field`, {
    method: "POST", body: JSON.stringify({ field, value: text, clear: text === "" }),
  }).then(d => {
    if (d && d.unchanged) throw new Error("Nothing was changed. The old text is still there.");
    return d;
  });
}

/** Why-this-score popover: the familiarity model's inputs as meter bars.
 *
 *  The popover is a SIBLING of the trigger inside .pstat.why, never a child of it. A
 *  button takes phrasing content, so a role="dialog" holding six meter <div>s and a <p>
 *  inside one would be an invalid content model, and because a button computes its name
 *  from its content, the trigger would announce every number in the dialog while the
 *  six numbers, the point of the control, were unreachable as content. As a sibling, a
 *  click in the dialog is not a click on the button that opened it, so no
 *  stopPropagation is needed. */
function ClosenessPopover({ c }) {
  const p = c.strength_parts || {};
  const gapText = p.days_since == null
    ? "no touchpoint yet"
    : p.days_since === 0 ? "in touch today"
    : `quiet ${p.days_since} day${p.days_since === 1 ? "" : "s"}`;
  const recip = p.reciprocity ?? 1;
  return html`
    <div class="omni-pop pd-why-pop" role="dialog"
         aria-label=${`Why closeness ${c.strength || 0}`}>
      <div class="omni-head">Why ${c.strength} — bond × warmth</div>
      <${Meter} label="Shared days" value=${p.days} hint=${`${(p.active_days || 0).toLocaleString()} days`} />
      <${Meter} label="Tenure" value=${p.tenure} />
      <${Meter} label="Volume" value=${p.volume} hint=${`${(p.total_messages || 0).toLocaleString()} msgs`} />
      <${Meter} label="Curation" value=${p.depth} />
      ${/* hint is null above .36, never "": Meter falls back with `hint ?? pct + "%"`,
            and ?? substitutes only on null/undefined, so an empty string would render
            no value at all, and the bar track is aria-hidden, so that row would say
            nothing to anyone. null takes the percentage fallback Meter documents. */ ""}
      <${Meter} label="Reciprocity" value=${recip} hint=${recip <= 0.36 ? "one-way" : null} />
      <${Meter} label="Warmth" value=${p.warmth} hint=${gapText} />
      <p class="pd-why-note">
        Bond = shared days · tenure · volume · curation, scaled by reciprocity and ${p.channels_used || 0}${" "}
        channel${(p.channels_used || 0) === 1 ? "" : "s"}. Warmth is recency against
        your own rhythm — it cools a score but never erases history.
      </p>
    </div>`;
}

/** Editable phone/email chips, so a wrong number can be fixed in-app. Add via the
 * composer, remove with the × — removal only drops the mapping; archived history stays
 * with the contact.
 *
 * Lives in the Details block now rather than the identity band: an identifier is a
 * property you curate, and the band above acts on it (see ChannelActions). */
function IdentifiersEditor({ c, reload }) {
  const [adding, setAdding] = useState(false);
  const [kind, setKind] = useState("phone");
  const [val, setVal] = useState("");
  const ids = c.identifiers || [];
  // Opening the composer replaces the trigger button with it, which destroys the
  // focused element, and the composer's field cannot rely on the `autofocus` attribute,
  // which is inert on an element Preact inserts after load (see PropRow). Closing it
  // destroys the field the same way and would move focus nowhere, dropping the user to
  // <body>. One effect owns both directions: into the field on open, back to the
  // trigger on a keyboard close.
  const trigRef = useRef(null);
  const fieldRef = useRef(null);
  const back = useRef(false);
  useEffect(() => {
    if (adding) fieldRef.current?.focus();
    else if (back.current) { back.current = false; trigRef.current?.focus(); }
  }, [adding]);
  const stop = () => { back.current = true; setAdding(false); };
  const post = (extra) => api(`/api/contact/${c.id}/identifier`, { method: "POST", body: JSON.stringify(extra) });
  const add = async () => {
    if (!val.trim()) return;
    try { await post({ kind, value: val.trim() }); setVal(""); stop(); reload(); }
    catch (e) { toast(e.message); }
  };
  const remove = async (i) => {
    try { await post({ kind: i.kind, value: i.value, remove: true }); reload(); }
    catch (e) { toast(e.message); }
  };
  return [
    ...ids.map(i => html`
      ${/* The inline style here restated the .tag base verbatim (inline-flex,
            align-items:center, gap:5px) on an element already carrying .tag. */ ""}
      <span class="tag" key=${i.kind + i.value}>
        <${Icon} name=${i.kind === "phone" ? "phone" : "email"} /> ${i.value}
        ${/* The aria-label says which number or address this deletes: a <button> takes
              its name from its content before its title, so N identical remove buttons
              would otherwise all announce as "×". */ ""}
        <button class="tag-x" aria-label=${`Remove ${i.value}`} onClick=${() => remove(i)}><${Icon} name="close" /></button>
      </span>`),
    adding
      ? html`<span key="add" class="pd-compose">
          <select class="input mini" aria-label="Identifier type" value=${kind}
                  onChange=${e => setKind(e.target.value)}>
            <option value="phone">phone</option><option value="email">email</option>
          </select>
          <input class="input mini" ref=${fieldRef}
            aria-label=${kind === "phone" ? "Phone number" : "Email address"}
            placeholder=${kind === "phone" ? "+46…" : "name@…"} value=${val}
            onInput=${e => setVal(e.target.value)}
            onKeyDown=${e => { if (e.key === "Enter") add(); if (e.key === "Escape") stop(); }} />
          <button class="btn mini" onClick=${add}>Add</button>
        </span>`
      : html`<button key="addbtn" class="tag" ref=${trigRef} onClick=${() => setAdding(true)}><${Icon} name="plus" /> number / email</button>`,
  ];
}

/** Channel quick-actions: open the profile, write, call. These read the
 *  linkedin_url and identifiers the record already holds — no new request, no new
 *  state, and each is a plain anchor, so there is nothing to fail. Brand colour is
 *  legitimate here and nowhere else on the page: channel identity is one of the three
 *  meanings tokens.css spends colour on. Editing these values happens in Details. */
function ChannelActions({ c }) {
  const ids = c.identifiers || [];
  const email = ids.find(i => i.kind === "email");
  const phone = ids.find(i => i.kind === "phone");
  const out = [];
  if (c.linkedin_url) out.push(html`
    <a key="li" class="pd-ch-btn ch-linkedin" href=${c.linkedin_url} target="_blank" rel="noopener"
       title="Open the LinkedIn profile" aria-label="Open the LinkedIn profile">in</a>`);
  if (email) out.push(html`
    <a key="em" class="pd-ch-btn ch-email" href=${"mailto:" + email.value}
       title=${"Write to " + email.value} aria-label=${"Write to " + email.value}><${Icon} name="email" /></a>`);
  if (phone) out.push(html`
    <a key="ph" class="pd-ch-btn ch-imessage" href=${"tel:" + phone.value}
       title=${"Call " + phone.value} aria-label=${"Call " + phone.value}><${Icon} name="phone" /></a>`);
  if (!out.length) return null;
  return html`<span class="pd-ch">${out}</span>`;
}

/** The vital-signs strip. Four tiles: Closeness · Last touch · Known
 *  since · Messages — the two numbers this app exists to compute, plus the two facts
 *  that qualify them.
 *
 *  It replaces up to eleven .pstat tiles, because the seven per-channel counts each
 *  rendered as another tile of exactly the same shape and flattened the closeness tile
 *  beside them. They are now one sub-line of coloured dots. */
function VitalStrip({ c, showWhy, setShowWhy }) {
  const counts = c.counts || {};
  const msgs = Object.values(counts).reduce((a, b) => a + (b || 0), 0);
  const tierKey = c.strength_label || "faint";
  // The same mapping as the table row: STRENGTH_LABEL turns the lowercase value that
  // db.strength_label() returns into "Close"/"Active"/"Loose"/"Faint", so in split view
  // the rail and the pane beside it say the same word for the same person.
  const tag = STRENGTH_LABEL[tierKey] || tierKey;
  const stale = staleTier(c.last);
  const since = c.connected_on || c.first_contact;
  const whyRef = useRef(null);
  useDismiss(showWhy, () => setShowWhy(false), whyRef);
  return html`
    <div class="person-stats">
      ${/* .pstat.why is the positioning wrapper, NOT the control: the breakdown dialog
            is a sibling of the trigger, never a child of it (see ClosenessPopover).
            useDismiss holds this wrapper, so a click inside the dialog is still
            "inside", and Escape finds the trigger with its querySelector("button"). */ ""}
      <div class="pstat why" ref=${whyRef}>
        <button class="pd-why-btn" type="button" title="Why this score?"
                aria-expanded=${showWhy} aria-haspopup="dialog"
                onClick=${() => setShowWhy(w => !w)}>
          ${/* ⓘ is decoration on a control that already says what it does, and a
                screen reader would otherwise read its Unicode name into the button's
                accessible name. */ ""}
          <span class="lbl">Closeness <${Icon} name="info" /></span>
          <span class="pd-close">
            ${/* KEPT INLINE, deliberately: the value is a token NAME computed from the
                  tier, so nothing is hardcoded, and this is the same mechanism
                  api.strengthRing uses for the avatar ring and Row uses for .nx-dot.
                  Enumerating four rules here instead would give one job two idioms. */ ""}
            <span class="pd-dot" style=${{ background: `var(--ring-${tierKey})` }} />
            <b>${c.strength || 0}</b>
            <span class="pd-tier">${tag}</span>
          </span>
        </button>
        ${/* The row prints "No messages" and suppresses the score when every channel
              count is 0. Strength can be non-zero with no messages, because
              strength_breakdown earns depth from status/how_we_met/brief alone, so this
              tile states the same fact in the row's words; otherwise the rail would say
              "No messages" beside a pane showing a closeness number. It sits outside the
              button: it is a fact about the person, not part of the control's name. */ ""}
        ${!msgs && html`<span class="pd-sub">No messages</span>`}
        ${showWhy && html`<${ClosenessPopover} c=${c} />`}
      </div>
      <div class=${"pstat s-" + stale}>
        <span class="lbl">Last touch</span>
        <b>${c.last ? relDate(c.last) : "No contact yet"}</b>
      </div>
      <div class="pstat">
        <span class="lbl">${c.connected_on ? "Connected" : "First contact"}</span>
        <b>${since ? fmtMonthYear(since) : html`<span class="pd-ghost">Not known</span>`}</b>
      </div>
      <div class="pstat">
        <span class="lbl">Messages</span>
        <b>${msgs.toLocaleString()}</b>
        ${/* The channel's name is visible text beside each count. A `title` is
              pointer-only, so it reaches neither the keyboard nor touch, and colour
              cannot be the sole carrier of which channel a count belongs to: the seven
              hues are not separable by luminance (--ch-imessage against --ch-whatsapp
              is 1.00:1, and so is --ch-email against --ch-linkedin, in both themes),
              which is the failure ui.css argues against for links. The dot stays as
              redundant hue and is aria-hidden because the text beside it says the same
              thing. */ ""}
        ${msgs > 0 && html`
          <span class="pd-chan">
            ${CHANNEL_ORDER.filter(k => counts[k]).map(k => html`
              <span class="pd-chan-c" key=${k}>
                <span class=${"pd-chan-d ch-" + k} aria-hidden="true" />
                ${/* The count is its own element, not a bare text node beside the name:
                      the flex gap spaces them visually either way, but a text node
                      concatenates into "LinkedIn120" for anything reading textContent. */ ""}
                <span class="pd-chan-n">${CHANNEL_LABEL[k] || k}</span>
                <span class="pd-chan-v">${counts[k].toLocaleString()}</span>
              </span>`)}
          </span>`}
      </div>
    </div>`;
}

/** The identity band, the action cluster and the vital strip.
 *
 *  The taxonomy row holds category, plus an excluded or deleted marker when either
 *  applies — there is no c.status field anywhere in the app. Topics, location, role type and the
 *  identifier chips left the row for Details: they are properties you curate, and
 *  eight pills of four different kinds on one row gave the name nothing to lead.
 *
 *  All three markers are conditional, so this row can render empty and still take
 *  its top margin; .person-meta-row:empty in ui.css collapses it. */
function Header({ c, reload, listIds, go, openMerge, openDraft }) {
  const [showWhy, setShowWhy] = useState(false);
  const [draft, setDraft] = useState(false);
  // Same one-shot contract HeadMenu uses for openMerge: the nonce in the hash
  // makes it repeatable, so right-clicking two people in a row opens the drafter
  // twice rather than once.
  useEffect(() => { if (openDraft) setDraft(true); }, [openDraft]);
  // Stepper: flip through the current filtered list. Hidden when
  // the list has one row or this person fell out of it.
  const stepIdx = (listIds || []).indexOf(c.id);
  const showStep = (listIds || []).length > 1 && stepIdx >= 0;
  // One field per input — no more parsing a single "Role · Company" box, which
  // silently blanked whichever half you didn't retype.
  //
  // saveOne and the name onChange below reload after saving, as PhotoUpload and
  // IdentifiersEditor do, so in split view the rail row picks up a new name and
  // roleStr(c) after a rename. `reload` here is PersonPage's reloadAll, which is exactly
  // what refreshes the rail.
  const saveOne = (field, v) => saveField(c.id, field, v.trim())
    .then(() => { toast("Saved"); reload(); }).catch(e => toast(e.message));
  return html`
    <div class="person-head">
      <${PhotoUpload} c=${c} onDone=${reload} />
      <div class="person-id">
        ${/* An aria-label, because the field has no visible label and no placeholder;
              without one it would announce as an unlabeled text box holding a name. */ ""}
        <input class="person-name-input" aria-label="Name" value=${c.name}
          onChange=${async e => { const v = e.target.value.trim(); if (!v) return reload(); try { await saveField(c.id, "name", v); toast("Saved"); reload(); } catch (er) { toast(er.message); } }} />
        <div class="pd-idline">
          <input class="person-role-input" aria-label="Role" value=${c.position || ""} placeholder="Role"
            onChange=${e => saveOne("position", e.target.value)} />
          <span class="pd-sep" aria-hidden="true">·</span>
          <input class="person-role-input" aria-label="Company" value=${c.company || ""} placeholder="Company"
            onChange=${e => saveOne("company", e.target.value)} />
        </div>
        ${c.blurb && html`<div class="person-blurb">${c.blurb}</div>`}
        <div class="person-meta-row">
          ${/* KEPT INLINE, deliberately: CAT_COLOR's values are already var(--cat-*)
                strings, so nothing is hardcoded, and ui.css documents "set --cat-col
                inline" as .tag.cat's own contract. CatDot in ui.js uses the same
                mechanism, so converting one without the other would leave two idioms
                for one job. */ ""}
          ${c.category && c.category !== "unknown" && html`
            <span class="tag cat" style=${`--cat-col:${CAT_COLOR[c.category] || "var(--cat-none)"}`}>${CAT_LABEL[c.category]}</span>`}
          ${c.excluded && html`<span class="tag excl">excluded</span>`}
          ${c.deleted && html`<span class="tag excl">deleted</span>`}
        </div>
      </div>
      ${/* Exactly ONE filled button on the whole record (DESIGN_DIRECTION §6: actions
            are ink). Draft message spends it, so the next-action card, the note
            composer and every chip composer are bordered or quiet. */ ""}
      <div class="pd-acts">
        ${showStep && html`
          <${Stepper} index=${stepIdx + 1} total=${listIds.length}
            onPrev=${() => go(`people/${listIds[stepIdx - 1]}`)}
            onNext=${() => go(`people/${listIds[stepIdx + 1]}`)} />`}
        <${ChannelActions} c=${c} />
        <button class="btn primary" onClick=${() => setDraft(true)}><${Icon} name="pencil" /> Draft message</button>
        <${AddToList} contactIds=${[c.id]} trigger=${html`<${Icon} name="plus" /> List`} />
        <${HeadMenu} c=${c} reload=${reload} go=${go} openMerge=${openMerge} />
      </div>
      <${VitalStrip} c=${c} showWhy=${showWhy} setShowWhy=${setShowWhy} />
      ${draft && html`<${DraftModal} person=${c} onClose=${() => setDraft(false)} />`}
    </div>`;
}

function HeadMenu({ c, reload, go, openMerge }) {
  const [open, setOpen] = useState(false);
  const [confirmPurge, setConfirmPurge] = useState(false);
  const [merging, setMerging] = useState(false);
  // ⌘K → "Merge <name> with…" arrives as people/<id>?merge=1 and skips straight
  // to the modal, with this person pinned as the record that survives.
  useEffect(() => { if (openMerge) setMerging(true); }, [openMerge]);
  // Every row here closes the popover while the row itself holds focus, so activating
  // one destroys the focused element and would drop the user to <body>, back at the top
  // of the document. useDismiss already restores focus to the trigger on Escape;
  // close() applies the same contract to a click. The trigger button is never
  // unmounted, so the ref stays valid across the re-render.
  const trigRef = useRef(null);
  const close = () => { setOpen(false); trigRef.current?.focus(); };
  const setCat = async (cat) => {
    try { await api(`/api/contact/${c.id}/category`, { method: "POST", body: JSON.stringify({ category: cat }) }); reload(); }
    catch (e) { toast(e.message); }
  };
  // NC-150. `warning` is the server saying the app's own half worked and the
  // half the importers read did not — a read-only or full data folder. It is
  // not an error (nothing to retry, and hiding IS in force everywhere in the
  // app), so it replaces the tick rather than throwing. Never say "done" over
  // the top of something that did not happen.
  const setExcl = async (v) => {
    close();
    try {
      const d = await api(`/api/contact/${c.id}/exclude`, { method: "POST", body: JSON.stringify({ excluded: v }) });
      toast(d.warning || (v ? "Excluded — hidden from review and imports" : "Back in your network"));
      reload();
    } catch (e) { toast(e.message); }
  };
  const setDel = async (v) => {
    close();
    try {
      const d = await api(`/api/contact/${c.id}/delete`, { method: "POST", body: JSON.stringify({ deleted: v }) });
      if (v) { toast(d.warning || "Deleted — restore from the Deleted filter"); go("people"); }
      else { toast(d.warning || "Restored"); reload(); }
    } catch (e) { toast(e.message); }
  };
  // Permanent delete — only offered on an already-deleted contact, and only
  // after a second explicit click. Irreversible, so never a one-tap action.
  const purge = async () => {
    close(); setConfirmPurge(false);
    try {
      const d = await api(`/api/contact/${c.id}/purge`, { method: "POST", body: JSON.stringify({}) });
      // NC-150/NC-172: if the skip-list write failed, say that instead of a tick.
      toast(d.warning || `Permanently deleted${d.messages_removed ? ` · ${d.messages_removed.toLocaleString()} messages removed` : ""}`);
      go("people");
    } catch (e) { toast(e.message); }
  };
  const menuRef = useRef(null);
  useDismiss(open, () => setOpen(false), menuRef);
  return html`
    <div class="pd-menu" ref=${menuRef}>
      ${/* The trigger carries an aria-label and aria-expanded: a bare "⋯" is no name,
            and without aria-expanded its open state would be exposed nowhere.

            It deliberately has no aria-haspopup="menu", and the popover has no
            role="menu" with role="menuitem" rows. role="menu" owns only menuitem /
            menuitemradio / menuitemcheckbox / group / separator children, so the three
            <div class="omni-head"> group labels would be DROPPED for assistive tech —
            and "Danger" is what tells a listener that the "Delete" they just heard is
            the reversible one and "Delete permanently…" is not. Declaring role="menu"
            also promises arrow keys, Home/End and roving focus, none of which this
            component implements. What it really is, is a disclosure holding a group of
            buttons: aria-expanded alone states that honestly, the headings stay in the
            reading order, and the promise matches the code. */ ""}
      <button class="btn quiet" title="More actions" aria-label="More actions"
              ref=${trigRef} aria-expanded=${open}
              onClick=${() => setOpen(o => !o)}><${Icon} name="ellipsis" /></button>
      ${merging && html`
        <${MergeModal} fixed=${c} onClose=${() => setMerging(false)}
                       onMerged=${keptId => { if (keptId === c.id) reload(); else go(`people/${keptId}`); }} />`}
      ${open && html`
        <div class="omni-pop pd-menu-pop">
          <div class="omni-head">Duplicates</div>
          ${/* close(), not setOpen(false): MergeModal contains no .focus() call of its
                own (it is ui.js's, not this file's), so if this row simply vanished the
                keyboard would be left on <body> behind the overlay. */ ""}
          <button class="omni-row" onClick=${() => { close(); setMerging(true); }}>
            <div class="grow">
              <div class="r-name">Merge with…</div>
              <div class="r-meta">fold another record into this one</div>
            </div>
          </button>
          <div class="omni-head">Category</div>
          ${/* The dot was `class="cat-dot"` plus an inline 9px×9px, which made the same
                semantic dot render at three sizes across the app (8px base, 6px in a
                table row, 9px here) and bypassed tokens with literal px. CatDot in
                ui.js already owns this exact colour mapping. (The table-row dot has
                since been removed outright — this picker is CatDot's only caller now,
                so there is one size left, not three.)

                Selection carries three ways — --accent-ink, a ✓, and aria-pressed —
                because colour alone is a weak affordance, which is the argument ui.css
                makes for links. It was aria-current, which reads as "current item in a
                set of links or steps" rather than as a chosen value; aria-pressed is
                the state a <button> may legitimately hold, and it is announced in every
                screen reader as pressed / not pressed. The ✓ is aria-hidden, so the
                state is stated once rather than twice. */ ""}
          ${CATS.map(cat => html`
            <button key=${cat} class="omni-row" aria-pressed=${c.category === cat}
                    onClick=${() => { setCat(cat); close(); }}>
              <${CatDot} category=${cat} />
              <div class="grow"><div class=${"r-name" + (c.category === cat ? " on" : "")}>${CAT_LABEL[cat]}</div></div>
              ${c.category === cat && html`<span class="r-kind"><${Icon} name="check" /></span>`}
            </button>`)}
          <div class="omni-head">Danger</div>
          ${/* Four identical inline `color:var(--red)` copies became one .omni-row.danger
                rule in ui.css, on --danger (the same value under the semantic name),
                which also brings the hover fill .btn.danger already uses. */ ""}
          ${c.deleted
            ? html`
              <button class="omni-row" onClick=${() => setDel(false)}><div class="grow"><div class="r-name">Restore</div><div class="r-meta">bring this deleted contact back</div></div></button>
              ${confirmPurge
                ? html`<button class="omni-row danger" onClick=${purge}><div class="grow"><div class="r-name">Yes — erase permanently</div><div class="r-meta">notes, photo and archived messages too</div></div></button>`
                : html`<button class="omni-row danger" onClick=${() => setConfirmPurge(true)}><div class="grow"><div class="r-name">Delete permanently…</div><div class="r-meta">cannot be undone</div></div></button>`}`
            : html`
              ${c.excluded
                ? html`<button class="omni-row" onClick=${() => setExcl(false)}><div class="grow"><div class="r-name">Unexclude</div></div></button>`
                : html`<button class="omni-row danger" onClick=${() => setExcl(true)}><div class="grow"><div class="r-name">Exclude</div><div class="r-meta">hide everywhere</div></div></button>`}
              <button class="omni-row danger" onClick=${() => setDel(true)}><div class="grow"><div class="r-name">Delete</div><div class="r-meta">reversible — restore from the Deleted filter</div></div></button>`}
          <button class="omni-row" onClick=${async () => { close(); try { await api(`/api/contact/${c.id}/photo`, { method: "DELETE" }); toast("Photo removed"); reload(); } catch (e) { toast(e.message); } }}>
            <div class="grow"><div class="r-name">Remove photo</div></div>
          </button>
        </div>`}
    </div>`;
}

/** Derived next-action card — sits above the story. One kind
 * in v1: going-cold reconnect. "Not now" snoozes via the reconnect-skip
 * endpoint and hides the card locally. */
function NextCard({ c }) {
  const [snoozed, setSnoozed] = useState(false);
  const [draft, setDraft] = useState(false);
  const na = c.next_action;
  // "Not now" must not simply unmount the card: that destroys the button the user just
  // pressed and leaves the keyboard on <body>, and a toast alone says nothing to a
  // screen reader because toast() writes to no live region. The card is replaced by one
  // line that states the outcome where the card was, and that line takes the focus.
  const doneRef = useRef(null);
  useEffect(() => { if (snoozed) doneRef.current?.focus(); }, [snoozed]);
  const skip = async () => {
    try {
      await api(`/api/contact/${c.id}/reconnectskip`, { method: "POST", body: JSON.stringify({}) });
      setSnoozed(true);
      toast("Snoozed — off the list for a while");
    } catch (e) { toast(e.message); }
  };
  if (!na) return null;
  if (snoozed) return html`
    <div class="pd-next-done" tabindex="-1" ref=${doneRef}>Snoozed. This person is off the list for a while.</div>`;
  // Microcopy per DESIGN_DIRECTION §3 (ASD-STE100): one instruction per sentence, no
  // dropped words. The fact leads; the evidence (quotation and channel) is a second,
  // quieter line, not one line chaining a count, an em-dash, a quotation and a
  // parenthetical channel.
  const bits = [];
  if (na.last_snippet) bits.push(`“${na.last_snippet}”`);
  if (na.last_channel) bits.push(CHANNEL_LABEL[na.last_channel] || na.last_channel);
  return html`
    <div class="next-card">
      <span class="next-tag">Going cold</span>
      <div class="next-line">
        Last exchange ${na.days_silent} day${na.days_silent === 1 ? "" : "s"} ago.
      </div>
      ${bits.length > 0 && html`<div class="pd-next-note">${bits.join(" · ")}</div>`}
      ${/* Bordered, not filled: the record already spent its one filled button on
            Draft message in the action cluster above. */ ""}
      <div class="next-actions">
        <button class="btn mini" onClick=${() => setDraft(true)}>Draft a message</button>
        <button class="btn quiet mini" onClick=${skip}>Not now</button>
      </div>
      ${draft && html`<${DraftModal} person=${c} onClose=${() => setDraft(false)} />`}
    </div>`;
}

/** Birthdays, anniversaries, meetings and custom dates. Same chip+composer
 * grammar as IdentifiersEditor. Recurrence is resolved server-side — this
 * component never does calendar math. Manual entry only: no calendar access.
 *
 * Now one row of the Details block rather than a strip floating between the header and
 * the story. Nothing here is acted on from this page — a birthday becomes a Today
 * nudge — so it has not earned a position above the timeline. Same chip grammar as the
 * identifiers, so it belongs in the same block. */
function DatesEditor({ c, reload }) {
  const [adding, setAdding] = useState(false);
  const [kind, setKind] = useState("birthday");
  const [on, setOn] = useState("");
  const [label, setLabel] = useState("");
  const [noYear, setNoYear] = useState(false);
  const dates = c.dates || [];
  // Same focus contract as IdentifiersEditor: the trigger and the composer replace one
  // another, so whichever one arrives takes the focus the other one took away. This
  // composer never had autofocus at all, so its caret landed nowhere.
  const trigRef = useRef(null);
  const fieldRef = useRef(null);
  const back = useRef(false);
  useEffect(() => {
    if (adding) fieldRef.current?.focus();
    else if (back.current) { back.current = false; trigRef.current?.focus(); }
  }, [adding]);
  const stop = () => { back.current = true; setAdding(false); };
  const post = (b) => api(`/api/contact/${c.id}/dates`, { method: "POST", body: JSON.stringify(b) });
  const add = async () => {
    if (!on) return;
    try {
      await post({ kind, on_date: on, label: label.trim(), year_known: !noYear });
      setOn(""); setLabel(""); setNoYear(false); stop(); reload();
    } catch (e) { toast(e.message); }
  };
  const remove = async (d) => {
    try { await post({ remove: true, date_id: d.id }); reload(); }
    catch (e) { toast(e.message); }
  };
  const dated = kind === "birthday" || kind === "anniversary";
  // Returns a flat run of chips so it can sit inside one .pd-props value cell, the
  // same shape IdentifiersEditor returns.
  return [
    ...dates.map(d => html`
      <span class="tag" key=${d.id} title=${d.on_date + (d.recurring ? " · every year" : "")}>
        <${Icon} name=${DATE_ICON[d.kind] || "star"} /> ${d.reason}
        <button class="tag-x" aria-label=${`Remove ${d.reason}`} onClick=${() => remove(d)}><${Icon} name="close" /></button>
      </span>`),
    adding
      ? html`
        <span key="add" class="pd-compose">
          <select class="input mini" aria-label="Date type" value=${kind}
                  onChange=${e => setKind(e.target.value)}>
            <option value="birthday">birthday</option>
            <option value="anniversary">anniversary</option>
            <option value="meeting">meeting</option>
            <option value="custom">date</option>
          </select>
          ${/* The date field and the selects carry aria-labels: a placeholder is ignored
                on a date input, so without one it would announce as an unlabeled date
                field. */ ""}
          <input class="input mini" type="date" aria-label="Date" ref=${fieldRef} value=${on}
            onInput=${e => setOn(e.target.value)}
            onKeyDown=${e => { if (e.key === "Enter") add(); if (e.key === "Escape") stop(); }} />
          ${kind !== "birthday" && html`
            <input class="input mini" aria-label=${kind === "meeting" ? "Where or what" : "Label"}
              placeholder=${kind === "meeting" ? "where / what" : "label"} value=${label}
              onInput=${e => setLabel(e.target.value)}
              onKeyDown=${e => { if (e.key === "Enter") add(); if (e.key === "Escape") stop(); }} />`}
          ${dated && html`
            <label class="pd-check">
              <input type="checkbox" checked=${noYear} onChange=${e => setNoYear(e.target.checked)} /> year unknown
            </label>`}
          <button class="btn mini" disabled=${!on} onClick=${add}>Add</button>
        </span>`
      : html`<button key="addbtn" class="tag" ref=${trigRef} onClick=${() => setAdding(true)}><${Icon} name="plus" /> birthday / meeting</button>`,
  ];
}

/** Mutual connections + shared history re-parsed from the LinkedIn profile.
 * LLM-parsed → labelled unverified. Hidden when there's nothing.
 *
 * Relationship intelligence, in its honest place: below what you know for
 * certain, because this is unverified LLM output. It is also the one block on the page
 * that may vanish entirely — absence here is not neglect. */
function Mutuals({ c }) {
  const m = c.mutuals || {};
  const names = m.mutual_names || [], hist = m.shared_history || [], act = m.recent_activity || "";
  if (!names.length && !hist.length && !act) return null;
  return html`
    <div class="pd-mutuals">
      ${/* The provenance caption is a sibling of .section-label, never a <span> INSIDE
            it: there it would inherit uppercase + letterspacing and render "· FROM
            LINKEDIN (UNVERIFIED)". It uses the one provenance pill every other call
            site uses. */ ""}
      <div class="pd-cap">
        <span class="section-label">Mutual connections</span>
        <span class="pd-src">LinkedIn · unverified</span>
      </div>
      ${names.length > 0 && html`<div class="pd-mutual-names">${names.join(", ")}</div>`}
      ${hist.length > 0 && html`
        <ul class="pd-mutual-hist">${hist.map((h, i) => html`<li key=${i}>${h}</li>`)}</ul>`}
      ${act && html`<div class="pd-mutual-act">Lately: ${act}</div>`}
    </div>`;
}

/** The LLM-written paragraph, and nothing else.
 *
 *  Everything from "How you met" downward — how_we_met, home_city, home_metro,
 *  home_country, role_type, blurb — moved to Details. Six field editors inside a
 *  narrative block made this a form wearing a story's costume, and it made the LLM
 *  paragraph read as one more field to fill in. The label is one noun, matching
 *  TIMELINE / DETAILS and the references; "The story so far" was the last of the
 *  editorial-magazine register DESIGN_DIRECTION §4 is leaving behind. */
function Story({ c, reload }) {
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try {
      await api(`/api/contact/${c.id}/brief`, { method: "POST", body: JSON.stringify({}) });
      toast("Story refreshed");
      reload();
    } catch (e) { toast(e.message.includes("ANTHROPIC") || e.message.includes("Anthropic") ? "AI stories need an Anthropic key. Add one in Settings." : e.message); }
    setBusy(false);
  };
  return html`
    <div class="story">
      <div class="story-label">
        <span class="section-label">Story</span>
        <span class="pd-src">${c.story_source === "ai" ? "AI written" : "From your data"}</span>
        <button class="btn quiet mini refresh" disabled=${busy} onClick=${refresh} title="Rewrite with AI from the latest data">${busy ? "writing…" : html`<${Icon} name="refresh" /> ${c.brief_meta?.newer_messages > 0 ? "rebuild" : "rewrite"}`}</button>
      </div>
      <div class="story-body">
        ${/* Was three instructions in one sentence. STE wants one per sentence. */ ""}
        ${c.story
          ? html`<${Md} text=${c.story} />`
          : html`<span class="nx-none">No story yet. Import your conversations in Data. Or write how you met in Details.</span>`}
      </div>
      <${Receipt} meta=${c.brief_meta} />
    </div>`;
}

/** One read-mode property row: a key, and a value you click to edit.
 *
 *  Properties render as key/value rows, with editing as a separate affordance. An
 *  empty value shows the field's own NAME as ghost text rather than a dash or a
 *  collapsed row — on this app, where most people have no
 *  history at all, neglect visibility IS the product.
 *
 *  Returns the <dt>/<dd> pair as a flat array, so .pd-props stays one grid and the key
 *  is programmatically the key OF that value rather than a div that happens to sit
 *  beside it.
 *
 *  `onSave` must REJECT on failure. That is the whole contract that keeps a typed value
 *  from being thrown away — see commit(). */
function PropRow({ label, value, ghost, onSave, rows = 0 }) {
  const [editing, setEditing] = useState(false);
  const cur = value || "";
  const [v, setV] = useState(cur);
  // Follow the server whenever this row is not being edited. (NoteEntry has the
  // mirror-image job: it must reset its draft on cancel, or a discarded draft comes
  // back.)
  useEffect(() => { if (!editing) setV(cur); }, [cur, editing]);
  // Removing a focused element does not reliably fire blur, and when it does the
  // handler still sees the pre-render value — so Escape has to say "do not save"
  // out of band rather than rely on the ordering.
  const cancelled = useRef(false);
  const inflight = useRef(false);
  // An explicit focus, not the `autofocus` attribute: a document stops honouring
  // autofocus once it has loaded, so on an element Preact inserts later it is silently
  // inert and click-to-edit would need a second click. The two chip composers below
  // share this pattern.
  //
  // Focus also has to come back out: Enter and Escape replace the input with a
  // brand-new read-mode button, so without a hand-back the most common action in the
  // pane would end with the keyboard on <body>, at the top of the document. `back` is
  // set only on the keyboard exits: a blur means the user has already chosen where
  // focus goes.
  const fieldRef = useRef(null);
  const btnRef = useRef(null);
  const back = useRef(false);
  useEffect(() => {
    if (editing) fieldRef.current?.focus();
    else if (back.current) { back.current = false; btnRef.current?.focus(); }
  }, [editing]);
  const start = () => { cancelled.current = false; setV(cur); setEditing(true); };
  // Await the save, leave edit mode only on success, and on failure stay in edit mode
  // with the draft intact. Leaving edit mode before the request resolves would let the
  // resync effect above reset the draft to `cur` — the unchanged server value — so a
  // failed save would discard what the user typed and leave only a toast. This covers
  // all six fields on this path (How you met, City, Metro, Country, Role type, Blurb).
  // Awaiting also avoids a flicker on the happy path: the value does not drop back to
  // its ghost placeholder for the duration of the POST and the reload that follows it.
  const commit = async () => {
    if (cancelled.current) { cancelled.current = false; return; }
    const next = v.trim();
    if (next === cur) { setEditing(false); return; }
    if (inflight.current) return;          // Enter, then a blur from the same exit
    inflight.current = true;
    try { await onSave(next); setEditing(false); }
    catch { back.current = false; }        // keep the draft, and the caret, where it is
    inflight.current = false;
  };
  const keys = (e) => {
    if (e.key === "Escape") { cancelled.current = true; back.current = true; setV(cur); setEditing(false); }
    if (e.key === "Enter" && !rows) { e.preventDefault(); back.current = true; commit(); }
  };
  return [
    html`<dt class="pd-k" key="k">${label}</dt>`,
    html`<dd class="pd-v" key="v">
      ${editing
        ? (rows
          ? html`<textarea class="input" ref=${fieldRef} rows=${rows} aria-label=${label} value=${v}
                    onInput=${e => setV(e.target.value)} onKeyDown=${keys} onBlur=${commit} />`
          : html`<input class="input" ref=${fieldRef} aria-label=${label} value=${v}
                    onInput=${e => setV(e.target.value)} onKeyDown=${keys} onBlur=${commit} />`)
        : html`${/* The read-mode button has an aria-label naming the field: its content
                     is the VALUE alone, so a filled City row would announce as
                     "Paris, button" and never say which field it edits, because the
                     field name would reach assistive tech only through a `title`, and
                     per accname contents beat title. The edit-mode branch above has its
                     aria-label too, so both halves of one control agree. */ ""}
          <button class="pd-v-btn" ref=${btnRef} title=${"Edit " + label.toLowerCase()}
                  aria-label=${cur ? `${label}: ${cur}` : `${label}: empty`} onClick=${start}>
            ${cur || html`<span class="pd-ghost">${ghost || label}</span>`}
          </button>`}
    </dd>`,
  ];
}

/** Details — the properties block, and the last section on the page.
 *
 *  Absorbs everything evicted from .story plus the identifier and date chips evicted
 *  from the identity band. This is curation, not consumption, so it goes after the
 *  activity feed: properties, sources and groups below the timeline. */
function Details({ c, reload, reloadAll }) {
  // Each save reloads: blurb, home_city, home_metro and role_type are rendered again in
  // the header and the rail, so without a reload one screen would show two values for
  // one field.
  //
  // Both handlers RE-THROW after toasting. PropRow needs the rejection to know it must
  // stay in edit mode and keep the draft; swallowing it here would silently discard the
  // typed value. They also await the reload, so `value` is fresh before the row returns
  // to read mode.
  const locSave = (field) => async (v) => {
    try { await saveField(c.id, field, v.trim()); toast("Saved"); await reloadAll(); }
    catch (e) { toast(e.message); throw e; }
  };
  // how_we_met is its own editable field.
  const saveHowWeMet = async (v) => {
    try {
      await api(`/api/contact/${c.id}/howwemet`, { method: "POST", body: JSON.stringify({ text: v.trim() }) });
      toast("Saved"); await reload();
    } catch (e) { toast(e.message); throw e; }
  };
  const topics = (c.topics || []).filter(Boolean);
  return html`
    <div class="pd-details">
      <div class="section-label">Details</div>
      ${/* A description list, so the markup associates each key with its value instead
            of leaving the visual pairing as the only one. dt and dd are the two grid
            items, so the grid costs nothing and .pd-props keeps every rule it has. */ ""}
      <dl class="pd-props">
        <${PropRow} label="How you met" rows=${2} value=${c.how_we_met}
          ghost=${`Where you met ${c.name.split(" ")[0]}`} onSave=${saveHowWeMet} />
        ${/* Neutral .tag pills, per DESIGN_DIRECTION §6 — no tint, no per-topic hue. */ ""}
        <dt class="pd-k">Topics</dt>
        <dd class="pd-v pd-chips">
          ${topics.length
            ? topics.map(t => html`<span class="tag" key=${t}>${t}</span>`)
            : html`<span class="pd-ghost">Topics</span>`}
        </dd>
        <${PropRow} label="City" value=${c.home_city} onSave=${locSave("home_city")} />
        <${PropRow} label="Metro" value=${c.home_metro} onSave=${locSave("home_metro")} />
        <${PropRow} label="Country" value=${c.home_country} onSave=${locSave("home_country")} />
        <${PropRow} label="Role type" value=${c.role_type} onSave=${locSave("role_type")} />
        <${PropRow} label="Blurb" rows=${2} value=${c.blurb} onSave=${locSave("blurb")} />
        <dt class="pd-k">Phone and email</dt>
        <dd class="pd-v pd-chips"><${IdentifiersEditor} c=${c} reload=${reloadAll} /></dd>
        <dt class="pd-k">Dates</dt>
        <dd class="pd-v pd-chips"><${DatesEditor} c=${c} reload=${reload} /></dd>
        <dt class="pd-k">LinkedIn</dt>
        <dd class="pd-v">
          ${/* Ink plus an underline, never --accent (§6). */ ""}
          ${c.linkedin_url
            ? html`<a href=${c.linkedin_url} target="_blank" rel="noopener">${c.linkedin_url.replace(/^https?:\/\/(www\.)?/, "")}</a>`
            : html`<span class="pd-ghost">LinkedIn</span>`}
        </dd>
      </dl>
    </div>`;
}

/* ── Timeline: notes + episodes merged ──────────────────────────────────────── */
/** One archived message bubble. Long bodies clamp with a per-message
 * "Show more" toggle. */
function Msg({ m, who }) {
  const [expanded, setExpanded] = useState(false);
  const body = m.body || "(media)";
  const long = body.length > 280;
  // Who said it is carried as text, not by the "out"/"in" class alone (an alignment and
  // a fill): read linearly, a sent and a received message would be indistinguishable,
  // which is the one fact a transcript has to state. The name joins the timestamp line,
  // so direction becomes text at no cost in height.
  return html`
    <div class=${"msg " + (m.direction ? "out" : "in")}>
      ${m.subject ? html`<b>${m.subject}</b><br/>` : ""}<span class=${"m-body" + (long && !expanded ? " clamp" : "")}>${body}</span>${long && html`<button class="m-more" onClick=${() => setExpanded(x => !x)}>${expanded ? "Show less" : "Show more"}</button>`}
      <span class="m-when"><span class="m-who">${m.direction ? "You" : (m.who || who)}</span> ${m.ts?.slice(0, 16)}</span>
    </div>`;
}

// The server's own ceiling on one history request (api_messages.py clamps `limit` to
// 200). Named here because two things read it: the request, and the truncation line
// that has to admit when the expanded list is shorter than the label above it.
const HISTORY_CAP = 200;

/* NC-106. Show group threads on everyone's page.
 *
 * THE WIDENING IS ON THE EXPANSION, NOT ON THE LIST, and that is the whole
 * design decision. The episode list is still built from this person's own
 * messages, because widening it would let one busy group chat fill the 600
 * messages the server scans and push their one-to-one history off the page
 * entirely — the timeline is supposed to be a record of knowing THEM. Open a
 * group episode and the request asks for `threads=1`, and the whole exchange
 * arrives with every line attributed.
 *
 * So `ep.count` counts their lines and the expansion can legitimately hold
 * more, which is why the header says whose count it is and the truncation line
 * below trusts the server's has_more rather than comparing the two. */
function EpisodeEntry({ c, ep }) {
  // A GROUP EPISODE NEVER STARTS FROM THE INLINE PAYLOAD. The server sends
  // messages inline for episodes of six or fewer
  // (api_messages.EPISODE_INLINE_MSGS), those are this person's lines only, and
  // `toggle` returns early when it already has messages — so every small group
  // episode would open showing one voice while the widening worked on the large
  // ones. Most group episodes are small. Starting them at null means opening
  // always asks, and the early return still stops a second fetch once it has.
  const [msgs, setMsgs] = useState(ep.group ? null : (ep.messages || null));
  const [more, setMore] = useState(false);
  const [open, setOpen] = useState(false);
  const toggle = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (msgs) return;
    try {
      // A group episode asks for the CONVERSATION'S window, not its own. Its
      // own bounds are the first and last thing this person said, so every
      // other voice in the thread sits outside them — the server computes the
      // wider window and hands it over as thread_since/thread_before so the
      // gap that defines it has one definition.
      const wide = !!(ep.group && ep.thread_since && ep.thread_before);
      const params = new URLSearchParams({
        channel: ep.channel,
        before: (wide ? ep.thread_before : ep.ts_end) + "z",
        limit: String(wide ? HISTORY_CAP : Math.min(ep.count, HISTORY_CAP)),
      });
      if (wide) { params.set("threads", "1"); params.set("since", ep.thread_since); }
      const d = await api(`/api/contact/${c.id}/history?` + params);
      // The server already bounded a widened request from both ends; only the
      // narrow path still has to trim to the episode.
      const list = wide ? (d.messages || [])
                        : (d.messages || []).filter(m => m.ts >= ep.ts_start);
      setMsgs(list.reverse());
      setMore(!!d.has_more);
    } catch { setMsgs([]); }
  };
  // Lines from someone other than this person and other than you. Counted from
  // what actually arrived, so the sentence cannot claim voices the list lacks.
  const others = (msgs || []).filter(m => m.who && m.who !== c.name).length;
  const day = fmtDay(ep.ts_end);
  return html`
    <div class="tl-entry">
      <div class="tl-meta">
        <span class="ch"><${Icon} name=${CHANNEL_ICON[ep.channel]} /> ${CHANNEL_LABEL[ep.channel] || ep.channel}</span>
        <span class="microlabel">${day}</span>
      </div>
      ${/* aria-expanded carries the state of this disclosure, like the ⋯ trigger in this
            file; without it the only carrier would be a ▸/▾ glyph, which is aria-hidden
            because aria-expanded says the same thing. */ ""}
      <button class="tl-episode" aria-expanded=${open} onClick=${toggle}>
        <div class="ep-line">
          <span class="ep-count">${ep.count}${" "}
            ${ep.count === 1 ? "message" : "messages"}${ep.group ? ` from ${c.name.split(" ")[0]}` : ""}</span>
          <span class="ep-snippet">${ep.snippet}</span>
          <span class="ep-open"><${Icon} name=${open ? "chevronDown" : "caretRight"} /></span>
        </div>
      </button>
      ${open && html`
        <div class="ep-msgs">
          ${msgs === null && html`<${SkelRows} n=${2} h="calc(var(--row-h) * .75)" />`}
          ${msgs !== null && ep.group && others > 0 && html`
            <div class="ep-thread-note">The whole conversation —${" "}
              ${c.name.split(" ")[0]}'s ${ep.count}${" "}
              ${ep.count === 1 ? "message" : "messages"} and ${others}${" "}
              ${others === 1 ? "line" : "lines"} from everyone else in${" "}
              ${ep.group}.</div>`}
          ${(msgs || []).map((m, i) => html`<${Msg} key=${i} m=${m} who=${c.name.split(" ")[0]} />`)}
          ${/* The server clamps history to 200 rows and reports has_more, which this
                client must not discard. An episode can hold up to 600 messages
                (api_messages.EPISODE_SCAN_MSGS), so a long episode renders 200 bubbles
                under a label giving a larger count; without a truncation marker that
                is one episode with two numbers. Loading the rest needs a paging change,
                so the shortfall is at least STATED.

                The comparison is has_more, not `msgs.length < ep.count` (NC-106):
                `ep.count` counts this person's own lines while a widened group episode
                renders everybody's, so msgs.length can exceed it and that comparison
                goes quietly false on exactly the episodes most likely to be truncated.
                The server's has_more is the only figure that means "there is more than
                this", which is the claim being made. */ ""}
          ${msgs !== null && more && html`
            <div class="tl-trunc">The last ${msgs.length} messages of a longer conversation.</div>`}
        </div>`}
    </div>`;
}

function NoteEntry({ c, note, reload }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(note.body);
  const save = async () => {
    try {
      await api(`/api/note/${note.id}`, { method: "PUT", body: JSON.stringify({ body: text.trim() }) });
      setEditing(false); reload();
    } catch (e) { toast(e.message); }
  };
  const del = async () => {
    try { await api(`/api/note/${note.id}`, { method: "DELETE" }); reload(); }
    catch (e) { toast(e.message); }
  };
  return html`
    <div class="tl-entry note">
      <div class="tl-meta">
        <span class="ch"><${Icon} name="pencil" /> ${note.kind === "how_we_met" ? "how you met" : note.kind === "voice" ? "voice note" : "note"}</span>
        <span class="microlabel">${fmtDay(note.created_at)}</span>
        <span class="acts">
          ${/* Cancel resets the draft: `text` is initialised once from note.body while
                the entry keeps its identity across a reload, so an abandoned edit would
                otherwise come back on the next open and be written by the next Save. */ ""}
          <button class="btn quiet mini"
                  onClick=${() => { if (editing) setText(note.body); setEditing(e => !e); }}>${editing ? "cancel" : "edit"}</button>
          ${/* .btn.danger already exists, on --danger, and brings a hover state. */ ""}
          <button class="btn quiet mini danger" onClick=${del}>delete</button>
        </span>
      </div>
      ${editing
        ? html`<div class="tl-note-edit">
            <textarea class="input" rows="2" aria-label="Note text" value=${text}
                      onInput=${e => setText(e.target.value)} />
            <button class="btn mini" onClick=${save}>Save</button>
          </div>`
        : html`<div class="tl-note-body">${note.body}</div>`}
    </div>`;
}

/** A system event — one quiet gray line with a source caption. Derived from
 * existing signals (relationship milestone, logged meetings/dates), never a
 * new schema. The third timeline voice: not a conversation, not your note. */
function EventEntry({ ev }) {
  return html`
    <div class="tl-entry event">
      <div class="tl-event-line">
        <span class="ev-glyph"><${Icon} name=${ev.icon} /></span>
        <span class="ev-label">${ev.label}</span>
        ${ev.source && html`<span class="ev-src">${ev.source}</span>`}
        <span class="microlabel ev-when">${fmtDay(ev.date)}</span>
      </div>
    </div>`;
}

/** Derive timeline events from the contact's existing fields — the milestone
 * you connected, and any one-off dates you logged (meetings, marked days).
 * Recurring birthdays/anniversaries stay chips + future reminders, not past
 * history. */
function deriveEvents(c) {
  const out = [];
  if (c.connected_on)
    out.push({ icon: "link", label: "Connected on LinkedIn", source: "LinkedIn", date: c.connected_on });
  for (const d of (c.dates || [])) {
    if (d.recurring || d.kind === "birthday" || d.kind === "anniversary" || !d.on_date) continue;
    const label = d.label || (d.kind === "meeting" ? "Met in person" : "Marked date");
    // source was hardcoded "added" for every date, so a meeting read off a
    // calendar invite was captioned exactly like one typed by hand — an inferred
    // guess wearing the authority of a fact. api_dates now sends the real origin,
    // and rows predating that column default to manual, which is what they are.
    out.push({ icon: DATE_ICON[d.kind] || "star", label, date: d.on_date,
               source: d.source === "calendar" ? "Calendar" : "added" });
  }
  return out;
}

function Timeline({ c, reload, focusNote }) {
  const [eps, setEps] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [nextBefore, setNextBefore] = useState(null);
  const [noteText, setNoteText] = useState("");
  const noteRef = useRef(null);

  // ⌘K → "Add a note about <name>" lands on people/<id>?note=1. The composer is
  // below the fold on anyone with history, so scroll to it as well as focus it.
  // Writes nothing itself — addNote bails on an empty body.
  useEffect(() => {
    if (!focusNote) return;
    noteRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    noteRef.current?.focus();
  }, [focusNote]);

  const loadEps = useCallback(async (before) => {
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (before) params.set("before", before);
      const d = await api(`/api/contact/${c.id}/episodes?` + params);
      setEps(prev => before ? [...(prev || []), ...(d.episodes || [])] : (d.episodes || []));
      setHasMore(!!d.has_more && (d.episodes || []).length > 0);
      setNextBefore(d.next_before);
    } catch { setEps([]); }
  }, [c.id]);

  useEffect(() => { setEps(null); loadEps(); }, [c.id, loadEps]);

  const addNote = async () => {
    const body = noteText.trim();
    if (!body) return;
    try {
      await api(`/api/contact/${c.id}/note`, { method: "POST", body: JSON.stringify({ body }) });
      setNoteText("");
      toast("Noted");
      reload();
    } catch (e) { toast(e.message); }
  };

  // merge the three timeline voices — notes, conversation episodes, and
  // derived system events — newest first. Each renders in its own grammar.
  //
  // Notes and episodes carry 'YYYY-MM-DD HH:MM:SS' while derived events carry
  // connected_on / on_date, which are date-only. Sliced as they are, the key would be 10
  // characters against 19, and a lexicographic descending compare would push the shorter
  // string last, rendering "Connected on LinkedIn" UNDER that day's first conversation.
  // A date-only stamp means "sometime that day", so it is padded to the end of its day,
  // which puts a same-day milestone at the head of the day.
  const tsKey = (s) => {
    const v = String(s || "").slice(0, 19);
    return v.length <= 10 ? v + " 23:59:59" : v;
  };
  const entries = [
    ...(c.notes || []).map(n => ({ kind: "note", ts: tsKey(n.created_at), n })),
    ...(eps || []).map(ep => ({ kind: "ep", ts: tsKey(ep.ts_end), ep })),
    ...deriveEvents(c).map(ev => ({ kind: "event", ts: tsKey(ev.date), ev })),
  ].sort((a, b) => (a.ts < b.ts ? 1 : -1));

  return html`
    <div class="tl">
      <div class="section-label">Timeline</div>
      ${/* Bordered, not filled: the identity band's Draft message is the record's one
            filled button. Before, this competed with it and with the next-action card. */ ""}
      <div class="tl-compose">
        <input class="input" ref=${noteRef} aria-label="New note"
          placeholder=${`Add a note about ${c.name.split(" ")[0]}… (Enter to save)`}
          value=${noteText} onInput=${e => setNoteText(e.target.value)}
          onKeyDown=${e => { if (e.key === "Enter") addNote(); }} />
        <button class="btn" disabled=${!noteText.trim()} onClick=${addNote}>Note</button>
      </div>
      <div class="tl-list">
        ${eps === null && html`<${SkelRows} n=${4} h="calc(var(--row-h) * .85)" />`}
        ${entries.map((e, i) => e.kind === "note"
          ? html`<${NoteEntry} key=${"n" + e.n.id} c=${c} note=${e.n} reload=${reload} />`
          : e.kind === "event"
          ? html`<${EventEntry} key=${"v" + i + e.ts} ev=${e.ev} />`
          : html`<${EpisodeEntry} key=${"e" + i + e.ep.ts_end} c=${c} ep=${e.ep} />`)}
        ${eps !== null && entries.length === 0 && html`
          <${Empty}>No history yet. Import WhatsApp, iMessage or email in <b>Data</b> and this becomes a story.</${Empty}>`}
      </div>
      ${hasMore && html`<button class="btn tl-more" onClick=${() => loadEps(nextBefore)}>Earlier…</button>`}
    </div>`;
}

function PersonPage({ id, reloadList, go, listIds, openMerge, openNote, openDraft }) {
  const [c, reload, err] = useContact(id);
  if (err) return html`<div class="person"><div class="person-empty"><div class="big">Could not load</div>${err}</div></div>`;
  if (!c) return html`<div class="person"><${SkelRows} n=${5} h="calc(var(--row-h) * 1.5)" /></div>`;
  // Returns the reload promise, so `await reloadAll()` in a save handler actually waits
  // for the fresh record. Without it PropRow returned to read mode holding the value the
  // server had before the write, and flashed the stale one until the GET landed.
  const reloadAll = () => { reloadList?.(); return reload(); };
  // The reading path, in the order a user asks: who → act → what happened → correct.
  // .pd-main is the primary column; .pd-side is curation, which gets its own column on
  // a wide window and simply follows below on a narrow one. Dates and Mutuals both
  // moved DOWN into it: neither is something you act on from this page.
  return html`
    <div class="person pd-rec">
      <div class="pd-grid">
        <div class="pd-main">
          <${Header} c=${c} reload=${reloadAll} listIds=${listIds} go=${go} openMerge=${openMerge}
                   openDraft=${openDraft} />
          <${NextCard} key=${"na" + c.id} c=${c} />
          <${Story} c=${c} reload=${reload} />
          <${Timeline} c=${c} reload=${reload} focusNote=${openNote} />
        </div>
        <div class="pd-side">
          <${Details} c=${c} reload=${reload} reloadAll=${reloadAll} />
          <${Mutuals} c=${c} />
        </div>
      </div>
    </div>`;
}

export function People({ id, go, params }) {
  const [listIds, setListIds] = useState([]);
  const onVisible = useCallback(ids => setListIds(ids), []);
  // Bumped when the open person is edited, which makes the table refetch. Held
  // here because People is the only component that sees both halves.
  const [listNonce, setListNonce] = useState(0);
  const reloadList = useCallback(() => setListNonce(n => n + 1), []);
  // What a quick command asked for on the way in (see COMMANDS in main.js).
  const preset = {
    filter: params?.get("filter") || "", sort: params?.get("sort") || "",
    location: params?.get("location") || "", role: params?.get("role") || "",
    topic: params?.get("topic") || "",
  };
  // Two states, one table. With nobody selected the table is full width and shows
  // every column; selecting a person narrows it to a rail (name · last contact,
  // closeness on the avatar ring) and gives the rest of the width to them — the
  // row → drawer progression. There is no "Pick someone"
  // placeholder any more: the table is the answer, so filler above it would only
  // cost rows.
  return html`
    <div class=${"nx-people" + (id ? " split" : "")}>
      <${PeopleTable} selectedId=${id} go=${go} onVisible=${onVisible}
                      preset=${preset} addNow=${oneShot(params, "new")}
                      reloadToken=${listNonce} />
      ${id && html`
        <div class="nx-detail">
          <${PersonPage} key=${id} id=${id} go=${go} listIds=${listIds}
                         reloadList=${reloadList}
                         openMerge=${oneShot(params, "merge")}
                         openNote=${oneShot(params, "note")}
                         openDraft=${oneShot(params, "draft")} />
        </div>`}
    </div>`;
}
