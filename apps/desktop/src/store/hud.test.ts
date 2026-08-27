import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $composerNewChatGeneration } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { $hudActive, $hudSession, openHud, reportHudSession, resetHudLayout, watchHudState } from './hud'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const open = vi.fn().mockResolvedValue({ ok: true })
const resetLayout = vi.fn().mockResolvedValue({ ok: true })
const setSession = vi.fn()
const unsubscribe = vi.fn()

let emitHudChanged: ((state: {
  newChatGeneration: number | string | null
  open: boolean
  sessionId: string | null
}) => void) | null = null

const onChanged = vi.fn(callback => {
  emitHudChanged = callback

  return unsubscribe
})

function installBridge() {
  desktopWindow.hermesDesktop = {
    hud: { onChanged, open, resetLayout, setSession }
  } as unknown as Window['hermesDesktop']
}

function session(overrides: Partial<SessionInfo>): SessionInfo {
  return { id: 's', title: '', created_at: '', updated_at: '', ...overrides } as SessionInfo
}

beforeEach(() => {
  open.mockClear()
  resetLayout.mockClear()
  setSession.mockClear()
  onChanged.mockClear()
  unsubscribe.mockClear()
  emitHudChanged = null
  installBridge()
  $hudActive.set(false)
  $hudSession.set(null)
  $sessions.set([])
  $activeGatewayProfile.set('default')
  $composerNewChatGeneration.set('44444444-4444-4444-8444-444444444444')
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('watchHudState', () => {
  it('hands the exact New Chat generation to the close callback', () => {
    const onClosed = vi.fn()

    expect(watchHudState(onClosed)).toBe(unsubscribe)
    emitHudChanged?.({
      newChatGeneration: '55555555-5555-4555-8555-555555555555',
      open: false,
      sessionId: null
    })

    expect(onClosed).toHaveBeenCalledWith({
      newChatGeneration: '55555555-5555-4555-8555-555555555555',
      sessionId: null
    })
  })
})

describe('reportHudSession', () => {
  it('reports the exact current generation for New Chat', () => {
    reportHudSession(null)

    expect(setSession).toHaveBeenCalledWith({
      newChatGeneration: '44444444-4444-4444-8444-444444444444',
      sessionId: null
    })
  })

  it('reports no generation for a stored session', () => {
    reportHudSession('stored-hud')

    expect(setSession).toHaveBeenCalledWith({ newChatGeneration: null, sessionId: 'stored-hud' })
  })
})

describe('resetHudLayout', () => {
  it('uses the native HUD recovery capability', () => {
    resetHudLayout()

    expect(resetLayout).toHaveBeenCalledOnce()
  })
})

describe('openHud profile targeting (#82285)', () => {
  it('carries the session-stamped profile when the target belongs to another profile', () => {
    $sessions.set([session({ id: 'abc', profile: 'work' })])
    $activeGatewayProfile.set('default')

    openHud('abc')

    expect(open).toHaveBeenCalledWith({ sessionId: 'abc', profile: 'work' })
  })

  it('falls back to the active gateway profile for an unstamped session', () => {
    $sessions.set([session({ id: 'abc', profile: '' })])
    $activeGatewayProfile.set('work')

    openHud('abc')

    expect(open).toHaveBeenCalledWith({ sessionId: 'abc', profile: 'work' })
  })

  it('uses the active gateway profile when opening without a session', () => {
    $activeGatewayProfile.set('research')

    openHud()

    expect(open).toHaveBeenCalledWith({
      newChatGeneration: '44444444-4444-4444-8444-444444444444',
      sessionId: null,
      profile: 'research'
    })
  })

  it('normalizes to default for single-profile users', () => {
    openHud()

    expect(open).toHaveBeenCalledWith({
      newChatGeneration: '44444444-4444-4444-8444-444444444444',
      sessionId: null,
      profile: 'default'
    })
  })

  it('uses the active profile when the target session is not in the cache', () => {
    $activeGatewayProfile.set('work')

    openHud('unknown-session')

    expect(open).toHaveBeenCalledWith({ sessionId: 'unknown-session', profile: 'work' })
  })
})
