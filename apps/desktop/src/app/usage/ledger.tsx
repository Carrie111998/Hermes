import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { RowButton } from '@/components/ui/row-button'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n/context'
import { AlertTriangle, ChevronRight, Search, X } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { UsageSelect } from './controls'
import {
  eventKey,
  eventTokens,
  formatCompact,
  formatCurrency,
  formatNumber,
  formatTimestamp,
  uniqueValues
} from './format'
import { SectionHeader } from './overview'
import type { RouteDrilldown } from './routes'
import type { UsageMeterEvent, UsageMeterRecent } from './types'

type LedgerDeckProps = {
  drilldown: RouteDrilldown | null
  error: boolean
  isFetching: boolean
  limit: number
  onClearDrilldown: () => void
  onLimitChange: (limit: number) => void
  recent?: UsageMeterRecent
}

type LedgerFilters = {
  apiMode: string
  costStatus: string
  model: string
  platform: string
  profile: string
  provider: string
  query: string
}

const DEFAULT_FILTERS: LedgerFilters = {
  apiMode: 'all',
  costStatus: 'all',
  model: 'all',
  platform: 'all',
  profile: 'all',
  provider: 'all',
  query: ''
}

function eventCostTone(event: UsageMeterEvent): 'estimated' | 'included' | 'unavailable' {
  if (event.pricing_status === 'included') {
    return 'included'
  }

  if (event.pricing_status === 'unknown' || event.pricing_status === 'unpriced') {
    return 'unavailable'
  }

  return 'estimated'
}

function DetailCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 border-t border-border pt-2">
      <dt className="font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-all font-mono text-[11px] tabular-nums text-foreground">{value}</dd>
    </div>
  )
}

function EventDisclosure({
  event,
  eventId,
  expanded,
  onToggle
}: {
  event: UsageMeterEvent
  eventId: string
  expanded: boolean
  onToggle: () => void
}) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard
  const tone = eventCostTone(event)

  return (
    <article className="border-b border-border last:border-b-0">
      <RowButton
        aria-controls={`${eventId}-details`}
        aria-expanded={expanded}
        className="usage-disclosure grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 border-s border-transparent px-3 py-3 text-start sm:grid-cols-[auto_minmax(11rem,1.4fr)_minmax(7rem,0.7fr)_minmax(7rem,0.7fr)_auto_auto]"
        onClick={onToggle}
      >
        <ChevronRight className={cn('size-3.5 text-muted-foreground transition-transform', expanded && 'rotate-90')} />
        <div className="min-w-0">
          <p className="truncate text-xs font-medium text-foreground">{event.model || u.unknown}</p>
          <p className="mt-0.5 truncate font-mono text-[9px] text-muted-foreground">
            {event.provider || u.unknown} · {event.api_mode || u.unknown}
          </p>
        </div>
        <div className="hidden min-w-0 sm:block">
          <p className="truncate font-mono text-[10px] text-foreground">{event.profile || u.unknown}</p>
          <p className="mt-0.5 truncate font-mono text-[9px] text-muted-foreground">{event.platform || u.unknown}</p>
        </div>
        <div className="hidden min-w-0 sm:block">
          <p className="font-mono text-[10px] tabular-nums text-foreground">
            {formatCompact(eventTokens(event), locale)}
          </p>
          <p className="mt-0.5 font-mono text-[9px] text-muted-foreground">{u.token.tokensShort}</p>
        </div>
        <div className="text-end">
          <p className="font-mono text-xs tabular-nums text-foreground">
            {formatCurrency(tone === 'unavailable' ? null : event.estimated_cost_usd, locale)}
          </p>
          <p className="usage-status mt-0.5 font-mono text-[9px] uppercase" data-tone={tone}>
            {u.costStatus[tone]}
          </p>
        </div>
        <time
          className="hidden whitespace-nowrap text-end font-mono text-[9px] tabular-nums text-muted-foreground sm:block"
          dateTime={new Date(event.ts * 1000).toISOString()}
        >
          {formatTimestamp(event.ts, locale)}
        </time>
      </RowButton>

      {expanded && (
        <div className="border-s border-ui-green bg-card/20 px-7 py-4" id={`${eventId}-details`}>
          <dl className="grid gap-x-5 gap-y-3 sm:grid-cols-2 lg:grid-cols-5">
            <DetailCell label={u.table.input} value={formatNumber(event.input_tokens, locale)} />
            <DetailCell label={u.table.cacheRead} value={formatNumber(event.cache_read_tokens, locale)} />
            <DetailCell label={u.table.cacheWrite} value={formatNumber(event.cache_write_tokens, locale)} />
            <DetailCell label={u.table.output} value={formatNumber(event.output_tokens, locale)} />
            <DetailCell label={u.table.reasoning} value={formatNumber(event.reasoning_tokens, locale)} />
            <DetailCell label={u.ledger.sessionId} value={event.session_id || u.unknown} />
            <DetailCell label={u.ledger.turnId} value={event.task_id || u.unknown} />
            <DetailCell label={u.ledger.eventId} value={String(event.id)} />
            <DetailCell label={u.ledger.costSource} value={event.pricing_source || u.unknown} />
            <DetailCell label={u.ledger.timestamp} value={formatTimestamp(event.ts, locale)} />
          </dl>
        </div>
      )}
    </article>
  )
}

export function LedgerDeck({
  drilldown,
  error,
  isFetching,
  limit,
  onClearDrilldown,
  onLimitChange,
  recent
}: LedgerDeckProps) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard
  const [filters, setFilters] = useState<LedgerFilters>(DEFAULT_FILTERS)
  const [expanded, setExpanded] = useState<string | null>(null)
  const events = useMemo(() => recent?.events ?? [], [recent?.events])

  useEffect(() => {
    if (!drilldown) {
      return
    }

    setFilters(current => ({
      ...current,
      apiMode: drilldown.apiMode || 'all',
      model: drilldown.model || 'all',
      provider: drilldown.provider || 'all'
    }))
  }, [drilldown])

  const options = useMemo(
    () => ({
      apiModes: uniqueValues(events.map(event => event.api_mode)),
      costStatuses: uniqueValues(events.map(event => event.pricing_status)),
      models: uniqueValues(events.map(event => event.model)),
      platforms: uniqueValues(events.map(event => event.platform)),
      profiles: uniqueValues(events.map(event => event.profile)),
      providers: uniqueValues(events.map(event => event.provider))
    }),
    [events]
  )

  const visibleEvents = useMemo(() => {
    const needle = filters.query.trim().toLowerCase()

    return events
      .filter(
        event =>
          drilldown?.scope !== 'month' ||
          drilldown.startTs == null ||
          drilldown.endTs == null ||
          (event.ts >= drilldown.startTs && event.ts < drilldown.endTs)
      )
      .filter(event => filters.provider === 'all' || event.provider === filters.provider)
      .filter(event => filters.model === 'all' || event.model === filters.model)
      .filter(event => filters.apiMode === 'all' || event.api_mode === filters.apiMode)
      .filter(event => filters.platform === 'all' || event.platform === filters.platform)
      .filter(event => filters.profile === 'all' || event.profile === filters.profile)
      .filter(event => filters.costStatus === 'all' || event.pricing_status === filters.costStatus)
      .filter(event => {
        if (!needle) {
          return true
        }

        return [
          String(event.id),
          event.profile,
          event.session_id,
          event.task_id,
          event.provider,
          event.model,
          event.api_mode,
          event.platform,
          event.pricing_source
        ].some(value => value.toLowerCase().includes(needle))
      })
  }, [drilldown, events, filters])

  const activeFilterCount =
    Object.entries(filters).filter(([key, value]) => (key === 'query' ? Boolean(value) : value !== 'all')).length +
    (drilldown?.scope === 'month' ? 1 : 0)

  const updateFilter = <K extends keyof LedgerFilters>(key: K, value: LedgerFilters[K]) => {
    setFilters(current => ({ ...current, [key]: value }))
    setExpanded(null)
  }

  return (
    <div className="space-y-4">
      <section className="usage-rail border-y border-border py-3">
        <SectionHeader
          action={
            <div aria-label={u.ledger.limitAria} role="group">
              <SegmentedControl
                onChange={value => onLimitChange(Number(value))}
                options={[50, 100, 250, 500].map(value => ({ id: String(value), label: String(value) }))}
                value={String(limit)}
              />
            </div>
          }
          description={u.ledger.description}
          eyebrow={u.sections.ledger}
          title={u.ledger.title}
        />

        {error && (
          <div className="mb-4 flex items-start gap-2 border-y border-ui-red/40 bg-ui-red/5 px-3 py-2 text-xs text-ui-red">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{u.ledger.loadFailed}</span>
          </div>
        )}

        {drilldown?.scope === 'month' && (
          <div className="mb-4 flex items-start gap-2 border-y border-ui-yellow/40 bg-ui-yellow/5 px-3 py-2 text-xs text-ui-yellow">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{u.ledger.scopeNotice}</span>
          </div>
        )}

        <div className="mb-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
          <Input
            aria-label={u.filters.searchLedgerAria}
            className="sm:col-span-2"
            onChange={event => updateFilter('query', event.target.value)}
            placeholder={u.filters.searchLedger}
            prefix={<Search className="size-3.5" />}
            size="sm"
            value={filters.query}
          />
          <UsageSelect
            label={u.filters.profile}
            onChange={value => updateFilter('profile', value)}
            options={[
              { label: u.filters.allProfiles, value: 'all' },
              ...options.profiles.map(value => ({ label: value, value }))
            ]}
            value={filters.profile}
          />
          <UsageSelect
            label={u.filters.provider}
            onChange={value => updateFilter('provider', value)}
            options={[
              { label: u.filters.allProviders, value: 'all' },
              ...options.providers.map(value => ({ label: value, value }))
            ]}
            value={filters.provider}
          />
          <UsageSelect
            label={u.filters.model}
            onChange={value => updateFilter('model', value)}
            options={[
              { label: u.filters.allModels, value: 'all' },
              ...options.models.map(value => ({ label: value, value }))
            ]}
            value={filters.model}
          />
          <UsageSelect
            label={u.filters.apiMode}
            onChange={value => updateFilter('apiMode', value)}
            options={[
              { label: u.filters.allModes, value: 'all' },
              ...options.apiModes.map(value => ({ label: value, value }))
            ]}
            value={filters.apiMode}
          />
          <UsageSelect
            label={u.filters.platform}
            onChange={value => updateFilter('platform', value)}
            options={[
              { label: u.filters.allPlatforms, value: 'all' },
              ...options.platforms.map(value => ({ label: value, value }))
            ]}
            value={filters.platform}
          />
          <UsageSelect
            label={u.filters.costStatus}
            onChange={value => updateFilter('costStatus', value)}
            options={[
              { label: u.filters.allCostStates, value: 'all' },
              ...options.costStatuses.map(value => ({ label: u.costStatus[value] ?? value, value }))
            ]}
            value={filters.costStatus}
          />
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-3 border-y border-border bg-card/20 px-3 py-2 font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">
          <span>
            {u.ledger.visible(formatNumber(visibleEvents.length, locale), formatNumber(events.length, locale))}
          </span>
          <span aria-hidden="true">//</span>
          <span>{u.ledger.filterCount(formatNumber(activeFilterCount, locale))}</span>
          {isFetching && <span className="text-ui-green">{u.syncing}</span>}
          {activeFilterCount > 0 && (
            <Button
              className="ms-auto"
              onClick={() => {
                setFilters(DEFAULT_FILTERS)
                onClearDrilldown()
              }}
              size="xs"
              variant="ghost"
            >
              <X className="size-3.5" />
              {u.filters.clear}
            </Button>
          )}
        </div>

        {visibleEvents.length ? (
          <div className="border-y border-border">
            <div className="hidden grid-cols-[auto_minmax(11rem,1.4fr)_minmax(7rem,0.7fr)_minmax(7rem,0.7fr)_auto_auto] gap-3 border-b border-border bg-card/50 px-3 py-2 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground sm:grid">
              <span className="w-3.5" />
              <span>{u.table.route}</span>
              <span>{u.table.profile}</span>
              <span>{u.table.tokens}</span>
              <span className="text-end">{u.table.cost}</span>
              <span className="text-end">{u.table.time}</span>
            </div>
            {visibleEvents.map((event, index) => {
              const id = eventKey(event, index)

              return (
                <EventDisclosure
                  event={event}
                  eventId={`usage-event-${index}`}
                  expanded={expanded === id}
                  key={id}
                  onToggle={() => setExpanded(current => (current === id ? null : id))}
                />
              )
            })}
          </div>
        ) : (
          <div className="dither grid min-h-52 place-items-center border-y border-border px-4 py-10 text-center text-xs text-muted-foreground">
            {events.length ? u.ledger.noMatch : u.ledger.empty}
          </div>
        )}
      </section>

      <p className="max-w-3xl text-[10px] leading-4 text-muted-foreground">{u.ledger.disclaimer}</p>
    </div>
  )
}
