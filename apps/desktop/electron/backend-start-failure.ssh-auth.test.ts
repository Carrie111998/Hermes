/**
 * Unit tests for shouldLatchSshAuthFailure — the predicate that prevents
 * the SSH auth-failed retry loop that locks users out of Settings (#72698).
 */

import { describe, expect, it } from 'vitest'
import { shouldLatchSshAuthFailure } from './backend-start-failure'

describe('shouldLatchSshAuthFailure', () => {
  it('returns false when not remote', () => {
    expect(shouldLatchSshAuthFailure({ attemptedRemote: false, isSshAuthFailed: true })).toBe(false)
  })

  it('returns false when remote but not an SSH auth failure', () => {
    expect(shouldLatchSshAuthFailure({ attemptedRemote: true, isSshAuthFailed: false })).toBe(false)
  })

  it('returns true when remote AND SSH auth failed', () => {
    expect(shouldLatchSshAuthFailure({ attemptedRemote: true, isSshAuthFailed: true })).toBe(true)
  })

  it('returns false when neither is true', () => {
    expect(shouldLatchSshAuthFailure({ attemptedRemote: false, isSshAuthFailed: false })).toBe(false)
  })
})
