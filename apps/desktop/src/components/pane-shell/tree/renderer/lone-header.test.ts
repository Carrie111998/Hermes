import { describe, expect, it } from 'vitest'

import { forceLoneHeaderForPanes, resolveZoneHeaderHidden } from './lone-header'

describe('forceLoneHeaderForPanes', () => {
  const chrome =
    (placement?: string, uncloseable = false) =>
    () => ({ placement, uncloseable })

  const noCollapse = () => false

  // Every mirrored tile (session / page / preview) is a closeable `main` pane, so
  // dragging one into a zone of its own must keep its tab — it used to strand a
  // preview headerless, with nothing to grab and no ✕.
  it('forces a header for closeable placement:main panes', () => {
    expect(forceLoneHeaderForPanes(['preview-tile:url:x'], chrome('main'), noCollapse)).toBe(true)
    expect(forceLoneHeaderForPanes(['session-tile:abc'], chrome('main'), noCollapse)).toBe(true)
  })

  it('forces a header for a lone collapse tool pane', () => {
    expect(
      forceLoneHeaderForPanes(
        ['terminal'],
        () => ({}),
        id => id === 'terminal'
      )
    ).toBe(true)
  })

  it('leaves a lone uncloseable workspace headerless', () => {
    expect(forceLoneHeaderForPanes(['workspace'], chrome('main', true), noCollapse)).toBe(false)
  })

  it('leaves standing side chrome (files / sessions) headerless', () => {
    expect(forceLoneHeaderForPanes(['files'], chrome('right'), noCollapse)).toBe(false)
  })
})

describe('resolveZoneHeaderHidden', () => {
  it('keeps a lone session workspace strip visible', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: false,
        headerVeto: false,
        persistedHidden: undefined,
        sessionStrip: true,
        shownCount: 1
      })
    ).toBe(false)
  })

  it('ignores a persisted hide for session strips', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: true,
        headerVeto: false,
        persistedHidden: true,
        sessionStrip: true,
        shownCount: 2
      })
    ).toBe(false)
  })

  it('still honors a full-page header veto', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: false,
        headerVeto: true,
        persistedHidden: false,
        sessionStrip: true,
        shownCount: 1
      })
    ).toBe(true)
  })

  it('preserves explicit and automatic hiding for non-session zones', () => {
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: true,
        headerVeto: false,
        persistedHidden: true,
        sessionStrip: false,
        shownCount: 1
      })
    ).toBe(true)
    expect(
      resolveZoneHeaderHidden({
        forceLoneHeader: false,
        headerVeto: false,
        persistedHidden: undefined,
        sessionStrip: false,
        shownCount: 1
      })
    ).toBe(true)
  })
})
