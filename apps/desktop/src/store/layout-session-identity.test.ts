import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'

beforeEach(() => {
  window.localStorage.clear()
  vi.resetModules()
})

describe('layout session identity migration', () => {
  it('loads a legacy default pin canonically so one unpin removes it', async () => {
    window.localStorage.setItem('hermes.desktop.pinnedSessions', JSON.stringify(['shared']))
    const { $pinnedSessionIds, unpinSession } = await import('./layout')
    const canonical = sessionIdentityKey('shared', 'default')

    expect($pinnedSessionIds.get()).toEqual([canonical])
    unpinSession(canonical)
    expect($pinnedSessionIds.get()).toEqual([])
  })

  it('loads a legacy manual order as default-profile compound identities', async () => {
    window.localStorage.setItem('hermes.desktop.sessionOrder', JSON.stringify(['other', 'shared']))
    const { $sidebarSessionOrderIds } = await import('./layout')

    expect($sidebarSessionOrderIds.get()).toEqual([
      sessionIdentityKey('other', 'default'),
      sessionIdentityKey('shared', 'default')
    ])
  })

  it('round-trips opaque legacy ids separately from same-looking compound identities', async () => {
    const sentinelLookingId = 'alpha\u0000shared'
    window.localStorage.setItem('hermes.desktop.pinnedSessions', JSON.stringify([sentinelLookingId]))
    const first = await import('./layout')
    const legacyDefault = sessionIdentityKey(sentinelLookingId, 'default')
    const ownedAlpha = sessionIdentityKey('shared', 'alpha')

    expect(first.$pinnedSessionIds.get()).toEqual([legacyDefault])

    first.$pinnedSessionIds.set([legacyDefault, ownedAlpha])
    expect(JSON.parse(window.localStorage.getItem('hermes.desktop.pinnedSessions')!)).not.toBeInstanceOf(Array)

    vi.resetModules()
    const second = await import('./layout')

    expect(second.$pinnedSessionIds.get()).toEqual([legacyDefault, ownedAlpha])
  })

  it("reorders visible pins without discarding another profile's pins", async () => {
    const alphaA = sessionIdentityKey('a', 'alpha')
    const alphaB = sessionIdentityKey('b', 'alpha')
    const betaA = sessionIdentityKey('a', 'beta')
    const { $pinnedSessionIds, setPinnedSessionOrder } = await import('./layout')

    $pinnedSessionIds.set([alphaA, alphaB, betaA])
    setPinnedSessionOrder([alphaB, alphaA])

    expect($pinnedSessionIds.get()).toEqual([alphaB, alphaA, betaA])
  })
})
