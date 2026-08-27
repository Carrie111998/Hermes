import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { $activeGatewayProfile, $showAllProfiles } from './profile'
import { $selectedStoredSessionId, $sessions } from './session'
import {
  $switcherIndex,
  $switcherOpen,
  $switcherSessions,
  closeSwitcher,
  commitOnCtrlUp,
  onSwitcherTabDown,
  onSwitcherTabUp,
  openOrAdvanceSwitcher,
  slotSessionId,
  SWITCHER_REVEAL_MS
} from './session-switcher'

const session = (id: string, profile?: string, lineageRootId?: string): SessionInfo =>
  ({ id, profile, _lineage_root_id: lineageRootId }) as SessionInfo

const seed = (ids: string[], selected: null | string) => {
  $sessions.set(ids.map(session))
  $selectedStoredSessionId.set(selected)
}

const seedRows = (rows: SessionInfo[], selected: null | string) => {
  $sessions.set(rows)
  $selectedStoredSessionId.set(selected)
}

const tabTap = (direction: 1 | -1 = 1) => {
  onSwitcherTabDown()
  const target = openOrAdvanceSwitcher(direction)
  onSwitcherTabUp()

  return target
}

beforeEach(() => {
  vi.useRealTimers()
  closeSwitcher()
  $switcherSessions.set([])
  $switcherIndex.set(0)
  $activeGatewayProfile.set('default')
  $showAllProfiles.set(false)
})

afterEach(() => {
  closeSwitcher()
  seed([], null)
  $activeGatewayProfile.set('default')
  $showAllProfiles.set(false)
})

describe('openOrAdvanceSwitcher', () => {
  it('does nothing with fewer than two sessions', () => {
    seed(['a'], 'a')
    onSwitcherTabDown()

    expect(openOrAdvanceSwitcher(1)).toBeNull()
  })

  it('ignores foreign sessions when the active profile has only one session', () => {
    $activeGatewayProfile.set('profile-a')
    seedRows([session('a1', 'profile-a'), session('b1', 'profile-b'), session('b2', 'profile-b')], 'a1')
    onSwitcherTabDown()

    expect(openOrAdvanceSwitcher(1)).toBeNull()
    expect($switcherOpen.get()).toBe(false)
    expect($switcherSessions.get()).toEqual([])
    expect(commitOnCtrlUp()).toBeNull()
  })

  it('jumps immediately on a quick Tab tap without opening the HUD', () => {
    seed(['a', 'b', 'c'], 'a')

    expect(tabTap()).toBe('b')
    expect($switcherOpen.get()).toBe(false)
    expect(commitOnCtrlUp()).toBeNull()
  })

  it('does not open the HUD when Ctrl stays down but Tab was released quickly', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'], 'a')

    tabTap()
    vi.advanceTimersByTime(SWITCHER_REVEAL_MS)

    expect($switcherOpen.get()).toBe(false)
  })

  it('opens the HUD when Tab stays held past the reveal delay', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'], 'a')

    onSwitcherTabDown()
    openOrAdvanceSwitcher(1)
    vi.advanceTimersByTime(SWITCHER_REVEAL_MS)

    expect($switcherOpen.get()).toBe(true)
    onSwitcherTabUp()
  })

  it('opens on a second Tab while Ctrl is still down', () => {
    seed(['a', 'b', 'c'], 'a')

    expect(tabTap()).toBe('b')
    onSwitcherTabDown()
    openOrAdvanceSwitcher(1)
    onSwitcherTabUp()

    expect($switcherOpen.get()).toBe(true)
    expect($switcherIndex.get()).toBe(2)
  })

  it('commits the HUD highlight on Ctrl up', () => {
    seed(['a', 'b', 'c'], 'a')

    expect(tabTap()).toBe('b')
    onSwitcherTabDown()
    openOrAdvanceSwitcher(1)
    onSwitcherTabUp()

    expect(commitOnCtrlUp()).toBe('c')
  })

  it('wraps forward from A2 to A1 within the active profile', () => {
    $activeGatewayProfile.set('profile-a')
    seedRows([session('a1', 'profile-a'), session('a2', 'profile-a'), session('b1', 'profile-b')], 'a2')

    expect(tabTap(1)).toBe('a1')
  })

  it('wraps backward from A1 to A2 within the active profile', () => {
    $activeGatewayProfile.set('profile-a')
    seedRows([session('a1', 'profile-a'), session('a2', 'profile-a'), session('b1', 'profile-b')], 'a1')

    expect(tabTap(-1)).toBe('a2')
  })

  it('keeps the held-switcher snapshot inside the active profile', () => {
    vi.useFakeTimers()
    $activeGatewayProfile.set('profile-a')
    seedRows([session('a1', 'profile-a'), session('a2', 'profile-a'), session('b1', 'profile-b')], 'a1')

    onSwitcherTabDown()
    expect(openOrAdvanceSwitcher(1)).toBe('a2')
    vi.advanceTimersByTime(SWITCHER_REVEAL_MS)

    expect($switcherOpen.get()).toBe(true)
    expect($switcherSessions.get().map(row => row.id)).toEqual(['a1', 'a2'])
    onSwitcherTabUp()
  })

  it('fails closed in explicit All Profiles scope', () => {
    vi.useFakeTimers()
    $activeGatewayProfile.set('profile-a')
    $showAllProfiles.set(true)
    seedRows(
      [session('a1', 'profile-a'), session('a2', 'profile-a'), session('b1', 'profile-b'), session('c1', 'profile-c')],
      'a2'
    )

    expect(tabTap(1)).toBeNull()
    expect(tabTap(-1)).toBeNull()
    expect(slotSessionId(1)).toBeNull()
    expect(slotSessionId(2)).toBeNull()
    onSwitcherTabDown()
    expect(openOrAdvanceSwitcher(1)).toBeNull()
    vi.advanceTimersByTime(SWITCHER_REVEAL_MS)

    expect($switcherOpen.get()).toBe(false)
    expect($switcherSessions.get()).toEqual([])
    onSwitcherTabUp()
  })

  it('closes pending browsing when switching to All Profiles', () => {
    vi.useFakeTimers()
    $activeGatewayProfile.set('profile-a')
    seedRows([session('a1', 'profile-a'), session('a2', 'profile-a'), session('b1', 'profile-b')], 'a1')

    onSwitcherTabDown()
    expect(openOrAdvanceSwitcher(1)).toBe('a2')
    $showAllProfiles.set(true)

    expect($switcherOpen.get()).toBe(false)
    vi.advanceTimersByTime(SWITCHER_REVEAL_MS)
    expect($switcherOpen.get()).toBe(false)
    expect(commitOnCtrlUp()).toBeNull()
  })

  it('closes an open HUD on profile change and allows a fresh browse', () => {
    vi.useFakeTimers()
    $activeGatewayProfile.set('profile-a')
    seedRows(
      [session('a1', 'profile-a'), session('a2', 'profile-a'), session('b1', 'profile-b'), session('b2', 'profile-b')],
      'a1'
    )

    onSwitcherTabDown()
    expect(openOrAdvanceSwitcher(1)).toBe('a2')
    vi.advanceTimersByTime(SWITCHER_REVEAL_MS)
    expect($switcherOpen.get()).toBe(true)

    $activeGatewayProfile.set('profile-b')

    expect($switcherOpen.get()).toBe(false)
    expect(commitOnCtrlUp()).toBeNull()
    $selectedStoredSessionId.set('b1')
    expect(tabTap()).toBe('b2')
    expect($switcherSessions.get().map(row => row.id)).toEqual(['b1', 'b2'])
  })

  it('normalizes blank and legacy profile values to default', () => {
    seedRows([session('a1', ''), session('a2'), session('b1', 'profile-b')], 'a2')

    expect(tabTap(1)).toBe('a1')
    expect($switcherSessions.get().map(row => row.id)).toEqual(['a1', 'a2'])
  })

  it('finds the selected row by its stored lineage identity', () => {
    $activeGatewayProfile.set('profile-a')
    seedRows(
      [session('a1', 'profile-a'), session('a2-tip', 'profile-a', 'a2-root'), session('b1', 'profile-b')],
      'a2-root'
    )

    expect(tabTap(-1)).toBe('a1')
  })
})

describe('slotSessionId', () => {
  it('reads the armed snapshot while browsing is pending', () => {
    seed(['a', 'b', 'c'], 'a')
    tabTap()
    $sessions.set([session('x')])

    expect(slotSessionId(2)).toBe('b')
  })

  it('uses the active profile candidate list while idle', () => {
    $activeGatewayProfile.set('profile-a')
    seedRows([session('a1', 'profile-a'), session('a2', 'profile-a'), session('b1', 'profile-b')], 'a1')

    expect(slotSessionId(2)).toBe('a2')
    expect(slotSessionId(3)).toBeNull()
  })
})
