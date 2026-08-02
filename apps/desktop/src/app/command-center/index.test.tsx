import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AnalyticsResponse } from '@/hermes'

import { UsagePanel } from './index'

const usage: AnalyticsResponse = {
  by_model: [
    {
      api_calls: 7,
      estimated_cost: 0.25,
      input_tokens: 900_000,
      model: 'provider/model-a',
      output_tokens: 100_000,
      sessions: 2
    },
    {
      api_calls: 3,
      estimated_cost: 0.006,
      input_tokens: 45_000,
      model: 'provider/model-b',
      output_tokens: 5_000,
      sessions: 1
    },
    {
      api_calls: 1,
      estimated_cost: 0.000001,
      input_tokens: 10,
      model: 'provider/tiny-cost-model',
      output_tokens: 0,
      sessions: 1
    }
  ],
  daily: [],
  period_days: 30,
  skills: {
    summary: {
      distinct_skills_used: 0,
      total_skill_actions: 0,
      total_skill_edits: 0,
      total_skill_loads: 0
    },
    top_skills: []
  },
  totals: {
    total_actual_cost: 0,
    total_api_calls: 75,
    total_cache_read: 0,
    total_estimated_cost: 0.3190519836,
    total_input: 1_708_111,
    total_output: 23_204,
    total_reasoning: 0,
    total_sessions: 3
  }
}

describe('UsagePanel', () => {
  it('shows each model estimated cost and effective rate over displayed tokens', () => {
    render(<UsagePanel error="" loading={false} onRefresh={vi.fn()} period={30} usage={usage} />)

    expect(
      screen.getByText('1M I/O tokens · recorded est. $0.25 · effective $0.25 per 1M displayed I/O tokens')
    ).toBeTruthy()
    expect(
      screen.getByText('50k I/O tokens · recorded est. $0.0060 · effective $0.12 per 1M displayed I/O tokens')
    ).toBeTruthy()
    expect(
      screen.getByText('10 I/O tokens · recorded est. <$0.0001 · effective $0.10 per 1M displayed I/O tokens')
    ).toBeTruthy()
  })

  it('does not present unknown or included zero-cost data as a priced estimate', () => {
    const zeroCostUsage: AnalyticsResponse = {
      ...usage,
      by_model: [
        {
          api_calls: 1,
          estimated_cost: 0,
          input_tokens: 1_000,
          model: 'provider/zero-cost-model',
          output_tokens: 100,
          sessions: 1
        }
      ]
    }

    render(<UsagePanel error="" loading={false} onRefresh={vi.fn()} period={30} usage={zeroCostUsage} />)

    expect(screen.getByText('1.1k I/O tokens')).toBeTruthy()
    expect(screen.queryByText(/\$0\.00|unavailable/i)).toBeNull()
  })
})
