export type CostStatus = 'actual' | 'estimated' | 'included' | 'unknown' | 'unpriced'

export type UsageCostBucket = {
  sessions: number
  cost_usd: number
  input_tokens: number
  output_tokens: number
  at_market_cost_usd?: number
}

export type UsageTotals = {
  sessions: number | null
  api_calls: number | null
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  total_tokens: number | null
  cost: number | null
  actual_cost: number | null
  tool_calls: number | null
  skill_calls: number | null
  cost_buckets: {
    estimated: UsageCostBucket | null
    included: UsageCostBucket | null
    unknown: UsageCostBucket | null
  }
}

export type DailyUsage = {
  date: string
  sessions: number | null
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  cost: number | null
}

export type ModelUsage = {
  model: string
  sessions: number | null
  api_calls: number | null
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  reasoning_tokens: number | null
  tool_calls: number | null
  estimated_cost: number | null
  actual_cost: number | null
  cost_status: CostStatus
  has_pricing: boolean
}

export type PlatformUsage = {
  platform: string
  sessions: number
  total_tokens: number
}

export type ActivityUsage = {
  hour: number
  sessions: number
}

export type TopSession = {
  label: string
  session_id: string
  value: string
  date: string
}

export type NamedUsage = {
  name: string
  count: number
}

export type UsageReport = {
  empty: boolean
  generated_at: string | null
  period_days: number
  source: string | null
  days: DailyUsage[]
  models: ModelUsage[]
  platforms: PlatformUsage[]
  activity: ActivityUsage[]
  top_sessions: TopSession[]
  tools: NamedUsage[]
  skills: NamedUsage[]
  totals: UsageTotals
}

export type UsageOverviewResponse = {
  days: number
  source_filter: string | null
  empty: boolean
  generated_at?: number
  overview: Partial<{
    total_sessions: number
    total_messages: number
    total_tool_calls: number
    total_input_tokens: number
    total_output_tokens: number
    total_cache_read_tokens: number
    total_cache_write_tokens: number
    total_tokens: number
    estimated_cost: number
    actual_cost: number
    actual_cost_available: boolean
    unknown_cost_sessions: number
    included_cost_sessions: number
    cost_buckets: {
      estimated: UsageCostBucket
      included: UsageCostBucket
      unknown: UsageCostBucket
    }
  }>
  models: Array<{
    model: string
    sessions?: number
    api_calls?: number
    input_tokens?: number
    output_tokens?: number
    cache_read_tokens?: number
    cache_write_tokens?: number
    reasoning_tokens?: number
    total_tokens?: number
    tool_calls?: number
    cost?: number
    actual_cost?: number
    actual_cost_available?: boolean
    cost_status?: string
    has_pricing?: boolean
  }>
  platforms: Array<{
    platform: string
    sessions: number
    total_tokens: number
  }>
  tools: Array<{
    tool: string
    count: number
    percentage: number
  }>
  skills: {
    summary: {
      total_skill_loads: number
      total_skill_edits: number
      total_skill_actions: number
      distinct_skills_used: number
    }
    top_skills: Array<{
      skill: string
      total_count: number
    }>
  }
  activity: Partial<{
    by_hour: Array<{ hour: number; count: number }>
  }>
  top_sessions: TopSession[]
  daily_series: Array<{
    date: string
    sessions?: number
    input_tokens?: number
    output_tokens?: number
    cache_read_tokens?: number
    cache_write_tokens?: number
    estimated_cost_usd?: number
  }>
}

export type UsageMeterBucket = {
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  reasoning_tokens: number
  estimated_cost_usd: number
  unpriced_calls: number
  included_calls: number
  priced_calls: number
  cache_hit_rate: number
  has_unpriced: boolean
}

export type UsageMeterAggregate = {
  summary: UsageMeterBucket
  routes: UsageMeterRoute[]
  event_count: number
}

export type UsageMeterSummaryResponse = {
  month_label: string
  month_start_ts: number
  month_end_ts: number
  month: UsageMeterAggregate
  all_time: UsageMeterAggregate
  db_path: string
  caveat: string
}

export type UsageMeterRoute = UsageMeterBucket & {
  provider: string
  model: string
  api_mode: string
}

export type UsageMeterDetails = UsageMeterAggregate & {
  scope: 'all' | 'month'
  label: string
  start_ts: number | null
  end_ts: number | null
  caveat: string
}

export type UsageMeterEvent = {
  id: number
  ts: number
  profile: string
  provider: string
  model: string
  api_mode: string
  platform: string
  session_id: string
  task_id: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  reasoning_tokens: number
  estimated_cost_usd: number | null
  pricing_status: string
  pricing_source: string
  request_count: number
}

export type UsageMeterRecent = {
  events: UsageMeterEvent[]
}

export type UsageMetric = 'cost' | 'tokens' | 'sessions'
export type UsageDeck = 'overview' | 'routes' | 'ledger'
export type RouteSort = 'cost' | 'tokens' | 'calls' | 'cache'
export type MeterScope = 'all' | 'month'
