import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  usageMeterDetailsFixture,
  usageMeterRecentFixture,
  usageMeterSummaryFixture,
  usageOverviewFixture
} from './fixtures.test-util'

import { UsageView } from './index'

const gatewayMock = vi.hoisted(() => vi.fn())

function responseFor(method: string, params: Record<string, unknown>) {
  if (method === 'usage.overview') {
    return { ...usageOverviewFixture, days: Number(params.days) }
  }

  if (method === 'usage.meter.summary') {
    return usageMeterSummaryFixture
  }

  if (method === 'usage.meter.details') {
    return {
      ...usageMeterDetailsFixture,
      end_ts: params.scope === 'month' ? usageMeterSummaryFixture.month_end_ts : null,
      scope: params.scope,
      start_ts: params.scope === 'month' ? usageMeterSummaryFixture.month_start_ts : null
    }
  }

  if (method === 'usage.meter.recent') {
    return usageMeterRecentFixture
  }

  throw new Error(`Unexpected RPC: ${method}`)
}

function renderUsage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } }
  })

  render(
    <QueryClientProvider client={client}>
      <UsageView requestGateway={gatewayMock} />
    </QueryClientProvider>
  )

  return client
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn()
  })
  gatewayMock.mockImplementation((method: string, params: Record<string, unknown>) =>
    Promise.resolve(responseFor(method, params))
  )
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('UsageView', () => {
  it('loads all telemetry surfaces and renders macro plus cost-truth data', async () => {
    renderUsage()

    expect(await screen.findByRole('heading', { name: 'Usage deck' })).toBeTruthy()
    expect(await screen.findByText('1.7M')).toBeTruthy()
    expect(screen.getAllByText('$32.11').length).toBeGreaterThan(0)
    expect(screen.getAllByText('$34.87').length).toBeGreaterThan(0)
    expect(screen.getByText('Session provider actual')).toBeTruthy()
    expect(screen.getByText('Price unavailable')).toBeTruthy()

    await waitFor(() => {
      expect(gatewayMock).toHaveBeenCalledWith('usage.overview', { days: 30 })
      expect(gatewayMock).toHaveBeenCalledWith('usage.meter.summary', {})
    })
    expect(gatewayMock).not.toHaveBeenCalledWith('usage.meter.details', expect.anything())
    expect(gatewayMock).not.toHaveBeenCalledWith('usage.meter.recent', expect.anything())
  })

  it('changes the session insight period without conflating the meter scope', async () => {
    renderUsage()
    await screen.findByText('Usage deck')

    fireEvent.click(screen.getByRole('button', { name: '90d' }))

    await waitFor(() => expect(gatewayMock).toHaveBeenCalledWith('usage.overview', { days: 90 }))
    expect(gatewayMock).toHaveBeenCalledWith('usage.meter.summary', {})
  })

  it('sorts and filters the route matrix, then drills into matching captured calls', async () => {
    renderUsage()
    fireEvent.click(await screen.findByRole('button', { name: 'Routes' }))
    expect(await screen.findByRole('heading', { name: 'Route matrix' })).toBeTruthy()
    expect(await screen.findByText('Hermes-4-405B')).toBeTruthy()

    fireEvent.change(screen.getByRole('textbox', { name: 'Search usage routes' }), {
      target: { value: 'openai' }
    })
    expect(screen.queryByText('Hermes-4-405B')).toBeNull()
    expect(screen.getByText('gpt-5.6-sol')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Inspect recent calls for openai-codex\/gpt-5.6-sol/ }))

    expect(await screen.findByRole('heading', { name: 'Captured call ledger' })).toBeTruthy()
    await waitFor(() => expect(gatewayMock).toHaveBeenCalledWith('usage.meter.recent', { limit: 500 }))
    expect(screen.getAllByText('gpt-5.6-sol').length).toBeGreaterThan(0)
    expect(screen.queryByText('Hermes-4-405B')).toBeNull()
  })

  it('micromanages the call ledger with profile filters and full disclosure details', async () => {
    renderUsage()
    fireEvent.click(await screen.findByRole('button', { name: 'Call ledger' }))

    expect(await screen.findByText('gpt-5.6-sol')).toBeTruthy()
    fireEvent.click(screen.getByRole('combobox', { name: 'Profile filter' }))
    fireEvent.click(await screen.findByRole('option', { name: 'research' }))

    expect(screen.queryByText('gpt-5.6-sol')).toBeNull()
    expect(screen.getByText('Hermes-4-405B')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Hermes-4-405B/ }))
    expect(screen.getByText('Session ID')).toBeTruthy()
    expect(screen.getByText('20260811_161955_bb22cc')).toBeTruthy()
    expect(screen.getByText('pricing_catalog')).toBeTruthy()
    expect(screen.getByText('41,000')).toBeTruthy()
  })

  it('renders a production-shaped empty overview without fabricating activity', async () => {
    gatewayMock.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === 'usage.overview') {
        return Promise.resolve({
          ...usageOverviewFixture,
          activity: { by_hour: [] },
          daily_series: [],
          empty: true,
          models: [],
          overview: {},
          platforms: [],
          skills: {
            summary: {
              distinct_skills_used: 0,
              total_skill_actions: 0,
              total_skill_edits: 0,
              total_skill_loads: 0
            },
            top_skills: []
          },
          tools: [],
          top_sessions: []
        })
      }

      return Promise.resolve(responseFor(method, params))
    })

    renderUsage()

    expect(await screen.findByText('No daily activity in this range.')).toBeTruthy()
    expect(screen.getByText('No model traffic in this range.')).toBeTruthy()
    expect(screen.getByText('No platform traffic in this range.')).toBeTruthy()
  })

  it('labels month route drilldown as a bounded recent-event window', async () => {
    renderUsage()
    fireEvent.click(await screen.findByRole('button', { name: 'Routes' }))
    fireEvent.click(await screen.findByRole('button', { name: 'This month' }))
    await waitFor(() => expect(gatewayMock).toHaveBeenCalledWith('usage.meter.details', { scope: 'month' }))
    fireEvent.click(await screen.findByRole('button', { name: /Inspect recent calls for openai-codex\/gpt-5.6-sol/ }))

    expect(await screen.findByText(/Month scope is applied to the newest captured window/)).toBeTruthy()
  })

  it('searches numeric event identifiers from the live ledger contract', async () => {
    renderUsage()
    fireEvent.click(await screen.findByRole('button', { name: 'Call ledger' }))
    const search = await screen.findByRole('textbox', { name: 'Search captured calls' })
    fireEvent.change(search, { target: { value: '302' } })

    expect(await screen.findByText('Hermes-4-405B')).toBeTruthy()
    expect(screen.queryByText('gpt-5.6-sol')).toBeNull()
  })

  it('keeps session analytics usable when the optional installation meter fails', async () => {
    gatewayMock.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === 'usage.meter.summary') {
        return Promise.reject(new Error('meter disabled'))
      }

      return Promise.resolve(responseFor(method, params))
    })

    renderUsage()

    expect(await screen.findByText('ledger degraded')).toBeTruthy()
    expect(screen.getByText('install meter unavailable')).toBeTruthy()
    expect(screen.getByText('Model pressure stack')).toBeTruthy()
  })

  it('synchronizes all four data surfaces on demand', async () => {
    renderUsage()
    await screen.findByText('Usage deck')
    await waitFor(() => expect(gatewayMock).toHaveBeenCalledTimes(2))

    fireEvent.click(await screen.findByRole('button', { name: 'Sync' }))

    await waitFor(() => expect(gatewayMock.mock.calls.length).toBeGreaterThanOrEqual(6))
  })
})
