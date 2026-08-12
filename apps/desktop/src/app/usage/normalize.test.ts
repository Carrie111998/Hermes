import { describe, expect, it } from 'vitest'

import { usageOverviewFixture } from './fixtures.test-util'
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
})