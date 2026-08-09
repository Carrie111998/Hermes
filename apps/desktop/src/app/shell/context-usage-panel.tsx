import { useEffect, useMemo, useRef, useState } from 'react'

import { ErrorIcon } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import { compactNumber } from '@/lib/format'
import { cn } from '@/lib/utils'
import type {
  ContextBreakdown,
  ContextUsageCategory,
  UsageAccount,
  UsageAccountsContract,
  UsageProvider,
  UsageStats
} from '@/types/hermes'

interface ContextUsagePanelProps {
  currentUsage: UsageStats
  onUsageSnapshot?: (usage: Pick<UsageStats, 'context_max' | 'context_percent' | 'context_used'>) => void
  profile?: string
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  sessionId: string | null
}

type AccountLoadState = 'error' | 'loading' | 'ready' | 'unsupported'

export function ContextUsagePanel({
  currentUsage,
  onUsageSnapshot,
  profile,
  requestGateway,
  sessionId
}: ContextUsagePanelProps) {
  const { t } = useI18n()
  const copy = t.shell.statusbar.contextUsagePanel
  const [breakdown, setBreakdown] = useState<ContextBreakdown | null>(null)
  const [loading, setLoading] = useState(false)
  const [accounts, setAccounts] = useState<UsageAccountsContract | null>(null)
  const [accountState, setAccountState] = useState<AccountLoadState>('loading')
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

  useEffect(() => {
    let cancelled = false
    setAccountState('loading')

    const params: Record<string, unknown> = { refresh: true }
    if (sessionId) {
      params.session_id = sessionId
    }
    if (profile) {
      params.profile = profile
    }

    void requestGateway<UsageAccountsContract>('usage.accounts', params)
      .then(payload => {
        if (cancelled) {
          return
        }
        if (payload.contract?.name !== 'usage.accounts' || payload.contract.version !== 1) {
          setAccounts(null)
          setAccountState('unsupported')
          return
        }
        setAccounts(payload)
        setAccountState('ready')
      })
      .catch(error => {
        if (cancelled) {
          return
        }
        setAccounts(null)
        setAccountState(isMissingMethodError(error) ? 'unsupported' : 'error')
      })

    return () => {
      cancelled = true
    }
  }, [profile, requestGateway, sessionId])

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

      {loading && <Loader className="size-5 text-muted-foreground" label={copy.loading} type="fourier-flow" />}

      {!loading && !categories.length && <p className="text-[0.6875rem] text-muted-foreground">{copy.empty}</p>}

      <AccountUsageSection contract={accounts} copy={copy} state={accountState} />
    </div>
  )
}

function isMissingMethodError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error ?? '')
  const normalized = message.toLowerCase()

  return (
    normalized.includes('-32601') || normalized.includes('method not found') || normalized.includes('unknown method')
  )
}

function AccountUsageSection({
  contract,
  copy,
  state
}: {
  contract: UsageAccountsContract | null
  copy: ReturnType<typeof useI18n>['t']['shell']['statusbar']['contextUsagePanel']
  state: AccountLoadState
}) {
  return (
    <section className="flex flex-col gap-3 border-t border-(--ui-stroke-tertiary) pt-3">
      <p className="font-medium text-foreground">{copy.accountTitle}</p>

      {state === 'loading' && (
        <Loader className="size-5 text-muted-foreground" label={copy.accountLoading} type="fourier-flow" />
      )}

      {state === 'unsupported' && <p className="text-[0.6875rem] text-muted-foreground">{copy.accountUnsupported}</p>}

      {state === 'error' && (
        <div className="flex items-center gap-2 text-destructive" role="alert">
          <ErrorIcon size="0.875rem" />
          <span>{copy.accountError}</span>
        </div>
      )}

      {state === 'ready' && contract && (
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

          {!contract.providers.length && <p className="text-[0.6875rem] text-muted-foreground">{copy.accountEmpty}</p>}

          {contract.providers.map(provider => (
            <ProviderUsage copy={copy} key={provider.provider} provider={provider} />
          ))}
        </>
      )}
    </section>
  )
}

function ProviderUsage({
  copy,
  provider
}: {
  copy: ReturnType<typeof useI18n>['t']['shell']['statusbar']['contextUsagePanel']
  provider: UsageProvider
}) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate font-medium text-foreground">{provider.provider}</span>
        <span className="shrink-0 text-[0.6875rem] text-muted-foreground">
          {provider.routing.ready}/{provider.accounts.length} {copy.health.ready}
        </span>
      </div>

      {provider.usage_capability === 'unsupported' && (
        <p className="text-[0.6875rem] text-muted-foreground">{copy.quota.unsupported}</p>
      )}

      {provider.accounts.map(account => (
        <AccountUsage
          copy={copy}
          account={account}
          key={account.account_id}
          showQuota={provider.usage_capability === 'supported'}
        />
      ))}
    </section>
  )
}

function AccountUsage({
  account,
  copy,
  showQuota
}: {
  account: UsageAccount
  copy: ReturnType<typeof useI18n>['t']['shell']['statusbar']['contextUsagePanel']
  showQuota: boolean
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-foreground">{copy.accountName(account.account_id.slice(-4))}</span>
        <span className="shrink-0 text-[0.6875rem] text-muted-foreground">{copy.health[account.health.status]}</span>
      </div>

      {showQuota && account.quota.status === 'available' && (
        <>
          {account.quota.windows.map(window => {
            const used = Math.min(100, Math.max(0, Math.round(window.used_percent ?? 0)))
            const remaining = 100 - used

            return (
              <div className="flex flex-col gap-1" key={`${account.account_id}:${window.label}`}>
                <div className="flex items-center justify-between gap-2 text-[0.6875rem]">
                  <span className="min-w-0 truncate text-muted-foreground">{window.label}</span>
                  <span className="shrink-0 tabular-nums text-foreground">{copy.remaining(remaining)}</span>
                </div>
                <Progress aria-label={`${window.label}: ${copy.remaining(remaining)}`} size="sm" value={used / 100} />
              </div>
            )
          })}

          {account.quota.details?.map(detail => (
            <p className="break-words text-[0.6875rem] text-muted-foreground" key={detail}>
              {detail}
            </p>
          ))}
        </>
      )}

      {showQuota && account.quota.status !== 'available' && (
        <p className="text-[0.6875rem] text-muted-foreground">{copy.quota[account.quota.status]}</p>
      )}
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
