import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ContextBreakdown, UsageAccountsContract, UsageStats } from '@/types/hermes'

import { ContextUsagePanel } from './context-usage-panel'

const initialUsage: UsageStats = {
  calls: 1,
  context_max: 272_000,
  context_percent: 47,
  context_used: 128_200,
  input: 0,
  output: 0,
  total: 0
}

const breakdown: ContextBreakdown = {
  categories: [{ color: 'teal', id: 'conversation', label: 'Conversation', tokens: 241_400 }],
  context_max: 272_000,
  context_percent: 89,
  context_used: 241_400,
  estimated_total: 286_600,
  model: 'test-model'
}

const usageAccounts: UsageAccountsContract = {
  capabilities: {
    credential_pool_health: true,
    local_session_analytics: true,
    provider_usage: { per_account: true, providers: ['openai-codex'] }
  },
  contract: { name: 'usage.accounts', version: 1 },
  generated_at: '2026-08-10T00:00:00Z',
  local: {
    calls: 2,
    model: 'runtime-model',
    provider: 'openai-codex',
    status: 'available',
    tokens: { input: 100, output: 40, total: 140 }
  },
  providers: [
    {
      accounts: [
        {
          account_id: 'acct_alpha',
          health: { auth_type: 'oauth', status: 'ready' },
          quota: {
            status: 'available',
            windows: [{ label: 'Weekly', used_percent: 40 }]
          },
          routing: { priority: 0, request_count: 3 }
        },
        {
          account_id: 'acct_beta',
          health: { auth_type: 'oauth', status: 'cooldown' },
          quota: { status: 'unavailable', windows: [] },
          routing: { priority: 1, request_count: 5 }
        }
      ],
      provider: 'openai-codex',
      routing: { cooldown: 1, error: 0, expired: 0, ready: 1, unavailable: 0 },
      usage_capability: 'supported'
    },
    {
      accounts: [
        {
          account_id: 'acct_local',
          health: { auth_type: 'api_key', status: 'ready' },
          quota: { status: 'unsupported', windows: [] },
          routing: { priority: 0, request_count: 0 }
        }
      ],
      provider: 'local-provider',
      routing: { cooldown: 0, error: 0, expired: 0, ready: 1, unavailable: 0 },
      usage_capability: 'unsupported'
    }
  ]
}

function gatewayResponse<T = unknown>(method: string): Promise<T> {
  return Promise.resolve((method === 'usage.accounts' ? usageAccounts : breakdown) as T)
}

function missingMethodResponse<T = unknown>(method: string): Promise<T> {
  if (method === 'usage.accounts') {
    return Promise.reject(new Error('-32601 method not found'))
  }

  return Promise.resolve(breakdown as T)
}

function failedRequestResponse<T = unknown>(method: string): Promise<T> {
  if (method === 'usage.accounts') {
    return Promise.reject(new Error('provider request failed'))
  }

  return Promise.resolve(breakdown as T)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ContextUsagePanel', () => {
  it('publishes once without refetching when publication recreates the callback', async () => {
    const requestGateway = vi.fn(gatewayResponse)
    const published = vi.fn()
    const renderedUsage: UsageStats[] = []

    function Harness() {
      const [currentUsage, setCurrentUsage] = useState(initialUsage)
      renderedUsage.push(currentUsage)

      return (
        <ContextUsagePanel
          currentUsage={currentUsage}
          onUsageSnapshot={snapshot => {
            published(snapshot)
            setCurrentUsage(current => ({ ...current, ...snapshot }))
          }}
          requestGateway={requestGateway as never}
          sessionId="runtime-1"
        />
      )
    }

    render(<Harness />)

    await waitFor(() => {
      expect(published).toHaveBeenCalledWith({
        context_max: 272_000,
        context_percent: 89,
        context_used: 241_400
      })
      expect(renderedUsage.at(-1)?.context_used).toBe(241_400)
    })
    await act(async () => {})

    expect(requestGateway.mock.calls.filter(([method]) => method === 'session.context_breakdown')).toHaveLength(1)
    expect(requestGateway).toHaveBeenCalledWith('session.context_breakdown', { session_id: 'runtime-1' })
    expect(requestGateway).toHaveBeenCalledWith('usage.accounts', { refresh: true, session_id: 'runtime-1' })
  })

  it('refetches when the session or gateway requester changes', async () => {
    const firstGateway = vi.fn(gatewayResponse)
    const secondGateway = vi.fn(gatewayResponse)

    const { rerender } = render(
      <ContextUsagePanel currentUsage={initialUsage} requestGateway={firstGateway as never} sessionId="runtime-1" />
    )

    await waitFor(() =>
      expect(firstGateway.mock.calls.filter(([method]) => method === 'session.context_breakdown')).toHaveLength(1)
    )

    rerender(
      <ContextUsagePanel currentUsage={initialUsage} requestGateway={firstGateway as never} sessionId="runtime-2" />
    )

    await waitFor(() => {
      expect(firstGateway.mock.calls.filter(([method]) => method === 'session.context_breakdown')).toHaveLength(2)
      expect(firstGateway).toHaveBeenCalledWith('session.context_breakdown', { session_id: 'runtime-2' })
    })

    rerender(
      <ContextUsagePanel currentUsage={initialUsage} requestGateway={secondGateway as never} sessionId="runtime-2" />
    )

    await waitFor(() =>
      expect(secondGateway.mock.calls.filter(([method]) => method === 'session.context_breakdown')).toHaveLength(1)
    )
  })

  it('renders dynamic providers, separated accounts, health, quota, and local analytics', async () => {
    const requestGateway = vi.fn(gatewayResponse)

    render(
      <ContextUsagePanel
        currentUsage={initialUsage}
        profile="work"
        requestGateway={requestGateway as never}
        sessionId="runtime-1"
      />
    )

    expect(await screen.findByText('openai-codex')).toBeTruthy()
    expect(screen.getByText('Account lpha')).toBeTruthy()
    expect(screen.getByText('Account beta')).toBeTruthy()
    expect(screen.getByText('60% remaining')).toBeTruthy()
    expect(screen.getByText('Cooling down')).toBeTruthy()
    // Unsupported providers are hidden entirely in the quick layer — no row,
    // no disclosure — as long as at least one provider has a real signal.
    expect(screen.queryByText('local-provider')).toBeNull()
    expect(screen.queryByRole('button', { name: /Other providers/ })).toBeNull()
    expect(screen.queryByText(/don't report usage/)).toBeNull()
    expect(screen.getByText('openai-codex · runtime-model')).toBeTruthy()
    expect(screen.getByText('2 calls · 140 tokens')).toBeTruthy()
    expect(requestGateway).toHaveBeenCalledWith('usage.accounts', {
      profile: 'work',
      refresh: true,
      session_id: 'runtime-1'
    })
  })

  it('renders loading, empty, and unavailable states without inventing data', async () => {
    let resolveAccounts: (value: UsageAccountsContract) => void = () => undefined
    const requestGateway = vi.fn((method: string) => {
      if (method === 'usage.accounts') {
        return new Promise<UsageAccountsContract>(resolve => {
          resolveAccounts = resolve
        })
      }

      return Promise.resolve(breakdown)
    })

    render(<ContextUsagePanel currentUsage={initialUsage} requestGateway={requestGateway as never} sessionId={null} />)

    expect(screen.getByRole('status', { name: 'Loading account usage' })).toBeTruthy()

    await act(async () => {
      resolveAccounts({ ...usageAccounts, local: { status: 'unavailable' }, providers: [] })
    })

    expect(await screen.findByText('No local session analytics are available')).toBeTruthy()
    expect(screen.getByText('No accounts are configured')).toBeTruthy()
  })

  it('distinguishes an older unsupported backend from a provider request error', async () => {
    const missingMethod = vi.fn(missingMethodResponse)

    const { rerender } = render(
      <ContextUsagePanel currentUsage={initialUsage} requestGateway={missingMethod as never} sessionId="runtime-1" />
    )

    expect(await screen.findByText('Account usage is not supported by this backend')).toBeTruthy()

    const failedRequest = vi.fn(failedRequestResponse)
    rerender(
      <ContextUsagePanel currentUsage={initialUsage} requestGateway={failedRequest as never} sessionId="runtime-1" />
    )

    expect(await screen.findByText('Account usage could not be loaded')).toBeTruthy()
  })
})
