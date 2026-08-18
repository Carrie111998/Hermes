import { describe, expect, it } from 'vitest'

import { resolveZoneHeaderHidden, sessionStripAllowsHide } from './zone-header'

describe('resolveZoneHeaderHidden', () => {
  it('keeps the session switcher visible for a lone workspace', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: false,
        headerVeto: false,
        shown: ['workspace']
      })
    ).toBe(false)
  })

  it('ignores persisted headerHidden on a session-strip zone', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: false,
        headerVeto: false,
        persistedHidden: true,
        shown: ['workspace', 'session-tile:abc']
      })
    ).toBe(false)
  })

  it('still stands down for a full-page view', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: false,
        headerVeto: true,
        shown: ['workspace']
      })
    ).toBe(true)
  })

  it('still auto-hides a lone uncloseable side-chrome pane', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: false,
        headerVeto: false,
        shown: ['files']
      })
    ).toBe(true)
  })

  it('still honors an explicit hide on a tool zone', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: true,
        headerVeto: false,
        persistedHidden: true,
        shown: ['terminal']
      })
    ).toBe(true)
  })
})

describe('sessionStripAllowsHide', () => {
  it('refuses hide when the zone hosts the chat switcher', () => {
    expect(sessionStripAllowsHide(['workspace'])).toBe(false)
    expect(sessionStripAllowsHide(['session-tile:abc'])).toBe(false)
    expect(sessionStripAllowsHide(['terminal'])).toBe(true)
  })
})
