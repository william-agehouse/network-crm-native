// The Settings page (NC-114), rebuilt, and since NC-236 the only one.
//
// The same three sections and the same row as the Data page:
//
//   Needs you      only what the app cannot do without you: your name and email,
//                  features that failed to install, a search install waiting
//                  for a restart
//   What it can do eight abilities, each an offer with what it takes, in place of
//                  ten empty boxes in a column
//   This install   the version, whether it checks for updates, the data folder
//
// Every word comes from one endpoint, GET /api/settings/ledger
// (crm/api_abilities.py), and after anything you do here the page asks again.
// The six per-source credentials the old page held are on their sources' rows
// on the Data page now, with no copy here. The goals box, the search install and
// the list of models moved here from the old page with the switch-over.
import { html, useState, useEffect, useCallback, useRef } from "../vendor/preact-htm.module.js";
import { api, toast } from "./api.js";
import { SkelRows, openTarget } from "./ui.js";
import { Icon } from "./icons.js";
import { waitFor } from "./fda.js";
import { Section, LedgerRow, RowDetail, NeedsRow, StatusRow, CredentialBox,
         TextSetting, Switch, Help, useOpenRow, useRowFromLink,
         ANTHROPIC_SPEND_HINT } from "./ledger.js";

const ROW_ICON = {
  identity: "person", goals: "pencil", autosync: "sync", intake_web: "globe",
  ics_fetch: "calendar", dates: "birthday", ai: "sparkle", semantic: "search",
};

// ── Moved from the old Settings page with the switch-over, NC-236 ──

// Keep in sync with the ANTHROPIC_MODEL default in crm/settings.py — a default
// that isn't one of these values leaves the picker showing nothing selected.
export const MODEL_OPTIONS = [
  { value: "claude-opus-5-5", label: "Opus 5.5 — most capable" },
  { value: "claude-sonnet-5-5", label: "Sonnet 5.5 — balanced speed & smarts, half the price" },
  { value: "claude-haiku-5-5", label: "Haiku 5.5 — fastest, cheapest" },
  { value: "claude-opus-5", label: "Opus 5 — older, costs more than 5.5" },
];

/** ── The one field on this page that is not a setting ─────────────────────
 *  NC-010: a personal page where you describe your own goals. It is one textarea
 *  rather than a page of forms, and it lives in Essentials because it is
 *  identity — the declared half of who the owner is, beside the name and email
 *  that are the measured half's inputs.
 *
 *  It does NOT go through /api/settings, and that is the whole reason it needs
 *  its own component rather than a `type: "textarea"` on a spec in ESSENTIALS.
 *  Every other field on this page writes a line into `secrets/.env`; this is
 *  prose the user edits in the app, so it is a `state` row in crm.db — which
 *  also means it travels with the syncable database instead of staying on one
 *  Mac. So: no KeyHint (there is no key to hint at), and its own endpoint pair.
 *
 *  Three differences from `Field`, each with a reason:
 *   · Enter does NOT save. In a textarea the newline is the point. Cmd/Ctrl+Enter
 *     saves for anyone who wants the accelerator; Escape still reverts, so the
 *     revealed button is not the only way out.
 *   · The ceiling comes from the server (`max_chars`), never a literal here. A
 *     number duplicated in JavaScript drifts from GOALS_MAX_CHARS the first time
 *     anyone changes the Python, and then the counter lies about when a save
 *     will be trimmed.
 *   · The counter only appears near the limit. A character count under a
 *     paragraph box reads as a constraint on what you are allowed to say, which
 *     is the opposite of what this field is asking for. */
export function GoalsField({ onSaved = null } = {}) {
  const [state, setState] = useState(null);   // {goals, max_chars} once loaded
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/api/owner")
      .then(d => { setState(d); setDraft(d.goals || ""); })
      // A failure here must not take Essentials down with it — the name, email
      // and API key beside it are what a broken install needs to reach first.
      .catch(() => setState({ goals: "", max_chars: 4000, error: true }));
  }, []);

  if (state === null) return html`<${SkelRows} n=${1} h=${96} />`;

  const stored = state.goals || "";
  const dirty = draft !== stored;
  const max = state.max_chars || 4000;
  const left = max - draft.length;

  const save = async () => {
    setBusy(true);
    try {
      const d = await api("/api/owner/goals",
        { method: "POST", body: JSON.stringify({ goals: draft }) });
      // Re-seed from what the server stored, not from what we sent: the write
      // strips and truncates, so the box must show the shorter reality rather
      // than keep a longer draft that no prompt will ever see.
      setState({ ...state, goals: d.goals || "" });
      setDraft(d.goals || "");
      toast(d.truncated
        ? `Saved, trimmed to ${max.toLocaleString()} characters`
        : "Saved");
      onSaved?.();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  const onKey = (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && dirty && !busy) {
      e.preventDefault(); save();
    } else if (e.key === "Escape" && dirty) {
      e.preventDefault(); setDraft(stored);
    }
  };

  return html`
    <div class="settings-field">
      <div class="settings-field-label" id="owner-goals-label">
        <span class="settings-field-name">What you are working on</span>
      </div>
      <div class="settings-field-help" id="owner-goals-help">
        In your own words: what you are trying to do, and what you want from the
        people in here. Ask weighs this when it judges who is worth your
        attention, and briefs and draft messages read it too. Nothing can infer
        it from your message history — this is the half only you can tell it.
      </div>
      <textarea
        class="input settings-goals"
        rows="5"
        value=${draft}
        aria-labelledby="owner-goals-label"
        aria-describedby="owner-goals-help"
        placeholder="Opening a second bakery; want introductions to flour suppliers and anyone who has taken a shop from one location to several."
        onInput=${e => setDraft(e.target.value)}
        onKeyDown=${onKey}
      ></textarea>
      <div class="settings-field-row">
        ${dirty && html`
          <button class="btn primary" disabled=${busy} onClick=${save}>
            ${busy ? "Saving…" : "Save"}
          </button>
          <span class="set-enter">or press ⌘↩</span>`}
        ${/* Only within the last tenth of the ceiling — see the note above. */""}
        ${left <= max / 10 && html`
          <span class=${"field-note" + (left < 0 ? " soft-invalid" : "")} role="status">
            ${left < 0
              ? `${(-left).toLocaleString()} characters over — saving will trim it`
              : `${left.toLocaleString()} characters left`}
          </span>`}
      </div>
    </div>`;
}

/** Optional semantic (meaning-based) search. It needs sentence-transformers +
 * numpy, which pull PyTorch (~2 GB), so it is NOT shipped by default — keyword
 * and FTS5 search work without it. This row installs it on demand into the app's
 * own Python and shows live pip progress. Status + progress ride the
 * semantic_search section of GET /api/diagnostics (already self-refreshing);
 * the install itself is POST /api/semantic/install.
 *
 * The download size is stated in the help line AND on the button, before any
 * click: a 2 GB surprise is the failure mode this row exists to prevent.
 *
 * NC-003: this used to sit under the one numeric field that governed its
 * install timeout (NC_SEMANTIC_INSTALL_TIMEOUT_SEC). That field is gone from
 * the UI — nobody needs to tune how long a one-time install may run — so this
 * panel now stands on its own under a plain "Search" heading. */
export function SemanticSearch({ onChanged = null } = {}) {
  const [st, setSt] = useState(null);   // the semantic_search status object
  const [err, setErr] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);

  const read = useCallback(() =>
    api("/api/diagnostics")
      .then(d => setSt(d.semantic_search || null))
      .catch(() => setErr(true)), []);

  useEffect(() => { read(); }, [read]);

  // Poll while an install is running; stop the moment it settles.
  const running = !!(st && st.install && st.install.state === "running");
  // Tell the page around it when an install settles, so a row that said
  // "Installing" catches up without a reload (the rebuilt Settings page, NC-114).
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) onChanged?.();
    wasRunning.current = running;
  }, [running]);
  useEffect(() => {
    if (!running) return undefined;
    timer.current = setInterval(read, 2500);
    return () => { clearInterval(timer.current); timer.current = null; };
  }, [running, read]);

  const install = async () => {
    setBusy(true);
    try {
      const r = await api("/api/semantic/install", { method: "POST", body: "{}" });
      if (r.status) setSt(r.status);
      toast(r.message || "Installing…");
      onChanged?.();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  if (err) return html`<div class="settings-field-help">Semantic search status did not load.</div>`;
  if (st === null) return html`<${SkelRows} n=${1} h=${36} />`;

  const ins = st.install || {};
  const gb = st.size_gb || 2;
  const isRunning = ins.state === "running";
  const failed = ins.state === "failed";
  const succeeded = ins.state === "succeeded";

  let helpText;
  if (st.available) helpText = "On. Search ranks people by meaning, not only by keyword.";
  else if (isRunning) helpText = `Installing. ${ins.message || ""}`;
  else if (succeeded && ins.restart_required) helpText = "Installed. Restart the app to turn it on.";
  else helpText = `Off. Keyword search still works. Meaning-based ranking needs a `
    + `one-time download of about ${gb} GB (PyTorch)${st.reason ? ` · ${st.reason}` : ""}.`;

  return html`
    <div class="settings-field">
      <div class="settings-field-label">
        <span class="settings-field-name">Meaning-based ranking</span>
      </div>
      <div class="settings-field-help">${helpText}</div>
      <div class="settings-field-row">
        ${st.available
          ? html`<span class="settings-field-help">Installed.</span>`
          : isRunning
            ? html`<button class="btn primary" disabled>Installing…</button>`
            : html`<button class="btn primary" disabled=${busy} onClick=${install}>
                ${busy ? "Starting…" : `${failed ? "Retry install" : "Install"} (~${gb} GB)`}
              </button>`}
      </div>
      ${(isRunning || failed) && ins.tail && ins.tail.length > 0 && html`
        <details class="log-details" open=${failed}>
          <summary>${failed ? "install log (failed)" : "install log"}</summary>
          <pre class=${"src-log " + (failed ? "err" : "ok")}>${ins.tail.join("\n")}</pre>
        </details>`}
    </div>`;
}

/** The model the paid features use. Saved on change, so it needs no button. */
function ModelPicker({ act, onSaved }) {
  const [busy, setBusy] = useState(false);
  const known = MODEL_OPTIONS.some(o => o.value === act.value);
  const options = known || !act.value ? MODEL_OPTIONS
    : [{ value: act.value, label: act.value }, ...MODEL_OPTIONS];
  const choose = async (value) => {
    setBusy(true);
    try {
      const d = await api("/api/settings", { method: "POST",
        body: JSON.stringify({ key: act.key, value }) });
      toast(d.warning || "Model changed");
      onSaved?.();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };
  return html`
    <div class="lg-cred">
      <label class="lg-cred-label" for="set-model">Model</label>
      <select class="lg-cred-input" id="set-model" disabled=${busy} value=${act.value}
        onChange=${e => choose(e.target.value)}>
        ${options.map(o => html`<option key=${o.value} value=${o.value}>${o.label}</option>`)}
      </select>
    </div>`;
}

/** Set up from scratch again. It takes over the app, so it asks once more. */
function SetupAgain() {
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  const start = async () => {
    if (!sure) { setSure(true); return; }
    setBusy(true);
    try {
      await api("/api/onboarding", { method: "POST", body: JSON.stringify({ done: false }) });
      location.hash = "";
      location.reload();
    } catch (e) { toast(e.message); setBusy(false); }
  };
  return html`
    <div class="lg-controls">
      <button class=${"btn" + (sure ? " primary" : "")} disabled=${busy} onClick=${start}>
        ${sure ? "Start setup now" : "Set up again"}</button>
      ${sure && html`<button class="btn ghost" onClick=${() => setSure(false)}>Not now</button>`}
      ${sure && html`<span class="lg-export-note">Nothing you have imported is removed.</span>`}
    </div>`;
}

/** NC-314. Remove everything this app keeps on this Mac.
 *
 *  Putting the app in the Trash left the copying helper running with Full Disk
 *  Access, and every person, message and saved key behind. This is the way out,
 *  at the very bottom of the page, and built to be hard to press by accident:
 *  the first press only shows what goes, and the button that does it stays off
 *  until the person types the word the engine asks for (crm/api_remove.py).
 *
 *  Once it has run the engine stops itself, so the answer it sends is the last
 *  thing this page can show: what went, anything left, and the two steps macOS
 *  lets no program do. Nothing on that last screen asks the engine anything. */
function RemoveEverything() {
  const [plan, setPlan] = useState(null);
  const [armed, setArmed] = useState(false);
  const [word, setWord] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [done, setDone] = useState(null);

  useEffect(() => {
    api("/api/remove/plan").then(setPlan).catch(() => setPlan({
      allowed: false, why_not: "Could not ask what this would remove just now." }));
  }, []);

  if (done) {
    return html`
      <div class="remove-all is-done" role="status">
        <p><b>${done.says}</b></p>
        ${done.left?.length > 0 && html`<ul class="remove-all-list">${done.left.map(p => html`
          <li key=${p.path}>${p.name}: <code>${p.path}</code></li>`)}</ul>`}
        ${done.helper?.output && !done.ok && html`<pre class="fda-output">${done.helper.output}</pre>`}
        ${done.by_hand?.length > 0 && html`
          <p>Two steps are left, and macOS lets only you do them:</p>
          <ol class="remove-all-steps">${done.by_hand.map((s, i) => html`<li key=${i}>${s}</li>`)}</ol>`}
        ${done.stopping && html`<p>The app has stopped. Close this window.</p>`}
      </div>`;
  }
  if (!plan) return null;

  const run = async () => {
    if (word.trim().toLowerCase() !== plan.word) {
      setNote(`Type ${plan.word} in the box first.`); return;
    }
    setBusy(true); setNote("");
    try {
      setDone(await api("/api/remove/everything", { method: "POST",
        body: JSON.stringify({ confirm: word.trim() }),
        timeoutMs: waitFor("uninstall", plan.waits) }));
    } catch (e) { setNote(e.message); }
    setBusy(false);
  };

  if (!plan.allowed) {
    return html`<div class="remove-all">
      <button class="btn danger" disabled>Remove everything</button>
      <span class="lg-export-note">${plan.why_not}</span></div>`;
  }
  if (!armed) {
    return html`<div class="remove-all">
      <button class="btn danger" onClick=${() => setArmed(true)}>Remove everything…</button>
      <span class="lg-export-note">Deletes every person, message and setting this app
        keeps on this Mac, and turns automatic copying off. It cannot be undone.</span></div>`;
  }
  return html`
    <div class="remove-all is-armed">
      <p><b>This deletes everything and cannot be undone.</b> Export a backup on the
        Data page first if you may want any of it back. What goes:</p>
      <ul class="remove-all-list">${(plan.removes || []).map(p => html`
        <li key=${p.path} title=${p.path}>${p.name}</li>`)}</ul>
      <p>Afterwards you drag the app to the Trash and remove one entry in System
        Settings yourself. The last screen says how.</p>
      <div class="lg-controls">
        <label class="lg-cred-label" for="remove-word">Type ${plan.word} to confirm</label>
        <input class="lg-cred-input" id="remove-word" type="text" autocomplete="off"
          spellcheck="false" autocapitalize="off" value=${word} disabled=${busy}
          onInput=${e => { setWord(e.target.value); setNote(""); }}
          onKeyDown=${e => { if (e.key === "Escape") { setArmed(false); setWord(""); } }} />
        <button class="btn danger" disabled=${busy || word.trim().toLowerCase() !== plan.word}
          onClick=${run}>${busy ? "Removing…" : "Remove everything for good"}</button>
        <button class="btn ghost" disabled=${busy}
          onClick=${() => { setArmed(false); setWord(""); setNote(""); }}>Keep it all</button>
      </div>
      ${note && html`<div class="field-note" role="status">${note}</div>`}
    </div>`;
}

function Acts({ acts, onChanged, go, openRow }) {
  return (acts || []).map((a, i) => {
    const key = `${a.kind}-${a.key || a.row || ""}-${i}`;
    switch (a.kind) {
      case "text":
        return html`<${TextSetting} key=${key} act=${a} onSaved=${onChanged} />`;
      case "secret":
        // NC-327: the one box that can cost money says how to put a ceiling on it.
        return a.key === "ANTHROPIC_API_KEY"
          ? [html`<${CredentialBox} key=${key} act=${a} onSaved=${onChanged} />`,
             html`<p class="lg-cred-hint" key=${`${key}-hint`}>${ANTHROPIC_SPEND_HINT}</p>`]
          : html`<${CredentialBox} key=${key} act=${a} onSaved=${onChanged} />`;
      case "switch":
        return html`<${Switch} key=${key} act=${a} onSaved=${onChanged} />`;
      case "model":
        return html`<${ModelPicker} key=${key} act=${a} onSaved=${onChanged} />`;
      case "goals":
        return html`<div class="lg-embed" key=${key}><${GoalsField} onSaved=${onChanged} /></div>`;
      case "semantic":
        return html`<div class="lg-embed" key=${key}><${SemanticSearch} onChanged=${onChanged} /></div>`;
      case "page":
        // It named a row and dropped it, so "The addresses are on the Data page"
        // left you at the top of that page to find them (NC-218).
        return html`<button class="btn" key=${key}
          onClick=${() => go(a.row ? `${a.page}?row=${a.row}` : a.page)}>${a.label}</button>`;
      case "open":
        return html`<button class="btn" key=${key} onClick=${() => openTarget(a.target)}>
          <${Icon} name="external" /> ${a.label}</button>`;
      case "row":
        return html`<button class="btn" key=${key} onClick=${() => openRow(a.row)}>${a.label}</button>`;
      default:
        return null;
    }
  });
}

function AbilityRow({ row, open, onToggle, onChanged, go, openRow }) {
  const d = row.detail || {};
  const note = ["blocked", "behind", "paused", "na", "ready"].includes(row.state.tone)
    ? row.state.says : "";
  return html`
    <${LedgerRow} id=${`lg-row-${row.key}`} icon=${ROW_ICON[row.key]} name=${row.label}
      gives=${row.gives} have=${row.have.says} note=${note} noteTone=${row.state.tone}
      takes=${row.takes} cost=${row.cost} tone=${row.state.tone}
      open=${open} onToggle=${onToggle}>
      <${RowDetail}
        act=${(d.acts || []).length > 0 && html`<div class="lg-acts">
          <${Acts} acts=${d.acts} onChanged=${onChanged} go=${go} openRow=${openRow} /></div>`}
        how=${d.help && html`<${Help} text=${d.help} />`}
        other=${d.manual && html`<p>${d.manual}</p>`} />
    <//>`;
}

export function SettingsPage({ go, params = null }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [openKey, setOpenKey, openRow] = useOpenRow();
  const asked = useRef(0);

  const load = useCallback(async () => {
    const mine = ++asked.current;
    try {
      const d = await api("/api/settings/ledger");
      if (mine === asked.current) { setData(d); setError(""); }
    } catch (e) {
      if (mine === asked.current) setError(e.message || "Could not read this page.");
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  useRowFromLink(params, !!data, openRow);

  if (!data) {
    return html`<div class="data-view lg-page">
      <h1 class="data-title">Settings</h1>
      ${error ? html`<p class="data-sub">${error}</p>` : html`<${SkelRows} n=${6} h=${40} />`}
    </div>`;
  }
  const rows = data.rows || [];
  const needs = data.needs || [];
  const inst = data.install || {};

  return html`
    <div class="data-view lg-page">
      <h1 class="data-title">Settings</h1>
      <p class="data-sub">What the app can do for you, and what each one takes.
        Anything that belongs to one source is on that source's row on the Data page.</p>

      <${Section} id="lg-needs" title="Needs you" count=${needs.length || null}>
        ${needs.length > 0
          ? html`<div class="lg-needs">${needs.map(n => html`
              <${NeedsRow} key=${`${n.row}|${n.says}`} tone=${n.tone} says=${n.says}>
                <${Acts} acts=${n.acts} onChanged=${load} go=${go} openRow=${openRow} />
              <//>`)}</div>`
          : html`<p class="lg-quiet">Nothing. Everything here is either on or yours to switch on.</p>`}
      <//>

      <${Section} id="lg-abilities" title="What it can do" count=${rows.length}
        note="What the app can do besides importing, and what each one asks of you. Click a row to change it.">
        <div class="lg-list">
          <div class="lg-cols" aria-hidden="true">
            <span></span><span>What it does</span><span class="lg-gives-col">Gives you</span>
            <span>Now</span><span class="lg-takes-col">What it takes</span>
            <span>Effort</span><span></span>
          </div>
          ${rows.map(r => html`
            <${AbilityRow} key=${r.key} row=${r} open=${openKey === r.key}
              onToggle=${() => setOpenKey(k => (k === r.key ? "" : r.key))}
              onChanged=${load} go=${go} openRow=${openRow} />`)}
        </div>
      <//>

      <${Section} id="lg-install" title="This install">
        <div class="lg-install">
          <${StatusRow} label="Version" value=${html`<b>Network CRM ${inst.version || ""}</b>`}
            detail=${inst.updates_says || ""} />
          ${/* NC-235: moved here from the list of abilities. */ ""}
          <div class="lg-export">
            <span class="lg-status-label">Set up from scratch again</span>
            <${SetupAgain} />
            <span class="lg-export-note">Walks you through finding your data, importing it,
              and describing a few people. It takes over the app until you are done.</span>
          </div>
          <div class="lg-export">
            <span class="lg-status-label">Your data</span>
            ${inst.data_folder && html`<button class="btn" onClick=${() => openTarget(inst.data_folder.target)}>
              <${Icon} name="folder" /> ${inst.data_folder.label}</button>`}
            <span class="lg-export-note">Everything is kept on this Mac. Back it up from the Data page.</span>
          </div>
          <p class="lg-install-foot">Your sources, and the passwords and addresses they
            need, are on the Data page.${" "}<button class="btn ghost mini"
            onClick=${() => go("data")}>Open the Data page</button></p>
          ${/* NC-314: last on the page, below everything else. */ ""}
          <div class="lg-export" id="remove-everything">
            <span class="lg-status-label">Remove everything</span>
            <${RemoveEverything} />
          </div>
        </div>
      <//>
    </div>`;
}
