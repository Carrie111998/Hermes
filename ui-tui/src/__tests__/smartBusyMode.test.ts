import { describe, expect, it } from 'vitest'

import { normalizeBusyInputMode } from '../app/useConfigSync.js'


describe('normalizeBusyInputMode', () => {
  it('preserves SMART mode instead of silently demoting it to queue', () => {
    expect(normalizeBusyInputMode('smart')).toBe('smart')
    expect(normalizeBusyInputMode(' SMART ')).toBe('smart')
  })

  it('keeps the existing modes and fails unknown values closed to queue', () => {
    expect(normalizeBusyInputMode('interrupt')).toBe('interrupt')
    expect(normalizeBusyInputMode('steer')).toBe('steer')
    expect(normalizeBusyInputMode('queue')).toBe('queue')
    expect(normalizeBusyInputMode('nonsense')).toBe('queue')
  })
})
