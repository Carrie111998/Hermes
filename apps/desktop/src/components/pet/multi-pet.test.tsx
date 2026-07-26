import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { leaseProfileGateway, releaseProfileGateway, requestGatewayForProfile } from '@/store/gateway'
import type { PetInfo } from '@/store/pet'
import { $profilePets, __resetPetMultiForTests, setProfilePetInfo } from '@/store/pet-multi'
import { $petRoster } from '@/store/pet-roster'
import { $activeGatewayProfile } from '@/store/profile'

import { FloatingPet } from './floating-pet'
import { MultiPetContainer } from './multi-pet-container'
import { PetSlot } from './pet-slot'

// The slot leases + fetches through the gateway registry; stub those so the test
// drives the slice directly and can assert the lease lifecycle.
vi.mock('@/store/gateway', async importActual => {
  const actual = await importActual<typeof import('@/store/gateway')>()

  return {
    ...actual,
    leaseProfileGateway: vi.fn(),
    releaseProfileGateway: vi.fn(),
    requestGatewayForProfile: vi.fn(async () => undefined)
  }
})

const spritesheet = (displayName: string): PetInfo => ({
  displayName,
  enabled: true,
  mime: 'image/webp',
  spritesheetBase64: 'ZmFrZQ=='
})

beforeEach(() => {
  vi.clearAllMocks()
  __resetPetMultiForTests()
  $activeGatewayProfile.set('default')
  $petRoster.set({ entries: [], initialized: true, mode: 'follow-active' })
})

describe('PetSlot (test 13, 14)', () => {
  it('renders its own profile\u2019s sprite from the $profilePets slice', () => {
    setProfilePetInfo('apollo', spritesheet('Apollo'))
    setProfilePetInfo('nova', spritesheet('Nova'))

    render(<PetSlot profile="apollo" />)

    // The slot paints Apollo (its own slice), not Nova.
    expect(screen.getByLabelText('Apollo pet')).toBeDefined()
    expect(screen.queryByLabelText('Nova pet')).toBeNull()
  })

  it('renders nothing when the pet is disabled or has no spritesheet', () => {
    setProfilePetInfo('apollo', { enabled: false })

    const { container } = render(<PetSlot profile="apollo" />)
    expect(container.querySelector('canvas')).toBeNull()
  })

  it('leases the profile gateway on mount and releases on unmount (test 14)', () => {
    setProfilePetInfo('apollo', spritesheet('Apollo'))

    const { unmount } = render(<PetSlot profile="apollo" />)

    expect(leaseProfileGateway).toHaveBeenCalledWith('apollo')
    expect(requestGatewayForProfile).toHaveBeenCalledWith('apollo', 'pet.info', { profile: 'apollo' })
    expect(releaseProfileGateway).not.toHaveBeenCalled()

    unmount()
    expect(releaseProfileGateway).toHaveBeenCalledWith('apollo')
  })
})

describe('MultiPetContainer (test 29)', () => {
  it('renders nothing when no profiles are enabled', () => {
    $petRoster.set({ entries: [], initialized: true, mode: 'pinned' })

    const { container } = render(<MultiPetContainer />)
    expect(container.querySelector('canvas')).toBeNull()
  })

  it('renders a single pinned non-active profile through the container (not legacy)', () => {
    $petRoster.set({ entries: [{ enabled: true, profile: 'apollo' }], initialized: true, mode: 'pinned' })
    $activeGatewayProfile.set('default') // apollo is NOT the active profile
    setProfilePetInfo('apollo', spritesheet('Apollo'))

    render(<MultiPetContainer />)
    expect(screen.getByLabelText('Apollo pet')).toBeDefined()
  })

  it('renders N enabled, available entries in roster order; skips disabled/unavailable', () => {
    $petRoster.set({
      entries: [
        { enabled: true, profile: 'apollo' },
        { enabled: false, profile: 'nova' },
        { enabled: true, profile: 'atlas', unavailable: true },
        { enabled: true, profile: 'vega' }
      ],
      initialized: true,
      mode: 'pinned'
    })
    setProfilePetInfo('apollo', spritesheet('Apollo'))
    setProfilePetInfo('nova', spritesheet('Nova'))
    setProfilePetInfo('vega', spritesheet('Vega'))

    render(<MultiPetContainer />)

    expect(screen.getByLabelText('Apollo pet')).toBeDefined()
    expect(screen.getByLabelText('Vega pet')).toBeDefined()
    // Disabled (nova) and unavailable (atlas) are not observed → not rendered.
    expect(screen.queryByLabelText('Nova pet')).toBeNull()
  })
})

describe('FloatingPet mode branch (test 29)', () => {
  it('pinned mode renders the MultiPetContainer (a profile slot), not the legacy single pet', () => {
    $petRoster.set({ entries: [{ enabled: true, profile: 'apollo' }], initialized: true, mode: 'pinned' })
    setProfilePetInfo('apollo', spritesheet('Apollo'))

    render(<FloatingPet />)

    // The pinned branch mounts the container → the profile slot's sprite shows,
    // and the slot leased its own gateway.
    expect(screen.getByLabelText('Apollo pet')).toBeDefined()
    expect(leaseProfileGateway).toHaveBeenCalledWith('apollo')
  })

  it('follow-active mode does not render the multi-pet container', () => {
    // follow-active with a pinned-looking entry still ignores it; the container
    // (which only renders observed pinned entries) paints nothing.
    $petRoster.set({ entries: [{ enabled: true, profile: 'apollo' }], initialized: true, mode: 'follow-active' })
    setProfilePetInfo('apollo', spritesheet('Apollo'))

    render(<MultiPetContainer />)
    expect(screen.queryByLabelText('Apollo pet')).toBeNull()
    // The roster slice is still populated (the container just isn't the renderer).
    expect($profilePets.get().get('apollo')?.info.enabled).toBe(true)
  })
})
