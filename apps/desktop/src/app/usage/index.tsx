import './usage.css'

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { PAGE_INSET_X, PAGE_MAX_W } from '@/app/layout-constants'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n/context'
import { triggerHaptic } from '@/lib/haptics'
import { AlertTriangle, RefreshCw } from '@/lib/icons'
import { useTheme } from '@/themes/context'

import type { GatewayRequester } from '../contrib/types'

import { usageDarkStyle } from './dark-style'
import { formatTimestamp } from './format'
import { LedgerDeck } from './ledger'
import { normalizeUsageOverview } from './normalize'
import { MacroStrip, OverviewDeck } from './overview'
import { type RouteDrilldown, RouteMatrix } from './routes'
import type {
  MeterScope,
  UsageDeck,
  UsageMeterBucket,
  UsageMeterDetails,
  UsageMeterRecent,
  UsageMeterSummaryResponse,
  UsageMetric,
  UsageOverviewResponse
} from './types'

type UsageViewProps = {
  requestGateway: GatewayRequester
}

function LoadingDeck({ label }: { label: string }) {
  return (
    <div aria-label={label} className="space-y-8" role="status">
      <div className="grid grid-cols-2 border-y border-border sm:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }, (_, index) => (
          <div className="border-b border-border px-4 py-4 sm:border-r xl:border-b-0" key={index}>
            <div className="h-2.5 w-20 animate-pulse bg-muted" />
            <div className="mt-4 h-6 w-24 animate-pulse bg-muted" />
            <div className="mt-2 h-2 w-16 animate-pulse bg-muted" />
          </div>
        ))}
      </div>
      <div className="grid gap-8 xl:grid-cols-[minmax(0,1.65fr)_minmax(17rem,0.75fr)]">
        <div className="h-80 animate-pulse border-y border-border bg-card/20" />
        <div className="h-80 animate-pulse border-y border-border bg-card/20" />
      </div>
    </div>
  )
}

export function UsageView({ requestGateway }: UsageViewProps) {
  const { locale, t } = useI18n()
  const { themeName } = useTheme()
  const u = t.usageDashboard
  const darkStyle = useMemo(() => usageDarkStyle(themeName), [themeName])
  const [days, setDays] = useState(30)
  const [deck, setDeck] = useState<UsageDeck>('overview')
  const [metric, setMetric] = useState<UsageMetric>('cost')
  const [meterScope, setMeterScope] = useState<MeterScope>('all')
  const [recentLimit, setRecentLimit] = useState(100)
  const [drilldown, setDrilldown] = useState<RouteDrilldown | null>(null)

  const overviewQuery = useQuery({
    queryFn: () => requestGateway<UsageOverviewResponse>('usage.overview', { days }),
    queryKey: ['usage', 'overview', days],
    select: normalizeUsageOverview,
    staleTime: 30_000
  })

  const meterSummaryQuery = useQuery<UsageMeterSummaryResponse, Error, UsageMeterBucket>({
    queryFn: () => requestGateway<UsageMeterSummaryResponse>('usage.meter.summary', {}),
    queryKey: ['usage', 'meter', 'summary', 'all'],
    retry: false,
    select: response => response.all_time.summary,
    staleTime: 30_000
  })

  const meterDetailsQuery = useQuery({
    enabled: deck === 'routes',
    queryFn: () => requestGateway<UsageMeterDetails>('usage.meter.details', { scope: meterScope }),
    queryKey: ['usage', 'meter', 'details', meterScope],
    retry: false,
    staleTime: 30_000
  })

  const meterRecentQuery = useQuery({
    enabled: deck === 'ledger',
    queryFn: () => requestGateway<UsageMeterRecent>('usage.meter.recent', { limit: recentLimit }),
    queryKey: ['usage', 'meter', 'recent', recentLimit],
    retry: false,
    staleTime: 15_000
  })

  const isFetching =
    overviewQuery.isFetching ||
    meterSummaryQuery.isFetching ||
    meterDetailsQuery.isFetching ||
    meterRecentQuery.isFetching

  const meterDegraded =
    meterSummaryQuery.isError ||
    (deck === 'routes' && meterDetailsQuery.isError) ||
    (deck === 'ledger' && meterRecentQuery.isError)

  const meterHasEvents = (meterSummaryQuery.data?.calls ?? 0) > 0
  const statusTone = meterDegraded ? 'error' : meterHasEvents ? 'actual' : 'neutral'
  const statusLabel = meterDegraded ? u.status.degraded : meterHasEvents ? u.status.active : u.status.empty

  const selectDays = (value: string) => {
    triggerHaptic('selection')
    setDays(Number(value))
  }

  const selectDeck = (value: string) => {
    triggerHaptic('selection')
    const nextDeck = value as UsageDeck

    if (nextDeck === 'ledger') {
      setDrilldown(null)
    }

    setDeck(nextDeck)
  }

  const selectMetric = (value: UsageMetric) => {
    triggerHaptic('selection')
    setMetric(value)
  }

  const selectScope = (value: MeterScope) => {
    triggerHaptic('selection')
    setMeterScope(value)
  }

  const selectLimit = (value: number) => {
    triggerHaptic('selection')
    setRecentLimit(value)
  }

  const inspectRoute = (route: RouteDrilldown) => {
    triggerHaptic('tap')
    setDrilldown(route)
    setRecentLimit(500)
    setDeck('ledger')
  }

  const refetchAll = () => {
    void Promise.all([
      overviewQuery.refetch(),
      meterSummaryQuery.refetch(),
      meterDetailsQuery.refetch(),
      meterRecentQuery.refetch()
    ])
  }

  if (overviewQuery.isError) {
    const message = overviewQuery.error instanceof Error ? overviewQuery.error.message : u.error.description

    return (
      <main className="usage-deck dark h-full min-h-0 overflow-y-auto" style={darkStyle}>
        <div className={`mx-auto w-full ${PAGE_MAX_W} ${PAGE_INSET_X} py-6 pb-24`}>
          <ErrorState description={message} title={u.error.title}>
            <Button className="mx-auto" onClick={() => void overviewQuery.refetch()} variant="outline">
              <RefreshCw className="size-3.5" />
              {u.error.retry}
            </Button>
          </ErrorState>
        </div>
      </main>
    )
  }

  const report = overviewQuery.data

  return (
    <main className="usage-deck dark h-full min-h-0 overflow-y-auto" style={darkStyle}>
      <div className={`mx-auto w-full ${PAGE_MAX_W} ${PAGE_INSET_X} py-4 pb-24`}>
        <header className="mb-4 border-b border-border pb-4">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2 flex flex-wrap items-center gap-2 font-mono text-[10px] font-semibold uppercase tracking-[0.18em]">
                <span className="text-ui-green">{u.eyebrow}</span>
                <span aria-hidden="true" className="text-muted-foreground">
                  //
                </span>
                <span className="usage-status inline-flex items-center gap-1.5" data-tone={statusTone}>
                  <span aria-hidden="true" className={meterDegraded || !meterHasEvents ? '' : 'usage-led'}>
                    ●
                  </span>
                  {statusLabel}
                </span>
              </div>
              <h1 className="text-2xl font-semibold tracking-tight text-foreground">{u.title}</h1>
              <p className="mt-1 max-w-2xl text-sm leading-5 text-muted-foreground">{u.subtitle}</p>
            </div>

            <div className="flex flex-wrap items-center justify-end gap-2">
              <div aria-label={u.rangeAria} role="group">
                <SegmentedControl
                  onChange={selectDays}
                  options={[7, 30, 90, 365].map(value => ({ id: String(value), label: u.days(value) }))}
                  value={String(days)}
                />
              </div>
              <Button disabled={isFetching} onClick={refetchAll} size="sm" variant="outline">
                <RefreshCw className={isFetching ? 'size-3.5 animate-spin' : 'size-3.5'} />
                {isFetching ? u.syncing : u.sync}
              </Button>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
            <span>{u.generated(formatTimestamp(report?.generated_at, locale))}</span>
            <span aria-hidden="true">//</span>
            <span>{u.sources.session}</span>
            <span aria-hidden="true">+</span>
            <span>{u.sources.install}</span>
            {meterDegraded && (
              <span className="usage-status inline-flex items-center gap-1" data-tone="error">
                <AlertTriangle className="size-3" /> {u.partialData}
              </span>
            )}
          </div>
        </header>

        {overviewQuery.isLoading || !report ? (
          <LoadingDeck label={u.loading} />
        ) : (
          <>
            <MacroStrip meter={meterSummaryQuery.data} meterUnavailable={meterSummaryQuery.isError} report={report} />

            <div className="my-4 flex items-center justify-between gap-3 border-b border-border pb-3">
              <div aria-label={u.deckAria} role="group">
                <SegmentedControl
                  onChange={selectDeck}
                  options={[
                    { id: 'overview', label: u.decks.overview },
                    { id: 'routes', label: u.decks.routes },
                    { id: 'ledger', label: u.decks.ledger }
                  ]}
                  value={deck}
                />
              </div>
              <p className="hidden font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground md:block">
                {deck === 'overview'
                  ? u.deckHints.overview
                  : deck === 'routes'
                    ? u.deckHints.routes
                    : u.deckHints.ledger}
              </p>
            </div>

            {deck === 'overview' && (
              <OverviewDeck
                meter={meterSummaryQuery.data}
                meterUnavailable={meterSummaryQuery.isError}
                metric={metric}
                onMetricChange={selectMetric}
                report={report}
              />
            )}
            {deck === 'routes' && (
              <RouteMatrix
                details={meterDetailsQuery.data}
                error={meterDetailsQuery.isError}
                isFetching={meterDetailsQuery.isFetching}
                onInspect={inspectRoute}
                onScopeChange={selectScope}
                scope={meterScope}
              />
            )}
            {deck === 'ledger' && (
              <LedgerDeck
                drilldown={drilldown}
                error={meterRecentQuery.isError}
                isFetching={meterRecentQuery.isFetching}
                limit={recentLimit}
                onClearDrilldown={() => setDrilldown(null)}
                onLimitChange={selectLimit}
                recent={meterRecentQuery.data}
              />
            )}
          </>
        )}
      </div>
    </main>
  )
}
