// Shared components: avatars with strength rings, tags, markdown-lite, empty
// states, skeletons, meter bars, the add-to-list popover, and the AI
// draft-message modal.
//
// These render thousands of times per session, so every value here comes from
// static/css/tokens.css — a hardcoded colour or size in this file fights the
// palette on every surface at once. Sizes are expressed as multiples of
// --row-h, which makes density one decision rather than forty.
import { html, useState, useEffect } from "../vendor/preact-htm.module.js";
import { api, toast, initials, huePairFor, strengthRing, fmtDay, noun } from "./api.js";
import { Icon } from "./icons.js";

/** Avatar scale, derived from the row height so avatars stay inside their rows
 *  when density is retuned. Callers may still pass a plain number (px) — every
 *  other view does, and a number keeps working. */
export const AV = {
  chip: "calc(var(--row-h) * .45)",   // omnibox context chip
  xs: "calc(var(--row-h) * .6)",      // popover rows
  sm: "calc(var(--row-h) * .7)",      // dense list rows
  md: "calc(var(--row-h) * .8)",      // cards
  lg: "calc(var(--row-h) * 1.1)",     // section heads
  hero: "calc(var(--row-h) * 1.6)",   // review card, person head
};

const len = (v) => (typeof v === "number" ? v + "px" : v);

/** What to say when the engine answers `ok: false` to an open request, which is how
 *  it reports that macOS refused or a command was missing. Said in a sentence,
 *  because a button that does nothing reads as a button that is broken. */
export const OPEN_REFUSED = "Your Mac did not open it. Press the button again, or open it yourself.";

/** Ask the server to reveal an allowlisted path in Finder (or open a URL). The
 *  client only ever sends a key — the path lives in importers.OPEN_TARGETS.
 *
 *  A refusal comes back as a normal answer with `ok: false`, not as an error, so
 *  it is read here: ignoring it left the button silently doing nothing. */
export async function openTarget(key) {
  try {
    const d = await api("/api/import/open", { method: "POST", body: JSON.stringify({ source: key }) });
    if (d && d.ok === false) toast(d.message || d.detail || OPEN_REFUSED);
  } catch (e) { toast(e.message); }
}

/** The ↗ buttons for a source. Most have one; iMessage has two (copy from,
 *  paste into). Falls back to the older single `open_helper` shape so a source
 *  that hasn't been given a list still renders. */
export function OpenHelpers({ spec }) {
  const helpers = (spec || {}).open_helpers
    || (spec && spec.open_helper ? [{ key: spec.key, label: spec.open_helper }] : []);
  return helpers.map(h => html`
    <button class="btn" key=${h.key} onClick=${() => openTarget(h.key)}><${Icon} name="external" /> ${h.label}</button>`);
}

/** The box the picker's own error messages promise.
 *
 *  NC-178. Both failures of POST /api/import/pick-file ended "you can paste the
 *  path into the field instead", and there was no field: the chosen path was
 *  rendered as a read-only chip, so the only advice given to somebody whose
 *  picker could not open named a control that was not on screen — and the two
 *  cases where that fires are a Mac with no osascript and a picker that would
 *  not launch, which is exactly when there is no other way in.
 *
 *  It is always shown, never only after a successful pick. A box that appears
 *  once the picker has already worked helps nobody the picker failed.
 *
 *  Lives here rather than in dataview.js because there are three of these — the
 *  Data page's source row and both onboarding steps — and one shared component
 *  cannot be wrong in three different ways.
 */
export function PathBox({ kind, path, onPath }) {
  const folder = kind === "folder";
  return html`
    <input class="src-path-box" type="text" spellcheck="false"
      autocapitalize="off" autocorrect="off" autocomplete="off"
      aria-label=${folder ? "Path to the folder" : "Path to the file"}
      placeholder=${folder ? "…or paste the folder's path here"
                           : "…or paste the file's path here"}
      title=${path || ""}
      value=${path}
      onInput=${e => onPath(e.target.value)} />`;
}

/** Escape closes a modal. Both modals could only be dismissed with the mouse —
 *  overlay click or the × — which became a trap the moment ⌘K learned to open
 *  MergeModal directly: a keystroke put you somewhere no keystroke could leave.
 *  Captured on the window so it works wherever focus happens to be. */
function useEscape(onClose) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

/** One searchable side of a merge: type a name, pick a person. */
function MergePick({ label, chosen, exclude, onPick, onClear }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState([]);
  useEffect(() => {
    if (chosen || q.trim().length < 2) { setRows([]); return; }
    let live = true;
    const t = setTimeout(async () => {
      try {
        const d = await api("/api/contacts?" + new URLSearchParams({ q: q.trim() }));
        if (live) setRows((d.contacts || []).filter(r => r.id !== exclude).slice(0, 6));
      } catch { /* a failed lookup just shows nothing */ }
    }, 200);
    return () => { live = false; clearTimeout(t); };
  }, [q, chosen, exclude]);

  if (chosen) {
    return html`
      <div>
        <div class="omni-head">${label}</div>
        <div class="src-row"
             style="display:flex;align-items:center;gap:var(--gap-sm);padding:var(--gap-sm);min-height:var(--row-h)">
          <${Avatar} c=${chosen} size=${AV.sm} />
          <div class="grow" style="flex:1;min-width:0">
            <div class="r-name">${chosen.name}</div>
            <div class="r-meta">${personLine(chosen)}</div>
          </div>
          ${onClear && html`<button class="btn quiet mini" onClick=${onClear}>Change</button>`}
        </div>
      </div>`;
  }
  return html`
    <div>
      <div class="omni-head">${label}</div>
      <input class="input" placeholder="Type a name" value=${q}
             onInput=${e => setQ(e.target.value)} autoFocus />
      ${rows.map(r => html`
        <button key=${r.id} class="omni-row" onClick=${() => onPick(r)}>
          <${Avatar} c=${r} size=${AV.xs} />
          <div class="grow">
            <div class="r-name">${r.name}</div>
            <div class="r-meta">${personLine(r)}</div>
          </div>
        </button>`)}
      ${q.trim().length >= 2 && rows.length === 0
        && html`<div class="r-meta" style="padding:var(--gap-sm)">No one else has that name.</div>`}
    </div>`;
}

/** Every message the app holds with this person, across all channels. The rows
 *  carry them as `counts` (one number per channel), not as the separate
 *  `msg_count`, `imsg_count`, `wa_count` and `em_count` columns, which an
 *  /api/contacts row does not have: reading those always gave none. A `total` key,
 *  if the engine ever adds one, is skipped so it is never counted twice. */
export function messageTotal(c) {
  return Object.entries(c.counts || {})
    .reduce((sum, [channel, n]) => (channel === "total" ? sum : sum + (n || 0)), 0);
}

/** Enough detail to tell two same-named people apart before folding them. */
function personLine(c) {
  const msgs = messageTotal(c);
  return [c.position, c.company, c.home_city,
          msgs ? `${msgs.toLocaleString()} ${noun(msgs, "message", "messages")}` : ""].filter(Boolean).join(" · ")
         || "No details yet";
}

/** Fold one contact into another. `fixed` pins one side (opened from a profile);
 *  omit it to search both (opened from Find duplicates).
 *
 *  Deliberately two explicit steps — pick, then confirm — because a merge is not
 *  reversible: the folded record is deleted, and which of the two survives
 *  decides whose id, notes and history the result carries. */
export function MergeModal({ fixed = null, onClose, onMerged }) {
  const [keep, setKeep] = useState(fixed);
  const [fold, setFold] = useState(null);
  const [busy, setBusy] = useState(false);
  useEscape(onClose);

  const swap = () => { const k = keep; setKeep(fold); setFold(k); };
  const merge = async () => {
    if (!keep || !fold || keep.id === fold.id) return;
    setBusy(true);
    try {
      await api("/api/merge", {
        method: "POST",
        body: JSON.stringify({ keep_id: keep.id, dup_id: fold.id }),
      });
      toast(`Merged ${fold.name} into ${keep.name}`);
      onMerged && onMerged(keep.id);
      onClose();
    } catch (e) {
      toast(e.message);
      setBusy(false);
    }
  };

  const ready = keep && fold && keep.id !== fold.id;
  return html`
    <div class="overlay" onClick=${e => { if (e.target === e.currentTarget) onClose(); }}>
      <div class="modal" role="dialog" aria-modal="true" aria-label="Merge two people">
        <button class="close-x" onClick=${onClose} aria-label="Close"><${Icon} name="close" /></button>
        <h2>Merge two people</h2>
        <p class="r-meta">Notes, messages, dates, lists and identifiers move to the
          person you keep. The app then deletes the other record. You cannot undo this.</p>

        <${MergePick} label="Keep" chosen=${keep} exclude=${fold && fold.id}
                      onPick=${setKeep} onClear=${fixed ? null : () => setKeep(null)} />
        <${MergePick} label="Delete after the merge" chosen=${fold} exclude=${keep && keep.id}
                      onPick=${setFold} onClear=${() => setFold(null)} />

        ${ready && html`
          <button class="btn quiet mini" style="margin-top:var(--gap-sm)" onClick=${swap}>
            <${Icon} name="sort" /> Keep ${fold.name.split(" ")[0]} instead
          </button>`}

        <div class="modal-foot">
          <button class="btn quiet" onClick=${onClose}>Cancel</button>
          <button class="btn primary" disabled=${!ready || busy} onClick=${merge}>
            ${busy ? "Merging…" : ready ? `Merge into ${keep.name}` : "Select both people"}
          </button>
        </div>
      </div>
    </div>`;
}

/** The ring tooltip. Says the number and the tier, and says so plainly when
 *  there is no score — a blank ring around a stranger is information too. */
function ringTitle(c) {
  if (!c.strength && !c.strength_label) return "No closeness score yet";
  return `Closeness ${c.strength || 0} of 100`
    + (c.strength_label ? ` — ${c.strength_label}` : "");
}

/** Monogram or photo, with an optional strength ring.
 *
 *  `size` is the inner avatar edge: a number is px (what every view passed
 *  before the AV scale existed), a string is any CSS length — pass AV.* so the
 *  avatar tracks --row-h. The monogram colours come from --avatar-* (cool and
 *  low-chroma by design: thousands of these must not shout).
 *
 *  aria-hidden because every call site renders the person's name next to it;
 *  without that the initials get read out twice on every row. */
export function Avatar({ c, size = AV.md, ring = false, fontSize }) {
  const [h1, h2] = huePairFor(c.name);
  const style = {
    "--av": len(size),
    "--h": h1,
    "--h2": h2,
    width: "var(--av)",
    height: "var(--av)",
    fontSize: fontSize == null ? "calc(var(--av) * .38)" : len(fontSize),
  };
  const inner = html`
    <div class="avatar" style=${style} aria-hidden="true">
      <span>${initials(c.name)}</span>
      ${c.photo && html`<img src=${c.photo} alt="" loading="lazy" onerror=${e => e.target.remove()} />`}
    </div>`;
  if (!ring) return inner;
  return html`<div class="avatar-ring" style=${strengthRing(c)} title=${ringTitle(c)}>${inner}</div>`;
}

export function CatDot({ category }) {
  const col = { personal: "var(--cat-personal)", professional: "var(--cat-professional)", both: "var(--cat-both)", unknown: "var(--cat-unknown)" }[category] || "var(--cat-none)";
  return html`<span class="cat-dot" style=${{ background: col }} title=${category || "No category"} />`;
}

/** Location and role are factual (outlined); topics are categorical (tinted).
 *  Extra topics collapse into a +N pill rather than vanishing — a silent
 *  truncation makes a row look complete when it is not. */
export function Tags({ c, max = 3 }) {
  const out = [];
  const loc = c.home_metro || c.home_city;
  if (loc) out.push(html`<span class="tag loc" key="loc">${loc}</span>`);
  if (c.role_type) out.push(html`<span class="tag role" key="role">${c.role_type}</span>`);
  const topics = c.topics || [];
  const room = Math.max(0, max - out.length);
  for (const t of topics.slice(0, room)) {
    out.push(html`<span class="tag topic" key=${t}>${t}</span>`);
  }
  const rest = topics.length - Math.min(room, topics.length);
  if (rest > 0) {
    out.push(html`<span class="tag" key="more" title=${topics.slice(room).join(", ")}>+${rest}</span>`);
  }
  return out;
}

/** Markdown-lite → preact nodes. Handles **bold**, *italic*, bullet lists,
 * paragraphs, and bare links. Safe by construction — never uses innerHTML. */
export function Md({ text }) {
  const blocks = String(text || "").split(/\n{2,}/);
  return html`<div class="md">
    ${blocks.map(block => {
      const lines = block.split("\n");
      const isList = lines.every(l => /^\s*[-•*]\s+/.test(l) || !l.trim());
      if (isList && lines.some(l => l.trim())) {
        return html`<ul>${lines.filter(l => l.trim()).map(l =>
          html`<li>${inline(l.replace(/^\s*[-•*]\s+/, ""))}</li>`)}
        </ul>`;
      }
      return html`<p>${lines.map((l, i) => html`${i > 0 ? html`<br/>` : ""}${inline(l)}`)}</p>`;
    })}
  </div>`;
}

function inline(text) {
  // tokenize: **bold**, *italic*, `code`, http(s) links
  const parts = [];
  const re = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`|https?:\/\/[^\s)]+)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) parts.push(html`<b>${tok.slice(2, -2)}</b>`);
    else if (tok.startsWith("`")) parts.push(html`<code>${tok.slice(1, -1)}</code>`);
    else if (tok.startsWith("*")) parts.push(html`<i>${tok.slice(1, -1)}</i>`);
    else parts.push(html`<a href=${tok} target="_blank" rel="noopener">${tok}</a>`);
    last = m.index + tok.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

/** An empty state that reads as deliberate: a stated fact, then what to do
 *  about it. `hint` is the quieter third line when there is one. */
export function Empty({ title, hint, children }) {
  return html`
    <div class="empty-soft">
      ${title && html`<b style="color:var(--muted)">${title}</b>`}
      ${children && html`<div>${children}</div>`}
      ${hint && html`<div style="color:var(--faint)">${hint}</div>`}
    </div>`;
}

/** Loading placeholder rows. `h` defaults to the real row height so the page
 *  does not jump when the data lands; the shimmer highlight is --skel-hi. */
export function SkelRows({ n = 5, h = "var(--row-h)", gap = "var(--gap-sm)" }) {
  return html`<div role="status" aria-label="Loading"
    style=${{ display: "flex", flexDirection: "column", gap: len(gap), padding: "var(--gap-sm) 0" }}>
    ${Array.from({ length: n }, (_, i) => html`<div class="skel" key=${i} style=${{ height: len(h) }} />`)}
  </div>`;
}

/** A labeled 0..1 meter bar (closeness breakdown, coverage, etc.). The track is
 *  decoration — the label and the hint already say the number out loud. */
export function Meter({ label, value, hint }) {
  const pct = Math.round(Math.max(0, Math.min(1, value || 0)) * 100);
  return html`
    <div class="meter">
      <span class="m-label">${label}</span>
      <span class="m-track" aria-hidden="true"><span class="m-fill" style=${{ width: pct + "%" }} /></span>
      <span class="m-hint">${hint ?? pct + "%"}</span>
    </div>`;
}

/** AI-receipt line: "From N messages · M channels · built <date>",
 * plus a stale micro-tag when newer messages exist. meta = { messages, channels,
 * built_at, newer_messages }; channels may be a count or an array. */
export function Receipt({ meta }) {
  if (!meta) return null;
  const nCh = Array.isArray(meta.channels) ? meta.channels.length : meta.channels;
  const bits = [];
  if (meta.messages != null) bits.push(`From ${meta.messages} message${meta.messages === 1 ? "" : "s"}`);
  if (nCh != null) bits.push(`${nCh} channel${nCh === 1 ? "" : "s"}`);
  if (meta.built_at) bits.push(`built ${fmtDay(meta.built_at)}`);
  if (!bits.length && !meta.newer_messages) return null;
  const newer = meta.newer_messages || 0;
  return html`
    <div class="receipt">
      <span>${bits.join(" · ")}</span>
      ${newer > 0 && html`<span class="stale">${newer} new message${newer === 1 ? "" : "s"} since</span>`}
    </div>`;
}

/** Compact "‹ 3 / 12 ›" stepper. index is 1-based. */
export function Stepper({ index, total, onPrev, onNext }) {
  if (!total) return null;
  return html`
    <div class="stepper">
      <button title="Previous" aria-label="Previous" disabled=${index <= 1} onClick=${onPrev}><${Icon} name="chevronLeft" /></button>
      <span class="s-count">${index} / ${total}</span>
      <button title="Next" aria-label="Next" disabled=${index >= total} onClick=${onNext}><${Icon} name="chevronRight" /></button>
    </div>`;
}

/** The sentence after adding people to a list, from what the engine says it ADDED.
 *  `sent` is how many people were offered. Someone who was taken off the list and is
 *  put back counts as added (the engine restores them), so only a person who is on
 *  the list right now is called "already in the list". */
export function addedMessage(listName, sent, added) {
  const n = typeof added === "number" ? added : sent;
  const skipped = sent - n;
  return n === 0
    ? `“${listName}” already holds everyone you selected`
    : `Added ${n} ${n === 1 ? "person" : "people"} to “${listName}”`
      + (skipped > 0 ? ` · ${skipped} already in the list` : "");
}

/** Add a set of people to a list — existing or new. Shared by the People
 * bulk action (the whole filtered set), the person page (one person), and the
 * per-turn selection bar in Ask.
 *
 * Lives here rather than in people.js because Ask needs the same popover, and
 * "add to an existing list OR name a new one" is a decision that must be made
 * in exactly one place — two copies would drift the moment one of them learned
 * about, say, list ordering.
 *
 * `btnClass` and `alignLeft` exist only because the trigger sits in a row of
 * mini buttons in Ask and at the left edge of a wide card column, where the
 * default right-anchored popover would hang off screen. Both default to what
 * People has always rendered. */
export function AddToList({ contactIds, trigger, dropUp, onDone, btnClass = "btn", alignLeft = false }) {
  const [open, setOpen] = useState(false);
  const [lists, setLists] = useState([]);
  const [name, setName] = useState("");
  useEffect(() => { if (open) api("/api/lists").then(d => setLists(d.lists || [])).catch(() => {}); }, [open]);
  const ids = contactIds || [];
  const commit = async (listId, listName) => {
    if (!ids.length || !listName) return;
    try {
      // Report what the server actually ADDED, not what we sent: both endpoints
      // return `added`, and members are deduped, so picking 20 people off an Ask
      // answer for a list that already holds 12 of them adds 8. Claiming 20
      // would be the kind of small lie that makes you distrust the count.
      const d = listId
        ? await api(`/api/list/${listId}/add`, { method: "POST", body: JSON.stringify({ contact_ids: ids }) })
        : await api("/api/lists", { method: "POST", body: JSON.stringify({ name: listName, contact_ids: ids }) });
      toast(addedMessage(listName, ids.length, d?.added));
      setOpen(false); setName(""); onDone && onDone();
    } catch (e) { toast(e.message); }
  };
  const pos = dropUp ? "bottom:calc(100% + var(--gap-sm));top:auto" : "top:calc(100% + var(--gap-sm));bottom:auto";
  const side = alignLeft ? "left:0;right:auto;" : "left:auto;right:0;";
  return html`
    <div style="position:relative">
      <button class=${btnClass} aria-expanded=${open} onClick=${() => setOpen(o => !o)}>
        ${trigger || html`<${Icon} name="plus" /> Add to list`}
      </button>
      ${open && html`
        <div class="omni-pop" style=${side + "width:min(17rem,80vw);" + pos}>
          <div class="omni-head">Add ${ids.length} ${ids.length === 1 ? "person" : "people"} to a list</div>
          ${lists.map(l => html`
            <button key=${l.id} class="omni-row" style="min-height:var(--row-h)" onClick=${() => commit(l.id, l.name)}>
              <div class="grow"><div class="r-name">${l.name}</div><div class="r-meta">${l.members} ${l.members === 1 ? "person" : "people"}</div></div>
              <span class="r-kind">add</span>
            </button>`)}
          <div style="display:flex;gap:var(--gap-sm);padding:var(--gap-sm)">
            <input class="input" placeholder="Name a new list" value=${name}
              onInput=${e => setName(e.target.value)}
              onKeyDown=${e => { if (e.key === "Enter") commit(null, name.trim()); }} />
            <button class="btn primary mini" disabled=${!name.trim()} onClick=${() => commit(null, name.trim())}>Create</button>
          </div>
        </div>`}
    </div>`;
}

/** AI reconnect-message modal — self-contained: generates on open, editable,
 * Regenerate + Copy. Used from Today's rail, the person page, and Lists. */
export function DraftModal({ person, onClose }) {
  const [text, setText] = useState("");
  const [meta, setMeta] = useState(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  useEscape(onClose);

  const generate = async () => {
    setBusy(true); setError("");
    try {
      const d = await api(`/api/contact/${person.id}/draftmessage`, { method: "POST", body: JSON.stringify({}) });
      setText(d.message || "");
      setMeta(d.meta || null);
    } catch (e) {
      setError(e.message.includes("ANTHROPIC")
        ? "Drafts need an API key. Open Settings."
        : e.message);
    }
    setBusy(false);
  };

  useEffect(() => { generate(); }, [person.id]);

  const copy = async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch { toast("Could not copy. Select the text and press ⌘C."); }
  };

  return html`
    <div class="overlay" onClick=${e => { if (e.target === e.currentTarget) onClose(); }}>
      <div class="modal" role="dialog" aria-modal="true"
           aria-label=${`Message for ${person.name}`}>
        <button class="close-x" onClick=${onClose} aria-label="Close"><${Icon} name="close" /></button>
        <h2>Message for ${person.name.split(" ")[0]}</h2>
        ${busy && html`<p class="r-meta">Writing the message…</p>`}
        ${error && html`<p style="color:var(--red)">${error}</p>`}
        ${!busy && !error && html`
          <textarea class="input" rows="5" aria-label="Draft message"
                    value=${text} onInput=${e => setText(e.target.value)} />`}
        ${!busy && !error && html`<${Receipt} meta=${meta} />`}
        <div class="modal-foot">
          <button class="btn quiet" disabled=${busy} onClick=${generate}><${Icon} name="refresh" /> Write another</button>
          <button class="btn primary" disabled=${busy || !text.trim()} onClick=${copy}>Copy</button>
        </div>
      </div>
    </div>`;
}
