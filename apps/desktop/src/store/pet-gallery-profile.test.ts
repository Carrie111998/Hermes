/**
 * Layer 7 — profile-scoped gallery + pet-transport. Gallery state is per-profile
 * (concurrent loads don't clobber each other), mutations route to the right
 * profile's slice, scale debounce/reconciliation is per-profile, and non-active
 * profiles route through requestGatewayForProfile (Electron resolves the
 * connection) — never the active requester, never a lease.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { leaseProfileGateway, requestGatewayForProfile } from '@/store/gateway'
import { $profilePets } from '@/store/pet-multi'
import { $activeGatewayProfile } from '@/store/profile'

import {
  $petGalleries,
  adoptPet,
  loadPetGallery,
  resetPetGallery,
  setPetScale,
  type GatewayRequest,
  type PetGallery
} from './pet-gallery'

vi.mock('@/store/gateway', async importOriginal => {
  const actual = await importOriginal<typeof import('@/store/gateway')>()

  return {
    ...actual,
    leaseProfileGateway: vi.fn(),
    requestGatewayForProfile: vi.fn(async () => ({}))
  }
})

const profileRequest = vi.mocked(requestGatewayForProfile)
const lease = vi.mocked(leaseProfileGateway)

// A foreground requester that should NEVER be used for a non-active profile.
const activeRequest = vi.fn(async () => ({})) as unknown as GatewayRequest

const galleryFor = (profile: string): PetGallery => ({
  active: `${profile}-pet`,
  enabled: true,
  pets: [{ displayName: profile, installed: true, slug: `${profile}-pet` }]
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  resetPetGallery()
  $activeGatewayProfile.set('default')
  profileRequest.mockImplementation(async (profile: string, method: string) => {
    if (method === 'pet.gallery') {
      return galleryFor(profile)
    }

    if (method === 'pet.info') {
      return { enabled: true }
    }

    return {}
  })
})

afterEach(() => {
  vi.useRealTimers()
  resetPetGallery()
  $activeGatewayProfile.set('default')
})

describe('per-profile gallery state (tests 27, 28)', () => {
  it('concurrent profile loads do not overwrite each other (test 27)', async () => {
    await Promise.all([
      loadPetGallery(activeRequest, { profile: 'apollo' }),
      loadPetGallery(activeRequest, { profile: 'nova' })
    ])

    const galleries = $petGalleries.get()

    expect(galleries.get('apollo')?.active).toBe('apollo-pet')
    expect(galleries.get('nova')?.active).toBe('nova-pet')
  })

  it('a mutation patches only the targeted profile\u2019s slice (test 28)', async () => {
    await Promise.all([
      loadPetGallery(activeRequest, { profile: 'apollo' }),
      loadPetGallery(activeRequest, { profile: 'nova' })
    ])

    profileRequest.mockResolvedValue({ ok: true })
    await adoptPet(activeRequest, 'custom', 'fallback', 'apollo')

    // apollo flipped to the adopted pet; nova is untouched.
    expect($petGalleries.get().get('apollo')?.active).toBe('custom')
    expect($petGalleries.get().get('apollo')?.enabled).toBe(true)
    expect($petGalleries.get().get('nova')?.active).toBe('nova-pet')
    // The mutation routed through apollo's own socket.
    expect(profileRequest).toHaveBeenCalledWith('apollo', 'pet.select', { profile: 'apollo', slug: 'custom' }, undefined, undefined)
  })
})

describe('per-profile scale debounce/reconciliation (test 47)', () => {
  it('debounces and reconciles two profiles independently', () => {
    profileRequest.mockResolvedValue({ ok: true })

    setPetScale(activeRequest, 0.5, 'apollo')
    setPetScale(activeRequest, 0.7, 'nova')

    // Before the debounce fires, nothing persisted.
    expect(profileRequest).not.toHaveBeenCalledWith('apollo', 'pet.scale', expect.anything(), undefined, undefined)

    vi.advanceTimersByTime(200)

    expect(profileRequest).toHaveBeenCalledWith('apollo', 'pet.scale', { profile: 'apollo', scale: 0.5 }, undefined, undefined)
    expect(profileRequest).toHaveBeenCalledWith('nova', 'pet.scale', { profile: 'nova', scale: 0.7 }, undefined, undefined)

    // Each profile's own slice reconciled (not the global foreground pet).
    expect($profilePets.get().get('apollo')?.info.scale).toBe(0.5)
    expect($profilePets.get().get('nova')?.info.scale).toBe(0.7)
  })
})

describe('pet-transport routing (tests 59, 60)', () => {
  it('a non-active (pool) profile uses requestGatewayForProfile, never the active requester, with no lease (test 59)', async () => {
    await loadPetGallery(activeRequest, { profile: 'apollo' })

    expect(profileRequest).toHaveBeenCalledWith('apollo', 'pet.gallery', expect.objectContaining({ profile: 'apollo' }), undefined, undefined)
    // The foreground requester is never used for a background profile.
    expect(activeRequest).not.toHaveBeenCalled()
    // Gallery calls take no lease.
    expect(lease).not.toHaveBeenCalled()
  })

  it('per-profile calls resolve through Electron for the named profile (test 60)', async () => {
    await loadPetGallery(activeRequest, { profile: 'nova' })

    // Every RPC carries the named profile so Electron routes it to that
    // profile's own backend/connection (local or remote), not the active one.
    const profiles = profileRequest.mock.calls.map(call => call[0])

    expect(profiles.every(p => p === 'nova')).toBe(true)
    expect(activeRequest).not.toHaveBeenCalled()
  })
})
