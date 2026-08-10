import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { UsageAccountsContract } from '@/types/hermes'

import { AccountLimitsView, orderUsageProviders, useUsageAccounts } from './account-limits'

function makeContract(overrides?: Partial<UsageAccountsContract>): UsageAccountsContract {
  return {
    capabilities: {
      credential_pool_health: true,
      local_session_analytics: true,
      provider_usage: { per_account: true, providers: ['openai-codex'] }
    },
    contract: { name: 'usage.accounts', version: 1 },
    generated_at: '2026-08-10T00:00:00Z',
    local: { status: 'unavailable' },
    providers: [
      {
        accounts: [
          {
            account_id: 'acct_alpha99',
            display_name: 'Codex 1',
            health: { auth_type: 'oauth', status: 'ready' },
            quota: {
              details: [],
              fetched_at: '2026-08-10T04:00:00Z',
              plan: 'Pro',
              source: 'provider_reported',
              status: 'available',
              windows: [
                {
                  label: 'Weekly',
                  reset_at: '2026-08-17T00:00:00Z',
                  used_percent: 40
                }
              ]
            },
            routing: { priority: 0, request_count: 3 }
          }
        ],
        provider: 'openai-codex',
        routing: { cooldown: 0, error: 0, expired: 0, ready: 1, unavailable: 0 },
        usage_capability: 'supported'
      }
    ],
    ...overrides
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('AccountLimitsView', () => {
  it('renders display_name, plan, official source and a fill that matches % left', () => {
    render(<AccountLimitsView contract={makeContract()} />)

    expect(screen.getByText('Codex 1')).toBeTruthy()
    expect(screen.getByText('Pro')).toBeTruthy()
    expect(screen.getByText('Official provider data')).toBeTruthy()
    expect(screen.getByText('60% remaining')).toBeTruthy()

    // The bar must visualize REMAINING quota, matching its label — never used%.
    const bar = screen.getByRole('progressbar', { name: 'Weekly: 60% remaining' })
    expect(bar.getAttribute('aria-valuenow')).toBe('60')

    expect(screen.getByText(/Resets /)).toBeTruthy()
  })

  it('falls back to the opaque account suffix when display_name is absent', () => {
    const contract = makeContract()
    delete contract.providers[0].accounts[0].display_name

    render(<AccountLimitsView contract={contract} />)

    expect(screen.getByText('Account ha99')).toBeTruthy()
  })

  it('announces stale data via role=status and failure reasons via role=alert', () => {
    const contract = makeContract()
    const staleAccount = contract.providers[0].accounts[0]
    staleAccount.quota.stale = true
    staleAccount.quota.details = ['Cached · 2026-08-10T04:00:00Z']
    contract.providers[0].accounts.push({
      account_id: 'acct_beta11',
      display_name: 'Codex 2',
      health: { auth_type: 'oauth', status: 'ready' },
      quota: {
        reason: 'Credential authentication failed (HTTP 401)',
        status: 'unavailable',
        windows: []
      },
      routing: { priority: 1, request_count: 5 }
    })

    render(<AccountLimitsView contract={contract} />)

    const status = screen.getByRole('status')
    expect(status.textContent).toContain('Cached data')

    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('Credential authentication failed (HTTP 401)')
  })

  it('invokes the refresh and command-center actions', () => {
    const onOpenCommandCenter = vi.fn()
    const onRefresh = vi.fn()

    render(
      <AccountLimitsView
        contract={makeContract()}
        onOpenCommandCenter={onOpenCommandCenter}
        onRefresh={onRefresh}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(onRefresh).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Open in Command Center' }))
    expect(onOpenCommandCenter).toHaveBeenCalledTimes(1)
  })
})

describe('useUsageAccounts', () => {
  function Harness({
    requestGateway
  }: {
    requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  }) {
    const { contract, refresh, state } = useUsageAccounts({
      requestGateway,
      sessionId: 'runtime-1'
    })

    return (
      <div>
        <span data-testid="state">{state}</span>
        {contract && (
          <AccountLimitsView contract={contract} onRefresh={refresh} />
        )}
      </div>
    )
  }

  it('a slower superseded refresh cannot overwrite a newer in-flight response', async () => {
    const resolvers: Array<(value: UsageAccountsContract) => void> = []
    const requestGateway = vi.fn(
      () =>
        new Promise<UsageAccountsContract>(resolve => {
          resolvers.push(resolve)
        })
    )

    render(<Harness requestGateway={requestGateway as never} />)
    await waitFor(() => expect(resolvers).toHaveLength(1))

    const v1 = makeContract()
    const v2 = makeContract()
    const v3 = makeContract()
    v2.providers[0].accounts[0].display_name = 'Codex Stale'
    v3.providers[0].accounts[0].display_name = 'Codex Updated'

    // Initial load completes; two refreshes overlap in flight.
    await act(async () => {
      resolvers[0](v1)
    })
    expect(await screen.findByText('Codex 1')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(resolvers).toHaveLength(2))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(resolvers).toHaveLength(3))

    // The newer refresh (request 3) resolves first...
    await act(async () => {
      resolvers[2](v3)
    })
    expect(await screen.findByText('Codex Updated')).toBeTruthy()

    // ...then the genuinely-still-pending older refresh (request 2) lands late
    // and must be dropped — this fails without the monotonic request id guard.
    await act(async () => {
      resolvers[1](v2)
    })
    expect(screen.getByText('Codex Updated')).toBeTruthy()
  })

  it('ignores a response that arrives after unmount', async () => {
    const resolvers: Array<(value: UsageAccountsContract) => void> = []
    const requestGateway = vi.fn(
      () =>
        new Promise<UsageAccountsContract>(resolve => {
          resolvers.push(resolve)
        })
    )

    const { unmount } = render(<Harness requestGateway={requestGateway as never} />)
    await waitFor(() => expect(resolvers).toHaveLength(1))

    unmount()
    // Late resolution after unmount must not throw or set state.
    await act(async () => {
      resolvers[0](makeContract())
    })
  })

  it('keeps the last-known contract visible when a refresh fails', async () => {
    let calls = 0
    const requestGateway = vi.fn(() => {
      calls += 1
      return calls === 1
        ? Promise.resolve(makeContract())
        : Promise.reject(new Error('provider request failed'))
    })

    render(<Harness requestGateway={requestGateway as never} />)
    expect(await screen.findByText('Codex 1')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(screen.getByTestId('state').textContent).toBe('error'))
    // Last-known-good data stays on screen alongside the failure notice.
    expect(screen.getByText('Codex 1')).toBeTruthy()
  })

  it('marks an older backend without the method as unsupported', async () => {
    const requestGateway = vi.fn(() => Promise.reject(new Error('-32601 method not found')))

    render(<Harness requestGateway={requestGateway as never} />)

    await waitFor(() => expect(screen.getByTestId('state').textContent).toBe('unsupported'))
  })
})

describe('orderUsageProviders', () => {
  function provider(name: string, capability: 'supported' | 'unsupported', quotaStatus?: string) {
    return {
      accounts: [
        {
          account_id: `acct_${name}`,
          health: { auth_type: 'api_key', status: 'ready' as const },
          quota: { status: (quotaStatus ?? 'unsupported') as never, windows: [] },
          routing: { priority: 0, request_count: 0 }
        }
      ],
      provider: name,
      routing: { cooldown: 0, error: 0, expired: 0, ready: 1, unavailable: 0 },
      usage_capability: capability
    }
  }

  it('orders current session provider first, then real quota, then attention, then unsupported', () => {
    const providers = [
      provider('copilot', 'unsupported'),
      provider('openai-codex', 'supported', 'error'),
      provider('kimi-coding', 'supported', 'available'),
      provider('openrouter', 'supported', 'available'),
      provider('deepseek', 'unsupported')
    ]

    const { pinned, unsupported } = orderUsageProviders(providers, 'openrouter')

    expect(pinned.map(p => p.provider)).toEqual(['openrouter', 'kimi-coding', 'openai-codex'])
    expect(unsupported.map(p => p.provider)).toEqual(['copilot', 'deepseek'])
  })

  it('keeps input order within a group and works without a current provider', () => {
    const providers = [
      provider('b-no-quota', 'supported', 'error'),
      provider('a-available', 'supported', 'available'),
      provider('c-available', 'supported', 'available')
    ]

    const { pinned, unsupported } = orderUsageProviders(providers, null)

    expect(pinned.map(p => p.provider)).toEqual(['a-available', 'c-available', 'b-no-quota'])
    expect(unsupported).toEqual([])
  })
})

describe('AccountLimitsView usage-ordered layout', () => {
  function orderedContract(): UsageAccountsContract {
    const base = makeContract()
    base.local = { provider: 'kimi-coding', status: 'available' }
    base.providers = [
      {
        accounts: [
          {
            account_id: 'acct_cop1',
            health: { auth_type: 'api_key', status: 'ready' },
            quota: { status: 'unsupported', windows: [] },
            routing: { priority: 0, request_count: 0 }
          }
        ],
        provider: 'copilot',
        routing: { cooldown: 0, error: 0, expired: 0, ready: 1, unavailable: 0 },
        usage_capability: 'unsupported'
      },
      {
        accounts: [
          {
            account_id: 'acct_kimi1',
            display_name: 'Kimi 1',
            health: { auth_type: 'api_key', status: 'ready' },
            quota: {
              fetched_at: '2026-08-10T04:00:00Z',
              source: 'provider_reported',
              status: 'available',
              windows: [{ label: 'Weekly', used_percent: 25 }]
            },
            routing: { priority: 0, request_count: 1 }
          }
        ],
        provider: 'kimi-coding',
        routing: { cooldown: 0, error: 0, expired: 0, ready: 1, unavailable: 0 },
        usage_capability: 'supported'
      },
      {
        accounts: [
          {
            account_id: 'acct_ds1',
            health: { auth_type: 'api_key', status: 'ready' },
            quota: { status: 'unsupported', windows: [] },
            routing: { priority: 0, request_count: 0 }
          }
        ],
        provider: 'deepseek',
        routing: { cooldown: 0, error: 0, expired: 0, ready: 1, unavailable: 0 },
        usage_capability: 'unsupported'
      }
    ]
    return base
  }

  it('pins the current session provider with a badge and hides unsupported providers entirely', () => {
    render(<AccountLimitsView contract={orderedContract()} />)

    const kimi = screen.getByText('kimi-coding')
    expect(kimi.parentElement?.textContent).toContain('Current')

    // Unused providers leave no trace in the quick layer — no rows, no
    // disclosure, no repeated "does not report" notes.
    expect(screen.queryByText('copilot')).toBeNull()
    expect(screen.queryByText('deepseek')).toBeNull()
    expect(screen.queryByRole('button', { name: /Other providers/ })).toBeNull()
    expect(screen.queryByText(/No usage reporting/)).toBeNull()
  })

  it('shows one quiet line only when every configured provider is hidden', () => {
    const contract = orderedContract()
    contract.local = { status: 'unavailable' }
    contract.providers = contract.providers.filter(p => p.usage_capability === 'unsupported')

    render(<AccountLimitsView contract={contract} />)

    expect(screen.getByText(/2 configured providers don't report usage/)).toBeTruthy()
    expect(screen.queryByText('copilot')).toBeNull()
  })

  it('renders a health icon next to each account status', () => {
    const { container } = render(<AccountLimitsView contract={orderedContract()} />)

    const badge = container.querySelector('[data-slot="health-badge"]')
    expect(badge).toBeTruthy()
    expect(badge?.querySelector('svg')).toBeTruthy()
    expect(badge?.textContent).toContain('Ready')
  })

  it('renders freshness and reset as relative time', () => {
    const contract = makeContract()
    const now = Date.now()
    contract.providers[0].accounts[0].quota.fetched_at = new Date(now - 90_000).toISOString()
    contract.providers[0].accounts[0].quota.stale = true
    contract.providers[0].accounts[0].quota.windows = [
      { label: 'Weekly', reset_at: new Date(now + 3 * 86_400_000).toISOString(), used_percent: 40 }
    ]

    render(<AccountLimitsView contract={contract} />)

    // Intl.RelativeTimeFormat localizes ("2 min. ago" / "2分钟前") — assert
    // relative semantics in a locale-agnostic way.
    expect(screen.getByRole('status').textContent).toMatch(/ago|前/)
    expect(screen.getByText(/Resets in 3 days|3天后/)).toBeTruthy()
  })

  it('colors the quota bar by remaining threshold: default → amber <20% → red <5%', () => {
    const contract = makeContract()
    contract.providers[0].accounts[0].quota.windows = [
      { label: 'Comfortable', used_percent: 40 },
      { label: 'Low', used_percent: 85 },
      { label: 'Critical', used_percent: 97 }
    ]

    render(<AccountLimitsView contract={contract} />)

    const comfortable = screen.getByRole('progressbar', { name: 'Comfortable: 60% remaining' })
    const low = screen.getByRole('progressbar', { name: 'Low: 15% remaining' })
    const critical = screen.getByRole('progressbar', { name: 'Critical: 3% remaining' })

    expect(comfortable.querySelector('[class*="bg-amber-500"]')).toBeNull()
    expect(comfortable.querySelector('[class*="bg-destructive"]')).toBeNull()
    expect(low.querySelector('[class*="bg-amber-500"]')).toBeTruthy()
    expect(critical.querySelector('[class*="bg-destructive"]')).toBeTruthy()
  })
})
