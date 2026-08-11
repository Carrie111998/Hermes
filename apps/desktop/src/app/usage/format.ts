import type { DailyUsage, ModelUsage, UsageMeterEvent, UsageMeterRoute, UsageMetric, UsageReport } from './types'

export const EMPTY_VALUE = '—'

export function formatNumber(value: number, locale: string): string {
  return new Intl.NumberFormat(locale).format(Number.isFinite(value) ? value : 0)
}

export function formatCompact(value: number, locale: string): string {
  if (!Number.isFinite(value)) {
    return '0'
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

export function formatPercent(value: number, locale: string, fractionDigits = 0): string {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: fractionDigits,
    style: 'percent'
  }).format(Number.isFinite(value) ? value : 0)
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
  const timestamp = new Date(`${value}T00:00:00`)

  if (Number.isNaN(timestamp.getTime())) {
    return value
  }

  return new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short' }).format(timestamp)
}

export function cacheRatio(read: number, input: number): number {
  const total = read + input

  return total > 0 ? read / total : 0
}

export function routeTokens(route: UsageMeterRoute): number {
  return route.input_tokens + route.output_tokens + route.cache_read_tokens + route.cache_write_tokens
}

export function modelTokens(model: ModelUsage): number {
  return model.input_tokens + model.output_tokens + model.cache_read_tokens + model.cache_write_tokens
}

export function dailyMetricValue(day: DailyUsage, metric: UsageMetric): number {
  if (metric === 'cost') {
    return day.cost ?? 0
  }

  if (metric === 'sessions') {
    return day.sessions
  }

  return day.input_tokens + day.output_tokens + day.cache_read_tokens + day.cache_write_tokens
}

export function reportReasoningTokens(report: UsageReport): number {
  return report.models.reduce((sum, model) => sum + model.reasoning_tokens, 0)
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
