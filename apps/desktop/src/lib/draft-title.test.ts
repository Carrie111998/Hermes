import { describe, expect, it } from 'vitest'

import { deriveDraftTitle, shouldUseDraftTabTitle } from './draft-title'

describe('shouldUseDraftTabTitle', () => {
  it('names a true new chat after the composer', () => {
    expect(shouldUseDraftTabTitle({ storedSessionId: null })).toBe(true)
    expect(shouldUseDraftTabTitle({ storedSessionId: '   ' })).toBe(true)
  })

  it('never overlays composer text on a listed session with a title', () => {
    expect(
      shouldUseDraftTabTitle({
        storedSessionId: '20260818_212755_f07ce6',
        listedRow: { title: 'Hermes Desktop-App Sessions-Bug beheben', message_count: 87 }
      })
    ).toBe(false)
  })

  it('never overlays composer text on a listed session that already has messages', () => {
    expect(
      shouldUseDraftTabTitle({
        storedSessionId: '20260818_212029_b9c00b',
        listedRow: { title: '', message_count: 78 }
      })
    ).toBe(false)
  })

  it('keeps a persisted chat titled even when its recents row is missing', () => {
    // Compression ancestor / off-page tab: we still have a transcript, so the
    // tab must not fall back to deriveDraftTitle("die h") / "New session".
    expect(
      shouldUseDraftTabTitle({
        storedSessionId: '20260818_212029_b9c00b',
        listedRow: null,
        hasMessages: true
      })
    ).toBe(false)
  })

  it('still live-names an unused + / ⌘T draft', () => {
    expect(
      shouldUseDraftTabTitle({
        storedSessionId: 'draft-unlisted',
        listedRow: null,
        hasMessages: false
      })
    ).toBe(true)
  })
})

describe('deriveDraftTitle', () => {
  it('takes the first meaningful line', () => {
    expect(deriveDraftTitle('die h')).toBe('die h')
  })
})
