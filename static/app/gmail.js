// Gmail's setup, on the screen, as a thing somebody can finish.
//
// NC-117. The instructions in crm/importers.py said "Click **Connect** below —
// a browser tab opens for Google sign-in." There was no Connect control here,
// and there never had been. They also named a file inside a folder with no
// button to open it, under a name derived by a regular expression that appears
// nowhere on screen, and pointed at a section of the README that does not
// exist. Everything a person needed was true and none of it was reachable.
//
// So this panel is the missing half of that sentence. It is built as three
// steps because the setup genuinely has three, they fail differently, and
// collapsing them is how the old version managed to be wrong about all of them
// at once:
//
//   1. the address      — which mailbox, saved as GMAIL_ACCOUNTS
//   2. the file         — what Google gives you once, from its Cloud Console
//   3. the sign-in      — a person clicking Allow in a Google tab
//
// Only the third can fail because of something outside this Mac, and only the
// third takes time, which is why it is the one with a waiting state.
//
// It follows static/app/linkedin.js, deliberately and closely: a trip through
// somebody else's developer console needs numbered steps, a button that opens
// the right page rather than a URL to copy, and a step-by-step that says what
// goes wrong at each one. That panel exists because one sentence was not enough
// for a five-step journey; this is the same journey with a file at the end of it.

import { html, useState, useEffect, useCallback, useRef } from "../vendor/preact-htm.module.js";
import { api, toast } from "./api.js";
import { Icon } from "./icons.js";
import { SkelRows, openTarget } from "./ui.js";

// How often to ask "have they clicked Allow yet". Same cadence as the semantic
// install's poll in settings.js: slow enough to be free, fast enough that the
// screen changes about when the person expects it to.
const POLL_MS = 2500;

/* The steps, as data, because the wording is the deliverable here and it will be
   revised. Each one names the specific way it goes wrong, for the reason
   linkedin.js gives for its own list: every mistake in this journey fails LATE.
   A Web-application client saves fine and is refused by Google at sign-in; a
   project without the Gmail API switched on signs in fine and collects nothing;
   an address typed differently from the one you sign in with produces a token
   that works and matches no mail.

   The worst of them is the fourth step, and it fails latest of all: a project
   left on Google's "Testing" publishing status hands out permissions that
   Google deletes after seven days. Everything works, for a week. Then this app
   starts saying "Gmail needs you to sign in again" every week, and the person
   has no way to connect that to a radio button they did not press in a console
   they visited once. That is the reason that step is worded as strongly as it
   is. */
const STEPS = [
  html`<li><b>Make a project.</b> Open the Google Cloud Console with the button
    above and make a new project — any name will do, it is only a folder for one
    app's settings and it costs nothing.</li>`,
  html`<li><b>Turn on the Gmail API</b> for that project: <b>APIs and
    Services</b> → <b>Library</b>, search for <b>Gmail API</b>, open it and
    press <b>Enable</b>. Skip this and the sign-in still works — it is the
    reading of mail afterwards that fails, a confusing place to end up.</li>`,
  html`<li><b>Say what the app is called.</b> In the left menu open <b>Google
    Auth Platform</b> (Google renamed this from "OAuth consent screen"), fill in
    a name and your own address under <b>Branding</b>. Then open the tab
    called <b>Audience</b> and choose <b>External</b> — a personal Google
    account has no company to be Internal to, so it is the only choice
    offered.</li>`,
  html`<li><b>Press Publish app, on that same Audience page.</b> This is the step
    every other guide skips and the one that decides whether you do this once or
    every week: while it still says <b>Testing</b>, Google throws your permission
    away after seven days and this app will ask you to sign in again next
    Monday, and the Monday after that. Published, it lasts.</li>`,
  html`<li><b>Make the sign-in file.</b> Still in Google Auth Platform, open
    the <b>Clients</b> tab and press <b>Create client</b>. For the type, pick
    the one called <b>Desktop app</b> — not Web application: a web client has
    nowhere to send you back to on this Mac, and Google refuses it at sign-in
    with an error about a mismatched address. Press <b>Create</b>, then download
    the JSON file it offers. It lands in Downloads under a long name that starts
    with <code>client_secret_</code>. Leave that name alone: the <b>Choose the
    file Google gave you</b> button above these steps, beside your address,
    renames and files it for you.</li>`,
];

/* Google's own warning screen, met once, by everybody. It is worth saying in
   advance: a person who has just been walked through five steps and then meets
   a red "this app is not verified" screen reasonably stops. */
const UNVERIFIED_NOTE = html`
  <p class="setup-fine">The first time you press Sign in, Google will say it has
  not verified this app. That is expected: the app is the one you made a minute
  ago, on your own account, and it never leaves this Mac — nobody reviewed it
  because there is nobody else to protect from it. Choose <b>Advanced</b>, then
  the <b>Go to …</b> link underneath, then <b>Allow</b>.</p>`;

function looksLikeEmail(s) {
  const v = (s || "").trim();
  return v.includes("@") && !v.includes(",") && !v.includes(" ") && v.length > 3;
}

/** Add an address to GMAIL_ACCOUNTS through POST /api/settings.
 *
 *  Through the settings endpoint on purpose, never a writer of its own:
 *  crm/settings.py is explicit that it is the only thing that may touch
 *  secrets/.env, and linkedin.js's TokenBox makes the same point in the same
 *  words — a second BUTTON for one path, never a second path. */
function AddAccount({ existing, onAdded }) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const value = draft.trim().toLowerCase();

  const save = async () => {
    if (!looksLikeEmail(value) || busy) return;
    setBusy(true);
    try {
      const d = await api("/api/settings");
      const current = ((d.settings || []).find(s => s.key === "GMAIL_ACCOUNTS") || {}).value || "";
      // Append rather than replace: somebody with one mailbox already set up
      // must not lose it by adding a second, and the explicit
      // `address:file.json` form other people use has to survive untouched.
      const parts = current.split(",").map(x => x.trim()).filter(Boolean);
      const already = parts.some(p => p.split(":")[0].trim().toLowerCase() === value);
      if (!already) parts.push(value);
      const saved = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ key: "GMAIL_ACCOUNTS", value: parts.join(",") }),
      });
      setDraft("");
      // NC-240: a save that could not lock the settings file says so (NC-150).
      // The warning replaces "added", not the other way round: a toast wipes
      // the one before it.
      toast(saved.warning || (already ? `${value} was already set up.` : `${value} added.`));
      onAdded();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  const onKey = (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    else if (e.key === "Escape" && draft) { e.preventDefault(); setDraft(""); }
  };

  return html`
    <div class="gm-add">
      <label class="setup-label" for="gm-account-input">
        ${existing ? "Add another Google address" : "Which Google address?"}
      </label>
      <div class="gm-add-row">
        <input id="gm-account-input" class="input" type="email" autocomplete="off"
               spellcheck="false" autocapitalize="off" disabled=${busy}
               value=${draft} placeholder="you@gmail.com"
               onInput=${e => setDraft(e.target.value)} onKeyDown=${onKey} />
        <button class="btn primary" disabled=${!looksLikeEmail(value) || busy}
                onClick=${save}>${busy ? "Saving…" : "Add"}</button>
      </div>
      <p class="setup-fine">The mailbox you want read. It has to be the address
        you actually sign in to Google with.</p>
    </div>`;
}

/** One configured address, and whatever it is still missing. */
function AccountRow({ row, connect, onChanged }) {
  const [busy, setBusy] = useState(false);
  const waiting = connect.state === "running" && connect.account === row.email;

  const pickFile = async () => {
    setBusy(true);
    try {
      const d = await api("/api/import/pick-file", {
        method: "POST", body: JSON.stringify({ kind: "json" }),
      });
      // Three outcomes and not two — a path, a deliberate cancel (say nothing),
      // or a picker that could not open (say so). See handle_import_pick_file.
      if (d.path) {
        const r = await api("/api/gmail/credentials", {
          method: "POST",
          body: JSON.stringify({ email: row.email, path: d.path }),
        });
        toast(r.warning || `Saved as ${r.saved_as}.`);
        onChanged();
      } else if (d.error) { toast(d.error); }
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  const signIn = async () => {
    setBusy(true);
    try {
      const r = await api("/api/gmail/connect", {
        method: "POST", body: JSON.stringify({ email: row.email }),
      });
      toast(r.message);
      onChanged();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  /* NC-183. `sign_in_expired` outranks the token file, and that order is the
     whole point: Google taking a permission back leaves the file on this Mac
     exactly where it was, so the file says somebody signed in ONCE and nothing
     about now. Ranking it below the file is what printed "Connected" in this
     panel while the row two lines above said "Gmail needs you to sign in
     again" — the app disagreeing with itself inside one screen.

     Above `waiting` it must not go: while a Google tab is actually open, what
     the person needs to know is that it is open. */
  const expired = !!row.sign_in_expired;
  let says, tone;
  if (waiting) { says = "Waiting for you in the Google tab"; tone = "warn"; }
  else if (!row.has_credentials) { says = "Needs the file from Google"; tone = "warn"; }
  else if (expired) { says = "Google has taken the permission back"; tone = "warn"; }
  else if (!row.has_token) { says = "Ready to sign in"; tone = "plain"; }
  else { says = "Connected"; tone = "ok"; }

  return html`
    <div class=${"gm-account is-" + tone}>
      <div class="gm-account-head">
        <i class="gm-dot" aria-hidden="true"></i>
        <span class="gm-account-name">${row.email}</span>
        <span class="gm-account-says">${says}</span>
      </div>
      <div class="setup-actions">
        ${!row.has_credentials && html`
          <button class="btn primary" disabled=${busy} onClick=${pickFile}>
            ${busy ? "…" : "Choose the file Google gave you"}</button>`}
        ${/* One primary button, and it is whichever thing is actually needed.
             An expired permission used to leave "Sign in again" sitting third,
             quiet and grey, beside a line claiming the account was connected —
             the right control in the wrong place, which reads as no control at
             all when the page has already told you nothing is wrong. */ ""}
        ${row.has_credentials && (!row.has_token || expired) && html`
          <button class="btn primary" disabled=${busy || waiting} onClick=${signIn}>
            ${waiting ? "Waiting for Google…"
                      : expired ? "Sign in again" : "Sign in to Google"}</button>`}
        ${row.has_credentials && html`
          <button class="btn" disabled=${busy} onClick=${pickFile}>
            ${row.has_token ? "Replace the file" : "Choose a different file"}</button>`}
        ${row.has_token && !expired && !waiting && html`
          <button class="btn" disabled=${busy} onClick=${signIn}>Sign in again</button>`}
      </div>
    </div>`;
}

/** The panel. Rendered inside the Gmail source row on the Data page, not on the
 *  page itself: an install that has never wanted Gmail should not meet five
 *  steps about Google Cloud Console on the way past. */
export function GmailSetup({ onChanged, onAttention = null }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(false);
  const timer = useRef(null);

  const read = useCallback(() =>
    api("/api/gmail/status")
      .then(d => setSt(d))
      .catch(() => setErr(true)), []);

  useEffect(() => { read(); }, [read]);

  const connect = (st && st.connect) || {};
  const running = connect.state === "running";

  /* NC-076's contract, enforced by tests/test_page_truth.py: a panel that holds
     its own status has to tell the page whether it is asking for something, or
     "No source needs you now." can print directly underneath it while it asks.

     Gmail asks when an address is set up and still missing its file or its
     sign-in. It does NOT ask when nothing is configured — somebody who has
     never wanted Gmail is not being asked for anything by a folded row — and it
     does not ask while a Google tab is open, because the person is already
     doing the thing. Above the early returns on purpose: hooks may not sit
     behind a conditional, and reporting false before the status loads is
     correct, since a panel that has not loaded is not asking yet. */
  const asks = !!st && !running
    && ((st.needs_file || []).length > 0 || (st.needs_sign_in || []).length > 0);
  useEffect(() => {
    if (!onAttention) return undefined;
    onAttention(asks);
    // Reporting false on the way out is not tidiness — this panel lives inside a
    // source row that unmounts when the row is folded and when "Add a source"
    // closes. Without it, opening the Gmail row once and folding it again would
    // leave "needs you" latched true for the rest of the session, with nothing
    // on screen to explain it. The page's own setter ignores a repeat value, so
    // the transient false costs one render and only when `asks` actually moves.
    return () => onAttention(false);
  }, [onAttention, asks]);

  // Poll only while somebody is looking at a Google tab, and stop the moment it
  // settles — the same shape as the semantic install's poll in settings.js.
  useEffect(() => {
    if (!running) return undefined;
    timer.current = setInterval(read, POLL_MS);
    return () => { clearInterval(timer.current); timer.current = null; };
  }, [running, read]);

  // When the sign-in finishes, the source row above this needs to know: a
  // connected Gmail changes what the row may say about itself, and the two
  // moving separately is NC-076 again.
  const settled = connect.state === "succeeded" || connect.state === "failed";
  const lastSettled = useRef("");
  useEffect(() => {
    if (!settled) return;
    const stamp = connect.finished_at || "";
    if (stamp && stamp !== lastSettled.current) {
      lastSettled.current = stamp;
      if (onChanged) onChanged();
    }
  }, [settled, connect.finished_at, onChanged]);

  if (err) return html`<div class="settings-field-help">
    Gmail's setup did not load. Reload the page.</div>`;
  if (st === null) return html`<${SkelRows} n=${2} h=${36} />`;

  const rows = st.accounts || [];
  const minutes = Math.max(1, Math.round((st.connect_timeout_sec || 600) / 60));

  return html`
    <div class="gm-setup">
      ${rows.map(r => html`
        <${AccountRow} key=${r.email} row=${r} connect=${connect}
                       onChanged=${() => { read(); if (onChanged) onChanged(); }} />`)}

      ${running && html`
        <div class="gm-waiting" role="status">
          <${Icon} name="external" />
          <div>
            <b>A Google tab is open — choose Allow there.</b>
            <p class="setup-fine">Nothing is happening on this Mac until you do.
              Take as long as you like; the app gives up after ${minutes} minutes
              and you can start again.</p>
          </div>
        </div>`}

      ${connect.state === "failed" && html`
        <div class="gm-failed" role="status">
          <b>${connect.message}</b>
          ${(connect.tail || []).length > 0 && html`
            <details class="log-details">
              <summary>what Google said</summary>
              <pre class="src-log err">${connect.tail.join("\n")}</pre>
            </details>`}
        </div>`}

      ${connect.state === "succeeded" && !running && html`
        <p class="setup-fine">${connect.message}</p>`}

      ${/* The steps are here whenever an address is, not only while the file is
           missing. Two reasons: the
           person who needs step 4 most is the one who already has a file and is
           being asked to sign in again every week, and they would never see a
           block that only appears before the file exists. And a person halfway
           through the console wants to re-read step 3 without undoing step 5.
           Open by default only when the file is still missing, so it is a wall
           of text exactly once. */ ""}
      ${rows.length > 0 && html`
        <details class="gm-steps-block" open=${st.needs_file.length > 0}>
          <summary>How to make the sign-in file Google asks for</summary>
          <div class="setup-actions">
            <button class="btn" onClick=${() => openTarget("gmail_console")}>
              <${Icon} name="external" /> Open Google Cloud Console</button>
          </div>
          ${/* Why this is five steps through somebody else's console and not a
               button: reading mail is a restricted permission at Google, so a
               shared client that anyone could click through would need brand
               verification plus an annual third-party security review, and an
               unverified one is capped at 100 people behind a warning screen.
               Saying so is not an excuse — it is the thing that makes the effort
               make sense, and without it this page is just long. */ ""}
          <p class="setup-fine">Google will not let one app read many people's
            mail without a yearly audit, so each person makes their own key. It
            is five minutes, once.</p>
          <ol class="setup-steps">${STEPS}</ol>
          ${UNVERIFIED_NOTE}
          <p class="setup-fine"><b>Signing in again every week?</b> Step 4 is
            why. Go back to Google Auth Platform → Audience and check that it
            says <b>In production</b> and not <b>Testing</b>; Testing deletes
            the permission after seven days, every time.</p>
        </details>`}

      <${AddAccount} existing=${rows.length > 0}
                     onAdded=${() => { read(); if (onChanged) onChanged(); }} />

      ${rows.length === 0 && html`
        ${/* Both halves of htm's whitespace rule in one sentence: the explicit
             space before the tag, and "source" kept on the tag's own line so
             the closing </b> is not glued to it. Without both, htm renders this
             as "(.mbox)source". */ ""}
        <p class="setup-fine">Or skip all of this: the${" "}
          <b>Email (.mbox)</b> source imports a Google Takeout export of your
          mail with no account access at all.</p>`}
    </div>`;
}
