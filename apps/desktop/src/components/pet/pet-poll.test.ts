/**
 * Layer 8 — polling budget. Cadence tiers (3s bootstrap / 15s foreground /
 * 30s background / 120s blurred), offline/reauth skip, the single background
 * lane (foreground never queued behind it), stagger, and the enabled-profile cap.
 */

import { describe, expect, it } from 'vitest'

import { PINNED_HARD_CAP, PINNED_SOFT_CAP } from '@/store/pet-roster'

import {
  createPollLane,
  petPollInterval,
  PET_ACTIVE_REFRESH_MS,
  PET_BG_POLL_CONCURRENCY,
  PET_BG_POLL_MS,
  PET_BLURRED_POLL_MS,
  PET_POLL_MS,
  staggerOffset
} from './pet-poll'

describe('petPollInterval cadence (test 62)', () => {
  it('uses the fast bootstrap cadence until a payload has loaded', () => {
    expect(petPollInterval({ active: false, blurred: false, loaded: false, offline: false })).toBe(PET_POLL_MS)
    expect(PET_POLL_MS).toBe(3_000)
  })

  it('uses the foreground cadence for the loaded active pet', () => {
    expect(petPollInterval({ active: true, blurred: false, loaded: true, offline: false })).toBe(PET_ACTIVE_REFRESH_MS)
    expect(PET_ACTIVE_REFRESH_MS).toBe(15_000)
  })

  it('uses the slower background cadence for loaded non-active profiles', () => {
    expect(petPollInterval({ active: false, blurred: false, loaded: true, offline: false })).toBe(PET_BG_POLL_MS)
    expect(PET_BG_POLL_MS).toBe(30_000)
  })

  it('slows every loaded profile to the blurred cadence when the window is hidden', () => {
    expect(petPollInterval({ active: true, blurred: true, loaded: true, offline: false })).toBe(PET_BLURRED_POLL_MS)
    expect(petPollInterval({ active: false, blurred: true, loaded: true, offline: false })).toBe(PET_BLURRED_POLL_MS)
    expect(PET_BLURRED_POLL_MS).toBe(120_000)
  })

  it('skips polling entirely while offline or reauth-required', () => {
    expect(petPollInterval({ active: false, blurred: false, loaded: true, offline: true })).toBeNull()
    expect(petPollInterval({ active: true, blurred: false, loaded: false, offline: true })).toBeNull()
  })
})

describe('background poll lane (test 62)', () => {
  it('runs at most one background poll at a time', async () => {
    const lane = createPollLane(PET_BG_POLL_CONCURRENCY)
    let running = 0
    let maxRunning = 0

    const task = () =>
      new Promise<void>(resolve => {
        running += 1
        maxRunning = Math.max(maxRunning, running)
        queueMicrotask(() => {
          running -= 1
          resolve()
        })
      })

    await Promise.all([lane.runBackground(task), lane.runBackground(task), lane.runBackground(task)])

    expect(maxRunning).toBe(1)
  })

  it('never queues a foreground poll behind background work', async () => {
    const lane = createPollLane(1)
    const order: string[] = []

    // A background task that holds the single lane until released.
    let release!: () => void
    const held = lane.runBackground(
      () =>
        new Promise<void>(resolve => {
          release = resolve
        })
    )

    // Foreground runs immediately even though the background lane is saturated.
    await lane.runForeground(async () => {
      order.push('foreground')
    })

    expect(order).toEqual(['foreground'])

    release()
    await held
  })
})

describe('stagger', () => {
  it('spreads background profiles across the window without colliding on tick 0', () => {
    const a = staggerOffset(0)
    const b = staggerOffset(1)
    const c = staggerOffset(2)

    expect(a).toBe(0)
    expect(b).toBeGreaterThan(0)
    expect(c).toBeGreaterThan(b)
    expect(c).toBeLessThan(PET_BG_POLL_MS)
  })
})

describe('enabled-profile cap (test 62)', () => {
  it('bounds enabled pinned profiles (soft warning at 4, hard stop at 8)', () => {
    expect(PINNED_SOFT_CAP).toBe(4)
    expect(PINNED_HARD_CAP).toBe(8)
  })
})
