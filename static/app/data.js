// The Data page (NC-088), rebuilt, and since NC-236 the only one.
//
// Three sections:
//
//   Needs you      only what the app cannot do without you, cheapest to fix first
//   Your sources   every source, always: the ones you use first, then the rest
//                  cheapest to switch on first — an unused source is the app's
//                  own menu of what is possible — and the app's own work last
//   This install   read-only figures about the whole install, and the export
//
// Every word and number on it comes from one endpoint, GET /api/data/ledger
// (crm/api_ledger.py). The old page asked eight and worked out on its own
// side what needed you, which could put two true numbers side by side that
// disagreed. This page decides nothing: it draws the answer, and after anything
// you do on it, it asks again (NC-125 — there is no refresh button to press).
//
// It replaced the old page (NC-220). The parts the old page lent it moved here
// with it: the import runner, a run's result, what a source added, and a
// duplicates group.
import { html, useState, useEffect, useCallback, useRef } from "../vendor/preact-htm.module.js";
import { api, toast, relDate, CHANNEL_LABEL, importTimeoutMs, noteImportCeiling } from "./api.js";
import { Avatar, SkelRows, openTarget, PathBox, MergeModal } from "./ui.js";
import { Icon, SRC_ICON } from "./icons.js";
import { FdaPanel } from "./fda.js";
import { GmailSetup } from "./gmail.js";
import { Section, LedgerRow, RowDetail, NeedsRow, StatusRow, CredentialBox,
         TextSetting, Figure, Switch, Help, useOpenRow, useRowFromLink } from "./ledger.js";

// The shape for each row is chosen here, so the API stays free of presentation
// (the same rule icons.js states for dates).
const ROW_ICON = { ...SRC_ICON, helper: "sync", linkedin_connections: "linkedin",
                   linkedin_messages: "linkedin", worked_out: "chart" };

/** How long ago, in words: "12 minutes ago", "3 hours ago", "yesterday". The
 *  seconds come from the server, which knows the stamp's time zone. */
function agoWords(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s)) return "";
  if (s < 90) return "just now";
  const m = Math.round(s / 60), h = Math.round(s / 3600), d = Math.round(s / 86400);
  if (s < 3600) return `${m} minutes ago`;
  if (s < 36 * 3600) return h === 1 ? "an hour ago" : `${h} hours ago`;
  if (d < 2) return "yesterday";
  return `${d} days ago`;
}

// What an open row's "Now" block says about when it last ran. It used to round
// the time to the day, so a source that runs every two minutes read "Last ran
// today", and a week-old one read "7d ago" (NC-217).
function lastRun(detail) {
  if (!detail.last_run) return "";
  const words = agoWords(detail.last_run_seconds);
  return `Last imported ${words || relDate(detail.last_run.slice(0, 10))}.`;
}

// What the files in the data folder are, largest first, in plain names.
const PART_LIMIT = 4;

// Server sentences are written to stand alone, some without a full stop; the
// "Now" block runs several together.
const stop = s => (/[.!?]$/.test(s) ? s : `${s}.`);

function size(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6).toLocaleString()} MB`;
  return `${Math.max(1, Math.round(n / 1e3)).toLocaleString()} KB`;
}


// ── Moved from the old Data page (dataview.js) with the switch-over, NC-236 ──

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// `form`: the act's own extra fields, sent with the run. NC-372: the LinkedIn
// profile facts row's Read shared history is that source's run with
// {extras: true}; the three fields every run sends cannot be overridden by it.
export function useImportRunner(sourceKey, label, onRan, form = null) {
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState(null);
  const [ok, setOk] = useState(null);
  const [receipts, setReceipts] = useState([]);
  const [added, setAdded] = useState(0);  // new people this import placed
  const run = async (path, dryRun) => {
    setRunning(true); setLog(null); setReceipts([]); setAdded(0);
    try {
      // NC-176: outlast the server's own ceiling rather than giving up at 20s
      // while the import goes on and succeeds without anyone being told.
      const d = await api("/api/import/run", { method: "POST", timeoutMs: importTimeoutMs(),
        body: JSON.stringify({ ...(form || {}), source: sourceKey, path, dry_run: !!dryRun }) });
      setLog(d.output || "(no output)");
      setOk(!!d.ok);
      // the free offline facts pass runs automatically after an import, so its
      // chips join the import's own
      setReceipts([...(d.receipts || []), ...(d.enriched || [])]);
      setAdded(d.newly_added || 0);
      // a preview changed nothing, so don't claim it did or refresh coverage
      if (d.ok) {
        if (d.dry_run) { toast(`${label} preview — nothing changed`); }
        else { toast(`${label} done`); onRan(); }
      }
    } catch (e) { setLog(e.message); setOk(false); }
    setRunning(false);
  };
  return { running, log, ok, receipts, added, run };
}

// A count is never rendered as a bare 0 or a blank. Three cases, three
// sentences: nothing yet, cannot be counted at all, or the number.
const provCount = (k) => (
  k.count === null || k.count === undefined ? "Not known"
    : k.count === 0 ? "None yet"
    : k.count.toLocaleString());

/** What one source brought, with every caveat the schema forces on it.
 *
 *  Weight, not colour, marks a soft number: a count the column can answer
 *  exactly is 600 in --ink, and one that is only the current winner, only the
 *  record's creator, or shared with another importer is 400 in --muted, with the
 *  reason stated underneath. Colour on this page belongs to the staleness ramp
 *  (DESIGN_DIRECTION §6), and "this figure is approximate" is not a state on it. */
export function Provenance({ source, label }) {
  const [res, setRes] = useState({ status: "loading" });
  useEffect(() => {
    let alive = true;
    setRes({ status: "loading" });
    api(`/api/provenance?source=${encodeURIComponent(source)}`)
      .then(d => alive && setRes({ status: "ok", panel: (d.panels || [])[0] || null }))
      .catch(e => alive && setRes({ status: "err", message: e.message || "" }));
    return () => { alive = false; };
  }, [source]);

  if (res.status === "loading") {
    return html`<div class="prov"><${SkelRows} n=${3} h=${20} /></div>`;
  }
  if (res.status === "err" || !res.panel) {
    return html`
      <div class="prov">
        <p class="prov-note">Couldn't read what ${label} brought just now.
          Nothing already imported is affected.</p>
      </div>`;
  }
  const p = res.panel;
  if (p.records_nothing) {
    return html`<div class="prov"><p class="prov-note">${p.note}</p></div>`;
  }
  return html`
    <div class="prov">
      ${p.kinds.map(k => html`
        <div class="prov-kind" key=${k.kind}>
          <div class="prov-line">
            <span class="prov-label">${k.label}</span>
            <span class=${"prov-count" + (k.certainty === "exact" ? " firm" : "")}>
              ${provCount(k)}</span>
            ${k.count > 0 && k.of_total > 0 && html`
              <span class="prov-of">of ${k.of_total.toLocaleString()}</span>`}
            ${k.count > 0 && k.detail && html`<span class="prov-detail">${k.detail}</span>`}
          </div>
          ${k.note && html`<p class="prov-note">${k.note}</p>`}
        </div>`)}
      ${p.not_recorded_note && html`<p class="prov-note">${p.not_recorded_note}</p>`}
    </div>`;
}

/** Result of a finished run — verb-count chips ("Archived 120") with the raw
    output tucked behind a disclosure when the log distilled cleanly;
    otherwise the raw log as before. Failures always show the log expanded.
    Refusals are part of the receipt (see crm/ics_import.report): nothing this
    page renders may quietly drop what an importer reported. */
export function RunResult({ ok, log, receipts, added, go }) {
  // close the loop: after an import placed new people, offer to go describe
  // them (Today's deck) instead of leaving the user on a wall of log text
  const bridge = ok && added > 0 && go
    ? html`<button class="btn primary mini bridge" onClick=${() => go("")}>
        Describe ${added.toLocaleString()} new ${added === 1 ? "person" : "people"}<${Icon} name="chevronRight" /></button>`
    : null;
  if (ok && receipts.length > 0) return html`
    <div class="run-result" aria-live="polite">
      ${bridge}
      <div class="verb-chips">
        ${receipts.map(r => html`<span class="verb-chip" key=${r.verb}>${r.verb} <b>${(r.count || 0).toLocaleString()}</b></span>`)}
      </div>
      <details class="log-details">
        <summary>details</summary>
        <pre class="src-log ok">${log}</pre>
      </details>
    </div>`;
  return html`
    <div class="run-result" aria-live="polite">
      ${bridge}
      <pre class=${"src-log " + (ok ? "ok" : "err")}>${log}</pre>
    </div>`;
}

export function DupGroup({ group, onMerge }) {
  const msgs = m => Object.values(m.counts || {}).reduce((a, b) => a + (b || 0), 0);
  const fill = m => [m.company, m.role_type, m.home_metro, m.blurb].filter(Boolean).length + (m.topics || []).length;
  // default 'keep' to the most-filled record (tiebreak: most messages) so a
  // bare row can't silently win over the enriched one and lose its facts
  const best = [...group].sort((a, b) => fill(b) - fill(a) || msgs(b) - msgs(a))[0];
  const [keep, setKeep] = useState(best?.id ?? group[0]?.id);
  const [confirming, setConfirming] = useState(false);
  const kept = group.find(g => g.id === keep);
  return html`
    <div class="dup-group">
      ${group.map(m => html`
        <label key=${m.id} class="dup-opt">
          <input type="radio" checked=${keep === m.id} onChange=${() => { setKeep(m.id); setConfirming(false); }} />
          <${Avatar} c=${m} size=${26} />
          <div class="grow">
            <div class="dup-name">${m.name}</div>
            <div class="dup-meta">${[m.role_type, m.home_metro, m.company, (m.topics || []).slice(0, 3).join(", "), m.source, m.last && ("last " + relDate(m.last))].filter(Boolean).join(" · ")}</div>
          </div>
        </label>`)}
      <div class="dup-foot">
        ${confirming
          ? html`
            <span class="dup-ask">Fold ${plural(group.length - 1, "record")} into “${kept?.name}”? The facts move across. The extra records are deleted.</span>
            <button class="btn quiet mini" onClick=${() => setConfirming(false)}>Cancel</button>
            <button class="btn primary mini" onClick=${() => { setConfirming(false); onMerge(group, keep); }}>Confirm merge</button>`
          : html`<button class="btn primary mini" onClick=${() => setConfirming(true)}>Merge into selected</button>`}
      </div>
    </div>`;
}

/** Import a file, or run a source that needs none. One verb, Import, for every
 *  source — they all go to the one endpoint, and how often a source runs by
 *  itself is a fact in its row, not a second button. */
function ImportAct({ act, label, onRan, go, onResult = null }) {
  const { running, log, ok, receipts, added, run } = useImportRunner(act.source, label, onRan, act.form);
  // In Needs you, a finished import removes its own need, and the result went
  // with it (NC-216). There the page keeps the result instead of this control.
  useEffect(() => {
    if (onResult && log !== null && !running) onResult({ label, ok, log, receipts, added });
  }, [log, running]);
  const [path, setPath] = useState(act.path || "");
  const [note, setNote] = useState("");
  const pick = async () => {
    try {
      const d = await api("/api/import/pick-file", { method: "POST",
        body: JSON.stringify({ kind: act.pick }) });
      // A path, a cancel (say nothing), or a picker that could not open (say so).
      if (d.path) { setPath(d.path); setNote(""); }
      else if (d.error) setNote(d.error);
    } catch (e) { setNote(e.message); }
  };
  const start = (dryRun) => {
    if (act.needs_path && !path.trim()) {
      setNote(act.folder ? "Choose a folder, or paste its path into the box."
                         : "Choose a file, or paste its path into the box.");
      return;
    }
    setNote("");
    run(path, dryRun);
  };
  return html`
    <div class="lg-import">
      <div class="lg-controls">
        ${act.needs_path && act.pick && html`
          <button class="btn" onClick=${pick}>${act.folder ? "Choose folder…" : "Choose file…"}</button>`}
        ${act.needs_path && html`<${PathBox} kind=${act.folder ? "folder" : "file"} path=${path}
          onPath=${v => { setPath(v); setNote(""); }} />`}
        ${!act.needs_path && act.path && (act.path_says
          ? html`<span class="lg-path-says" title=${act.path}>${act.path_says}</span>`
          : html`<code class="lg-path" title=${act.path}>${act.path}</code>`)}
        ${act.preview && html`
          <button class="btn" disabled=${running} title="Show what would change. Change nothing."
            onClick=${() => start(true)}>Preview</button>`}
        <button class=${act.verb === "Import this one instead" ? "btn" : "btn primary"}
          disabled=${running} onClick=${() => start(false)}>
          ${running ? (act.form ? "Working…" : "Importing…") : (act.verb || "Import")}</button>
      </div>
      ${note && html`<div class="field-note" role="status">${note}</div>`}
      ${log !== null && !onResult && html`<${RunResult} ok=${ok} log=${log} receipts=${receipts}
        added=${added} go=${go} />`}
    </div>`;
}

/** Every control a row or a need can carry, drawn from what the server sent. */
function Acts({ acts, label, onChanged, go, openRow, onResult = null }) {
  return (acts || []).map((a, i) => {
    const key = `${a.kind}-${a.key || a.target || a.source || ""}-${i}`;
    switch (a.kind) {
      case "import":
        return html`<${ImportAct} key=${key} act=${a} label=${label} onRan=${onChanged} go=${go}
          onResult=${onResult} />`;
      case "switch":
        // The server sent this, and nothing drew it: the calendar rows' "switch
        // downloading on" had no control on this page (NC-215).
        return html`<div class="lg-switch-act" key=${key}>
          <span class="lg-switch-label">${a.label}</span>
          <${Switch} act=${a} onSaved=${onChanged} /></div>`;
      case "open":
        return html`<button class="btn" key=${key} onClick=${() => openTarget(a.target)}>
          <${Icon} name="external" /> ${a.label}</button>`;
      case "secret":
        return html`<${CredentialBox} key=${key} act=${a} onSaved=${onChanged} />`;
      case "text":
        return html`<${TextSetting} key=${key} act=${a} onSaved=${onChanged} />`;
      case "helper":
        return html`<div class="lg-embed" key=${key}><${FdaPanel} onChanged=${onChanged} heading="h4" /></div>`;
      case "gmail":
        return html`<div class="lg-embed" key=${key}><${GmailSetup} onChanged=${onChanged} /></div>`;
      case "settings":
        return html`<button class="btn" key=${key}
          onClick=${() => go(a.row ? `settings?row=${a.row}` : "settings")}>${a.label}</button>`;
      case "row":
        return html`<button class="btn" key=${key} onClick=${() => openRow(a.row)}>${a.label}</button>`;
      default:
        return null;
    }
  });
}

/** What a source put on people, fetched only when asked for. */
function Record({ source, label }) {
  const [shown, setShown] = useState(false);
  if (!shown) {
    return html`<button class="btn ghost" onClick=${() => setShown(true)}>
      Show what ${label} added</button>`;
  }
  return html`<${Provenance} source=${source} label=${label} />`;
}

/** NC-231. The three passes the app runs by itself, folded into one row: each
 *  one's state and last run under Now, and what each added under its own button. */
function Parts({ parts }) {
  return html`<ul class="lg-parts">${parts.map(p => html`
    <li key=${p.key}><b>${p.label}.</b>${" "}${[p.in_use ? p.state.says : (p.state.says || "Not yet"),
      p.last_run_seconds != null ? `Last ran ${agoWords(p.last_run_seconds)}` : "", p.have]
      .filter(Boolean).map(stop).join(" ")}</li>`)}</ul>`;
}

function SourceRow({ row, open, onToggle, onChanged, go, openRow }) {
  const d = row.detail || {};
  if (d.parts) {
    return html`
      <${LedgerRow} id=${`lg-row-${row.key}`} icon=${ROW_ICON[row.key]} name=${row.label}
        gives=${row.gives} have=${row.have.says} note=${""} noteTone=${row.state.tone}
        takes=${row.takes} cost=${row.cost} tone=${row.state.tone} when=${row.when || ""}
        open=${open} onToggle=${onToggle}>
        <${RowDetail}
          now=${html`<${Parts} parts=${d.parts} />`}
          how=${d.help && html`<${Help} text=${d.help} />`}
          record=${row.in_use && html`<div class="lg-acts">${d.parts
            .filter(p => p.in_use && p.provenance).map(p => html`
              <${Record} key=${p.key} source=${p.provenance} label=${p.label} />`)}</div>`} />
      <//>`;
  }
  // A state worth a second line under "You have now": anything but plain
  // working or plain off, whose dot already says it.
  const note = ["blocked", "behind", "paused", "na", "ready"].includes(row.state.tone)
    ? row.state.says : "";
  // A row that is off must not describe itself as running: "Copies every couple
  // of minutes" under Now is wrong for a helper that is not installed.
  // Nor say one thing twice ("Starts by itself after your first import. Once it
  // is on: runs after an import…"), nor "Once it is on: only when you press
  // Import", which does not parse (NC-217).
  const working = row.in_use && row.state.tone !== "paused";
  const runs = !d.runs || d.runs === row.state.says ? ""
    : working || /^Only when/.test(d.runs) ? d.runs
    : row.state.says ? ""
    : `Once it is on: ${d.runs.charAt(0).toLowerCase()}${d.runs.slice(1)}`;
  // The setup steps are for setting it up. On a row that works they fold away,
  // so what it added is not pushed below the fold every time (NC-217).
  const how = d.help && (row.in_use && row.state.tone === "ok"
    ? html`<details class="lg-how-fold"><summary>Show how it was set up</summary>
        <${Help} text=${d.help} /></details>`
    : html`<${Help} text=${d.help} />`);
  // NC-238: the server's own extra sentences (LinkedIn's five-way split, when
  // the newest message arrived) come last, already whole sentences.
  const now = [row.in_use ? row.state.says : (row.state.says || "Off"), runs,
               lastRun(d), row.in_use ? d.last_run_caveat : "", ...(d.now || [])]
    .filter(Boolean).map(stop);
  // "What it added" is offered on a row not used yet too, where it says "None
  // yet", as the old page did (NC-239).
  return html`
    <${LedgerRow} id=${`lg-row-${row.key}`} icon=${ROW_ICON[row.key]} name=${row.label}
      gives=${row.gives} have=${row.have.says} note=${note} noteTone=${row.state.tone}
      takes=${row.takes} cost=${row.cost} tone=${row.state.tone} when=${row.when || ""}
      open=${open} onToggle=${onToggle}>
      <${RowDetail}
        now=${now.length > 0 && html`<p>${now.join(" ")}</p>`}
        act=${(d.acts || []).length > 0 && html`<div class="lg-acts">
          <${Acts} acts=${d.acts} label=${row.label} onChanged=${onChanged} go=${go}
            openRow=${openRow} /></div>`}
        how=${how}
        other=${d.manual && html`<p>${d.manual}</p>`}
        record=${d.provenance && html`<${Record} source=${d.provenance}
          label=${row.label} />`} />
    <//>`;
}

// What a ⌘K command or a menu item asks of this page once it is open:
// #data?dup=1 shows the possible duplicates, ?merge=1 opens the merge box, and
// ?export=1 brings the backup into view (File ▸ Export a Backup…, ⌘⇧E).
const ASKS = ["dup", "merge", "export"];
const ASK_TARGET = { dup: "lg-dups", merge: "lg-dups", export: "lg-export" };

/** The ask a link carries, counted, so the same command pressed twice acts
 *  twice (each press carries its own number, main.js ONE_SHOT_PARAMS). Brought
 *  into view once the page has an answer, and again once the scan has settled,
 *  since the scan can add needs above it. */
function useAskFromLink(params, ready, settled) {
  const link = params?.toString?.() || "";
  const [ask, setAsk] = useState({ what: "", n: 0 });
  const done = useRef("");
  useEffect(() => {
    if (!ready || done.current === link) return;
    done.current = link;
    const what = ASKS.find(k => params?.get?.(k)) || "";
    if (what) setAsk(a => ({ what, n: a.n + 1 }));
  }, [ready, link]);
  useEffect(() => {
    if (!ask.what) return;
    requestAnimationFrame(() => {
      const el = document.getElementById(ASK_TARGET[ask.what]) || document.getElementById("lg-install");
      if (!el) return;
      const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "center" });
      if (ask.what === "export") el.querySelector(".btn")?.focus({ preventScroll: true });
    });
  }, [ask.n, settled]);
  return ask;
}

/** NC-228: the duplicates finder, moved here from the old page's upkeep fold,
 *  as one line of This install. */
function PossibleDuplicates({ count, onMerged, ask }) {
  const [groups, setGroups] = useState(null);
  const [open, setOpen] = useState(false);
  const [manual, setManual] = useState(false);
  const load = useCallback(async () => {
    try { setGroups((await api("/api/duplicates")).groups || []); } catch { setGroups([]); }
  }, []);
  // Asked for from ⌘K or the Go menu: show the groups, and for "Merge two
  // people" go straight to the box rather than make you find its button.
  useEffect(() => {
    if (ask.what !== "dup" && ask.what !== "merge") return;
    if (ask.what === "merge") setManual(true);
    else if (count > 0) { setOpen(true); load(); }
  }, [ask.n]);
  const merge = async (group, keepId) => {
    try {
      for (const m of group) {
        if (m.id !== keepId) {
          await api("/api/merge", { method: "POST",
            body: JSON.stringify({ keep_id: keepId, dup_id: m.id }) });
        }
      }
      toast("Merged");
      load(); onMerged?.();
    } catch (e) { toast(e.message); }
  };
  const says = count == null ? "Could not be checked just now"
    : count === 0 ? "None found"
    : `${count.toLocaleString()} ${count === 1 ? "group" : "groups"} of people who may be the same person`;
  return html`
    <div class="lg-dups" id="lg-dups">
      <${StatusRow} label="Possible duplicates" value=${html`<b>${says}</b>`}
        detail="Found by identical names, a shared phone number or a shared email address. Merging cannot be undone." />
      <div class="lg-acts">
        ${count > 0 && html`<button class="btn" aria-expanded=${open ? "true" : "false"}
          onClick=${() => { setOpen(o => !o); if (!open && groups === null) load(); }}>
          ${open ? "Hide them" : "Review them"}</button>`}
        <button class="btn ghost" onClick=${() => setManual(true)}>Merge two people by name…</button>
      </div>
      ${manual && html`<${MergeModal} onClose=${() => setManual(false)}
        onMerged=${() => { if (groups !== null) load(); onMerged?.(); }} />`}
      ${open && html`<div class="lg-dup-list">
        ${groups === null && html`<${SkelRows} n=${2} h=${40} />`}
        ${groups !== null && groups.length === 0 && html`<p class="lg-quiet">None left.</p>`}
        ${(groups || []).map((g, gi) => html`<${DupGroup} key=${gi} group=${g} onMerge=${merge} />`)}
        <p class="lg-export-note">It does not match “Bob” to “Robert”. Use Merge two people by
          name for those.</p>
      </div>`}
    </div>`;
}

function Install({ install, go, onChanged, ask }) {
  const people = install.people || {};
  const left = (people.parts?.hidden || 0) + (people.parts?.deleted || 0);
  const byChannel = Object.entries(install.messages?.by_channel || {})
    .filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const parts = (install.disk?.named || []).filter(p => p.bytes >= 1e6).slice(0, PART_LIMIT);
  return html`
    <div class="lg-install">
      <${StatusRow} label="People"
        value=${html`<${Figure} fig=${people.figure} words="you can see and use" whole=${false} />`}
        detail=${[left > 0 ? `${left.toLocaleString()} more you hid or deleted` : "",
                  // The count stopped adding up: say so, in the server's words. Silent
                  // when it adds up (figures.ledger_says). NC-236, from the old page.
                  people.unexplained && people.says
                    ? people.says.charAt(0).toUpperCase() + people.says.slice(1) : ""]
                 .filter(Boolean).join(". ")} />
      ${/* NC-152, moved from the old page's introduction with the switch-over. */ ""}
      <p class="lg-install-foot"><b>Hiding someone is a filter, not a delete.</b>${" "}
        They leave every view, search and AI answer, but they stay in the files in the
        data folder, and in the search index, which is built over everyone. Anything
        that copies that folder copies them too. To remove someone for good, delete
        them.</p>
      <${StatusRow} label="Messages"
        value=${html`<${Figure} fig=${install.messages?.figure} words="saved on this Mac" />`}
        detail=${byChannel.map(([c, n]) => `${CHANNEL_LABEL[c] || c} ${n.toLocaleString()}`).join(" · ")} />
      <${StatusRow} label="On disk" value=${html`<b>${size(install.disk?.bytes)}</b>`}
        detail=${parts.map(p => `${p.name} ${size(p.bytes)}`).join(" · ")} />
      ${!install.empty && html`<${PossibleDuplicates} count=${install.duplicates}
        onMerged=${onChanged} ask=${ask} />`}
      ${install.empty
        ? html`<div class="lg-export" id="lg-export">
            <span class="lg-status-label">Export a backup</span>
            <span class="lg-export-note">Nothing to back up yet. Once people are here,
              this saves them and your notes as a file.</span>
          </div>`
        : html`<div class="lg-export" id="lg-export">
            <span class="lg-status-label">Export a backup</span>
            <a class="btn" href="/api/export?format=md" download>A document to read</a>
            <a class="btn" href="/api/export?format=csv" download>A spreadsheet</a>
            <a class="btn" href="/api/export?format=json" download>For another program</a>
            <span class="lg-export-note">Your people and your notes. Messages are not
              included and stay on this Mac.</span>
          </div>`}
      <p class="lg-install-foot">What the app does for you, and your name and email,
        are in Settings.${" "}<button class="btn ghost mini" onClick=${() => go("settings")}>
        Open Settings</button></p>
    </div>`;
}

/** NC-236, from the old page (NC-033, NC-034): "I could not look" is a
 *  different answer from "nothing found". A check that never answers is almost
 *  always macOS waiting on a folder permission nobody has seen. */
function ScanFailed({ how }) {
  return html`<p class="lg-scan-failed" role="status">${how === "timeout"
    ? html`Couldn't check this Mac for new files. macOS is most likely waiting for
        you to allow access to your Desktop and Downloads folders: look for a
        permission dialog, or allow it in System Settings, Privacy & Security, Files
        and Folders. Everything already imported is unaffected.`
    : html`Couldn't check this Mac for new files just now. Everything already
        imported is unaffected.`}</p>`;
}

/** The quiet line under Needs you: what the page knows, and only once it knows
 *  it. Before the scan it used to say "Nothing" while it was still looking. */
function AllClear({ data, openRow, scanFailed }) {
  if (scanFailed) return html`<${ScanFailed} how=${scanFailed} />`;
  if (!data.scanned) {
    return html`<p class="lg-scanning" role="status">Checking this Mac for files to
      import… If macOS asks whether Network CRM may read a folder, your answer
      only affects that one source.</p>`;
  }
  if (data.in_use > 0) {
    const n = data.newest;
    return html`<p class="lg-quiet">Nothing. ${data.in_use === 1
      ? "The one source you use is working."
      : `The ${data.in_use.toLocaleString()} sources you use are all working.`}
      ${n && n.seconds != null && html`${" "}The newest import was ${n.label},${" "}
        ${agoWords(n.seconds)}.`}</p>`;
  }
  if (data.next) {
    return html`<p class="lg-quiet">Nothing is switched on yet. The cheapest place to
      start is <b>${data.next.label}</b>: ${data.next.takes}${" "}
      ${data.next.why && html`${data.next.why}${" "}`}
      <button class="btn mini" onClick=${() => openRow(data.next.row)}>
        Open ${data.next.label}</button></p>`;
  }
  return html`<p class="lg-quiet">Nothing.</p>`;
}

export function DataPage({ go, params = null }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [openKey, setOpenKey, openRow, bringIntoView] = useOpenRow();
  const [ran, setRan] = useState(null);   // the last import started from Needs you
  const [scanFailed, setScanFailed] = useState("");   // "", "timeout" or "error"
  const asked = useRef(0);

  // Ask once quickly, draw, then ask again with the scan for files on this Mac,
  // which can wait on a macOS permission prompt. After any action, ask with the
  // scan straight away: the page already has something on screen.
  const load = useCallback(async (quickFirst) => {
    const mine = ++asked.current;
    try {
      let shown = !quickFirst;
      if (quickFirst) {
        const quick = await api("/api/data/ledger");
        noteImportCeiling(quick.import_timeout_sec);
        if (mine === asked.current) { setData(quick); shown = true; }
      }
      try {
        const full = await api("/api/data/ledger?scan=1");
        noteImportCeiling(full.import_timeout_sec);
        if (mine === asked.current) {
          setData(full); setError(""); setScanFailed(full.scan_failed ? "error" : "");
        }
      } catch (e) {
        // The rows are already on screen: say the check failed, keep them.
        if (!shown) throw e;
        if (mine === asked.current) {
          setScanFailed(/did not answer/.test(e?.message || "") ? "timeout" : "error");
        }
      }
    } catch (e) {
      if (mine === asked.current) setError(e.message || "Could not read this page's figures.");
    }
  }, []);
  useEffect(() => { load(true); }, [load]);
  const changed = useCallback(() => load(false), [load]);
  useRowFromLink(params, !!data, openRow, !!data?.scanned, bringIntoView);
  const ask = useAskFromLink(params, !!data, !!data?.scanned);

  if (!data) {
    return html`<div class="data-view lg-page">
      <h1 class="data-title">Data</h1>
      ${error ? html`<p class="data-sub">${error}</p>` : html`<${SkelRows} n=${8} h=${40} />`}
    </div>`;
  }

  const rows = data.rows || [];
  const needs = data.needs || [];
  const byKey = Object.fromEntries(rows.map(r => [r.key, r]));

  return html`
    <div class="data-view lg-page">
      <h1 class="data-title">Data</h1>
      ${/* NC-215. It said "Every import runs on this Mac, and nothing leaves it",
           on a page whose rows fetch from LinkedIn, Google and Apple and one of
           which sends pieces of your messages to Anthropic. */ ""}
      <p class="data-sub">Everything this app knows about people comes from the
        sources below, and it is kept only on this Mac. Most imports never touch the
        internet. The LinkedIn messages, Gmail, Google contact photos and calendar
        link rows fetch from those services, LinkedIn profile facts come from your
        other Mac, and Facts written by AI sends short pieces of your messages to
        Anthropic, the company that makes Claude. Read shared history, on the
        LinkedIn profile facts row, sends the profiles your other Mac saved to
        Anthropic too. Each row says which it is.</p>

      <${Section} id="lg-needs" title="Needs you" count=${needs.length || null}>
        ${ran && html`<div class="lg-ran">
            <div class="lg-ran-head"><b>${ran.label}</b>${" "}
              <button class="btn ghost mini" onClick=${() => setRan(null)}>Dismiss</button></div>
            <${RunResult} ok=${ran.ok} log=${ran.log} receipts=${ran.receipts}
              added=${ran.added} go=${go} /></div>`}
        ${needs.length > 0
          ? html`<div class="lg-needs">${needs.map(n => html`
              <${NeedsRow} key=${`${n.row}|${n.source}|${n.tone}`} tone=${n.tone} says=${n.says}>
                <${Acts} acts=${n.acts} label=${(byKey[n.row] || {}).label || n.row}
                  onChanged=${changed} go=${go} openRow=${openRow} onResult=${setRan} />
                ${/* NC-239: what this source added, offered from its need too. */ ""}
                ${n.record && html`<${Record} source=${n.record}
                  label=${(byKey[n.row] || {}).label || n.row} />`}
              <//>`)}</div>${""}
              ${scanFailed ? html`<${ScanFailed} how=${scanFailed} />`
                : !data.scanned && html`<p class="lg-scanning" role="status">Checking this
                Mac for files to import…</p>`}`
          : html`<${AllClear} data=${data} openRow=${openRow} scanFailed=${scanFailed} />`}
      <//>

      <${Section} id="lg-sources" title="Your sources" count=${rows.length}
        note=${data.in_use > 0
          ? "The ones you use first, then the rest, cheapest to switch on first. Click a row to see what it holds and how to switch it on."
          : "Cheapest to switch on first. Click a row to see what it holds and how to switch it on."}>
        <div class="lg-list" role="list">
          <div class="lg-cols" aria-hidden="true">
            <span></span><span>Source</span><span class="lg-gives-col">Gives you</span>
            <span>You have now</span><span class="lg-takes-col">What it takes, or its last import</span>
            <span>Effort</span><span></span>
          </div>
          ${rows.map(r => html`
            <${SourceRow} key=${r.key} row=${r} open=${openKey === r.key}
              onToggle=${() => setOpenKey(k => (k === r.key ? "" : r.key))}
              onChanged=${changed} go=${go} openRow=${openRow} />`)}
        </div>
      <//>

      <${Section} id="lg-install" title="This install">
        <${Install} install=${data.install || {}} go=${go} onChanged=${changed} ask=${ask} />
      <//>
    </div>`;
}
