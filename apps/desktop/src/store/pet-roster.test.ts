import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import {
  $observedPetProfiles,
  $petRoster,
  PINNED_HARD_CAP,
  PINNED_SOFT_CAP,
  isProfilePetEnabled,
  normalizeStoredPetRoster,
  normalizeStoredPetRosterWithClamp,
  setPetRosterEntries,
  setPetRosterMode,
  setProfilePetEnabled,
  type RosterEntry
} from './pet-roster'

const entry = (profile: string, enabled = true, unavailable?: boolean): RosterEntry => ({
  enabled,
  profile,
  ...(unavailable ? { unavailable: true } : {})
})

beforeEach(() => {
  // Reset the live atom to the default follow-active roster so cases are isolated
  // (the atom is module-cached and otherwise leaks state between tests).
  $petRoster.set({ entries: [], initialized: true, mode: 'follow-active' })
  $activeGatewayProfile.set('default')
})

afterEach(() => {
  $activeGatewayProfile.set('default')
})

describe('normalizeStoredPetRoster', () => {
  it('defaults null / malformed input to an initialized follow-active roster', () => {
    expect(normalizeStoredPetRoster(null)).toEqual({ entries: [], initialized: true, mode: 'follow-active' })
    expect(normalizeStoredPetRoster('garbage')).toEqual({ entries: [], initialized: true, mode: 'follow-active' })
    expect(normalizeStoredPetRoster({ entries: 'nope' })).toEqual({
      entries: [],
      initialized: true,
      mode: 'follow-active'
    })
  })

  it('migrates an initialized:false first-boot marker to the default roster', () => {
    expect(normalizeStoredPetRoster({ entries: [entry('apollo')], initialized: false, mode: 'pinned' })).toEqual({
      entries: [],
      initialized: true,
      mode: 'follow-active'
    })
  })

  it('an initialized empty pinned roster stays empty (deliberately hid all pets)', () => {
    expect(normalizeStoredPetRoster({ entries: [], initialized: true, mode: 'pinned' })).toEqual({
      entries: [],
      initialized: true,
      mode: 'pinned'
    })
  })

  it('drops entries with a missing/blank profile and normalizes the key', () => {
    const { roster } = normalizeStoredPetRosterWithClamp({
      entries: [{ enabled: true }, { enabled: true, profile: '  ' }, { enabled: true, profile: ' Apollo ' }],
      initialized: true,
      mode: 'pinned'
    })

    // normalizeProfileKey trims (and defaults blank → "default") but preserves case.
    expect(roster.entries).toEqual([entry('Apollo')])
  })
})

describe('roster enabled-profile cap invariant (test 24)', () => {
  it('exposes a soft cap of 4 and a hard cap of 8', () => {
    expect(PINNED_SOFT_CAP).toBe(4)
    expect(PINNED_HARD_CAP).toBe(8)
  })

  it('keeps the first eight enabled in stored order and disables the rest', () => {
    const profiles = Array.from({ length: 10 }, (_, i) => `p${i}`)
    const { clampedProfiles, roster } = normalizeStoredPetRosterWithClamp({
      entries: profiles.map(name => entry(name, true)),
      initialized: true,
      mode: 'pinned'
    })

    const enabled = roster.entries.filter(e => e.enabled).map(e => e.profile)
    expect(enabled).toEqual(['p0', 'p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'])
    // The last two (in stored order) were force-disabled and reported once.
    expect(clampedProfiles).toEqual(['p8', 'p9'])
    expect(roster.entries.find(e => e.profile === 'p8')?.enabled).toBe(false)
    expect(roster.entries.find(e => e.profile === 'p9')?.enabled).toBe(false)
  })

  it('unavailable and disabled rows never count toward the cap', () => {
    const entries: RosterEntry[] = [
      entry('gone', true, true), // unavailable — excluded
      entry('off', false), // disabled — excluded
      ...Array.from({ length: 8 }, (_, i) => entry(`p${i}`, true))
    ]

    const { clampedProfiles, roster } = normalizeStoredPetRosterWithClamp({
      entries,
      initialized: true,
      mode: 'pinned'
    })

    expect(clampedProfiles).toEqual([])
    expect(roster.entries.filter(e => e.enabled && !e.unavailable)).toHaveLength(8)
  })

  it('clamps regardless of the current mode (a follow-active roster later switched to pinned)', () => {
    const { roster } = normalizeStoredPetRosterWithClamp({
      entries: Array.from({ length: 9 }, (_, i) => entry(`p${i}`, true)),
      initialized: true,
      mode: 'follow-active'
    })

    expect(roster.entries.filter(e => e.enabled)).toHaveLength(PINNED_HARD_CAP)
  })
})

describe('roster mutation APIs reuse the invariant', () => {
  it('setProfilePetEnabled refuses to enable a ninth profile and leaves the roster unchanged', () => {
    setPetRosterMode('pinned')
    setPetRosterEntries(Array.from({ length: 8 }, (_, i) => entry(`p${i}`, true)))

    const before = $petRoster.get()
    expect(setProfilePetEnabled('p9', true)).toBe(false)
    expect($petRoster.get()).toEqual(before)
    expect(isProfilePetEnabled('p9')).toBe(false)
  })

  it('setProfilePetEnabled enables within the cap and disables freely', () => {
    setPetRosterMode('pinned')

    expect(setProfilePetEnabled('apollo', true)).toBe(true)
    expect(isProfilePetEnabled('apollo')).toBe(true)

    // Disabling always succeeds, even at the cap.
    expect(setProfilePetEnabled('apollo', false)).toBe(true)
    expect(isProfilePetEnabled('apollo')).toBe(false)
  })

  it('re-enabling an unavailable row clears unavailable and counts toward the cap', () => {
    setPetRosterMode('pinned')
    setPetRosterEntries([entry('apollo', false, true)])

    expect(setProfilePetEnabled('apollo', true)).toBe(true)

    const restored = $petRoster.get().entries.find(e => e.profile === 'apollo')
    expect(restored?.enabled).toBe(true)
    expect(restored?.unavailable).toBeUndefined()
  })
})

describe('$observedPetProfiles', () => {
  it('follow-active with empty entries still observes the active profile (test 44)', () => {
    $petRoster.set({ entries: [], initialized: true, mode: 'follow-active' })
    $activeGatewayProfile.set('apollo')

    expect($observedPetProfiles.get()).toEqual(new Set(['apollo']))
  })

  it('follow-active observes only the active profile, never background ones', () => {
    $petRoster.set({ entries: [entry('nova', true)], initialized: true, mode: 'follow-active' })
    $activeGatewayProfile.set('default')

    // The pinned entry is ignored in follow-active mode.
    expect($observedPetProfiles.get()).toEqual(new Set(['default']))
  })

  it('pinned mode observes every enabled, available entry', () => {
    $petRoster.set({
      entries: [entry('apollo', true), entry('nova', false), entry('gone', true, true)],
      initialized: true,
      mode: 'pinned'
    })

    // nova is disabled, gone is unavailable → only apollo is observed.
    expect($observedPetProfiles.get()).toEqual(new Set(['apollo']))
  })
})
