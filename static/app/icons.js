/* ── Icons — one vector set, no emoji ─────────────────────────────────────────
 * The app used to draw its glyphs with characters: emoji (💼 💬 🟢 ✉️) for
 * sources and channels, dingbats (✓ ✕ ✎ ✆ ★) for marks. Both are font
 * characters, which means the machine decides how they look — colour-emoji
 * presentation appears uninvited (✉ and ✈ flip to colour on several systems),
 * stroke weight never matches the surrounding text, and the same build renders
 * differently on two Macs. They are neither text nor icon.
 *
 * This is the SF Symbols answer instead: one monochrome vector family, drawn on
 * a 16px grid at 1.5px stroke, inheriting `currentColor` and the surrounding
 * font's colour and opacity. No icon library is loaded and there is no new
 * dependency — the same rule the rest of the app already follows (the
 * ICON_PATHS block that used to live in people.js, now folded in here).
 * Seven shapes are Feather Icons' drawings rescaled to this grid (gear, phone,
 * link, sun, external, filter, save). Feather is MIT, so its notice is in
 * THIRD_PARTY_NOTICES.md (NC-280); the rest were drawn here.
 *
 * Drawing rules, so a new glyph matches the ninety already here:
 *   · 16×16 viewBox, and keep 1.5px of padding — shapes live inside 2…14.
 *   · Strokes only. No fills, no even-odd tricks, no <circle r> where a path
 *     works: `fill: none; stroke: currentColor` is set once, in CSS.
 *   · Round caps and joins. Straight runs land on .5 offsets so a 1.5px stroke
 *     sits on the pixel grid rather than straddling it.
 *   · One silhouette per meaning. Two icons that read alike at 16px are one
 *     icon, however different they look at 64px.
 */
import { html } from "../vendor/preact-htm.module.js";

/* Each entry is an array of paths, drawn in order. A path is normally a `d`
 * string and is stroked; `{ d, fill: true }` fills it with currentColor and
 * draws no stroke, which a few solid shapes need (a caret, the filled half of
 * the system-appearance circle). */
export const ICONS = {
  /* ── Marks ─────────────────────────────────────────────────────────────── */
  check: ["M3.3 8.4 6.4 11.5 12.7 5"],
  close: ["M4.2 4.2 11.8 11.8", "M11.8 4.2 4.2 11.8"],
  plus: ["M8 3.4V12.6", "M3.4 8H12.6"],
  minus: ["M3.4 8H12.6"],
  pencil: ["M10.8 2.9a1.6 1.6 0 0 1 2.3 2.3L5.5 12.8l-3 .7.7-3z", "M9.6 4.1l2.3 2.3"],
  trash: ["M2.9 4.4h10.2", "M6.4 4.4V3.1h3.2v1.3", "M4.2 4.4l.6 8.5h6.4l.6-8.5", "M6.7 6.8v3.8", "M9.3 6.8v3.8"],
  ban: ["M8 13.6A5.6 5.6 0 1 0 8 2.4a5.6 5.6 0 0 0 0 11.2Z", "M4 4 12 12"],
  info: ["M8 13.6A5.6 5.6 0 1 0 8 2.4a5.6 5.6 0 0 0 0 11.2Z", "M8 7.4v3.4", "M8 5.2h.01"],
  ellipsis: ["M4 8h.01", "M8 8h.01", "M12 8h.01"],
  star: ["M8 2.6l1.7 3.5 3.8.5-2.8 2.7.7 3.8L8 11.3l-3.4 1.8.7-3.8L2.5 6.6l3.8-.5z"],
  dot: ["M8 10.4a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8Z"],
  diamond: ["M8 2.6 13.4 8 8 13.4 2.6 8z"],

  /* ── Direction ─────────────────────────────────────────────────────────── */
  chevronLeft: ["M10 3.4 5.4 8l4.6 4.6"],
  chevronRight: ["M6 3.4 10.6 8 6 12.6"],
  chevronUp: ["M3.4 10 8 5.4l4.6 4.6"],
  chevronDown: ["M3.4 6 8 10.6 12.6 6"],
  caretRight: [{ d: "M6.2 4.2 10.6 8l-4.4 3.8z", fill: true }],
  arrowUp: ["M8 12.6V3.4", "M4.2 7.2 8 3.4l3.8 3.8"],
  arrowDown: ["M8 3.4v9.2", "M4.2 8.8 8 12.6l3.8-3.8"],
  enter: ["M12.6 3.4v4.2a1.4 1.4 0 0 1-1.4 1.4H3.4", "M6.4 5.8 3.4 9l3 3.2"],
  external: ["M12.6 8.9v3.1a1.4 1.4 0 0 1-1.4 1.4H4a1.4 1.4 0 0 1-1.4-1.4V4.7A1.4 1.4 0 0 1 4 3.3h3.1", "M9.8 2.6h3.6v3.6", "M7.4 8.6l6-6"],
  sort: ["M4.5 3.3v9.4", "M2.5 10.6l2 2.1 2-2.1", "M11.5 12.7V3.3", "M9.5 5.4l2-2.1 2 2.1"],
  sync: ["M2.8 6.2h8.1l-2-2", "M13.2 9.8H5.1l2 2"],
  refresh: ["M13 8a5 5 0 1 1-1.6-3.7", "M13.3 2.6v3.1h-3.1"],
  download: ["M8 2.8v6.6", "M5.2 6.8 8 9.6l2.8-2.8", "M2.9 11.4v1.3a.9.9 0 0 0 .9.9h8.4a.9.9 0 0 0 .9-.9v-1.3"],

  /* ── Objects ───────────────────────────────────────────────────────────── */
  person: ["M8 8.1a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2Z", "M2.9 13.4c.6-2.3 2.6-3.6 5.1-3.6s4.5 1.3 5.1 3.6"],
  people: ["M6.2 7.6a2.3 2.3 0 1 0 0-4.6 2.3 2.3 0 0 0 0 4.6Z", "M1.9 13.2c.5-2.1 2.2-3.3 4.3-3.3s3.8 1.2 4.3 3.3", "M10.6 3.3a2.3 2.3 0 0 1 0 4.4", "M11.6 9.9c1.3.4 2.2 1.4 2.5 3"],
  work: ["M2.5 5.7h11v7.3h-11z", "M6.2 5.7V4.3c0-.6.5-1.1 1.1-1.1h1.4c.6 0 1.1.5 1.1 1.1v1.4", "M2.5 9h11"],
  pin: ["M8 13.9s4.3-4 4.3-7A4.3 4.3 0 0 0 3.7 6.9c0 3 4.3 7 4.3 7Z", "M8 8.3a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z"],
  tag: ["M8.6 2.6H13V7l-6 6-4.4-4.4 6-6Z", "M10.5 5.3h.01"],
  clock: ["M8 13.9A5.9 5.9 0 1 0 8 2.1a5.9 5.9 0 0 0 0 11.8Z", "M8 4.9V8l2.2 1.6"],
  bolt: ["M8.9 2.4 4.3 9.2h3l-.5 4.4 4.7-6.9H8.5z"],
  chat: ["M13.4 8.7c0 2.4-2.4 4.3-5.4 4.3-.7 0-1.4-.1-2-.3l-3 1.1.9-2.5A4.1 4.1 0 0 1 2.6 8.7c0-2.4 2.4-4.3 5.4-4.3s5.4 1.9 5.4 4.3Z"],
  filter: ["M2.6 3.6h10.8L9.3 8.3v4.1L6.7 11V8.3z"],
  search: ["M7.4 12.1a4.7 4.7 0 1 0 0-9.4 4.7 4.7 0 0 0 0 9.4Z", "M13.4 13.4 10.7 10.7"],
  gear: ["M8 10.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4Z", "M12.6 9.8a1.1 1.1 0 0 0 .2 1.2l.1.1a1.3 1.3 0 1 1-1.9 1.9l-.1-.1a1.1 1.1 0 0 0-1.2-.2 1.1 1.1 0 0 0-.7 1v.2a1.3 1.3 0 1 1-2.6 0v-.1a1.1 1.1 0 0 0-.7-1 1.1 1.1 0 0 0-1.2.2l-.1.1a1.3 1.3 0 1 1-1.9-1.9l.1-.1a1.1 1.1 0 0 0 .2-1.2 1.1 1.1 0 0 0-1-.7h-.2a1.3 1.3 0 1 1 0-2.6h.1a1.1 1.1 0 0 0 1-.7 1.1 1.1 0 0 0-.2-1.2l-.1-.1a1.3 1.3 0 1 1 1.9-1.9l.1.1a1.1 1.1 0 0 0 1.2.2h.1a1.1 1.1 0 0 0 .7-1v-.2a1.3 1.3 0 1 1 2.6 0v.1a1.1 1.1 0 0 0 .7 1 1.1 1.1 0 0 0 1.2-.2l.1-.1a1.3 1.3 0 1 1 1.9 1.9l-.1.1a1.1 1.1 0 0 0-.2 1.2v.1a1.1 1.1 0 0 0 1 .7h.2a1.3 1.3 0 1 1 0 2.6h-.1a1.1 1.1 0 0 0-1 .7Z"],
  folder: ["M13.6 12.1a1.1 1.1 0 0 1-1.1 1.1h-9a1.1 1.1 0 0 1-1.1-1.1V3.9a1.1 1.1 0 0 1 1.1-1.1h3l1.2 1.7h4.8a1.1 1.1 0 0 1 1.1 1.1z"],
  save: ["M12.9 13.2H3.1a1 1 0 0 1-1-1V3.8a1 1 0 0 1 1-1h7.4l3.4 3.4v5.9a1 1 0 0 1-1 1.1Z", "M11 13.2V8.8H5v4.4", "M5 2.8v3h4.4"],
  list: ["M5.6 4.4h8", "M5.6 8h8", "M5.6 11.6h8", "M2.9 4.4h.01", "M2.9 8h.01", "M2.9 11.6h.01"],
  /* Two sheets, the back one offset — "merge these two records". */
  layers: ["M6.1 2.6h5.3a1 1 0 0 1 1 1v5.3", "M9.9 13.4H4.6a1 1 0 0 1-1-1V6.1a1 1 0 0 1 1-1h5.3a1 1 0 0 1 1 1v6.3a1 1 0 0 1-1 1Z"],
  link: ["M6.8 9.2a2.6 2.6 0 0 0 3.9.3l1.6-1.6a2.6 2.6 0 0 0-3.7-3.7l-.9.9", "M9.2 6.8a2.6 2.6 0 0 0-3.9-.3L3.7 8.1a2.6 2.6 0 0 0 3.7 3.7l.9-.9"],
  globe: ["M8 13.9A5.9 5.9 0 1 0 8 2.1a5.9 5.9 0 0 0 0 11.8Z", "M2.4 8h11.2", "M8 2.1a9 9 0 0 1 0 11.8 9 9 0 0 1 0-11.8Z"],
  calendar: ["M12.4 3.6H3.6a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h8.8a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1Z", "M2.6 6.7h10.8", "M5.6 2.3v2.4", "M10.4 2.3v2.4"],
  cake: ["M2.6 13.4h10.8", "M3.9 13.4V9.3a1 1 0 0 1 1-1h6.2a1 1 0 0 1 1 1v4.1", "M3.9 10.9c1.4 0 1.4 1 2.7 1s1.4-1 2.7-1 1.4 1 2.8 1", "M8 8.3V6.4", "M8 4.9a.9.9 0 0 1-.9.8.9.9 0 0 0 1.8 0 .9.9 0 0 1-.9-.8Z"],
  heart: ["M8 13.2 3.4 8.7a2.8 2.8 0 0 1 4-4l.6.6.6-.6a2.8 2.8 0 0 1 4 4z"],
  sparkle: ["M8 2.4 9.3 6l3.6 1.3L9.3 8.6 8 12.2 6.7 8.6 3.1 7.3 6.7 6z"],
  chart: ["M2.9 13.2h10.2", "M4.9 13.2V7.4", "M8 13.2V3.8", "M11.1 13.2V9.6"],
  image: ["M12.4 2.9H3.6a1 1 0 0 0-1 1v8.2a1 1 0 0 0 1 1h8.8a1 1 0 0 0 1-1V3.9a1 1 0 0 0-1-1Z", "M5.9 7a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z", "M13.4 10.2 10.6 7.6l-5.9 5.5"],
  cloud: ["M11.8 12.4a2.9 2.9 0 0 0 .3-5.7 4 4 0 0 0-7.7-1.1 2.9 2.9 0 0 0 .4 6.8Z"],
  /* A nib: shoulders, a slit, and a tip pointing at the baseline. */
  penNib: ["M9.7 3.1 3.1 9.7l3.2 3.2 6.6-6.6z", "M12.9 6.3 9.7 3.1l1.1-1.1 3.2 3.2z", "M6.3 12.9 2.1 14l1.1-4.3", "M8.4 4.4l3.2 3.2"],
  plug: ["M6 2.6v3.6", "M10 2.6v3.6", "M4.4 6.2h7.2v2a3.6 3.6 0 0 1-3.6 3.6A3.6 3.6 0 0 1 4.4 8.2Z", "M8 11.8v1.6"],
  lock: ["M11.9 7.3H4.1a1 1 0 0 0-1 1v4.1a1 1 0 0 0 1 1h7.8a1 1 0 0 0 1-1V8.3a1 1 0 0 0-1-1Z", "M5.4 7.3V5.4a2.6 2.6 0 0 1 5.2 0v1.9"],
  unlock: ["M11.9 7.3H4.1a1 1 0 0 0-1 1v4.1a1 1 0 0 0 1 1h7.8a1 1 0 0 0 1-1V8.3a1 1 0 0 0-1-1Z", "M5.4 7.3V5.4a2.6 2.6 0 0 1 5-1"],
  brain: ["M8 4.1v9", "M8 4.1a1.9 1.9 0 0 0-3.3 1.2 2 2 0 0 0-.8 3.4 1.9 1.9 0 0 0 1.4 3.2A1.8 1.8 0 0 0 8 13.1", "M8 4.1a1.9 1.9 0 0 1 3.3 1.2 2 2 0 0 1 .8 3.4 1.9 1.9 0 0 1-1.4 3.2A1.8 1.8 0 0 1 8 13.1", "M5.6 7.3H8", "M8 9.7h2.4"],
  duplicates: ["M6.3 11.4a3.7 3.7 0 1 1 0-7.4 3.7 3.7 0 0 1 0 7.4Z", "M9.7 11.4a3.7 3.7 0 1 1 0-7.4 3.7 3.7 0 0 1 0 7.4Z"],

  /* ── Weather / appearance ──────────────────────────────────────────────── */
  sun: ["M8 10.9a2.9 2.9 0 1 0 0-5.8 2.9 2.9 0 0 0 0 5.8Z", "M8 1.9v1.3", "M8 12.8v1.3", "M2.9 2.9l.9.9", "M12.2 12.2l.9.9", "M1.9 8h1.3", "M12.8 8h1.3", "M2.9 13.1l.9-.9", "M12.2 3.8l.9-.9"],
  moon: ["M13.1 9.4A5.6 5.6 0 0 1 6.6 2.9a5.6 5.6 0 1 0 6.5 6.5Z"],
  /* "Follow the Mac" — one dial, half light and half dark. The filled half is
     what carries the meaning, so it is a fill rather than an outline. */
  auto: ["M8 13.9A5.9 5.9 0 1 0 8 2.1a5.9 5.9 0 0 0 0 11.8Z", { d: "M8 2.85a5.15 5.15 0 0 1 0 10.3z", fill: true }],

  /* ── Channels ──────────────────────────────────────────────────────────── */
  /* One silhouette each — these sit beside a colour token (--ch-*), so the
     shape only has to separate them from each other, not carry the brand. */
  imessage: ["M8 12.6c3 0 5.4-2.1 5.4-4.8S11 3 8 3 2.6 5.1 2.6 7.8a4.4 4.4 0 0 0 1.6 3.3l-.8 2.3 2.7-1a6.5 6.5 0 0 0 1.9.2Z"],
  whatsapp: ["M8 13.4a5.4 5.4 0 0 0 4.6-8.2A5.4 5.4 0 0 0 3.4 10l-.8 2.6 2.7-.8a5.4 5.4 0 0 0 2.7.7Z", "M6.4 6.5c.5 1.6 1.5 2.6 3.1 3.1"],
  email: ["M12.9 3.6H3.1a1 1 0 0 0-1 1v6.8a1 1 0 0 0 1 1h9.8a1 1 0 0 0 1-1V4.6a1 1 0 0 0-1-1Z", "M2.4 4.9 8 8.6l5.6-3.7"],
  inbox: ["M12.9 3.6H3.1a1 1 0 0 0-1 1v6.8a1 1 0 0 0 1 1h9.8a1 1 0 0 0 1-1V4.6a1 1 0 0 0-1-1Z", "M2.1 9.1h3l.8 1.4h4.2l.8-1.4h3"],
  linkedin: ["M12.4 2.6H3.6a1 1 0 0 0-1 1v8.8a1 1 0 0 0 1 1h8.8a1 1 0 0 0 1-1V3.6a1 1 0 0 0-1-1Z", "M5.3 7.2v4", "M5.3 5.1h.01", "M8 11.2v-4", "M8 8.9a1.7 1.7 0 0 1 3.4 0v2.3"],
  telegram: ["M13.6 2.9 2.5 7.2l3.1 1.1 1.2 3.4 1.8-2.1", "M5.6 8.3 13.6 2.9l-2.1 9.4-3.5-2.7"],
  messenger: ["M8 12.9c3 0 5.4-2.2 5.4-5S11 2.9 8 2.9 2.6 5.1 2.6 7.9a4.7 4.7 0 0 0 1.9 3.7v2.1l1.8-1a6.3 6.3 0 0 0 1.7.2Z", "M5.2 9.1 7.4 6.8l1.5 1.5 1.9-1.5"],
  instagram: ["M11.4 2.9H4.6a1.7 1.7 0 0 0-1.7 1.7v6.8a1.7 1.7 0 0 0 1.7 1.7h6.8a1.7 1.7 0 0 0 1.7-1.7V4.6a1.7 1.7 0 0 0-1.7-1.7Z", "M8 10.4a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8Z", "M11.2 4.8h.01"],
  phone: ["M13.3 11.3v1.5a1 1 0 0 1-1.1 1 10.3 10.3 0 0 1-4.5-1.6 10.1 10.1 0 0 1-3.1-3.1 10.3 10.3 0 0 1-1.6-4.6 1 1 0 0 1 1-1.1h1.5a1 1 0 0 1 1 .9c.1.6.2 1.2.4 1.8a1 1 0 0 1-.2 1.1l-.6.6a8.3 8.3 0 0 0 3.1 3.1l.6-.6a1 1 0 0 1 1.1-.2c.6.2 1.2.3 1.8.4a1 1 0 0 1 .9 1Z"],
};

/* Every glyph the app draws resolves through here. A name that is not in ICONS
 * renders nothing rather than a fallback dot: a missing icon should be a hole
 * you notice in review, not a "•" that quietly ships. */
export function Icon({ name, className = "ic", title = null }) {
  const paths = ICONS[name];
  if (!paths) return null;
  return html`
    <svg class=${className} viewBox="0 0 16 16" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
      role=${title ? "img" : null} aria-hidden=${title ? null : "true"} focusable="false">
      ${title && html`<title>${title}</title>`}
      ${paths.map((p, i) => (typeof p === "string"
        ? html`<path key=${i} d=${p} />`
        : html`<path key=${i} d=${p.d} fill="currentColor" stroke="none" />`))}
    </svg>`;
}

/* ── Semantic maps ────────────────────────────────────────────────────────────
 * The name-per-meaning tables that used to hold emoji. Kept here rather than in
 * each view so two surfaces can't drift apart on what iMessage looks like. */

export const CHANNEL_ICON = {
  linkedin: "linkedin", imessage: "imessage", whatsapp: "whatsapp", email: "email",
  telegram: "telegram", messenger: "messenger", instagram: "instagram",
};

/* Keyed by importers.REGISTRY key, so an entry with no row is unreachable.
 * telegram / messenger / instagram have no import row (NC-015) — but they are
 * still in CHANNEL_ICON above, and that is not an inconsistency: the channels
 * outlive the rows, because anything a CLI run already archived keeps showing up
 * in a person's timeline. */
export const SRC_ICON = {
  contacts: "person", linkedin: "linkedin", imessage: "imessage", whatsapp: "whatsapp",
  whatsapp_photos: "image", google_photos: "cloud", email: "email",
  gmail: "inbox", facts: "pin",
  facts_llm: "sparkle", engagement: "chart", linkedin_facts: "globe",
  calendar: "calendar", calendar_local: "calendar",
  calendar_google: "calendar", calendar_icloud: "calendar",
  style: "penNib",
};

/* Contact-date kinds. The server sends the kind; the shape is chosen here, so
 * the API stays free of presentation (it used to ship the emoji itself). */
export const DATE_ICON = {
  birthday: "cake", anniversary: "heart", meeting: "people", custom: "star",
  event: "calendar",
};

export const CATEGORY_ICON = {
  personal: "heart", professional: "work", both: "people", unknown: "dot",
};
