import type { CostStatus, UsageCostBucket, UsageOverviewResponse, UsageReport } from './types'

const EMPTY_COST_BUCKET: UsageCostBucket = {
  sessions: 0,
  cost_usd: 0,
  input_tokens: 0,
  output_tokens: 0
}

function number(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function normalizeCostStatus(value: string, actualCost: number, hasPricing: boolean): CostStatus {
  if (actualCost > 0) {return 'actual'}

  if (value === 'included') {return 'included'}

  if (value === 'estimated') {return 'estimated'}

  if (value === 'unpriced') {return 'unpriced'}

  if (value === 'unknown') {return 'unknown'}

  return hasPricing ? 'estimated' : 'unknown'
}

function normalizeCostBucket(value: UsageCostBucket | undefined): UsageCostBucket {
  if (!value) {return { ...EMPTY_COST_BUCKET }}

  return {
    sessions: number(value.sessions),
    cost_usd: number(value.cost_usd),
    input_tokens: number(value.input_tokens),
    output_tokens: number(value.output_tokens),
    ...(value.at_market_cost_usd == null ? {} : { at_market_cost_usd: number(value.at_market_cost_usd) })
  }
}

export function normalizeUsageOverview(response: UsageOverviewResponse): UsageReport {
  const overview = response.overview ?? {}

  const models = (response.models ?? []).map(model => {
    const actualCost = number(model.actual_cost)
    const hasPricing = Boolean(model.has_pricing)
    const costStatus = normalizeCostStatus(model.cost_status, actualCost, hasPricing)
    const estimatedCost = number(model.cost)

    return {
      model: model.model || 'unknown',
      sessions: number(model.sessions),
      api_calls: number(model.api_calls),
      input_tokens: number(model.input_tokens),
      output_tokens: number(model.output_tokens),
      cache_read_tokens: number(model.cache_read_tokens),
      cache_write_tokens: number(model.cache_write_tokens),
      reasoning_tokens: number(model.reasoning_tokens),
      tool_calls: number(model.tool_calls),
      cost: costStatus === 'unknown' || costStatus === 'unpriced' ? null : actualCost > 0 ? actualCost : estimatedCost,
      actual_cost: actualCost,
      cost_status: costStatus,
      has_pricing: hasPricing
    }
  })

  const costBuckets = overview.cost_buckets

  return {
    empty: Boolean(response.empty),
    generated_at:
      typeof response.generated_at === 'number' && Number.isFinite(response.generated_at)
        ? new Date(response.generated_at * 1000).toISOString()
        : null,
    period_days: number(response.days),
    source: response.source_filter ?? null,
    days: (response.daily_series ?? []).map(day => ({
      date: day.date,
      sessions: number(day.sessions),
      input_tokens: number(day.input_tokens),
      output_tokens: number(day.output_tokens),
      cache_read_tokens: number(day.cache_read_tokens),
      cache_write_tokens: number(day.cache_write_tokens),
      cost: number(day.estimated_cost_usd)
    })),
    models,
    platforms: (response.platforms ?? []).map(platform => ({
      platform: platform.platform || 'unknown',
      sessions: number(platform.sessions),
      total_tokens: number(platform.total_tokens)
    })),
    activity: (response.activity?.by_hour ?? []).map(entry => ({
      hour: number(entry.hour),
      sessions: number(entry.count)
    })),
    top_sessions: response.top_sessions ?? [],
    tools: (response.tools ?? []).map(tool => ({ name: tool.tool || 'unknown', count: number(tool.count) })),
    skills: (response.skills?.top_skills ?? []).map(skill => ({
      name: skill.skill || 'unknown',
      count: number(skill.total_count)
    })),
    totals: {
      sessions: number(overview.total_sessions),
      api_calls: models.reduce((sum, model) => sum + model.api_calls, 0),
      input_tokens: number(overview.total_input_tokens),
      output_tokens: number(overview.total_output_tokens),
      cache_read_tokens: number(overview.total_cache_read_tokens),
      cache_write_tokens: number(overview.total_cache_write_tokens),
      total_tokens: number(overview.total_tokens),
      cost: number(overview.estimated_cost),
      actual_cost: number(overview.actual_cost),
      tool_calls: number(overview.total_tool_calls),
      skill_calls: number(response.skills?.summary?.total_skill_actions),
      cost_buckets: {
        estimated: normalizeCostBucket(costBuckets?.estimated),
        included: normalizeCostBucket(costBuckets?.included),
        unknown: normalizeCostBucket(costBuckets?.unknown)
      }
    }
  }
}
