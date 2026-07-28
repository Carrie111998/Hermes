import spinners, { type BrailleSpinnerName as SpinnerName } from 'unicode-animations'

import { cn } from '@/lib/utils'

export type { SpinnerName }

// Some spinners ship multi-character frames. Pull the first cell so each
// glyph fits in one monospace box, matching how the TUI uses them. Desktop
// deliberately keeps the first frame stable to avoid decorative React churn.
const GLYPH_BY_NAME: Record<SpinnerName, string> = (() => {
  const out = {} as Record<SpinnerName, string>

  for (const name of Object.keys(spinners) as SpinnerName[]) {
    const raw = spinners[name]

    out[name] = [...(raw.frames[0] ?? '')][0] ?? '⠀'
  }

  return out
})()

interface GlyphSpinnerProps {
  ariaLabel?: string
  className?: string
  spinner?: SpinnerName
}

/**
 * One-char status glyph sampled from `unicode-animations` (braille, orbit,
 * scan, etc. — pick any `spinner` name). The Desktop glyph stays static so a
 * busy status does not schedule React updates; the Ink TUI remains animated.
 * Renders inside an `inline-flex` cell with `leading-none` and `items-center`
 * so it sits vertically centred inside its parent's line-box.
 */
export function GlyphSpinner({ ariaLabel = 'Loading', className, spinner = 'braille' }: GlyphSpinnerProps) {
  const glyph = GLYPH_BY_NAME[spinner] ?? GLYPH_BY_NAME.braille!

  return (
    <span
      aria-label={ariaLabel}
      className={cn('inline-flex items-center justify-center font-mono leading-none tabular-nums', className)}
      role="status"
    >
      {glyph}
    </span>
  )
}
