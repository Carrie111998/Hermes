# Design

Scope: `server/webui`. Source of truth is `css/tokens.css`; this file explains
the reasoning so variants stay on-brand. If the two disagree, the CSS wins and
this file is stale.

Rota inherits the **Interfaze house design system** (`Portfolyo/DESIGN.json`),
reinterpreted for an eight-hour tool rather than a portfolio site. The house
rules below are not negotiable per-feature.

## Visual theme

An instrument panel. Flat, square, hairline-divided, information-dense.
Structure comes from rules and tone, never from elevation.

### The five house rules

| Rule | Meaning |
|---|---|
| **One Voice** | Signal Blue appears on well under 10% of any screen: marks, links, single accents. Never as fill behind content, never as a second accent. |
| **Tinted Black** | Ink is `#0A0A0A`, never `#000`. Neutrals stay on the ink/paper axis: no warm beiges, no cool blue-greys. |
| **Flat Instrument** | Surfaces never cast shadows. Separate with tone or a 1px line. A shadow anywhere is a bug. |
| **Square Corners** | `border-radius: 0` on everything: buttons, inputs, cards, media. Circles (avatars, status dots) are the sole exception. |
| **Cursor** | The Signal Blue underscore is the house mark. In Rota it doubles as the agent state indicator. |

### Theme choice

The scene: *a salesperson clearing the approval queue at 9am on an office
desktop in Istanbul, fluorescent light overhead, a spreadsheet open on the
second monitor.*

That scene argues for **light as the working default**. A dark app beside a
white spreadsheet forces constant pupil re-adaptation across an eight-hour day.
Dark ships as a full peer theme for people who prefer it, not as the primary.

Current default is `system`. Two surfaces are deliberately locked regardless of
theme:

- **The navigation rail is always ink.** It is chrome, not content.
- **The login screen is always ink.** A white form panel beside a dark image
  panel read as two unrelated screens stapled together, and the front door is
  where the product should look most like itself.

Anything painted inside those locked surfaces must use `--sidebar-*` tokens.
Page-surface tokens leak through as light chips on a dark rail.

## Color palette

**Strategy: Restrained.** Tinted neutrals plus one accent under 10%. This is
the house One Voice Rule and it is also the correct product-register default.

### Light (paper)

| Role | Token | Value |
|---|---|---|
| Ground | `--bg` | `#FFFFFF` |
| Surface | `--surface` | `#F4F4F5` |
| Raised | `--surface-2` | `#E6E6E8` |
| Sunken | `--surface-3` | `#D2D2D6` |
| Ink | `--text` | `#0A0A0A` |
| Muted | `--muted` | `#68686D` |
| Faint | `--faint` | `#8A8A90` |
| Line | `--border` | `#D2D2D6` |
| Line strong | `--border-strong` | `#A2A2A7` |
| **Signal Blue** | `--accent` | `#2563FF` |

### Dark (ink)

| Role | Token | Value |
|---|---|---|
| Ground | `--bg` | `#0A0A0A` |
| Surface | `--surface` | `#141416` |
| Raised | `--surface-2` | `#1C1C1E` |
| Sunken | `--surface-3` | `#2C2C2F` |
| Ink | `--text` | `#E6E6E8` |
| Strong | `--text-strong` | `#FFFFFF` |
| Muted | `--muted` | `#8F8F94` |
| Faint | `--faint` | `#6D6D72` |
| Line | `--border` | `#313136` |
| Line strong | `--border-strong` | `#4C4C52` |
| Blue (fill) | `--accent` | `#2563FF` |
| Blue (text) | `--accent-text` | `#5B85FF` |

**Why blue splits on dark.** `#2563FF` on `#0A0A0A` measures 4.04:1, which
fails AA for text. White on `#2563FF` is 4.84:1 and passes, so fills keep the
canonical blue and text steps up to `#5B85FF` (5.83:1). Do not use the fill
blue for small text on ink.

Dark hairlines are lifted (`#313136`, not `#2C2C2F`) because in a shadowless
mono UI they carry every division on screen and must survive an unmanaged
monitor.

### Status

Not accents. These report facts and appear nowhere in marketing.

| State | Light | Dark |
|---|---|---|
| Success / sent | `#0F7A4A` | `#3DBB7F` |
| Warning | `#8A5A00` | `#D9A441` |
| Error / failed | `#C42B1C` | `#F2685A` |

Green means mail left the building. An empty queue is idle, not success; it
renders neutral.

### Two deviations from the impeccable colour law, recorded deliberately

1. **Hex, not OKLCH.** The house system is hex-defined and shared with the
   agency site. Converting one product to OKLCH would fork the brand.
2. **`#FFFFFF` is used as paper.** The law says never `#fff`; the house rule
   says *"Paper is #ffffff used flat but never glowing."* The house rule wins
   here. Ink is correctly tinted (`#0A0A0A`, never `#000`), so the spirit of the
   law holds on the side where it matters.

## Typography

**Rota is mono-dominant.** This is the defining typographic decision. `body`
inherits mono, so navigation, tables, labels, codes, statuses and every number
are monospace by default. Sans is the exception, handed back explicitly.

| Role | Family | Used for |
|---|---|---|
| **Primary** | JetBrains Mono | Everything structural: nav, tables, labels, buttons, badges, all numerals |
| **Display** | Satoshi | Page headlines only, 24px and up |
| **Prose** | Inter | Running text only: email bodies, descriptions, help, inputs |

All three are self-hosted in `fonts/` (CSP is `font-src 'self'`, so CDN links
fail silently to Arial). Turkish coverage is verified against each font's cmap.
Inter requires **both** `latin` and `latin-ext` subsets: they are complementary,
and shipping one breaks Turkish.

**Opting back into prose.** Mono past a sentence or two is unpleasant. Any
surface holding a paragraph joins the `.ifz-prose` allowlist in `app.css`. That
list was built by scanning for prose-shaped rules (reading line-height, `ch`
measure), not by eye. Add new prose surfaces to it or they render monospace.

Provenance labels ("Public professional profile") stay mono on purpose. They
are data, not prose.

### Scale

`12 · 13 · 15 · 16 · 18 · 22 · 28 · 34`, base 13px (mono has a tall x-height,
so 13 reads close to 14 in Inter).

Display runs tight, per the house Tight-Display Rule: weight **500**, tracking
`-0.055em`, leading below 1.0. Do not set display type heavier; large type
needs less weight, and 680 fights the tracking.

Labels are uppercase mono at `0.08em`. Never synthesise mono weights: system
mono stacks ship 400 and 700 only, so a value like 620 silently rounds to bold.

Every number uses tabular figures. The user works in spreadsheets and unaligned
digits read as amateur.

## Components

### The caret

The house Cursor Rule doing a second job. A blinking underscore is the oldest
signal in computing for *ready, waiting for you*, which is exactly this
product's relationship with its operator.

| State | Behaviour | Meaning |
|---|---|---|
| `idle` | dim, still | nothing running |
| `working` | extends and retracts; tracks real progress when known | a run is in flight |
| `waiting` | blinks | needs a human. The only state that asks for attention |
| `sent` | solid green, fades | mail left the building |

`js/caret.js`. Deliberately not a glowing orb: the house donts rule out
"glowing accent on black", and the caret scales down to a table row, a tab and
the favicon.

Live regions are **opt-out** (`live: false`). Most pages already own one, and
two polite regions double-announce.

### Identity

- **Wordmark:** `rota_` with `BY INTERFAZE` beneath. No accompanying square
  mark; the caret already lives in the word and pairing them prints it twice.
- **Square mark:** `r_` on Signal Blue, for favicon, app icon, avatar,
  collapsed rail. Drawn as paths, never `<text>`, because favicons render
  outside the page and cannot load Satoshi.
- Below wordmark scale the blue ground carries the house and the letter carries
  the product. An ink favicon disappears into Chrome's dark tab strip.

### Structure

- **Tables** are pure instrument: mono, tabular, rows divided by 1px hairlines.
  No zebra striping; doing both makes a table read as texture instead of data.
- **Buttons** are mono caps with `0.08em` tracking. Hover lifts 2px per the
  house motion spec. No scale-down; that reads like a phone app.
- **Cards** are the lazy answer here. Prefer hairline-divided regions. Nested
  cards are always wrong.
- **Focus rings** are 2px solid accent at 3px offset, always visible on
  `:focus-visible`. On the ink rail they switch to white; page accent is too
  dark against ink to see.

## Layout

Division by **hairline, not by gap**. A 1px rule buys structure at zero spatial
cost, which is how density arrives without cramping. Reach for `border` before
`gap`.

Vary spacing for rhythm. The scale is `4 · 8 · 12 · 16 · 24 · 32 · 48 · 64`.

Density is the product's posture. A 68px headline and three rows floating in
margin reads as unfinished; the same flatness at high density reads as precise.
Empty space is only earned when the emptiness is content (the world map), never
when it is padding.

Body prose caps at 65 to 75ch.

## Motion

One system ease: `cubic-bezier(0.22, 1, 0.36, 1)`.

| Token | Value | Use |
|---|---|---|
| `--dur-fast` | 180ms | input border shifts, small hover colour changes |
| `--dur-standard` | 220ms | button fill, transform, most state transitions |

No bounce, no elastic. Never animate layout properties. The caret's blink
(~0.9Hz, well under the 3Hz flash threshold) is the only looping animation in
the product, and it stops under `prefers-reduced-motion` with the label
carrying the state alone.

## Imagery

Photography is the one place warmth enters an otherwise cold system, so it
should be human and industrial rather than more abstract tech.

Rules: sector-agnostic (the customer base spans every export industry, so no
factory that names a vertical); desaturated toward the palette, baked into the
file rather than applied as a CSS filter; no text, no logos, no signage; no
golden hour, no teal-orange grade.

The login photograph is a container terminal, chosen because a container is the
one thing every exporter shares and stacked containers rhyme with the UI's grid
of rectangles.

**Type over photography must be measured, not eyeballed.** The login scrim is
left-weighted (`0.90 / 0.70 / 0.15 / 0`) because all the type sits in the left
45% where the raw image is bright sky and water: unscrimmed, the headline
measured 1.94:1. Current values give 16.5 / 9.0 / 11.7:1. Re-measure if the
photograph is ever swapped.
