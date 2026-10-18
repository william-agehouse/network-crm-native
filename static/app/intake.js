/* Intake — paste people in, review what was found, then save.
 *
 * THIS FILE IS THE FRONT END OF `crm/intake.py` AND `crm/api_intake.py` (NC-016).
 * A backend feature is not done until the front end can reach it, and
 * `tests/test_intake_ui.py` checks that: it derives the route list from
 * `api_intake.ROUTES` at test time and fails if the client stops calling one.
 * So: every one of the eight routes is called from here, each from exactly one
 * literal call site.
 *
 * THE SHAPE OF THE FLOW, AND WHY IT IS THREE SCREENS
 * Paste → review → save. `propose` and `commit` are separate endpoints on
 * purpose: nothing reaches crm.db until the list has been on screen and the
 * commit body carries `confirm: true`. The review screen is therefore not a
 * confirmation dialog bolted onto a write — it is the product, and it is where
 * every refusal the server made is read.
 *
 * WHAT THE SERVER DECIDES, AND WHAT THIS FILE MUST NOT PRETEND TO DECIDE
 * `commit_batch` re-runs the matcher and ignores any contact_id, verdict or ref
 * a client sends, so a batch is a *description of people* and never an
 * instruction naming a row. This client keeps that honest by never sending one:
 * the commit body carries only `intake.COMMIT_KEYS` (name, name_confirmed,
 * company, position, how_we_met, linkedin_url, email, phone, note, origin).
 *
 * Two things the client is trusted for, narrowly, and both are real human acts:
 *   · `name_confirmed` — "a person read this exact string". A name the person
 *     typed arrives already confirmed; a name guessed from a LinkedIn slug, an email
 *     local part or a model does not, and `commit_batch` refuses to create from
 *     an unconfirmed one. That is the one tick this surface asks for, and the
 *     row says which string it is confirming.
 * That list used to have a third item — `allowed_urls` on a lookup, which the
 * client sent because an empty allowlist read as "allow everything" on the
 * server. NC-148 took that away from the client entirely: a lookup now sends the
 * PASTE, the server re-derives the allowlist from it with the same parser
 * `/parse` uses, and an empty allowlist refuses everything. The browser is no
 * longer trusted to say which addresses were the owner's own, which is right —
 * it was never in a position to be believed about that.
 *
 * WHY THERE IS NO "READ IT WITH THE MODEL" BUTTON WITHOUT A KEY
 * `/api/intake/propose` degrades on purpose — with no key it runs the offline
 * parser and says so in `report.degraded`. That is right for a call that has
 * already been made; it is wrong as a button, because a control that cannot do
 * the thing its label promises is the NC-006 defect. So the model button renders
 * only when `/api/intake/status` says a key is set, and the keyless case is
 * stated up front on the paste screen instead of discovered afterwards. The
 * degraded path still renders: a key that is set and a call that fails lands in
 * exactly the same report block.
 *
 * Register: docs/DESIGN_DIRECTION.md §2/§6 — cool neutral, structure from
 * hairlines, actions are ink (`--action`), `--accent` means interaction only,
 * and colour is STATE: a refusal is red, something that needs you is gold,
 * everything normal is neutral. Every value comes from tokens.css through
 * static/css/view-intake.css.
 */
import { html, useState, useEffect, useRef, useCallback } from "../vendor/preact-htm.module.js";
import { api, toast } from "./api.js";
import { Icon } from "./icons.js";
import { Empty, SkelRows } from "./ui.js";

/* Two timeouts api()'s 20s default is wrong for, named rather than inlined.
 * The model pass is one Anthropic call over a whole paste; the commit can write
 * sixty people, each with identifiers and a note. The page fetch derives its own
 * from the server's NC_INTAKE_WEB_TIMEOUT_SEC, so there is one number and the
 * browser always waits slightly longer than the fetch it is waiting on. */
const MODEL_TIMEOUT_MS = 120000;
const COMMIT_TIMEOUT_MS = 60000;
const LOOKUP_SLACK_MS = 8000;

/* Verdict → how the row reads. Colour is state: a refusal the server made about
 * your database is red, a row that needs something from you is gold, and the two
 * ordinary outcomes (a new person, a person you already have) are neutral. */
const VERDICT = {
  new: { label: "New person", tone: "" },
  matched: { label: "Already in your CRM", tone: "" },
  ambiguous: { label: "Matches more than one", tone: "warn" },
  // NC-361. The People page calls this list Hidden contacts; one list, one name.
  refused_excluded: { label: "In Hidden contacts", tone: "bad" },
  refused_deleted: { label: "You deleted this person", tone: "bad" },
};
const FIELDS = [
  ["name", "Name"],
  ["company", "Company"],
  ["position", "Position"],
  ["email", "Email"],
  ["phone", "Phone"],
  ["linkedin_url", "LinkedIn"],
  ["how_we_met", "How you met"],
  ["note", "Note"],
];
/* Which of the above come out of `proposal.identifiers` rather than
 * `proposal.fields` — they are match keys, not columns. */
const IDENT_FIELDS = new Set(["email", "phone"]);
/* `proposal.name.source` → the words a person reads. Mirrors intake.py's
 * SOURCE_WORDS; an unknown value falls back to itself with spaces. */
const SOURCE_WORDS = {
  linkedin_slug: "LinkedIn address", email_local: "email address",
  llm: "model", pasted_line: "pasted line", pasted: "paste",
};

const nPeople = (n) => `${n} ${n === 1 ? "person" : "people"}`;
const kb = (bytes) => `${((bytes || 0) / 1024).toFixed(1)} KB`;

/** The item `commit_batch` reads, built from one proposal plus whatever was
 *  edited on screen. Nothing else is sent — no ref, no verdict, no contact_id,
 *  because the server would throw all three away and a client that sends them
 *  reads as though it expects them to be honoured. */
function itemFor(p, edit) {
  const e = edit || {};
  const field = (key) => {
    if (key in e) return e[key];
    if (IDENT_FIELDS.has(key)) {
      const hit = (p.identifiers || []).find((i) => i.kind === key);
      return hit ? hit.value : "";
    }
    if (key === "note") return p.note || "";
    if (key === "name") return p.name.value || "";
    return (p.fields && p.fields[key] && p.fields[key].value) || "";
  };
  return {
    name: field("name"),
    name_confirmed: "name_confirmed" in e ? !!e.name_confirmed : !!p.name.confirmed,
    company: field("company"),
    position: field("position"),
    how_we_met: field("how_we_met"),
    linkedin_url: field("linkedin_url"),
    email: field("email"),
    phone: field("phone"),
    note: field("note"),
    origin: (p.provenance && p.provenance.origin) || "paste",
  };
}

/** What still stops this row being written, judged against the text on screen.
 *
 *  The two name refusals are RECOMPUTED here rather than read off the server's
 *  `blocked_by`, because that list is a verdict on the paste as it arrived: a
 *  row you have just confirmed would otherwise go on telling you to confirm it.
 *  Everything the server knows and this page cannot — a match on someone you
 *  excluded or deleted, an ambiguous match, a repeat of an earlier line — is
 *  kept exactly as it was written. */
function blockers(p, item) {
  const out = [];
  if (p.verdict === "new") {
    if (!item.name.trim()) {
      out.push("A new person needs a name. Type one, or nobody is created.");
    } else if (!item.name_confirmed) {
      // Where the name came from, in words (intake.py SOURCE_WORDS): the raw
      // value made this read "a guess from the linkedin slug".
      const src = SOURCE_WORDS[p.name.source] || (p.name.source || "paste").replace(/_/g, " ");
      out.push(`The name “${item.name}” is a guess from the ${src}. Confirm it below. `
               + "Nobody is created from a guessed name.");
    }
  }
  for (const b of p.blocked_by || []) {
    // Drop the server's own two name sentences, recomputed above. Matching on
    // its copy is a cosmetic filter only: if the wording changes, the worst case
    // is one duplicated sentence, never a refusal that stops being shown.
    if (b.includes("is a guess from the") || b.includes("needs a name")) continue;
    out.push(b);
  }
  return out;
}

/* ── The honesty surface: what the parse found, and what it dropped ─────────── */
function ReportPanel({ report }) {
  const acc = report.accounting || {};
  const lost = (acc.lines_unexplained || 0) + (acc.records_unexplained || 0);
  const declined = Object.keys(report.facts_declined_by_field || {});
  const spend = report.model && report.model.spend;
  const detail = report.skipped_detail || [];
  return html`
    <section class="ik-report">
      <div class="section-label">What the paste held</div>
      <div class="ik-nums">
        <span class="ik-num"><b>${report.would_create ?? 0}</b> to add</span>
        <span class="ik-num"><b>${report.would_enrich ?? 0}</b> already known</span>
        <span class="ik-num"><b>${report.blocked ?? 0}</b> refused</span>
        <span class="ik-num">${nPeople(report.records_parsed ?? 0)} read from${" "}
          ${report.lines_seen ?? 0} lines</span>
      </div>
      ${report.degraded && html`
        <p class="ik-line warn"><${Icon} name="info" /> ${report.degraded}</p>`}
      ${lost > 0 && html`
        <p class="ik-line bad"><${Icon} name="info" />
          ${lost} lines are unaccounted for. The parse lost something it did not
          report — treat the list below as incomplete.</p>`}
      ${/* The module's own sentences, verbatim. They are written in the same
            Simplified-Technical-English register as the rest of the app and they
            state every cap, refusal and degraded path; rewriting them here would
            create a second copy to drift. */ ""}
      <ul class="ik-sentences">
        ${(report.sentences || []).map((s, i) => html`<li key=${i}>${s}</li>`)}
      </ul>
      ${declined.length > 0 && html`
        <p class="ik-line muted">Facts were declined for ${declined.join(", ")}.
          Intake writes identity only, never a city, a topic, a role or a blurb.</p>`}
      ${report.linkedin_url_note && html`
        <p class="ik-line muted">${report.linkedin_url_note}</p>`}
      ${spend && html`
        <p class="ik-line muted">The model call cost $${(spend.usd ?? 0).toFixed(4)}${" "}
          — ${spend.input_tokens ?? 0} in, ${spend.output_tokens ?? 0} out,${" "}
          ${spend.model || "model"}.</p>`}
      ${detail.length > 0 && html`
        <details class="ik-dropped">
          <summary>${detail.length} ${detail.length === 1 ? "entry was" : "entries were"} not proposed</summary>
          ${detail.map((d, i) => html`
            <div class="ik-drop" key=${i}>
              <span class="ik-drop-where">${d.line ? `Line ${d.line}` : "—"}</span>
              <span class="grow">${[d.name || d.text, d.reason].filter(Boolean).join(" — ")}</span>
            </div>`)}
        </details>`}
    </section>`;
}

/* ── One person, as proposed, editable ─────────────────────────────────────── */
function ProposalRow({ p, item, blocked, edit, onEdit, skip, onSkip }) {
  const v = VERDICT[p.verdict] || { label: p.verdict, tone: "warn" };
  const needsName = p.verdict === "new" && !!item.name.trim() && !item.name_confirmed;
  const fieldNote = (key) => {
    if (IDENT_FIELDS.has(key)) {
      const hit = (p.identifiers || []).find((i) => i.kind === key);
      return hit ? hit.why : "";
    }
    if (key === "name" && p.name.existing) {
      return `Kept as “${p.name.existing}”. Intake never renames somebody it matched.`;
    }
    const f = p.fields && p.fields[key];
    return f && !f.writes ? f.why : "";
  };
  /* Whether typing in this field could change anything, which decides input vs
   * text. `commit_batch` fills EMPTY columns only and never renames somebody it
   * matched, so an editable box over a column that already has a value is a
   * control that cannot work — the NC-006 defect, and NC-036's "a row's job is
   * to report, not to ask". A row the server refused outright is all report. */
  const writable = (key) => {
    if (p.verdict === "new") return true;              // every column is empty
    if (p.verdict !== "matched") return false;         // refused: nothing is written
    if (key === "name") return false;                  // a match is never renamed
    if (key === "note") return true;                   // notes are append-only
    if (IDENT_FIELDS.has(key)) {
      const hit = (p.identifiers || []).find((i) => i.kind === key);
      return !hit || hit.writes !== false;
    }
    const f = p.fields && p.fields[key];
    return !f || f.writes !== false;
  };
  return html`
    <article class=${"ik-row" + (blocked.length ? " blocked" : "") + (skip ? " skipped" : "")}>
      <header class="ik-rhead">
        <span class=${"tag" + (v.tone ? " " + v.tone : "")}>${v.label}</span>
        <span class="ik-rname">${item.name || "No name"}</span>
        <span class="grow"></span>
        <button class="btn quiet mini" aria-pressed=${skip ? "true" : "false"}
          title=${skip ? "Put this person back in the batch" : "Leave this person out"}
          onClick=${() => onSkip(!skip)}>${skip ? "Include" : "Skip"}</button>
      </header>

      ${p.match && p.match.reason && html`<p class="ik-line muted">${p.match.reason}</p>`}

      ${blocked.map((b, i) => html`
        <p class=${"ik-line " + (p.verdict === "new" ? "warn" : "bad")} key=${i}>
          <${Icon} name="info" /> ${b}</p>`)}

      ${!skip && html`
        <div class="ik-fields">
          ${FIELDS.map(([key, label]) => {
            const note = fieldNote(key);
            const value = item[key];
            // A field with no value and nothing to say about it is not rendered:
            // eight empty inputs per person turns a review into a form.
            if (!value && !note) return null;
            if (!writable(key)) return html`
              <div class="ik-field" key=${key}>
                <span class="ik-flabel">${label}</span>
                <span class="ik-fval">${value || "—"}</span>
                ${note && html`<span class="ik-why">${note}</span>`}
              </div>`;
            return html`
              <label class="ik-field" key=${key}>
                <span class="ik-flabel">${label}</span>
                <input class="input" value=${value} aria-label=${label}
                  onInput=${(e) => onEdit(key, e.target.value)} />
                ${note && html`<span class="ik-why">${note}</span>`}
              </label>`;
          })}
        </div>`}

      ${!skip && needsName && html`
        <div class="ik-confirm">
          <button class="btn mini" onClick=${() => onEdit("name_confirmed", true)}>
            Yes, they are called “${item.name}”
          </button>
          <span class="ik-why">Correct the name above first if it is wrong. This is the
            one thing the server will not decide for you.</span>
        </div>`}
      ${!skip && p.verdict === "new" && item.name_confirmed && item.name.trim() && html`
        <p class="ik-line ok"><${Icon} name="check" /> The name is confirmed.${ ""}
          ${(edit && "name" in edit) ? " You typed it." : ""}</p>`}

      ${(p.warnings || []).map((w, i) => html`
        <p class="ik-line warn" key=${"w" + i}>${w}</p>`)}
      ${(p.facts_declined || []).length > 0 && html`
        <p class="ik-line muted">Not saved:${" "}
          ${p.facts_declined.map((d) => d.field).join(", ")}. ${p.facts_declined[0].why}</p>`}
    </article>`;
}

/* ── Links in the paste: opening one is a request to that site ─────────────── */
function LinkPanel({ urls, pages, webOn, busy, onLookup }) {
  if (!urls.length) return null;
  return html`
    <section class="ik-links">
      <div class="section-label">Links in your paste</div>
      ${!webOn && html`
        <p class="ik-line muted">Web lookup is off, so no link was opened. Turn on
          “Let Intake read a web page” in Settings to allow it. Pasting names,
          LinkedIn addresses and emails works fully without it.</p>`}
      ${urls.map((u) => {
        const page = pages.find((pg) => pg.url === u);
        return html`
          <div class="ik-link" key=${u}>
            <span class="ik-url">${u}</span>
            ${page
              ? html`<span class="ik-read">Read ${page.bytes} bytes from ${page.host}</span>`
              : webOn && html`
                <button class="btn mini" disabled=${!!busy}
                  onClick=${() => onLookup(u)}>Read this page</button>`}
          </div>`;
      })}
      ${pages.length > 0 && html`
        <p class="ik-line muted">
          ${pages.length} ${pages.length === 1 ? "page" : "pages"} were
          read and have NOT been sent to a model. This text is treated as information
          about people and never as an instruction. Press “Read it with the model” to
          use it.</p>`}
    </section>`;
}

/* ── What actually happened ────────────────────────────────────────────────── */
function CommitReceipt({ receipt, go }) {
  const acc = receipt.accounting || {};
  const outcomes = receipt.outcomes || [];
  return html`
    <section class=${"ik-receipt" + (receipt.wrote_anything ? "" : " nothing")}>
      <div class="section-label">${receipt.wrote_anything ? "Saved" : "Nothing was written"}</div>
      <ul class="ik-sentences">
        ${(receipt.sentences || []).map((s, i) => html`<li key=${i}>${s}</li>`)}
      </ul>
      ${(acc.unaccounted || 0) !== 0 && html`
        <p class="ik-line bad"><${Icon} name="info" /> ${acc.unaccounted} submitted${" "}
          ${acc.unaccounted === 1 ? "row" : "rows"} are unaccounted for in this receipt.</p>`}
      ${outcomes.length > 0 && html`
        <div class="ik-outcomes">
          ${outcomes.map((o) => html`
            <div class=${"ik-outcome" + (o.refused ? " bad" : "")} key=${o.index}>
              ${o.contact_id
                ? html`<button class="ik-olink" onClick=${() => go(`people/${o.contact_id}`)}>
                    ${o.name || "This person"}</button>`
                : html`<span class="ik-oname">${o.name || "No name"}</span>`}
              ${/* `said` is the server's sentence in words ("Saved company and
                    email."); the raw lists printed code names such as how_we_met
                    and identifier:email. The fallback serves an older engine. */ ""}
              <span class="grow">
                ${o.said || (o.refused
                  ? o.refused
                  : [(o.wrote || []).length ? `Wrote ${o.wrote.join(", ")}.` : "",
                     (o.kept || []).length ? `Kept ${o.kept.join(", ")}.` : ""]
                      .filter(Boolean).join(" ") || "Nothing to change.")}
              </span>
            </div>`)}
        </div>`}
    </section>`;
}

/* ── The view ──────────────────────────────────────────────────────────────── */
export function Intake({ go }) {
  const [status, setStatus] = useState(null);
  const [text, setText] = useState("");
  const [plan, setPlan] = useState(null);       // {proposals, report, allowed_lookup_urls}
  const [edits, setEdits] = useState({});       // ref -> {field: value}
  const [skips, setSkips] = useState({});       // ref -> true
  const [pages, setPages] = useState([]);       // fetched page text, never auto-used
  const [receipt, setReceipt] = useState(null);
  const [draft, setDraft] = useState(null);     // a parked draft found at mount
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [confirming, setConfirming] = useState(false);
  const pasteRef = useRef(null);

  useEffect(() => {
    let live = true;
    api("/api/intake/status")
      .then((d) => { if (live) setStatus(d); })
      .catch((e) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, []);

  // The parked draft. Loaded once, offered rather than restored: silently
  // reopening a week-old review would hide that this page also does something
  // else. `load_pending` answers {} when there is none and {error: …} when the
  // blob is unreadable, so both are states and neither is a crash.
  useEffect(() => {
    let live = true;
    api("/api/intake/batch")
      .then((d) => {
        const b = d.batch || {};
        if (!live) return;
        if (b.error) { setErr(b.error); return; }
        if ((b.proposals || []).length) setDraft(b);
      })
      .catch(() => { /* a missing draft is not worth a message */ });
    return () => { live = false; };
  }, []);

  // The user came here to paste, so the caret starts in the box. Not on every
  // render — only when the paste screen is the one on show.
  useEffect(() => { if (!plan && !receipt) pasteRef.current?.focus(); }, [plan, receipt]);

  useEffect(() => {
    if (!confirming) return;
    const onKey = (e) => { if (e.key === "Escape") setConfirming(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirming]);

  const caps = (status && status.settings) || {};
  const pasteMax = caps.NC_INTAKE_PASTE_MAX_CHARS || 0;
  const over = pasteMax ? Math.max(0, text.length - pasteMax) : 0;
  const lookupTimeout = (caps.NC_INTAKE_WEB_TIMEOUT_SEC || 15) * 1000 + LOOKUP_SLACK_MS;

  const setEdit = useCallback((ref, key, value) => {
    setEdits((prev) => {
      const row = { ...(prev[ref] || {}), [key]: value };
      // Confirming a name confirms the string that was on screen when it was
      // ticked. Typing after that changes the string, so the tick no longer
      // refers to anything a human read and has to come off — which is exactly
      // what `_confirmed_name` trusts the flag to mean.
      if (key === "name") row.name_confirmed = false;
      return { ...prev, [ref]: row };
    });
  }, []);

  const reset = () => {
    setPlan(null); setReceipt(null); setEdits({}); setSkips({});
    setPages([]); setSaved(""); setConfirming(false); setErr("");
  };

  /** Back to an EMPTY paste box, once a list has been saved. `reset` keeps the text,
   *  which is right for "Start again" (you are re-reading the same paste), but after
   *  a save the people in it are in the CRM now: leaving them in the box made the next
   *  read propose the people just saved as matches for people already there. */
  const pasteAnother = () => { reset(); setText(""); };

  /** Read the paste. `parse` is the offline path and needs no key; `propose` is
   *  the model path and returns the identical shape, so there is one renderer
   *  and the no-key path is not a second-class citizen. */
  const read = async (which) => {
    if (!text.trim() || busy) return;
    setBusy(which); setErr(""); setReceipt(null); setConfirming(false);
    try {
      const d = which === "propose"
        ? await api("/api/intake/propose", {
            method: "POST", timeoutMs: MODEL_TIMEOUT_MS,
            // Only pages this server fetched and handed back, and only their
            // host and text. A page is prompt material, never a record on its
            // own — `records_from_model_output` re-parses everything anyway.
            body: JSON.stringify({ text, pages: pages.map((p) => ({ host: p.host, text: p.text })) }),
          })
        : await api("/api/intake/parse", { method: "POST", body: JSON.stringify({ text }) });
      setPlan({ proposals: d.proposals || [], report: d.report || {},
                allowed_lookup_urls: d.allowed_lookup_urls || [] });
      setEdits({}); setSkips({}); setSaved("");
    } catch (e) { setErr(e.message); }
    setBusy("");
  };

  /** Fetch ONE address out of the paste. The paste itself goes with it: the
   *  server re-derives the allowlist from the text rather than taking this
   *  component's word for which addresses were in it (NC-148). */
  const lookup = async (url) => {
    if (busy) return;
    setBusy("lookup"); setErr("");
    try {
      const d = await api("/api/intake/lookup", {
        method: "POST", timeoutMs: lookupTimeout,
        body: JSON.stringify({ url, text }),
      });
      // `url` is kept locally as the key for this row. It is the user's own
      // pasted text, which is already on screen — the rule the server keeps is
      // that IT never echoes a URL, because a link can be a credential.
      setPages((ps) => [...ps.filter((p) => p.url !== url), { ...d.page, url }]);
      toast(`Read ${d.page.host}`);
    } catch (e) { setErr(e.message); }
    setBusy("");
  };

  const park = async () => {
    if (!plan || busy) return;
    setBusy("park"); setErr("");
    try {
      const d = await api("/api/intake/batch", {
        method: "POST",
        body: JSON.stringify({ batch: { text, proposals: plan.proposals, report: plan.report,
                                        allowed_lookup_urls: plan.allowed_lookup_urls,
                                        pages, edits, skips } }),
      });
      setSaved(`${(d.sentences || ["The draft was saved."])[0]} ${kb(d.bytes)}.`);
    } catch (e) { setErr(e.message); }
    setBusy("");
  };

  const discard = async () => {
    setBusy("discard"); setErr("");
    try {
      const d = await api("/api/intake/batch", { method: "DELETE" });
      setDraft(null); setSaved("");
      toast((d.sentences || ["The draft was discarded."])[0]);
    } catch (e) { setErr(e.message); }
    setBusy("");
  };

  const resume = () => {
    setText(draft.text || "");
    setPlan({ proposals: draft.proposals || [], report: draft.report || {},
              allowed_lookup_urls: draft.allowed_lookup_urls || [] });
    setEdits(draft.edits || {}); setSkips(draft.skips || {});
    setPages(draft.pages || []); setDraft(null); setErr("");
  };

  // Every row, with its live item and whatever still blocks it. Computed once
  // per render so the count in the confirm strip and the rows themselves can
  // never disagree — two components each counting the same thing is the exact
  // incoherence DESIGN_DIRECTION §1 names.
  const rows = ((plan && plan.proposals) || []).map((p) => {
    const item = itemFor(p, edits[p.ref]);
    return { p, item, blocked: blockers(p, item), skip: !!skips[p.ref] };
  });
  const live = rows.filter((r) => !r.skip);
  const willCreate = live.filter((r) => r.p.verdict === "new" && !r.blocked.length).length;
  const willEnrich = live.filter((r) => r.p.verdict === "matched" && !r.blocked.length).length;
  const willRefuse = live.filter((r) => r.blocked.length).length;
  const skipped = rows.length - live.length;
  const willWrite = willCreate + willEnrich;

  const commit = async () => {
    if (busy || !live.length) return;
    setBusy("commit"); setErr("");
    try {
      // `confirm` is the JSON boolean true, never a string: the server's gate is
      // bool() on whatever arrives, so "false" would confirm the batch.
      const d = await api("/api/intake/commit", {
        method: "POST", timeoutMs: COMMIT_TIMEOUT_MS,
        body: JSON.stringify({ batch: live.map((r) => r.item), confirm: true }),
      });
      setReceipt(d);
      setConfirming(false);
      if (d.wrote_anything) { setSaved(""); }
    } catch (e) { setErr(e.message); setConfirming(false); }
    setBusy("");
  };

  const phase = receipt && receipt.wrote_anything ? "done" : (plan ? "review" : "paste");
  const stepClass = (name) => "ik-phase" + (phase === name ? " on" : "");

  return html`
    <div class="intake-view">
      <header class="ik-head">
        <h1 class="ik-title">Paste people in</h1>
        <p class="ik-sub">One person per line — a name, an email signature, a LinkedIn
          address. Nothing is saved until you have read the list and confirmed it.</p>
        <div class="ik-phases" aria-label="Where you are">
          <span class=${stepClass("paste")}>1 · Paste</span>
          <span class=${stepClass("review")}>2 · Review</span>
          <span class=${stepClass("done")}>3 · Saved</span>
        </div>
      </header>

      ${err && html`<p class="ik-line bad" role="alert"><${Icon} name="info" /> ${err}</p>`}

      ${phase === "paste" && html`
        ${status === null && !err && html`<${SkelRows} n=${3} />`}

        ${draft && html`
          <div class="ik-draft">
            <span class="grow">A draft of ${nPeople((draft.proposals || []).length)} is
              parked from last time. Nobody in it has been added.</span>
            <button class="btn" onClick=${resume}>Open the draft</button>
            <button class="btn quiet" disabled=${!!busy} onClick=${discard}>Discard it</button>
          </div>`}

        <textarea class="input ik-paste" rows="12" ref=${pasteRef}
          aria-label="People to add, one per line"
          placeholder=${"Ada Lovelace — Analytical Engines, London\nlinkedin.com/in/some-profile\nGrace Hopper, COBOL, grace@example.com, +46 70 174 06 05"}
          value=${text} onInput=${(e) => setText(e.target.value)} />

        <div class="ik-below">
          <span class=${"ik-count" + (over ? " warn" : "")}>
            ${text.length}${pasteMax ? ` / ${pasteMax}` : ""} characters${ ""}
            ${over ? ` · ${over} past the limit will not be read` : ""}
          </span>
          <span class="grow"></span>
          <div class="ik-acts">
            <button class="btn primary" disabled=${!text.trim() || !!busy}
              onClick=${() => read("parse")}>
              ${busy === "parse" ? "Reading…" : "Read this list"}
            </button>
            ${status && status.llm && html`
              <button class="btn" disabled=${!text.trim() || !!busy}
                onClick=${() => read("propose")}>
                ${busy === "propose" ? "Asking the model…" : "Read it with the model"}
              </button>`}
          </div>
        </div>

        ${status && html`
          <section class="ik-caps">
            <div class="section-label">What this can do on this Mac</div>
            <ul class="ik-sentences">
              ${(status.sentences || []).map((s, i) => html`<li key=${i}>${s}</li>`)}
            </ul>
            ${/* NC-365. This printed the column and match-key names raw
                  ("It matches on name_norm, phone_last9") and then every limit's
                  setting name, "read from the environment at call time". What it
                  saves and how it matches are now sentences in the list above,
                  from the server; what is left here is the one limit a person
                  meets and what to do about it. */ ""}
            <p class="ik-line muted">
              Up to ${nPeople(caps.NC_INTAKE_MAX_PEOPLE || 0)} per paste. Paste a
              longer list in parts.${ ""}
              ${status.llm ? ` The model is ${status.model}.` : ""}
            </p>
          </section>`}`}

      ${phase === "review" && html`
        ${receipt && html`<${CommitReceipt} receipt=${receipt} go=${go} />`}
        <${ReportPanel} report=${plan.report} />
        <${LinkPanel} urls=${plan.allowed_lookup_urls} pages=${pages}
          webOn=${!!(status && status.web_lookup)} busy=${busy} onLookup=${lookup} />

        ${rows.length === 0
          ? html`
            <${Empty} title="Nobody was found in that text"
              hint="A line needs a name, a LinkedIn address, an email or a phone number.">
              Nothing was written. Go back and paste a list with one person per line.
            </${Empty}>`
          : rows.map((r) => html`
            <${ProposalRow} key=${r.p.ref} p=${r.p} item=${r.item} blocked=${r.blocked}
              edit=${edits[r.p.ref]} skip=${r.skip}
              onEdit=${(k, v) => setEdit(r.p.ref, k, v)}
              onSkip=${(on) => setSkips((s) => ({ ...s, [r.p.ref]: on }))} />`)}

        <div class="ik-bar">
          <button class="btn quiet" disabled=${!!busy} onClick=${reset}>Start again</button>
          <button class="btn" disabled=${!!busy} onClick=${park}>Save the draft</button>
          <button class="btn quiet" disabled=${!!busy} onClick=${discard}>Discard the draft</button>
          <span class="grow">${saved || (!willWrite && rows.length
            ? "Every row above is refused or still needs a name, so there is "
              + "nothing to save yet."
            : "")}</span>
          ${/* Disabled when nothing would be written, rather than offering a
                confirm that resolves to "0 people added" — a control that cannot
                do what its label says is the NC-006 defect. */ ""}
          ${!confirming
            ? html`
              <button class="btn primary" disabled=${!willWrite || !!busy}
                onClick=${() => setConfirming(true)}>
                ${willWrite ? `Save ${nPeople(willWrite)}` : "Nothing to save"}
              </button>`
            : html`
              <div class="ik-confirm-strip" role="group" aria-label="Confirm this batch">
                <span class="ik-cq">
                  Add ${nPeople(willCreate)} and fill empty fields on${" "}
                  ${nPeople(willEnrich)}.${ ""}
                  ${willRefuse ? ` ${willRefuse} will be refused, with the reasons above.` : ""}
                  ${skipped ? ` ${skipped} skipped ${skipped === 1 ? "row is" : "rows are"} not sent.` : ""}
                </span>
                <button class="btn primary" autofocus disabled=${!!busy} onClick=${commit}>
                  ${busy === "commit" ? "Saving…" : "Confirm"}
                </button>
                <button class="btn quiet" onClick=${() => setConfirming(false)}>Cancel</button>
              </div>`}
        </div>`}

      ${phase === "done" && html`
        <${CommitReceipt} receipt=${receipt} go=${go} />
        <div class="ik-bar">
          <button class="btn primary" onClick=${pasteAnother}>Paste another list</button>
          <button class="btn" onClick=${() => go("people")}>Open People</button>
        </div>`}
    </div>`;
}
