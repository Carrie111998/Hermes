import type {
  DailyUsage,
  ModelUsage,
  UsageCostBucket,
  UsageMeterBucket,
  UsageMeterEvent,
  UsageMeterRoute,
  UsageMetric,
  UsageReport
} from './types'

export const EMPTY_VALUE = '—'

export function formatNumber(value: number | null | undefined, locale: string): string {
  if (value == null || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }

  return new Intl.NumberFormat(locale).format(value)
}

export function formatCompact(value: number | null | undefined, locale: string): string {
  if (value == null || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }

  return new Intl.NumberFormat(locale, {
    compactDisplay: 'short',
    maximumFractionDigits: 1,
    notation: 'compact'
  }).format(value)
}

export function formatCurrency(value: number | null | undefined, locale: string): string {
  if (value == null || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }

  if (value === 0) {
    return '$0.00'
  }

  if (Math.abs(value) < 0.01) {
    return '<$0.01'
  }

  return new Intl.NumberFormat(locale, {
    currency: 'USD',
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
    style: 'currency'
  }).format(value)
}

export function formatPercent(value: number | null | undefined, locale: string, fractionDigits = 0): string {
  if (value == null || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }

  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: fractionDigits,
    style: 'percent'
  }).format(value)
}

export function formatTimestamp(value: number | string | null | undefined, locale: string): string {
  if (value == null || value === '') {
    return EMPTY_VALUE
  }

  const timestamp = new Date(typeof value === 'number' && value < 1_000_000_000_000 ? value * 1000 : value)

  if (Number.isNaN(timestamp.getTime())) {
    return String(value)
  }

  return new Intl.DateTimeFormat(locale, {
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    month: 'short',
    year: 'numeric'
  }).format(timestamp)
}

export function formatShortDate(value: string, locale: string): string {
  const englishMonths = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  const compactMatch = /^([A-Z][a-z]{2}) (\d{1,2})$/.exec(value)
  const compactMonth = compactMatch ? englishMonths.indexOf(compactMatch[1]) : -1

  const timestamp =
    compactMatch && compactMonth >= 0
      ? new Date(Date.UTC(2000, compactMonth, Number(compactMatch[2])))
      : new Date(`${value}T00:00:00`)

  if (Number.isNaN(timestamp.getTime())) {
    return value
  }

  return new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', timeZone: 'UTC' }).format(timestamp)
}

export function cacheRatio(read: number | null | undefined, input: number | null | undefined): number | null {
  if (read == null || input == null) {
    return null
  }

  const total = read + input

  return total > 0 ? read / total : 0
}

export function routeTokens(route: UsageMeterRoute): number {
  return route.input_tokens + route.output_tokens + route.cache_read_tokens + route.cache_write_tokens
}

export function meterEstimatedCost(
  meter: Pick<UsageMeterBucket, 'calls' | 'estimated_cost_usd' | 'included_calls' | 'priced_calls' | 'unpriced_calls'>
): number | null {
  if (meter.calls <= 0) {
    return null
  }

  if (meter.priced_calls > 0) {
    return meter.estimated_cost_usd
  }

  if (meter.unpriced_calls > 0) {
    return null
  }

  return meter.included_calls > 0 ? 0 : null
}

export function reportMarketEquivalent(report: UsageReport): number | null {
  const estimated = report.totals.cost_buckets.estimated
  const included = report.totals.cost_buckets.included
  const unknown = report.totals.cost_buckets.unknown

  if (report.totals.cost == null || (unknown?.sessions ?? 0) > 0) {
    return null
  }

  const bucketMarketValue = (bucket: UsageCostBucket | null, legacyFallback?: number): number | null => {
    if (!bucket || bucket.sessions <= 0) {
      return 0
    }

    if (bucket.at_market_cost_usd === undefined) {
      return legacyFallback ?? null
    }

    return bucket.at_market_cost_usd
  }

  const estimatedMarket = bucketMarketValue(estimated, estimated?.cost_usd ?? 0)
  const includedMarket = bucketMarketValue(included)

  return estimatedMarket == null || includedMarket == null ? null : estimatedMarket + includedMarket
}

export function modelTokens(model: ModelUsage): number | null {
  const values = [model.input_tokens, model.output_tokens, model.cache_read_tokens, model.cache_write_tokens]

  return values.some(value => value == null) ? null : values.reduce<number>((sum, value) => sum + (value ?? 0), 0)
}

export function dailyMetricValue(day: DailyUsage, metric: UsageMetric): number | null {
  if (metric === 'cost') {
    return day.cost
  }

  if (metric === 'sessions') {
    return day.sessions
  }

  const values = [day.input_tokens, day.output_tokens, day.cache_read_tokens, day.cache_write_tokens]

  return values.some(value => value == null) ? null : values.reduce<number>((sum, value) => sum + (value ?? 0), 0)
}

export function reportReasoningTokens(report: UsageReport): number | null {
  return report.models.some(model => model.reasoning_tokens == null)
    ? null
    : report.models.reduce((sum, model) => sum + (model.reasoning_tokens ?? 0), 0)
}

export function eventTokens(event: UsageMeterEvent): number {
  return event.input_tokens + event.output_tokens + event.cache_read_tokens + event.cache_write_tokens
}

export function eventKey(event: UsageMeterEvent, index: number): string {
  return String(event.id || `${event.ts}:${event.profile}:${event.session_id}:${event.task_id}:${index}`)
}

export function uniqueValues(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b))
}

export function compactIdentifier(value: string, edge = 6): string {
  if (!value) {
    return EMPTY_VALUE
  }

  if (value.length <= edge * 2 + 1) {
    return value
  }

  return `${value.slice(0, edge)}…${value.slice(-edge)}`
}
