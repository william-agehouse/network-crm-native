// The shared parts of the rebuilt Data and Settings pages (NC-088, NC-114).
//
// The old pages used many different container shapes, verbs and ways to reveal
// more. This file is what they come down to, so both pages are learned once:
//
//   Section     a titled part of a page: Needs you, the ledger, This install
//   LedgerRow   one named thing, one line, opened by clicking it — the ONE way
//               to reveal more on either page
//   RowDetail   what an open row shows: five blocks, always in the same order
//   NeedsRow    one thing the app cannot do without you, with the fix
//   StatusRow   one read-only figure about the whole install
//
// and the leaf controls every row uses: CostChip, ToneDot, Figure (a number
// that says which people it counts), CredentialBox (Connect, then Replace or
// Remove — a stored secret is never edited in place) and TextSetting.
//
// Nothing here decides anything. The rows, their tones and their sentences come
// from the server (crm/api_ledger.py for Data); these parts only draw them.
// view-ledger.css styles them, under the `lg-` prefix. Help keeps the old
// page's `src-help` and `src-steps` classes, which onboarding shares.
import { html, useState, useEffect, useRef, useCallback } from "../vendor/preact-htm.module.js";
import { api, toast } from "./api.js";
import { Md } from "./ui.js";
import { Icon } from "./icons.js";

/** Open one row and bring it into view. Focus moves to the row's own button, so
 *  a keyboard or a screen reader lands where the eye does (NC-219), and the
 *  scroll is instant for anyone who has asked their Mac for less motion. */
export function useOpenRow() {
  const [openKey, setOpenKey] = useState("");
  const bringIntoView = useCallback((key) => {
    requestAnimationFrame(() => {
      const row = document.getElementById(`lg-row-${key}`);
      if (!row) return;
      const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
      row.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
      row.querySelector(".lg-row-head")?.focus({ preventScroll: true });
    });
  }, []);
  const openRow = useCallback((key) => { setOpenKey(key); bringIntoView(key); }, [bringIntoView]);
  return [openKey, setOpenKey, openRow, bringIntoView];
}

/** Open the row a link from the other page names (#settings?row=ai), once
 *  the rows have arrived. Each press of the link carries its own number
 *  (main.js, ONE_SHOT_PARAMS), so pressing it again opens the row again, while
 *  closing it by hand stays closed. */
export function useRowFromLink(params, ready, openRow, settled = true, bringIntoView = null) {
  const link = params?.toString?.() || "";
  const want = params?.get?.("row") || "";
  const done = useRef("");
  const pending = useRef("");
  useEffect(() => {
    if (ready && want && done.current !== link) {
      done.current = link;
      openRow(want);
      pending.current = settled ? "" : want;
    }
  }, [ready, want, link, openRow]);
  // The Data page draws a quick answer first, then the one with the scan, which
  // can add needs above the row and push it down after the scroll. Once the
  // page has settled, bring the linked row back into view.
  useEffect(() => {
    if (settled && pending.current && bringIntoView) {
      const key = pending.current;
      pending.current = "";
      bringIntoView(key);
    }
  }, [settled, bringIntoView]);
}

/** A source's or a setting's own instructions, laid out as what they are: a procedure.
 *  Numbered lines become a real ordered list, one instruction per step;
 *  everything else stays prose. The text comes from importers.REGISTRY —
 *  this changes the layout, not a word of it. */
export function Help({ text }) {
  const blocks = String(text || "").split(/\n{2,}/);
  return html`
    <div class="src-help">
      ${blocks.map((block, i) => {
        const lines = block.split("\n").filter(l => l.trim());
        const steps = lines.length > 0 && lines.every(l => /^\s*\d+\.\s+/.test(l));
        return steps
          ? html`<ol class="src-steps" key=${i}>
              ${lines.map((l, j) => html`
                <li key=${j}><${Md} text=${l.replace(/^\s*\d+\.\s+/, "")} /></li>`)}
            </ol>`
          : html`<${Md} key=${i} text=${block} />`;
      })}
    </div>`;
}

/** A titled part of a page. `count` is shown beside the title when given. */
export function Section({ id, title, count = null, note = "", children }) {
  return html`
    <section class="lg-section" id=${id} aria-labelledby=${id && `${id}-title`}>
      <div class="lg-section-head">
        <h2 class="lg-section-title" id=${id && `${id}-title`}>${title}</h2>
        ${count !== null && html`<span class="lg-section-n">${count.toLocaleString()}</span>`}
      </div>
      ${note && html`<p class="lg-section-note">${note}</p>`}
      ${children}
    </section>`;
}

/** What switching something on takes from you, on one scale, in words. NC-230:
 *  the chip had a coloured dot from the staleness ramp, and on a fresh install
 *  four green "free" dots read as four sources working. Colour on these pages
 *  now means only a row's state. */
export function CostChip({ cost }) {
  if (!cost) return null;
  return html`<span class="lg-cost">${cost.label}</span>`;
}

/** The state of one row: ok, off, behind, blocked, paused, or not in this copy. */
const TONE_SAYS = {
  ok: "Working", off: "Off", behind: "Falling behind", blocked: "Stopped",
  paused: "Paused", na: "Not in this copy", ready: "Waiting for you",
};
export function ToneDot({ tone }) {
  return html`<span class=${`lg-dot is-${tone}`} role="img"
    aria-label=${TONE_SAYS[tone] || tone} title=${TONE_SAYS[tone] || tone}></span>`;
}

/** One line per thing the page keeps a row for. The whole line is one button;
 *  clicking it opens the row's detail beneath it. Columns fold, in a fixed
 *  order, as the window narrows (view-ledger.css): "gives" goes under the
 *  name below 1400px, "takes" under "have" below 1100px, and below 900px the
 *  row is one column. */
export function LedgerRow({ id, icon, name, gives, have, note = "", noteTone = "",
                            takes, cost, tone, open, onToggle, children, when = "" }) {
  // NC-233: a row in use says when it last ran where the others say what they
  // take, and has no effort chip: that effort is spent.
  return html`
    <div class=${"lg-row" + (open ? " is-open" : "")} id=${id} role="listitem">
      ${/* A short spoken name (NC-219): the whole line read aloud is about 40
           words, eighteen times over. */ ""}
      <button class="lg-row-head" type="button" aria-expanded=${open ? "true" : "false"}
        aria-controls=${`${id}-body`}
        aria-label=${[name, TONE_SAYS[tone] || tone, have].filter(Boolean).join(", ")}
        onClick=${onToggle}>
        <span class="lg-lead">
          <${ToneDot} tone=${tone} />
          <${Icon} name=${icon || "dot"} className="ic lg-ic" />
        </span>
        <span class="lg-name">
          <span class="lg-name-text">${name}</span>
          <span class="lg-gives lg-gives-folded">${gives}</span>
        </span>
        <span class="lg-gives lg-gives-col">${gives}</span>
        <span class="lg-have">
          ${/* "Nothing yet" only when the row has nothing else to say here: above
               a status line such as "Not set" it only repeats it. */ ""}
          ${(have || !note) && html`<span class=${have ? "lg-have-text" : "lg-have-text is-empty"}>
            ${have || "Nothing yet"}</span>`}
          ${note && html`<span class=${`lg-note is-${noteTone}`}>${note}</span>`}
          <span class="lg-takes lg-takes-folded">${when ? when : html`<span class="lg-fold-label">Takes</span>${" "}${takes}`}</span>
        </span>
        <span class=${"lg-takes lg-takes-col" + (when ? " is-when" : "")}>${when || takes}</span>
        <span class="lg-cost-cell">${!when && html`<${CostChip} cost=${cost} />`}</span>
        <${Icon} name=${open ? "chevronUp" : "chevronDown"} className="ic lg-chev" />
      </button>
      ${open && html`<div class="lg-row-body" id=${`${id}-body`}>${children}</div>`}
    </div>`;
}

/** The five blocks of an open row, always in this order and always under these
 *  names, so a person who has opened one row knows where to look in every other.
 *  A block with nothing to say is left out; the order of the rest never changes. */
export const DETAIL_BLOCKS = [
  ["now", "Now"],
  ["act", "What you can do"],
  ["how", "How"],
  ["other", "The other way in"],
  ["record", "What it added"],
];
export function RowDetail(blocks) {
  return html`
    <div class="lg-detail">
      ${DETAIL_BLOCKS.filter(([k]) => blocks[k]).map(([k, title]) => html`
        <div class=${`lg-block lg-block-${k}`} key=${k}>
          <h3 class="lg-block-title">${title}</h3>
          <div class="lg-block-body">${blocks[k]}</div>
        </div>`)}
    </div>`;
}

/** One thing the app cannot do without you. The sentence says what and why;
 *  the controls beside it are the fix, cheapest first. */
export function NeedsRow({ tone, says, children }) {
  return html`
    <div class="lg-need">
      <${ToneDot} tone=${tone} />
      <p class="lg-need-says">${says}</p>
      <div class="lg-need-acts">${children}</div>
    </div>`;
}

/** One read-only figure about the whole install, with what it is made of. */
export function StatusRow({ label, value, detail = null }) {
  return html`
    <div class="lg-status">
      <span class="lg-status-label">${label}</span>
      <span class="lg-status-value">${value}</span>
      ${detail && html`<span class="lg-status-detail">${detail}</span>`}
    </div>`;
}

// The words for a unit, one and many — crm/figures.py UNITS, for the units the
// two pages print.
const UNIT = {
  people: ["person", "people"], messages: ["message", "messages"],
  photos: ["photo", "photos"], meetings: ["meeting", "meetings"],
  sources: ["source", "sources"], profiles: ["profile", "profiles"],
};
/** A number that says what it counts. Only takes a figure object from the
 *  server (crm/figures.py figure()), never a bare number: the whole point is
 *  that nothing on these pages can print a bare number without saying what it
 *  counts.
 *  Hovering it names the group of people it counts. */
export function Figure({ fig, words = "", whole = true }) {
  if (!fig || typeof fig.n !== "number" || !fig.population) {
    console.warn("Figure without a population", fig);   // eslint-disable-line no-console
    return null;
  }
  const [one, many] = UNIT[fig.unit] || [fig.unit, fig.unit];
  const n = fig.n.toLocaleString();
  const unit = fig.n === 1 ? one : many;
  // `whole={false}` when the line beside it already says what the rest are,
  // so "1 of 4 people" does not repeat "3 more you hid or deleted".
  const lead = whole && typeof fig.of === "number"
    ? `${n} of ${fig.of.toLocaleString()} ${fig.of === 1 ? one : many}` : `${n} ${unit}`;
  const tail = words || fig.says || "";
  return html`<span class="lg-figure" title=${`Counting ${fig.population_says}`}>
    <b>${lead}</b>${tail ? ` ${tail}` : ""}</span>`;
}

// NC-327. Said beside every box that takes a key from Anthropic (Settings and
// first-run setup), because each person pays their own bill. The spend cap on
// the same row (NC-364) limits one paid run; nothing in the app caps a month of
// questions, which only Anthropic's own spending limit can.
export const ANTHROPIC_SPEND_HINT = "Anthropic bills your own account for every "
  + "question, draft and summary. To put a ceiling on that, set a monthly spending "
  + "limit on Anthropic's site, in your account's Settings, under Limits.";

// NC-150: a save that could not lock the file down says so, never a bare tick.
// The caller shows the warning in place of its own success message: a toast
// replaces the one before it, so a warning shown here would be wiped by the
// caller's "saved" a moment later (NC-236).
async function saveSetting(key, value) {
  return api("/api/settings", { method: "POST", body: JSON.stringify({ key, value }) });
}

/** Why a pasted secret is refused before it is sent, or "". Pasting is where
 *  these go wrong: a line break from a copied email, the masked placeholder
 *  pasted back, or a whole private key where a token was asked for. */
// What a value must look like before it is saved (NC-218). The old Settings
// page refused these three mistakes; without the checks a mis-pasted key is
// saved, the row says "Connected" in green, and it fails the next time Ask runs.
// The server checks none of them, so the box is the one place.
// NC-303: a calendar row takes several addresses, saved with one comma between.
const CALENDAR_ADDRESS = v => (v.split(",").every(a => /^(https|webcals?):\/\/\S+$/i.test(a)) ? ""
  : "Start each address with https:// or webcal://. Copy it again from the calendar's settings.");
const CALENDAR_KEYS = new Set(["NC_ICS_GOOGLE_URL", "NC_ICS_ICLOUD_URL"]);
/** What is saved for a pasted value. On a calendar row, whatever separates two
 *  addresses as pasted (a comma, a space, a line break) becomes one comma, so
 *  "first, second" is not refused for its space (NC-303). */
export const pastedValue = (key, value) => {
  const v = String(value || "").trim();
  return CALENDAR_KEYS.has(key) ? v.split(/[\s,;]+/).filter(Boolean).join(",") : v;
};
export const FORMAT = {
  // NC-328: one address, or several separated by commas, so mail from and to
  // your other address reads as yours. Each one must be whole.
  SELF_EMAIL: v => (v.split(/[,;\s]+/).filter(Boolean).every(a => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(a)) ? ""
    : "Type whole email addresses, like you@example.com. Put a comma between two."),
  ANTHROPIC_API_KEY: v => (v.startsWith("sk-ant-") ? ""
    : "Keys from Anthropic start with sk-ant-. Copy the whole key again."),
  NC_ICS_GOOGLE_URL: CALENDAR_ADDRESS,
  NC_ICS_ICLOUD_URL: CALENDAR_ADDRESS,
  NC_DATES_LOOKAHEAD_DAYS: v => (/^\d+$/.test(v) && +v >= 1 && +v <= 365 ? ""
    : "Type a whole number of days, from 1 to 365."),
};

export function secretProblem(value, masked = "", key = "") {
  const raw = String(value || "");
  if (!raw.trim()) return "Paste it first.";
  if (/-----BEGIN/.test(raw)) return "That is a private key. Paste the token itself, never a key file.";
  const v = pastedValue(key, raw);
  if (/\s/.test(v)) return "That has a space or a line break in it. Copy the value again, on its own.";
  if (masked && v === masked) return "That is the hidden copy shown here. Paste the real value.";
  return FORMAT[key] ? FORMAT[key](v) : "";
}

/** A secret the app keeps for one source. Connect first; once one is stored it
 *  is shown by its last characters only, and the only things you can do are
 *  Replace it or Remove it. A stored secret is never edited in place, because
 *  "Save" over a masked value would lie about what the box holds. */
export function CredentialBox({ act, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const showBox = !act.set || editing;

  const connect = async () => {
    const problem = secretProblem(value, act.masked, act.key);
    if (problem) { setNote(problem); return; }
    setBusy(true);
    try {
      const d = await saveSetting(act.key, pastedValue(act.key, value));
      toast(d.warning || (act.set ? `${act.label} replaced` : `${act.label} connected`));
      setValue(""); setEditing(false); setNote("");
      onSaved?.();
    } catch (e) { setNote(e.message); }
    setBusy(false);
  };
  const remove = async () => {
    if (!confirmRemove) { setConfirmRemove(true); return; }
    setBusy(true);
    try {
      const d = await saveSetting(act.key, "");
      toast(d.warning || `${act.label} removed`);
      setConfirmRemove(false);
      onSaved?.();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  if (!showBox) {
    return html`
      <div class="lg-cred">
        <span class="lg-cred-label">${act.label}</span>
        <code class="lg-cred-masked" aria-label=${`${act.label}, stored, ending ${act.masked}`}>${act.masked}</code>
        <button class="btn" disabled=${busy} onClick=${() => { setEditing(true); setConfirmRemove(false); }}>Replace</button>
        <button class=${"btn" + (confirmRemove ? " danger" : "")} disabled=${busy} onClick=${remove}>
          ${confirmRemove ? "Remove it for good" : "Remove"}</button>
        ${confirmRemove && html`<button class="btn ghost" onClick=${() => setConfirmRemove(false)}>Keep it</button>`}
      </div>`;
  }
  return html`
    <div class="lg-cred">
      <label class="lg-cred-label" for=${`cred-${act.key}`}>${act.label}</label>
      <input class="lg-cred-input" id=${`cred-${act.key}`} type="password"
        autocomplete="off" spellcheck="false" autocapitalize="off"
        placeholder=${act.placeholder || "Paste it here"} value=${value}
        onInput=${e => { setValue(e.target.value); setNote(""); }}
        onKeyDown=${e => {
          if (e.key === "Enter") connect();
          if (e.key === "Escape") { setValue(""); setNote(""); }
        }} />
      <button class="btn primary" disabled=${busy} onClick=${connect}>
        ${busy ? "Connecting…" : (act.set ? "Replace" : "Connect")}</button>
      ${editing && html`<button class="btn ghost" onClick=${() => { setEditing(false); setValue(""); setNote(""); }}>Cancel</button>`}
      ${note && html`<div class="field-note lg-cred-note" role="status">${note}</div>`}
    </div>`;
}

/** Why a plain setting is refused before it is sent, or "". Only one rule so
 *  far (NC-119): the scraper's key setting wants WHERE the key file is, and a
 *  pasted key must never be written into the settings file instead. */
export function textProblem(key, value) {
  if (key === "KEY" && /-----BEGIN/.test(String(value || ""))) {
    return "That is the key itself. Paste where the key file is instead, like ~/.ssh/id_ed25519.";
  }
  // An empty box is Clear, which is always allowed.
  const v = String(value || "").trim();
  return v && FORMAT[key] ? FORMAT[key](v) : "";
}

/** A setting that is not a secret, so it is shown back in full. */
export function TextSetting({ act, onSaved }) {
  const [value, setValue] = useState(act.value || "");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const changed = value.trim() !== (act.value || "");
  const save = async () => {
    const problem = textProblem(act.key, value);
    if (problem) { setNote(problem); return; }
    setBusy(true);
    try {
      const d = await saveSetting(act.key, value.trim());
      // `name` when the label is a sentence, so the toast is not one.
      toast(d.warning || (value.trim() ? `${act.name || act.label} saved` : `${act.name || act.label} cleared`));
      setNote("");
      onSaved?.();
    } catch (e) { setNote(e.message); }
    setBusy(false);
  };
  return html`
    <div class="lg-cred">
      <label class="lg-cred-label" for=${`set-${act.key}`}>${act.label}</label>
      <input class="lg-cred-input" id=${`set-${act.key}`} type="text"
        autocomplete="off" spellcheck="false" autocapitalize="off"
        placeholder=${act.placeholder || ""} value=${value}
        onInput=${e => { setValue(e.target.value); setNote(""); }}
        onKeyDown=${e => {
          if (e.key === "Enter" && changed) save();
          if (e.key === "Escape") { setValue(act.value || ""); setNote(""); }
        }} />
      <button class="btn" disabled=${busy || !changed} onClick=${save}>
        ${value.trim() || !act.value ? "Save" : "Clear"}</button>
      ${/* NC-218. Name and email each have their own Save, so a name typed and
           left while you saved the email looked saved and was not. */ ""}
      ${(note || changed) && html`<div class="field-note lg-cred-note" role="status">
        ${note || "Not saved yet. Press Save or Return."}</div>`}
    </div>`;
}

/** An on/off setting, as two buttons that say what each one does. Saved the
 *  moment you choose, so it has no Save button. The old Settings page's
 *  Toggle, in the shared grammar. */
export function Switch({ act, onSaved }) {
  const [busy, setBusy] = useState(false);
  const set = async (on) => {
    if (on === act.on) return;
    setBusy(true);
    try {
      const d = await saveSetting(act.key, on ? "1" : "0");
      toast(d.warning || `${act.label}: ${on ? "on" : "off"}`);
      onSaved?.();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };
  return html`
    <div class="lg-switch" role="group" aria-label=${act.label}>
      <button class=${"btn" + (act.on ? " primary" : "")} aria-pressed=${act.on ? "true" : "false"}
        disabled=${busy} onClick=${() => set(true)}>On</button>
      <button class=${"btn" + (!act.on ? " primary" : "")} aria-pressed=${!act.on ? "true" : "false"}
        disabled=${busy} onClick=${() => set(false)}>Off</button>
    </div>`;
}
