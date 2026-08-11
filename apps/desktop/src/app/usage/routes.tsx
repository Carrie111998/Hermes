import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n/context'
import { AlertTriangle, Eye, Search, SlidersHorizontal } from '@/lib/icons'

import { UsageSelect } from './controls'
import {
  cacheRatio,
  formatCompact,
  formatCurrency,
  formatNumber,
  formatPercent,
  routeTokens,
  uniqueValues
} from './format'
import { SectionHeader } from './overview'
import type { MeterScope, RouteSort, UsageMeterDetails, UsageMeterRoute } from './types'

export type RouteDrilldown = {
  apiMode: string
  endTs: number | null
  model: string
  provider: string
  scope: MeterScope
  startTs: number | null
}

type RouteMatrixProps = {
  details?: UsageMeterDetails
  error: boolean
  isFetching: boolean
  onInspect: (route: RouteDrilldown) => void
  onScopeChange: (scope: MeterScope) => void
  scope: MeterScope
}

function routeSortValue(route: UsageMeterRoute, sort: RouteSort): number {
  if (sort === 'cost') {
    return route.unpriced_calls === route.calls ? -1 : route.estimated_cost_usd
  }

  if (sort === 'calls') {
    return route.calls
  }

  if (sort === 'cache') {
    return cacheRatio(route.cache_read_tokens, route.input_tokens)
  }

  return routeTokens(route)
}

function routeCostTone(route: UsageMeterRoute): 'estimated' | 'included' | 'mixed' | 'unavailable' {
  if (route.unpriced_calls === route.calls && route.calls > 0) {
    return 'unavailable'
  }

  if (route.included_calls === route.calls && route.calls > 0) {
    return 'included'
  }

  if (route.included_calls > 0 || route.unpriced_calls > 0) {
    return 'mixed'
  }

  return 'estimated'
}

export function RouteMatrix({ details, error, isFetching, onInspect, onScopeChange, scope }: RouteMatrixProps) {
  const { locale, t } = useI18n()
  const u = t.usageDashboard
  const [sort, setSort] = useState<RouteSort>('cost')
  const [query, setQuery] = useState('')
  const [provider, setProvider] = useState('all')
  const [apiMode, setApiMode] = useState('all')
  const routes = useMemo(() => details?.routes ?? [], [details?.routes])
  const providers = useMemo(() => uniqueValues(routes.map(route => route.provider)), [routes])
  const apiModes = useMemo(() => uniqueValues(routes.map(route => route.api_mode)), [routes])

  const visibleRoutes = useMemo(() => {
    const needle = query.trim().toLowerCase()

    return routes
      .filter(route => provider === 'all' || route.provider === provider)
      .filter(route => apiMode === 'all' || route.api_mode === apiMode)
      .filter(route => !needle || `${route.provider} ${route.model} ${route.api_mode}`.toLowerCase().includes(needle))
      .sort((a, b) => routeSortValue(b, sort) - routeSortValue(a, sort))
  }, [apiMode, provider, query, routes, sort])

  const totals = useMemo(
    () =>
      visibleRoutes.reduce(
        (sum, route) => ({
          calls: sum.calls + route.calls,
          cost: sum.cost + route.estimated_cost_usd,
          costKnownCalls: sum.costKnownCalls + route.priced_calls + route.included_calls,
          includedCalls: sum.includedCalls + route.included_calls,
          pricedCalls: sum.pricedCalls + route.priced_calls,
          tokens: sum.tokens + routeTokens(route),
          unpricedCalls: sum.unpricedCalls + route.unpriced_calls
        }),
        { calls: 0, cost: 0, costKnownCalls: 0, includedCalls: 0, pricedCalls: 0, tokens: 0, unpricedCalls: 0 }
      ),
    [visibleRoutes]
  )

  return (
    <div className="space-y-4">
      <section className="usage-rail border-y border-border py-3">
        <SectionHeader
          action={
            <div aria-label={u.routes.scopeAria} role="group">
              <SegmentedControl
                onChange={value => onScopeChange(value as MeterScope)}
                options={[
                  { id: 'all', label: u.scope.all },
                  { id: 'month', label: u.scope.month }
                ]}
                value={scope}
              />
            </div>
          }
          description={u.routes.description}
          eyebrow="ROUTE // MATRIX"
          title={u.routes.title}
        />

        {error && (
          <div className="mb-4 flex items-start gap-2 border-y border-ui-red/40 bg-ui-red/5 px-3 py-2 text-xs text-ui-red">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{u.routes.loadFailed}</span>
          </div>
        )}

        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Input
            aria-label={u.filters.searchAria}
            className="w-full sm:w-64"
            onChange={event => setQuery(event.target.value)}
            placeholder={u.filters.searchRoutes}
            prefix={<Search className="size-3.5" />}
            size="sm"
            value={query}
          />
          <UsageSelect
            className="min-w-36"
            label={u.filters.provider}
            onChange={setProvider}
            options={[
              { label: u.filters.allProviders, value: 'all' },
              ...providers.map(value => ({ label: value, value }))
            ]}
            value={provider}
          />
          <UsageSelect
            className="min-w-32"
            label={u.filters.apiMode}
            onChange={setApiMode}
            options={[{ label: u.filters.allModes, value: 'all' }, ...apiModes.map(value => ({ label: value, value }))]}
            value={apiMode}
          />
          <div className="ms-auto flex items-center gap-2">
            <SlidersHorizontal className="size-3.5 text-muted-foreground" />
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
          </div>
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-y border-border bg-card/20 px-3 py-2 font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">
          <span>
            {u.routes.visible(formatNumber(visibleRoutes.length, locale), formatNumber(routes.length, locale))}
          </span>
          <span aria-hidden="true">//</span>
          <span>{u.routes.calls(formatNumber(totals.calls, locale))}</span>
          <span aria-hidden="true">//</span>
          <span>{u.routes.tokens(formatCompact(totals.tokens, locale))}</span>
          <span aria-hidden="true">//</span>
          <span>{u.routes.cost(formatCurrency(totals.costKnownCalls > 0 ? totals.cost : null, locale))}</span>
          <span aria-hidden="true">//</span>
          <span>{u.macro.pricingCoverage(totals.pricedCalls, totals.includedCalls, totals.unpricedCalls)}</span>
          {isFetching && <span className="ms-auto text-ui-green">{u.syncing}</span>}
        </div>

        {visibleRoutes.length ? (
          <div className="usage-table-scroll overflow-x-auto border-y border-border">
            <table className="w-full min-w-[980px] text-start text-xs">
              <thead className="bg-card/50 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">{u.table.route}</th>
                  <th className="px-3 py-2 font-medium">{u.table.apiMode}</th>
                  <th className="px-3 py-2 text-end font-medium">{u.table.calls}</th>
                  <th className="px-3 py-2 text-end font-medium">{u.table.input}</th>
                  <th className="px-3 py-2 text-end font-medium">{u.table.cacheRead}</th>
                  <th className="px-3 py-2 text-end font-medium">{u.table.cacheWrite}</th>
                  <th className="px-3 py-2 text-end font-medium">{u.table.output}</th>
                  <th className="px-3 py-2 text-end font-medium">{u.table.reasoning}</th>
                  <th className="px-3 py-2 text-end font-medium">{u.table.cost}</th>
                  <th className="w-12 px-3 py-2">
                    <span className="sr-only">{u.table.inspect}</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {visibleRoutes.map(route => {
                  const tone = routeCostTone(route)

                  return (
                    <tr className="hover:bg-hover" key={`${route.provider}:${route.model}:${route.api_mode}`}>
                      <td className="max-w-72 px-3 py-2.5">
                        <p className="truncate font-medium text-foreground" title={route.model}>
                          {route.model}
                        </p>
                        <p className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">{route.provider}</p>
                      </td>
                      <td className="px-3 py-2.5 font-mono text-[10px] text-muted-foreground">
                        {route.api_mode || u.unknown}
                      </td>
                      <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                        {formatNumber(route.calls, locale)}
                      </td>
                      <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                        {formatCompact(route.input_tokens, locale)}
                      </td>
                      <td className="px-3 py-2.5 text-end font-mono tabular-nums text-ui-cyan">
                        {formatCompact(route.cache_read_tokens, locale)}
                        <span className="ms-1 text-[9px] text-muted-foreground">
                          {formatPercent(cacheRatio(route.cache_read_tokens, route.input_tokens), locale)}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                        {formatCompact(route.cache_write_tokens, locale)}
                      </td>
                      <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                        {formatCompact(route.output_tokens, locale)}
                      </td>
                      <td className="px-3 py-2.5 text-end font-mono tabular-nums">
                        {formatCompact(route.reasoning_tokens, locale)}
                      </td>
                      <td className="px-3 py-2.5 text-end">
                        <p className="font-mono tabular-nums text-foreground">
                          {formatCurrency(tone === 'unavailable' ? null : route.estimated_cost_usd, locale)}
                        </p>
                        <p className="usage-status mt-0.5 font-mono text-[9px] uppercase" data-tone={tone}>
                          {u.costStatus[tone]}
                        </p>
                      </td>
                      <td className="px-2 py-2 text-end">
                        <Button
                          aria-label={u.routes.inspect(`${route.provider}/${route.model}`)}
                          onClick={() =>
                            onInspect({
                              apiMode: route.api_mode,
                              endTs: details?.end_ts ?? null,
                              model: route.model,
                              provider: route.provider,
                              scope,
                              startTs: details?.start_ts ?? null
                            })
                          }
                          size="icon-xs"
                          title={u.routes.inspect(`${route.provider}/${route.model}`)}
                          variant="ghost"
                        >
                          <Eye className="size-3.5" />
                        </Button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="dither grid min-h-48 place-items-center border-y border-border px-4 py-10 text-center text-xs text-muted-foreground">
            {routes.length ? u.routes.noMatch : u.routes.empty}
          </div>
        )}
      </section>

      <p className="max-w-3xl text-[10px] leading-4 text-muted-foreground">{u.routes.disclaimer}</p>
    </div>
  )
}
