import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { UsageAccountsContract } from '@/types/hermes'

import { AccountLimitsView, useUsageAccounts } from './account-limits'

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

  it('refresh re-requests and a superseded earlier response cannot overwrite a newer one', async () => {
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
    v2.providers[0].accounts[0].display_name = 'Codex Updated'

    // Resolve the initial load, then trigger a refresh.
    await act(async () => {
      resolvers[0](v1)
    })
    expect(await screen.findByText('Codex 1')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(resolvers).toHaveLength(2))

    // Newer response lands first...
    await act(async () => {
      resolvers[1](v2)
    })
    expect(await screen.findByText('Codex Updated')).toBeTruthy()

    // ...then the superseded earlier refresh response arrives late and must be dropped.
    await act(async () => {
      resolvers[0](v1)
    })
    expect(screen.getByText('Codex Updated')).toBeTruthy()
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
