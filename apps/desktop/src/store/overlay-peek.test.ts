// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $overlayPeek, beginOverlayPeek, pulseOverlayPeek, resetOverlayPeek } from './overlay-peek'

const active = () => document.documentElement.hasAttribute('data-hermes-overlay-peek')

describe('overlay peek lifecycle', () => {
  beforeEach(() => resetOverlayPeek())

  afterEach(() => {
    resetOverlayPeek()
    vi.useRealTimers()
  })

  it('stays active until every overlapping owner has released', () => {
    const releaseA = beginOverlayPeek()
    const releaseB = beginOverlayPeek()

    expect($overlayPeek.get()).toBe(2)
    expect(active()).toBe(true)

    releaseA()
    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    releaseB()
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('makes each owner release idempotent without consuming another owner', () => {
    const releaseA = beginOverlayPeek()
    const releaseB = beginOverlayPeek()

    releaseA()
    releaseA()

    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    releaseB()
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
    const releaseHold = beginOverlayPeek()

    pulseOverlayPeek(900)
    vi.advanceTimersByTime(900)

    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    releaseHold()
    expect(active()).toBe(false)
  })

  it('reset clears every owner and late releases stay harmless', () => {
    const releaseA = beginOverlayPeek()
    const releaseB = beginOverlayPeek()

    resetOverlayPeek()
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)

    releaseA()
    releaseB()
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('a stale pulse cannot consume a new owner after reset', () => {
    vi.useFakeTimers()

    pulseOverlayPeek(900)
    resetOverlayPeek()
    const releaseNewHold = beginOverlayPeek()

    vi.advanceTimersByTime(900)
    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    releaseNewHold()
    expect(active()).toBe(false)
  })
})
