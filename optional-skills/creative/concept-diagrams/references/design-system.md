# Concept Diagrams — Design System & Node Patterns

The canonical design-system rules for every concept diagram: philosophy, the 9 color ramps, color assignment, typography, spacing, stroke/shape, the arrow marker, and the CSS classes the template provides. Node patterns for reusable SVG building blocks are at the bottom.


### Philosophy

- **Flat**: no gradients, drop shadows, blur, glow, or neon effects.
- **Minimal**: show the essential. No decorative icons inside boxes.
- **Consistent**: same colors, spacing, typography, and stroke widths across every diagram.
- **Dark-mode ready**: all colors auto-adapt via CSS classes — no per-mode SVG.

### Color Palette

9 color ramps, each with 7 stops. Put the class name on a `<g>` or shape element; the template CSS handles both modes.

| Class      | 50 (lightest) | 100     | 200     | 400     | 600     | 800     | 900 (darkest) |
|------------|---------------|---------|---------|---------|---------|---------|---------------|
| `c-purple` | #EEEDFE | #CECBF6 | #AFA9EC | #7F77DD | #534AB7 | #3C3489 | #26215C |
| `c-teal`   | #E1F5EE | #9FE1CB | #5DCAA5 | #1D9E75 | #0F6E56 | #085041 | #04342C |
| `c-coral`  | #FAECE7 | #F5C4B3 | #F0997B | #D85A30 | #993C1D | #712B13 | #4A1B0C |
| `c-pink`   | #FBEAF0 | #F4C0D1 | #ED93B1 | #D4537E | #993556 | #72243E | #4B1528 |
| `c-gray`   | #F1EFE8 | #D3D1C7 | #B4B2A9 | #888780 | #5F5E5A | #444441 | #2C2C2A |
| `c-blue`   | #E6F1FB | #B5D4F4 | #85B7EB | #378ADD | #185FA5 | #0C447C | #042C53 |
| `c-green`  | #EAF3DE | #C0DD97 | #97C459 | #639922 | #3B6D11 | #27500A | #173404 |
| `c-amber`  | #FAEEDA | #FAC775 | #EF9F27 | #BA7517 | #854F0B | #633806 | #412402 |
| `c-red`    | #FCEBEB | #F7C1C1 | #F09595 | #E24B4A | #A32D2D | #791F1F | #501313 |

#### Color Assignment Rules

Color encodes **meaning**, not sequence. Never cycle through colors like a rainbow.

- Group nodes by **category** — all nodes of the same type share one color.
- Use `c-gray` for neutral/structural nodes (start, end, generic steps, users).
- Use **2-3 colors per diagram**, not 6+.
- Prefer `c-purple`, `c-teal`, `c-coral`, `c-pink` for general categories.
- Reserve `c-blue`, `c-green`, `c-amber`, `c-red` for semantic meaning (info, success, warning, error).

Light/dark stop mapping (handled by the template CSS — just use the class):
- Light mode: 50 fill + 600 stroke + 800 title / 600 subtitle
- Dark mode:  800 fill + 200 stroke + 100 title / 200 subtitle

### Typography

Only two font sizes. No exceptions.

| Class | Size | Weight | Use |
|-------|------|--------|-----|
| `th`  | 14px | 500    | Node titles, region labels |
| `ts`  | 12px | 400    | Subtitles, descriptions, arrow labels |
| `t`   | 14px | 400    | General text |

- **Sentence case always.** Never Title Case, never ALL CAPS.
- Every `<text>` MUST carry a class (`t`, `ts`, or `th`). No unclassed text.
- `dominant-baseline="central"` on all text inside boxes.
- `text-anchor="middle"` for centered text in boxes.

**Width estimation (approx):**
- 14px weight 500: ~8px per character
- 12px weight 400: ~6.5px per character
- Always verify: `box_width >= (char_count × px_per_char) + 48` (24px padding each side)

### Spacing & Layout

- **ViewBox**: `viewBox="0 0 680 H"` where H = content height + 40px buffer.
- **Safe area**: x=40 to x=640, y=40 to y=(H-40).
- **Between boxes**: 60px minimum gap.
- **Inside boxes**: 24px horizontal padding, 12px vertical padding.
- **Arrowhead gap**: 10px between arrowhead and box edge.
- **Single-line box**: 44px height.
- **Two-line box**: 56px height, 18px between title and subtitle baselines.
- **Container padding**: 20px minimum inside every container.
- **Max nesting**: 2-3 levels deep. Deeper gets unreadable at 680px width.

### Stroke & Shape

- **Stroke width**: 0.5px on all node borders. Not 1px, not 2px.
- **Rect rounding**: `rx="8"` for nodes, `rx="12"` for inner containers, `rx="16"` to `rx="20"` for outer containers.
- **Connector paths**: MUST have `fill="none"`. SVG defaults to `fill: black` otherwise.

### Arrow Marker

Include this `<defs>` block at the start of **every** SVG:

```xml
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
          markerWidth="6" markerHeight="6" orient="auto-start-reverse">
    <path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke"
          stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </marker>
</defs>
```

Use `marker-end="url(#arrow)"` on lines. The arrowhead inherits the line color via `context-stroke`.

### CSS Classes (Provided by the Template)

The template page provides:

- Text: `.t`, `.ts`, `.th`
- Neutral: `.box`, `.arr`, `.leader`, `.node`
- Color ramps: `.c-purple`, `.c-teal`, `.c-coral`, `.c-pink`, `.c-gray`, `.c-blue`, `.c-green`, `.c-amber`, `.c-red` (all with automatic light/dark mode)

You do **not** need to redefine these — just apply them in your SVG. The template file contains the full CSS definitions.


### Node Patterns

**Single-line node (44px):**
```xml
<g class="node c-blue">
  <rect x="100" y="20" width="180" height="44" rx="8" stroke-width="0.5"/>
  <text class="th" x="190" y="42" text-anchor="middle" dominant-baseline="central">Service name</text>
</g>
```

**Two-line node (56px):**
```xml
<g class="node c-teal">
  <rect x="100" y="20" width="200" height="56" rx="8" stroke-width="0.5"/>
  <text class="th" x="200" y="38" text-anchor="middle" dominant-baseline="central">Service name</text>
  <text class="ts" x="200" y="56" text-anchor="middle" dominant-baseline="central">Short description</text>
</g>
```

**Connector (no label):**
```xml
<line x1="200" y1="76" x2="200" y2="120" class="arr" marker-end="url(#arrow)"/>
```

**Container (dashed or solid):**
```xml
<g class="c-purple">
  <rect x="40" y="92" width="600" height="300" rx="16" stroke-width="0.5"/>
  <text class="th" x="66" y="116">Container label</text>
  <text class="ts" x="66" y="134">Subtitle info</text>
</g>
```

