// App shell: hash router, top bar with omnibox (⌘K), theme toggle, status dot.
import { html, render, useState, useEffect, useRef, useCallback } from "../vendor/preact-htm.module.js";
import { api, toast, debounce, roleStr, relDate, needsAccessToken } from "./api.js";
import { Icon, CHANNEL_ICON } from "./icons.js";
import { AV, Avatar, openTarget } from "./ui.js";
import { People } from "./people.js";
import { Ask, INTENTS } from "./ask.js";
import { Lists } from "./lists.js";
import { DataPage } from "./data.js";
import { Intake } from "./intake.js";
import { SettingsPage } from "./settings.js";
import { Onboarding } from "./onboarding.js";
import { dismissBoot } from "./boot.js";

// The Today page was removed (NC-009). People is the landing surface now, which
// is also where the omnibox is most useful.
const TABS = [
  ["people", "People"],
  ["ask", "Ask"],
  ["lists", "Lists"],
  ["data", "Data"],
];

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  const [path, query] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query || "");
  // People, not "", is the fallback — so the bare hash, a stale #​ bookmark and
  // anything unrecognised all land somewhere real. Making it a NAME rather than a
  // redirect keeps one canonical route: the tab highlights, SECTION_LABEL
  // resolves, and go("") still works from the wordmark.
  // "intake" is a full surface with no top tab (static/app/intake.js): the paste
  // → review → save flow for crm/api_intake.py. It is reached from the omnibox
  // and by its own hash, and it is deliberately NOT in TABS — that array fixes
  // ⌘1…⌘n. (The packaged app's old web-page window hardcoded its Go menu against
  // those digits; since NC-330 the app's window is native and this page runs only
  // from source, in a browser.)
  // "data-next" and "settings-next" were the addresses of the rebuilt Data and
  // Settings pages while they sat beside the old ones (NC-088, NC-114). Since the
  // switch-over (NC-236) they are the pages at "data" and "settings", and the
  // old addresses lead there.
  const first = { "data-next": "data", "settings-next": "settings" }[parts[0]] || parts[0];
  const name = ["people", "ask", "lists", "data", "settings", "intake"].includes(first)
    ? first : "people";
  return { name, id: parts[1] ? parseInt(parts[1], 10) : null, params };
}

// Params a quick command uses to say "…and do this once you're there" — open the
// add form, expand the duplicates drawer. They are instructions, not places, so
// they must never be remembered as the tab's spot: clicking People a week later
// should not pop the add form open again.
// "row" opens one row of the rebuilt Data or Settings page from a link on the
// other one (NC-216), and must open it again the second time it is pressed.
const ONE_SHOT_PARAMS = ["new", "merge", "dup", "export", "note", "src", "prefill", "draft",
                         "row"];
const NONCE_PARAM = "_";

// An instruction has to be repeatable, and assigning location.hash the value it
// already holds fires no hashchange at all — so "Add someone new" worked once and
// then never again while you stayed put, and "Export a backup" died the moment you
// collapsed the row it had just opened. Tag instruction navigations so the hash
// always differs and the receiving view always hears about it.
let oneShotSeq = 0;
export function go(hash) {
  const [path, query] = hash.split("?");
  if (query && ONE_SHOT_PARAMS.some(k => new URLSearchParams(query).has(k))) {
    hash = `${path}?${query}&${NONCE_PARAM}=${++oneShotSeq}`;
  }
  location.hash = hash;
}
// Navigation from outside the page goes through this rather than assigning the
// hash itself, so the nonce above covers it too. (The packaged app's old web-page
// window called it from its menu bar until NC-330.)
window.ncGo = go;

// Last-visited hash per tab, so clicking a top-nav tab restores where you were
// (the person you had open, your filters) instead of resetting to bare root.
const TAB_HASH = {};

function tabPlace(hash) {
  const [path, query] = hash.split("?");
  if (!query) return hash;
  const p = new URLSearchParams(query);
  for (const k of [...ONE_SHOT_PARAMS, NONCE_PARAM]) p.delete(k);
  const rest = p.toString();
  return rest ? `${path}?${rest}` : path;
}

// Tabs where the route id is a detail pane covering a list, so pressing the tab
// again should collapse it. NOT "ask": there, bare #ask is the newest chat rather
// than a list, so collapsing would throw you out of the conversation you were
// reading into a different one. Today and Data never carry an id at all.
const COLLAPSIBLE = { people: "Back to the full people table", lists: "Back to all lists" };

/** Where a tab press should land. Away from a tab it restores your place; already
 *  on a collapsible tab it drops the open id, which is the way out of a detail pane.
 *
 *  Restore-your-place alone had no exit. #people/42 is remembered AS the People
 *  tab's place, so pressing People while Ada was open re-navigated to the hash
 *  already in the bar — and assigning location.hash the value it already holds
 *  fires no hashchange, so the press did nothing whatsoever. Same trap on Lists.
 *  A second press now drops the id, matching the platform convention (tap the
 *  active tab → top of that section) and the row toggle in people.js.
 *
 *  Root is the bare hash, not tabPlace(), so it drops any ?filter= a quick command
 *  left there too. That is invisible in both views: their filter, search and sort
 *  state lives in the view, the params are only read as a seed at mount, and
 *  dropping the id does not remount the view (main is keyed by name, and People
 *  renders the same unkeyed PeopleTable either way). */
function tabTarget(hash, hereName) {
  return hash === hereName && COLLAPSIBLE[hash] ? hash : (TAB_HASH[hash] || hash);
}

/* ── Theme ──────────────────────────────────────────────────────────────────── */
function themeNow() { return document.documentElement.dataset.theme || "light"; }
function applyTheme(pref) {
  const dark = pref === "dark"
    || (pref === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  localStorage.setItem("nc_theme", pref);
  // The header's sun/moon icon lives in App, which the palette has no handle
  // on — announce the change instead of threading a setter through.
  window.dispatchEvent(new CustomEvent("nc-theme"));
}
function toggleTheme() { applyTheme(themeNow() === "dark" ? "light" : "dark"); }

// The app ships following macOS (index.html reads nc_theme, default "auto"), and
// until now the first toggle pinned it forever — nothing anywhere wrote "auto"
// back. This is the way home.
function followSystemTheme() {
  applyTheme("auto");
  toast(matchMedia("(prefers-color-scheme: dark)").matches
    ? "Follows your Mac — dark now"
    : "Follows your Mac — light now");
}

/* ── Omnibox: names jump, questions ask, commands run ─────────────────────────
 * The box is three things at once, and the third one used to be invisible: the
 * commands only appeared once your typing happened to match a regex, so unless
 * you already knew they existed you never met them. Now an empty box lists them
 * all, and `>` filters to commands only.
 *
 * Each command is a jump (`hash`) or an action (`run`). `keys` are the words
 * someone would actually reach for, matched as prefixes — "dupe", "tidy" and
 * "clean" all find Find duplicates, so there's no vocabulary to learn. Labels
 * and icons may be functions when they depend on state.
 *
 * `typedOnly` keeps a command out of the bare list but leaves it one word away.
 * An empty box is a menu you read, and a menu is only useful while you can take
 * it in — so the dozen everyday things are visible and the long tail (recovery
 * views, appearance, the generated per-city and per-person entries) waits until
 * you name it. Everything below the divider in this array is tail. */
const COMMANDS = [
  { label: "Add someone new", icon: "plus", hint: "People",
    keys: ["add", "new", "someone", "person", "contact", "create"], hash: "people?new=1" },
  // The entry point for crm/api_intake.py (NC-016). Visible rather than
  // typedOnly: a surface only reachable by guessing a keyword is, in practice,
  // unreachable.
  { label: "Paste a list of people", icon: "penNib", hint: "Intake",
    keys: ["paste", "intake", "bio", "signature", "batch", "several", "many", "add", "clipboard", "parse"],
    hash: "intake" },
  { label: "Who I've gone quiet with", icon: "clock", hint: "People",
    keys: ["quiet", "quietest", "neglected", "overdue", "cold", "stale", "silent", "forgotten"],
    hash: "people?sort=quiet" },
  { label: "People still to describe", icon: "pencil", hint: "People",
    keys: ["describe", "todo", "undescribed", "blank", "unknown"], hash: "people?filter=todo" },
  { label: "Ask your network a question", icon: "chat", hint: "Ask",
    keys: ["ask", "question", "chat", "answer", "why", "wonder"], hash: "ask" },
  // A one-shot param, not a `run` that posts to /api/chats itself: creating a chat
  // stays in ask.js, which is the only place that knows whether the chat you are
  // already in is empty. Same shape as "Start a new list", and "new" is already in
  // ONE_SHOT_PARAMS so the nonce makes it repeatable.
  { label: "Start a new chat", icon: "plus", hint: "Ask",
    keys: ["chat", "new", "thread", "fresh", "clean", "restart"], hash: "ask?new=1" },
  { label: "Import & sync data", icon: "sync", hint: "Data",
    keys: ["import", "sync", "data", "refresh", "messages", "gmail", "linkedin", "imessage", "whatsapp"],
    hash: "data" },
  { label: "Merge two people", icon: "layers", hint: "Data",
    keys: ["merge", "combine", "fold", "same"], hash: "data?merge=1" },
  { label: "Find duplicates", icon: "duplicates", hint: "Data",
    keys: ["duplicate", "duplicates", "dupe", "dup", "tidy", "clean"], hash: "data?dup=1" },
  { label: "Export a backup", icon: "save", hint: "Data",
    keys: ["export", "backup", "download", "csv", "json", "markdown", "save"], hash: "data?export=1" },
  { label: "Open your lists", icon: "list", hint: "Lists",
    keys: ["list", "lists", "group", "segment"], hash: "lists" },
  { label: "Start a new list", icon: "plus", hint: "Lists",
    keys: ["list", "new", "dinner", "invite", "event", "pipeline"], hash: "lists?new=1" },
  { label: () => (themeNow() === "dark" ? "Switch to light mode" : "Switch to dark mode"),
    icon: () => (themeNow() === "dark" ? "sun" : "moon"), hint: "Appearance",
    keys: ["dark", "light", "theme", "mode", "night"], run: toggleTheme },
  { label: "Open settings", icon: "gear", hint: "Settings",
    keys: ["settings", "preferences", "config", "key", "model", "api"], hash: "settings" },

  /* ── tail: one word away, never in the bare list ─────────────────────────── */
  { label: "Who I've been talking to lately", icon: "bolt", hint: "People", typedOnly: true,
    keys: ["recent", "recently", "active", "lately", "talking", "warm"], hash: "people?sort=recent" },
  { label: "Deleted people", icon: "trash", hint: "People", typedOnly: true,
    keys: ["deleted", "trash", "removed", "restore", "bin"], hash: "people?filter=deleted" },
  { label: "People you've excluded", icon: "ban", hint: "People", typedOnly: true,
    keys: ["excluded", "exclude", "hidden", "blocked", "ignored"],
    hash: "people?filter=excluded" },
  // Nothing runs it for you: the post-import chain does the free facts pass
  // only. Opens the row, with its Import — it does not fire the pull itself.
  { label: "Pull new LinkedIn profile facts", icon: "globe", hint: "Data", typedOnly: true,
    keys: ["linkedin", "profile", "facts", "enrich", "cities", "scrape", "pull"],
    hash: "data?row=linkedin_facts" },
  { label: "Follow my Mac's appearance", icon: "auto", hint: "Appearance", typedOnly: true,
    keys: ["auto", "system", "mac", "theme", "appearance", "follow"], run: followSystemTheme },
  { label: "Show my data folder in Finder", icon: "folder", hint: "This Mac", typedOnly: true,
    keys: ["finder", "folder", "file", "reveal", "where", "disk", "backup"],
    run: () => openTarget("data") },
  // Ask's four intent chips only render in its EMPTY state, so after your first
  // ever question they are gone for good — four affordances you can never see
  // again. Generated from Ask's own list so the two surfaces can't drift, and
  // they prefill the composer without sending, exactly as the chips did.
  ...INTENTS.map(it => ({
    label: `Ask: ${it.prefill.trimEnd()}${it.prefill.endsWith(" ") ? "…" : ""}`,
    icon: "chat", hint: "Ask", typedOnly: true,
    keys: ["ask", ...it.label.toLowerCase().split(/\s+/),
           ...it.prefill.toLowerCase().replace(/[^a-z ]/g, "").split(/\s+/).filter(w => w.length > 3)],
    hash: `ask?prefill=${encodeURIComponent(it.prefill)}`,
  })),
];

const of = (v) => (typeof v === "function" ? v() : v);

const IDENT_CMD = {
  email: { icon: "email", noun: "email" },
  phone: { icon: "phone", noun: "number" },
};

async function copyToClipboard(value, noun) {
  try { await navigator.clipboard.writeText(value); toast(`Copied ${noun}`); }
  catch { toast("Couldn’t copy"); }
}

// Commands that only exist somewhere. Merging from the open person's page pins
// THEM as the record that survives — which is the reason to start there rather
// than from Data, where you'd have to pick the keeper yourself. The rest need no
// fetch at all: the Omnibox already holds the whole /api/contact payload for the
// context chip, identifiers included.
function personCommands(c) {
  const idents = (c.identifiers || [])
    .filter(i => IDENT_CMD[i.kind] && i.value)
    .map(i => ({
      label: `Copy ${c.name}'s ${IDENT_CMD[i.kind].noun}`, icon: IDENT_CMD[i.kind].icon,
      hint: i.value, typedOnly: true,
      keys: ["copy", IDENT_CMD[i.kind].noun, i.kind, "clipboard"],
      run: () => copyToClipboard(i.value, IDENT_CMD[i.kind].noun),
    }));
  return [
    { label: `Merge ${c.name} with…`, icon: "layers", hint: "This person",
      keys: ["merge", "duplicate", "dupe", "combine", "fold"], hash: `people/${c.id}?merge=1` },
    { label: `Add a note about ${c.name}`, icon: "pencil", hint: "This person",
      keys: ["note", "remember", "write", "jot", "log"], hash: `people/${c.id}?note=1` },
    ...idents,
  ];
}

// One entry per city / role / topic the network actually contains, so ⌘K answers
// "who do I know in Paris". Worth the generation: /api/contacts?q= matches only
// name, company and position, so typing a city or a topic today quietly matches
// nobody at all. All typedOnly — on a large network these would bury the visible
// menu.
function facetCommands(facets) {
  if (!facets) return [];
  const mk = (value, label, icon, param, extraKeys) => ({
    label, icon, hint: "People", typedOnly: true,
    keys: [...String(value).toLowerCase().split(/[\s,/-]+/).filter(Boolean), ...extraKeys],
    hash: `people?${param}=${encodeURIComponent(value)}`,
  });
  // Topics are tallied out of free text, so the same one arrives in several
  // casings ("AI" and "ai" both come back) — two rows that read identically and
  // filter differently. Keep the first, which is the most common spelling.
  const firstOfEach = (vals) => {
    const seen = new Set();
    return (vals || []).filter(v => {
      const k = String(v).toLowerCase();
      return seen.has(k) ? false : (seen.add(k), true);
    });
  };
  return [
    ...firstOfEach(facets.locations).map(l => mk(l, `People in ${l}`, "pin", "location", ["in", "city", "where"])),
    ...firstOfEach(facets.roles).map(r => mk(r, `${r} in my network`, "work", "role", ["role", "job"])),
    ...firstOfEach(facets.topics).map(t => mk(t, `People who know ${t}`, "tag", "topic", ["topic", "about", "knows"])),
  ];
}

function commandsFor(context, facets) {
  const c = context?.contact;
  return [...(c ? personCommands(c) : []), ...COMMANDS, ...facetCommands(facets)];
}

// 3 = you typed a whole keyword · 2 = a keyword's prefix · 1 = it's somewhere in
// the label · 0 = no match. Every word of a multi-word query has to land.
function cmdScore(cmd, needle) {
  if (!needle) return 1;
  const label = of(cmd.label).toLowerCase();
  let worst = 3;
  for (const w of needle.split(/\s+/).filter(Boolean)) {
    let best = 0;
    for (const k of cmd.keys) {
      if (k === w) { best = 3; break; }
      if (k.startsWith(w)) best = Math.max(best, 2);
    }
    if (!best && label.includes(w)) best = 1;
    if (!best) return 0;
    worst = Math.min(worst, best);
  }
  return worst;
}

// Best match first, then the authored order — so an empty box reads as written
// and a typed one puts the thing you named on top. `min` is the bar: 1 shows
// everything that matches at all, 2 demands a real keyword. A typedOnly command
// always needs a real keyword, which is exactly what keeps it out of a bare box
// (an empty needle scores 1 for everything).
function rankCommands(cmds, needle, min) {
  return cmds
    .map((c, i) => ({ c, i, s: cmdScore(c, needle) }))
    .filter((x) => x.s >= (x.c.typedOnly ? Math.max(min, 2) : min))
    .sort((a, b) => b.s - a.s || a.i - b.i)
    .map((x) => x.c);
}

// What the omnibox pill advertises as "where you are" — the section, or the
// open person by name+avatar (a scope chip). Falls back to
// the section label while a person's name is still loading, so it never flashes
// a wrong or empty label.
const SECTION_LABEL = { people: "People", ask: "Ask", lists: "Lists", data: "Data", settings: "Settings", intake: "Intake" };

function Omnibox({ route }) {
  const [q, setQ] = useState("");
  const [people, setPeople] = useState([]);
  const [convos, setConvos] = useState([]);
  const [convoMode, setConvoMode] = useState("keyword"); // flips to "semantic" on demand
  const [semBusy, setSemBusy] = useState(false);
  const [hint, setHint] = useState("");  // "not installed" / "nothing imported" line
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState(0);
  const [context, setContext] = useState({ text: "People" });
  const [facets, setFacets] = useState(null);   // cities / roles / topics -> commands
  const inputRef = useRef(null);
  const boxRef = useRef(null);
  const popRef = useRef(null);
  const keyNav = useRef(false);   // arrow keys, as opposed to the mouse hovering

  // The popover caps at 420px and scrolls, so arrow-keying down a full command
  // list used to highlight rows nobody could see. Only follow the keyboard —
  // scrolling under a hovering mouse would fight the pointer.
  useEffect(() => {
    if (!open || !keyNav.current) return;
    popRef.current?.querySelector(".omni-row.sel")?.scrollIntoView({ block: "nearest" });
  }, [sel, open]);

  // The vocabulary for the per-city / per-role / per-topic commands. Both are
  // GETs the People view already calls, fetched once here and never refetched —
  // a stale city in the list costs a jump that finds nobody, which is what the
  // whole feature fixes.
  useEffect(() => {
    let live = true;
    Promise.all([
      api("/api/locations").then(d => d.locations || []).catch(() => []),
      api("/api/facets").then(d => ({ roles: d.roles || [], topics: d.topics || [] })).catch(() => ({ roles: [], topics: [] })),
    ]).then(([locations, f]) => { if (live) setFacets({ locations, ...f }); });
    return () => { live = false; };
  }, []);

  // Resolve the current context. A person page shows their name + avatar (one
  // cheap fetch); everything else is a static section label. While a person
  // loads we show "People" — the truthful parent section — never a stale name.
  useEffect(() => {
    if (route.name === "people" && route.id) {
      let live = true;
      setContext({ text: "People" });
      api("/api/contact/" + route.id)
        .then(d => { if (live) setContext({ text: d.name || "Person", contact: d }); })
        .catch(() => { if (live) setContext({ text: "Person" }); });
      return () => { live = false; };
    }
    setContext({ text: SECTION_LABEL[route.name] ?? "People" });
  }, [route.name, route.id]);

  const search = useCallback(debounce(async (query) => {
    if (!query.trim()) { setPeople([]); setConvos([]); setHint(""); return; }
    setConvoMode("keyword");
    // sort=closeness, the same order the People list opens in. Without a sort the
    // engine falls back on one LinkedIn column, so typing a common first name
    // listed whoever was connected most recently instead of the closest people.
    const ppl = api("/api/contacts?" + new URLSearchParams({ q: query, sort: "closeness" }))
      .then(d => (d.contacts || []).slice(0, 6)).catch(() => []);
    // live keyword pass over archived message text — FTS, cheap per keystroke.
    // available:false carries a real reason ("nothing imported yet") to show
    // instead of a silent empty result.
    const cnv = api("/api/search/conversations?" + new URLSearchParams({ q: query, mode: "keyword" }))
      .then(d => d.available === false ? { list: [], msg: d.message || "" } : { list: (d.conversations || []).slice(0, 4), msg: "" })
      .catch(() => ({ list: [], msg: "" }));
    const [p, c] = await Promise.all([ppl, cnv]);
    setPeople(p); setConvos(c.list); setHint(c.msg);
  }, 180), []);

  // one-shot semantic pass (embedding model — too heavy per keystroke)
  const searchMeaning = async () => {
    const query = q.trim();
    if (!query || semBusy) return;
    setSemBusy(true);
    try {
      // meaning search finds PEOPLE too, not just conversations:
      // "furniture designer in Denmark" should surface the person, not require
      // an exact tag match.
      const pplP = api("/api/search/semantic?" + new URLSearchParams({ q: query }))
        .then(d => d.available === false ? { list: [], msg: d.message || "" } : { list: (d.contacts || []).slice(0, 6), msg: "" })
        .catch(() => ({ list: [], msg: "" }));
      const cnvP = api("/api/search/conversations?" + new URLSearchParams({ q: query, mode: "semantic" }))
        .then(d => d.available === false ? { list: [], msg: d.message || "" } : { list: (d.conversations || []).slice(0, 6), msg: "" })
        .catch(() => ({ list: [], msg: "" }));
      const [pp, cc] = await Promise.all([pplP, cnvP]);
      if (pp.list.length) setPeople(pp.list);   // keep keyword people if meaning found none
      setConvos(cc.list);
      setHint(pp.msg || cc.msg);
      setConvoMode("semantic");
    } catch { /* keep keyword results */ }
    setSemBusy(false);
    inputRef.current?.focus();
  };

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        // ⌘K toggles. It used to only ever open, so the shortcut that summons
        // the palette could not dismiss it — Escape was the single way out.
        if (open) { setOpen(false); inputRef.current?.blur(); return; }
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    const onClick = (e) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mousedown", onClick); };
  }, [open]);

  // An empty box is the command palette; a `>` prefix narrows it to commands.
  // Anything else is a search, where a command only elbows in if you actually
  // typed one of its keywords — looking for a person shouldn't be padded out.
  const raw = q.trim();
  const cmdMode = !raw || raw.startsWith(">");
  const cmds = commandsFor(context, facets);

  const rows = [];
  if (cmdMode) {
    const needle = raw.startsWith(">") ? raw.slice(1).trim().toLowerCase() : "";
    for (const c of rankCommands(cmds, needle, 1)) rows.push({ kind: "cmd", c });
  } else {
    for (const p of people) rows.push({ kind: "person", p });
    for (const c of convos) rows.push({ kind: "convo", c });
    for (const c of rankCommands(cmds, raw.toLowerCase(), 2)) rows.push({ kind: "cmd", c });
    rows.push({ kind: "meaning" });
    rows.push({ kind: "ask" });
  }
  const firstCmd = rows.findIndex((r) => r.kind === "cmd");

  const run = (row) => {
    if (!row) return;
    if (row.kind === "meaning") { searchMeaning(); return; } // stays open, swaps results
    if (row.kind === "person") go(`people/${row.p.id}`);
    else if (row.kind === "convo") {
      // NC-112: a message you sent to a group has no person of its own. The
      // server names someone in that conversation whose page shows it, or
      // nobody, and then there is no page to open.
      const to = row.c.open_id || row.c.contact_id;
      if (!to) return;
      go(`people/${to}`);
    }
    else if (row.kind === "ask") go(`ask?q=${encodeURIComponent(q.trim())}`);
    else if (row.kind === "cmd") {
      if (row.c.run) row.c.run();
      else go(of(row.c.hash));
    }
    setQ(""); setPeople([]); setConvos([]); setHint(""); setOpen(false);
    inputRef.current?.blur();
  };

  // Hovering highlights, but must not hand the scroll position to the keyboard.
  const hover = (i) => { keyNav.current = false; setSel(i); };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); keyNav.current = true; setSel(s => Math.min(s + 1, rows.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); keyNav.current = true; setSel(s => Math.max(s - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); run(rows[sel] || rows[0]); }
    else if (e.key === "Escape") { setOpen(false); inputRef.current?.blur(); }
  };

  return html`
    <div class="omni-wrap" ref=${boxRef}>
      <div class="omni">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        ${context && html`
          <button class="omni-context" title=${`On ${context.text}. Press ⌘K to jump anywhere.`}
            onMouseDown=${e => e.preventDefault()}
            onClick=${() => { inputRef.current?.focus(); inputRef.current?.select(); }}>
            ${context.contact && html`<${Avatar} c=${context.contact} size=${AV.chip} />`}
            <span class="octx-label">${context.text}</span>
          </button>`}
        <input
          ref=${inputRef}
          value=${q}
          placeholder="Search a name · > for commands"
          aria-label="Search people or run a command"
          role="combobox"
          aria-expanded=${open ? "true" : "false"}
          aria-controls="omni-pop"
          aria-autocomplete="list"
          onInput=${e => {
            const v = e.target.value;
            setQ(v); setSel(0); setOpen(true);
            // ">" is ours; don't spend a search round-trip looking for people
            // called ">merge", and drop whatever the last query found.
            if (v.trim().startsWith(">")) { setPeople([]); setConvos([]); setHint(""); }
            else search(v);
          }}
          onFocus=${() => setOpen(true)}
          onKeyDown=${onKeyDown}
        />
        ${/* The palette is advertised, not hidden — click = ⌘K. */ ""}
        <button class="kbd-pill" title="Jump anywhere (⌘K)" aria-label="Open the command palette"
          onMouseDown=${e => e.preventDefault()}
          onClick=${() => { inputRef.current?.focus(); inputRef.current?.select(); }}>⌘K</button>
      </div>
      ${open && html`
        <div class="omni-pop" id="omni-pop" ref=${popRef} role="listbox" aria-label="Results">
          ${hint && !cmdMode && html`<div class="omni-note">${hint}</div>`}
          ${/* The popover used to vanish outright on no matches, which reads as
                a broken box rather than an answer. ">" mode is the only way to
                get here — a plain query always keeps its two fallback rows. */ ""}
          ${rows.length === 0 && html`
            <div class="omni-note">No command matches. Clear the box to see them all.</div>`}
          ${!cmdMode && people.length > 0 && html`<div class="omni-head">People</div>`}
          ${rows.map((row, i) => {
            const cls = "omni-row" + (i === sel ? " sel" : "");
            if (row.kind === "person") return html`
              <button key=${"p" + row.p.id} class=${cls} onMouseEnter=${() => hover(i)} onClick=${() => run(row)}>
                <${Avatar} c=${row.p} size=${AV.xs} />
                <div class="grow">
                  <div class="r-name">${row.p.name}</div>
                  <div class="r-meta">${roleStr(row.p) || row.p.home_metro || ""}</div>
                </div>
                <span class="r-kind">open</span>
              </button>`;
            if (row.kind === "convo") {
              const btn = html`
                <button key=${"cv" + i} class=${cls} onMouseEnter=${() => hover(i)} onClick=${() => run(row)}>
                  <span class="omni-glyph"><${Icon} name=${CHANNEL_ICON[row.c.channel] || "chat"} /></span>
                  <div class="grow">
                    <div class="r-name">${row.c.conversation ? "You, in " + row.c.conversation : row.c.contact_name}</div>
                    <div class="r-meta">${(row.c.snippet || "").replace(/<[^>]*>/g, "")}</div>
                  </div>
                  <span class="r-kind">${relDate(row.c.ts) || ""}</span>
                </button>`;
              if (convos[0] === row.c) return [html`<div key="cvhead" class="omni-head">Conversations${convoMode === "semantic" ? " · by meaning" : ""}</div>`, btn];
              return btn;
            }
            if (row.kind === "meaning") return html`
              <button key="meaning" class=${cls} onMouseEnter=${() => hover(i)} onClick=${() => run(row)}>
                <span class="omni-glyph"><${Icon} name="brain" /></span>
                <div class="grow"><div class="r-name">${semBusy ? "Searches by meaning…" : "Search conversations by meaning"}</div>
                <div class="r-meta">Finds the words that mean the same</div></div>
                <span class="r-kind">${semBusy ? "…" : html`<${Icon} name="enter" />`}</span>
              </button>`;
            if (row.kind === "ask") return html`
              <button key="ask" class=${cls} onMouseEnter=${() => hover(i)} onClick=${() => run(row)}>
                <span class="omni-glyph"><${Icon} name="chat" /></span>
                <div class="grow"><div class="r-name">Ask: “${q.trim()}”</div>
                <div class="r-meta">Asks your whole network</div></div>
                <span class="r-kind"><${Icon} name="enter" /></span>
              </button>`;
            const label = of(row.c.label);
            const btn = html`
              <button key=${"cmd" + label} class=${cls} onMouseEnter=${() => hover(i)} onClick=${() => run(row)}>
                <span class="omni-glyph"><${Icon} name=${of(row.c.icon)} /></span>
                <div class="grow"><div class="r-name">${label}</div></div>
                <span class="r-kind">${row.c.hint}</span>
              </button>`;
            if (i === firstCmd) return [html`<div key="cmdhead" class="omni-head">Commands</div>`, btn];
            return btn;
          })}
          ${cmdMode && rows.length > 0 && html`
            <div class="omni-foot"><${Icon} name="arrowUp" /><${Icon} name="arrowDown" /> move · <${Icon} name="enter" /> run · type a name, a city or a topic for more</div>`}
        </div>`}
    </div>`;
}

/* ── App ────────────────────────────────────────────────────────────────────── */
function App() {
  const [route, setRoute] = useState(parseHash());
  const [statusOk, setStatusOk] = useState(true);
  // NC-011: when startup finds the database unreadable, it moves it aside and
  // makes a fresh one. That is the single most alarming thing this app can do to
  // someone's data, so it has to be said in the app — printing it to a terminal
  // the .app user never opens is the same as not saying it.
  const [dbRecovery, setDbRecovery] = useState("");
  const [onboarded, setOnboarded] = useState(null); // null = unknown yet
  const [, bump] = useState(0); // re-render on theme change (icon swap)

  useEffect(() => {
    const onHash = () => {
      const r = parseHash();
      TAB_HASH[r.name] = tabPlace(location.hash.replace(/^#/, ""));  // remember this tab's place
      setRoute(r);
    };
    onHash();  // record the initial place too
    window.addEventListener("hashchange", onHash);
    const onTheme = () => bump(x => x + 1);   // a quick command flipped the theme
    window.addEventListener("nc-theme", onTheme);
    // ⌘1–⌘5 for the tabs, with the same last-place restore as clicking one.
    //
    // In a browser this handler IS the binding. (Until NC-330 the packaged app
    // showed this page in a window whose menu bar claimed ⌘1–⌘5 and ⌘K and handed
    // them back as synthetic keydowns; the app's window is native now, and this
    // page runs only from source.)
    const onTabKey = (e) => {
      if (!e.metaKey && !e.ctrlKey) return;
      if (e.altKey || e.shiftKey) return;
      const n = parseInt(e.key, 10);
      if (!(n >= 1 && n <= TABS.length)) return;
      e.preventDefault();
      const [hash] = TABS[n - 1];
      // parseHash() rather than `route`: this effect runs once, so its closure
      // would hold whatever route was current at mount.
      go(tabTarget(hash, parseHash().name));
    };
    window.addEventListener("keydown", onTabKey);
    // Nobody reads the status dot of a window that is hidden or minimised, so the
    // check waits until it is seen again (NC-212: every check is a request).
    const ping = async () => {
      if (document.hidden) return;
      try {
        const d = await api("/api/status");
        setStatusOk(true);
        setDbRecovery(d.db_recovery || "");
      } catch { setStatusOk(false); }
    };
    ping();
    const t = setInterval(ping, 15000);
    document.addEventListener("visibilitychange", ping);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("nc-theme", onTheme);
      window.removeEventListener("keydown", onTabKey);
      document.removeEventListener("visibilitychange", ping);
      clearInterval(t);
    };
  }, []);

  useEffect(() => {
    api("/api/onboarding").then(d => setOnboarded(!!d.done)).catch(() => setOnboarded(true));
  }, []);

  const view = {
    "people": () => html`<${People} key=${"people"} id=${route.id} params=${route.params} go=${go} />`,
    // Ask is many chats now, so it takes the route id the parser has always
    // produced and used to discard: #ask/7 is chat 7, bare #ask is the newest one.
    "ask": () => html`<${Ask} key="ask" id=${route.id} params=${route.params} go=${go} />`,
    "lists": () => html`<${Lists} key="lists" id=${route.id} params=${route.params} go=${go} />`,
    "data": () => html`<${DataPage} key="data" params=${route.params} go=${go} />`,
    "settings": () => html`<${SettingsPage} key="settings" params=${route.params} go=${go} />`,
    // Paste people in, review what was found, save. Takes no route id: a batch
    // is a draft, not a record with an address.
    "intake": () => html`<${Intake} key="intake" go=${go} />`,
  }[route.name]();

  // Shell grammar: .app-shell is the tinted page the
  // app sits in; .app-view is the inset sheet the live view floats on. Every
  // view renders INSIDE .app-view, so it may assume a --panel background.
  return html`
    <div class="app-shell">
      <header class="topbar">
        <button class="wordmark" onClick=${() => go("people")} aria-label="Go to People" title="People">
          <span class="mark" aria-hidden="true">n</span>
          <span class="name">Network CRM</span>
          ${/* NC-099: this said "Runs in the cloud. Data syncs from your Mac."
                on any non-loopback address — including README's home-server
                shape, where nothing is in a cloud and nothing syncs. The dot
                reports where the page came from, which is all it can know. */
            needsAccessToken()
            ? html`<span class="local-dot remote" title="Served over your network from another Mac. Your data stays on that Mac." />`
            : html`<span class=${"local-dot" + (statusOk ? "" : " down")} title=${statusOk ? "Runs on this Mac. Your data stays here." : "Server unreachable"} />`}
        </button>
        <${Omnibox} route=${route} />
        <nav class="nav-tabs" aria-label="Sections">
          ${TABS.map(([hash, label]) => html`
            <button key=${hash} class=${"nav-tab" + (route.name === hash ? " active" : "")}
              aria-current=${route.name === hash ? "page" : null}
              title=${route.name === hash && route.id ? COLLAPSIBLE[hash] : null}
              onClick=${() => go(tabTarget(hash, route.name))}>${label}</button>`)}
        </nav>
        <button class=${"icon-btn" + (route.name === "settings" ? " on" : "")}
          aria-label="Settings" title="Settings" onClick=${() => go("settings")}><${Icon} name="gear" /></button>
        ${/* toggleTheme announces itself (nc-theme), which is what re-renders this icon */ ""}
        <button class="icon-btn" aria-label="Toggle dark mode" title="Toggle dark mode" onClick=${toggleTheme}>
          <${Icon} name=${themeNow() === "dark" ? "sun" : "moon"} />
        </button>
      </header>
      ${dbRecovery && html`
        <div class="db-recovery" role="alert">
          <${Icon} name="info" />
          <p>${dbRecovery}</p>
          <button class="btn" onClick=${async () => {
            // A POST, because dismissing changes something: a GET can be repeated by a
            // prefetch or a second window without anyone pressing this button.
            try { await api("/api/db-recovery-notice/dismiss", { method: "POST", body: JSON.stringify({}) }); }
            catch { /* it reappears next poll */ }
            setDbRecovery("");
          }}>I've read this</button>
        </div>`}
      ${/* keyed by view name ONLY: People/Lists key their detail pane by id
           themselves, and keying main on the id would remount the whole
           subtree on every selection — wiping the list column's search box.
           The one exception is setup: the view under it loads while setup is
           open, so without a fresh start when setup closes, People would still
           show the empty list from before setup's imports, and no line saying
           it is waiting for the LinkedIn file setup just asked for (NC-319). */ ""}
      <main class="app-view view" key=${route.name + (onboarded === false ? ":setup" : "")}>${view}</main>
      ${onboarded === false && html`<${Onboarding} onDone=${() => setOnboarded(true)} />`}
    </div>`;
}

render(html`<${App} />`, document.getElementById("root"));

// NC-049. Straight after render(), not on DOMContentLoaded: this is a module,
// modules defer, so DOMContentLoaded fires after the app has already mounted.
dismissBoot();

// ── Service worker (PWA) ──────────────────────────────────────────────────────
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err) => {
      console.warn("SW registration failed:", err);
    });
  });
}
