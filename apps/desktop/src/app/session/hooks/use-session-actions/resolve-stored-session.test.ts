import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { getSession } from '@/hermes'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { resolveSessionProfile, resolveStoredSession } from './utils'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getSession: vi.fn()
}))

const mockGetSession = vi.mocked(getSession)

const session = (over: Partial<SessionInfo>): SessionInfo => over as SessionInfo

const profiles = (...names: string[]) => names.map(name => ({ name }) as never)

describe('resolveStoredSession profile ownership', () => {
  beforeEach(() => {
    $sessions.set([])
    $profiles.set(profiles('default', 'meta'))
    $activeGatewayProfile.set('meta')
    mockGetSession.mockReset()
  })

  afterEach(() => {
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

  it('treats a profile-less cache hit as unresolved when multiple profiles exist', async () => {
    $sessions.set([session({ id: 's1' })])
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    // rung 2 (bare) then rung 3 (stamped cross-profile probe)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
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

  it('skips an owning empty unknown shadow when another profile owns a materialized twin', async () => {
    // Legacy damage: active profile (meta/production) holds a titled empty
    // source=unknown shadow for the same id that default owns as a real desktop row.
    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(resolved?.message_count).toBe(4)
    expect(resolved?.source).toBe('desktop')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('keeps probing past an earlier zero-message known-source for a transcript-bearing twin', async () => {
    // Profile order meta → other → default: remember the first known-source
    // (desktop/0) compression-root candidate, but keep probing so a later
    // transcript-bearing twin still wins immediately.
    $profiles.set(profiles('meta', 'other', 'default'))
    $activeGatewayProfile.set('meta')

    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const zeroMessageOther = session({
      id: 's1',
      message_count: 0,
      profile: 'other',
      source: 'desktop',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(zeroMessageOther)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(resolved?.message_count).toBe(4)
    expect(resolved?.source).toBe('desktop')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'other')
    expect(mockGetSession).toHaveBeenNthCalledWith(3, 's1', 'default')
  })

  it('falls back to the first known-source zero-message twin when no transcript candidate exists', async () => {
    // Raw REST getSession returns the exact row and does not walk the
    // compression chain — a legitimate compression root can be desktop/0.
    // Prefer that known-source candidate over the deferred legacy shadow.
    $profiles.set(profiles('meta', 'other', 'default'))
    $activeGatewayProfile.set('meta')

    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Untitled draft'
    })

    const zeroMessageOther = session({
      id: 's1',
      message_count: 0,
      profile: 'other',
      source: 'desktop',
      title: 'Untitled draft'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(zeroMessageOther)
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('other')
    expect(resolved?.message_count).toBe(0)
    expect(resolved?.source).toBe('desktop')
    expect(resolved?.title).toBe('Untitled draft')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('other')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'other')
    expect(mockGetSession).toHaveBeenNthCalledWith(3, 's1', 'default')
  })

  it('keeps a titled empty unknown row when no other profile owns a materialized twin', async () => {
    const emptyDraft = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Untitled draft'
    })

    $sessions.set([emptyDraft])
    mockGetSession.mockResolvedValueOnce(emptyDraft)
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect(resolved?.message_count).toBe(0)
    expect(resolved?.source).toBe('unknown')
    expect(resolved?.title).toBe('Untitled draft')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('returns a single-profile empty unknown draft from cache without probing', async () => {
    // No cross-profile twin can exist, so the exact legacy shadow shape must
    // not force a getSession round-trip.
    $profiles.set(profiles('default'))
    $activeGatewayProfile.set('default')
    $sessions.set([
      session({
        id: 's1',
        message_count: 0,
        profile: 'default',
        source: 'unknown',
        title: 'Untitled draft'
      })
    ])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.id).toBe('s1')
    expect(resolved?.source).toBe('unknown')
    expect(resolved?.message_count).toBe(0)
    expect(resolved?.title).toBe('Untitled draft')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('does not treat an untitled empty unknown row as the legacy title shadow', async () => {
    // The observed legacy damage is a title-generation stub: source=unknown,
    // message_count=0, and a persisted title. An untitled auxiliary/accounting
    // row is ambiguous and must not be retargeted to a same-id session owned by
    // another profile.
    const ambiguous = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: null
    })

    $sessions.set([ambiguous])
    mockGetSession.mockResolvedValueOnce(
      session({
        id: 's1',
        message_count: 4,
        profile: 'default',
        source: 'desktop',
        title: 'Different conversation'
      })
    )

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(ambiguous)
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('keeps a titled known-source zero-message draft from cache without probing', async () => {
    // `/title` before the first prompt intentionally creates desktop/0 or
    // tui/0 rows. A title alone does not make a row a legacy shadow.
    const draft = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'desktop',
      title: 'Planned conversation'
    })

    $sessions.set([draft])

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(draft)
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('does not treat omitted message_count as the legacy empty shadow', async () => {
    // Only message_count === 0 is the damaged shape; undefined must keep the
    // direct cache short-circuit even under multi-profile.
    const ambiguous = session({
      id: 's1',
      profile: 'meta',
      source: 'unknown',
      title: 'Maybe a draft'
    })

    $sessions.set([ambiguous])

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(ambiguous)
    expect(resolved?.message_count).toBeUndefined()
    expect(mockGetSession).not.toHaveBeenCalled()
  })
})
