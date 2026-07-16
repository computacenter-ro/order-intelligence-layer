---
name: cc-presentations
description: >
  Generates and reviews Computacenter-branded presentations (slide decks) — for internship
  week-1 sessions, courses, demos, and team talks. Trigger this skill whenever the task
  involves building, editing, or reviewing a presentation, deck, slides, talk, or course
  for Computacenter — even if the user does not explicitly say "brand" or "design". The
  output is a self-contained, interactive single HTML file (no build, no internet, open
  full-screen). This skill encodes the CC brand presentation layer: the application colour
  palette, Albert Sans typography, a fixed-canvas slide layout that scales to any screen,
  slide layout conventions, the interactive vote-then-reveal activity pattern, keyboard
  navigation, and accessibility. It complements `cc-frontend-guidelines` (which governs web
  apps, not slides). If anything here conflicts with what merely "looks good", the brand system wins.
---

# Computacenter Presentation Guidelines

Use these rules for every Computacenter slide deck. They adapt the CC design system
(`docs/frontend-guidelines.md`) for projection. **Start from the template, don't start from a blank file.**

## 0. Start here

- **Canonical starter:** `docs/presentation-template.html` in this package (after `npm install`:
  `node_modules/@computacenter-ro/style-guide/docs/presentation-template.html`). Duplicate it, then
  edit the `<section class="slide">` blocks. It already ships the correct tokens, embedded Albert Sans,
  the fixed-canvas scaler, keyboard nav, slide counter, progress bar, overview grid, and a working
  interactive activity.
- **Output format:** one self-contained `.html` file. Fonts embedded (base64), all CSS/JS inline,
  zero external requests. It must open by double-click and present full-screen with no server.
- **Golden rule:** never hard-code a hex value in a slide. Every colour is a `var(--cc-*)` token.

## 1. Colour palette (exact — application palette)

**Primary — must dominate every deck:**

| Token | Hex | Use on slides |
|---|---|---|
| Heritage Blue | `#0D21A0` | Headings on light slides, links, stat numbers, emphasis |
| Foundation Blue | `#011749` | Dark slide background, deep surfaces |
| Horizon Blue | `#49ABEB` | Kickers/eyebrows, accent on headings, progress bar — never body text |
| Cloud White | `#FAFAFF` | Light slide background and text on dark — **never pure `#FFFFFF`** |

**Accent / signal — small highlights, badges, single data points only. Never large fills:**

| Token | Hex | Meaning |
|---|---|---|
| United Red | `#F12938` | Error, failure, "don't", negative |
| Circuit Green | `#54C664` | Success, "do", positive |
| Fibre Orange | `#FF7900` | In progress, warning |
| Neural Purple | `#8459E2` | Pending / alternative |
| Voltage Yellow | `#EABE42` | Caution only |

**Greys:** Grey One `#2A2A2B` · Two `#494B4D` · Three `#8C8D8F` · Four `#C3C7CC` · Five `#D6DCE3` · Six `#EDF2F8`.

**Rules**
- Never introduce a colour outside this palette. No raw hex in slide markup — only `var(--cc-*)`.
- Never pure white. White surfaces and light text use Cloud White `#FAFAFF`.
- Heritage Blue is invisible on Foundation Blue (~1.3:1) — never put one on the other. On dark slides, headings/accents use Horizon Blue or Cloud White.
- Accent colours stay small (a badge, one stat, an icon). A whole slide is never an accent colour.
- Gradients are allowed on slides as decoration (title / section / takeaway), built only from palette blues (e.g. `135deg, #011749 → #0D21A0 → #49ABEB`). This is a presentation-only exception; gradients remain banned on app navigation.

## 2. Typography

**Albert Sans exclusively** for all slide text (embedded in the template, weights 300/400/500/600/700).
The **only** sanctioned second face is **JetBrains Mono → monospace, for code and commands only** — never for prose.
Never the Google Fonts CDN: fonts are embedded so the deck works offline.

**Presentation type scale** — fixed px on the **1280×720 canvas** (see §3). The whole stage scales to
fit the screen, so these sizes grow proportionally on a projector or TV. This is the *presentation
layer*, deliberately larger than the app UI scale, which is sized for dense screens and must not be
used on slides. Do **not** reintroduce `vw`/`clamp()` units — they cap out on large screens and leave
TVs under-filled, which is the exact problem the fixed canvas solves.

| Role | Size | Weight | Use |
|---|---|---|---|
| Display | 84px | 700 | Title & takeaway slides only |
| H1 | 58px | 700 | Section dividers, big statements |
| H2 | 40px | 700 | Standard slide heading |
| H3 | 26px | 600 | Card titles, sub-headings |
| Body | 21px | 400 | Bullet/supporting text |
| Kicker | 14px | 600 | Uppercase eyebrow, letter-spaced, Horizon Blue |
| Caption | 14px | 400/500 | Footnotes, presenter line |

**Rules**
- Sentence case for headings, body, captions. Title Case only for button labels / CTAs. ALL CAPS only for kickers and KPI stat labels.
- One idea per slide. Body text supports the speaker — it is not the script. Aim for ≤ ~6 lines of body per slide.
- Bold sparingly (emphasis only). No underline, text-shadow, or decorative effects.

## 3. Layout, spacing, motion

- **Fixed canvas:** slides are authored on a fixed **1280×720** 16:9 stage. A small `fit()` helper in
  the template scales the whole stage by the largest factor that fits the viewport and centres it, so
  the deck renders *identically — just larger* on a laptop, projector, or 4K TV. Letterbox bars (when
  the screen isn't exactly 16:9) show the Foundation-Blue body background. Do not reintroduce viewport
  (`vw`) units or media queries for slide sizing — they re-break large screens.
- **Fit your content to the frame.** Because the canvas height is fixed at 720px, content that's too
  tall overflows the slide instead of being silently shrunk. If a slide overflows, cut content or split
  it across two slides — never hand-tune font sizes down to cram more in.
- Slide padding is `60px 96px`. Spacing on a 4px grid. Radii: sm 8 · md 12 · lg 20 · xl 32 (presentation layer — a touch larger than app radii). Pills 999px.
- Shadows are **Foundation Blue tinted** (`rgba(1,23,73,…)`), never generic black.
- Transitions: `120ms` micro, `200ms` base, `400ms` slide changes. Respect `prefers-reduced-motion` when adding heavy animation.
- Slide background rotation: pair **light** content slides with **dark/gradient** title, section, and takeaway slides for rhythm.

### Visual variety & contrast (from UX review — the most common failure)

The single biggest complaint about generated decks is that **slides look empty and have no
point of interest** — typically a white slide carrying white cards whose border is barely
visible. It shows worst on a big screen. Fix it with contrast and colour, not more text:

- **Don't make everything one mode.** Deliberately alternate light and dark/gradient slides.
  Mode can also *group* content (e.g. one topic/app in light slides, the next in dark) — this
  separates sections and stops the deck feeling monotone. Avoid long runs of identical slides.
- **Never put a same-colour card on a same-colour slide.** A white `.card` on a Cloud-White
  slide reads as empty. Use a contrast card instead:
  - On **light** slides → `.card.solid` (deep-blue fill, white text) for the hero/callout, or
    `.card.accent` (soft blue fill) for a lighter step.
  - On **dark/gradient** slides → `.card.invert` (Cloud-White fill, dark text) to make a card pop;
    don't use only translucent dark-on-dark cards.
- **Give every content slide at least one point of interest:** a contrast card, a stat number in
  Heritage Blue, an accent badge, a kicker, or a subtle background decoration — not just text.
- **Play with colour combinations** from the palette; don't leave a slide monochrome. Accents
  still stay small (a card, a stat, a badge) — never a whole slide in an accent colour.
- **Fill empty space.** Add `class="decor"` to a slide for a subtle palette-blue background glow
  (mirrors the background elements used in the PPT decks). Use it sparingly and keep it behind
  text — it adds interest, it is not a focal element.

> A richer library of reusable slide components (varied layouts like the PPT set) is planned for
> later — for now, achieve variety with the slide modes, the card variants, and `decor` above.

## 4. Slide patterns (all in the template)

- **Title** (gradient): logo lockup, kicker, display heading, subtitle, presenter line.
- **Section divider** (dark): kicker (`Part N`) + H1 + one framing sentence.
- **Content** (light): kicker + H2, then a `.cols` split of tick-list + a `.card` callout.
- **Stats / three-up** (light): `.cols-3` of `.card.stat` (number in Heritage Blue + uppercase label) and status `.badge`s.
- **Code** (dark): `<pre>` in JetBrains Mono, accents via `.c` (keyword) and `.g` (comment) spans.
- **Takeaway** (gradient): one memorable sentence, display size.
- **Closing** (dark): logo + thanks + contact.

**Badges** (pills, sentence case, ~12% fill + ~25%-darker border/text): green=completed, blue=in progress, red=failed, purple=pending, orange=warning. Never ALL CAPS.

### Components for richer slides (icons, layouts, motif)

The template ships these so slides can have a visual without hand-rolling SVG. Use them; don't reinvent.

**Icons** — an inline SVG sprite is embedded (line style, regular weight — the brand Phosphor look, no
runtime dependency). Use one with:

```html
<span class="chip"><svg class="ico" aria-hidden="true"><use href="#i-target"/></svg></span>
```

`.chip` is a coloured tile (Heritage default; `.chip.horizon`, `.chip.soft` variants) that anchors a
bullet, card, or stat. Available ids: `i-check i-x i-arrow-right i-trend-up i-lightbulb i-warning
i-info i-question i-database i-cloud i-code i-chart-line i-chart-bar i-gear i-flask i-target i-bolt
i-shield i-lock i-eye i-users i-clock i-doc i-layers i-search i-star i-bell i-gauge`. Need another?
Add one `<symbol id="i-…" viewBox="0 0 24 24">` with stroke paths — never import an icon font/library,
never use filled icons.

**Content + visual (`.split`)** — the default content layout: words on one side, a visual on the other
(icon list, card, illustration, stat, or image). `.split.lead-text` widens the text column.

**Icon feature list (`ul.feat`)** — replaces plain bullet/tick lists; each row is a chip + text with a
bold lead-in. Far stronger than a tick list.

**Icon card grid** — `.cols-3` of `.card`s, each opened by a `.chip`; vary chip colour and make one a
`.card.solid` for a focal point.

**Concentric-rings motif (`.rings`)** — the brand decorative device. Drop
`<svg class="rings" style="top:-140px;right:-120px"><use href="#i-rings"/></svg>` into a title, section,
or closing slide for identity and to fill space. It sits behind content; position with inline offsets.

> **Pair most content slides with a visual** (icon list, card grid, stat, diagram, image, or `.split`).
> Text-only slides are allowed when a point genuinely needs no visual — a strong one-line statement, a
> section divider, a quote — just don't let *most* of the deck be text-only.

### Slide-type catalogue (the template doubles as a living gallery)

`docs/presentation-template.html` now contains one labelled example of every pattern below — read it
to see each rendered, then keep the slides you need and delete the rest. Pick a *variety* of types so
the deck isn't monotone:

| Type | Mode | Use for |
|---|---|---|
| Title | gradient | Opener — logo, kicker, display heading, presenter line; add `.rings` |
| Section divider | dark | One per major part — kicker `Part N` + H1 + one sentence |
| Content + visual (`.split`) | light | The workhorse — text one side, visual the other |
| Icon feature list (`ul.feat`) | any | Bulleted points with icon chips |
| Icon card grid (`.cols-3` + `.chip`) | light | "Three things" / capabilities / steps |
| Stats (`.card.stat`) | light | KPI numbers — vary solid/accent fills |
| Diagram — flow / compare / timeline / tokens | light | Process, do-vs-don't, sequence, highlight-one-value |
| Chart — bar / line / donut | light | A number that's clearer as a picture |
| Image — split or full-bleed | any | A photo/screenshot; full-bleed uses a scrim |
| Table (`.tbl`) | any | Small comparisons (~5 rows) |
| Code (`<pre>`) | dark | Commands/snippets, JetBrains Mono |
| Activity (`[data-activity]`) | light | Vote-then-reveal |
| Takeaway | gradient | One memorable sentence |
| Closing | dark | Thanks + contact; add `.rings` |

### Recipes (copy from the template, change the content)

```html
<!-- Diagram: process/flow -->
<div class="flow">
  <div class="node"><div class="num">1</div><div class="h">Ingest</div><div class="s">Sources</div></div>
  <span class="arrow"><svg class="ico"><use href="#i-arrow-right"/></svg></span>
  <div class="node"><div class="num">2</div><div class="h">Model</div><div class="s">Train</div></div>
</div>

<!-- Diagram: comparison -->
<div class="compare">
  <div class="col good"><div class="hd"><svg class="ico"><use href="#i-check"/></svg> Do</div>…</div>
  <div class="col bad"><div class="hd"><svg class="ico"><use href="#i-x"/></svg> Don't</div>…</div>
</div>

<!-- Chart: drawn from data by the template's renderCharts() — no library -->
<div class="chart" data-chart="bar"   data-values="42,55,63,71" data-labels="Q1,Q2,Q3,Q4"></div>
<div class="chart" data-chart="line"  data-values="12,18,16,24,30"></div>
<div class="chart" data-chart="donut" data-values="62,28,10" data-center="62%"></div>

<!-- Image beside text (embed base64; always give alt) -->
<div class="split">
  <ul class="feat">…</ul>
  <figure class="figure" style="height:420px"><img src="data:image/…" alt="…"></figure>
</div>

<!-- Table (small; tabular numerals; navigable cells in Heritage) -->
<table class="tbl"><thead><tr><th>Model</th><th class="num">F1</th></tr></thead>
  <tbody><tr><td class="nav">Baseline</td><td class="num">0.71</td></tr></tbody></table>

<!-- Step reveal: appears on the next arrow press -->
<li class="frag">…</li>
```

**Image rules:** embed as base64 (keeps the single self-contained file) and compress first — a few
hundred KB total, not megabytes; always include `alt` (or `alt=""` if purely decorative); use the full
canvas frame, never let an image push a slide past 720px. **Table rules:** comparisons only (~5 rows ×
~5 cols), never a raw data dump; right-align numbers (`class="num"`), link-style cells in Heritage
(`class="nav"`). **Chart rules:** charts read best on light slides; keep to one clear series.

### Tooling built into the template / repo

- **Overflow badge** — if a slide's content exceeds the 1280×720 frame, a red badge names the slide at
  runtime. If you see it, trim or split — don't shrink type.
- **Step reveals** — `.frag` elements appear one Right-arrow press at a time, then the deck advances.
- **Print / PDF** — browser Print → Save as PDF emits one slide per page (chrome hidden, frags shown).
- **Auto logo swap** — white mark on dark/gradient slides, blue on light (both embedded; never recolour).
- **Linter** — run `node scripts/check-deck.js <deck>.html` before sharing: it flags pure white,
  off-palette hex, fluid units, missing `alt`, external resources, and a too-high text-only ratio.

### Accessible text colours (UX review)

Grey Three (`#8C8D8F`) fails WCAG AA for small text on Cloud White, so secondary/caption text now uses
accessible tokens — they're already applied to `.muted`, chart labels, captions, stat labels and flow
subtext; use them for any small secondary text you add:

- `var(--cc-muted-light)` (`#6F7072`) — captions / secondary text **on light slides**.
- `var(--cc-muted-dark)` (`#A9ABB1`) — captions / secondary text **on dark or image slides**.
- `var(--cc-green-acc)` (`#1C7C2C`) — green **text and borders** (the pale-fill green `#3A9B4A` is too
  light); used by `.compare .col.good`, `.badge.green`, `.tok.ok`. Keep the ~12% green *fill* as-is.
- Text over a full-bleed image must be light (the template sets `.slide.image` body/lead to a light tint).

> The same low-contrast green/grey exists in the **web** design-system tokens (`colors.ts` badgeColors,
> `frontend-guidelines.md`). Worth propagating `#1C7C2C` and an accessible caption grey there too — that's
> a separate cross-file token change; flag it rather than silently diverging.

### More slide patterns (UX review)

- **Subtitle** — add `<p class="subhead">…</p>` after an `<h1>`/`<h2>` for the PPT-style title→subtitle structure.
- **6-up / many ideas** — six `<div class="card compact">` inside `.cols-3` wrap to a tidy 3×2 grid (`.cols-2` for two columns). More than six → split across slides.
- **Quote** — `.quote` (`<span class="qm">“</span>` + `<p>` + `<div class="who">`) as a full-page key quote or an inline block.
- **Title / opener over an image** — a `.slide.image` with the text inside a `.panel` (translucent Foundation-Blue frame) so it stays legible on a photo.
- **Divider with a half image** — `.slide.dark.flush` + `.split-bleed` (`.txt-half` + `.img-half` with a cover image).
- **Persistent brand mark** — a CC mark is pinned bottom-left on every slide automatically (filled by JS from the embedded logos, colour-swapped by slide mode, hidden on slides that already have a `.logo` lockup). No markup needed.

### Subtle background graphics — the technical approach

For the outlined-circle / watermark look from the PPT decks, **don't use background image files** (they'd
break the single self-contained deck). Use inline SVG from the sprite, positioned absolutely behind content
at low opacity — this is what `.rings` / `.rings` + `#i-rings-outline` and `.decor` already do:

- `<svg class="rings"><use href="#i-rings-outline"/></svg>` with inline `style="top/right/…"` — outlined
  concentric circles; set them large and bleeding off a corner. Works on content slides too, not just title/closing.
- `class="decor"` on a slide adds a soft palette-blue radial glow behind content.
- Both sit at `z-index:0` behind content and are decorative (`aria-hidden`); keep opacity low so text stays dominant.
- Need a different motif (dots, grid, a big faint mark)? Add one `<symbol>` to the sprite and reuse it the same way — still one file, still offline.

## 5. Interactive activities (vote → reveal)

The signature CC course pattern. Each activity: a prompt, clickable option buttons (audience votes), a
**Reveal Answer** button that marks the correct/wrong options and shows an explanation panel.

To add one, copy a `[data-activity]` block, put `data-correct` on the right `.opt`, and write the `.reveal`
text. The template's JS wires voting and reveal automatically — no per-activity scripting.
Keep examples generic unless the user asks otherwise. Always follow an activity with a one-line key takeaway.

## 6. Deck mechanics (required, all in the template)

- Keyboard: `→`/Space/PageDown next · `←`/PageUp prev · `Home`/`End` first/last · `F` fullscreen · `O` overview grid.
- A slide counter (`n / total`) and a top progress bar that recolours readably on dark slides.
- Deep-linking via URL hash (`#5` opens slide 5).
- Click right/left third of the screen to advance — works with presentation remotes.
- The `fit()` scaler runs on load, on resize, and on fullscreen change — keep all three when editing the script.

## 7. Logo & assets

Logos live in this package under `logos/` (Blue, Mono, White — full + mark). **Never** recolour, stretch, crop,
or rotate the logo. On dark slides use the white logo/mark; on light slides use the blue. The template's CSS
"CC" mark lockup is a lightweight placeholder — swap in the real `cc-logo-white-mark.png` /
`cc-logo-blue-mark.png` for final/client-facing decks.

## 8. Accessibility

- Text contrast ≥ WCAG AA: Cloud White on Foundation Blue and Heritage Blue on Cloud White both pass; never rely on Horizon Blue for body text or on small accent text on coloured fills.
- Status is never colour-only — badges always carry text.
- Interactive buttons are real `<button>`s, focusable and operable by keyboard.
- Don't autoplay motion that can't be paused; honour `prefers-reduced-motion`.

## 9. Workflow when asked to build a deck

1. Confirm: topic, audience, duration, number of slides / structure, and which activities (if any).
2. Copy `docs/presentation-template.html`; keep its `:root` tokens, the fixed-canvas `fit()` scaler, and embedded fonts untouched.
3. Replace slide content section by section. Reuse existing patterns; do not invent new component styles unless asked.
4. Self-check before delivering: no hard-coded hex, no pure white, Albert Sans everywhere (mono only for code), one idea per slide, every accent small, no slide overflows the 1280×720 frame, activities reveal correctly, keyboard nav + counter + scaler work. **Visual review:** light and dark slides alternate (not all one mode), no white-card-on-white-slide (cards contrast with their background), most content slides pair text with a visual (`.split`, icon feature list, icon card grid, stat, or diagram) with text-only used only where a point needs no visual, and icons come from the embedded sprite (line style, never a filled icon or external library). Then run `node scripts/check-deck.js <deck>.html` and clear every error before delivering.
5. Deliver the single `.html` file and tell the user to open it and press `F`.
