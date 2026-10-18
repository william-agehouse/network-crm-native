# Design direction

One document, one direction for how the app looks and is laid out. The
data-integrity work (per-field manual provenance, soft delete, one-field-per-input)
**has shipped**, so this document is about form and structure, not safety.

Grounded in how good interfaces already solve the same problems, not in taste: a
dense record table with relationship columns, a daily review worked down to zero, a
polished shell, and a detailed person page.

---

## 1. The diagnosis

**The app is a dense data tool wearing an editorial-magazine skin.**

It can hold thousands of people, a long message history, seven channels and a
0–100 strength score. It is styled for calm reading of a few paragraphs: cream
canvas, Fraunces display serif, terracotta warmth, generous whitespace, floating
rounded cards.

The mismatch is measurable, not aesthetic opinion:

- The People list fits **~6 rows** in a 720px viewport. A denser table fits **10** in
  the same space at 40px rows, and reads as calmer because the structure carries the eye
  instead of the whitespace.
- The header says "First 500 of N" and a button below says "+ Add these 12
  matches to a list" — two different answers to "how many?" on one screen, because
  nothing owns the count.
- Ask replies open with "Here's who I found:" — conversational filler above a list
  that speaks for itself.

Second problem: **it looks generic.**
Warm cream, serif display face, terracotta accent — a widely used editorial idiom
for AI products. That is a fit problem as much as an identity one: the idiom is
built for prose.

## 2. The direction

### Shell — a tinted shell with an inset panel
Tinted shell (`#f3f3f3`-ish) with the active view floating as an **inset rounded
panel**. One wrapper div, a radius, a background. Buys most of the perceived quality
of a redesign, and immediately reads as a tool rather than a document.

### Palette — cool neutral, accent means something
Retire cream and terracotta-as-warmth. Move to a cool neutral register: white surfaces,
`#E8E9EB` hairlines, `#F1F2F4` hover, a text ramp of `#17181A` / `#75777C` / `#9B9DA1`,
and **one** action colour.

> Superseded by §6. There is no action *colour*: actions are ink (`--action`),
> and the blue (`--accent`) means interaction only. The rest of this section
> holds.

Colour then becomes information, never decoration:
- **staleness** on the last-contact column (fresh → cooling → cold)
- **channel** identity (iMessage / WhatsApp / email / LinkedIn)
- **strength** as a dot + humanised label

> Also superseded by §6: topics are **not** a pastel family. Tinting them meant
> `.tag.topic` shared a rule with `.tag.warn`, so every topic pill was painted
> with the warning colour. Topics take the neutral `.tag` base. The soft
> semantic fills are reserved for genuine states — warn / bad / ok / info.

### Type and density — weight, not size
Drop the display serif from the UI (it can survive in one or two deliberate places).
One humanist sans. 12.5–13px for table and UI text, 15px titles, 11px letterspaced
overlines, **40px rows**. The rule: hierarchy comes from *weight*, not size.
This roughly doubles what a screen holds without feeling denser.

### Today — one list, processed to zero

*The Today view was deleted in NC-009, and People is the landing page now. This
section is kept as the record of what was decided for it.*

A list worked down to zero, which is exactly the shape of the daily review. Replace the
current queue-plus-attention-list mixture with a single ranked list, grouped by date
buckets in whitespace (no rules), and the **hover-swap affordance**: the timestamp is
replaced in place by the actions (done / snooze / open) on hover. No extra chrome, no
row growth.

Empty state is the reward, not a placeholder — full-bleed imagery plus "All caught up
— streak: 4 days." `api_views.py` is the disposable churn zone built for exactly this
rewrite.

### People — a record table with the intelligence columns
The data already exists and the UI hides it. Adopt two headline columns:
**Last contact** (relative, coloured by staleness) and **Connection strength** (dot +
label). And adopt the detail that matters most here:

> Empty cells show explicit gray text — "No contact yet", "No messages" — never blank.

With thousands of contacts and most of them having no history, **neglect visibility
is the product.**
Blank cells hide the thing the app exists to tell you.

Replace the chip wall with **Sort / Filter as ghost pills carrying active-count
badges**, plus a saved-views dropdown ("Everyone", "Founders", "Going cold"). That
also kills the "First 500 of N" incoherence: one owner for the count, stated once.

## 3. Microcopy — ASD-STE100 discipline

The app's own words follow Simplified Technical English, the aerospace maintenance-manual
standard: **one meaning per word, active voice, one instruction per sentence, short
sentences, no dropped words.** Applies to reason lines, empty states, errors, buttons,
Ask's framing text.

- "Here's who I found:" → nothing. The list is the answer.
- "Choose a person on the left — or press ⌘K and type a name." → "Select a person."
- "AI chat was unavailable: No module named 'anthropic'" → "Ask needs an API key.
  Open Settings."

## 4. Explicitly leaving behind

- Cream canvas, Fraunces in the UI chrome, terracotta as warmth
- Floating cards on a tinted page — structure comes from alignment and hairlines
- Size-based hierarchy
- Conversational filler in system text
- Two components each computing their own count

## 5. Order of work

The refactor makes this cheap in a specific way: `api_views.py` is
composition-only and disposable, and the frontend is one module per surface. So:

1. **Tokens** — `static/css/tokens.css`: palette, type scale, radius, row height.
   Nothing structural. Immediately visible, trivially revertable.
2. **Shell** — the tinted-shell + inset panel wrapper.
3. **People** — record table, intelligence columns, explicit empty text, ghost-pill
   filters, saved views. The densest win.
4. **Today** — one list to zero, date buckets, hover-swap actions, reward state.
   Touches `api_views.py` + `today.js` only. (Done, and later deleted with the whole
   view in NC-009.)
5. **Person detail** — a fuller layout for the person page.
6. **Microcopy pass** — STE across every surface.

Steps 1–2 are hours and reversible. 3–4 are the real work.

## 6. Decisions taken

How these apply in the built `.app`:

- **Actions are ink, not colour.** Primary buttons and count badges take
  `--action` (near-black, inverted on dark); links are ink plus an underline.
  `--accent` keeps its name and its blue, and now means exactly one thing: the
  system responding to you — focus rings, the selected row, info notices. The
  reasoning is budgetary. This screen already spends colour on meaning (the
  staleness ramp, seven channel hues, the closeness tiers), and a saturated
  Submit button competes with all of it. Ink wins hierarchy on weight instead.
- **Keep the staleness traffic light.** Green for days → gold for weeks → orange
  for months → grey for over a year, on the text, not just a dot. The
  alternatives considered were colouring only the neglected end (so a fresh
  contact renders plain, since it asks nothing of you) and a single hue fading
  out. Rejected: the absolute scale is easier to read a tier from, and with
  actions now ink there is no longer any colour for it to compete with.
- **Topic pills stay near-invisible.** The neutral `.tag` base — `--panel-2`
  with a `--line` hairline. They are metadata, so they recede and let names and
  staleness lead. A cool tint was considered and not adopted.

### Further decisions

- **No category dot in the table.** It was drawn on every row, so for the great
  majority of contacts, which have no category, it resolved to `--cat-none` (#d7d9dd) at 1.41:1 —
  below the 3:1 WCAG asks of a meaningful graphic. It therefore read as "one
  person has a dot" rather than as a scale. It also collided: `--cat-personal`
  and `--ring-close` are both #2f8a5b, so the dot and the avatar ring beside it
  were the same green for two unrelated facts. Removed rather than repointed,
  because every well-populated field is already its own column — a dot there
  needs a fact the row does not already show (an owed reply, an upcoming date),
  and both need a backend join. Category keeps its labelled pill on the person
  page, where a word carries the meaning. The avatar ring had the same defect:
  `--ring-faint` was the same #d7d9dd at 1.41:1, and it is the colour every ring
  falls back to (ui.css, `--ring-col`). Fixed 2026-10-17 (NC-373): #85878c light,
  #73757d dark, at least 3 to 1 on every surface a ring is drawn on, and still the
  quietest of the four tiers. *(Until then this said the ring was not yet fixed.)*
- **A detail pane must have a visible way out.** Opening a person narrows the
  table to a 340px rail and hides four columns, and there was no route back:
  the open row re-navigated to the hash it was already on (no `hashchange`, so
  no effect), and the People tab restored `TAB_HASH["people"]` — that same
  person. Two exits now: pressing the open row closes it, and pressing the
  already-active tab drops the id. Applies to People and Lists, which have the
  same shape; **not** to Ask, where bare `#ask` is the newest chat rather than a
  list, so collapsing would throw you into a different conversation.

## 7. Still open

- **Sidebar or top tabs?** Many record tools use a left sidebar; the app currently
  uses top tabs, and a daily-review screen is calmer with no sidebar at all. A
  sidebar buys saved views, favourites and a visible ⌘K; tabs keep the ritual calm.
  Recommendation, from before the Today view was deleted in NC-009: keep top tabs,
  and add a sidebar rail only inside People.
  This is the one with the most downstream work attached.
- **Dark mode**: currently a full second theme, verified at parity including the
  action inversion. Keep it at parity, or make light the designed one and dark
  merely correct?
- **How much serif survives?** Zero in chrome is cleanest; one or two editorial
  moments (the person page name; the Today greeting went with Today in NC-009)
  could justify it. Still
  live in onboarding: `.onb-title`, `.onb-h2`, `.onb-mark`.
- **⌘K promotion**: a visible keyboard-shortcut chip would advertise it. Worth the header space?
