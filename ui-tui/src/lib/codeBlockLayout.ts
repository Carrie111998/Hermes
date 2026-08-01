// Shared layout helpers for the fenced-code panel renderer
// (`ui-tui/src/components/markdown.tsx`::CodeBlock) and its virtual-height
// estimator (`ui-tui/src/lib/virtualHeights.ts`). Keeping these in one place
// makes sure the two views agree on:
//   * the panel's effective inner content width,
//   * the safe width budget for the `borderText` language label,
//   * how the fence opens/closes, so the estimator does not silently drift
//     from what the renderer actually paints.

import { stringWidth } from '@hermes/ink'

// Below this body width, a full outline costs more than half the row in
// border + padding cells, so the renderer falls back to a left-accent view.
// The virtual-height estimator must use the same threshold or its row count
// will diverge from the mounted transcript.
export const CODE_PANEL_MIN_WIDTH = 20

// Normal-mode chrome: 1 left border + 1 right border + 1 left padding +
// 1 right padding. Narrow/compact mode only has the left border + padding,
// so its inner width is narrower by one cell on each side compared to the
// full border's *inner* width.
const NORMAL_PANEL_OVERHEAD = 4
const NARROW_PANEL_OVERHEAD = 2

export const isNarrowPanel = (cols: number, compact: boolean): boolean =>
  compact || cols < CODE_PANEL_MIN_WIDTH

export const innerContentWidth = (cols: number, compact: boolean): number => {
  const overhead = isNarrowPanel(cols, compact) ? NARROW_PANEL_OVERHEAD : NORMAL_PANEL_OVERHEAD

  return Math.max(1, cols - overhead)
}

// Ink's border-embedding path (packages/hermes-ink/src/ink/render-border.ts)
// takes the dangerous JS-substring fallback whenever
//   `stringWidth(text) >= borderLength - 2`
// The `borderText.content` here is ` ${label} `, so two of those border cells
// are the leading/trailing space, and one is the `╭` corner. The remaining
// label width is therefore `cols - 5`. Truncation must keep the *whole*
// post-truncation string inside this budget (the ellipsis is included in the
// measurement, not added on top).
export const borderLabelWidth = (cols: number): number => Math.max(0, cols - 5)

// Returns grapheme segments for a string. Uses Intl.Segmenter when available
// and falls back to code-point split (Array.from) so non-segmenter
// environments still split combined emoji / surrogate pairs cleanly.
const graphemes = (text: string): string[] => {
  if (!text) {
    return []
  }

  if (typeof Intl !== 'undefined' && 'Segmenter' in Intl) {
    const seg = new Intl.Segmenter(undefined, { granularity: 'grapheme' })

    return [...seg.segment(text)].map(s => s.segment)
  }

  return Array.from(text)
}

// Truncate `text` so the result fits inside `maxWidth` display cells, with
// grapheme-cluster boundaries preserved. `ellipsis` (default `…`) is appended
// only when truncation occurs and its width is *included* in the budget so
// the post-truncation result always satisfies `stringWidth(result) <= maxWidth`.
//
// Returns the original `text` if it already fits, an empty string if the
// budget is too small to fit even the ellipsis, and never produces broken
// surrogate pairs or replacement characters.
export const truncateToWidth = (text: string, maxWidth: number, ellipsis: string = '…'): string => {
  if (!text) {
    return ''
  }

  if (maxWidth <= 0) {
    return ''
  }

  if (stringWidth(text) <= maxWidth) {
    return text
  }

  const ellipsisWidth = stringWidth(ellipsis)

  if (ellipsisWidth > maxWidth) {
    return ''
  }

  const budget = maxWidth - ellipsisWidth
  let out = ''

  for (const g of graphemes(text)) {
    const w = stringWidth(out + g)

    if (w > budget) {
      break
    }

    out += g
  }

  return out + ellipsis
}

// Renderer- and estimator-matching fence detector. The renderer uses two
// regexes (open and close) and a small inline loop; the estimator in
// `virtualHeights.ts` consumes the same shapes so it can short-circuit on
// capped scan budgets without doing two different parses.
export const FENCE_OPEN_RE = /^\s*(`{3,}|~{3,})(.*)$/
export const FENCE_CLOSE_RE = /^\s*(`{3,}|~{3,})\s*$/

export interface FenceMatch {
  // Original language info, untrimmed — used for syntax-highlight detection.
  lang: string
  // Was the fence closed by a matching closer on a later line? Unclosed
  // fences are still rendered as code blocks by the existing renderer
  // (the closing scan is best-effort), and the estimator should mirror that.
  closed: boolean
}

// Quick check whether `line` looks like a fence opener. Cheap; the estimator
// calls this per line.
export const isFenceOpenLine = (line: string): boolean => FENCE_OPEN_RE.test(line)

// Extract the language string from a fence opener, mirroring the renderer's
// `fence[2].trim().toLowerCase()` step. Returns '' if the line is not a
// fence opener.
export const fenceLangOf = (line: string): string => {
  const m = line.match(FENCE_OPEN_RE)

  return m ? m[2]!.trim().toLowerCase() : ''
}

// Returns the inner width a wrapped source line should be measured at, given
// a transcript body width and the same compact flag the renderer was given.
// This is the only number the estimator and the renderer have to agree on.
export const fenceWrapWidth = (bodyWidth: number, compact: boolean): number =>
  innerContentWidth(bodyWidth, compact)

// Number of chrome rows the renderer adds on top of the wrapped code rows.
// Normal panel: top border + bottom border. Narrow panel: optional language
// header row only — no top/bottom border. Unconditional (renderer always
// renders an empty content row for an empty fence, so callers add at least
// one wrapped row for the code body before adding this).
export const chromeRows = (bodyWidth: number, compact: boolean, hasLang: boolean): number =>
  isNarrowPanel(bodyWidth, compact) ? (hasLang ? 1 : 0) : 2
