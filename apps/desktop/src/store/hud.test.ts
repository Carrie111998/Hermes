import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import {
  $hudActive,
  $hudSession,
  $hudWindowContext,
  hudWindowContextFromResult,
  openHud,
  startHudWindowContextTracking
} from './hud'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop
const initialScreenX = window.screenX

const open = vi.fn().mockResolvedValue({ ok: true })

function installBridge() {
  desktopWindow.hermesDesktop = {
    hud: { open }
  } as unknown as Window['hermesDesktop']
}

function session(overrides: Partial<SessionInfo>): SessionInfo {
  return { id: 's', title: '', created_at: '', updated_at: '', ...overrides } as SessionInfo
}

beforeEach(() => {
  open.mockClear()
  installBridge()
  $hudActive.set(false)
  $hudSession.set(null)
  $hudWindowContext.set(null)
  $sessions.set([])
  $activeGatewayProfile.set('default')
})

afterEach(() => {
  vi.useRealTimers()
  Object.defineProperty(window, 'screenX', { configurable: true, value: initialScreenX })

  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
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

    expect(open).toHaveBeenCalledWith({ sessionId: null, profile: 'research' })
  })

  it('normalizes to default for single-profile users', () => {
    openHud()

    expect(open).toHaveBeenCalledWith({ sessionId: null, profile: 'default' })
  })

  it('uses the active profile when the target session is not in the cache', () => {
    $activeGatewayProfile.set('work')

    openHud('unknown-session')

    expect(open).toHaveBeenCalledWith({ sessionId: 'unknown-session', profile: 'work' })
  })
})

describe('HUD live window context', () => {
  it('keeps only normalized app/title metadata', () => {
    expect(
      hudWindowContextFromResult({
        platform: 'win32',
        window: { app: '  Visual   Studio Code ', bounds: { x: 1 }, id: 42, title: ' main.py\n— project ' }
      })
    ).toEqual({ app: 'Visual Studio Code', title: 'main.py — project' })
    expect(hudWindowContextFromResult({ window: null })).toBeNull()
    expect(hudWindowContextFromResult({ error: 'unavailable' })).toBeNull()
  })

  it('refreshes immediately, on movement, and periodically without stacking reads', async () => {
    vi.useFakeTimers()
    let resolveFirst!: (value: unknown) => void

    const first = new Promise<unknown>(resolve => {
      resolveFirst = resolve
    })

    const readWindowBelow = vi
      .fn<() => Promise<unknown>>()
      .mockReturnValueOnce(first)
      .mockResolvedValue({ window: { app: 'Browser', title: 'Docs' } })

    desktopWindow.hermesDesktop = { readWindowBelow } as unknown as Window['hermesDesktop']
    const stop = startHudWindowContextTracking()

    expect(readWindowBelow).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1500)
    expect(readWindowBelow).toHaveBeenCalledTimes(1)

    resolveFirst({ window: { app: 'Editor', title: 'main.ts' } })
    await first
    await vi.advanceTimersByTimeAsync(0)
    expect(readWindowBelow).toHaveBeenCalledTimes(2)
    expect($hudWindowContext.get()).toEqual({ app: 'Browser', title: 'Docs' })

    Object.defineProperty(window, 'screenX', { configurable: true, value: initialScreenX + 20 })
    await vi.advanceTimersByTimeAsync(250)
    expect(readWindowBelow).toHaveBeenCalledTimes(3)

    stop()
    expect($hudWindowContext.get()).toBeNull()
  })
})
