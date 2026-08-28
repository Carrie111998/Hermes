import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { I18nProvider } from '@/i18n'
import type { HermesConfigRecord } from '@/types/hermes'

const saveHermesConfig = vi.fn((_config: HermesConfigRecord) => Promise.resolve({ ok: true }))
const getHermesConfigRecord = vi.fn<() => Promise<HermesConfigRecord>>(() => Promise.resolve({}))

vi.mock('@/hermes', async () => {
  const actual = await vi.importActual<typeof HermesApi>('@/hermes')

  return { ...actual, getHermesConfigRecord: () => getHermesConfigRecord(), saveHermesConfig: (c: HermesConfigRecord) => saveHermesConfig(c) }
})

const { DelegationToggle } = await import('./delegation-toggle')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  getHermesConfigRecord.mockResolvedValue({})
})

function renderToggle() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <I18nProvider configClient={null} initialLocale="en">
        <DelegationToggle disabled={false} />
      </I18nProvider>
    </QueryClientProvider>
  )
}

const button = () => screen.getByRole('button')

describe('DelegationToggle', () => {
  it('reads as off when the key has never been written', async () => {
    renderToggle()

    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('false'))
  })

  it('reads as on when the config says so', async () => {
    getHermesConfigRecord.mockResolvedValue({ delegate_wave: { route_repo_changes: true } } as HermesConfigRecord)
    renderToggle()

    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('true'))
  })

  it('writes the nested key the backend reads, not a flat one', async () => {
    renderToggle()
    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('false'))

    button().click()

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(expect.objectContaining({ delegate_wave: { route_repo_changes: true } }))
    )
  })

  it('turns the policy back off from an on state', async () => {
    getHermesConfigRecord.mockResolvedValue({ delegate_wave: { route_repo_changes: true } } as HermesConfigRecord)
    renderToggle()
    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('true'))

    button().click()

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(expect.objectContaining({ delegate_wave: { route_repo_changes: false } }))
    )
  })

  it('renders nothing until the config has loaded, rather than guessing a state', () => {
    getHermesConfigRecord.mockReturnValue(new Promise(() => {}))
    renderToggle()

    expect(screen.queryByRole('button')).toBeNull()
  })
})
