import { describe, expect, it } from 'vitest'

import {
  clipboardImageFingerprint,
  createQuietClipboardProbeState,
  noteQuietClipboardAttach,
  noteQuietClipboardProbe,
  QUIET_CLIPBOARD_ATTACH_DEDUPE_MS,
  QUIET_CLIPBOARD_PROBE_COOLDOWN_MS,
  shouldAttachQuietClipboardImage,
  shouldQuietClipboardProbe
} from '../lib/quietClipboardProbe.js'

describe('quietClipboardProbe', () => {
  it('allows the first quiet probe and cools down subsequent ones', () => {
    const state = createQuietClipboardProbeState()
    const t0 = 1_000_000

    expect(shouldQuietClipboardProbe(state, t0)).toBe(true)
    noteQuietClipboardProbe(state, t0)
    expect(shouldQuietClipboardProbe(state, t0 + QUIET_CLIPBOARD_PROBE_COOLDOWN_MS - 1)).toBe(false)
    expect(shouldQuietClipboardProbe(state, t0 + QUIET_CLIPBOARD_PROBE_COOLDOWN_MS)).toBe(true)
  })

  it('dedupes the same clipboard image fingerprint within the attach window', () => {
    const state = createQuietClipboardProbeState()
    const t0 = 2_000_000
    const key = clipboardImageFingerprint({ width: 1594, height: 1382, token_estimate: 1000 })

    expect(shouldAttachQuietClipboardImage(state, key, t0)).toBe(true)
    noteQuietClipboardAttach(state, key, t0)
    expect(shouldAttachQuietClipboardImage(state, key, t0 + 1_000)).toBe(false)
    expect(shouldAttachQuietClipboardImage(state, key, t0 + QUIET_CLIPBOARD_ATTACH_DEDUPE_MS)).toBe(true)
  })

  it('allows a different fingerprint immediately after a quiet attach', () => {
    const state = createQuietClipboardProbeState()
    const t0 = 3_000_000
    const first = clipboardImageFingerprint({ width: 100, height: 100, token_estimate: 85 })
    const second = clipboardImageFingerprint({ width: 200, height: 200, token_estimate: 85 })

    noteQuietClipboardAttach(state, first, t0)
    expect(shouldAttachQuietClipboardImage(state, second, t0 + 10)).toBe(true)
  })

  it('builds a stable fingerprint from width/height/token estimate', () => {
    expect(clipboardImageFingerprint({ width: 10, height: 20, token_estimate: 30 })).toBe('10x20:30')
    expect(clipboardImageFingerprint({})).toBe('0x0:0')
  })
})
