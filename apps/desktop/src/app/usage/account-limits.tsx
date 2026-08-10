import { useCallback, useEffect, useRef, useState } from 'react'

import { ErrorIcon } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight, Clock } from '@/lib/icons'
import { fmtDateTime } from '@/lib/time'
import type { UsageAccount, UsageAccountsContract, UsageProvider } from '@/types/hermes'

export type AccountLimitsState = 'error' | 'loading' | 'ready' | 'unsupported'

type RequestGateway = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>

type AccountLimitsCopy = ReturnType<typeof useI18n>['t']['shell']['statusbar']['contextUsagePanel']

/**
 * Order providers by actual usefulness: the current session's provider first
 * (session fact from local analytics — not a routing guess), then providers
 * with real quota data, then supported providers needing attention, with
 * unsupported providers split out for the merged collapsed group. Ordering is
 * stable within each group (backend priority order is preserved).
 */
export function orderUsageProviders(
  providers: readonly UsageProvider[],
  currentProvider?: null | string
): { pinned: UsageProvider[]; unsupported: UsageProvider[] } {
  const groupOf = (provider: UsageProvider): number => {
    if (currentProvider && provider.provider === currentProvider) {
      return 0
    }
    if (provider.usage_capability === 'supported') {
      return provider.accounts.some(account => account.quota.status === 'available') ? 1 : 2
    }
    return 3
  }

  return {
    pinned: providers
      .map((provider, index) => ({ group: groupOf(provider), index, provider }))
      .filter(entry => entry.group < 3)
      .sort((a, b) => a.group - b.group || a.index - b.index)
      .map(entry => entry.provider),
    unsupported: providers.filter(provider => groupOf(provider) === 3)
  }
}

const HEALTH_ICON = {
  cooldown: Clock,
  error: AlertCircle,
  expired: AlertCircle,
  ready: CheckCircle2,
  unavailable: AlertCircle
} as const

const HEALTH_TONE = {
  cooldown: 'text-muted-foreground',
  error: 'text-destructive',
  expired: 'text-destructive',
  ready: 'text-emerald-500',
  unavailable: 'text-muted-foreground'
} as const

function HealthBadge({
  copy,
  status
}: {
  copy: AccountLimitsCopy
  status: UsageAccount['health']['status']
}) {
  const Icon = HEALTH_ICON[status] ?? AlertCircle
  const tone = HEALTH_TONE[status] ?? 'text-muted-foreground'

  return (
    <span className={`inline-flex shrink-0 items-center gap-1 text-[0.6875rem] ${tone}`} data-slot="health-badge">
      <Icon aria-hidden className="size-3" />
      {copy.health[status]}
    </span>
  )
}

export function isMissingUsageMethodError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')
  const normalized = message.toLowerCase()

  return (
    normalized.includes('-32601') || normalized.includes('method not found') || normalized.includes('unknown method')
  )
}

/**
 * Canonical `usage.accounts` reader shared by the statusbar popover and the
 * Command Center. All quota semantics (stable ids, display names, stale,
 * error categories) come from the backend contract — this hook never infers
 * provider specifics. A failed refresh keeps the last-known contract on
 * screen; superseded responses are dropped via a monotonic request id.
 */
export function useUsageAccounts({
  profile,
  requestGateway,
  sessionId
}: {
  profile?: null | string
  requestGateway: RequestGateway
  sessionId: null | string
}): {
  contract: UsageAccountsContract | null
  refresh: () => void
  refreshing: boolean
  state: AccountLimitsState
} {
  const [contract, setContract] = useState<UsageAccountsContract | null>(null)
  const [state, setState] = useState<AccountLimitsState>('loading')
  const [refreshing, setRefreshing] = useState(false)
  const requestIdRef = useRef(0)
  const mountedRef = useRef(true)

  const load = useCallback(
    (isRefresh: boolean) => {
      const requestId = requestIdRef.current + 1
      requestIdRef.current = requestId
      if (isRefresh) {
        setRefreshing(true)
      } else {
        setState('loading')
      }

      const params: Record<string, unknown> = { refresh: true }
      if (sessionId) {
        params.session_id = sessionId
      }
      if (profile) {
        params.profile = profile
      }

      void requestGateway<UsageAccountsContract>('usage.accounts', params)
        .then(payload => {
          if (!mountedRef.current || requestId !== requestIdRef.current) {
            return
          }
          if (payload.contract?.name !== 'usage.accounts' || payload.contract.version !== 1) {
            setContract(null)
            setState('unsupported')
            return
          }
          setContract(payload)
          setState('ready')
        })
        .catch((error: unknown) => {
          if (!mountedRef.current || requestId !== requestIdRef.current) {
            return
          }
          if (isMissingUsageMethodError(error)) {
            setContract(null)
            setState('unsupported')
          } else {
            // Keep the last-known contract visible next to the failure notice.
            setState('error')
          }
        })
        .finally(() => {
          if (mountedRef.current && requestId === requestIdRef.current) {
            setRefreshing(false)
          }
        })
    },
    [profile, requestGateway, sessionId]
  )

  useEffect(() => {
    mountedRef.current = true
    load(false)

    return () => {
      mountedRef.current = false
    }
  }, [load])

  const refresh = useCallback(() => load(true), [load])

  return { contract, refresh, refreshing, state }
}

export function AccountLimitsStateNotice({ state }: { state: AccountLimitsState }) {
  const { t } = useI18n()
  const copy = t.shell.statusbar.contextUsagePanel

  if (state === 'loading') {
    return <Loader className="size-5 text-muted-foreground" label={copy.accountLoading} type="fourier-flow" />
  }
  if (state === 'unsupported') {
    return <p className="text-[0.6875rem] text-muted-foreground">{copy.accountUnsupported}</p>
  }
  if (state === 'error') {
    return (
      <div className="flex items-center gap-2 text-destructive" role="alert">
        <ErrorIcon size="0.875rem" />
        <span>{copy.accountError}</span>
      </div>
    )
  }
  return null
}

function formatTimestamp(value: null | string | undefined): string {
  if (!value) {
    return ''
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return fmtDateTime.format(date)
}

function AccountLimitRow({
  account,
  copy,
  showQuota
}: {
  account: UsageAccount
  copy: AccountLimitsCopy
  showQuota: boolean
}) {
  const quota = account.quota
  const reason = quota.reason?.trim() ?? ''
  const isAuthFailure = reason.startsWith('Credential authentication failed')
  const alertWorthy = quota.status === 'error' || isAuthFailure
  const fetchedAt = formatTimestamp(quota.fetched_at)

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-foreground">
          {account.display_name ?? copy.accountName(account.account_id.slice(-4))}
        </span>
        <HealthBadge copy={copy} status={account.health.status} />
      </div>

      {quota.plan && <span className="text-[0.6875rem] text-muted-foreground">{quota.plan}</span>}

      {showQuota && quota.status === 'available' && (
        <>
          {quota.windows.map(window => {
            const used = Math.min(100, Math.max(0, Math.round(window.used_percent ?? 0)))
            const remaining = 100 - used
            const resetAt = formatTimestamp(window.reset_at)

            return (
              <div className="flex flex-col gap-1" key={`${account.account_id}:${window.label}`}>
                <div className="flex items-center gap-2 text-[0.6875rem]">
                  <span className="w-14 shrink-0 truncate text-muted-foreground">{window.label}</span>
                  {/* The label says "% left", so the fill must visualize the
                      remaining share — never the used share. */}
                  <Progress
                    aria-label={`${window.label}: ${copy.remaining(remaining)}`}
                    className="min-w-0 flex-1"
                    size="sm"
                    value={remaining / 100}
                  />
                  <span className="shrink-0 tabular-nums text-foreground">{copy.remaining(remaining)}</span>
                </div>
                {resetAt && (
                  <span className="pl-16 text-[0.625rem] text-muted-foreground">{copy.resetsAt(resetAt)}</span>
                )}
              </div>
            )
          })}

          {quota.stale && (
            <p className="text-[0.6875rem] text-muted-foreground" role="status">
              {copy.staleData}
              {fetchedAt ? ` · ${copy.updatedAt(fetchedAt)}` : ''}
            </p>
          )}

          {quota.details?.map(detail => (
            <p className="break-words text-[0.6875rem] text-muted-foreground" key={detail}>
              {detail}
            </p>
          ))}
        </>
      )}

      {showQuota && quota.status !== 'available' && (
        <p
          className={`break-words text-[0.6875rem] ${alertWorthy ? 'text-destructive' : 'text-muted-foreground'}`}
          role={alertWorthy ? 'alert' : undefined}
        >
          {reason || copy.quota[quota.status as keyof typeof copy.quota] || copy.quota.unavailable}
        </p>
      )}
    </div>
  )
}

function ProviderLimits({
  copy,
  isCurrent,
  provider
}: {
  copy: AccountLimitsCopy
  isCurrent?: boolean
  provider: UsageProvider
}) {
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="flex min-w-0 items-baseline gap-1.5">
          <span className="min-w-0 truncate font-medium text-foreground">{provider.provider}</span>
          {isCurrent && (
            <span className="shrink-0 rounded-[3px] bg-accent/60 px-1 py-px text-[0.625rem] text-foreground">
              {copy.currentBadge}
            </span>
          )}
        </span>
        <span className="shrink-0 text-[0.6875rem] text-muted-foreground">
          {provider.routing.ready}/{provider.accounts.length} {copy.health.ready}
        </span>
      </div>

      {provider.accounts.map(account => (
        <AccountLimitRow
          account={account}
          copy={copy}
          key={account.account_id}
          showQuota={provider.usage_capability === 'supported'}
        />
      ))}
    </section>
  )
}

export function AccountLimitsView({
  contract,
  onOpenCommandCenter,
  onRefresh,
  refreshing = false
}: {
  contract: UsageAccountsContract
  onOpenCommandCenter?: () => void
  onRefresh?: () => void
  refreshing?: boolean
}) {
  const { t } = useI18n()
  const copy = t.shell.statusbar.contextUsagePanel
  const [unsupportedOpen, setUnsupportedOpen] = useState(false)
  const hasProviderReported = contract.providers.some(provider =>
    provider.accounts.some(account => account.quota.source === 'provider_reported')
  )
  const currentProvider = contract.local.status === 'available' ? contract.local.provider : null
  const { pinned, unsupported } = orderUsageProviders(contract.providers, currentProvider)
  const unsupportedReady = unsupported.reduce((sum, provider) => sum + provider.routing.ready, 0)
  const unsupportedAccounts = unsupported.reduce((sum, provider) => sum + provider.accounts.length, 0)

  return (
    <div className="flex min-w-0 flex-col gap-3" data-slot="account-limits">
      {(hasProviderReported || onRefresh || onOpenCommandCenter) && (
        <div className="flex items-center justify-between gap-2">
          <span className="min-w-0 truncate text-[0.6875rem] text-muted-foreground">
            {hasProviderReported ? copy.sourceOfficial : ''}
          </span>
          <span className="flex shrink-0 items-center gap-2">
            {onRefresh && (
              <button
                className="text-[0.6875rem] text-(--ui-text-tertiary) underline-offset-2 hover:text-foreground hover:underline focus-visible:text-foreground focus-visible:underline disabled:opacity-50"
                disabled={refreshing}
                onClick={onRefresh}
                type="button"
              >
                {refreshing ? copy.refreshing : copy.refresh}
              </button>
            )}
            {onOpenCommandCenter && (
              <button
                className="text-[0.6875rem] text-(--ui-text-tertiary) underline-offset-2 hover:text-foreground hover:underline focus-visible:text-foreground focus-visible:underline"
                onClick={onOpenCommandCenter}
                type="button"
              >
                {copy.openInCommandCenter}
              </button>
            )}
          </span>
        </div>
      )}

      {!contract.providers.length && <p className="text-[0.6875rem] text-muted-foreground">{copy.accountEmpty}</p>}

      {pinned.map(provider => (
        <ProviderLimits
          copy={copy}
          isCurrent={Boolean(currentProvider && provider.provider === currentProvider)}
          key={provider.provider}
          provider={provider}
        />
      ))}

      {unsupported.length > 0 && (
        <section className="flex min-w-0 flex-col gap-1.5">
          <button
            aria-expanded={unsupportedOpen}
            className="flex min-w-0 items-center gap-1.5 text-left text-[0.6875rem] text-muted-foreground hover:text-foreground focus-visible:text-foreground focus-visible:underline"
            onClick={() => setUnsupportedOpen(open => !open)}
            type="button"
          >
            {unsupportedOpen ? (
              <ChevronDown aria-hidden className="size-3 shrink-0" />
            ) : (
              <ChevronRight aria-hidden className="size-3 shrink-0" />
            )}
            <span className="min-w-0 truncate">
              {copy.otherProviders(unsupported.length)}{' '}
              <span data-slot="unsupported-note">({copy.unsupportedShort})</span>
            </span>
            <span className="shrink-0 tabular-nums">
              {unsupportedReady}/{unsupportedAccounts}
            </span>
          </button>

          {unsupportedOpen &&
            unsupported.map(provider => (
              <div className="flex items-baseline justify-between gap-2 pl-4" key={provider.provider}>
                <span className="min-w-0 truncate text-muted-foreground">{provider.provider}</span>
                <span className="shrink-0 text-[0.6875rem] text-muted-foreground">
                  {provider.routing.ready}/{provider.accounts.length} {copy.health.ready}
                </span>
              </div>
            ))}
        </section>
      )}
    </div>
  )
}
