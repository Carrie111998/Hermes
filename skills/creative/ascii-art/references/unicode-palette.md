# Unicode Palette (LLM-Generated Art)

Fallback route: when no tool has what's needed, draw the art directly using these
character sets.

## Character Palette

**Box Drawing:** `╔ ╗ ╚ ╝ ║ ═ ╠ ╣ ╦ ╩ ╬ ┌ ┐ └ ┘ │ ─ ├ ┤ ┬ ┴ ┼ ╭ ╮ ╰ ╯`

**Block Elements:** `░ ▒ ▓ █ ▄ ▀ ▌ ▐ ▖ ▗ ▘ ▝ ▚ ▞`

**Geometric & Symbols:** `◆ ◇ ◈ ● ○ ◉ ■ □ ▲ △ ▼ ▽ ★ ☆ ✦ ✧ ◀ ▶ ◁ ▷ ⬡ ⬢ ⌂`

## Rules

- Max width: 60 characters per line (terminal-safe)
- Max height: 15 lines for banners, 25 for scenes
- Monospace only: output must render correctly in fixed-width fonts

## Composition guidance

- Pick one shading ramp and stay in it: `░ ▒ ▓ █` for gradients, or pure
  line-drawing characters for diagrams. Mixing the two reads as noise.
- Do not mix box-drawing weights (single `─` with double `═`) on the same frame;
  the corners will not join.
- Count columns before emitting a wide frame — a single overflowing line wraps
  and destroys the whole picture.
