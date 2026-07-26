/**
 * Layer 7 — orphan reconciliation. A pinned roster reconciles against the live
 * profile catalog: a deleted/renamed profile is marked unavailable + disabled
 * (its lease released once); a restored profile clears unavailable WITHOUT
 * auto-enabling. Gated on `$profilesLoaded` so the initial empty catalog is
 * never read as "everything was deleted".
 */

import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePetRosterReconciliation } from '@/components/pet/use-pet-roster-reconciliation'
import { releaseProfileGateway } from '@/store/gateway'
import { $profiles, $profilesLoaded, $activeGatewayProfile } from '@/store/profile'
import type { ProfileInfo } from '@/types/hermes'

import { $petRoster, reconcilePetRoster, setPetRosterEntries, setPetRosterMode } from './pet-roster'

vi.mock('@/store/gateway', async importOriginal => {
  const actual = await importOriginal<typeof import('@/store/gateway')>()

  return { ...actual, releaseProfileGateway: vi.fn() }
})

const release = vi.mocked(releaseProfileGateway)

const profile = (name: string): ProfileInfo => ({ name } as ProfileInfo)

beforeEach(() => {
  vi.clearAllMocks()
  $petRoster.set({ entries: [], initialized: true, mode: 'follow-active' })
  $activeGatewayProfile.set('default')
  $profiles.set([])
  $profilesLoaded.set(false)
})

afterEach(() => {
  cleanup()
  $activeGatewayProfile.set('default')
  $profiles.set([])
  $profilesLoaded.set(false)
})

describe('reconcilePetRoster', () => {
  it('marks a deleted pinned profile unavailable + disabled and releases its lease (test 48)', () => {
    setPetRosterMode('pinned')
    setPetRosterEntries([{ enabled: true, profile: 'apollo' }])

    reconcilePetRoster([profile('nova')])

    const entry = $petRoster.get().entries.find(e => e.profile === 'apollo')

    expect(entry?.enabled).toBe(false)
    expect(entry?.unavailable).toBe(true)
    expect(release).toHaveBeenCalledWith('apollo')
  })

  it('releases a missing profile only once across repeated refreshes (test 52)', () => {
    setPetRosterMode('pinned')
    setPetRosterEntries([{ enabled: true, profile: 'apollo' }])

    reconcilePetRoster([])
    reconcilePetRoster([])
    reconcilePetRoster([])

    // Disabled+unavailable after the first pass, so subsequent passes don't
    // re-release (entry.enabled is false).
    expect(release).toHaveBeenCalledTimes(1)
    expect(release).toHaveBeenCalledWith('apollo')
  })

  it('clears unavailable when a profile returns, without auto-enabling (test 52)', () => {
    setPetRosterMode('pinned')
    setPetRosterEntries([{ enabled: false, profile: 'apollo', unavailable: true }])

    reconcilePetRoster([profile('apollo')])

    const entry = $petRoster.get().entries.find(e => e.profile === 'apollo')

    // Normalization drops a false `unavailable`, so a cleared row reads falsy.
    expect(entry?.unavailable).toBeFalsy()
    // Restoration must not silently re-pin.
    expect(entry?.enabled).toBe(false)
  })

  it('is a no-op in follow-active mode', () => {
    setPetRosterMode('follow-active')
    setPetRosterEntries([{ enabled: true, profile: 'apollo' }])

    reconcilePetRoster([])

    expect($petRoster.get().entries.find(e => e.profile === 'apollo')?.enabled).toBe(true)
    expect(release).not.toHaveBeenCalled()
  })
})

describe('usePetRosterReconciliation gating (test 51)', () => {
  it('does NOT reconcile before the first successful catalog load', () => {
    setPetRosterMode('pinned')
    setPetRosterEntries([{ enabled: true, profile: 'apollo' }])
    // $profilesLoaded is false and $profiles is empty (pre-refresh).

    renderHook(() => usePetRosterReconciliation())

    // The empty initial catalog must not be read as "apollo was deleted".
    const entry = $petRoster.get().entries.find(e => e.profile === 'apollo')

    expect(entry?.enabled).toBe(true)
    expect(entry?.unavailable).toBeFalsy()
    expect(release).not.toHaveBeenCalled()
  })

  it('reconciles once the catalog has loaded', () => {
    setPetRosterMode('pinned')
    setPetRosterEntries([{ enabled: true, profile: 'apollo' }])

    renderHook(() => usePetRosterReconciliation())

    act(() => {
      $profiles.set([profile('nova')])
      $profilesLoaded.set(true)
    })

    const entry = $petRoster.get().entries.find(e => e.profile === 'apollo')

    expect(entry?.unavailable).toBe(true)
    expect(entry?.enabled).toBe(false)
    expect(release).toHaveBeenCalledWith('apollo')
  })
})
