// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $overlayPeek, beginOverlayPeek, endOverlayPeek, pulseOverlayPeek, resetOverlayPeek } from './overlay-peek'

const active = () => document.documentElement.hasAttribute('data-hermes-overlay-peek')

describe('overlay peek lifecycle', () => {
  beforeEach(() => resetOverlayPeek())

  afterEach(() => {
    resetOverlayPeek()
    vi.useRealTimers()
  })

  it('stays active until every overlapping hold has ended', () => {
    beginOverlayPeek()
    beginOverlayPeek()

    expect($overlayPeek.get()).toBe(2)
    expect(active()).toBe(true)

    endOverlayPeek()
    expect(active()).toBe(true)

    endOverlayPeek()
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('never goes negative after a stray release', () => {
    endOverlayPeek()
    endOverlayPeek()

    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('pulses for a bounded duration', () => {
    vi.useFakeTimers()

    pulseOverlayPeek(1_200)
    expect(active()).toBe(true)

    vi.advanceTimersByTime(1_199)
    expect(active()).toBe(true)

    vi.advanceTimersByTime(1)
    expect(active()).toBe(false)
  })

  it('lets a held preview outlive an overlapping pulse', () => {
    vi.useFakeTimers()

    beginOverlayPeek()
    pulseOverlayPeek(900)
    vi.advanceTimersByTime(900)

    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    endOverlayPeek()
    expect(active()).toBe(false)
  })

  it('reset clears every hold and late pulse expiry stays harmless', () => {
    vi.useFakeTimers()

    beginOverlayPeek()
    pulseOverlayPeek(900)
    resetOverlayPeek()

    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)

    vi.advanceTimersByTime(900)
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('a stale pulse cannot consume a new hold after reset', () => {
    vi.useFakeTimers()

    pulseOverlayPeek(900)
    resetOverlayPeek()
    beginOverlayPeek()

    vi.advanceTimersByTime(900)
    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    endOverlayPeek()
    expect(active()).toBe(false)
  })
})
