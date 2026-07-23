---
name: cc-frontend-guidelines
description: >
  Enforces Computacenter brand and design system rules when writing, reviewing, or modifying
  any frontend code for CC internal applications. Trigger this skill whenever the task involves
  React/Next.js components, Tailwind CSS, page layouts, UI feedback, or visual design for any
  Computacenter internal tool — even if the user does not explicitly mention design or brand.
  This skill covers the full design system: colour tokens, Albert Sans typography hierarchy,
  4px spacing grid, button/badge/card/input/table component specs, navbar rules, the global
  consistency mandate, and WCAG 2.1 AA accessibility requirements (keyboard navigation, ARIA
  attributes, contrast ratios, focus management). Always apply it before writing a single line
  of UI code. If there is any tension between what "looks good" and what these guidelines say,
  the guidelines win.
---

# Computacenter Frontend Guidelines

Every rule below is non-negotiable unless explicitly overridden by the UX designer in writing.
When in doubt, follow the brand system — do not invent.

---

## 1. Colour Palette

**Primary — must dominate all interfaces:**

| Token | Hex | Use |
|---|---|---|
| Heritage Blue | `#0D21A0` | Primary actions, headings, interactive text, icons on light bg |
| Foundation Blue | `#011749` | Navbar, dark backgrounds, dark card variants |
| Horizon Blue | `#49ABEB` | Accent on headings only — never body text, labels, or captions |
| Cloud White | `#FAFAFF` | Page background — never pure `#FFFFFF` |

**Accent / signal — small elements, badges, semantic indicators only. Never dominant:**

| Token | Hex | Use |
|---|---|---|
| United Red | `#F12938` | Error, failed, negative, delete |
| Neural Purple | `#8459E2` | Pending, assigning |
| Circuit Green | `#54C664` | Success, completed, positive |
| Fibre Orange | `#FF7900` | In progress, warning, active |
| Voltage Yellow | `#EABE42` | Caution only |

**Operational greys — functional only (backgrounds, dividers, borders, metadata):**

| Token | Hex |
|---|---|
| Grey One | `#2A2A2B` |
| Grey Two | `#494B4D` |
| Grey Three | `#8C8D8F` |
| Grey Four | `#C3C7CC` |
| Grey Five | `#D6DCE3` |
| Grey Six | `#EDF2F8` |

**Rules:**
- No hex values inline — always CSS custom properties (`var(--cc-*)`) or Tailwind config extensions
- **Never use pure white (`#ffffff` or `#fff`)** — all white surfaces must use Cloud White (`#FAFAFF`) via `var(--cc-cloud-white)`. Applies to card backgrounds, button backgrounds, input backgrounds, and every other white-appearing surface.
- **Heritage Blue (`#0D21A0`) must never be used as a background on Foundation Blue (`#011749`)** — contrast ratio is ~1.3:1 (invisible). Notification badges and active indicators on the Foundation Blue nav bar must use colours with sufficient contrast. Notification count badges specifically use United Red (`#F12938`).
- Never introduce colours outside this palette
- Horizon Blue on headings only — never body text, labels, or captions
- Accent colours only on small elements — never as large-area backgrounds
- Trend/delta text: Circuit Green (positive), United Red (negative), grey (neutral)
- Stat numbers: always Heritage Blue

---

## 2. Typography

Font: **Albert Sans exclusively.** Self-hosted from `src/fonts/` in the style-guide package. No other font, no mixing. Available weights: Light (300), Regular (400), Medium (500), SemiBold (600), Bold (700).

**Global type scale:**

| Level | Size | Weight | Line Height | Letter Spacing | Colour | Usage |
|---|---|---|---|---|---|---|
| H1 | 32px | 700 Bold | 44px | 0 | `#0D21A0` | One per page — main page heading |
| H2 | 24px | 700 Bold | 32px | 0 | `#0D21A0` | Section headings within a page |
| Card title | 20px | 600 SemiBold | 28px | 0 | `#011749` | Every card and section heading |
| Subtitle | 16px | 500 Medium | 24px | 0 | Contextual | Supporting headings, sub-labels |
| Body | 16px | 400 Regular | 22px | 0 | `#2A2A2B` | Main content text |
| Caption | 14px | 500 Medium | 18px | 0 | `#8C8D8F` | Timestamps, secondary metadata |
| Small caps | 14px | 500 Medium | 24px | 1.6px | `#8C8D8F` | Stat card labels, letter-spaced eyebrows |

**Component type scale:**

| Level | Size | Weight | Line Height | Usage |
|---|---|---|---|---|
| Button (regular) | 14px | 600 SemiBold | 24px | Primary and secondary buttons |
| Button (small) | 14px | 600 SemiBold | 20px | Compact/icon buttons |
| Label | 14px | 500 Medium | 18px | Form field labels |
| Input value | 16px | 400 Regular | 20px | Text inside input fields |
| Helper text | 14px | 400 Regular | 18px | Form validation and hint text |
| Table header | 14px | 600 SemiBold | 18px | Column header cells |
| Table cell | 14px | 400 Regular | 18px | Row data cells |
| Badge / tag | 12px | 600 SemiBold | 16px | Status badges, pills, tags |
| Navigation item | 14px | 500 Medium | 18px | Navbar and sidebar links |
| Breadcrumb | 12px | 400 Regular | 16px | Breadcrumb trail items — right-chevron (›) separator, never a slash |

**Rules:**
- Sentence case for body text, headings, captions, and badges — first word and proper nouns only.
- Title Case for button labels and CTAs — every significant word capitalised. Applies to all button variants (primary, secondary, ghost, danger) and any interactive element that triggers an action.
- Title Case for form field labels and table column headers — every word capitalised, e.g. "Connection Name", "Device Name". Never ALL CAPS by default, so deliberate acronyms/abbreviations ("ID", "SKU") remain distinguishable.
- ALL CAPS only for KPI stat card labels and acronyms
- No underline, shadow, or decorative text effects
- No custom sizes or weights outside this table

---

## 3. Layout & Spacing

- Page background: always `#FAFAFF` (`--cc-cloud-white`)
- Application content areas are full-width — never apply max-width to the main content area of an application. Content must fill the full available width (viewport minus navigation). Only narrow elements such as form inputs or modal dialogs may have their own width constraints.
- **4px base spacing unit** — every spacing value must be a multiple of 4px; use named tokens, never hard-coded px values
- All pages share identical top/left/right padding — one shared layout wrapper, never per-page margins
- All content including navbar aligns to the same horizontal boundaries

**Spacing tokens** (import from `@computacenter-ro/style-guide/tokens`):

| Token | Value | Use |
|---|---|---|
| `xs` | 4px | Icon gaps, badge padding |
| `sm` | 8px | Button gaps, compact list items |
| `md` | 12px | Table cells, input padding |
| `base` | 16px | Card inner padding, form groups |
| `lg` | 24px | Card padding, grid gaps |
| `xl` | 32px | Between major layout blocks |
| `page` | 48px | Page horizontal padding / top margin |
| `2xl` | 64px | Section separation |

**Transition tokens:**
- `fast` 120ms ease — hover fills, micro-interactions
- `base` 200ms ease — button states, focus rings (default for most elements)
- `slow` 350ms ease — panels, modals

**Radius tokens:** only 3 options — `sm` 4px (checkboxes, small tags, chips) · `md` 8px (the regular radius for almost everything: cards, tables, modals, buttons, text inputs, dropdowns) · `full` 9999px (badges, pills). `lg` and `button` are deprecated aliases of `md` (8px) — do not use in new code.

**Shadow tokens** (Foundation Blue tinted):
- `sm` — cards default · `md` — hover/elevated · `lg` — dropdowns · `xl` — modals

---

## 4. Component Specs

### Navigation Bar

**Top nav** (`#011749` bg, full width): CC logo mark + app name left; primary nav labels centre-left; dark mode toggle, notification bell, user chip right. Labels: white, 14px Regular. Active: white text + 3px Horizon Blue (`#49ABEB`) underline flush against the text baseline. Hover: white at 80% opacity. **No icons next to labels in the top nav** — label text only. Maximum 5–6 items; overflow into a "More" dropdown. Notification badge: United Red bg `#F12938`, Cloud White text, `rounded-cc-full`, visible only when count > 0, capped at 99+. User chip click opens the profile dropdown.

**Side nav — expanded** (`#011749` bg, 260px): header (logo + app name + collapse `«` toggle); full-width dark search input (`rgba(255,255,255,0.08)` fill, `rounded-cc-md`); section headers (all-caps, 11px, 300 Light, `#8C8D8F`, 0.08em spacing); primary items with 20px icon (Horizon Blue `#49ABEB` inactive / Heritage Blue `#0D21A0` active) + 14px 500 Medium label (white inactive / `#0D21A0` active) — active item has white `#FFFFFF` fill + `rounded-cc-md` background with 8px inset; sub-items indented 36px, no icon — sub-item hover fill (`rgba(255,255,255,0.08)`, `rounded-cc-md`) must keep an 8px gap from the vertical indent line, never touching it; bottom section has notification row, user row, and light/dark pill toggle. **Icons are required on every menu item.**

**Side nav — collapsed** (`#011749` bg, 64px): icon-only items (20px, centred, 12px padding); active item has Heritage Blue `#0D21A0` background pill; hover on expandable items opens a flyout panel to the right (white bg, `shadow-cc-lg`, `rounded-cc-md`) showing the item name + sub-items in expanded-sidebar sub-item style.

**User profile dropdown**: white card, `rounded-cc-md`, `shadow-cc-lg`, min-width 200px. Header: 40px avatar + name (14px 600, `#2A2A2B`) + email (12px, `#8C8D8F`), `#EDF2F8` separator. Items: 16px icon + 14px label (`#2A2A2B`); hover `#EDF2F8` bg + `rounded-cc-sm`; active/current item in Heritage Blue `#0D21A0`; Logout in United Red `#F12938`.

**Rules:**
1. Nav bg always `#011749` — no other colour, no gradients
2. Top nav: labels only, **no icons** next to labels
3. Side nav: **icons required** — every item must have an icon (the only nav content in collapsed state)
4. One active item at a time — mutually exclusive across all nav items
5. Logo: never alter, recolour, stretch, crop, or rotate
6. Notification badge: show only when count > 0; cap display at 99+
7. TopNav `<nav>` uses `aria-label="Primary navigation"` · SideNav `<nav>` uses `aria-label="Sidebar navigation"` — these labels must be distinct so both landmarks are uniquely identifiable on pages that render both simultaneously (WCAG 2.4.1 failure if identical)
8. Collapse state persists — store in localStorage or equivalent
9. No action buttons in the top nav unless usage research confirms extremely high frequency
10. Dropdown menus close on outside click, Escape key, and on route change

### Buttons

4 variants × 2 sizes × 3 types. All 6 states required on every button (Enabled / Hover / Pressed / Focused / Disabled / Loading).

**Variants (enabled state):**

| Variant | Background | Border | Text |
|---|---|---|---|
| Primary | `#0D21A0` | none | white |
| Secondary | `#D0E9FA` | none | `#0D21A0` |
| Hollow / Outline | transparent | 1px solid `#0D21A0` | `#0D21A0` |
| Clear / Ghost | transparent | none | `#0D21A0` |

> ⚠️ What was previously called "Secondary" (transparent + border) is now **Hollow / Outline**. The new Secondary has a light blue fill.

**Sizes:**

| Size | Height | H-padding | Font size | Weight |
|---|---|---|---|---|
| Regular | 40px | 24px | 14px | 600 SemiBold |
| Compact | 32px | 16px | 14px | 600 SemiBold |

Both sizes: border radius 8px (`md`). Use Compact in dense areas (toolbars, table actions, filter bars).

**Types:** Text only · Icon only (square) · Text + Icon — all radius 8px (`md`)

**Key state rules:**
- Button labels always use Title Case — "Open App", "View Details", "Add Application". Sentence case applies to body text and captions only, not to buttons, CTAs, form labels, or table headers.
- Hover/Pressed: tint bg with Horizon Blue shades (`#A3D4F5` pressed, `#D0E9FA` hover fill for non-primary)
- Focused: 2px solid `#011749` outline offset 2px
- Disabled: `#EDF2F8` / `#D6DCE3` / `#8C8D8F` — `cursor: not-allowed`
- Loading: show spinner + muted appearance; never silently disable
- One Primary per page maximum. Button styles identical across all pages.

### Cards
- Border: `1px solid #EDF2F8` · Shadow: `0 2px 8px rgba(1,23,73,0.06)` · Radius: 8px · Background: white
- Card title: 20px / 600 semibold / `#011749` — identical on every card, every page
- No visible borders on content containers inside cards — only on input fields and table rows

### Badges & Status Pills
- Shape: full border radius (pill)
- Colours: exact solid hexes below — background, 1px border, and text are three **different** colours per status (border ≠ text). Never re-derive via opacity formulas; combinations are accessibility-tested. Text 12–13px, sentence case.

| Status | Background | Border | Text |
|---|---|---|---|
| Error | `#FFE2E4` | `#F12938` | `#A30914` |
| Warning | `#FFF4E4` | `#FF7900` | `#B45500` |
| Pending | `#FFFEE4` | `#EABE42` | `#8D6700` |
| Success | `#E7F4E9` | `#54C664` | `#13681F` |
| Info | `#EAF7FF` | `#49ABEB` | `#005C99` |
| Inactive | `#E2E2E2` | `#8C8C8F` | `#4E4E4F` |
| Other | `#F3EAFF` | `#9459E2` | `#552496` |
| Primary | `#E7E9F6` | `#1F39DB` | `#0D21A0` |

A shared `Badge` component is planned. Until it ships, implement badges by following this table exactly (also exported as `badgeColors` from the tokens) — never deviate from the bg/border/text values. Text always sentence case, never ALL CAPS.

### Forms & Inputs
- Label always above and visible — never placeholder-only
- Label: 13–14px Regular/Medium, `#2A2A2B`, Title Case — e.g. "Connection Name"
- Focus: 2px `#0D21A0` border · Error: `#F12938` border + message below · Disabled: `#D6DCE3` bg, `#8C8D8F` text
- All dropdowns must show a visible chevron

### Tables & Lists
- Header row: `#E3E8F4` bg · text Foundation Blue `#011749`, 14px/600/18px, no letter spacing · Title Case ("Device Name", "Assigned To"), never ALL CAPS (deliberate acronyms like "ID"/"SKU" excepted)
- Zebra striping: odd rows default bg, even rows `#F4F4F9` (all-white bodies are hard on the eyes)
- Row separator: `1px solid #D6DCE3` bottom only · Hover: `#EDF2F8` bg · Row padding: 14–16px vertical
- Primary row text: 14px Regular Grey One · Metadata: 12px Light Grey Three
- Navigable text: Heritage Blue `#0D21A0` only — never use blue for non-navigable text
- Action icons: `#0D21A0` · Destructive icons: `#F12938`
- No decorative icons in tables

### Icons

All icons come from **Phosphor Icons** (`@phosphor-icons/react`). Phosphor is MIT-licensed, tree-shakeable, and ships 1,248 icons across six weights. It is a required peer dependency — install it alongside this package.

```bash
npm install @phosphor-icons/react
```

**Weight to use:** `regular` (the default). This matches the brand spec — line-style, single colour, no fills. Never use `fill`, `duotone`, or `bold` weights.

**Size:** 20px in most UI contexts (table rows, input trailing icons, nav items). 24px for standalone emphasis (empty states, section headers).

**Colour:** always via `currentColor` or a Tailwind colour class — never a hardcoded hex.

```tsx
import { ArrowDown, User, Bell, MagnifyingGlass } from '@phosphor-icons/react';

// Colour via Tailwind
<ArrowDown className="text-cc-heritage-blue" size={20} />

// Colour via inline style on a dark background
<Bell color="white" size={20} />
```

**Rules:**
- Import only from `@phosphor-icons/react` — never from external icon libraries (Font Awesome, Heroicons, Lucide, etc.)
- One icon per concept — do not mix Phosphor weights on the same page
- Icons must never be purely decorative — every icon must perform an action, indicate state, or convey information
- Colour on light backgrounds: Heritage Blue `#0D21A0` or Grey Two `#494B4D`
- Colour on Foundation Blue backgrounds: white only
- No icons next to top nav labels or card/section titles
- Icons are required on every side nav item (the only content visible in collapsed state)

---

## 5. Page-Level Rules

- One H1 page title per page: Heritage Blue `#0D21A0`, 32px Bold
- One primary CTA button per page (or none — use secondary/ghost)
- Spacing above the page title and horizontal page margins identical across all pages
- Search bar + table/grid: wrap together in a single shared card or container

---

## 6. Global Consistency Rule

> **This is the most important rule.**

Any change to a shared component (button, badge, card, input, nav, typography) must be implemented in the shared component and propagate automatically to every page. Never fix one page and leave others inconsistent.

Preference order:
1. Update the shared component → automatic global propagation
2. Update a shared CSS variable or token → targeted global propagation
3. Per-page fix → only when the element is genuinely page-specific

---

## 7. Enforcement Workflow

On every task, in this order:

1. **Audit first** — identify every page and component affected before writing any code
2. **Separate global from local** — implement global (shared component) changes first
3. **One component at a time** — verify each before moving on
4. **Do not implement placeholders** — anything marked "concept" or "to be revisited" stays untouched
5. **Deliver a compliance matrix** — list every item, files modified, and global vs local scope

---

## 8. Development Environment

**MSW:** active only when `NODE_ENV=development` AND `NEXT_PUBLIC_ENABLE_MOCK=true`. Zero MSW code in production builds. One handler file per API domain. Document in `MOCKING.md`.

**Design vs logic:** treat as strictly separate tasks. Never change logic as a side effect of a design change, and vice versa.
