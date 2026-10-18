/* ── Full Disk Access, explained inside the app ───────────────────────────────
 * The one permission this app depends on and cannot ask for.
 *
 * WHY THIS FILE EXISTS
 * Messages, Contacts and Calendar keep their databases in folders macOS guards
 * (the full list the helper copies is importers.HELPER_SOURCE_LABELS), and the
 * only permission that opens them is Full Disk Access — which has no API, no
 * prompt, and no way to be requested. It is granted by hand, in System Settings,
 * to one specific binary. Everything needed for that already worked; all of it
 * printed to a Terminal. The person using this app never opens a Terminal, so
 * from inside the product there was no way to learn the permission existed, let
 * alone that it was still missing.
 *
 * WHAT THIS CAN AND CANNOT DO, WHICH IS THE WHOLE DESIGN
 * It cannot grant the permission and it cannot ask for it. It can:
 *   · install the helper (one button, no Terminal),
 *   · open the exact System Settings pane and pre-select the exact binary in
 *     Finder, so the step left is one drag,
 *   · say, afterwards and truthfully, whether it worked — per source, because a
 *     Mac with WhatsApp Desktop and no Messages grant is the normal case, not an
 *     edge one.
 *
 * The third is the part that was missing, and it is why this reads as a status
 * panel with steps in it rather than as a set of instructions. The status comes
 * from the helper's own report of its own last run (GET /api/fda/status →
 * crm/api_fda.py), never from this file guessing.
 *
 * "Check now" runs the helper through launchd — the same path the scheduled
 * refresh takes — and waits for the new report. So a green answer here is a
 * statement about the real thing, not about a test.
 */
import { html, useState, useEffect, useCallback } from "../vendor/preact-htm.module.js";
import { api, toast } from "./api.js";
import { Icon } from "./icons.js";
import { openTarget } from "./ui.js";

/* Per-source line: icon, name, and what the helper said about it. `tone` drives
   the dot colour and nothing else, so the words stay the primary signal. */
const OUTCOME = {
  copied:  { tone: "ok",      text: "copied just now" },
  current: { tone: "ok",      text: "already up to date" },
  blocked: { tone: "blocked", text: "blocked by macOS" },
  absent:  { tone: "absent",  text: "not found on this Mac" },
  unknown: { tone: "absent",  text: "unknown" },
};

// Keyed on the helper's own source labels (packaging/src/imessage_sync.swift).
// It reports FOUR sources, not two: Contacts and Calendar were added in NC-042
// and shipped iconless here for the same reason calendar_local did on the Data
// page — Icon returns null for an unknown key, so a missing entry is a silent
// blank rather than an error. tests/test_frontend_gates.py now gates this.
const SRC_ICON = {
  iMessage: "imessage", WhatsApp: "whatsapp",
  Contacts: "contacts", Calendar: "calendar",
};

/** "a", "a and b", "a, b and c" — no Oxford comma, matching the helper's own
 *  message grammar so the two never read as different programs. */
function nameList(names) {
  if (names.length === 0) return "";
  if (names.length === 1) return names[0];
  return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

/* NC-187. The switch's other position. Invariant 6 calls the two ways in one
   switch, and this panel could only move it one way: it installed the helper,
   and turning it off meant knowing packaging/uninstall_imessage_sync.sh existed.

   Two clicks, inline, the way the rest of the app confirms (ask.js, lists.js —
   no confirm() dialogs): the first swaps the button for what it will do and
   the actual control; Escape or "Keep it" puts it back. The sentence is the
   point of the second step — it says the copies stay, so nobody declines for
   fear of losing their data. */
function TurnOff({ busy, onOff }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const onKey = (e) => { if (e.key === "Escape") setArmed(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [armed]);
  if (!armed) {
    return html`<button class="btn ghost" disabled=${!!busy}
                        onClick=${() => setArmed(true)}>Turn off automatic copying</button>`;
  }
  return html`
    <span class="fda-off-confirm">
      <span>Stop the helper? What it already copied stays, and the app goes back
      to reading the copies you make yourself.</span>
      <button class="btn" disabled=${!!busy} onClick=${() => { setArmed(false); onOff(); }}>
        ${busy === "uninstall" ? "Turning off…" : "Turn it off"}
      </button>
      <button class="btn ghost" onClick=${() => setArmed(false)}>Keep it</button>
    </span>`;
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const m = Math.round(seconds / 60);
  if (m < 1) return "just now";
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"} ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} hour${h === 1 ? "" : "s"} ago`;
  return `${Math.round(h / 24)} days ago`;
}

/* How many files the helper actually found for a source, and how big they were.
 *
 * This is the panel's answer to a silent failure: a flat address-book store can
 * hold a single person, with everyone else in per-account stores, so the helper
 * honestly reports "already current", the row goes green, and the import brings
 * in almost nobody with nothing on screen to notice it by. Showing the number of
 * files matched means a green row cannot hide an almost-empty import.
 * `matched` is written for every source whether it copied or not, which is why it
 * leads here — it is the only one of the three counts that is present in that
 * failure state. NC-093.
 *
 * Deliberately says "1 file" rather than hiding a count of one: a single file is
 * the suspicious case, so it is the one that must be legible. */
function found(s) {
  const bits = [];
  if (typeof s.matched === "number") {
    bits.push(`${s.matched} file${s.matched === 1 ? "" : "s"}`);
  }
  if (typeof s.bytes === "number" && s.bytes > 0) bits.push(humanBytes(s.bytes));
  return bits.join(", ");
}

function humanBytes(n) {
  if (!n) return "0 KB";
  const kb = n / 1024;
  if (kb < 1024) return `${Math.max(1, Math.round(kb))} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

function SourceLines({ sources }) {
  return html`
    <ul class="fda-sources">
      ${sources.map(s => {
        const o = OUTCOME[s.outcome] || OUTCOME.unknown;
        return html`
          <li key=${s.label} class=${`fda-src is-${o.tone}`}>
            <${Icon} name=${SRC_ICON[s.label] || "chat"} />
            <span class="fda-src-name">${s.label}</span>
            <span class="fda-src-state">${o.text}${found(s) ? ` · ${found(s)}` : ""}${s.hint ? ` — ${s.hint}` : ""}</span>
          </li>`;
      })}
    </ul>`;
}

/* The two buttons that are the actual point of the panel. Both go through the
   server-side allowlist (importers.OPEN_TARGETS): the client posts a key, never
   a path — same contract as every other "Open …" button in the app. */
function GrantButtons({ status }) {
  return html`
    <div class="fda-actions">
      <button class="btn primary" onClick=${() => openTarget(status.targets.settings)}>
        <${Icon} name="unlock" /> Open Full Disk Access settings
      </button>
      <button class="btn" onClick=${() => openTarget(status.targets.binary)}>
        <${Icon} name="folder" /> Show the helper in Finder
      </button>
    </div>`;
}

/** The permission panel — every state of it, in one component.
 *
 *  There is deliberately no second "offer" component: it would mean two fetches
 *  of the same status and a real question about which of the two was allowed to
 *  speak. Instead this renders the offer, the instructions and the confirmation
 *  from one source of truth, and nothing when it has nothing to say.
 *
 *  `refreshKey` re-reads the status when the page around it changes; `onChanged`
 *  lets the page re-read ITS data after an install or a successful check, since a
 *  newly-unblocked source changes what the rest of the Data page should show.
 *  `relevant` is the caller's answer to "is this user in the market for this at
 *  all" — false suppresses the not-yet-installed offer (but never a real problem
 *  with a helper that IS installed), so a Mac with no Messages and no WhatsApp is
 *  not sold a permission it has no use for.
 */
/* NC-206. The three buttons below run longer than the 20 s every other call is
   given (API_TIMEOUT_MS). The installer runs the helper once and waits up to a
   minute for its report; "Check now" starts it and waits for a new one. A page
   that gave up at 20 s while the install carried on and succeeded would go back
   to "Set up automatic updates" beside the OFF switch's old output, with the new
   helper already installed and reporting. Same mistake as NC-176, same cure: wait
   longer than the server, whose own limits arrive in `status.waits`. The margin is for the server's answer to
   travel once it gives up; it is plumbing, not a setting. */
const WAIT_MARGIN_MS = 15000;
const WAIT_FALLBACK_SEC = { install: 300, uninstall: 300, recheck: 65 };  // crm/api_fda.py
const WAIT_MAX_SEC = 3600;

export function waitFor(action, waits) {
  const sec = waits && waits[action];
  // typeof, not Number(), for the reason noteImportCeiling gives in api.js: a
  // malformed payload must never talk the page DOWN to a shorter wait.
  const ok = typeof sec === "number" && Number.isFinite(sec) && sec > 0;
  return Math.min(WAIT_MAX_SEC, ok ? sec : WAIT_FALLBACK_SEC[action]) * 1000 + WAIT_MARGIN_MS;
}

export function FdaPanel({ refreshKey = 0, onChanged = null, relevant = true,
                          onAttention = null, heading = "h2" }) {
  // Inside a row of the rebuilt Data page the panel sits under that row's own
  // headings, so it is given a lower level there (NC-219).
  const H = heading;
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState("");        // "" | "install" | "check"
  const [output, setOutput] = useState("");    // installer output, on demand
  const [showOutput, setShowOutput] = useState(false);
  const [checked, setChecked] = useState(false);  // a check has run this session

  const load = useCallback(() => {
    api("/api/fda/status").then(setStatus).catch(() => setStatus({ state: "unavailable" }));
  }, []);
  useEffect(load, [load, refreshKey]);

  /* NC-076. Whether this panel is asking the user for something, reported to the
     Data page so it can decide whether "No source needs you now." is true. The
     page could not previously know: it computed that sentence from its own scan
     for new FILES, which is blind to a permission macOS is refusing.

     Enumerated against the render branches below rather than guessed, and the
     three quiet ones are quiet for a reason:
       unavailable  this build ships no helper — the panel renders nothing at all
       absent       none of the apps the helper copies from are on this Mac, so
                    there is nothing to copy and nothing to ask for
       ok           working
     `not_installed` follows `relevant` for the same reason the panel does: on a
     Mac with no message history the offer is an advertisement, and an
     advertisement is not a source needing you. */
  const asks = !status ? false
    : status.state === "not_installed" ? !!relevant
    : ["blocked", "outdated", "quiet", "no_report"].includes(status.state);
  useEffect(() => { if (onAttention) onAttention(asks); }, [onAttention, asks]);

  const install = async () => {
    setBusy("install");
    setOutput("");   // never show one run's output beside another run's result
    try {
      const r = await api("/api/fda/install", { method: "POST", body: JSON.stringify({}),
                                                timeoutMs: waitFor("install", status?.waits) });
      setOutput(r.output || "");
      setStatus(r.status);
      // The installer opens the System Settings pane itself on the blocked path,
      // so saying "now grant it" is the whole message — not "installed ✓".
      toast(r.ok ? "Helper installed. Now grant it Full Disk Access."
                 : "The installer reported a problem — see the details below.");
      if (!r.ok) setShowOutput(true);
      onChanged?.();
    } catch (e) {
      toast(e.message);
      load();          // whatever happened, show what is true now, not what was
    } finally {
      setBusy("");
    }
  };

  const uninstall = async () => {
    setBusy("uninstall");
    setOutput("");
    try {
      const r = await api("/api/fda/uninstall", { method: "POST", body: JSON.stringify({}),
                                                  timeoutMs: waitFor("uninstall", status?.waits) });
      setOutput(r.output || "");
      setStatus(r.status);
      // The grant outlives the helper: macOS keeps the Full Disk Access entry
      // until the user removes it, and no script is allowed to. Say so here,
      // where the switch was moved, rather than only in the script's output.
      toast(r.ok ? "Automatic copying is off. Its Full Disk Access entry stays in System Settings until you remove it there."
                 : "Turning it off reported a problem — see the details below.");
      if (!r.ok) setShowOutput(true);
      onChanged?.();
    } catch (e) {
      toast(e.message);
      load();
    } finally {
      setBusy("");
    }
  };

  const check = async () => {
    setBusy("check");
    try {
      const r = await api("/api/fda/recheck", { method: "POST", body: JSON.stringify({}),
                                                timeoutMs: waitFor("recheck", status?.waits) });
      setStatus(r.status);
      setChecked(true);
      if (!r.ran) {
        toast(`The helper did not report back within ${r.timeout}s. It may still be copying.`);
      } else if (r.status.state === "ok") {
        toast("Working — macOS is not blocking anything.");
      } else if (r.status.blocked?.length) {
        toast(`Still blocked: ${r.status.blocked.join(" and ")}.`);
      }
      onChanged?.();
    } catch (e) {
      toast(e.message);
      load();
    } finally {
      setBusy("");
    }
  };

  if (!status) return null;
  // "unavailable" means this build carries no helper (appify.sh without swiftc).
  // Offering a permission flow that cannot be completed is worse than silence —
  // same judgement the onboarding wizard already makes about the installer path.
  if (status.state === "unavailable") return null;
  // Not opted in yet. This is an offer, not a warning — nothing is broken, the
  // user simply has snapshots instead of a live copy. Suppressed entirely when
  // the caller says the user has no Messages or WhatsApp history in play, since
  // then it is an advertisement rather than help.
  if (status.state === "not_installed") {
    if (!relevant) return null;
    // NC-194. Named from the server's list of what this build's helper copies,
    // for the same reason as the working line below: this said "Messages and
    // WhatsApp" and "those two databases" through two new sources.
    const copies = nameList(status.copies || []) || "your messages";
    return html`
      <div class="fda-panel is-offer">
        <div class="fda-head">
          <${Icon} name="clock" className="ic fda-head-ic" />
          <div class="fda-head-text">
            <${H}>Keep ${copies} up to date by themselves<//>
            <p>A copy you paste or export is a snapshot — it goes stale the moment
            something changes. One small helper can refresh them every couple of
            minutes. It needs one macOS permission, <b>Full Disk Access</b>, because
            for most of these macOS offers nothing narrower. <b>Network CRM itself
            never gets that permission</b> and never reads the protected files.
            Setting it up is two steps, both from here.</p>
          </div>
        </div>
        <div class="fda-actions">
          <button class="btn primary" disabled=${!!busy} onClick=${install}>
            <${Icon} name="download" />
            ${busy === "install" ? "Setting up…" : "Set up automatic updates"}
          </button>
        </div>
        <p class="fda-fine">This installs one small compiled program that copies
        those files and does nothing else — no network access of any kind.
        Granting the permission stays your own act in System Settings; this page
        will take you to the right pane and then tell you whether it worked.</p>
        ${/* NC-324. Everything above is true and still left one thing out: the
             permission protects the originals, not what the app keeps. Whichever
             route is used, the imported messages sit unencrypted in the app's
             own folder, which every program running as this person can read. */ ""}
        <p class="fda-fine">Whichever way you choose, what this app imports, and
        the copies the helper makes, are kept unencrypted in the app's own folder
        on this Mac. Any program you run can read them there, just as it can read
        your other files.</p>
        ${output && html`<pre class="fda-output">${output}</pre>`}
      </div>`;
  }

  const path = status.binary;
  const blocked = status.state === "blocked";

  // The installed helper is older than this build and copies fewer sources than
  // it should. Its own line, because it is neither broken nor working: what it
  // already syncs keeps syncing, and the named source simply never becomes
  // automatic. Without this the only symptom is an absence, which reads as "the
  // feature does not work" rather than "run the installer again".
  //
  // NC-223: it can also copy everything and still be older than the copy inside
  // this app. Then there is no source to name, so the line says what the newer
  // one changes instead (`newer_says`, written by the engine).
  if (status.state === "outdated") {
    const names = (status.missing || []).join(" and ");
    const regrant = html`Setting up again also means <b>re-granting Full Disk
            Access</b>: macOS ties that permission to the exact program, and this
            replaces it with a newer one. The next screen shows you how.`;
    return html`
      <div class="fda-panel">
        <div class="fda-head">
          <${Icon} name="download" className="ic fda-head-ic" />
          <div class="fda-head-text">
            ${names ? html`
            <${H}>The sync helper needs setting up again to include ${names}<//>
            <p>It is installed and running, but it is an older copy that does not
            know about ${names} yet — so ${names} will not update on its own until
            you set it up again. Everything it already syncs keeps working
            meanwhile. ${regrant}</p>` : html`
            <${H}>A newer sync helper came with this version of the app<//>
            <p>The one installed still works and keeps copying. ${status.newer_says}
            ${" "}${regrant}</p>`}
          </div>
        </div>
        ${status.sources.length > 0 && html`<${SourceLines} sources=${status.sources} />`}
        <div class="fda-actions">
          <button class="btn primary" disabled=${!!busy} onClick=${install}>
            <${Icon} name="download" />
            ${busy === "install" ? "Setting up…" : "Set it up again"}
          </button>
          <button class="btn" disabled=${!!busy} onClick=${check}>
            <${Icon} name="refresh" /> ${busy === "check" ? "Checking…" : "Check now"}
          </button>
          <${TurnOff} busy=${busy} onOff=${uninstall} />
        </div>
        ${output && html`<pre class="fda-output">${output}</pre>`}
      </div>`;
  }

  // Working. One line, not a panel — worth confirming, not worth explaining.
  if (status.state === "ok" || status.state === "absent" || status.state === "quiet") {
    // Named from the helper's own report rather than written out here. Copy that
    // said "Messages and WhatsApp" went wrong once NC-042 added Contacts and
    // Calendar to the helper — it under-reported what the permission was
    // buying, on the one line that exists to confirm it.
    const working = (status.sources || [])
      .filter(x => x.outcome === "copied" || x.outcome === "current")
      .map(x => x.label);
    const head = status.state === "ok"
      ? `${working.length ? nameList(working) : "Every source"} ` +
        `${working.length === 1 ? "refreshes itself" : "refresh themselves"}. ` +
        `Last run ${ago(status.ran_ago_seconds)}.`
      : status.state === "quiet"
      ? `The sync helper has not reported for over ${status.stale_hours} hours.`
      : "The sync helper found none of the apps it copies from on this Mac.";
    // How much each of them actually read. This one quiet line is the whole point
    // of NC-093, and it has to live HERE rather than only in the panel states,
    // because "ok" is the state the failure it exists for can occur in: the helper
    // reports every source `current`, this line goes green, and Apple Contacts
    // has matched only a flat store holding almost nobody. A green line with no
    // number cannot tell that apart from a full import; a per-source count can.
    const read = (status.sources || [])
      .filter(x => typeof x.matched === "number"
                   && (x.outcome === "copied" || x.outcome === "current"))
      .map(x => `${x.label} ${x.matched.toLocaleString()}`)
      .join(" · ");
    return html`
      <div class=${`fda-line ${status.state === "ok" ? "is-ok" : "is-warn"}`}>
        <${Icon} name=${status.state === "ok" ? "check" : "info"} />
        <span class="fda-line-text">
          ${head}
          ${read && html`<span class="fda-line-read">${read} ${read.includes("·") ? "files each" : "files"}</span>`}
        </span>
        <button class="btn" disabled=${!!busy} onClick=${check}>
          <${Icon} name="refresh" /> ${busy === "check" ? "Checking…" : "Check now"}
        </button>
        <${TurnOff} busy=${busy} onOff=${uninstall} />
      </div>
      ${/* NC-187: the one-line state had nowhere to show a script's output, so a
           failed "Turn it off" said "see the details below" about nothing. */ ""}
      ${showOutput && output && html`<pre class="fda-output">${output}</pre>`}`;
  }

  // The two states that need the full explanation: macOS is refusing, or the
  // helper has never told us anything.
  return html`
    <div class="fda-panel">
      <div class="fda-head">
        <${Icon} name="lock" className="ic fda-head-ic" />
        <div class="fda-head-text">
          <${H}>${blocked
            ? `macOS is blocking ${status.blocked.join(" and ")}`
            : "The sync helper has not reported yet"}<//>
          <p>${blocked
            ? html`Your message history needs one macOS permission called <b>Full
                   Disk Access</b>, and nothing can ask for it — not this app, not
                   macOS. It has to be granted by hand, once, to the one small
                   helper below. <b>Network CRM itself never gets it.</b>`
            : html`It is installed but has not run yet, or it is an older copy
                   that cannot report back. Run it now to see the real answer.`}</p>
        </div>
      </div>

      ${status.sources.length > 0 && html`<${SourceLines} sources=${status.sources} />`}

      ${blocked && html`
        <div class="src-help">
          ${/* EVERY step wraps its content in one .fda-step-block, and it has to.
               .src-help .src-steps li is display:flex (digit in its own column,
               text beside it), so a bare `<b>` in the step text becomes a second
               FLEX ITEM and gets its own narrow column — "Open | Full Disk Access |
               in System Settings" laid out as three columns. The import rows never
               hit this because their step text goes through <Md>, which always
               yields a single <p>.

               Step 2 also holds the binary's path. It lives inside the step rather
               than below the list, so the file appears where step 2 says "drag in
               this one file:". */ ""}
          <ol class="src-steps">
            <li><div class="fda-step-block">Open <b>Full Disk Access</b> in System
                Settings — the button at the bottom of this panel goes straight
                there.</div></li>
            <li>
              <div class="fda-step-block">
                <div>Click <b>+</b>, then drag in (or select) this one file — "Show
                     the helper in Finder" opens the folder with it already picked:</div>
                <div class="fda-path-row">
                  <div class="src-path" title=${path}><code>${path}</code></div>
                  <button class="btn" onClick=${() => {
                    navigator.clipboard?.writeText(path).then(
                      () => toast("Path copied."), () => toast("Could not copy the path."));
                  }}>Copy path</button>
                </div>
              </div>
            </li>
            <li><div class="fda-step-block">Make sure its toggle is <b>on</b>. If
                macOS says the program has to be quit before this takes effect, you
                can ignore it — the helper runs for a few seconds at a time and has
                already stopped.</div></li>
            <li><div class="fda-step-block">Come back here and press <b>Check
                now</b>. It will say whether it worked, rather than leaving you to
                guess.</div></li>
          </ol>
          <p>Grant it to <b>that binary only</b> — not to Terminal, and not to
             Network CRM. It is one small compiled program whose only job is to
             copy those databases into a folder this app can already read; it has
             no network access of any kind. Its source code is published with the project, in <code>packaging/src/imessage_sync.swift</code>.</p>
        </div>`}

      <${GrantButtons} status=${status} />

      <div class="fda-foot">
        <button class="btn" disabled=${!!busy} onClick=${check}>
          <${Icon} name="refresh" /> ${busy === "check" ? "Checking…" : "Check now"}
        </button>
        ${/* Re-running the installer is safe and sometimes the fix (a helper the
             LaunchAgent lost, a binary that never landed). It deliberately does
             NOT rebuild an unchanged helper, because a new binary has a new code
             hash and macOS silently drops the grant when that changes. */ ""}
        <button class="btn" disabled=${!!busy} onClick=${install}>
          ${busy === "install" ? "Reinstalling…" : "Reinstall the helper"}
        </button>
        <${TurnOff} busy=${busy} onOff=${uninstall} />
        ${output && html`
          <button class="btn ghost" onClick=${() => setShowOutput(o => !o)}>
            ${showOutput ? "Hide details" : "Show details"}
          </button>`}
        ${checked && status.ran_ago_seconds !== null && html`
          <span class="fda-stamp">checked ${ago(status.ran_ago_seconds)}</span>`}
      </div>
      ${showOutput && output && html`<pre class="fda-output">${output}</pre>`}
    </div>`;
}
