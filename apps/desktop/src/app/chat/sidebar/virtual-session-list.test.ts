import { describe, expect, it } from 'vitest'

import type { SidebarListRow } from '@/lib/session-date-groups'

import { estimateSidebarRowSize, shouldMeasureSidebarRows } from './virtual-session-list'

const sessionRow = {
  entry: { session: { id: 's1' } },
  kind: 'session'
} as SidebarListRow

const dividerRow: SidebarListRow = {
  key: 'this-week',
  kind: 'divider',
  label: 'Earlier this week'
}

describe('estimateSidebarRowSize', () => {
  it('sizes session rows at the plain row estimate in compact mode', () => {
    expect(estimateSidebarRowSize(sessionRow, false)).toBe(26)
  })

  it('sizes session rows at the taller card estimate in card mode', () => {
    expect(estimateSidebarRowSize(sessionRow, true)).toBe(66)
  })

  it('sizes dividers at their own estimate regardless of card mode', () => {
    expect(estimateSidebarRowSize(dividerRow, false)).toBe(32)
    expect(estimateSidebarRowSize(dividerRow, true)).toBe(32)
  })

  it('falls back to the row estimate for unknown slots', () => {
    expect(estimateSidebarRowSize(undefined, false)).toBe(26)
    expect(estimateSidebarRowSize(undefined, true)).toBe(66)
  })

  it('only live-measures inbox cards, never compact rows', () => {
    expect(shouldMeasureSidebarRows(false)).toBe(false)
    expect(shouldMeasureSidebarRows(true)).toBe(true)
  })
})
