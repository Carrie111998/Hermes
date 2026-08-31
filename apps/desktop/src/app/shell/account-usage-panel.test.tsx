import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $providerUsage, _resetProviderUsageForTests, refreshProviderUsage } from '@/store/provider-usage'
import type { ProviderUsageSnapshot } from '@/types/hermes'

import { AccountUsagePanel } from './account-usage-panel'

const claude: ProviderUsageSnapshot = {
  available: true,
  display_name: 'Claude',
  plan: 'Max',
  provider: 'anthropic',
  stale: false,
  state: 'ok',
  windows: [{ label: '5h', unit: 'percent', used: '28', used_percent: 28 }]
}

const openrouter: ProviderUsageSnapshot = {
  available: true,
  display_name: 'OpenRouter',
  provider: 'openrouter',
  stale: false,
  state: 'ok',
  windows: [{ currency: 'USD', label: 'credits', remaining: '12.34', unit: 'currency' }]
}

const copilot: ProviderUsageSnapshot = {
  available: true,
  display_name: 'GitHub Copilot',
  provider: 'copilot',
  stale: false,
  state: 'ok',
  windows: [{ label: 'chat', limit: '200', remaining: '150', unit: 'count', used_percent: 25 }]
}

function seed(providers: ProviderUsageSnapshot[]) {
  $providerUsage.set({ loading: false, providers })
}

afterEach(() => {
  cleanup()
  _resetProviderUsageForTests()
  vi.restoreAllMocks()
})

describe('AccountUsagePanel', () => {
  it('renders every connected subscription, each in its own unit', () => {
    seed([claude, openrouter, copilot])
    const view = render(<AccountUsagePanel />)

    expect(view.container.querySelectorAll('[data-slot="account-usage-provider"]')).toHaveLength(3)
    // percent stays percent, money keeps its cents, a count stays a count —
    // none of them coerced into one another.
    expect(screen.getByText('28%')).not.toBeNull()
    expect(screen.getByText('$12.34')).not.toBeNull()
    expect(screen.getByText('150 / 200')).not.toBeNull()
  })

  it('draws a bar only where the backend derived a percentage', () => {
    seed([openrouter])
    const view = render(<AccountUsagePanel />)

    // Credits with no limit have no meaningful percentage; a bar would be a
    // number we invented.
    expect(view.container.querySelectorAll('[data-slot="account-usage-bar"]')).toHaveLength(0)
  })

  it('shows a translated reason instead of a raw provider message', () => {
    seed([{ ...claude, message: 'HTTP 401 from api.anthropic.com', state: 'unauthorized', windows: [] }])
    render(<AccountUsagePanel />)

    expect(screen.getByText('Sign-in expired')).not.toBeNull()
    expect(screen.queryByText(/HTTP 401/)).toBeNull()
  })

  it('renders one provider failing next to another succeeding', () => {
    seed([{ ...claude, state: 'network_error', windows: [] }, copilot])
    const view = render(<AccountUsagePanel />)

    expect(screen.getByText('Could not reach the provider')).not.toBeNull()
    expect(screen.getByText('150 / 200')).not.toBeNull()
    expect(view.container.querySelectorAll('[data-slot="account-usage-provider"]')).toHaveLength(2)
  })

  it('says so when nothing is connected', () => {
    render(<AccountUsagePanel />)

    expect(screen.getByText('No connected subscriptions')).not.toBeNull()
  })
})

describe('refreshProviderUsage', () => {
  // The store only issues the RPC when it believes it is inside the desktop
  // shell, so every case here needs the marker present.
  beforeEach(() => {
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: {} })
  })

  it('keeps the previous numbers when the call fails', async () => {
    seed([claude])
    const request = vi.fn().mockRejectedValue(new Error('socket closed'))
    const { $gateway } = await import('@/store/gateway')
    $gateway.set({ request } as never)

    await refreshProviderUsage()

    // Blanking the panel on a dropped socket is worse than slightly old data.
    expect($providerUsage.get().providers).toEqual([claude])
    expect($providerUsage.get().loading).toBe(false)
    $gateway.set(null as never)
  })

  it('passes the force flag through as the refresh param', async () => {
    const request = vi.fn().mockResolvedValue({ providers: [claude] })
    const { $gateway } = await import('@/store/gateway')
    $gateway.set({ request } as never)

    await refreshProviderUsage({ force: true })

    await waitFor(() => expect(request).toHaveBeenCalledWith('account.usage', { refresh: true }))
    $gateway.set(null as never)
  })
})
