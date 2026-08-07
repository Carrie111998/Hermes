/**
 * Behavioral tests for avatar pack contracts.
 *
 * Covers: activity→state mapping priority, asset extension validation,
 * renderer type guard against real production exports (no local copies).
 */

import { describe, expect, it } from 'vitest'

import { activityToAvatarState, isValidRendererType } from './avatar-pack-store'
import {
  isAssetExt,
  isImageExt,
  isVideoExt
} from './avatar-pack-types'

// ── Tests ──────────────────────────────────────────────────────────────────────

describe('activityToAvatarState priority chain', () => {
  it('defaults to idle with no activity', () => {
    expect(activityToAvatarState({})).toBe('idle')
  })

  it('returns talk for error, celebrate, and justCompleted (top priority)', () => {
    expect(activityToAvatarState({ error: true })).toBe('talk')
    expect(activityToAvatarState({ celebrate: true })).toBe('talk')
    expect(activityToAvatarState({ justCompleted: true })).toBe('talk')
  })

  it('returns listen for awaitingInput, below terminal signals', () => {
    expect(activityToAvatarState({ awaitingInput: true })).toBe('listen')
    // error + awaitingInput → talk wins (error is higher priority)
    expect(activityToAvatarState({ error: true, awaitingInput: true })).toBe('talk')
  })

  it('returns think for toolRunning and reasoning, above bare busy', () => {
    expect(activityToAvatarState({ toolRunning: true })).toBe('think')
    expect(activityToAvatarState({ reasoning: true })).toBe('think')
    // awaitingInput beats think
    expect(activityToAvatarState({ awaitingInput: true, toolRunning: true })).toBe('listen')
  })

  it('returns talk for busy as lowest non-idle signal', () => {
    expect(activityToAvatarState({ busy: true })).toBe('talk')
  })

  it('honors full priority: error > celebrate > complete > awaiting > tool > busy', () => {
    // error beats everything
    expect(activityToAvatarState({
      error: true, celebrate: true, justCompleted: true,
      awaitingInput: true, toolRunning: true, reasoning: true, busy: true
    })).toBe('talk')

    // celebrate beats everything below it
    expect(activityToAvatarState({
      celebrate: true, awaitingInput: true, toolRunning: true, busy: true
    })).toBe('talk')

    // awaitingInput beats think/busy
    expect(activityToAvatarState({
      awaitingInput: true, toolRunning: true, busy: true
    })).toBe('listen')

    // toolRunning beats bare busy
    expect(activityToAvatarState({ toolRunning: true, busy: true })).toBe('think')
  })
})

describe('isValidRendererType (production export)', () => {
  it('accepts petdex and avatar-pack', () => {
    expect(isValidRendererType('petdex')).toBe(true)
    expect(isValidRendererType('avatar-pack')).toBe(true)
  })

  it('rejects invalid values', () => {
    expect(isValidRendererType('')).toBe(false)
    expect(isValidRendererType('hchar')).toBe(false)
    expect(isValidRendererType('unknown')).toBe(false)
  })

  it('preserves the TypeScript type guard', () => {
    const v: string = 'petdex'
    if (isValidRendererType(v)) {
      // If this compiles, the type guard narrows correctly.
      const _check: 'petdex' | 'avatar-pack' = v
      expect(_check).toBe('petdex')
    }
  })
})

describe('asset extension validation (production exports)', () => {
  it('recognizes video extensions', () => {
    expect(isVideoExt('.webm')).toBe(true)
    expect(isVideoExt('.mp4')).toBe(true)
    expect(isVideoExt('.mov')).toBe(true)
  })

  it('recognizes image extensions', () => {
    expect(isImageExt('.gif')).toBe(true)
    expect(isImageExt('.webp')).toBe(true)
    expect(isImageExt('.png')).toBe(true)
    expect(isImageExt('.svg')).toBe(true)
  })

  it('is case-insensitive for extensions', () => {
    expect(isVideoExt('.WEBM')).toBe(true)
    expect(isVideoExt('.Mp4')).toBe(true)
    expect(isImageExt('.PNG')).toBe(true)
    expect(isImageExt('.Svg')).toBe(true)
  })

  it('rejects unsupported extensions', () => {
    expect(isAssetExt('.jpg')).toBe(false)
    expect(isAssetExt('.jpeg')).toBe(false)
    expect(isAssetExt('.avi')).toBe(false)
    expect(isAssetExt('.exe')).toBe(false)
    expect(isAssetExt('.js')).toBe(false)
    expect(isAssetExt('.html')).toBe(false)
    expect(isAssetExt('')).toBe(false)
    expect(isAssetExt('png')).toBe(false) // no dot
  })

  it('isAssetExt validates all 7 supported formats', () => {
    const supported = ['.webm', '.mp4', '.mov', '.gif', '.webp', '.png', '.svg']
    for (const ext of supported) {
      expect(isAssetExt(ext)).toBe(true)
    }
  })
})
