import type { UsageMeterDetails, UsageMeterRecent, UsageMeterSummaryResponse, UsageOverviewResponse } from './types'

const dailyCosts = Array.from({ length: 30 }, (_, index) => {
  const pulse = index % 6 === 0 ? 1.55 : index % 4 === 0 ? 1.22 : 0.91

  return Number((pulse * 1.026 + (index === 29 ? 0.01 : 0)).toFixed(4))
})

const costScale = 32.11 / dailyCosts.reduce((sum, value) => sum + value, 0)

export const usageOverviewFixture: UsageOverviewResponse = {
  days: 30,
  source_filter: null,
  empty: false,
  generated_at: 1_786_465_135,
  overview: {
    total_sessions: 24,
    total_messages: 311,
    total_tool_calls: 162,
    total_input_tokens: 620_000,
    total_output_tokens: 410_000,
    total_cache_read_tokens: 510_000,
    total_cache_write_tokens: 112_000,
    total_tokens: 1_652_000,
    estimated_cost: 32.11,
    actual_cost: 4.83,
    unknown_cost_sessions: 2,
    included_cost_sessions: 3,
    cost_buckets: {
      estimated: { sessions: 19, cost_usd: 27.28, input_tokens: 498_000, output_tokens: 338_000 },
      included: {
        sessions: 3,
        cost_usd: 0,
        input_tokens: 84_000,
        output_tokens: 51_000,
        at_market_cost_usd: 4.83
      },
      unknown: { sessions: 2, cost_usd: 0, input_tokens: 38_000, output_tokens: 21_000 }
    }
  },
  models: [
    {
      model: 'gpt-5.6-sol',
      sessions: 15,
      api_calls: 62,
      input_tokens: 391_000,
      output_tokens: 241_000,
      cache_read_tokens: 342_000,
      cache_write_tokens: 82_000,
      reasoning_tokens: 46_000,
      total_tokens: 1_056_000,
      tool_calls: 91,
      cost: 20.74,
      actual_cost: 0,
      cost_status: 'estimated',
      has_pricing: true
    },
    {
      model: 'Hermes-4-405B',
      sessions: 7,
      api_calls: 31,
      input_tokens: 188_000,
      output_tokens: 143_000,
      cache_read_tokens: 158_000,
      cache_write_tokens: 30_000,
      reasoning_tokens: 21_000,
      total_tokens: 519_000,
      tool_calls: 58,
      cost: 11.37,
      actual_cost: 4.83,
      cost_status: 'actual',
      has_pricing: true
    },
    {
      model: 'unpriced-lab-model',
      sessions: 2,
      api_calls: 9,
      input_tokens: 41_000,
      output_tokens: 26_000,
      cache_read_tokens: 10_000,
      cache_write_tokens: 0,
      reasoning_tokens: 6_000,
      total_tokens: 77_000,
      tool_calls: 13,
      cost: 0,
      actual_cost: 0,
      cost_status: 'unknown',
      has_pricing: false
    }
  ],
  platforms: [
    { platform: 'desktop', sessions: 13, total_tokens: 920_000 },
    { platform: 'telegram', sessions: 7, total_tokens: 463_000 },
    { platform: 'cli', sessions: 4, total_tokens: 269_000 }
  ],
  tools: [
    { tool: 'terminal', count: 62, percentage: 38.3 },
    { tool: 'read_file', count: 41, percentage: 25.3 },
    { tool: 'patch', count: 32, percentage: 19.8 }
  ],
  skills: {
    summary: { total_skill_loads: 29, total_skill_edits: 3, total_skill_actions: 32, distinct_skills_used: 9 },
    top_skills: [
      { skill: 'hermes-agent', total_count: 18 },
      { skill: 'execution-integrity', total_count: 9 },
      { skill: 'source-of-truth-or-silence', total_count: 5 }
    ]
  },
  activity: {
    by_hour: Array.from({ length: 24 }, (_, hour) => ({
      hour,
      count: hour >= 12 && hour <= 20 ? (hour % 5) + 2 : hour % 3
    }))
  },
  top_sessions: [
    { label: 'Longest session', session_id: '20260811_141049_aa11bb', value: '2h 14m', date: 'Aug 11' },
    { label: 'Most messages', session_id: '20260811_161955_bb22cc', value: '311 msgs', date: 'Aug 11' },
    { label: 'Most tokens', session_id: '20260810_202511_cc33dd', value: '614,000 tokens', date: 'Aug 10' },
    { label: 'Most tool calls', session_id: '20260810_192211_dd44ee', value: '87 calls', date: 'Aug 10' }
  ],
  daily_series: Array.from({ length: 30 }, (_, index) => {
    const date = new Date(Date.UTC(2026, 6, 13 + index))
    const pulse = index % 6 === 0 ? 1.55 : index % 4 === 0 ? 1.22 : 0.91

    return {
      date: date.toISOString().slice(0, 10),
      sessions: Math.max(1, Math.round(pulse * 1.3)),
      input_tokens: Math.round(pulse * 20_400),
      output_tokens: Math.round(pulse * 13_500),
      cache_read_tokens: Math.round(pulse * 16_800),
      cache_write_tokens: Math.round(pulse * 3_700),
      estimated_cost_usd: Number((dailyCosts[index] * costScale).toFixed(4))
    }
  })
}

const allTimeSummary = {
  calls: 102,
  input_tokens: 655_000,
  output_tokens: 432_000,
  cache_read_tokens: 551_000,
  cache_write_tokens: 121_000,
  reasoning_tokens: 73_000,
  estimated_cost_usd: 34.87,
  unpriced_calls: 9,
  included_calls: 7,
  priced_calls: 86,
  cache_hit_rate: 0.4569,
  has_unpriced: true
}

const allTimeRoutes = [
  {
    provider: 'openai-codex',
    model: 'gpt-5.6-sol',
    api_mode: 'responses',
    calls: 62,
    input_tokens: 401_000,
    output_tokens: 248_000,
    cache_read_tokens: 351_000,
    cache_write_tokens: 83_000,
    reasoning_tokens: 47_000,
    estimated_cost_usd: 21.42,
    unpriced_calls: 0,
    included_calls: 4,
    priced_calls: 58,
    cache_hit_rate: 0.4668,
    has_unpriced: false
  },
  {
    provider: 'nous',
    model: 'Hermes-4-405B',
    api_mode: 'chat_completions',
    calls: 31,
    input_tokens: 201_000,
    output_tokens: 158_000,
    cache_read_tokens: 190_000,
    cache_write_tokens: 38_000,
    reasoning_tokens: 22_000,
    estimated_cost_usd: 13.45,
    unpriced_calls: 0,
    included_calls: 3,
    priced_calls: 28,
    cache_hit_rate: 0.486,
    has_unpriced: false
  },
  {
    provider: 'lab',
    model: 'unpriced-lab-model',
    api_mode: 'chat_completions',
    calls: 9,
    input_tokens: 53_000,
    output_tokens: 26_000,
    cache_read_tokens: 10_000,
    cache_write_tokens: 0,
    reasoning_tokens: 4_000,
    estimated_cost_usd: 0,
    unpriced_calls: 9,
    included_calls: 0,
    priced_calls: 0,
    cache_hit_rate: 0.1587,
    has_unpriced: true
  }
]

export const usageMeterSummaryFixture: UsageMeterSummaryResponse = {
  month_label: '2026-08',
  month_start_ts: 1_785_542_400,
  month_end_ts: 1_788_220_800,
  month: { summary: { ...allTimeSummary, calls: 88 }, routes: allTimeRoutes, event_count: 88 },
  all_time: { summary: allTimeSummary, routes: allTimeRoutes, event_count: 102 },
  db_path: 'C:/Users/test/.hermes/usage.db',
  caveat: 'Only locally captured usage-meter events are included.'
}

export const usageMeterDetailsFixture: UsageMeterDetails = {
  scope: 'all',
  label: 'all-time',
  start_ts: null,
  end_ts: null,
  summary: allTimeSummary,
  routes: allTimeRoutes,
  event_count: 102,
  caveat: 'Only locally captured usage-meter events are included.'
}

export const usageMeterRecentFixture: UsageMeterRecent = {
  events: [
    {
      id: 301,
      ts: 1_786_465_130,
      profile: 'default',
      provider: 'openai-codex',
      model: 'gpt-5.6-sol',
      api_mode: 'responses',
      platform: 'desktop',
      session_id: '20260811_141049_aa11bb',
      task_id: 'usage-dashboard',
      input_tokens: 48_000,
      output_tokens: 21_000,
      cache_read_tokens: 41_000,
      cache_write_tokens: 5_000,
      reasoning_tokens: 8_000,
      estimated_cost_usd: 2.11,
      pricing_status: 'estimated',
      pricing_source: 'pricing_catalog',
      request_count: 1
    },
    {
      id: 302,
      ts: 1_786_465_500,
      profile: 'research',
      provider: 'nous',
      model: 'Hermes-4-405B',
      api_mode: 'chat_completions',
      platform: 'telegram',
      session_id: '20260811_161955_bb22cc',
      task_id: 'accounting-audit',
      input_tokens: 41_000,
      output_tokens: 18_000,
      cache_read_tokens: 37_000,
      cache_write_tokens: 4_000,
      reasoning_tokens: 7_000,
      estimated_cost_usd: 0,
      pricing_status: 'included',
      pricing_source: 'pricing_catalog',
      request_count: 1
    },
    {
      id: 303,
      ts: 1_786_465_900,
      profile: 'lab',
      provider: 'lab',
      model: 'unpriced-lab-model',
      api_mode: 'chat_completions',
      platform: 'cli',
      session_id: '20260811_173100_cc33dd',
      task_id: 'pricing-probe',
      input_tokens: 12_000,
      output_tokens: 5_000,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      reasoning_tokens: 1_000,
      estimated_cost_usd: null,
      pricing_status: 'unpriced',
      pricing_source: '',
      request_count: 1
    }
  ]
}
