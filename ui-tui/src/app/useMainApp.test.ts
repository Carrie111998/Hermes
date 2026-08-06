import { describe, expect, it } from 'vitest'

import { transcriptContentColumns } from '../lib/inputMetrics.js'

import { tuiLayoutColumns } from './useMainApp.js'

describe('tuiLayoutColumns', () => {
  it('reserves the final physical column to avoid terminal pending-wrap corruption', () => {
    expect(tuiLayoutColumns(169)).toBe(168)
    expect(tuiLayoutColumns(80)).toBe(79)
  })

  it('keeps tiny and invalid widths usable', () => {
    expect(tuiLayoutColumns(1)).toBe(1)
    expect(tuiLayoutColumns(0)).toBe(1)
    expect(tuiLayoutColumns(Number.NaN)).toBe(1)
  })
})

describe('transcriptContentColumns', () => {
  it('reserves rails, scrollbar gutter, and inner padding', () => {
    expect(transcriptContentColumns(171, 0)).toBe(167)
    expect(transcriptContentColumns(171, 6)).toBe(161)
  })

  it('never over-allocates tiny or invalid viewports', () => {
    expect(transcriptContentColumns(3, 0)).toBe(1)
    expect(transcriptContentColumns(Number.NaN, Number.NaN)).toBe(1)
  })
})
