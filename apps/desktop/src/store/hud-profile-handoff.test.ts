import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import { openHud } from './hud'
import { hudWindowProfile } from './windows'

describe('hudWindowProfile', () => {
  const original = window.location

  afterEach(() => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: original
    })
  })

  it('reads ?profile= from the query before the hash', () => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { search: '?win=hud&profile=hudrepro', hash: '#/sess-1' }
    })

    expect(hudWindowProfile()).toBe('hudrepro')
  })

  it('returns null when the handoff omitted a profile', () => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { search: '?win=hud', hash: '#/sess-1' }
    })

    expect(hudWindowProfile()).toBeNull()
  })
})

describe('openHud profile handoff', () => {
  beforeEach(() => {
    $activeGatewayProfile.set('hudrepro')
  })

  afterEach(() => {
    $activeGatewayProfile.set('default')
    // @ts-expect-error test cleanup
    delete window.hermesDesktop
  })

  it('passes the active gateway profile with the session id', () => {
    const open = vi.fn(async () => ({ ok: true }))
    window.hermesDesktop = {
      hud: { open }
    } as unknown as Window['hermesDesktop']

    openHud('20260809_092408_d30862')

    expect(open).toHaveBeenCalledWith({
      sessionId: '20260809_092408_d30862',
      profile: 'hudrepro'
    })
  })
})
