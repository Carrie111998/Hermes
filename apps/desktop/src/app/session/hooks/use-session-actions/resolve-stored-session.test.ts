import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { getSession } from '@/hermes'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $cronSessions, $messagingSessions, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import {
  classifySessionLookupError,
  publishResolvedSessionForRestore,
  resolveSessionProfile,
  resolveStoredSession,
  resolveStoredSessionForRestore
} from './utils'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getSession: vi.fn()
}))

const mockGetSession = vi.mocked(getSession)

const session = (over: Partial<SessionInfo>): SessionInfo => over as SessionInfo

const profiles = (...names: string[]) => names.map(name => ({ name }) as never)

describe('resolveStoredSession profile ownership', () => {
  beforeEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $profiles.set(profiles('default', 'meta'))
    $activeGatewayProfile.set('meta')
    mockGetSession.mockReset()
  })

  afterEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $profiles.set([])
    $activeGatewayProfile.set('default')
  })

  it('returns a cached row that carries an owning profile', async () => {
    $sessions.set([session({ id: 's1', profile: 'default' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it.each([
    ['cron', $cronSessions],
    ['messaging', $messagingSessions]
  ])('resolves a %s sidebar row without duplicating it into regular sessions', async (_source, store) => {
    store.set([session({ id: 's1', profile: 'default' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(mockGetSession).not.toHaveBeenCalled()
    expect($sessions.get()).toEqual([])
  })

  it('treats a profile-less cache hit as unresolved when multiple profiles exist', async () => {
    $sessions.set([session({ id: 's1' })])
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    // rung 2 (active profile) then rung 3 (stamped cross-profile probe)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1', 'meta')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('scopes the first by-id lookup so a miss does not skip the active profile', async () => {
    $activeGatewayProfile.set('brain')
    $profiles.set(profiles('default', 'brain'))
    mockGetSession.mockImplementation(async (id, profile) => {
      if (profile === 'brain') {
        return session({ id, profile: 'brain' })
      }

      throw new Error('404: Session not found')
    })

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('brain')
    expect(mockGetSession).toHaveBeenCalledWith('s1', 'brain')
    expect(mockGetSession).not.toHaveBeenCalledWith('s1')
    expect(mockGetSession).not.toHaveBeenCalledWith('s1', 'default')
  })

  it('accepts a profile-less cache hit for single-profile users', async () => {
    $profiles.set(profiles('default'))
    $sessions.set([session({ id: 's1' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.id).toBe('s1')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('stamps the active profile on a bare by-id hit from an older backend', async () => {
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect(mockGetSession).toHaveBeenCalledWith('s1', 'meta')
    // the upserted cache row is owned too, so the next hit short-circuits
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
  })

  it('probed desktop profile overrides a remote backend answering as its own "default"', async () => {
    // Per-profile remote override: Electron strips the desktop alias before
    // forwarding, so the standalone backend stamps its backend-local root.
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))
    $activeGatewayProfile.set('default')
    $profiles.set(profiles('default', 'meta'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
  })

  it('stamps the probed profile on a scoped hit from an older backend that omits it', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    // the cached row is owned too — no unowned row is ever re-cached
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
  })

  it('resolveSessionProfile routes a default-profile session from a non-default gateway', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    await expect(resolveSessionProfile('s1')).resolves.toBe('default')
  })
})

describe('resolveStoredSessionForRestore exact target lookup', () => {
  const registryTarget = { connectionId: 'source-a', profile: 'meta', storageSuffix: '.source-a' }

  beforeEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    mockGetSession.mockReset()
  })

  afterEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
  })

  it('always probes the exact registry target instead of trusting a colliding bare-id cache row', async () => {
    $sessions.set([session({ connection_id: 'source-b', id: 's1', profile: 'meta' })])
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    const result = await resolveStoredSessionForRestore('s1', registryTarget)

    expect(mockGetSession).toHaveBeenCalledWith('s1', { connectionId: 'source-a', profile: 'meta' })
    expect(result).toMatchObject({
      ownerRoute: { connectionId: 'source-a', profile: 'meta' },
      session: { connection_id: 'source-a', id: 's1', profile: 'meta' },
      status: 'found'
    })
    // Lookup is read-only until the caller validates its activation generation.
    expect($sessions.get()).toEqual([expect.objectContaining({ connection_id: 'source-b', profile: 'meta' })])

    if (result.status !== 'found') {
      throw new Error('expected found restore candidate')
    }

    publishResolvedSessionForRestore(result.session, 's1', registryTarget)
    expect($sessions.get().filter(row => row.id === 's1')).toEqual([
      expect.objectContaining({ connection_id: 'source-a', profile: 'meta' }),
      expect.objectContaining({ connection_id: 'source-b', profile: 'meta' })
    ])
  })

  it('replaces same-owner lineage aliases while preserving foreign twins', () => {
    $sessions.set([
      session({ connection_id: 'source-a', id: 'root-1', profile: 'meta' }),
      session({ connection_id: 'source-b', id: 'root-1', profile: 'meta' })
    ])
    const resolved = session({
      _lineage_root_id: 'root-1',
      connection_id: 'source-a',
      id: 'tip-2',
      profile: 'meta'
    })

    publishResolvedSessionForRestore(resolved, 'tip-2', registryTarget)

    expect($sessions.get()).toEqual([
      expect.objectContaining({ connection_id: 'source-a', id: 'tip-2' }),
      expect.objectContaining({ connection_id: 'source-b', id: 'root-1' })
    ])
  })

  it('does not publish a late candidate before caller generation validation', async () => {
    $sessions.set([session({ connection_id: 'source-b', id: 's1', profile: 'meta' })])
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    await expect(resolveStoredSessionForRestore('s1', registryTarget)).resolves.toMatchObject({ status: 'found' })

    expect($sessions.get()).toEqual([expect.objectContaining({ connection_id: 'source-b' })])
  })

  it('accepts a compressed tip whose lineage root is the requested durable id', async () => {
    mockGetSession.mockResolvedValueOnce(session({ _lineage_root_id: 'root-1', id: 'tip-9' }))

    const result = await resolveStoredSessionForRestore('root-1', registryTarget)

    expect(result).toMatchObject({ status: 'found', session: { _lineage_root_id: 'root-1', id: 'tip-9' } })
  })

  it('routes a legacy/profile-only target without treating null connection as a wildcard', async () => {
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const result = await resolveStoredSessionForRestore('s1', {
      connectionId: null,
      profile: 'meta',
      storageSuffix: ''
    })

    expect(mockGetSession).toHaveBeenCalledWith('s1', { connectionId: null, profile: 'meta' })
    expect(result).toEqual({ status: 'found', session: expect.objectContaining({ id: 's1', profile: 'meta' }) })
  })

  it('keeps a profile-door lookup ownerless and preserves same-id registry twins when publishing', async () => {
    const profileTarget = { connectionId: null, profile: 'meta', storageSuffix: '' }
    $sessions.set([
      session({ connection_id: 'local', id: 's1', profile: 'meta' }),
      session({ connection_id: 'source-b', id: 's1', profile: 'meta' })
    ])
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const result = await resolveStoredSessionForRestore('s1', profileTarget)

    expect(mockGetSession).toHaveBeenCalledWith('s1', { connectionId: null, profile: 'meta' })
    expect(result).toEqual({
      status: 'found',
      session: expect.objectContaining({ connection_id: undefined, id: 's1', profile: 'meta' })
    })

    if (result.status !== 'found') {
      throw new Error('expected found restore candidate')
    }

    publishResolvedSessionForRestore(result.session, 's1', profileTarget)
    expect($sessions.get().filter(row => row.id === 's1')).toEqual([
      expect.objectContaining({ connection_id: undefined, profile: 'meta' }),
      expect.objectContaining({ connection_id: 'local', profile: 'meta' }),
      expect.objectContaining({ connection_id: 'source-b', profile: 'meta' })
    ])
  })

  it('returns not-found only for a target-scoped session-gone response', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    await expect(resolveStoredSessionForRestore('s1', registryTarget)).resolves.toEqual({ status: 'not-found' })
    expect(classifySessionLookupError(new Error('session not found'))).toBe('not-found')
  })

  it.each([
    ['generic endpoint miss', new Error('404 Not Found')],
    ['auth', new Error('401 Unauthorized')],
    ['server', new Error('503 Service Unavailable')],
    ['network', new Error('ECONNREFUSED')],
    ['timeout', new Error('request timed out')],
    ['abort', new DOMException('aborted', 'AbortError')]
  ])('preserves %s failures as inconclusive', async (_name, error) => {
    mockGetSession.mockRejectedValueOnce(error)

    const result = await resolveStoredSessionForRestore('s1', registryTarget)

    expect(result.status).toBe('inconclusive')
    expect(classifySessionLookupError(error)).toBe('inconclusive')
  })

  it('fails closed for malformed, mismatched-lineage, and conflicting-owner responses', async () => {
    mockGetSession
      .mockResolvedValueOnce(null as never)
      .mockResolvedValueOnce(session({ id: 'other' }))
      .mockResolvedValueOnce(session({ connection_id: 'source-b', id: 's1' }))

    await expect(resolveStoredSessionForRestore('s1', registryTarget)).resolves.toMatchObject({
      status: 'inconclusive'
    })
    await expect(resolveStoredSessionForRestore('s1', registryTarget)).resolves.toMatchObject({
      status: 'inconclusive'
    })
    await expect(resolveStoredSessionForRestore('s1', registryTarget)).resolves.toMatchObject({
      status: 'inconclusive'
    })
    expect($sessions.get()).toEqual([])
  })
})
