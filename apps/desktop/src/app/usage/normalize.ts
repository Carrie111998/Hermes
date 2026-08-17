import type { CostStatus, UsageCostBucket, UsageOverviewResponse, UsageReport } from './types'

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function overviewMetric(value: unknown, empty: boolean): number | null {
  return numberOrNull(value) ?? (empty ? 0 : null)
}

function normalizeCostStatus(value: string | undefined, hasPricing: boolean): CostStatus {
  if (value === 'included') {
    return 'included'
  }

  if (value === 'estimated') {
    return 'estimated'
  }

  if (value === 'actual') {
    return 'actual'
  }

  if (value === 'unpriced') {
    return 'unpriced'
  }

  if (value === 'unknown') {
    return 'unknown'
  }

  return hasPricing ? 'estimated' : 'unknown'
}

function normalizeCostBucket(value: UsageCostBucket | undefined): UsageCostBucket | null {
  if (!value) {
    return null
  }

  const sessions = numberOrNull(value.sessions)
  const cost = numberOrNull(value.cost_usd)
  const input = numberOrNull(value.input_tokens)
  const output = numberOrNull(value.output_tokens)

  if (sessions == null || cost == null || input == null || output == null) {
    return null
  }

  return {
    sessions,
    cost_usd: cost,
    input_tokens: input,
    output_tokens: output,
    ...(value.at_market_cost_usd === undefined
      ? {}
      : { at_market_cost_usd: numberOrNull(value.at_market_cost_usd) })
  }
}

export function normalizeUsageOverview(response: UsageOverviewResponse): UsageReport {
  const overview = response.overview ?? {}
  const empty = Boolean(response.empty)
  const rawModels = Array.isArray(response.models) ? response.models : null

  const models = (rawModels ?? []).map(model => {
    const hasPricing = Boolean(model.has_pricing)
    const costStatus = normalizeCostStatus(model.cost_status, hasPricing)
    const estimatedCost = numberOrNull(model.cost)
    const actualCost = numberOrNull(model.actual_cost)

    return {
      model: model.model || 'unknown',
      sessions: numberOrNull(model.sessions),
      api_calls: numberOrNull(model.api_calls),
      input_tokens: numberOrNull(model.input_tokens),
      output_tokens: numberOrNull(model.output_tokens),
      cache_read_tokens: numberOrNull(model.cache_read_tokens),
      cache_write_tokens: numberOrNull(model.cache_write_tokens),
      reasoning_tokens: numberOrNull(model.reasoning_tokens),
      tool_calls: numberOrNull(model.tool_calls),
      estimated_cost: costStatus === 'unknown' || costStatus === 'unpriced' ? null : estimatedCost,
      actual_cost:
        model.actual_cost_available === true || costStatus === 'actual'
          ? actualCost
          : actualCost != null && actualCost > 0
            ? actualCost
            : null,
      cost_status: costStatus,
      has_pricing: hasPricing
    }
  })

  const costBuckets = overview.cost_buckets
  const estimatedBucket = normalizeCostBucket(costBuckets?.estimated)
  const includedBucket = normalizeCostBucket(costBuckets?.included)
  const unknownBucket = normalizeCostBucket(costBuckets?.unknown)
  const includedCostSessions = numberOrNull(overview.included_cost_sessions)
  const unknownCostSessions = numberOrNull(overview.unknown_cost_sessions)
  const rawOverviewCost = numberOrNull(overview.estimated_cost)

  const hasPricedOrIncludedCoverage =
    (estimatedBucket?.sessions ?? 0) > 0 ||
    (includedBucket?.sessions ?? 0) > 0 ||
    (includedCostSessions ?? 0) > 0 ||
    models.some(model => model.cost_status === 'estimated' || model.cost_status === 'included')

  const overviewCost =
    rawOverviewCost == null || (rawOverviewCost === 0 && !hasPricedOrIncludedCoverage) ? null : rawOverviewCost

  return {
    empty,
    generated_at:
      typeof response.generated_at === 'number' && Number.isFinite(response.generated_at)
        ? new Date(response.generated_at * 1000).toISOString()
        : null,
    period_days: numberOrNull(response.days),
    source: response.source_filter ?? null,
    days: (response.daily_series ?? []).map(day => ({
      date: day.date,
      sessions: numberOrNull(day.sessions),
      input_tokens: numberOrNull(day.input_tokens),
      output_tokens: numberOrNull(day.output_tokens),
      cache_read_tokens: numberOrNull(day.cache_read_tokens),
      cache_write_tokens: numberOrNull(day.cache_write_tokens),
      cost: numberOrNull(day.estimated_cost_usd)
    })),
    models,
    platforms: (response.platforms ?? []).flatMap(platform => {
      const sessions = numberOrNull(platform.sessions)
      const totalTokens = numberOrNull(platform.total_tokens)

      return sessions == null || totalTokens == null
        ? []
        : [{ platform: platform.platform || 'unknown', sessions, total_tokens: totalTokens }]
    }),
    activity: (response.activity?.by_hour ?? []).flatMap(entry => {
      const hour = numberOrNull(entry.hour)
      const sessions = numberOrNull(entry.count)

      return hour == null || sessions == null ? [] : [{ hour, sessions }]
    }),
    top_sessions: response.top_sessions ?? [],
    tools: (response.tools ?? []).flatMap(tool => {
      const count = numberOrNull(tool.count)

      return count == null ? [] : [{ name: tool.tool || 'unknown', count }]
    }),
    skills: (response.skills?.top_skills ?? []).flatMap(skill => {
      const count = numberOrNull(skill.total_count)

      return count == null ? [] : [{ name: skill.skill || 'unknown', count }]
    }),
    totals: {
      sessions: overviewMetric(overview.total_sessions, empty),
      api_calls:
        rawModels == null || models.some(model => model.api_calls == null)
          ? null
          : models.reduce((sum, model) => sum + (model.api_calls ?? 0), 0),
      input_tokens: overviewMetric(overview.total_input_tokens, empty),
      output_tokens: overviewMetric(overview.total_output_tokens, empty),
      cache_read_tokens: overviewMetric(overview.total_cache_read_tokens, empty),
      cache_write_tokens: overviewMetric(overview.total_cache_write_tokens, empty),
      total_tokens: overviewMetric(overview.total_tokens, empty),
      cost: overviewCost,
      actual_cost:
        overview.actual_cost_available === true
          ? numberOrNull(overview.actual_cost)
          : null,
      included_cost_sessions: includedCostSessions,
      unknown_cost_sessions: unknownCostSessions,
      tool_calls: overviewMetric(overview.total_tool_calls, empty),
      skill_calls: overviewMetric(response.skills?.summary?.total_skill_actions, empty),
      cost_buckets: {
        estimated: estimatedBucket,
        included: includedBucket,
        unknown: unknownBucket
      }
    }
  }
}
