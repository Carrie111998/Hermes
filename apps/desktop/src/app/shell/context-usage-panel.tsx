import { useEffect, useMemo, useRef, useState } from 'react'

import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'
import { compactNumber } from '@/lib/format'
import { ChevronDown, ChevronRight } from '@/lib/icons'
import { cn } from '@/lib/utils'
import type {
  ContextBreakdown,
  ContextUsageCategory,
  UsageStats
} from '@/types/hermes'

import { AccountLimitsStateNotice, AccountLimitsView, useUsageAccounts } from '@/app/usage/account-limits'

interface ContextUsagePanelProps {
  currentUsage: UsageStats
  /** Opens the Command Center Usage section (full account limits + analytics). */
  onOpenCommandCenter?: () => void
  onUsageSnapshot?: (usage: Pick<UsageStats, 'context_max' | 'context_percent' | 'context_used'>) => void
  profile?: string
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  sessionId: string | null
}

export function ContextUsagePanel({
  currentUsage,
  onOpenCommandCenter,
  onUsageSnapshot,
  profile,
  requestGateway,
  sessionId
}: ContextUsagePanelProps) {
  const { t } = useI18n()
  const copy = t.shell.statusbar.contextUsagePanel
  const [breakdown, setBreakdown] = useState<ContextBreakdown | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const { contract, refresh, refreshing, state: accountState } = useUsageAccounts({
    profile,
    requestGateway,
    sessionId
  })
  const onUsageSnapshotRef = useRef(onUsageSnapshot)
  onUsageSnapshotRef.current = onUsageSnapshot

  useEffect(() => {
    if (!sessionId) {
      setBreakdown(null)
      setLoading(false)

      return
    }

    let cancelled = false
    setLoading(true)

    void requestGateway<ContextBreakdown>('session.context_breakdown', { session_id: sessionId })
      .then(data => {
        if (!cancelled) {
          setBreakdown(data)
          onUsageSnapshotRef.current?.({
            context_max: data.context_max,
            context_percent: data.context_percent,
            context_used: data.context_used
          })
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBreakdown(null)
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [requestGateway, sessionId])

  const contextMax = breakdown?.context_max ?? currentUsage.context_max ?? 0
  const contextUsed = breakdown?.context_used ?? currentUsage.context_used ?? 0

  const contextPercent = Math.max(
    0,
    Math.min(100, Math.round(breakdown?.context_percent ?? currentUsage.context_percent ?? 0))
  )

  const categories = useMemo(
    () =>
      (breakdown?.categories ?? []).map(category => ({
        ...category,
        label: copy.categories[category.id as keyof typeof copy.categories] ?? category.label
      })),
    [breakdown?.categories, copy]
  )

  const segmentTotal = categories.reduce((sum, category) => sum + category.tokens, 0) || contextUsed || 1

  return (
    <div
      className="flex max-h-[min(34rem,80vh)] w-80 flex-col gap-4 overflow-y-auto p-3 text-[0.75rem]"
      data-slot="context-usage-panel"
    >
      <div className="flex items-baseline justify-between gap-2">
        <p className="font-medium text-foreground">{copy.title}</p>

        <span className="text-[0.6875rem] text-muted-foreground">
          {copy.tokenSummary(`~${compactNumber(contextUsed)}`, compactNumber(contextMax))}
        </span>
      </div>

      <p className="text-[0.6875rem] text-foreground">{copy.percentFull(contextPercent)}</p>

      <ContextUsageBar categories={categories} segmentTotal={segmentTotal} />

      {categories.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <button
            aria-expanded={detailsOpen}
            className="flex items-center gap-1 text-left text-[0.6875rem] text-muted-foreground hover:text-foreground focus-visible:text-foreground focus-visible:underline"
            onClick={() => setDetailsOpen(open => !open)}
            type="button"
          >
            {detailsOpen ? (
              <ChevronDown aria-hidden className="size-3 shrink-0" />
            ) : (
              <ChevronRight aria-hidden className="size-3 shrink-0" />
            )}
            {copy.categoryDetails} ({categories.length})
          </button>

          {detailsOpen && (
            <ul className="flex flex-col gap-1.5">
              {categories.map(category => (
                <li className="flex items-center justify-between gap-2" key={category.id}>
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="size-2 shrink-0 rounded-[2px]" style={{ background: category.color }} />

                    <span className="truncate text-muted-foreground">{category.label}</span>
                  </span>

                  <span className="shrink-0 tabular-nums text-foreground">{compactNumber(category.tokens)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {loading && <Loader className="size-5 text-muted-foreground" label={copy.loading} type="fourier-flow" />}

      {!loading && !categories.length && <p className="text-[0.6875rem] text-muted-foreground">{copy.empty}</p>}

      <section className="flex flex-col gap-3 border-t border-(--ui-stroke-tertiary) pt-3">
        <p className="font-medium text-foreground">{copy.accountTitle}</p>

        <AccountLimitsStateNotice state={accountState} />

        {contract && (
          <>
            {contract.local.status === 'available' && (
              <div className="flex flex-col gap-0.5">
                <span className="text-[0.6875rem] text-muted-foreground">{copy.localTitle}</span>
                {(contract.local.provider || contract.local.model) && (
                  <span className="truncate text-foreground">
                    {contract.local.provider && contract.local.model
                      ? copy.modelSummary(contract.local.provider, contract.local.model)
                      : (contract.local.provider ?? contract.local.model)}
                  </span>
                )}
                <span className="text-[0.6875rem] tabular-nums text-muted-foreground">
                  {copy.callsAndTokens(contract.local.calls ?? 0, compactNumber(contract.local.tokens?.total ?? 0))}
                </span>
              </div>
            )}

            {contract.local.status === 'unavailable' && (
              <div className="flex flex-col gap-0.5">
                <span className="text-[0.6875rem] text-muted-foreground">{copy.localTitle}</span>
                <span className="text-[0.6875rem] text-muted-foreground">{copy.localUnavailable}</span>
              </div>
            )}

            <AccountLimitsView
              contract={contract}
              onOpenCommandCenter={onOpenCommandCenter}
              onRefresh={refresh}
              refreshing={refreshing}
            />
          </>
        )}
      </section>
    </div>
  )
}

function ContextUsageBar({
  categories,
  segmentTotal
}: {
  categories: readonly ContextUsageCategory[]
  segmentTotal: number
}) {
  return (
    <div
      className={cn(
        'flex h-1.5 overflow-hidden rounded-full',
        categories.length ? 'bg-(--ui-stroke-tertiary)' : 'dither bg-(--ui-bg-elevated)'
      )}
      data-slot="context-usage-bar"
    >
      {categories.map(category => (
        <span
          className="h-full min-w-px"
          key={category.id}
          style={{
            background: category.color,
            width: `${(category.tokens / segmentTotal) * 100}%`
          }}
        />
      ))}
    </div>
  )
}
