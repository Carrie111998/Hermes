import { type ReactNode, useMemo, useState } from 'react'

import { SegmentedControl } from '@/components/ui/segmented-control'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n/context'
import { Activity, Archive, Cpu, CreditCard, GitFork, Hash, Layers3, Terminal, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { useTheme } from '@/themes/context'

import { usageDarkStyle } from './dark-style'
import {
  cacheRatio,
  dailyMetricValue,
  formatCompact,
  formatCurrency,
  formatNumber,
  formatPercent,
  formatShortDate,
  meterEstimatedCost,
  modelTokens,
  reportMarketEquivalent,
  reportReasoningTokens
} from './format'
import type {
  ActivityUsage,
  ModelUsage,
  PlatformUsage,
  RouteSort,
  UsageMeterBucket,
  UsageMetric,
  UsageReport
} from './types'

type MacroStripProps = {
  meter?: UsageMeterBucket
  meterUnavailable: boolean
  report: UsageReport
}

type StatCellProps = {
  icon: ReactNode
  label: string
  meta: string
  tone?: 'actual' | 'estimated' | 'neutral'
  value: string
}

function StatCell({ icon, label, meta, tone = 'neutral', value }: StatCellProps) {
  return (
    <div className="min-w-0 border-b border-border px-4 py-3 last:border-r-0 sm:border-r xl:border-b-0">
      <dt className="flex items-center gap-2 font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
        <span className="text-foreground/70">{icon}</span>
        {label}
      </dt>
      <dd
        className={cn(
          'mt-2 truncate font-mono text-xl font-semibold tabular-nums',
          tone === 'actual' && 'text-ui-green',
          tone === 'estimated' && 'text-ui-yellow'
        )}
      >
        {value}
      </dd>
      <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{meta}</p>
    </div>
  )
}

export function MacroStrip({ meter, meterUnavailable, report }: MacroStripProps) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard
  const sessionCacheRatio = cacheRatio(report.totals.cache_read_tokens, report.totals.input_tokens)
  const capturedCost = meter ? meterEstimatedCost(meter) : null
  const marketEquivalent = reportMarketEquivalent(report)

  return (
    <dl className="grid grid-cols-2 border-y border-border bg-card/20 sm:grid-cols-3 xl:grid-cols-6">
      <StatCell
        icon={<CreditCard className="size-3.5" />}
        label={u.macro.marketCost}
        meta={u.macro.rangeEstimate(report.period_days)}
        tone="estimated"
        value={formatCurrency(marketEquivalent, locale)}
      />
      <StatCell
        icon={<Archive className="size-3.5" />}
        label={u.macro.capturedCost}
        meta={
          meterUnavailable
            ? u.macro.captureUnavailable
            : meter
              ? u.macro.pricingCoverage(meter.priced_calls, meter.included_calls, meter.unpriced_calls)
              : u.loading
        }
        tone="estimated"
        value={formatCurrency(capturedCost, locale)}
      />
      <StatCell
        icon={<Hash className="size-3.5" />}
        label={u.macro.tokens}
        meta={u.macro.inputOutput(
          formatCompact(report.totals.input_tokens, locale),
          formatCompact(report.totals.output_tokens, locale)
        )}
        value={formatCompact(report.totals.total_tokens, locale)}
      />
      <StatCell
        icon={<Activity className="size-3.5" />}
        label={u.macro.calls}
        meta={u.macro.range(report.period_days)}
        value={formatNumber(report.totals.api_calls, locale)}
      />
      <StatCell
        icon={<Layers3 className="size-3.5" />}
        label={u.macro.sessions}
        meta={u.macro.range(report.period_days)}
        value={formatNumber(report.totals.sessions, locale)}
      />
      <StatCell
        icon={<Zap className="size-3.5" />}
        label={u.macro.cacheLeverage}
        meta={u.macro.cacheRead(formatCompact(report.totals.cache_read_tokens, locale))}
        value={formatPercent(sessionCacheRatio, locale)}
      />
    </dl>
  )
}

type SectionHeaderProps = {
  action?: ReactNode
  description: string
  eyebrow: string
  title: string
}

export function SectionHeader({ action, description, eyebrow, title }: SectionHeaderProps) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-ui-green">{eyebrow}</p>
        <h2 className="mt-1 text-sm font-semibold text-foreground">{title}</h2>
        <p className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">{description}</p>
      </div>
      {action}
    </div>
  )
}

function metricLabel(metric: UsageMetric, value: number | null, locale: string): string {
  if (metric === 'cost') {
    return formatCurrency(value, locale)
  }

  return formatNumber(value, locale)
}

function BurnChart({ metric, report }: { metric: UsageMetric; report: UsageReport }) {
  const { locale, t } = useI18n()
  const { themeName } = useTheme()
  const u = t.usageDashboard
  const darkStyle = useMemo(() => usageDarkStyle(themeName), [themeName])
  const values = report.days.map(day => dailyMetricValue(day, metric))
  const availableValues = values.filter((value): value is number => value != null)
  const complete = availableValues.length === values.length
  const max = Math.max(1, ...availableValues)
  const total = complete ? availableValues.reduce((sum, value) => sum + value, 0) : null
  const width = 760
  const height = 218
  const chartTop = 14
  const chartBottom = 176
  const chartHeight = chartBottom - chartTop
  const slot = width / Math.max(report.days.length, 1)
  const barWidth = Math.max(2, Math.min(18, slot * 0.62))
  let cumulative = 0

  const points = values.map((value, index) => {
    cumulative += value ?? 0
    const x = slot * index + slot / 2
    const y = chartBottom - (cumulative / Math.max(total ?? 0, 1)) * chartHeight

    return `${x},${y}`
  })

  const labelEvery = Math.max(1, Math.ceil(report.days.length / 6))

  if (!report.days.length) {
    return <div className="dither grid h-56 place-items-center text-xs text-muted-foreground">{u.emptyDaily}</div>
  }

  return (
    <div className="min-w-0">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <p className="font-mono text-2xl font-semibold tabular-nums text-foreground">
            {metricLabel(metric, total, locale)}
          </p>
          <p className="text-[11px] text-muted-foreground">{u.chart.periodTotal(report.period_days)}</p>
        </div>
        <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
          {u.chart.cumulativeTrace}
        </p>
      </div>
      <svg
        aria-label={u.chart.aria(metric, report.period_days)}
        className="h-48 w-full overflow-visible"
        preserveAspectRatio="none"
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        {[0, 0.25, 0.5, 0.75, 1].map(mark => {
          const y = chartBottom - chartHeight * mark

          return (
            <line
              key={mark}
              opacity={mark === 0 ? 0.7 : 0.28}
              stroke="var(--ui-stroke-secondary)"
              strokeDasharray={mark === 0 ? undefined : '2 5'}
              x1="0"
              x2={width}
              y1={y}
              y2={y}
            />
          )
        })}
        {complete && (
          <polyline
            fill="none"
            opacity="0.62"
            points={points.join(' ')}
            stroke="var(--ui-text-tertiary)"
            strokeDasharray="3 5"
            strokeWidth="1.25"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {report.days.map((day, index) => {
          const value = values[index]
          const barHeight = value != null && value > 0 ? Math.max(1, (value / max) * chartHeight) : 0
          const x = slot * index + (slot - barWidth) / 2

          const tone =
            metric === 'cost' ? 'var(--ui-yellow)' : metric === 'sessions' ? 'var(--ui-cyan)' : 'var(--ui-green)'

          const tooltip = `${formatShortDate(day.date, locale)} · ${metricLabel(metric, value, locale)}`

          return (
            <g key={day.date}>
              <Tip delayDuration={0} label={tooltip} style={darkStyle}>
                <rect
                  aria-label={tooltip}
                  className="usage-chart-bar"
                  fill={tone}
                  height={barHeight}
                  opacity={value == null ? 0.08 : value > 0 ? 0.86 : 0.18}
                  role="img"
                  tabIndex={0}
                  width={barWidth}
                  x={x}
                  y={chartBottom - barHeight}
                />
              </Tip>
              {index % labelEvery === 0 && (
                <text
                  fill="var(--ui-text-tertiary)"
                  fontFamily="var(--font-mono)"
                  fontSize="9"
                  textAnchor="middle"
                  x={slot * index + slot / 2}
                  y={205}
                >
                  {formatShortDate(day.date, locale)}
                </text>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}

function TokenTopology({ report }: { report: UsageReport }) {
  const { locale, t } = useI18n()
  const { themeName } = useTheme()
  const u = t.usageDashboard
  const darkStyle = useMemo(() => usageDarkStyle(themeName), [themeName])

  const canonicalItems = [
    { label: u.token.input, tone: 'input', value: report.totals.input_tokens },
    { label: u.token.cacheRead, tone: 'cache-read', value: report.totals.cache_read_tokens },
    { label: u.token.cacheWrite, tone: 'cache-write', value: report.totals.cache_write_tokens },
    { label: u.token.output, tone: 'output', value: report.totals.output_tokens }
  ]

  const canonicalTotal = report.totals.total_tokens
  const reasoningItem = { label: u.token.reasoning, tone: 'reasoning', value: reportReasoningTokens(report) }

  const canonicalShare = (value: number | null) =>
    value != null && canonicalTotal != null && canonicalTotal > 0 ? value / canonicalTotal : null

  const reasoningShare =
    reasoningItem.value != null && report.totals.output_tokens != null && report.totals.output_tokens > 0
      ? reasoningItem.value / report.totals.output_tokens
      : null

  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden border border-border bg-background">
        {canonicalItems.map(item => {
          const label = `${item.label}: ${formatNumber(item.value, locale)} (${formatPercent(canonicalShare(item.value), locale)})`

          return (
            <Tip delayDuration={0} key={item.tone} label={label} style={darkStyle}>
              <div
                aria-label={label}
                className="usage-token-segment h-full"
                data-tone={item.tone}
                role="img"
                style={{ width: `${(canonicalShare(item.value) ?? 0) * 100}%` }}
                tabIndex={0}
              />
            </Tip>
          )
        })}
      </div>
      <div className="mt-4 space-y-3">
        {[...canonicalItems, reasoningItem].map(item => (
          <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3" key={item.tone}>
            <div className="flex min-w-0 items-center gap-2">
              <span className="usage-token-segment size-2 shrink-0" data-tone={item.tone} />
              <span className="truncate text-xs text-foreground">{item.label}</span>
            </div>
            <span className="font-mono text-xs tabular-nums text-foreground">{formatCompact(item.value, locale)}</span>
            <span className="w-24 text-end font-mono text-[10px] tabular-nums text-muted-foreground">
              {item.tone === 'reasoning'
                ? u.token.reasoningShare(formatPercent(reasoningShare, locale))
                : formatPercent(canonicalShare(item.value), locale)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function CostTruth({ meter, meterUnavailable, report }: MacroStripProps) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard
  const marketEquivalent = reportMarketEquivalent(report)

  if (meterUnavailable) {
    return (
      <div className="dither border-y border-border px-4 py-8 text-xs text-muted-foreground">
        {u.cost.captureUnavailable}
      </div>
    )
  }

  if (!meter) {
    return <div className="h-40 animate-pulse border-y border-border bg-card/30" />
  }

  const rows = [
    {
      calls: meter.priced_calls,
      label: u.cost.estimated,
      tone: 'estimated' as const,
      value: meter.priced_calls > 0 ? meter.estimated_cost_usd : null
    },
    {
      calls: meter.included_calls,
      label: u.cost.included,
      tone: 'included' as const,
      value: meter.included_calls > 0 ? 0 : null
    },
    {
      calls: meter.unpriced_calls,
      label: u.cost.unavailable,
      tone: 'unavailable' as const,
      value: null
    }
  ]

  return (
    <div>
      <div className="mb-4 flex items-baseline justify-between border-b border-border pb-3">
        <div>
          <p className="font-mono text-2xl font-semibold tabular-nums text-foreground">
            {formatCurrency(meterEstimatedCost(meter), locale)}
          </p>
          <p className="text-[11px] text-muted-foreground">{u.cost.capturedAllTime}</p>
        </div>
        <div className="text-end">
          <p className="font-mono text-sm tabular-nums text-ui-cyan">
            {formatCurrency(report.totals.actual_cost, locale)}
          </p>
          <p className="text-[10px] text-muted-foreground">{u.cost.actual}</p>
        </div>
      </div>
      <div className="divide-y divide-border border-y border-border">
        {rows.map(row => (
          <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 py-2.5" key={row.tone}>
            <div className="flex items-center gap-2">
              <span className="usage-status font-mono text-[10px]" data-tone={row.tone}>
                ●
              </span>
              <span className="text-xs text-foreground">{row.label}</span>
            </div>
            <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
              {u.cost.calls(formatNumber(row.calls, locale))}
            </span>
            <span className="w-20 text-end font-mono text-xs tabular-nums text-foreground">
              {formatCurrency(row.value, locale)}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-3 text-[10px] leading-4 text-muted-foreground">
        {u.cost.rangeComparison(formatCurrency(marketEquivalent, locale), report.period_days)}
      </p>
    </div>
  )
}

function modelSortValue(model: ModelUsage, sort: RouteSort): number {
  if (sort === 'cost') {
    return model.actual_cost ?? model.estimated_cost ?? -1
  }

  if (sort === 'calls') {
    return model.api_calls ?? -1
  }

  if (sort === 'cache') {
    return cacheRatio(model.cache_read_tokens, model.input_tokens) ?? 0
  }

  return modelTokens(model) ?? -1
}

function ModelTable({ report }: { report: UsageReport }) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard
  const [sort, setSort] = useState<RouteSort>('cost')

  const models = useMemo(
    () => [...report.models].sort((a, b) => modelSortValue(b, sort) - modelSortValue(a, sort)),
    [report.models, sort]
  )

  return (
    <div>
      <SectionHeader
        action={
          <div aria-label={u.sort.aria} role="group">
            <SegmentedControl
              onChange={value => setSort(value as RouteSort)}
              options={[
                { id: 'cost', label: u.sort.cost },
                { id: 'tokens', label: u.sort.tokens },
                { id: 'calls', label: u.sort.calls },
                { id: 'cache', label: u.sort.cache }
              ]}
              value={sort}
            />
          </div>
        }
        description={u.models.description}
        eyebrow={u.sections.model}
        title={u.models.title}
      />
      {models.length ? (
        <div className="usage-table-scroll overflow-x-auto border-y border-border">
          <table className="w-full min-w-[780px] text-start text-xs">
            <thead className="bg-card/50 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">{u.table.model}</th>
                <th className="px-3 py-2 text-end font-medium">{u.table.calls}</th>
                <th className="px-3 py-2 text-end font-medium">{u.table.input}</th>
                <th className="px-3 py-2 text-end font-medium">{u.table.cacheRead}</th>
                <th className="px-3 py-2 text-end font-medium">{u.table.output}</th>
                <th className="px-3 py-2 text-end font-medium">{u.table.reasoning}</th>
                <th className="px-3 py-2 text-end font-medium">{u.table.cost}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {models.map(model => (
                <tr className="hover:bg-hover" key={model.model}>
                  <td className="max-w-72 px-3 py-2.5">
                    <p className="truncate font-medium text-foreground">{model.model}</p>
                    <p className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">
                      {formatNumber(model.sessions, locale)} {u.macro.sessions}
                    </p>
                  </td>
                  <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                    {formatNumber(model.api_calls, locale)}
                  </td>
                  <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                    {formatCompact(model.input_tokens, locale)}
                  </td>
                  <td className="px-3 py-2.5 text-end font-mono tabular-nums text-ui-cyan">
                    {formatCompact(model.cache_read_tokens, locale)}
                    <span className="ms-1 text-[9px] text-muted-foreground">
                      {formatPercent(cacheRatio(model.cache_read_tokens, model.input_tokens), locale)}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                    {formatCompact(model.output_tokens, locale)}
                  </td>
                  <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                    {formatCompact(model.reasoning_tokens, locale)}
                  </td>
                  <td className="px-3 py-2.5 text-end">
                    <p className="font-mono tabular-nums text-foreground">
                      {formatCurrency(model.actual_cost ?? model.estimated_cost, locale)}
                    </p>
                    <p
                      className="usage-status mt-0.5 font-mono text-[9px] uppercase"
                      data-tone={
                        model.actual_cost != null || model.cost_status === 'actual'
                          ? 'actual'
                          : model.cost_status === 'estimated'
                            ? 'estimated'
                            : 'unavailable'
                      }
                    >
                      {model.actual_cost != null
                        ? u.costStatus.actual
                        : (u.costStatus[model.cost_status] ?? model.cost_status)}
                    </p>
                    {model.actual_cost != null && model.estimated_cost != null && (
                      <p className="mt-0.5 font-mono text-[9px] tabular-nums text-muted-foreground">
                        {u.cost.estimatedValue(formatCurrency(model.estimated_cost, locale))}
                      </p>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="dither border-y border-border px-4 py-10 text-center text-xs text-muted-foreground">
          {u.models.empty}
        </div>
      )}
    </div>
  )
}

function PlatformBars({ platforms }: { platforms: PlatformUsage[] }) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard
  const maxTokens = Math.max(1, ...platforms.map(platform => platform.total_tokens))

  if (!platforms.length) {
    return <p className="text-xs text-muted-foreground">{u.platform.empty}</p>
  }

  return (
    <div className="space-y-3">
      {platforms.slice(0, 8).map(platform => (
        <div key={platform.platform}>
          <div className="mb-1.5 flex items-center justify-between gap-3">
            <span className="truncate text-xs font-medium text-foreground">{platform.platform}</span>
            <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
              {formatCompact(platform.total_tokens, locale)} · {formatNumber(platform.sessions, locale)}{' '}
              {u.macro.sessions}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden bg-muted">
            <div
              className="usage-share-bar h-full bg-ui-green"
              style={{ width: `${(platform.total_tokens / maxTokens) * 100}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function ActivityHeatmap({ activity }: { activity: ActivityUsage[] }) {
  const { locale, t } = useI18n()
  const { themeName } = useTheme()
  const u = t.usageDashboard
  const darkStyle = useMemo(() => usageDarkStyle(themeName), [themeName])
  const byHour = new Map(activity.map(entry => [entry.hour, entry]))
  const cells = Array.from({ length: 24 }, (_, hour) => byHour.get(hour) ?? { hour, sessions: 0 })
  const maxSessions = Math.max(1, ...cells.map(entry => entry.sessions))
  const peak = cells.reduce((best, entry) => (entry.sessions > best.sessions ? entry : best), cells[0])

  return (
    <div>
      <div
        aria-label={u.activity.aria}
        className="grid grid-cols-12 gap-1 sm:grid-cols-[repeat(24,minmax(0,1fr))]"
        role="list"
      >
        {cells.map(entry => {
          const level = entry.sessions > 0 ? Math.max(1, Math.ceil((entry.sessions / maxSessions) * 4)) : 0

          const label = u.activity.cell(entry.hour, entry.sessions)

          return (
            <Tip delayDuration={0} key={entry.hour} label={label} style={darkStyle}>
              <div
                aria-label={label}
                className="usage-heat-cell aspect-square min-h-3"
                data-level={level}
                role="listitem"
              />
            </Tip>
          )
        })}
      </div>
      <div className="mt-2 flex justify-between font-mono text-[9px] text-muted-foreground">
        <span>00</span>
        <span>06</span>
        <span>12</span>
        <span>18</span>
        <span>23</span>
      </div>
      <p className="mt-3 text-[11px] text-muted-foreground">
        {u.activity.peak(String(peak.hour).padStart(2, '0'), formatNumber(peak.sessions, locale))}
      </p>
    </div>
  )
}

function SessionHotspots({ report }: { report: UsageReport }) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard

  const localizedLabel = (label: string) => {
    if (label === 'Longest session') {
      return u.sessions.labels.longest
    }

    if (label === 'Most messages') {
      return u.sessions.labels.messages
    }

    if (label === 'Most tokens') {
      return u.sessions.labels.tokens
    }

    if (label === 'Most tool calls') {
      return u.sessions.labels.tools
    }

    return label
  }

  const localizedValue = (label: string, value: string) => {
    const numeric = Number(value.split(' ')[0].replaceAll(',', ''))

    if (Number.isFinite(numeric)) {
      const count = formatNumber(numeric, locale)

      if (label === 'Most messages') {
        return u.sessions.messages(count)
      }

      if (label === 'Most tokens') {
        return u.sessions.tokens(count)
      }

      if (label === 'Most tool calls') {
        return u.sessions.calls(count)
      }
    }

    if (label === 'Longest session') {
      const matches = [...value.matchAll(/(\d+(?:\.\d+)?)([smhd])/g)]

      if (matches.length && matches.map(match => match[0]).join('') === value.replaceAll(' ', '')) {
        return matches
          .map(match => {
            const count = formatNumber(Number(match[1]), locale)

            if (match[2] === 's') {
              return u.sessions.duration.seconds(count)
            }

            if (match[2] === 'm') {
              return u.sessions.duration.minutes(count)
            }

            if (match[2] === 'h') {
              return u.sessions.duration.hours(count)
            }

            return u.sessions.duration.days(count)
          })
          .join(' ')
      }
    }

    return value
  }

  if (!report.top_sessions.length) {
    return <p className="text-xs text-muted-foreground">{u.sessions.empty}</p>
  }

  return (
    <div className="divide-y divide-border border-y border-border">
      {report.top_sessions.slice(0, 5).map(session => (
        <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-4 py-2.5" key={session.session_id}>
          <div className="min-w-0">
            <p className="truncate text-xs font-medium text-foreground">{localizedLabel(session.label)}</p>
            <p className="mt-0.5 truncate font-mono text-[9px] text-muted-foreground">{session.session_id}</p>
          </div>
          <div className="text-end">
            <p className="font-mono text-xs tabular-nums text-foreground">
              {localizedValue(session.label, session.value)}
            </p>
            <p className="font-mono text-[9px] tabular-nums text-muted-foreground">
              {formatShortDate(session.date, locale)}
            </p>
          </div>
        </div>
      ))}
    </div>
  )
}

function WorkloadSignals({ report }: { report: UsageReport }) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard

  const items = [
    ...report.skills.slice(0, 4).map(item => ({ ...item, kind: u.workload.skill })),
    ...report.tools.slice(0, 4).map(item => ({ ...item, kind: u.workload.tool }))
  ]
    .sort((a, b) => b.count - a.count)
    .slice(0, 6)

  if (!items.length) {
    return <p className="text-xs text-muted-foreground">{u.workload.empty}</p>
  }

  const max = Math.max(1, ...items.map(item => item.count))

  return (
    <div className="space-y-2.5">
      {items.map(item => (
        <div className="grid grid-cols-[minmax(0,1fr)_5rem_auto] items-center gap-3" key={`${item.kind}:${item.name}`}>
          <div className="min-w-0">
            <p className="truncate text-xs text-foreground">{item.name}</p>
            <p className="font-mono text-[9px] uppercase text-muted-foreground">{item.kind}</p>
          </div>
          <div className="h-1 overflow-hidden bg-muted">
            <div className="usage-share-bar h-full bg-ui-cyan" style={{ width: `${(item.count / max) * 100}%` }} />
          </div>
          <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
            {formatNumber(item.count, locale)}
          </span>
        </div>
      ))}
      <p className="pt-1 text-[10px] leading-4 text-muted-foreground">{u.workload.disclaimer}</p>
    </div>
  )
}

type OverviewDeckProps = MacroStripProps & {
  metric: UsageMetric
  onMetricChange: (metric: UsageMetric) => void
}

export function OverviewDeck({ meter, meterUnavailable, metric, onMetricChange, report }: OverviewDeckProps) {
  const { t } = useI18n()
  const u = t.usageDashboard

  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.65fr)_minmax(17rem,0.75fr)]">
        <section className="usage-rail min-w-0 border-y border-border py-3">
          <SectionHeader
            action={
              <div aria-label={u.chart.metricAria} role="group">
                <SegmentedControl
                  onChange={value => onMetricChange(value as UsageMetric)}
                  options={[
                    { id: 'cost', label: u.chart.cost },
                    { id: 'tokens', label: u.chart.tokens },
                    { id: 'sessions', label: u.chart.calls }
                  ]}
                  value={metric}
                />
              </div>
            }
            description={u.chart.description}
            eyebrow={u.sections.burn}
            title={u.chart.title}
          />
          <BurnChart metric={metric} report={report} />
        </section>

        <section className="usage-rail border-y border-border py-3">
          <SectionHeader description={u.token.description} eyebrow={u.sections.token} title={u.token.title} />
          <TokenTopology report={report} />
          <div className="mt-4 border-t border-border pt-3">
            <div className="mb-2 flex items-center gap-2">
              <Activity className="size-3.5 text-ui-green" />
              <h3 className="font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-foreground">
                {u.activity.title}
              </h3>
            </div>
            <ActivityHeatmap activity={report.activity} />
          </div>
        </section>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="usage-rail border-y border-border py-3">
          <SectionHeader description={u.cost.description} eyebrow={u.sections.cost} title={u.cost.title} />
          <CostTruth meter={meter} meterUnavailable={meterUnavailable} report={report} />
        </section>
        <section className="usage-rail border-y border-border py-3">
          <SectionHeader description={u.platform.description} eyebrow={u.sections.platform} title={u.platform.title} />
          <PlatformBars platforms={report.platforms} />
        </section>
      </div>

      <ModelTable report={report} />

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="usage-rail border-y border-border py-3">
          <SectionHeader description={u.sessions.description} eyebrow={u.sections.sessions} title={u.sessions.title} />
          <SessionHotspots report={report} />
        </section>
        <section className="usage-rail border-y border-border py-3">
          <SectionHeader description={u.workload.description} eyebrow={u.sections.workload} title={u.workload.title} />
          <WorkloadSignals report={report} />
        </section>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <GitFork className="size-3" /> {u.footer.sessionInsights}
        </span>
        <span aria-hidden="true">//</span>
        <span className="flex items-center gap-1.5">
          <Terminal className="size-3" /> {u.footer.installLedger}
        </span>
        <span aria-hidden="true">//</span>
        <span className="flex items-center gap-1.5">
          <Cpu className="size-3" /> {u.footer.localData}
        </span>
      </div>
    </div>
  )
}
