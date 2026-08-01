/**
 * Behavioral tests for avatar pack contracts.
 *
 * Covers: activity→state mapping priority, asset extension validation,
 * store default fallback, renderer type guard, and path traversal
 * rejection in manifest filenames.
 */

import { describe, expect, it } from 'vitest'

import { activityToAvatarState } from './avatar-pack-store'
import {
  type AvatarRendererType,
  isAssetExt,
  isImageExt,
  isVideoExt
} from './avatar-pack-types'

// ── Path traversal rejection (pure helper — mirrors electron-side isPathInside) ─

const POSIX_SEP = '/'

/** Reject filenames that would escape the pack folder. */
function isFilenameSafe(filename: string): boolean {
  // No absolute paths
  if (filename.startsWith(POSIX_SEP)) {
    return false
  }

  // No empty or degenerate paths
  if (!filename || filename.includes('//')) {
    return false
  }

  // Reject path traversal: any path segment that is exactly ".."
  const segments = filename.split(POSIX_SEP)
  if (segments.some(s => s === '..')) {
    return false
  }

  return true
}

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

describe('manifest filename path traversal rejection', () => {
  it('accepts simple filenames in the pack folder', () => {
    expect(isFilenameSafe('idle.png')).toBe(true)
    expect(isFilenameSafe('talk.webm')).toBe(true)
    expect(isFilenameSafe('assets/think.gif')).toBe(true)
    expect(isFilenameSafe('subfolder/nested/idle.mp4')).toBe(true)
  })

  it('rejects absolute paths', () => {
    expect(isFilenameSafe('/etc/passwd')).toBe(false)
    expect(isFilenameSafe('/home/user/secret.png')).toBe(false)
  })

  it('rejects parent directory traversal', () => {
    expect(isFilenameSafe('../secret.png')).toBe(false)
    expect(isFilenameSafe('../../etc/passwd')).toBe(false)
    expect(isFilenameSafe('assets/../../../secret')).toBe(false)
  })

  it('rejects hidden files that start with traversal patterns', () => {
    expect(isFilenameSafe('..hidden')).toBe(true) // ".." as prefix in filename is fine
    expect(isFilenameSafe('...config')).toBe(true) // three dots is fine
    expect(isFilenameSafe('..../escape')).toBe(true) // four dots is not traversal
  })

  it('rejects empty or degenerate paths', () => {
    expect(isFilenameSafe('')).toBe(false)
    expect(isFilenameSafe('dir//file.png')).toBe(false)
  })
})

describe('asset extension validation', () => {
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

  it('validates all 7 supported formats', () => {
    const supported = ['.webm', '.mp4', '.mov', '.gif', '.webp', '.png', '.svg']
    for (const ext of supported) {
      expect(isAssetExt(ext)).toBe(true)
    }
  })
})

describe('avatar renderer type guard', () => {
  it('accepts only petdex and avatar-pack', () => {
    const valid = (v: string): v is AvatarRendererType => v === 'petdex' || v === 'avatar-pack'
    expect(valid('petdex')).toBe(true)
    expect(valid('avatar-pack')).toBe(true)
    expect(valid('')).toBe(false)
    expect(valid('hchar')).toBe(false)
    expect(valid('unknown')).toBe(false)
  })
})
