/**
 * Layer 6c invariants that PR3 satisfied by construction, pinned here so a
 * refactor can't silently break them (deviation rule #2: focused tests assert
 * the seam's guarantees rather than spinning up the whole submit pipeline).
 *
 *  - Test 34: a profile-bound submit's recovery `session.resume` runs on the
 *    profile's OWN gateway and carries the profile param (never the active
 *    gateway, which would fork the conversation into the wrong DB).
 *  - Test 56: a background submit resolves its connection mode from the named
 *    profile's connection (Electron), never the foreground `$connection`, and
 *    exposes the profile-routed requester to the attachment sync.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestGatewayForProfile } from '@/store/gateway'

import { backgroundSubmitExecution } from './profile-submit'

vi.mock('@/store/gateway', async importOriginal => {
  const actual = await importOriginal<typeof import('@/store/gateway')>()

  return {
    ...actual,
    requestGatewayForProfile: vi.fn(async () => ({ session_id: 'resumed-rt' })),
    withProfileGatewayLease: vi.fn(async (_profile: string, run: () => Promise<unknown>) => run())
  }
})

const profileRequest = vi.mocked(requestGatewayForProfile)

const getConnection = vi.fn(async () => ({ mode: 'remote' as const }))

beforeEach(() => {
  vi.clearAllMocks()
  ;(globalThis as { window?: unknown }).window = { hermesDesktop: { getConnection } }
})

afterEach(() => {
  delete (globalThis as { window?: unknown }).window
})

describe('profile-bound submit recovery (test 34)', () => {
  it('routes session.resume through the profile gateway with the profile param', async () => {
    const execution = backgroundSubmitExecution('apollo')

    // The pipeline's session-not-found / timeout recovery calls
    // execution.requestGateway('session.resume', { session_id, profile }).
    const resumed = await execution.requestGateway<{ session_id: string }>('session.resume', {
      session_id: 'stored-123',
      source: 'desktop'
    })

    expect(resumed).toEqual({ session_id: 'resumed-rt' })
    expect(profileRequest).toHaveBeenCalledWith(
      'apollo',
      'session.resume',
      { profile: 'apollo', session_id: 'stored-123', source: 'desktop' },
      undefined,
      undefined
    )
  })

  it('names the execution after its profile so the lock + routing stay per-profile', () => {
    const execution = backgroundSubmitExecution('apollo')

    expect(execution.background).toBe(true)
    expect(execution.profile).toBe('apollo')
  })
})

describe('background attachment sync inputs (test 56)', () => {
  it('resolves connection mode from the named profile, never the foreground $connection', async () => {
    const execution = backgroundSubmitExecution('nova')

    const mode = await execution.resolveConnectionMode()

    expect(mode).toBe('remote')
    // Electron resolves the named profile's connection — not the active one.
    expect(getConnection).toHaveBeenCalledWith('nova')
  })

  it('exposes the profile-routed requester to the sync step', async () => {
    const execution = backgroundSubmitExecution('nova')

    // The attachment sync receives execution.requestGateway; a file.attach through
    // it lands on nova's own socket with the profile param stamped.
    await execution.requestGateway('file.attach', { name: 'a.txt', path: '/a.txt', session_id: 'rt-1' })

    expect(profileRequest).toHaveBeenCalledWith(
      'nova',
      'file.attach',
      { name: 'a.txt', path: '/a.txt', profile: 'nova', session_id: 'rt-1' },
      undefined,
      undefined
    )
  })

  it('falls back to local when the profile connection has no mode', async () => {
    getConnection.mockResolvedValueOnce({} as never)
    const execution = backgroundSubmitExecution('nova')

    expect(await execution.resolveConnectionMode()).toBe('local')
  })
})
