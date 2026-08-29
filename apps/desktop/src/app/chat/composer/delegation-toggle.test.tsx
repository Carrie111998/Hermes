import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import type { ProfileScope } from '@/hermes'
import { I18nProvider } from '@/i18n'
import type { HermesConfigRecord } from '@/types/hermes'

const saveHermesConfig = vi.fn((_config: HermesConfigRecord, _profile?: ProfileScope) => Promise.resolve({ ok: true }))
const getHermesConfigRecord = vi.fn((_profile?: ProfileScope) => Promise.resolve<HermesConfigRecord>({}))

vi.mock('@/hermes', async () => {
  const actual = await vi.importActual<typeof HermesApi>('@/hermes')

  return {
    ...actual,
    getHermesConfigRecord: (profile?: ProfileScope) => getHermesConfigRecord(profile),
    saveHermesConfig: (config: HermesConfigRecord, profile?: ProfileScope) => saveHermesConfig(config, profile)
  }
})

const { DelegationToggle } = await import('./delegation-toggle')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  getHermesConfigRecord.mockResolvedValue({})
})

function renderToggle(profileScope?: ProfileScope, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {

  return render(
    <QueryClientProvider client={client}>
      <I18nProvider configClient={null} initialLocale="en">
        <DelegationToggle disabled={false} profileScope={profileScope} />
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
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({ delegate_wave: { route_repo_changes: true } }),
        undefined
      )
    )
  })

  it('turns the policy back off from an on state', async () => {
    getHermesConfigRecord.mockResolvedValue({ delegate_wave: { route_repo_changes: true } } as HermesConfigRecord)
    renderToggle()
    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('true'))

    button().click()

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({ delegate_wave: { route_repo_changes: false } }),
        undefined
      )
    )
  })

  it('rolls back when the backend rejects the config without throwing', async () => {
    saveHermesConfig.mockResolvedValueOnce({ ok: false })
    renderToggle()
    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('false'))

    button().click()

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('false'))
  })

  it('rolls back when saving and the follow-up refetch both fail', async () => {
    saveHermesConfig.mockRejectedValueOnce(new Error('offline'))
    getHermesConfigRecord.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('still offline'))
    renderToggle()
    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('false'))

    button().click()

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    await waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('false'))
  })

  it('renders nothing until the config has loaded, rather than guessing a state', () => {
    getHermesConfigRecord.mockReturnValue(new Promise(() => {}))
    renderToggle()

    expect(screen.queryByRole('button')).toBeNull()
  })

  it('reads and writes the exact owner profile without touching the ambient profile', async () => {
    const ownerA = { connectionId: 'pc-a', profile: 'default' }
    const ownerB = { connectionId: 'pc-b', profile: 'work' }
    getHermesConfigRecord.mockImplementation(async profile =>
      typeof profile === 'object' && profile?.connectionId === 'pc-b'
        ? ({ delegate_wave: { route_repo_changes: false } } as HermesConfigRecord)
        : ({ delegate_wave: { route_repo_changes: true } } as HermesConfigRecord)
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    renderToggle(ownerA, client)
    renderToggle(ownerB, client)
    await waitFor(() => expect(screen.getAllByRole('button')).toHaveLength(2))
    const [buttonA, buttonB] = screen.getAllByRole('button')
    await waitFor(() => expect(buttonA.getAttribute('aria-pressed')).toBe('true'))
    await waitFor(() => expect(buttonB.getAttribute('aria-pressed')).toBe('false'))

    fireEvent.click(buttonB)

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({ delegate_wave: { route_repo_changes: true } }),
        ownerB
      )
    )
    expect(saveHermesConfig).not.toHaveBeenCalledWith(expect.anything(), ownerA)
    expect(buttonA.getAttribute('aria-pressed')).toBe('true')
  })
})
