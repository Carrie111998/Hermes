import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AnalyticsResponse, UsageAccountsContract } from '@/types/hermes'

import { UsagePanel } from './index'

const usageAccounts: UsageAccountsContract = {
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
            fetched_at: '2026-08-10T04:00:00Z',
            plan: 'Pro',
            source: 'provider_reported',
            status: 'available',
            windows: [{ label: 'Weekly', used_percent: 25 }]
          },
          routing: { priority: 0, request_count: 1 }
        }
      ],
      provider: 'openai-codex',
      routing: { cooldown: 0, error: 0, expired: 0, ready: 1, unavailable: 0 },
      usage_capability: 'supported'
    }
  ]
}

function makeAnalytics(overrides?: Partial<AnalyticsResponse>): AnalyticsResponse {
  return {
    by_model: [
      {
        api_calls: 3,
        estimated_cost: 0.5,
        input_tokens: 800,
        model: 'kimi-k3',
        output_tokens: 200,
        sessions: 2
      }
    ],
    by_provider: [
      {
        api_calls: 3,
        estimated_cost: 0.5,
        input_tokens: 800,
        output_tokens: 200,
        provider: 'kimi-coding',
        sessions: 2
      }
    ],
    by_task: [
      {
        api_calls: 1,
        estimated_cost: 0.1,
        input_tokens: 500,
        models: ['gemini-3-flash'],
        output_tokens: 50,
        task: 'vision'
      }
    ],
    daily: [],
    period_days: 30,
    skills: {
      summary: { distinct_skills_used: 0, total_skill_actions: 0, total_skill_edits: 0, total_skill_loads: 0 },
      top_skills: []
    },
    totals: {
      total_actual_cost: 0,
      total_api_calls: 3,
      total_cache_read: 0,
      total_estimated_cost: 0.5,
      total_input: 800,
      total_output: 200,
      total_reasoning: 0,
      total_sessions: 2
    },
    ...overrides
  }
}

function usageAccountsGateway<T = unknown>(method: string): Promise<T> {
  if (method === 'usage.accounts') {
    return Promise.resolve(usageAccounts as T)
  }
  return Promise.reject(new Error(`unexpected method ${method}`))
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('UsagePanel', () => {
  it('renders account limits plus Provider / Model / Task sections from real fields', async () => {
    const requestGateway = vi.fn(usageAccountsGateway)

    render(
      <UsagePanel
        error=""
        loading={false}
        onRefresh={() => undefined}
        period={30}
        requestGateway={requestGateway as never}
        sessionId="runtime-1"
        usage={makeAnalytics()}
      />
    )

    expect(await screen.findByText('Codex 1')).toBeTruthy()
    expect(screen.getByText('Account Limits')).toBeTruthy()
    expect(screen.getByText('75% remaining')).toBeTruthy()

    expect(screen.getByText('Provider')).toBeTruthy()
    expect(screen.getByText('kimi-coding')).toBeTruthy()
    expect(screen.getByText('Top models')).toBeTruthy()
    expect(screen.getByText('kimi-k3')).toBeTruthy()
    expect(screen.getByText('Task')).toBeTruthy()
    expect(screen.getByText('vision')).toBeTruthy()
  })

  it('keeps analytics visible when the account limits request fails', async () => {
    const requestGateway = vi.fn(() => Promise.reject(new Error('provider request failed')))

    render(
      <UsagePanel
        error=""
        loading={false}
        onRefresh={() => undefined}
        period={30}
        requestGateway={requestGateway as never}
        sessionId="runtime-1"
        usage={makeAnalytics()}
      />
    )

    expect(await screen.findByText('Account usage could not be loaded')).toBeTruthy()
    expect(screen.getByText('kimi-coding')).toBeTruthy()
    expect(screen.getByText('kimi-k3')).toBeTruthy()
  })

  it('keeps account limits visible when analytics failed', async () => {
    const requestGateway = vi.fn(usageAccountsGateway)

    render(
      <UsagePanel
        error="analytics backend offline"
        loading={false}
        onRefresh={() => undefined}
        period={30}
        requestGateway={requestGateway as never}
        sessionId="runtime-1"
        usage={null}
      />
    )

    expect(await screen.findByText('Codex 1')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('analytics backend offline')
  })

  it('degrades safely against an older backend without by_provider/by_task', async () => {
    const requestGateway = vi.fn(usageAccountsGateway)
    const legacy = makeAnalytics()
    delete legacy.by_provider
    delete legacy.by_task

    render(
      <UsagePanel
        error=""
        loading={false}
        onRefresh={() => undefined}
        period={30}
        requestGateway={requestGateway as never}
        sessionId="runtime-1"
        usage={legacy}
      />
    )

    expect(await screen.findByText('Codex 1')).toBeTruthy()
    expect(screen.getByText('kimi-k3')).toBeTruthy()
    expect(screen.getByText('No provider usage recorded.')).toBeTruthy()
    expect(screen.getByText('No auxiliary task usage recorded.')).toBeTruthy()
  })

  it('refreshes account limits independently of analytics', async () => {
    const requestGateway = vi.fn(usageAccountsGateway)

    render(
      <UsagePanel
        error=""
        loading={false}
        onRefresh={() => undefined}
        period={30}
        requestGateway={requestGateway as never}
        sessionId="runtime-1"
        usage={makeAnalytics()}
      />
    )

    expect(await screen.findByText('Codex 1')).toBeTruthy()
    expect(requestGateway).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(2))
    expect(requestGateway).toHaveBeenLastCalledWith('usage.accounts', {
      refresh: true,
      session_id: 'runtime-1'
    })
  })
})
