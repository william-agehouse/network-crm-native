// Onboarding — the first-run wizard, and the only screen a stranger sees before
// the app is theirs. Seven steps: what this is, who you are (NC-318), LinkedIn
// (NC-319), Apple Contacts, iMessage, remember three people, an optional AI key.
// Nothing here is required; "Skip setup" is always visible.
//
// The three sources are the ones that matter most (NC-307), and LinkedIn comes
// first among them for one reason: its file arrives by email a day or two after
// you ask, so asking first means it is on its way while you do the rest.
//
// Contacts comes before iMessage, and it is a quality choice, not a hard
// dependency. imessage_import.py can resolve a handle's name on its own, straight
// from the live macOS Address Book, and creates the contact itself if the name is
// new (imessage_import.py:124-148, :220-236) — so this order is not what makes
// iMessage work. It is what makes the result complete: db.find_by_handle
// (imessage_import.py:211) checks crm.db before iMessage ever touches the Address
// Book, so a contact already there from the vCard import — phone, email, company,
// title all filled in — gets its message stats attached to that full record,
// instead of to a bare name iMessage had to invent on its own.
//
// Two rules the copy follows. ASD-STE100 (docs/DESIGN_DIRECTION.md §3): one
// meaning per word, active voice, one instruction per sentence — this is
// procedural text, which is what the standard was written for. And the soft-ask:
// before any file picker or API key, this screen states what the app will read
// and what stays on the Mac. The user should never meet a system
// dialog they were not told about.
import { html, useState, useEffect, useRef } from "../vendor/preact-htm.module.js";
import { api, toast, roleStr, relDate, CHANNEL_LABEL, noteImportCeiling, importTimeoutMs } from "./api.js";
import { Avatar, Tags, Empty, Md, OpenHelpers, AV, PathBox } from "./ui.js";
import { Icon } from "./icons.js";
import { FdaPanel } from "./fda.js";
import { FORMAT, ANTHROPIC_SPEND_HINT, Help } from "./ledger.js";

// Icons for the two sources this wizard shows. Every other source still has a
// home on the Data page (dataview.js) — this map just no longer needs to carry
// them, now that ManualSourceRow and the mixed "Found on this Mac" list are gone
// from the wizard.
const STEP_ICON = { contacts: "person", imessage: "imessage" };

// LinkedIn, email and Gmail used to live here too — MANUAL_KEYS = ["linkedin",
// "email", "gmail"], each with a MANUAL_WHY blurb, injected into "Your data" as
// a third section below "Found on this Mac". They were removed: none of the
// three fits a setup that should take two minutes on a stranger's Mac.
//   - gmail needs the person's own Google sign-in files and a list of accounts
//     (the GMAIL_ACCOUNTS setting, see crm/gmail_import.py) before it can do
//     anything, wizard or not.
//   - linkedin needed a token the user had to leave the app and go generate
//     first (LinkedIn's own OAuth tools page).
//   - email needs a .mbox export done in Apple Mail or Google Takeout before
//     this app ever sees a file.
// Gmail and email still wait on the Data page. LinkedIn is back (NC-319), and
// not by the token: the one step that fits two minutes is ASKING LinkedIn for
// the connections file, in the browser, which costs no token and no developer
// app. The file arrives later, and the Data page and the People page carry on
// from there. Do not bring the token route into this wizard; that is still why
// it left.

async function finish(onDone) {
  try { await api("/api/onboarding", { method: "POST", body: JSON.stringify({ done: true }) }); }
  catch (e) { toast(e.message); }
  onDone();
}

/* ── Step 1 — What this is ────────────────────────────────────────────────────
   Step one is a plain spec for someone who has never seen the app: what it
   needs, what it does, and what stays on the machine. Three sentences each, no
   promises the code does not keep. */
function Fact({ label, children }) {
  return html`
    <p class="onb-sub"><b style="color:var(--ink)">${label}</b> ${children}</p>`;
}

function StepHello({ onNext, total }) {
  return html`
    <div class="onb-step onb-hello">
      <div class="onb-mark" aria-hidden="true">n</div>
      <h1 class="onb-title">Network CRM</h1>
      <p class="onb-lede">
        This app remembers your network: who each person is, how you met, and
        when you last spoke.
      </p>
      <div style="display:flex;flex-direction:column;gap:var(--gap-sm);text-align:left;max-width:26rem">
        ${/* NC-190. This said the app "never asks macOS for access to your whole
             disk" — true under the old rule, false since NC-123 made the sync
             helper the route the app leads with. The soft-ask rule in this file's
             header is why both routes are named HERE, before any step: the helper
             route ends in System Settings, and nobody should meet that without
             being told first. The simple route leads (invariant 6). */ ""}
        <${Fact} label="What it needs.">
          A small helper that copies your messages, contacts and calendar for you.
          You give it Full Disk Access in System Settings; this app itself never
          gets it. Or, if you would rather grant nothing, the files you copy or
          export yourself.
        </${Fact}>
        <${Fact} label="What it does.">
          It reads those files, scores every relationship, and keeps the notes you
          write about people.
        </${Fact}>
        <${Fact} label="What stays here.">
          All of it. Your messages live in a database on this Mac. The app uploads
          nothing.
        </${Fact}>
      </div>
      <p class="onb-quiet-line">Setup takes ${total} steps. You can skip any of them.</p>
      <button class="btn primary onb-cta" onClick=${onNext}>Set it up</button>
    </div>`;
}

/* ── Step 2 — Who you are (NC-318) ─────────────────────────────────────────────
   Your name and email decide which side of every conversation is yours: without
   them the importers keep any address instead of real two-way conversations,
   and drafts are written for "the user" (crm/api_instance.py identity_status).
   The Data page asks too, but in a row most people never open, so it is asked
   here, first. Prefilled with whatever is already set, so setting up again
   shows what is there rather than an empty form. */
function StepYou({ onNext }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    api("/api/settings").then(d => {
      const value = k => ((d.settings || []).find(x => x.key === k) || {}).value || "";
      setName(value("SELF_NAME")); setEmail(value("SELF_EMAIL"));
    }).catch(() => {});
  }, []);

  const typed = name.trim() || email.trim();
  const save = async () => {
    const problem = email.trim() ? FORMAT.SELF_EMAIL(email.trim()) : "";
    if (problem) { setNote(problem); return; }
    setBusy(true); setNote("");
    try {
      let warning = "";
      for (const [key, value] of [["SELF_NAME", name.trim()], ["SELF_EMAIL", email.trim()]]) {
        if (!value) continue;   // an empty box never clears what is already set
        const d = await api("/api/settings", { method: "POST", body: JSON.stringify({ key, value }) });
        warning = warning || d.warning || "";
      }
      // NC-240's rule: a save that could not lock the settings file says so.
      if (warning) toast(warning);
      onNext();
    } catch (e) { setNote(e.message); }
    setBusy(false);
  };

  return html`
    <div class="onb-step">
      <h2 class="onb-h2">Who you are</h2>
      <p class="onb-sub">
        Your name, and the email address you write from. The app uses them to
        tell your side of a conversation from everyone else's, and drafts it
        writes use your name instead of calling you "the user".
      </p>
      <p class="onb-privacy">Both are kept in this app's settings on this Mac.</p>
      <label class="section-label" for="onb-name">Your name</label>
      <input id="onb-name" class="input onb-key-input" type="text" autocomplete="name"
        placeholder="Ada Lovelace" value=${name}
        onInput=${e => { setName(e.target.value); setNote(""); }} />
      <label class="section-label" for="onb-email">Your email address</label>
      <input id="onb-email" class="input onb-key-input" type="email" autocomplete="email"
        spellcheck="false" autocapitalize="off" placeholder="ada@example.com" value=${email}
        onInput=${e => { setEmail(e.target.value); setNote(""); }}
        onKeyDown=${e => { if (e.key === "Enter") save(); }} />
      ${note && html`<div class="field-note" role="status">${note}</div>`}
      <button class="btn primary onb-cta" disabled=${busy} onClick=${save}>
        ${busy ? "Saving…" : typed ? "Save and continue" : "Continue"}
      </button>
      ${!typed && html`<p class="onb-quiet-line">You can set both later in Settings.</p>`}
    </div>`;
}

/* ── Step 3 — LinkedIn (NC-319) ────────────────────────────────────────────────
   The source most people have, and the slowest to arrive: LinkedIn emails the
   connections file a day or two after you ask. So this step does the one thing
   that can be done now, which is asking, with a link to LinkedIn's own page in
   the browser. "I have asked for it" records that (crm/api_ledger.py,
   /api/data/linkedin-asked), so the People page and the Data page can say they
   are waiting for the file until it is imported. One route, the recommended
   one (NC-307): the token and the second-Mac reader stay on the Data page. */
const LINKEDIN_DATA_PAGE = "https://www.linkedin.com/mypreferences/d/download-my-data";
// Drawn by Help, the same numbered layout every source's steps use.
const LINKEDIN_STEPS = [
  "1. Press **Open LinkedIn's data page**. It opens in your browser.",
  "2. Choose **Connections** only. That is the file that arrives first.",
  "3. Press **Request archive**. LinkedIn may ask for your password.",
  "4. Come back here and press **I have asked for it**.",
].join("\n");

/* NC-320. Said on the step BEFORE the first one that looks for files on this Mac
   (/api/import/detect, from the Contacts step on): that search reaches Desktop,
   Downloads and WhatsApp's protected folder, and macOS names the asker after
   the engine's interpreter, so the dialog says "python3", which nobody setting
   up a contacts app expects. tests/test_data_settings_fixes.py holds it to the
   step just before StepContacts in STEPS. */
function PromptWarning() {
  return html`<p class="onb-privacy">Next, the app looks on this Mac for files to
    import, so macOS may ask whether “python3” or “Python” may read your Desktop,
    Downloads or WhatsApp's folder. That is this app's own engine asking, not
    another program, and your answer affects only that one source.</p>`;
}

function StepLinkedIn({ onNext }) {
  const [asked, setAsked] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/api/data/linkedin-wait").then(d => setAsked(!!d.waiting)).catch(() => {});
  }, []);

  const markAsked = async () => {
    setBusy(true);
    try {
      const d = await api("/api/data/linkedin-asked", { method: "POST",
        body: JSON.stringify({ asked: true }) });
      setAsked(!!d.waiting);
      if (!d.waiting) toast("LinkedIn people are already here, so there is nothing to wait for.");
    } catch (e) { toast(e.message); }
    setBusy(false);
  };

  return html`
    <div class="onb-step">
      <h2 class="onb-h2">LinkedIn</h2>
      <p class="onb-sub">
        LinkedIn sends a copy of your connections by email, usually within a day
        or two of asking. Ask now, so it is on its way while you set up the rest.
      </p>
      <${Help} text=${LINKEDIN_STEPS} />
      <div class="src-run">
        <a class="btn" href=${LINKEDIN_DATA_PAGE} target="_blank" rel="noopener">
          <${Icon} name="external" /> Open LinkedIn's data page</a>
        <button class="btn primary" disabled=${busy || asked} onClick=${markAsked}>
          ${asked ? html`Asked <${Icon} name="check" />` : "I have asked for it"}</button>
      </div>
      ${asked && html`<p class="onb-sub">When the email comes, download and unzip the
        file, then choose Connections.csv on the LinkedIn connections row of the Data
        page. Until then, the People page says it is waiting.</p>`}
      <p class="onb-privacy">This app sends nothing to LinkedIn. You ask in your own
        browser, and the file LinkedIn sends stays on this Mac. LinkedIn messages and
        profile facts take more setup, and wait on the Data page.</p>
      <${PromptWarning} />
      <button class="btn primary onb-cta" onClick=${onNext}>Continue</button>
    </div>`;
}

/* ── Steps 4–5 — Apple Contacts, then iMessage ────────────────────────────────
   The wizard's "your data" moment, split into the two sources a first run
   needs, in the order that produces the most complete result (see the header
   comment above for why that is an order and not a dependency). runImport and
   FindingRow are shared by both steps, and by nothing else now that
   ManualSourceRow (LinkedIn/email/Gmail) is gone. */
/** One import run boiled down to what a wizard row can show: did it work, and
    the single line worth reading. Full logs and receipts stay in Data. */
async function runImport(source, path) {
  try {
    const d = await api("/api/import/run", {
      method: "POST",
      // NC-176: the wizard must not give up early. A first import can take
      // minutes, so the wait limit is long; a first import that is merely slow
      // must not be shown as a failure.
      timeoutMs: importTimeoutMs(),
      body: JSON.stringify({ source, path }),
    });
    const lines = (d.output || "").trim().split("\n").filter(Boolean);
    return { ok: !!d.ok, line: lines[lines.length - 1] || (d.ok ? "Imported" : "Import failed") };
  } catch (e) { return { ok: false, line: e.message }; }
}

function FindingRow({ finding, onImported }) {
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState(null);
  const [ok, setOk] = useState(null);

  const run = async () => {
    setBusy(true); setLog(null);
    const r = await runImport(finding.source, finding.path);
    setOk(r.ok); setLog(r.line);
    if (r.ok) onImported(finding.source);
    setBusy(false);
  };

  const countLabel = finding.count > 0
    ? (finding.source === "whatsapp" ? `${finding.count.toLocaleString()} conversations`
     : finding.source === "imessage" ? `${finding.count.toLocaleString()} contacts`
     : finding.source === "contacts" ? `${finding.count.toLocaleString()} contacts`
     : null)
    : null;

  return html`
    <div class="onb-found-row" style="min-height:var(--row-h)">
      <span class="src-icon"><${Icon} name=${STEP_ICON[finding.source]} /></span>
      <div class="grow">
        <div class="src-name">${finding.label}${countLabel ? html` <span class="onb-count">${countLabel}</span>` : ""}</div>
        ${finding.mtime && html`<div class="src-status">${finding.mtime}</div>`}
        ${log !== null && html`<div class=${"onb-found-log" + (ok ? " ok" : " err")}>${log}</div>`}
      </div>
      <button class="btn primary" disabled=${busy || ok === true} onClick=${run}>
        ${ok === true ? html`Imported <${Icon} name="check" />` : busy ? "Importing…" : "Import"}
      </button>
    </div>`;
}

/** Step 4 — Apple Contacts. First, but not a dependency: imessage_import.py can
 *  resolve a handle to a name on its own (crm/imessage_import.py:124-148), and
 *  will create the contact itself if the name is new (:220-236). What this step
 *  actually buys is completeness — db.find_by_handle (:211) is checked before
 *  iMessage ever touches the Address Book, so a contact already here carries
 *  phone, email, company and title into that match, instead of arriving as a
 *  bare name. */
function StepContacts({ onNext, contactsImported, onContactsImported }) {
  const [spec, setSpec] = useState(null);
  const [found, setFound] = useState(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [runLog, setRunLog] = useState(null);
  const [runOk, setRunOk] = useState(null);

  useEffect(() => {
    api("/api/import/sources").then(d => {
      noteImportCeiling(d.import_timeout_sec);
      setSpec((d.sources || []).find(s => s.key === "contacts") || null);
    }).catch(() => setSpec(null));
    api("/api/import/detect").then(d => setFound((d.found || []).filter(f => f.source === "contacts"))).catch(() => setFound([]));
  }, []);

  const pick = async () => {
    try {
      const d = await api("/api/import/pick-file", { method: "POST", body: JSON.stringify({ kind: spec.pick_kind }) });
      // A cancel is silent; a picker that could not open must say so, or
      // "Choose file" reads as a dead button. See handle_import_pick_file.
      if (d.path) setPath(d.path);
      else if (d.error) toast(d.error);
    } catch (e) { toast(e.message); }
  };

  const run = async () => {
    setBusy(true); setRunLog(null);
    const r = await runImport("contacts", path);
    setRunOk(r.ok); setRunLog(r.line);
    if (r.ok) onContactsImported();
    setBusy(false);
  };

  // contactsImported is lifted to the shell rather than kept as local state
  // here, so a Back-then-Continue round trip — which remounts this component —
  // still remembers a successful import instead of forgetting it and firing a
  // false alarm below. "Skip setup" always works, and so does clicking
  // Continue with nothing imported; this only makes the cost of that specific
  // choice visible at the moment it is chosen, per the file header's soft-ask
  // rule applied to a skip instead of a picker.
  const next = () => {
    if (!contactsImported) {
      toast("Skipping this means iMessage still adds people, but as bare names — no phone, email, company or title.");
    }
    onNext();
  };

  return html`
    <div class="onb-step">
      <h2 class="onb-h2">Apple Contacts</h2>
      <p class="onb-sub">
        Import your address book first. iMessage does not need this to work —
        it can find names on its own. But a contact already on this list
        arrives complete: phone, email, company, title. A name iMessage
        creates by itself has none of that.
      </p>
      ${/* NC-191. This used to say it reads "only the vCard file you pick",
           from before the sync helper copied Contacts: the helper's copy is
           offered right below it, under "Found on this Mac". */ ""}
      <p class="onb-privacy">
        This reads the sync helper's copy of your address book if it has made
        one, and otherwise only the card file you pick. It writes what it reads
        to a database on this Mac.
      </p>

      ${found && found.length > 0 && html`
        <div>
          <div class="section-label">Found on this Mac</div>
          <div class="onb-found-list">
            ${found.map(f => html`
              <${FindingRow} key=${f.source + f.path} finding=${f} onImported=${onContactsImported} />`)}
          </div>
        </div>`}

      ${spec && html`
        <div class="src-help"><${Md} text=${spec.export_help} /></div>
        <div class="src-run">
          <${OpenHelpers} spec=${spec} />
          ${spec.kind !== "none" && html`
            <button class="btn" onClick=${pick}>${spec.kind === "folder" ? "Choose a folder" : "Choose a file"}</button>`}
          ${/* NC-178: the box the picker's failure message points at. First run
               is where a broken picker hurts most — there is nothing else on
               the screen to try. */ ""}
          ${spec.kind !== "none" && html`
            <${PathBox} kind=${spec.kind} path=${path} onPath=${setPath} />`}
          <button class="btn primary" disabled=${busy || (spec.kind !== "none" && !path.trim())} onClick=${run}>
            ${busy ? "Importing…" : "Run the import"}
          </button>
        </div>
        ${runLog !== null && html`<div class=${"onb-found-log" + (runOk ? " ok" : " err")}>${runLog}</div>`}
      `}

      <button class="btn primary onb-cta" onClick=${next}>Continue</button>
    </div>`;
}

/** Step 5 — iMessage. The payload: years of conversation text. Matching happens
 *  in imessage_import.py's own two-stage lookup — crm.db first, the macOS
 *  Address Book second (see StepContacts above) — so this step's copy only has
 *  to say what a person gets from the step before it, not claim iMessage
 *  depends on it. The copy-three-files flow is unchanged from before this
 *  wizard split "Your data" in two: spec.export_help (crm/importers.py's
 *  "imessage" entry) carries the WAL warning verbatim, so there is exactly one
 *  copy of it to keep correct. */
function StepMessages({ onNext }) {
  const [spec, setSpec] = useState(null);
  const [found, setFound] = useState(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [runLog, setRunLog] = useState(null);
  const [runOk, setRunOk] = useState(null);

  // Re-detect: the helper writes a fresh chat.db copy into the import folder, so
  // installing it (or unblocking it) can turn "nothing found" into "found" without
  // the user touching a file. FdaPanel calls this when either of those happens.
  const detect = () => {
    api("/api/import/detect")
      .then(d => setFound((d.found || []).filter(f => f.source === "imessage")))
      .catch(() => setFound([]));
  };

  useEffect(() => {
    api("/api/import/sources").then(d => {
      noteImportCeiling(d.import_timeout_sec);
      setSpec((d.sources || []).find(s => s.key === "imessage") || null);
    }).catch(() => setSpec(null));
    detect();
  }, []);

  const pick = async () => {
    try {
      const d = await api("/api/import/pick-file", { method: "POST", body: JSON.stringify({ kind: spec.pick_kind }) });
      // A cancel is silent; a picker that could not open must say so, or
      // "Choose file" reads as a dead button. See handle_import_pick_file.
      if (d.path) setPath(d.path);
      else if (d.error) toast(d.error);
    } catch (e) { toast(e.message); }
  };

  const run = async () => {
    setBusy(true); setRunLog(null);
    const r = await runImport("imessage", path);
    setRunOk(r.ok); setRunLog(r.line);
    setBusy(false);
  };

  return html`
    <div class="onb-step">
      <h2 class="onb-h2">iMessage</h2>
      <p class="onb-sub">
        This step imports your iMessage history. It matches each message to a
        person by phone number or email. A person already in your address
        book — imported in the step before this — arrives with more than
        just a name.
      </p>

      ${/* NC-193: above the manual steps, not below them. iMessage's own help
           text (shown further down) says the helper is set up from "the panel
           at the top of this page", which is where the Data page has it; here
           it sat at the bottom, under the copy-it-yourself steps, so the simple
           route came second on the one screen that introduces it. */ ""}
      ${/* The one-time-setup route, and the only one that exists. A pasted copy is
            a snapshot: it stops being true the moment you send a message, so the
            manual flow below is a chore you repeat forever. macOS offers no
            permission narrower than Full Disk Access for ~/Library/Messages —
            there is no "Messages only" toggle, and a file picker does not help
            because mandatory access control overrides your own selection for that
            path (packaging/IMESSAGE_SYNC.md) — so something must hold FDA, and the
            only question is how small that something is. Here it is one short
            compiled binary that copies those databases (and Contacts', Calendar's
            and WhatsApp's) and does nothing else — no
            network, no subprocesses, one import. The app never holds the permission
            and never reads the protected path.

            Rendered only when this copy of the app actually carries the installer:
            a bundle built without swiftc ships no helper, and offering an option
            that cannot be delivered is worse than staying quiet. Granting the
            permission stays the user's own act in System Settings — nothing here
            can or should do it for them.

            Three paragraphs ending in "Run this in Terminal", with the installer's
            path printed for the user to copy, would be correct, complete, and
            useless to anyone who uses the packaged .app and does not open a
            Terminal: the app would never tell them how to grant the permission.
            So this is the same FdaPanel the Data page uses — a button that installs
            the helper, a button that opens the right System Settings pane with the
            right binary selected in Finder, and a check that says whether it
            worked. One component for both surfaces, so the two can never disagree
            about a permission this app cannot ask for. */ ""}
      <${FdaPanel} onChanged=${detect} />

      ${found && found.length > 0 && html`
        <div>
          <div class="section-label">Found on this Mac</div>
          <div class="onb-found-list">
            ${found.map(f => html`
              <${FindingRow} key=${f.source + f.path} finding=${f} onImported=${() => {}} />`)}
          </div>
        </div>`}

      ${spec && html`
        <div class="src-help"><${Md} text=${spec.export_help} /></div>
        <div class="src-run">
          <${OpenHelpers} spec=${spec} />
          ${spec.kind !== "none" && html`
            <button class="btn" onClick=${pick}>${spec.kind === "folder" ? "Choose a folder" : "Choose a file"}</button>`}
          ${/* NC-178: the box the picker's failure message points at. First run
               is where a broken picker hurts most — there is nothing else on
               the screen to try. */ ""}
          ${spec.kind !== "none" && html`
            <${PathBox} kind=${spec.kind} path=${path} onPath=${setPath} />`}
          <button class="btn primary" disabled=${busy || (spec.kind !== "none" && !path.trim())} onClick=${run}>
            ${busy ? "Importing…" : "Run the import"}
          </button>
        </div>
        ${runLog !== null && html`<div class=${"onb-found-log" + (runOk ? " ok" : " err")}>${runLog}</div>`}
      `}


      <p class="onb-quiet-line">WhatsApp, email and more wait in Data.</p>
      <button class="btn primary onb-cta" onClick=${onNext}>Continue</button>
    </div>`;
}

/* ── Step 6 — Remember three people ──────────────────────────────────────── */
function ReviewCard({ person, index, total, onSave, onSkip }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const taRef = useRef(null);

  useEffect(() => { setText(""); taRef.current?.focus(); }, [person.id]);

  const msgs = person.last_messages;
  const lastMsg = msgs && msgs.length ? msgs[msgs.length - 1] : null;

  const save = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    await onSave(person, text.trim());
    setBusy(false);
  };

  const skip = async () => {
    setBusy(true);
    await onSkip(person);
    setBusy(false);
  };

  const first = person.name.split(" ")[0];
  return html`
    <div class="card onb-review-card">
      <div class="stack-head">
        <${Avatar} c=${person} size=${AV.hero} ring />
        <div style="flex:1;min-width:0">
          <div class="stack-name">${person.name}</div>
          ${roleStr(person) && html`<div class="stack-role">${roleStr(person)}</div>`}
          <div class="stack-tags"><${Tags} c=${person} /></div>
        </div>
      </div>

      ${lastMsg && html`
        <div class="quote">
          <div class="q-meta">Last exchange · ${CHANNEL_LABEL[lastMsg.channel] || lastMsg.channel} · ${relDate(lastMsg.ts)}</div>
          <div class="q-body">${(lastMsg.body || "").slice(0, 220)}${(lastMsg.body || "").length > 220 ? "…" : ""}</div>
        </div>`}

      <label class="stack-ask" style="display:block" for="onb-note">How do you know ${first}?</label>
      <textarea
        id="onb-note"
        ref=${taRef}
        class="input" rows="2"
        placeholder="Where you met. What they do. What you want to remember."
        value=${text}
        onInput=${e => setText(e.target.value)}
        onKeyDown=${e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); } }}
      />
      <div class="stack-actions">
        <button class="btn primary" disabled=${busy || !text.trim()} onClick=${save}>Save</button>
        <button class="btn quiet" disabled=${busy} onClick=${skip}>Skip</button>
        <span class="spacer" />
        <span class="stack-remaining">${index + 1} of ${total}</span>
      </div>
    </div>`;
}

function StepRemember({ onNext }) {
  const [queue, setQueue] = useState(null);
  const [idx, setIdx] = useState(0);
  const [doneCount, setDoneCount] = useState(0);

  useEffect(() => {
    // Was /api/today, sliced to 3 — which coupled this step to a page it does
    // not appear on, and broke it when that page was deleted (NC-009).
    api("/api/describe-queue").then(d => setQueue(d.queue || [])).catch(() => setQueue([]));
  }, []);

  const advance = () => setIdx(i => i + 1);

  const onSave = async (person, body) => {
    try {
      await api(`/api/contact/${person.id}/note`, { method: "POST", body: JSON.stringify({ body }) });
      setDoneCount(n => n + 1);
      advance();
    } catch (e) { toast(e.message); }
  };

  const onSkip = async (person) => {
    try { await api(`/api/contact/${person.id}/skip`, { method: "POST", body: JSON.stringify({}) }); }
    catch (e) { toast(e.message); }
    advance();
  };

  if (queue === null) return html`
    <div class="onb-step">
      <h2 class="onb-h2">Remember three people</h2>
      <p class="onb-sub">Picking three people…</p>
    </div>`;

  // Zero data is a real state on a fresh Mac, not a failure. Say what is true,
  // say what fills it, and keep the way forward.
  if (queue.length === 0) {
    return html`
      <div class="onb-step">
        <h2 class="onb-h2">Remember three people</h2>
        <${Empty} title="Nobody to review yet"
          hint="This step picks three people worth remembering once your network is in.">
          Import a source first. This step then fills itself.
        </${Empty}>
        <button class="btn primary onb-cta" onClick=${onNext}>Continue</button>
      </div>`;
  }

  const current = queue[idx];
  if (!current) {
    return html`
      <div class="onb-step onb-remember-done">
        <div class="onb-mark"><${Icon} name="check" /></div>
        <h2 class="onb-h2">${doneCount} ${doneCount === 1 ? "person" : "people"} remembered</h2>
        <p class="onb-sub">That is the habit: a few minutes a day, a few people at a time.</p>
        <button class="btn primary onb-cta" onClick=${onNext}>Continue</button>
      </div>`;
  }

  return html`
    <div class="onb-step">
      <h2 class="onb-h2">Remember three people</h2>
      <p class="onb-sub">
        Write one line about each person. The app shows you that line the next
        time the name comes up.
      </p>
      <${ReviewCard} person=${current} index=${idx} total=${queue.length} onSave=${onSave} onSkip=${onSkip} />
    </div>`;
}

/* ── Step 7 — AI key (optional) ───────────────────────────────────────────────
   The soft-ask, applied to the one thing here that sends data off the Mac: say
   what the key buys, say exactly what leaves, and keep the no-key path a first
   class button rather than a link. */
function StepAI({ onDone }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);

  const saveAndFinish = async () => {
    setBusy(true);
    try {
      if (key.trim()) {
        const d = await api("/api/settings", { method: "POST", body: JSON.stringify({ key: "ANTHROPIC_API_KEY", value: key.trim() }) });
        // NC-240: the key is saved either way; if the file holding it could not
        // be locked down, say so before the wizard closes over it (NC-150).
        if (d.warning) toast(d.warning);
      }
      await finish(onDone);
    } catch (e) { toast(e.message); setBusy(false); }
  };

  return html`
    <div class="onb-step">
      <h2 class="onb-h2">Add an AI key <span class="onb-optional">(optional)</span></h2>
      <p class="onb-sub">
        An Anthropic API key turns on Ask, story rewrites and message drafts.
        Everything else works without a key.
      </p>
      ${/* The disclosure reads at the same weight as the value claim above it.
             A faint italic aside here would be a quiet way to say the one thing
             the user most needs to hear before they paste a key. */ ""}
      <p class="onb-sub">
        Ask sends your question to Anthropic. To answer it, Ask can also send
        names, notes and the message text that matches. Your database stays on
        this Mac.
      </p>
      <label class="section-label" for="onb-key">Anthropic API key</label>
      <input
        id="onb-key"
        class="input onb-key-input"
        type="password"
        autocomplete="off"
        placeholder="sk-ant-…"
        value=${key}
        onInput=${e => setKey(e.target.value)}
      />
      <p class="onb-privacy">${ANTHROPIC_SPEND_HINT}</p>
      <div class="onb-ai-actions">
        <button class="btn primary" disabled=${busy} onClick=${saveAndFinish}>
          ${key.trim() ? "Save the key and finish" : "Finish"}
        </button>
        ${key.trim() && html`
          <button class="btn quiet" disabled=${busy} onClick=${() => finish(onDone)}>Finish without a key</button>`}
      </div>
      <p class="onb-quiet-line">You can add or remove the key later in Settings.</p>
    </div>`;
}

/* ── Shell ──────────────────────────────────────────────────────────────────── */
const STEPS = [StepHello, StepYou, StepLinkedIn, StepContacts, StepMessages,
               StepRemember, StepAI];
// the data steps can grow tall once export help expands — scroll inside the
// panel so Continue (and the progress row) stay reachable on a short window
const PANEL_SCROLL = { maxHeight: "calc(100vh - 8rem)", overflowY: "auto" };

export function Onboarding({ onDone }) {
  const [step, setStep] = useState(0);
  // Lifted above StepContacts rather than kept local to it — see that
  // component's own comment for why a Back-then-Continue round trip needs
  // this to survive a remount.
  const [contactsImported, setContactsImported] = useState(false);
  const Cur = STEPS[step];
  const total = STEPS.length;

  const next = () => setStep(s => Math.min(s + 1, total - 1));
  const back = () => setStep(s => Math.max(s - 1, 0));
  const skipSetup = () => finish(onDone);

  return html`
    <div class="onb-overlay">
      <button class="btn quiet onb-skip" onClick=${skipSetup}>Skip setup</button>
      <div class="onb-panel" style=${PANEL_SCROLL}>
        <${Cur} onNext=${next} onDone=${onDone} total=${total}
          contactsImported=${contactsImported}
          onContactsImported=${() => setContactsImported(true)} />
      </div>
      ${/* Progress, and a way back. The wizard could only ever go forward, which
            made a mistyped key or a wrong file a dead end you had to skip out
            of. */ ""}
      <div style="display:flex;align-items:center;gap:var(--gap);margin-top:var(--pad-view)">
        <button class="btn quiet mini" style=${{ visibility: step === 0 ? "hidden" : "visible" }}
          onClick=${back}><${Icon} name="chevronLeft" /> Back</button>
        <div class="dots" role="progressbar" aria-label="Setup progress"
             aria-valuemin="1" aria-valuemax=${total} aria-valuenow=${step + 1}>
          ${STEPS.map((_, i) => html`
            <span key=${i} class=${"dot" + (i < step ? " done" : i === step ? " on" : "")} />`)}
        </div>
        <span class="onb-quiet-line">Step ${step + 1} of ${total}</span>
      </div>
    </div>`;
}
