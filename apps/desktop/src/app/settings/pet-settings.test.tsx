import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $petRoster, setPetRosterEntries, setPetRosterMode } from '@/store/pet-roster'
import { $profiles } from '@/store/profile'
import { $gatewayState } from '@/store/session'
import type { ProfileInfo } from '@/types/hermes'

// The gallery picker reaches for the live gateway; the roster tests don't
// exercise it, so stub the hook + the gallery load it triggers on open.
vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ requestGateway: vi.fn() })
}))

vi.mock('@/store/pet-gallery', async importOriginal => {
  const actual = await importOriginal<typeof import('@/store/pet-gallery')>()

  return { ...actual, loadPetGallery: vi.fn(async () => undefined) }
})

const profile = (name: string): ProfileInfo => ({ name } as ProfileInfo)

async function renderSettings() {
  const { PetSettings } = await import('./pet-settings')

  return render(<PetSettings />)
}

beforeEach(() => {
  $gatewayState.set('closed')
  $petRoster.set({ entries: [], initialized: true, mode: 'follow-active' })
  $profiles.set([])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PetSettings mode toggle (test 49)', () => {
  it('toggles between follow-active and pinned, preserving roster entries', async () => {
    setPetRosterMode('follow-active')
    setPetRosterEntries([{ enabled: true, profile: 'nova' }])
    const initialEntries = $petRoster.get().entries

    await renderSettings()

    // Default is follow-active.
    expect(screen.getByRole('button', { name: 'Follow Active' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Pinned' }).getAttribute('aria-pressed')).toBe('false')

    // Switch to pinned.
    fireEvent.click(screen.getByRole('button', { name: 'Pinned' }))
    expect($petRoster.get().mode).toBe('pinned')

    // Switch back — entries are preserved (just ignored in follow-active).
    fireEvent.click(screen.getByRole('button', { name: 'Follow Active' }))
    expect($petRoster.get().mode).toBe('follow-active')
    expect($petRoster.get().entries).toHaveLength(initialEntries.length)
    expect($petRoster.get().entries[0]?.profile).toBe('nova')
  })

  it('shows the roster panel only in pinned mode', async () => {
    setPetRosterMode('follow-active')
    setPetRosterEntries([{ enabled: true, profile: 'nova' }])

    const { rerender } = await renderSettings()

    expect(screen.queryByText('Pinned profiles')).toBeNull()

    act(() => {
      setPetRosterMode('pinned')
    })
    const { PetSettings } = await import('./pet-settings')
    rerender(<PetSettings />)

    expect(screen.getByText('Pinned profiles')).toBeTruthy()
    expect(screen.getByText('nova')).toBeTruthy()
  })
})

describe('PetSettings pinned roster (test 63)', () => {
  it('renders an unavailable row for a renamed/deleted profile and a disabled row for a new one', async () => {
    setPetRosterMode('pinned')
    // "sol" was pinned but is gone from the catalog (renamed/deleted); reconcile
    // would have marked it unavailable. "lune" is a fresh catalog profile not yet
    // in the roster.
    setPetRosterEntries([{ enabled: false, profile: 'sol', unavailable: true }])
    $profiles.set([profile('lune')])

    await renderSettings()

    // The unavailable row shows the "not found" affordance.
    expect(screen.getByText('sol')).toBeTruthy()
    expect(screen.getByText('Not found')).toBeTruthy()
    expect(screen.getByText(/Profile not found/)).toBeTruthy()

    // The new catalog profile appears as a discoverable, not-yet-pinned row.
    expect(screen.getByText('lune')).toBeTruthy()
  })

  it('enabling a discovered profile pins it to the roster', async () => {
    setPetRosterMode('pinned')
    setPetRosterEntries([])
    $profiles.set([profile('lune')])

    await renderSettings()

    fireEvent.click(screen.getByRole('button', { name: 'Enable' }))

    expect($petRoster.get().entries.some(e => e.profile === 'lune' && e.enabled)).toBe(true)
  })
})
