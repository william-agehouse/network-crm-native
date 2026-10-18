// Ask — chats over your own network. The transcript fills the surface, the
// composer docks at the bottom, and each answer carries a provenance line above
// it and the contact rows it is about below it. Restored history keeps its cards
// but not its provenance, so those turns render without one.
//
// Ask holds MANY threads now, not one endless transcript: #ask is the newest
// chat, #ask/7 is chat 7, and the index of them is a rail that collapses (see
// the Chats section below for the layout decision and why "Clear conversation"
// is gone). Every id-taking call goes through the chat it names, so a question
// can never land in whichever thread happens to be newest.
//
// ─── MICROCOPY STANDARD — ASD-STE100, for every surface in this app ───────────
// docs/DESIGN_DIRECTION.md §3 sets Simplified Technical English (the aerospace
// maintenance-manual standard) as the app's voice. These are the rules applied
// in this file, each with the string that forced it. The other views should
// follow the same list, so the app speaks once and speaks the same way.
//
//  R1  One meaning per word. "intros" → "introductions". "Ask" is the NAME of
//      this surface and never also a verb, so copy reads "Send the question
//      again", never "Ask again".
//  R2  Active voice, and name the actor. "Ask cannot reach the Anthropic API",
//      not "the Anthropic API could not be reached".
//  R3  One instruction per sentence, and put the instruction on the control.
//      The sentence states the fact; the button beside it says "Open Settings".
//  R4  Short sentences, one clause each. No em-dash welding two thoughts:
//      "That question needed too many steps — try asking something narrower"
//      becomes two sentences.
//  R5  No dropped words. "The list below is incomplete", not "list incomplete".
//  R6  Delete filler. When the data answers the question, write nothing:
//      "Here's who I found:" above a list of people is removed, not restyled.
//  R7  No contractions and no idioms. "cannot", not "can't". "contact", not
//      "catch up with". "No contact matches", not "came up empty".
//  R8  Never show a person an internal error. Name the cause in plain words,
//      give the one action that repairs it, and keep the raw text behind a
//      "Show detail" toggle for whoever has to debug it.
//  R9  State a number once. The count lives in "3 selected", so the button
//      beside it reads "Add to list" and counts nothing. (DESIGN_DIRECTION §4:
//      two components each computing their own count is a defect.)
//  R10 No terminal period on a button, a label, or a placeholder. Full stops
//      belong in sentences.
//  R11 Do not state one state twice. The pending line animates, so it reads
//      "Searching your network" with no trailing ellipsis.
import { html, useState, useEffect, useRef, useCallback } from "../vendor/preact-htm.module.js";
import { api, toast, roleStr, relDate, oneShot } from "./api.js";
import { Avatar, Tags, Md, AddToList, Empty, SkelRows } from "./ui.js";
import { Icon } from "./icons.js";

// Intent chips — prefill the composer, never auto-send. Exported because these
// chips only render in the empty state, so ⌘K generates a command per intent from
// this same list (see COMMANDS in main.js) — otherwise they're unreachable once
// you've asked anything at all.
// Labels are whole words (R1) and the prefills carry no idioms (R7).
export const INTENTS = [
  { label: "Find introductions", prefill: "Who could introduce me to " },
  { label: "Overdue contacts", prefill: "Who should I contact this month?" },
  { label: "Prepare a meeting", prefill: "What do I know about " },
  { label: "Find expertise", prefill: "Who in my network knows about " },
];

function greeting(name) {
  const h = new Date().getHours();
  const part = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
  // first name from the SELF_NAME setting when set; plain greeting otherwise
  const first = (name || "").trim().split(/\s+/)[0];
  return first ? `Good ${part}, ${first}` : `Good ${part}`;
}

// ── Server copy → app copy ────────────────────────────────────────────────────
// Some of Ask's words are still written in Python (crm/api_ask.py and
// crm/chat.py), so the canned ones are rewritten on the way to the screen.
//
// Rewriting at RENDER time rather than on receipt is deliberate: the raw text is
// what got persisted into chat history, so an older turn restored from history
// gets the new wording too. Delete this section once the backend owns the copy —
// the strings it replaces are listed beside each pattern so the move is a
// mechanical one.
/* NC-080. Ask's own timeout, because the app-wide 20s default was below this
 * feature's normal working range — not near the edge of it, below it.
 *
 * A long answer can take well over 20 seconds. A client that gives up at 20
 * aborts questions while the server is still working, and the server then hits
 * BrokenPipeError writing an answer nobody is listening for. The limit would not
 * be long enough for an answer that is still being worked out.
 *
 * This is one endpoint that runs a multi-round LLM tool-use loop; every other
 * api() call is a database read that genuinely should give up in 20 seconds, so
 * the default stays where it is and this one call overrides it. intake.js
 * does the same thing for its model calls (120s).
 *
 * Three minutes is deliberately well past the slowest plausible answer rather
 * than just past the fastest failure. A ceiling that has to be raised again the
 * first time a thorough question hits twelve rounds is the same bug with a
 * bigger number, and the cost of waiting too long is one impatient user, while
 * the cost of waiting too little is a billed answer thrown away.
 */
const ASK_TIMEOUT_MS = 180000;

/* Tells the client-side abort apart from a server-side fault. api() throws
   "<path> did not answer within Ns" for the former, and that string is api.js's
   to own — matched loosely on purpose so a reworded suffix does not silently
   reclassify every timeout as a fault. */
const TIMED_OUT_RE = /did not answer within/i;

/* NC-347. What a request that failed outright says. It used to be the sentence for
   a fallback ("used keyword search instead"), but a failed request has no keyword
   answer under it: nothing came back at all. So it says that it failed, what to
   try, and where the reason is. The cause is not named in advance, because the same
   notice covers a busy database and a dead connection, and they want different
   things; the details line under the notice carries the real message. */
const ASK_FAILED = "Ask could not answer this question, and nothing came back. Try it again in a moment. If it fails again, open the details to see why.";

const SERVER_COPY = [
  // "Here's who I found:" / "Here's who I found in London around tech:" — R6.
  // The rows underneath are the answer, so this line becomes nothing at all.
  [/^\s*here.?s who i found[^\n]*/i, ""],
  // Two files apologise for the same thing in two different voices (R2, R7).
  [/^\s*i couldn.?t find anyone matching that[^\n]*/i,
    "No contact matches that question. Use other words."],
  [/^\s*i didn.?t find anything for that\.?/i,
    "No contact matches that question. Use other words."],
  // Out of tool rounds. Was one sentence with an em-dash and vague advice (R4).
  [/^\s*that question needed too many steps[^\n]*/i,
    "This question needs too many steps. Split it into two questions."],
];

// handle_chat appends the raw Python exception to the reply when the LLM path
// fails and the rule-based engine answers instead. Left alone, a person would
// read "(AI chat was unavailable: No module named 'anthropic')" on screen.
const AI_DOWN_RE = /\s*\(AI chat was unavailable:\s*([\s\S]*?)\)\s*$/i;

/** Name the cause of a failed LLM call, and the one action that repairs it (R8).
 *
 *  The branch that produces these only runs when ANTHROPIC_API_KEY IS set, so
 *  "Ask needs an API key" would be wrong for the common case: the key is there
 *  and the `anthropic` package is not. A missing package is a dependency-install
 *  failure, which Settings already reports through deps_status with its own retry
 *  hint — so Settings is the right destination for both, but the sentence above
 *  the button has to say which one went wrong. */
function aiDownNotice(detail) {
  const d = String(detail || "").trim();
  if (/no module named|modulenotfound|import error|importerror/i.test(d)) {
    return {
      text: "The anthropic package is not installed. Ask used keyword search for this answer.",
      action: "Open Settings", detail: d,
    };
  }
  // Before the key branch, because an empty account and a bad key arrive as the
  // same 400 shape and the wrong one of the two sends you somewhere useless.
  // `credit balance` must not sit inside the key alternation below: Ask would
  // then say the key was rejected when the key is perfectly good, and offer
  // Settings, where there is nothing to fix. Billing lives on Anthropic's own
  // site, so this notice carries no action button: an action that cannot repair
  // the fault is worse than no action at all.
  if (/credit balance|\b402\b|billing|purchase credits|quota/i.test(d)) {
    return {
      text: "The Anthropic account is out of credit. Ask used keyword search for this answer. Top up at console.anthropic.com and ask again.",
      detail: d,
    };
  }
  if (/authentication|invalid.{0,3}api.{0,3}key|x-api-key|unauthorized|\b401\b|\b403\b/i.test(d)) {
    return {
      text: "The Anthropic API rejected the key. Ask used keyword search for this answer.",
      action: "Open Settings", detail: d,
    };
  }
  if (/rate.?limit|\b429\b|overloaded|\b529\b/i.test(d)) {
    return {
      text: "The Anthropic API is busy. Ask used keyword search for this answer. Send the question again in one minute.",
      detail: d,
    };
  }
  return {
    text: "Ask cannot reach the Anthropic API. Ask used keyword search for this answer.",
    detail: d,
  };
}

/** Split one stored reply into the text a person should read and the notice, if
 *  any, that explains how the answer was produced. Pure — safe to call on every
 *  render of every turn. */
function normalizeReply(raw) {
  let text = String(raw || "");
  let note = null;
  const down = text.match(AI_DOWN_RE);
  if (down) {
    note = aiDownNotice(down[1]);
    text = text.replace(AI_DOWN_RE, "");
  }
  for (const [re, to] of SERVER_COPY) text = text.replace(re, to);
  return { text: text.trim(), note };
}

/** One diagnosis, one action, and the raw cause on request (R3, R8).
 *
 *  Every degradation and failure in Ask renders through here, so the voice
 *  cannot drift per call site and a Python exception can never reach the surface
 *  unlabelled. `tone` is "warn" for a degraded answer and "bad" for no answer. */
function Notice({ tone = "warn", text, action, onAction, detail }) {
  const [open, setOpen] = useState(false);
  return html`
    <div class=${"ask-notice " + tone} role="status">
      <div class="ask-notice-row">
        <span class="ask-notice-text">${text}</span>
        ${action && html`
          <button class="btn quiet mini" onClick=${onAction}>${action}</button>`}
        ${detail && html`
          <button class="ask-notice-more" aria-expanded=${open}
            onClick=${() => setOpen(o => !o)}>${open ? "Hide detail" : "Show detail"}</button>`}
      </div>
      ${open && detail && html`<code class="ask-notice-detail">${detail}</code>`}
    </div>`;
}

// How many rows paint before the expander. An answer for "who works in
// logistics" can carry sixty people, and the prose is the part you actually
// read — so the rows stay a preview until you ask for the rest, and the expanded
// set scrolls inside its own box rather than pushing the answer off the screen.
const CARDS_SHOWN = 8;

/** The contact rows under one answer, and the bar for picking people off them.
 *
 *  Selection is per assistant turn and deliberately not persisted: it's a
 *  scratch gesture on the way to a list, and a tick that survived a reload would
 *  outlive the question that justified it. Held as a Set of contact ids rather
 *  than indices so the expander can't renumber it.
 *
 *  Rows are the shared .row primitive from ui.css at --row-h, not cards: a
 *  roster answer IS the list, and forty-pixel rows fit ten people where the old
 *  cards fit four. */
function ContactCards({ contacts, go }) {
  const [sel, setSel] = useState(() => new Set());
  const [expanded, setExpanded] = useState(false);
  const anchor = useRef(null);  // last row ticked — shift-click extends from here
  if (!contacts || !contacts.length) return null;

  const total = contacts.length;
  const capped = total > CARDS_SHOWN;
  const shown = expanded ? contacts : contacts.slice(0, CARDS_SHOWN);
  const n = sel.size;
  const clear = () => { setSel(new Set()); anchor.current = null; };

  const toggle = (i, e) => {
    const next = new Set(sel);
    if (e.shiftKey && anchor.current !== null && anchor.current !== i) {
      // A range only ever adds. Inverting it would let one stray shift-click
      // silently un-pick the twenty people you'd just worked through.
      const [a, b] = anchor.current < i ? [anchor.current, i] : [i, anchor.current];
      for (let k = a; k <= b; k++) next.add(contacts[k].id);
    } else if (next.has(contacts[i].id)) {
      next.delete(contacts[i].id);
    } else {
      next.add(contacts[i].id);
    }
    anchor.current = i;
    setSel(next);
  };

  // Select all covers the whole turn, hidden rows included — that's the point
  // of it when the answer named fifty people. Expanding as well keeps it honest:
  // you can see everything the count is now claiming.
  const selectAll = () => {
    setSel(new Set(contacts.map(c => c.id)));
    if (capped) setExpanded(true);
  };

  // Row order, not click order — the list you get out should read like the
  // answer you picked it from.
  const selIds = contacts.filter(c => sel.has(c.id)).map(c => c.id);

  return html`
    <div class="ask-results">
      ${/* Capped in BOTH states, not just expanded: eight rows is already tall
             enough to push the prose you were reading off the top. */ ""}
      <div class=${"rows ask-result-list" + (shown.length > 4 ? " capped" : "")}>
        ${shown.map((c, i) => {
          const on = sel.has(c.id);
          const why = roleStr(c) || c.reason || "";
          const tier = c.strength_label || "";
          return html`
            <div key=${c.id} class=${"row ask-result" + (on ? " sel" : "")}>
              <button class="ask-tick" role="checkbox" aria-checked=${on}
                aria-label=${on ? `Deselect ${c.name}` : `Select ${c.name}`}
                title=${on ? `Deselect ${c.name}` : `Select ${c.name}. Shift-click to extend.`}
                onClick=${e => toggle(i, e)}><${Icon} name="check" /></button>
              <button class="ask-open" title=${`Open ${c.name}`}
                onClick=${() => go(`people/${c.id}`)}>
                <${Avatar} c=${c} size=${24} ring=${!!c.strength} />
                <span class="row-title">${c.name}</span>
                ${/* Never blank (DESIGN_DIRECTION §2): an empty
                       cell hides the neglect this app exists to show. */ ""}
                <span class="row-meta">${why || "No role recorded"}</span>
                <span class="ask-r-tags"><${Tags} c=${c} max=${1} /></span>
                ${tier && html`
                  <span class="row-side ask-r-strength">
                    <i class=${"ask-r-dot t-" + tier} aria-hidden="true"></i>${tier}
                  </span>`}
              </button>
            </div>`;
        })}
      </div>
      ${/* A one-row answer gets no bar until you tick it — nothing to select all
             of, nothing to expand, and an empty row still costs a gap. */ ""}
      ${(total > 1 || n > 0) && html`
      <div class="ask-select-bar">
        ${/* R9: the count has exactly one owner, and it is this span. */ ""}
        ${n > 0 && html`<span class="ask-select-count">${n} selected</span>`}
        ${total > 1 && n < total && html`
          <button class="btn quiet mini" onClick=${selectAll}>Select all</button>`}
        ${n > 0 && html`
          <button class="btn quiet mini" aria-label="Clear selection" onClick=${clear}>Clear</button>`}
        ${/* dropUp: the bar sits under the rows, near the bottom of .ask-scroll's
               overflow box — a downward popover gets clipped by it. */ ""}
        ${n > 0 && html`
          <${AddToList} contactIds=${selIds} onDone=${clear} alignLeft=${true} dropUp=${true}
            btnClass="btn primary mini" trigger="Add to list" />`}
        ${capped && html`
          <button class="btn quiet mini" onClick=${() => setExpanded(x => !x)}>
            ${expanded ? "Show fewer" : `Show all ${total}`}
          </button>`}
      </div>`}
    </div>`;
}

// ── Composer height ───────────────────────────────────────────────────────────
// NC-153. A textarea has one fixed height and no idea what is in it, so this
// measures the content and resizes to fit. Reset to "auto" first: scrollHeight
// never shrinks below the height already set, so growing works without it and
// DELETING a line would leave the box stuck at its high-water mark.
const ASK_ROWS_MAX = 8;

function autoGrow(el) {
  if (!el) return;
  el.style.height = "auto";
  // Line height is read rather than assumed, because it comes from tokens.css
  // and a change there must not silently change the cap in rows.
  const line = parseFloat(getComputedStyle(el).lineHeight) || 20;
  const pad = el.offsetHeight - el.clientHeight + 12;   // borders + the CSS padding
  el.style.height = Math.min(el.scrollHeight, line * ASK_ROWS_MAX + pad) + "px";
}

// ── A question the answer asked back ─────────────────────────────────────────
//
// NC-156. The turn ends with a question and two to four buttons instead of an
// answer. Clicking one sends it as the next question, which is why they are
// buttons and not a form: the whole point is that answering costs one click.
//
// WHY IT KNOWS NOTHING ABOUT BEING ANSWERED. There is no "answered" flag
// anywhere — not in the database, not in this component. A question with a turn
// after it in the transcript has been answered, and the turn after it IS the
// answer. Storing that as well would be a second copy of the same fact, free to
// disagree with the first. So `answered` is computed by the caller from the
// array it is already mapping over, and `picked` is simply the next turn's text.
//
// An answered question keeps its options on screen, inert, with the chosen one
// marked. Removing them would leave "Suppliers or clients?" above a bare
// "Suppliers" with nothing saying those were the choices.
function AskOptions({ q, answered, picked, onPick, busy }) {
  const opts = (q && q.options) || [];
  const multi = !!(q && q.multi);
  const [sel, setSel] = useState(() => new Set());
  if (!opts.length) return null;
  if (answered) {
    // NC-162. WHOLE answers, not substrings. Asking whether the reply CONTAINS an
    // option's text would, when "Suppliers" and "Suppliers who ship overseas" are
    // offered and the long one is picked, mark both — and the person reading it
    // back cannot tell which they chose, which is the only job this state has.
    // The multi-select case joins its picks with ", ", so the reply is split on
    // commas and each piece compared whole.
    const norm = (t) => (t || "").trim().toLowerCase();
    const whole = norm(picked);
    const parts = new Set(whole.split(",").map(norm));
    const chose = (o) => whole === norm(o) || parts.has(norm(o));
    return html`
      <div class="ask-opts done" role="list" aria-label="The options offered">
        ${opts.map((o, i) => html`
          <span key=${i} role="listitem"
            class=${"chip mini" + (chose(o) ? " active" : "")}>${o}</span>`)}
      </div>`;
  }
  // Multi-select is the MODEL's call, not this component's: only it knows
  // whether "suppliers" and "clients" are alternatives or a pair worth asking
  // about together. One click sends in the single case, because making someone
  // click an option and then a Send button to answer a yes/no is the kind of
  // ceremony that stops people answering at all.
  const toggle = (o) => setSel(prev => {
    const next = new Set(prev);
    if (next.has(o)) next.delete(o); else next.add(o);
    return next;
  });
  return html`
    <div class="ask-opts">
      ${opts.map((o, i) => html`
        <button key=${i} class=${"chip" + (multi && sel.has(o) ? " active" : "")}
          disabled=${busy} aria-pressed=${multi ? sel.has(o) : undefined}
          onClick=${() => (multi ? toggle(o) : onPick(o))}>${o}</button>`)}
      ${multi && html`
        <button class="btn primary mini" disabled=${busy || sel.size === 0}
          onClick=${() => onPick(opts.filter(o => sel.has(o)).join(", "))}>
          Send</button>`}
    </div>`;
}

// Provenance line above a live answer — "Searched N contacts · M conversations ·
// K tool calls ›", expandable to the tool-call trail. History turns have neither
// searched nor trail, so this renders nothing for them.
function Provenance({ searched, trail, spend }) {
  const [open, setOpen] = useState(false);
  const n = (trail || []).length;
  // NC-155. This used to read `if (!searched) return null`, and a turn restored
  // from history had no `searched` — so the receipt existed for the few seconds
  // after an answer landed and was gone on the next reload, which is how you see
  // a chat almost every time you open one. The three fields are persisted now
  // (db.receipt_json), so the test is "is there anything to show" rather than
  // "is this the live answer". A turn older than that column still shows
  // nothing, which is honest: the numbers were never written down.
  if (!searched && !spend && !n) return null;
  // What this answer cost. Shown because it was invisible: a roster question can
  // run a dozen rounds, each re-sending every tool result, and nothing anywhere
  // told you the price. Cache reads are called out separately — they bill at
  // about a tenth of input, so they're the difference between a cheap follow-up
  // and an expensive one.
  const cost = spend && spend.usd != null ? html`
    ${" · "}$${spend.usd < 0.01 ? spend.usd.toFixed(4) : spend.usd.toFixed(2)}` : null;
  // Messages only when there are any, so an install with no archive imported
  // does not read "· 0 messages" as though something were missing. This line is
  // the only place the app says what Ask can reach, and it named only the
  // conversation blocks while the message tools search the far larger set of
  // message rows.
  const counts = searched ? html`
    Searched ${(searched.contacts ?? 0).toLocaleString()} contacts${ ""}
    ${" · "}${(searched.conversations ?? 0).toLocaleString()} conversations${ ""}
    ${(searched.messages ?? 0) > 0
      ? html`${" · "}${searched.messages.toLocaleString()} messages` : null}`
    : html`This answer`;
  // Which engine answered. Only claimed when it is provable: no tool calls AND
  // no token spend means nothing reached Anthropic, so the rule-based engine
  // (the zero-key fallback) answered. That fallback must never be silent, so it
  // says so on the answer it produced.
  const keyword = n === 0 && !spend
    ? html`<span class="tag warn ask-mode">Keyword search</span>` : null;
  // no tool calls AND no price (the rule-based fallback) → plain text line, and
  // no dead expander over nothing. A turn WITH a price still opens, even with an
  // empty trail: the tokens, rounds and model are the part worth reading, and
  // they are exactly what NC-155 is about.
  if (n === 0 && !spend) {
    return html`<div class="prov"><div class="prov-line">${counts}${cost}${keyword}</div></div>`;
  }
  return html`
    <div class="prov">
      <button class="prov-line" onClick=${() => setOpen(o => !o)} aria-expanded=${open}>
        ${counts}
        ${n > 0 ? html`${" · "}${n} tool call${n === 1 ? "" : "s"}` : null}${cost}
        <span class=${"prov-chev" + (open ? " open" : "")}><${Icon} name="caretRight" /></span>
      </button>
      ${open && (trail || []).map((t, i) => html`
        ${/* One separator grammar for the whole app: "·", never a mixture of
               dashes and arrows. */ ""}
        ${/* `label` for a tool that reports rather than matches (message_stats);
               otherwise the count — and the TOTAL when the per-call cap hid
               some, which chat.py has always sent and this line used to throw
               away. "12 matched" for a search that matched 400 is a wrong
               number, not a short one. */ ""}
        <div key=${i} class="prov-row">${t.tool} · ${t.detail} · ${t.label
          || (t.total > t.matched
              ? `${t.matched} of ${t.total.toLocaleString()} matched`
              : `${t.matched} matched`)}</div>`)}
      ${open && spend && html`
        ${/* NC-155: the dollar figure again, to four decimals. The collapsed
               line rounds to cents so it reads at a glance, which turns a $0.004
               follow-up into "$0.00" — a number that looks like nothing happened
               rather than like the cheap answer it was. */ ""}
        <div class="prov-row">
          ${spend.usd != null ? html`$${spend.usd.toFixed(4)}${" · "}` : null}
          ${/* NC-195: the prices that figure assumed. A stale price was invisible —
               it lived in secrets/.env and nowhere else. Older saved answers carry
               no prices, so they show none rather than today's. */ ""}
          ${spend.price_in_per_mtok != null && spend.price_out_per_mtok != null ? html`
            priced at $${spend.price_in_per_mtok} per million tokens in, $${spend.price_out_per_mtok} out${" · "}` : null}
          ${spend.rounds} round${spend.rounds === 1 ? "" : "s"} · ${spend.model}
          ${" · "}${spend.input_tokens.toLocaleString()} tokens in${ ""}
          ${" · "}${spend.output_tokens.toLocaleString()} out${ ""}
          ${spend.cache_read_tokens > 0
            ? html`${" · "}${spend.cache_read_tokens.toLocaleString()} from cache`
            : ""}
        </div>`}
    </div>`;
}

// ── Answer shape ──────────────────────────────────────────────────────────────
// THREE CHIPS, EACH SHOWING ITS CURRENT VALUE. Laying every option out flat —
// "PEOPLE 10 25 All DETAIL Brief Full SEARCH Quick Thorough" — is twelve elements
// to say three things. That is noise.
//
// A chip that states where it is set, and opens the alternatives only when asked,
// is how the rest of this app already works: People's Sort and Filter are exactly
// this. So the primitives are the shared ones — .chip from ui.css and the
// .omni-pop / .omni-row menu — rather than a third pattern invented here.
//
// The values carry their own meaning, so the chip needs no separate label: "All
// people" reads without a "PEOPLE" caption in front of it, and "Brief" next to it
// does not need "DETAIL". That is what takes twelve elements down to three.
const SHAPE_KEY = "nc_ask_shape";
const SHAPE_FIELDS = [
  { key: "people", aria: "How many people the answer names",
    help: "How many people the answer names. Every match still appears below it.",
    opts: [["10", "10 people", "A short answer"],
           ["25", "25 people", "A longer answer"],
           ["all", "All people", "Name everyone found"]] },
  { key: "detail", aria: "How much the answer explains",
    help: "Brief writes less prose. It can stop early on a long list.",
    opts: [["brief", "Brief", "One or two sentences"],
           ["full", "Full", "A reason for each person"]] },
  { key: "search", aria: "How hard the search works",
    help: "Quick makes fewer searches. It can miss people.",
    opts: [["quick", "Quick", "Fewer searches. It can miss people"],
           ["thorough", "Thorough", "More searches. It takes longer"]] },
];
const SHAPE_DEFAULTS = { people: "all", detail: "full", search: "thorough" };

function loadShape() {
  // try/catch because Safari private mode throws on localStorage, the same reason
  // settings.js and today.js wrap theirs.
  try {
    const raw = JSON.parse(localStorage.getItem(SHAPE_KEY) || "{}");
    return { ...SHAPE_DEFAULTS, ...(raw && typeof raw === "object" ? raw : {}) };
  } catch { return { ...SHAPE_DEFAULTS }; }
}

/** One chip: the current value, and the alternatives on click. */
function ShapeChip({ field, value, onPick, off, why }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const away = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const esc = (e) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      // Escape must land the caret back on the chip, not on <body>.
      ref.current?.querySelector(".chip")?.focus();
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const current = field.opts.find(([v]) => v === value) || field.opts[0];
  return html`<div class="ask-chip-w" ref=${ref}>
    <button class=${"chip mini" + (open ? " on" : "")} disabled=${off}
      aria-expanded=${open} aria-haspopup="true"
      aria-label=${field.aria + ": " + current[1]}
      title=${off ? why : field.help}
      onClick=${() => setOpen((o) => !o)}>
      ${current[1]}
      ${!off && html`<span class="ask-chip-cv"><${Icon} name="chevronDown" /></span>`}
    </button>
    ${open && html`<div class="omni-pop ask-chip-pop">
      ${field.opts.map(([v, label, note]) => html`
        <button class=${"omni-row" + (v === value ? " sel" : "")} key=${v}
          onClick=${() => { onPick(field.key, v); setOpen(false); }}>
          <div class="grow">
            <div class="r-name">${label}</div>
            <div class="r-meta">${note}</div>
          </div>
          ${v === value && html`<span class="r-kind"><${Icon} name="check" /></span>`}
        </button>`)}
    </div>`}
  </div>`;
}

function ShapeBar({ shape, setShape, status, aiOff }) {
  const pick = (key, value) => {
    const next = { ...shape, [key]: value };
    setShape(next);
    try { localStorage.setItem(SHAPE_KEY, JSON.stringify(next)); } catch { /* private mode */ }
  };
  return html`<div class="ask-chips" role="group" aria-label="Answer shape">
    ${SHAPE_FIELDS.map((field) => {
      // The SERVER decides availability and supplies the reason — the client never
      // guesses. A control that did nothing on the last answer is disabled with that
      // reason on it, so the explanation is where the pointer already is.
      const ctl = status?.controls?.[field.key];
      const off = ctl ? !ctl.available : (aiOff && field.key !== "people");
      const why = ctl?.reason || (off ? "Keyword search has one setting for this." : "");
      return html`<${ShapeChip} key=${field.key} field=${field}
        value=${shape[field.key]} onPick=${pick} off=${off} why=${why} />`;
    })}
  </div>`;
}

// ── Chats ─────────────────────────────────────────────────────────────────────
// THE INDEX IS THE ONE LISTS ALREADY USES. <aside class="plist">, .plist-tools,
// .plist-scroll, .prow rows with .active + aria-current, SkelRows while loading,
// Empty for none. A second index pattern in one app would be a second thing to
// learn for no gain, and ui.css already carries the mobile collapse for this one.
//
// A RAIL THAT COLLAPSES, NOT A PERMANENT SECOND COLUMN. That is the answer to
// the question DESIGN_DIRECTION §7 leaves open, and it is decided on two grounds:
//
//  · .ask-view has a deliberate max-width reading measure. A permanent column
//    eats into it on any window narrower than about 1050px, and a squeezed
//    transcript is a worse surface than a hidden list.
//  · §7's warning is about chrome. A thread index is navigation, and navigation
//    you are not using should not hold screen space.
//
// So the rail is open only where it costs the measure nothing (the RAIL_FITS
// query below), your own toggle wins from then on, and below the breakpoint the
// rail stops taking space and covers the transcript instead (view-ask.css). One
// piece of state in JS, two geometries in CSS.
//
// "CLEAR CONVERSATION" IS GONE, and this is why: it emptied the thread and kept
// it. With chats, both halves of that are better served by something else — you
// wanted a clean surface, which is New chat, or you wanted the thread gone,
// which is Delete chat. What Clear actually left behind was a husk still named
// after the question whose answer it had just destroyed. So Delete chat takes
// its place in the footer (the thread it acts on is the one on screen) and New
// chat lives at the top of the rail, where Lists puts Create.
// DELETE /api/chat/{id}/history now has no caller in this UI. It stays on the
// server for the stale-bundle path: an older cached copy of this page may still
// call it.
const RAIL_KEY = "nc_ask_rail";
// Must agree with the breakpoint in view-ask.css. Above it the rail is a column
// beside the transcript; below it the rail is a drawer over the transcript, so
// picking a chat closes it the way a drawer closes behind you.
const RAIL_FITS = "(min-width: 1080px)";
// db.CHAT_TITLE_MAX, as a fallback only: GET /api/chats publishes the live value
// as limits.title_max and the rename field reads that. Same discipline as the
// server's tunables — the number is read where it is used, and the default has a
// name rather than being a literal in the markup.
const TITLE_MAX_FALLBACK = 48;
// The upgrade receipt is a permanent row in `state`, so the note it feeds has to
// be dismissible or it becomes chrome forever. Keyed by the adopted chat's id, so
// a second adoption (a second old database) still gets to say its piece.
const ADOPT_KEY = "nc_ask_adoption_seen";

function storedRail() {
  try { return localStorage.getItem(RAIL_KEY) || ""; } catch { return ""; }
}

/** Is the rail open?
 *
 *  TWO RULES, and they exist because the rail is two things. Where the window can
 *  hold it beside the full reading measure it is a column, and a column remembers
 *  what you chose. Where it cannot, it is a drawer over the transcript — and a
 *  drawer's resting state is closed, whatever you last chose on a wide window,
 *  because opening the app should not cover the thing you came to read. */
function loadRail() {
  if (!matchMedia(RAIL_FITS).matches) return false;
  return storedRail() !== "closed";
}

/** One rail row's second line. Never blank — an empty chat says so rather than
 *  rendering a gap (DESIGN_DIRECTION §2).
 *
 *  The server calls the count `turns`, and it is a COUNT of chat_history rows,
 *  where one row is one message. So this says "messages": a person reading "12"
 *  beside a thread has to be able to count the same 12 in the transcript. */
function chatMeta(c) {
  const n = c.turns || 0;
  if (!n) return "No messages yet";
  const when = relDate(c.last_message_at || c.updated_at || "");
  return `${n} ${n === 1 ? "message" : "messages"}${when ? ` · ${when}` : ""}`;
}

/** The index of chats. Navigation only: no per-row menu, no per-row delete.
 *
 *  Rename and delete act on the chat you are IN, from the footer, exactly as
 *  lists.js keeps them in the detail pane rather than on the index row. That
 *  costs one click to delete an old thread and saves a hover-menu, a second
 *  confirm implementation and a row that means four things. */
function ChatsRail({ chats, currentId, adoption, onOpen, onNew, onHide }) {
  const [seen, setSeen] = useState(() => {
    try { return localStorage.getItem(ADOPT_KEY) || ""; } catch { return ""; }
  });
  const rows = chats || [];
  // Where the pre-chats transcript went. db._adopt_orphan_chat_history swept
  // every old turn into ONE thread during init_db, and a migration that silently
  // reorganises the user's own words is the same defect as a cap nobody can see.
  const receipt = adoption && adoption.adopted > 0
    && String(adoption.conversation_id) !== seen ? adoption : null;
  const dismiss = () => {
    const v = String(adoption.conversation_id);
    try { localStorage.setItem(ADOPT_KEY, v); } catch { /* private mode */ }
    setSeen(v);
  };

  return html`
    <aside class="plist ask-rail" aria-label="Your chats">
      <div class="plist-tools">
        <div class="ask-rail-head">
          <button class="btn primary ask-rail-new" onClick=${onNew}>New chat</button>
          ${/* Needed even though the footer carries the same toggle: below the
                 breakpoint this rail covers the bottom-left of the view, and the
                 footer button with it. */ ""}
          <button class="btn quiet mini" aria-label="Hide the chat list"
            title="Hide the chat list" onClick=${onHide}><${Icon} name="chevronLeft" /></button>
        </div>
        ${receipt && html`
          <div class="ask-adopt" role="status">
            <span class="ask-adopt-t">
              ${`Ask put ${receipt.adopted} earlier ${receipt.adopted === 1 ? "message" : "messages"} in one chat. It is in the list below.`}
            </span>
            <button class="ask-adopt-x" aria-label="Hide this message"
              title="Hide this message" onClick=${dismiss}><${Icon} name="close" /></button>
          </div>`}
        ${/* R9: this is the one owner of the chat count. The footer toggle that
               opens this rail states no number. */ ""}
        ${chats !== null && rows.length > 0 && html`
          <div class="section-label ask-rail-count">
            ${rows.length} ${rows.length === 1 ? "chat" : "chats"}
          </div>`}
      </div>
      <div class="plist-scroll">
        ${chats === null && html`<${SkelRows} n=${6} />`}
        ${chats !== null && rows.length === 0 && html`
          <${Empty} title="No chats yet" hint="A chat takes its name from your first question.">
            Send a question below. It starts your first chat.
          </${Empty}>`}
        ${rows.map(c => html`
          <button key=${c.id} class=${"prow" + (c.id === currentId ? " active" : "")}
            aria-current=${c.id === currentId ? "true" : "false"}
            onClick=${() => onOpen(c.id)}>
            <div class="grow">
              <div class="p-name"><span class="txt">${c.title}</span></div>
              <div class="p-meta">${chatMeta(c)}</div>
            </div>
          </button>`)}
      </div>
    </aside>`;
}

export function Ask({ id, params, go }) {
  const [msgs, setMsgs] = useState(null);       // [{role, content, contacts, searched?, trail?}]
  // Seeded EMPTY, not with a name. SELF_NAME's server default is "" and the
  // fetch below only assigns `if (s?.value)`, so any name seeded here is the
  // name every fresh install keeps forever, greeting each new user by it.
  // greeting() already drops the name when it is blank.
  const [selfName, setSelfName] = useState("");   // SELF_NAME setting, fetched once
  const [aiOff, setAiOff] = useState(false);            // no ANTHROPIC key → keyword fallback
  const [pending, setPending] = useState(false);
  // Which chat the in-flight question belongs to. Held as an id, not implied by
  // `pending`, so the "Searching your network" line cannot pulse over a thread
  // you switched to while waiting for a different one.
  const [pendingIn, setPendingIn] = useState(0);
  // NC-083. When the in-flight question started, and a tick to re-render on.
  //
  // The pending line's ONLY liveness signal was a CSS pulse, and the copy was
  // written to depend on that: R11 removed the trailing ellipsis *because* the
  // line animates. So anywhere the animation does not run, the screen says
  // "Searching your network" and looks frozen — indistinguishable from a dead
  // request. Two places it does not run: with reduced motion on it is switched
  // off entirely (view-ask.css loads after ui.css and its `animation: none`
  // resets the whole shorthand, defeating the iteration-count ui.css sets for
  // exactly this reason), and it can vanish when you switch away from the
  // window, which is WKWebView suspending the web process.
  //
  // A COUNTED SECOND is the fix, because it does not depend on anything
  // rendering continuously. elapsed is computed from `startedAt` rather than
  // incremented, so a throttled or suspended timer shows the true figure the
  // moment it ticks again instead of resuming from where it fell asleep. It also
  // earns its place now that NC-080 raised the ceiling to 180 seconds: watching
  // a still line for three minutes tells you nothing about whether to wait.
  const [startedAt, setStartedAt] = useState(0);
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(() => setTick(n => n + 1), 1000);
    return () => clearInterval(t);
  }, [pending]);
  const [text, setText] = useState("");
  // What shape to ask for, and what the server said actually applied. Two values,
  // not one: the request is a preference and the response is the fact. Reading the
  // controls back off `shape` alone would show a Brief button lit on an answer that
  // ran Full because there was no API key.
  const [shape, setShape] = useState(loadShape);
  const [lastShape, setLastShape] = useState(null);

  // ── Which chat this view is in ──────────────────────────────────────────────
  // `id` is the route's (#ask/7). No id means "the newest chat", which is exactly
  // what GET /api/chat/history resolves to server-side — so #ask is a real place
  // to be and the URL is never rewritten under the user. Rewriting #ask to
  // #ask/7 on arrival would also turn the back button into a trap: going back to
  // #ask would resolve forward again, forever.
  const [convId, setConvId] = useState(id || 0);
  const [conv, setConv] = useState(null);       // the chat's row: title, shape
  const convRef = useRef(convId);
  convRef.current = convId;   // read after an await, where a closure's copy is stale
  const [gone, setGone] = useState("");         // a named chat that is not there
  const [cut, setCut] = useState(null);         // {trimmed} when the transcript was cut
  const [chats, setChats] = useState(null);
  const [hub, setHub] = useState({ adoption: null, limits: null });
  const [railOpen, setRailOpen] = useState(loadRail);
  const [reloadKey, setReloadKey] = useState(0);   // re-read the same chat
  const reqRef = useRef(0);      // transcript request token — a slow one cannot win

  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  // NC-154. The id of the question in flight, and whether a stop has been asked
  // for. `stopping` is its own flag rather than being derived, because the gap
  // between pressing Stop and the answer returning is real — the loop finishes
  // the round it is in — and a button that looks unpressed for two seconds gets
  // pressed again.
  const runRef = useRef("");
  const [stopping, setStopping] = useState(false);
  // NC-157. Runs whose Stop was pressed from a turn that had already given up
  // waiting. Held per id rather than as one flag because two questions can be
  // outstanding in two chats, and "you stopped it" belongs to one of them.
  const [stoppedRuns, setStoppedRuns] = useState(() => new Set());
  // NC-164. One short line spoken when a turn lands. Set by send() rather than
  // derived from the transcript, so opening an old chat does not announce its
  // last answer as though it had just arrived.
  const [announce, setAnnounce] = useState("");

  // Ask the server to stop, by id. Deliberately NOT an abort of our own fetch:
  // the request stays open and comes back with the people already found and the
  // real bill. Aborting would hide the spinner and leave crm/chat.py looping and
  // billing Anthropic with nobody watching, which is worse than no button.
  const stopRun = useCallback(async (id) => {
    if (!id) return;
    try { await api("/api/chat/stop", { method: "POST",
      body: JSON.stringify({ run_id: id }) }); }
    catch { /* the run had already finished; the answer is on its way regardless */ }
  }, []);

  const stop = useCallback(async () => {
    if (!runRef.current) return;
    setStopping(true);
    await stopRun(runRef.current);
  }, [stopRun]);

  // NC-157. Stop an answer this view has STOPPED WATCHING. The browser gives up
  // after ASK_TIMEOUT_MS and clears runRef as it does — which used to leave the
  // answer running on the user's Mac with no control anywhere on screen and no
  // way to know it was still calling Anthropic. That is precisely the state NC-154
  // exists to make impossible; it was arriving through the timeout instead of
  // through an abort. Not automatic: the copy promises the answer may still be
  // coming, and silently killing a nearly-finished one would make that a lie.
  const stopTimedOut = useCallback(async (id) => {
    if (!id) return;
    setStoppedRuns(prev => new Set(prev).add(id));
    await stopRun(id);
  }, [stopRun]);
  const nameRef = useRef(null);
  const lastFired = useRef("");  // last omnibox ?q= we sent — fires each NEW one
  const lastPrefilled = useRef("");  // same, for ?prefill= (staged, never sent)
  const lastNew = useRef("");     // same, for ?new=1 from ⌘K

  const scrollDown = () => {
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  };

  // ── One chat's transcript ───────────────────────────────────────────────────
  // Re-runs on every switch. The token guard is what stops the answer for the
  // chat you left painting over the chat you opened: only the newest request may
  // write, and a stale one returns without touching state.
  useEffect(() => {
    const token = ++reqRef.current;
    setMsgs(null); setGone(""); setCut(null);
    api(id ? `/api/chat/${id}/history` : "/api/chat/history")
      .then(d => {
        if (token !== reqRef.current) return;
        setMsgs(d.messages || []);
        setConv(d.conversation || null);
        setConvId(d.conversation?.id || d.conversation_id || 0);
        setLastShape(null);   // the shape echo belongs to an answer, not to a chat
        // The shape this chat last ran with, restored so the chips describe the
        // answers on screen instead of the client's current dropdowns. Deliberately
        // NOT written back to localStorage: that value is the preference a NEW
        // chat starts from, and reopening an old thread must not overwrite it.
        if (d.conversation?.shape) setShape(s => ({ ...s, ...d.conversation.shape }));
        // A trimmed transcript says what it left behind. Kept as
        // `trimmed` alone, and not as total-and-shown, because trimmed is the one
        // number that does NOT drift: sending a question adds a turn to both the
        // total and the shown set, so a rendered "this chat has 8 messages" was
        // still saying 8 with ten in the thread. How many earlier messages are
        // missing does not change when you append to the end.
        setCut(d.trimmed > 0 ? { trimmed: d.trimmed } : null);
        scrollDown();
      })
      .catch(e => {
        if (token !== reqRef.current) return;
        setMsgs([]);
        // Only a NAMED chat can 404 — #ask resolves to whatever exists. A stale
        // link must say so rather than silently continuing another thread.
        if (id) { setGone(e.message || "Ask could not open this chat"); setConv(null); setConvId(0); }
      });
  }, [id, reloadKey]);

  const loadChats = useCallback(async () => {
    try {
      const d = await api("/api/chats");
      setChats(d.chats || []);
      setHub({ adoption: d.adoption || null, limits: d.limits || null });
    } catch { setChats([]); }
  }, []);
  useEffect(() => { loadChats(); }, [loadChats]);

  useEffect(() => {
    api("/api/settings")
      .then(d => {
        const s = (d.settings || []).find(x => x.key === "SELF_NAME");
        if (s?.value) setSelfName(s.value);
      })
      .catch(() => {});
    api("/api/status").then(d => setAiOff(d.ai === false)).catch(() => {});
  }, []);

  const send = useCallback(async (q, fromComposer = false) => {
    q = (q || "").trim();
    if (!q || pending || gone) return;
    setAnnounce("");
    // NC-154. One id per question, generated here so the Stop button has
    // something to name before the answer comes back. crypto.randomUUID is in
    // every WebKit this app runs on, but the .app is the product and a missing
    // API would cost the whole send — so it falls back rather than throwing.
    const runId = (crypto.randomUUID ? crypto.randomUUID()
                   : "r" + Date.now() + Math.random().toString(36).slice(2))
                  .replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64);
    runRef.current = runId;
    setStopping(false);
    // The chat this question belongs to, captured before the await. convRef holds
    // the live one, so the two can be compared when the answer lands.
    const forChat = convId;
    // NC-159. Only the COMPOSER's own send empties the composer. This used to
    // clear it for every send, including a click on one of NC-156's option
    // buttons — so typing a longer reply and then changing your mind and
    // clicking a button threw the draft away. `fromComposer` is a parameter and
    // not a comparison against `text`, because this callback is not memoised on
    // `text` and the copy it closes over is one render stale.
    if (fromComposer) {
      setText("");
      autoGrow(inputRef.current);   // NC-153: emptying the value does not shrink it
    }
    setMsgs(m => [...(m || []), { role: "user", content: q }]);
    setPending(true);
    setPendingIn(forChat);
    setStartedAt(Date.now());
    scrollDown();
    try {
      const d = await api("/api/chat", { method: "POST", timeoutMs: ASK_TIMEOUT_MS,
        body: JSON.stringify(forChat
          ? { message: q, shape, conversation_id: forChat, run_id: runId }
          : { message: q, shape, run_id: runId }) });
      // The chat list changed whatever happened: a first question DERIVES the
      // thread's title, and every answer moves the thread to the top.
      loadChats();
      // You switched threads while this was in flight. The turn is on disk in the
      // chat it was asked in — say where it went rather than appending it here,
      // which would file your words under a question you did not ask.
      if (convRef.current !== forChat) {
        toast(`The answer went to “${d.conversation?.title || "the other chat"}”`);
        setPending(false);
        return;
      }
      // Which chat this landed in, straight from the server: a first-ever question
      // creates one, and a client with no id continues the newest.
      if (d.conversation?.id) { setConvId(d.conversation.id); setConv(d.conversation); }
      // What actually applied, straight from the server. Never inferred from the
      // request: the two differ whenever a control was unavailable.
      setLastShape(d.shape || null);
      setMsgs(m => [...(m || []), {
        role: "assistant", content: d.reply || "", contacts: d.contacts || [],
        searched: d.searched || null, trail: d.trail || [], spend: d.spend || null,
        stopped: !!d.stopped,
        // NC-156. The question this turn asked back, when it asked one instead
        // of answering. Named here explicitly for the same reason the two card
        // counts below are: this object is built key by key, so a server field
        // nobody names is silently dropped.
        question: d.question || null,
        // How many the searches found vs how many survived to the screen. This
        // object is built key by key rather than spread, so a new server field
        // that is not named here is silently dropped, these two included. Absent on a
        // restored history turn, because chat_history persists the cards but not
        // the counts; the render guards on typeof, so an older turn simply says
        // nothing rather than lying.
        cards_matched: d.cards_matched, total_matched: d.total_matched,
        // Set by the server when the model ran out of room mid-answer
        // (chat.py maps stop_reason "max_tokens"), and persisted with the turn,
        // so a restored history turn still carries it.
        truncated: !!d.truncated,
        // How much of this thread the model actually read. The context window and
        // the rendered transcript are two different numbers now, and this is the
        // smaller one — so a long thread answered from its last 20 messages says
        // so instead of reading as the assistant forgetting.
        context: d.context || null,
      }]);
      // NC-164. The pending line announces itself and then vanishes, so the one
      // turn that NEEDS a response — a question with buttons — was the one that
      // arrived in silence. Short on purpose: a long roster read aloud in
      // full is worse than saying nothing.
      setAnnounce(d.question
        ? `Ask has a question for you, with ${(d.question.options || []).length} options`
        : "Answer ready");
    } catch (e) {
      // R8: the request itself failed, so there is no answer to read. Carry the
      // raw message as `failed` instead of pasting it into the reply text — the
      // turn renders a notice, not a sentence with an exception inside it.
      if (convRef.current === forChat) {
        setMsgs(m => [...(m || []), {
          role: "assistant", content: "", failed: e.message,
          // NC-157. The REQUEST gave up; the answer did not. Keep the handle on
          // the turn so its notice can still stop the spending — runRef is
          // cleared below, which is right for the composer and was what left a
          // timed-out answer unstoppable.
          runId: TIMED_OUT_RE.test(e.message || "") ? runId : "",
        }]);
      }
      setAnnounce("Ask could not answer");
      // NC-082. Put the question back in the box.
      //
      // setText("") above clears the input the moment you hit send, and nothing
      // else puts it back — so a question that failed would have to be RETYPED from
      // the transcript above it. A typical case: a write that could not get the
      // database lock, nothing saved, and the words gone from the box. One click
      // should resend it.
      //
      // Only when the box is still empty: if you typed a new question while this
      // one was in flight, restoring would overwrite what you are typing, which
      // is a worse bug than retyping.
      setText(t => (t.trim() ? t : q));
    }
    setPending(false);
    runRef.current = ""; setStopping(false);   // NC-154: the handle dies with the run
    scrollDown();
    // `shape` and `convId` are in here because they are READ inside: with only
    // [pending] the memoised callback kept the shape and the chat from the render
    // that built it, so a chip you had just changed applied one question late.
  }, [pending, gone, shape, convId, loadChats]);

  // ?q= from the omnibox: ask it once, as soon as history is loaded
  useEffect(() => {
    const q = params?.get("q");
    if (q && msgs !== null && q !== lastFired.current) {
      lastFired.current = q;
      history.replaceState(null, "", "#ask");
      send(q);
    }
  }, [msgs, params, send]);

  // ── The chat controls ───────────────────────────────────────────────────────
  const toggleRail = () => {
    setRailOpen(v => {
      const next = !v;
      try { localStorage.setItem(RAIL_KEY, next ? "open" : "closed"); } catch { /* private mode */ }
      return next;
    });
  };

  // The breakpoint is WATCHED, not sampled once at mount. Two reasons: the app
  // can open at any size and the pane it opens in can settle a moment after
  // first paint, and a rail that stayed shut on a 1280px
  // window because the window had been narrower for one frame looks broken. Crossing
  // the breakpoint also changes what the rail IS, so it re-reads loadRail's two
  // rules rather than keeping a state that meant something else.
  useEffect(() => {
    const mq = matchMedia(RAIL_FITS);
    const onChange = () => setRailOpen(loadRail());
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const openChat = (cid) => {
    go("ask/" + cid);
    // A drawer closes behind you; a column stays. Not persisted — you did not ask
    // for the rail to be shut, the window is simply too narrow to hold it open.
    if (!matchMedia(RAIL_FITS).matches) setRailOpen(false);
  };

  const newChat = async () => {
    // An empty chat you are already in IS what New chat would make, so it makes
    // nothing. Otherwise every stray click leaves another "New chat" husk in the
    // list. Says so, because a control that appears to do nothing is worse.
    if (convId && msgs !== null && msgs.length === 0) {
      toast("This chat is empty already");
      inputRef.current?.focus();
      return;
    }
    try {
      // The shape goes with it. handle_chats_post stores what the chat starts
      // with and the transcript effect reads that back, so posting the saved
      // preference is what stops a new chat resetting your chips to the defaults.
      //
      // The SAVED preference, read back from storage, and not `shape`: while an old
      // chat is open `shape` holds that chat's own settings (the transcript effect
      // restores them without saving them), so posting it made every new chat copy
      // whatever chat you happened to be looking at. The chips are set to the same
      // value once the chat exists, so they describe the chat you are about to be in.
      const saved = loadShape();
      const d = await api("/api/chats", { method: "POST", body: JSON.stringify({ shape: saved }) });
      setShape(saved);
      await loadChats();
      const cid = d.conversation?.id;
      if (cid) go("ask/" + cid); else setReloadKey(k => k + 1);
      inputRef.current?.focus();
    } catch (e) { toast(e.message); }
  };

  // Rename and delete are two-state, inline, and mutually exclusive: the first
  // click swaps the control for the confirm, and leaving it or pressing Escape
  // puts it back. Same idiom as lists.js — this app has no confirm() dialogs.
  const [renaming, setRenaming] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [delPending, setDelPending] = useState(false);

  // A pending confirm belongs to the chat it opened on, so switching chats (or
  // deleting this one) must not leave it armed over a different thread.
  useEffect(() => { setRenaming(false); setDelPending(false); }, [convId]);
  useEffect(() => {
    if (!delPending) return;
    const onKey = (e) => { if (e.key === "Escape") setDelPending(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [delPending]);
  useEffect(() => {
    if (renaming) { nameRef.current?.focus(); nameRef.current?.select(); }
  }, [renaming]);

  const startRename = () => {
    setDelPending(false);
    setDraftName(conv?.title || "");
    setRenaming(true);
  };

  const saveName = async () => {
    const typed = draftName.trim();
    setRenaming(false);
    if (!convId || !typed || typed === (conv?.title || "")) return;
    try {
      const d = await api(`/api/chat/${convId}/rename`,
        { method: "POST", body: JSON.stringify({ title: typed }) });
      // The server normalises a typed name exactly as it derives one, so what it
      // stored can differ from what you typed. Render the stored title, and when
      // the two differ say so — a silently shortened name is a silent drop.
      const stored = d.title || typed;
      setConv(c => (c ? { ...c, title: stored } : c));
      if (stored !== typed) toast(`Saved as “${stored}”`);
      loadChats();
    } catch (e) { toast(e.message); }
  };

  const del = async () => {
    setDelPending(false);
    if (!convId) return;
    try {
      const d = await api("/api/chat/" + convId, { method: "DELETE" });
      // The count comes from the server, and it is the honest one: the delete is
      // soft, so the turns are not destroyed. "Deleted" alone would say neither.
      const n = d.turns || 0;
      toast(n
        ? `Chat deleted. ${n} ${n === 1 ? "message stays" : "messages stay"} on disk.`
        : "Chat deleted");
      await loadChats();
      // The URL still names the chat that just went, so move off it: #ask means
      // "the newest chat", which is where you want to land. When the URL names no
      // chat there is no navigation to make — assigning the hash it already holds
      // fires no hashchange — so re-read in place instead.
      if (id) go("ask"); else setReloadKey(k => k + 1);
    } catch (e) { toast(e.message); }
  };

  // Intent chip: put the phrase in the composer and focus it — never send.
  const prefill = (p) => {
    setText(p);
    const el = inputRef.current;
    if (el) {
      el.focus();
      autoGrow(el);
      requestAnimationFrame(() => el.setSelectionRange(p.length, p.length));
    }
  };

  // ?prefill= from a ⌘K intent command. Deliberately NOT ?q=: it stages the
  // question and leaves the send to you, so a stray Enter in the palette can
  // never bill an Anthropic call. Cleans the hash like ?q= does, so the phrase
  // isn't re-staged over your typing on the next render.
  // Compared by token rather than by text, so asking for the SAME intent twice
  // stages it twice — you may well want "Who could introduce me to " again after
  // typing over the first one.
  useEffect(() => {
    const p = params?.get("prefill");
    const token = p ? `${p}:${params.get("_") || ""}` : "";
    if (p && msgs !== null && token !== lastPrefilled.current) {
      lastPrefilled.current = token;
      history.replaceState(null, "", "#ask");
      prefill(p);
    }
  }, [msgs, params]);

  // ?new=1 from ⌘K's "Start a new chat". Routed as a one-shot param rather than
  // as an action inside main.js, so creating a chat has exactly one
  // implementation and it is this file's — the same reason lists?new=1 exists.
  // Waits for the transcript, because newChat has to know whether the chat you
  // are in is already empty.
  useEffect(() => {
    const token = oneShot(params, "new");
    if (!token || msgs === null || token === lastNew.current) return;
    lastNew.current = token;
    history.replaceState(null, "", "#ask");
    newChat();
  }, [msgs, params]);

  const copyAnswer = async (content) => {
    try { await navigator.clipboard.writeText(content); toast("Copied"); }
    catch { toast("Copy failed"); }
  };

  const empty = msgs !== null && msgs.length === 0 && !pending && !gone;
  // From the timestamp, never a counter: correct on the first tick after the
  // window (or the whole web process) comes back, however long it was away.
  const elapsed = startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : 0;
  // NC-158. `pending` is one flag for the whole view, and the answer belongs to
  // ONE chat. Two names rather than one test repeated, because the two states
  // want opposite controls: the chat that is answering gets Stop, and every
  // other chat gets an Ask that is dead and says why.
  //
  // The option buttons under a question are NOT scoped this way, and that is
  // deliberate: send() refuses while ANY chat is answering, so a button scoped
  // to this chat would look live and do nothing.
  const busyHere = pending && pendingIn === convId;
  const busyElsewhere = pending && !busyHere;
  const titleMax = hub.limits?.title_max || TITLE_MAX_FALLBACK;
  // What the delete confirm has to state, and it is about the whole thread rather
  // than the part of it on screen. Taken from the rail's own row, which is a COUNT
  // over chat_history refreshed after every send — so this number has one owner
  // and it is the same one the list shows. The transcript length is only the
  // fallback for the moment before /api/chats has answered.
  const totalTurns = chats?.find(c => c.id === convId)?.turns ?? (msgs || []).length;

  return html`
    <div class="ask-layout">
      ${railOpen && html`
        <${ChatsRail} chats=${chats} currentId=${convId} adoption=${hub.adoption}
          onOpen=${openChat} onNew=${newChat} onHide=${toggleRail} />`}
      <div class="ask-view">
        <div class="ask-scroll" ref=${scrollRef}>
          ${/* R8: name the cause, give the one action, keep the raw text behind the
                 toggle. The composer is disabled while this is up, so a question
                 cannot land in a thread you did not open. */ ""}
          ${gone && html`
            <${Notice} tone="bad" text="Ask cannot open this chat. It may be deleted."
              action="Start a new chat" onAction=${newChat} detail=${gone} />`}
          ${/* The transcript cap states what it left behind. */ ""}
          ${cut && html`
            <div class="ask-cut">
              ${`Ask does not show the ${cut.trimmed.toLocaleString()} earliest ${cut.trimmed === 1 ? "message" : "messages"} in this chat.`}
            </div>`}
          ${msgs === null && !gone && html`<${SkelRows} n=${3} />`}
          ${empty && html`
            <div class="ask-empty">
              <h2>${greeting(selfName)}</h2>
              <p>Find who to invite, who to introduce, or who to contact.
                Every answer comes from your own contacts.</p>
              <div class="ask-intents">
                ${INTENTS.map(it => html`
                  <button key=${it.label} class="chip" onClick=${() => prefill(it.prefill)}>${it.label}</button>`)}
              </div>
              ${aiOff && html`
                <div class="ask-empty-notice">
                  <${Notice} tone="warn" text="Ask has no API key. Answers use keyword search."
                    action="Open Settings" onAction=${() => go("settings?row=ai")} />
                </div>`}
            </div>`}
          ${(msgs || []).map((m, i) => {
            if (m.role !== "assistant") {
              return html`<div key=${i} class="ask-question">${m.content}</div>`;
            }
            const { text: body, note } = normalizeReply(m.content);
            const cards = m.contacts || [];
            // NC-151. How many of this answer's cards were withheld because the
            // person is hidden now. Only set on restored turns, and only when
            // there were some, so a live answer and an unaffected one are
            // unchanged.
            const withheld = m.contacts_withheld || 0;
            // NC-156. Answered means "a turn exists after this one" — read off
            // the array being mapped, never off a stored flag. `picked` is that
            // turn's own text, which is exactly what the button sent.
            const nextTurn = (msgs || [])[i + 1];
            // NC-166. The turn after a question is the answer to it ONLY when it
            // is the user's. Adopted pre-chat rows do not guarantee alternation,
            // and two assistant turns in a row used to render the question as
            // answered with nothing marked and its buttons dead — a question you
            // could no longer answer.
            const answered = !!nextTurn && nextTurn.role === "user";
            const picked = answered ? nextTurn.content : "";
            // A stripped filler line leaves an empty answer, which is correct when
            // rows follow it (R6). With nothing at all to show, say so rather than
            // painting a blank turn.
            // NC-151: an answer that was all cards and no prose loses every card
            // when the people in it are hidden, and would otherwise fall through
            // to "No contact matches that question." — which would be a new lie
            // in place of the old one. The line below says what really happened.
            const shown = body || (!cards.length && !note && !m.failed && !withheld
              ? "No contact matches that question." : "");
            return html`
              <div key=${i} class="ask-turn">
                <${Provenance} searched=${m.searched} trail=${m.trail} spend=${m.spend} />
                ${/* NC-080: "Send the question again" was advice that would fail
                     identically every time. Two causes look the same from here
                     and want opposite responses, so they are told apart rather
                     than averaged: a timeout means the answer is still being
                     worked on and the thread will have it on reload, and anything
                     else is a fault that retrying will reproduce. */ ""}
                ${/* NC-157. A timed-out turn keeps its run handle, so the
                       notice can still stop the spending. The copy already
                       promises the answer may be on its way, so this is an
                       OFFER and never automatic: killing a nearly-finished
                       answer to tidy up would make that promise false. */ ""}
                ${m.failed && html`
                  <${Notice} tone="bad"
                    text=${!TIMED_OUT_RE.test(m.failed)
                      ? ASK_FAILED
                      : m.runId && stoppedRuns.has(m.runId)
                        ? "You stopped the answer. Reopen this chat to see how far it got."
                        : "Ask stopped waiting after 3 minutes. The answer can still be running on this Mac. Reopen this chat in a moment to see if it finished."}
                    action=${m.runId && !stoppedRuns.has(m.runId) ? "Stop the answer" : ""}
                    onAction=${() => stopTimedOut(m.runId)}
                    detail=${m.failed} />`}
                ${note && html`
                  <${Notice} tone="warn" text=${note.text} detail=${note.detail}
                    action=${note.action} onAction=${() => go("settings?row=ai")} />`}
                ${shown && html`<div class="ask-answer"><${Md} text=${shown} /></div>`}
                ${/* Quiet, not an error: the answer above is still worth reading, it
                     just stopped early, and the rows under it are however far it
                     got. Said once, plainly — no retry prompt, no red. */ ""}
                ${m.truncated && html`
                  <div class="ask-cut">
                    The answer stopped early.${cards.length ? " The list below is incomplete." : ""}
                  </div>`}
                ${/* State what the answer left out. The cards go through two
                     narrowings server-side — agreeing with the prose, then a payload
                     cap — and both would be silent, so a question that matched
                     many people could show fewer rows and say nothing. Only rendered when the
                     numbers actually disagree, so a complete answer stays quiet.
                     One sentence per fact, active voice (STE R1). */ ""}
                ${typeof m.cards_matched === "number" && m.cards_matched > cards.length && html`
                  <div class="ask-cut">
                    ${`The answer lists ${cards.length}. The searches found ${m.cards_matched}.`}
                  </div>`}
                ${/* The zero-key path: it scans every contact, keeps the best 30 and
                     used to report no total at all. */ ""}
                ${typeof m.total_matched === "number" && m.total_matched > cards.length && html`
                  <div class="ask-cut">
                    ${`Keyword search matched ${m.total_matched} people. The ${cards.length} closest are below.`}
                  </div>`}
                ${/* How much of this thread the model read. Only when it actually
                     left something out, and only on the LLM path — the rule-based
                     engine is fed nothing at all, which fed_to_llm states rather
                     than leaving it to be inferred from a zero. A restored history
                     turn has no context object, so it says nothing instead of
                     guessing. */ ""}
                ${m.context && m.context.fed_to_llm && m.context.trimmed > 0 && html`
                  <div class="ask-cut">
                    ${`Ask read the last ${m.context.turns} messages of this chat. It did not read ${m.context.trimmed} earlier messages.`}
                  </div>`}
                ${/* NC-151. Said in the same quiet voice as the other "what this
                       answer left out" lines, and for the same reason: the answer
                       above is still worth reading, it just no longer shows people
                       you have since hidden. Un-hide them and the cards come back —
                       nothing was rewritten. */ ""}
                ${withheld > 0 && html`
                  <div class="ask-cut">
                    ${withheld === 1
                      ? "One person in this answer is hidden now, so their card is not shown."
                      : `${withheld} people in this answer are hidden now, so their cards are not shown.`}
                  </div>`}
                ${/* NC-156. Above the cards, because a question turn has none —
                       and below the answer text, because the question IS the
                       last line of that text and these are its buttons. */ ""}
                ${m.question && html`
                  <${AskOptions} q=${m.question} answered=${answered} picked=${picked}
                    busy=${pending} onPick=${(o) => send(o)} />`}
                <${ContactCards} contacts=${cards} go=${go} />
                ${(shown || cards.length > 0) && html`
                  <div class="ask-actions">
                    ${shown && html`
                      <button class="btn quiet mini" onClick=${() => copyAnswer(shown)}>Copy answer</button>`}
                    ${/* This navigates to the People list; it does not carry these
                           people into it. "Open in People" claimed more than it
                           does (R1). */ ""}
                    ${cards.length > 0 && html`
                      <button class="btn quiet mini" onClick=${() => go("people")}>Open People</button>`}
                  </div>`}
              </div>`;
          })}
          ${/* Scoped to the chat the question was asked in: switching threads while
                 an answer is in flight must not make a different thread look busy. */ ""}
          ${pending && pendingIn === convId && html`
            <div class="ask-turn">
              ${/* R11: the line pulses, so it does not also carry an ellipsis. */ ""}
              ${/* The seconds are their own element and NOT inside .prov-searching:
                     that class carries the pulse, and a number fading in and out
                     is harder to read than a still one. aria-live stays on the
                     text alone so a screen reader is not read a new number every
                     second. */ ""}
              <div class="prov prov-pending">
                ${/* NC-154: the loop finishes the round it is in before it can
                       return, so "Stopping" is a real state with a real duration,
                       and saying "Searching" through it reads as a dead button. */ ""}
                <span class="prov-searching" aria-live="polite">
                  ${stopping ? "Stopping" : "Searching your network"}</span>
                <span class="prov-elapsed">${elapsed}s</span>
              </div>
              <div class="ask-answer"><div class="skel prov-skel" /></div>
            </div>`}
        </div>
        <div class="ask-foot">
          ${/* NC-164. Announced, not shown. The pending line above says
                 "Searching your network" out loud and then disappears, so the
                 arrival of the thing you were waiting for was silent — worst of
                 all for a question, which is the one turn that needs an answer
                 from you. send() sets this and clears it on the next send, so
                 two identical announcements in a row are still two changes and
                 both are read. */ ""}
          <div class="sr-only" role="status" aria-live="polite">${announce}</div>
          <${ShapeBar} shape=${shape} setShape=${setShape} status=${lastShape} aiOff=${aiOff} />
          <div class="composer">
            ${/* NC-153. This was an <input>, which cannot hold a newline at all —
                   so "Shift+Enter for a new line" was not a key-handler fix, the
                   element had to change. A textarea grows with what is typed and
                   stops at ASK_ROWS_MAX rows, after which it scrolls: a composer
                   that keeps growing eventually pushes the answer it is about
                   off the top of the screen.

                   Enter still sends, because one-line questions are almost all
                   of them and making everyone press a modifier to ask would be
                   a worse trade. Shift+Enter, and the other newline gesture
                   people arrive with (Alt/Option+Enter), insert a line instead.
                   An IME composing Japanese or Chinese fires Enter to COMMIT the
                   candidate — `isComposing` is the one check that stops that
                   keystroke sending a half-written question. */ ""}
            <textarea class="input ask-input" ref=${inputRef} value=${text}
              aria-label="Your question" rows="1"
              placeholder="Type a question about a person or a topic"
              disabled=${!!gone}
              onInput=${e => { setText(e.target.value); autoGrow(e.target); }}
              onKeyDown=${e => {
                if (e.key !== "Enter") return;
                if (e.shiftKey || e.altKey) return;        // a deliberate newline
                if (e.isComposing || e.keyCode === 229) return;   // IME committing
                e.preventDefault();                        // or the newline lands too
                send(text, true);                          // NC-159: this one owns the box
              }}></textarea>
            ${/* NC-154. One button in one place, swapping role — not a second
                   control appearing beside it and shifting the composer. It stays
                   `.btn.primary` in both states because view-ask.css sizes the
                   composer's button through that selector; a plain .btn would
                   change the footer's height the instant an answer started.

                   Disabled once pressed, so a second press cannot fire a second
                   stop at a run that is already stopping.

                   NC-158: scoped to THIS chat, the same test the spinner above
                   already uses. Keyed off `pending` alone, chat B's composer
                   said "Stop" while chat A was answering, and pressing it
                   cancelled a different chat's paid answer. In the other chats
                   the button stays Ask and says why it is dead, rather than
                   offering a control that acts somewhere else. */ ""}
            ${busyHere
              ? html`<button class="btn primary ask-stop" disabled=${stopping}
                  title="Stop this answer and keep what it has found"
                  onClick=${stop}>${stopping ? "Stopping…" : "Stop"}</button>`
              : html`<button class="btn primary"
                  disabled=${!text.trim() || !!gone || busyElsewhere}
                  title=${busyElsewhere
                    ? "Another chat is still answering. Wait for it to finish."
                    : ""}
                  onClick=${() => send(text, true)}>Ask</button>`}
          </div>
          ${/* The chat controls. Always present, because the rail toggle is how you
                 find your chats at all; the thread's own two controls appear once
                 there is a thread to act on. */ ""}
          <div class="ask-toolbar">
            <button class="btn quiet mini ask-rail-btn" aria-expanded=${railOpen}
              title=${railOpen ? "Hide the chat list" : "Show the chat list"}
              onClick=${toggleRail}>Chats</button>
            ${convId > 0 && html`
              <div class="ask-thread-acts">
                ${renaming
                  ? html`
                    <div class="ask-rename" role="group" aria-label="Rename this chat"
                      onBlur=${(e) => {
                        // Leaving the group cancels. relatedTarget is null when focus
                        // goes to the page itself, so treat that as leaving too.
                        if (!e.currentTarget.contains(e.relatedTarget)) setRenaming(false);
                      }}>
                      ${/* maxLength is the server's own ceiling, read from
                             /api/chats — so the field cannot accept a name the
                             list would then silently shorten. */ ""}
                      <input class="input mini ask-rename-in" ref=${nameRef} value=${draftName}
                        aria-label="Chat name" maxLength=${titleMax}
                        onInput=${e => setDraftName(e.target.value)}
                        onKeyDown=${e => {
                          if (e.key === "Enter") saveName();
                          if (e.key === "Escape") setRenaming(false);
                        }} />
                      <button class="btn primary mini" onClick=${saveName}>Save</button>
                    </div>`
                  : html`
                    <button class="btn quiet mini" onClick=${startRename}>Rename</button>`}
                ${/* Two steps, the lists.js way, and the question states the
                       consequence: the delete is SOFT, so the messages are not
                       destroyed with the thread. */ ""}
                ${delPending
                  ? html`
                    <div class="del-confirm" role="group" aria-label="Confirm deleting this chat"
                      onBlur=${(e) => {
                        if (!e.currentTarget.contains(e.relatedTarget)) setDelPending(false);
                      }}>
                      <span class="del-confirm-q">
                        ${totalTurns
                          ? `Delete this chat? The ${totalTurns} ${totalTurns === 1 ? "message stays" : "messages stay"} on disk.`
                          : "Delete this chat? It has no messages."}
                      </span>
                      <button class="btn danger mini" autofocus onClick=${del}>Confirm</button>
                      <button class="btn quiet mini" onClick=${() => setDelPending(false)}>Cancel</button>
                    </div>`
                  : html`
                    <button class="btn quiet mini danger" onClick=${() => setDelPending(true)}>
                      Delete chat
                    </button>`}
              </div>`}
          </div>
        </div>
      </div>
    </div>`;
}
