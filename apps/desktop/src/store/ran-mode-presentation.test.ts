import { describe, expect, it } from 'vitest'

import {
  ranModeStatusStackMaxClass,
  shouldAutoOpenToolDiff,
  shouldShowPinnedSection
} from './ran-mode-presentation'

describe('Ran Mode presentation policy', () => {
  it('collapses successful settled diffs but never hides running or failed work', () => {
    expect(
      shouldAutoOpenToolDiff({ hasInlineDiff: true, isError: false, isPending: false, ranModeEnabled: true })
    ).toBe(false)
    expect(
      shouldAutoOpenToolDiff({ hasInlineDiff: true, isError: false, isPending: true, ranModeEnabled: true })
    ).toBe(true)
    expect(
      shouldAutoOpenToolDiff({ hasInlineDiff: true, isError: true, isPending: false, ranModeEnabled: true })
    ).toBe(true)
    expect(
      shouldAutoOpenToolDiff({ hasInlineDiff: true, isError: false, isPending: false, ranModeEnabled: false })
    ).toBe(true)
    expect(
      shouldAutoOpenToolDiff({ hasInlineDiff: false, isError: true, isPending: false, ranModeEnabled: true })
    ).toBe(false)
  })

  it('hides only an empty pinned section while Ran Mode is active', () => {
    expect(shouldShowPinnedSection({ pinnedCount: 0, ranModeEnabled: true })).toBe(false)
    expect(shouldShowPinnedSection({ pinnedCount: 1, ranModeEnabled: true })).toBe(true)
    expect(shouldShowPinnedSection({ pinnedCount: 0, ranModeEnabled: false })).toBe(true)
  })

  it('uses the bounded 30vh status stack only while Ran Mode is active', () => {
    expect(ranModeStatusStackMaxClass(true)).toBe('max-h-[30vh]')
    expect(ranModeStatusStackMaxClass(false)).toBe('max-h-[40vh]')
  })
})
