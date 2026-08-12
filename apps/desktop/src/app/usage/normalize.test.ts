import { describe, expect, it } from 'vitest'

import { usageOverviewFixture } from './fixtures.test-util'
import { reportMarketEquivalent } from './format'
import { normalizeUsageOverview } from './normalize'
import type { UsageOverviewResponse } from './types'

describe('normalizeUsageOverview', () => {
  it('preserves a provider-actual zero when the producer marks it actual', () => {
    const response: UsageOverviewResponse = {
      ...usageOverviewFixture,
      models: [
        {
          ...usageOverviewFixture.models[0],
          actual_cost: 0,
          cost_status: 'actual'
        }
      ]
    }

    expect(normalizeUsageOverview(response).models[0].actual_cost).toBe(0)
  })

  it('does not apply report-wide actual availability to an unmarked model zero', () => {
    const response: UsageOverviewResponse = {
      ...usageOverviewFixture,
      overview: {
        ...usageOverviewFixture.overview,
        actual_cost: 0,
        actual_cost_available: true
      },
      models: [
        {
          ...usageOverviewFixture.models[0],
          actual_cost: 0,
          actual_cost_available: false,
          cost_status: 'estimated'
        }
      ]
    }

    const report = normalizeUsageOverview(response)

    expect(report.totals.actual_cost).toBe(0)
    expect(report.models[0].actual_cost).toBeNull()
  })

  it('keeps omitted non-empty model and daily metrics unavailable', () => {
    const response = {
      ...usageOverviewFixture,
      daily_series: [{ date: '2026-08-11', sessions: 1 }],
      models: [{ model: 'partial-model', cost_status: 'unknown', has_pricing: false }]
    } as unknown as UsageOverviewResponse

    const report = normalizeUsageOverview(response)

    expect(report.models[0]).toMatchObject({
      actual_cost: null,
      api_calls: null,
      input_tokens: null,
      output_tokens: null,
      sessions: null
    })
    expect(report.days[0]).toMatchObject({
      cost: null,
      input_tokens: null,
      output_tokens: null,
      sessions: 1
    })
  })

  it('does not collapse missing or malformed market-equivalent evidence to zero', () => {
    const omitted = normalizeUsageOverview({
      ...usageOverviewFixture,
      overview: {
        ...usageOverviewFixture.overview,
        cost_buckets: {
          estimated: usageOverviewFixture.overview.cost_buckets!.estimated,
          included: {
            sessions: 1,
            cost_usd: 0,
            input_tokens: 1_000,
            output_tokens: 500
          },
          unknown: usageOverviewFixture.overview.cost_buckets!.unknown
        }
      }
    })

    const malformed = normalizeUsageOverview({
      ...usageOverviewFixture,
      overview: {
        ...usageOverviewFixture.overview,
        cost_buckets: {
          estimated: usageOverviewFixture.overview.cost_buckets!.estimated,
          included: {
            sessions: 1,
            cost_usd: 0,
            input_tokens: 1_000,
            output_tokens: 500,
            at_market_cost_usd: Number.NaN
          },
          unknown: usageOverviewFixture.overview.cost_buckets!.unknown
        }
      }
    })

    expect(omitted.totals.cost_buckets.included?.at_market_cost_usd).toBeUndefined()
    expect(malformed.totals.cost_buckets.included?.at_market_cost_usd).toBeNull()
  })

  it('keeps unknown-only cost unavailable while preserving a proven included zero', () => {
    const unknownOnly = normalizeUsageOverview({
      ...usageOverviewFixture,
      models: [{ model: 'unknown-route', cost: 0, cost_status: 'unknown', has_pricing: false }],
      overview: {
        ...usageOverviewFixture.overview,
        estimated_cost: 0,
        included_cost_sessions: 0,
        unknown_cost_sessions: 1,
        cost_buckets: {
          estimated: { sessions: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0 },
          included: { sessions: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0 },
          unknown: { sessions: 1, cost_usd: 0, input_tokens: 1_000, output_tokens: 500 }
        }
      }
    })

    const includedOnly = normalizeUsageOverview({
      ...usageOverviewFixture,
      models: [{ model: 'included-route', cost: 0, cost_status: 'included', has_pricing: true }],
      overview: {
        ...usageOverviewFixture.overview,
        estimated_cost: 0,
        included_cost_sessions: 1,
        unknown_cost_sessions: 0,
        cost_buckets: {
          estimated: { sessions: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0 },
          included: {
            sessions: 1,
            cost_usd: 0,
            input_tokens: 1_000_000,
            output_tokens: 500_000,
            at_market_cost_usd: 20
          },
          unknown: { sessions: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0 }
        }
      }
    })

    expect(unknownOnly.totals.cost).toBeNull()
    expect(reportMarketEquivalent(unknownOnly)).toBeNull()
    expect(includedOnly.totals.cost).toBe(0)
    expect(reportMarketEquivalent(includedOnly)).toBe(20)
  })

  it('omits malformed secondary telemetry instead of fabricating zeros', () => {
    const report = normalizeUsageOverview({
      ...usageOverviewFixture,
      days: Number.NaN,
      activity: { by_hour: [{ hour: Number.NaN, count: 3 }, { hour: 4, count: Number.NaN }] },
      platforms: [{ platform: 'desktop' }],
      skills: { summary: {}, top_skills: [{ skill: 'invalid' }] },
      tools: [{ tool: 'invalid' }]
    })

    expect(report.period_days).toBeNull()
    expect(report.activity).toEqual([])
    expect(report.platforms).toEqual([])
    expect(report.skills).toEqual([])
    expect(report.tools).toEqual([])
    expect(report.totals.skill_calls).toBeNull()
  })

  it('keeps market equivalent unavailable when all cost buckets are absent', () => {
    const report = normalizeUsageOverview({
      ...usageOverviewFixture,
      overview: {
        ...usageOverviewFixture.overview,
        estimated_cost: 12.5,
        included_cost_sessions: undefined,
        unknown_cost_sessions: undefined,
        cost_buckets: undefined
      }
    })

    expect(report.totals.cost).toBe(12.5)
    expect(report.totals.cost_buckets).toEqual({
      estimated: null,
      included: null,
      unknown: null
    })
    expect(reportMarketEquivalent(report)).toBeNull()
  })

  it('fails closed when included-session evidence exists without its comparison bucket', () => {
    const report = normalizeUsageOverview({
      ...usageOverviewFixture,
      overview: {
        ...usageOverviewFixture.overview,
        estimated_cost: 2.17,
        included_cost_sessions: 1,
        unknown_cost_sessions: 0,
        cost_buckets: {
          estimated: { sessions: 1, cost_usd: 2.17, input_tokens: 18_000, output_tokens: 7_000 },
          unknown: { sessions: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0 }
        } as UsageOverviewResponse['overview']['cost_buckets']
      }
    })

    expect(report.totals.cost_buckets.included).toBeNull()
    expect(reportMarketEquivalent(report)).toBeNull()
  })
})
