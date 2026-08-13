import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $visibleModels } from '@/store/model-visibility'

const getGlobalModelOptions = vi.fn()
const saveDelegationRoutes = vi.fn()
const setConfigCache = vi.fn()
let config: Record<string, unknown> = {}

vi.mock('@/hermes', () => ({
  getApiRequestProfile: () => 'default',
  getGlobalModelOptions: (...args: unknown[]) => getGlobalModelOptions(...args),
  saveDelegationRoutes: (routes: unknown) => saveDelegationRoutes(routes),
  setApiRequestProfile: vi.fn()
}))

vi.mock('../hooks/use-config-record', () => ({
  setHermesConfigCache: (next: Record<string, unknown>) => setConfigCache(next),
  useHermesConfigRecord: () => ({ data: config })
}))

vi.mock('../hooks/use-on-profile-switch', () => ({
  useOnProfileSwitch: () => {}
}))

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  $visibleModels.set(null)
  config = {
    delegation: {
      max_spawn_depth: 3,
      routes: {
        careful: {
          max_output_tokens: 12000,
          model: 'gemini-3.1-pro',
          provider: 'google',
          reasoning_effort: 'high',
          request_overrides: { temperature: 0 }
        }
      }
    }
  }
  getGlobalModelOptions.mockResolvedValue({
    providers: [
      {
        capabilities: { 'gemini-3.1-pro': { fast: false, reasoning: true } },
        models: ['gemini-3.1-pro', 'gemini-2.5-flash'],
        name: 'Google',
        slug: 'google'
      }
    ]
  })
  saveDelegationRoutes.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function renderSettings() {
  const { DelegationRoutesSettings } = await import('./delegation-routes-settings')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <DelegationRoutesSettings />
    </QueryClientProvider>
  )
}

describe('DelegationRoutesSettings', () => {
  it('renders configured routes with model, provider, and reasoning', async () => {
    await renderSettings()

    expect(await screen.findByText('careful')).toBeTruthy()
    expect(screen.getByText(/google: gemini-3.1-pro/)).toBeTruthy()
    expect(screen.getByText(/High/)).toBeTruthy()
  })

  it('preserves advanced fields that the compact editor does not expose', async () => {
    const { delegationRoutesFromConfig } = await import('./delegation-routes-settings')

    expect(delegationRoutesFromConfig(config).careful).toEqual({
      max_output_tokens: 12000,
      model: 'gemini-3.1-pro',
      provider: 'google',
      reasoning_effort: 'high',
      request_overrides: { temperature: 0 }
    })
  })

  it('replaces the route map when a route is removed', async () => {
    await renderSettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Remove careful' }))

    await waitFor(() => expect(saveDelegationRoutes).toHaveBeenCalledWith({}))
    expect(setConfigCache).toHaveBeenCalledWith({
      delegation: { max_spawn_depth: 3, routes: {} }
    })
  })

  it('creates a route through the shared model catalog', async () => {
    config = { delegation: { routes: {} } }
    await renderSettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Add route' }))
    fireEvent.change(screen.getByLabelText('Route alias'), { target: { value: 'fast' } })
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Select a model' }), { button: 0 })
    fireEvent.click(await screen.findByText(/Gemini 3\.1 Pro/i))
    expect(screen.getByRole('button', { name: /google: gemini-3.1-pro/i })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save route' }))

    await waitFor(() =>
      expect(saveDelegationRoutes).toHaveBeenCalledWith({
        fast: {
          model: 'gemini-3.1-pro',
          provider: 'google'
        }
      })
    )
  })
})
