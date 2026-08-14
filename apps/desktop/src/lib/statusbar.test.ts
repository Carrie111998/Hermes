import { describe, expect, it } from 'vitest'

import type { UsageStats } from '@/types/hermes'

import { contextCompressionPressure, contextTokensUntilCompression } from './statusbar'

function usage(overrides: Partial<UsageStats> = {}): UsageStats {
  return {
    calls: 1,
    compression_threshold_percent: 75,
    compression_threshold_tokens: 150_000,
    context_max: 200_000,
    context_percent: 0,
    context_used: 0,
    input: 0,
    output: 0,
    total: 0,
    ...overrides
  }
}

describe('context compression pressure', () => {
  it('stays normal when threshold metadata or current occupancy is unavailable', () => {
    expect(contextCompressionPressure(usage({ compression_threshold_tokens: undefined }))).toBe('normal')
    expect(contextCompressionPressure(usage({ context_used: undefined }))).toBe('normal')
    expect(contextTokensUntilCompression(usage({ compression_threshold_tokens: undefined }))).toBeNull()
  })

  it('stays normal below 85% of the automatic compression threshold', () => {
    expect(contextCompressionPressure(usage({ context_used: 120_000 }))).toBe('normal')
    expect(contextTokensUntilCompression(usage({ context_used: 120_000 }))).toBe(30_000)
  })

  it('warns once occupancy reaches 85% of the automatic compression threshold', () => {
    expect(contextCompressionPressure(usage({ context_used: 127_500 }))).toBe('near')
    expect(contextTokensUntilCompression(usage({ context_used: 127_500 }))).toBe(22_500)
  })

  it('reports due at or beyond the automatic compression threshold', () => {
    expect(contextCompressionPressure(usage({ context_used: 150_000 }))).toBe('due')
    expect(contextCompressionPressure(usage({ context_used: 170_000 }))).toBe('due')
    expect(contextTokensUntilCompression(usage({ context_used: 170_000 }))).toBe(0)
  })
})
