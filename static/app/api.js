// Fetch wrapper + tiny shared utilities. No state here — just plumbing.

// ── Access token, for the network shape ───────────────────────────────────────
// NC-099. The access token is load-bearing: it is what makes README's
// home-server shape work at all.
//
// The app is loopback-only by default and needs no token. Bound to a network
// address instead (APP_HOST off loopback + APP_TOKEN set), crm/app.py guards
// every /api route — so the laptop opens http://<host>:<port>/?token=... , the
// callback at the bottom of this section stores the token and reloads clean, and
// every later call carries it as a Bearer header.
//
// On a server bound off loopback, with the token in the address the page loads
// its people; without it, every call 401s.
//
// There is deliberately no email sign-in overlay: no route serves one, and on the
// one shape that can produce a 401 an email form could not work, would not
// mention the token, and would leave a page that looks empty rather than locked.

const TOKEN_KEY = "nc_session_token";

export function accessToken() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

export function setAccessToken(token) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

/** Is this page being served from somewhere other than this machine's loopback?
 *
 * Named for what it tests: the host, not the protocol, so plain http on a LAN
 * address counts too. What the app then CLAIMS must match that — a LAN install
 * is not "in the cloud".
 */
export function needsAccessToken() {
  const h = location.hostname;
  return !(h === "localhost" || h === "127.0.0.1" || h === "[::1]" || h === "::1");
}

// How long any single API call may hang before we give up on it.
//
// NC-033: a bare fetch() with no timeout can hang forever. GET /api/import/detect
// globs Desktop and Downloads and stats WhatsApp's group container, all
// TCC-protected, so it can block on a consent dialog nobody is looking at. A
// request that never settles leaves the Data page rendered without it, printing
// "No source needs you now." A page that confidently states the opposite of what
// it knows is worse than an error.
//
// 20 s rather than something tight: an import RUN goes through this wrapper too,
// and a first LinkedIn archive parse legitimately takes a while. Callers that
// know better pass `timeoutMs` — 0 disables it, for the genuinely long ones.
// Change it here; it is deliberately not a 36th numeric field on Settings
// (NC-003), so it is named and commented instead of buried.
export const API_TIMEOUT_MS = 20000;

// NC-176. An import is the one call that legitimately outlives the 20s above.
// The server gives its subprocess NC_IMPORT_TIMEOUT_SEC (default 300), so a page
// that gave up after 20 would report every import between those two numbers as a
// failure while it carried on and succeeded. The receipts, the "N new people"
// count and the row refresh would go with it, and in the first-run wizard that
// reads as "your first import failed".
//
// The page has to OUTLAST the server rather than race it: when the server gives
// up it answers with what it managed to do, and that answer is the useful one.
// The margin is for that answer to travel; it is plumbing, not a setting. The
// ceiling itself is the setting, and it lives on the server, so it is asked for
// rather than duplicated — GET /api/import/sources carries it, and that call
// always happens before any import can be started from either surface.
const IMPORT_MARGIN_MS = 15000;
const IMPORT_CEILING_FALLBACK_SEC = 300;   // crm/api_import.py IMPORT_TIMEOUT_DEFAULT
const IMPORT_CEILING_MAX_SEC = 3600;       // what import_timeout_sec() clamps to
let importCeilingSec = IMPORT_CEILING_FALLBACK_SEC;

/** Learn the server's own ceiling. Ignores anything that is not a usable number,
 *  so an older or partial payload leaves the fallback in place. */
export function noteImportCeiling(sec) {
  // typeof, not Number(): Number([1]) is 1, so a malformed payload could have
  // talked the page down to a one-second ceiling — which is the original bug
  // again, only shorter. Anything that is not already a number is ignored.
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec <= 0) return;
  importCeilingSec = Math.min(IMPORT_CEILING_MAX_SEC, Math.round(sec));
}

export function importTimeoutMs() {
  return importCeilingSec * 1000 + IMPORT_MARGIN_MS;
}

// ── What a failed request says ────────────────────────────────────────────────
// NC-363. Every failure leaves api() as an Error whose .message is the sentence a
// person reads: ~55 call sites hand e.message straight to toast() or setErr(), so
// the one place that makes the Error is the one place to keep developer text out
// of them. Before this a stopped engine said "Load failed", a timeout named its
// route ("/api/chats did not answer within 30s"), and a crash said "500 Internal
// Server Error" or a Python exception. The detail still exists: console.error
// gets the route, the status and what the engine said, and `.kind`, `.status` and
// `.detail` ride along on the Error for code that needs to tell the cases apart.
//
// Three sorts of answer, three sorts of sentence:
//   - the engine REFUSED on purpose (400-499 with an error sentence): that
//     sentence is already written for people, so it passes through word for word;
//   - the engine says why a thing OUTSIDE it failed (502 and 504: a calendar
//     address that stopped working, a helper that timed out): shown as well,
//     unless it carries developer text, as the native app does;
//   - everything else (500 and up, an answer that is not the engine's JSON, a
//     refusal with no sentence, no answer at all): one plain sentence.
//
// "did not answer within" is matched by ask.js (TIMED_OUT_RE) and data.js to tell
// a request that gave up from one that failed. Reword the sentence and every Ask
// timeout is silently shown as a fault; the .kind below is there to move them to.
export const SAY_UNREACHABLE = "Network CRM is not answering. Quit and reopen the app, then try again.";
export const SAY_WENT_WRONG = "Something went wrong inside Network CRM. Try again, and if it keeps happening, quit and reopen the app.";

// Text that no person should read. Only used on a 502/504 sentence, which the
// engine builds from the other side's own words; a 400-499 sentence is the
// engine's own and is never second-guessed.
const DEVELOPER_TEXT = /\/api\/|\bHTTP\b|Internal Server Error|Traceback|Error code|Exception\b/i;

/** "20 seconds", "3 minutes": how long a request waited, in words. */
export function waitedFor(ms) {
  const s = Math.max(1, Math.round(ms / 1000));
  if (s < 120) return `${s} ${s === 1 ? "second" : "seconds"}`;
  return `${Math.round(s / 60)} minutes`;
}

/** The sentence for an answer that was not a success. `data` is the parsed body,
 *  `{}` when it was not JSON. */
export function sentenceForAnswer(status, data) {
  const said = [data?.detail, data?.error].find(v => typeof v === "string" && v.trim()) || "";
  if (said && status >= 400 && status < 500) return said;
  if (said && (status === 502 || status === 504) && !DEVELOPER_TEXT.test(said)) return said;
  return SAY_WENT_WRONG;
}

function failure(kind, sentence, detail, status = 0, log = true) {
  if (log) console.error(`Network CRM: ${detail}`);
  const err = new Error(sentence);
  err.kind = kind;       // "timeout" | "unreachable" | "refused" | "outside" | "server"
  err.status = status;   // 0 when nothing answered
  err.detail = detail;
  return err;
}

export async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (needsAccessToken()) {
    const token = accessToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  const { timeoutMs = API_TIMEOUT_MS, ...fetchOpts } = opts;
  // An AbortController per call, cleared in finally: an un-cleared timer keeps
  // the tab awake and can abort a LATER request that reused the controller.
  const ctl = timeoutMs > 0 ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
  let res;
  try {
    res = await fetch(path, { headers, ...(ctl ? { signal: ctl.signal } : {}), ...fetchOpts });
  } catch (err) {
    const call = `${fetchOpts.method || "GET"} ${path}`;
    if (err?.name === "AbortError") {
      throw failure("timeout", `Network CRM did not answer within ${waitedFor(timeoutMs)}. Try again.`,
        `${call} did not answer within ${timeoutMs} ms`);
    }
    // fetch() rejects with a bare TypeError ("Load failed", "Failed to fetch") when
    // nothing is listening: the engine has stopped, or is not yet back.
    throw failure("unreachable", SAY_UNREACHABLE, `${call} could not be reached (${err?.name}: ${err?.message})`);
  } finally {
    if (timer) clearTimeout(timer);
  }
  let data = {};
  let readable = true;
  try { data = await res.json(); } catch { readable = false; /* not JSON */ }
  if (res.status === 401 && needsAccessToken()) {
    _showLocked();
    throw new Error("This copy needs its access token — see the message on screen");
  }
  if (!res.ok) {
    const sentence = sentenceForAnswer(res.status, data);
    const replaced = sentence === SAY_WENT_WRONG;
    // The engine's own sentence says all there is to say; only a replaced answer
    // needs its detail kept.
    throw failure(replaced ? "server" : res.status < 500 ? "refused" : "outside", sentence,
      `${fetchOpts.method || "GET"} ${path} answered ${res.status} ${res.statusText || ""}`.trim()
        + `: ${JSON.stringify(data)?.slice(0, 500) ?? ""}`, res.status, replaced);
  }
  // Every route the page calls answers in JSON. Something else on this port (or a
  // proxy's error page) is not the engine, and {} would draw it as an empty screen.
  if (!readable) {
    throw failure("server", SAY_WENT_WRONG,
      `${fetchOpts.method || "GET"} ${path} answered ${res.status} with something that is not JSON`, res.status);
  }
  return data;
}

// ── Locked: this copy is on the network and wants its token ───────────────────
// Replaces the magic-link sign-in overlay (NC-099). Deliberately NOT a form: the
// token is not a password to be typed into a box and posted somewhere, it is one
// query parameter this app already accepts, and every route that could have
// received a posted credential is gone. So this says the true thing and shows
// the exact address to open.

let _lockedOverlay = null;

function _showLocked() {
  if (_lockedOverlay) return;
  const here = `${location.protocol}//${location.host}${location.pathname}`;
  _lockedOverlay = document.createElement("div");
  _lockedOverlay.id = "nc-locked-overlay";
  _lockedOverlay.innerHTML = `
    <div id="nc-locked-card">
      <div id="nc-locked-logo">n</div>
      <h1>This copy needs its access token</h1>
      <p>It is being served from <b></b> rather than from your own Mac, so it
         asks for the token before it shows anyone your contacts.</p>
      <p>Open it with the token in the address:</p>
      <code id="nc-locked-url"></code>
      <p class="nc-locked-where">The token is <b>APP_TOKEN</b> in <b>secrets/.env</b>, on the Mac that is running this.</p>
    </div>`;
  // textContent, never innerHTML, for anything derived from the address bar.
  _lockedOverlay.querySelector("#nc-locked-card p b").textContent = location.host;
  _lockedOverlay.querySelector("#nc-locked-url").textContent = `${here}?token=YOUR_TOKEN`;
  _lockedOverlay.style.cssText =
    "position:fixed;inset:0;background:var(--bg,#f3f3f4);display:flex;align-items:center;"
    + "justify-content:center;z-index:9999;padding:24px";
  document.body.appendChild(_lockedOverlay);
  const card = _lockedOverlay.querySelector("#nc-locked-card");
  card.style.cssText =
    "background:var(--panel,#fff);padding:32px;border-radius:16px;max-width:440px;"
    + "text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.12);color:var(--ink,#17181a)";
  _lockedOverlay.querySelector("#nc-locked-logo").style.cssText =
    "width:56px;height:56px;border-radius:50%;background:var(--accent,#3b6fe0);color:#fff;"
    + "font:600 32px/56px Georgia,serif;font-style:italic;display:inline-block;margin:0 auto 16px";
  _lockedOverlay.querySelector("#nc-locked-url").style.cssText =
    "display:block;padding:10px 14px;margin:8px 0 16px;border-radius:8px;"
    + "background:var(--panel-2,#fafafb);border:1px solid var(--line,#e8e9eb);"
    + "font-family:ui-monospace,SF Mono,Menlo,monospace;font-size:13px;word-break:break-all";
  _lockedOverlay.querySelector(".nc-locked-where").style.cssText =
    "font-size:13px;color:var(--muted,#6d6e73);margin:0";
}

// ── The token in the address ────────────────────────────────────────────────
// Opening http://<host>:<port>/?token=... stores the token for this tab and
// reloads without it, so it does not sit in the address bar or in history.
(function _handleTokenInUrl() {
  const params = new URLSearchParams(location.search);
  const token = params.get("token");
  if (!token) return;
  setAccessToken(token);
  history.replaceState(null, "", location.pathname);
  location.reload();
})();

export const CHANNEL_LABEL = {
  linkedin: "LinkedIn", imessage: "iMessage", whatsapp: "WhatsApp", email: "Email",
  telegram: "Telegram", messenger: "Messenger", instagram: "Instagram",
};
export const CHANNEL_ORDER = ["linkedin", "imessage", "whatsapp", "email", "telegram", "messenger", "instagram"];
/* CHANNEL_ICON moved to icons.js when the emoji became vectors — it names a
   glyph now, not a character, so it belongs with the drawing it selects. */

export const CATS = ["personal", "professional", "both", "unknown"];
export const CAT_LABEL = { personal: "Personal", professional: "Professional", both: "Both", unknown: "Not sure" };
export const CAT_COLOR = {
  personal: "var(--cat-personal)", professional: "var(--cat-professional)",
  both: "var(--cat-both)", unknown: "var(--cat-unknown)", "": "var(--cat-none)",
};

/** "person" or "people" for a count. NC-122: "1 people with history" read as a
 *  bug even when the number was right. */
export const noun = (n, one, many) => (n === 1 ? one : many);

export function initials(name) {
  return (name || "?").trim().split(/\s+/).slice(0, 2).map(w => w[0] || "").join("").toUpperCase() || "?";
}

// Stable hue per name → warm pastel avatar colors (see --avatar-* tokens).
export function hueFor(name) {
  let h = 0;
  for (const ch of String(name || "")) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

// Gradient pair from the same hash: the partner hue sits 40–70° around the
// wheel, so every name gets the same subtle two-tone every render.
export function huePairFor(name) {
  const h = hueFor(name);
  return [h, (h + 40 + (h % 31)) % 360];
}

export function roleStr(c) {
  return [c.position, c.company].filter(Boolean).join(" · ");
}

export function relDate(s) {
  if (!s) return "";
  const d = new Date(String(s).slice(0, 10) + "T12:00:00Z");
  if (isNaN(d)) return String(s).slice(0, 10);
  const days = Math.round((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.round(days / 30)}mo ago`;
  return `${(days / 365).toFixed(days < 730 ? 1 : 0)}y ago`;
}

export function fmtMonthYear(s) {
  if (!s) return "";
  const d = new Date(String(s).replace("UTC", "").trim().replace(" ", "T"));
  if (isNaN(d)) return String(s).slice(0, 10);
  return d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

export function fmtDay(s) {
  if (!s) return "";
  const d = new Date(String(s).slice(0, 10) + "T12:00:00Z");
  if (isNaN(d)) return String(s).slice(0, 10);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function strengthRing(c) {
  const tier = c.strength_label || "faint";
  return {
    "--ring-pct": Math.max(6, Math.min(100, c.strength || 0)),
    "--ring-col": `var(--ring-${tier})`,
  };
}

let toastTimer = null;
let liveRegion = null;

/** The persistent announcer. Two elements rather than one, deliberately.
 *
 *  A live region is only observed while it is IN the accessibility tree, and it
 *  has to be there BEFORE the text arrives — an element appended with its text
 *  already set is a new node, not a mutation, so there is nothing to announce.
 *  That rules out hiding it with display:none or visibility:hidden, since both
 *  remove it from that tree. Hence .sr-only (clip-and-shrink) on a node that is
 *  created once and never removed, while the visible toast stays ephemeral. */
function announcer() {
  if (!liveRegion || !liveRegion.isConnected) {
    liveRegion = document.createElement("div");
    liveRegion.className = "sr-only";
    liveRegion.setAttribute("role", "status");
    // polite, not assertive: these confirm something the user just did, so they
    // should wait for a pause instead of interrupting whatever is being read.
    liveRegion.setAttribute("aria-live", "polite");
    document.body.appendChild(liveRegion);
  }
  return liveRegion;
}

/** The app's only confirmation and error channel — every save, delete, exclude
 *  and purge in the app reports through here, which is why it has to be
 *  announced and not merely drawn. */
export function toast(msg) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = "toast";
  // The announcer already says this; without aria-hidden a screen reader user
  // hears every confirmation twice.
  el.setAttribute("aria-hidden", "true");
  el.textContent = msg;
  document.body.appendChild(el);

  // Clear first, then write. Saving the same field twice is two events, and a
  // live region ignores a write that does not change the text.
  const live = announcer();
  live.textContent = "";
  live.textContent = msg;

  // NC-363. A confirmation stays 2.6 s. The plain failure sentences run to a hundred
  // characters, which takes about six seconds to read, so past 40 characters a toast
  // stays 60 ms longer for each one, up to nine seconds.
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), Math.min(9000, 2600 + Math.max(0, String(msg).length - 40) * 60));
}

/** A one-shot instruction carried in the hash (?new=1, ?merge=1, ?src=…).
 *
 *  Returns "" when it isn't there, and otherwise a token that CHANGES every time
 *  the same instruction is sent again — go() in main.js supplies the varying
 *  part. Effects must key on this rather than on a boolean: a boolean goes
 *  true once and then never changes, so picking "Add someone new" a second time
 *  (from the palette or the native File menu) fired nothing at all, which was
 *  most obvious right after you'd closed the thing it had just opened. */
export function oneShot(params, key, expect = "1") {
  const v = params?.get(key);
  if (v === null || v === undefined || v === "" || (expect !== null && v !== expect)) return "";
  return `${v}:${params.get("_") || ""}`;
}

export function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
