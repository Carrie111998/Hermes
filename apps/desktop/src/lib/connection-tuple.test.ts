import { describe, expect, it } from 'vitest'

import { resolveUnambiguousConnectionProfile } from './connection-tuple'

describe('resolveUnambiguousConnectionProfile', () => {
  it('uses the last-used profile when it is still on that connection', () => {
    expect(
      resolveUnambiguousConnectionProfile({
        connectionId: 'homelab',
        lastProfileByConnection: { homelab: 'omer' },
        rosterProfiles: ['default', 'omer', 'scout']
      })
    ).toBe('omer')
  })

  it('uses the sole roster profile when no last-used profile is stored', () => {
    expect(
      resolveUnambiguousConnectionProfile({
        connectionId: 'vps',
        lastProfileByConnection: {},
        rosterProfiles: ['default']
      })
    ).toBe('default')
  })

  it('returns null when several profiles exist and none is explicit', () => {
    expect(
      resolveUnambiguousConnectionProfile({
        connectionId: 'pandora',
        lastProfileByConnection: {},
        rosterProfiles: ['default', 'scout']
      })
    ).toBeNull()
  })

  it('returns null when the route is missing entirely', () => {
    expect(
      resolveUnambiguousConnectionProfile({
        connectionId: 'ghost',
        lastProfileByConnection: {},
        rosterProfiles: []
      })
    ).toBeNull()
  })

  it('does not invent a local/mac-cockpit default for a different gateway', () => {
    expect(
      resolveUnambiguousConnectionProfile({
        connectionId: 'pop-os-hermes',
        lastProfileByConnection: { local: 'mac-cockpit' },
        rosterProfiles: ['default', 'work']
      })
    ).toBeNull()
  })
})
