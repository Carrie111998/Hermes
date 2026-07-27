import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { $activeGatewayProfile } from '@/store/profile'
import { $selectedStoredSessionId, setSessions } from '@/store/session'
import {
  $sessionTiles,
  closeSessionTile,
  discardSessionTile,
  locateSessionTile,
  openSessionTile,
  patchSessionTile,
  patchSessionTileAt,
  sessionTileDelegate
} from '@/store/session-states'
import type { SessionInfo, SessionResumeResponse } from '@/types/hermes'

import { useSessionTileDelegate } from './use-session-tile-delegate'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getSession: vi.fn(),
  getSessionMessages: vi.fn(async () => ({ messages: [], session_id: '' }))
}))

const { getSession, getSessionMessages } = await import('@/hermes')

const deferred = <T>() => {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

const row = (over: Partial<SessionInfo>): SessionInfo =>
  ({
    ended_at: null,
    id: 'live',
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source: null,
    started_at: 0,
    title: null,
    ...over
  }) as SessionInfo

function renderTile(requestGateway: ReturnType<typeof vi.fn>) {
  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchStoredSession: vi.fn(async () => undefined),
      executeSlashCommand: vi.fn(async () => undefined) as never,
      removeSession: vi.fn(async () => undefined),
      requestGateway: requestGateway as never,
      runtimeIdByStoredSessionIdRef: { current: new Map() },
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn()
    })
  )
}

describe('useSessionTileDelegate resumeTile', () => {
  beforeEach(() => {
    $activeGatewayProfile.set('default')
    $selectedStoredSessionId.set(null)
    setSessions([])
    $sessionTiles.set([])
    vi.mocked(getSession).mockReset()
    vi.mocked(getSessionMessages).mockClear()
  })

  afterEach(() => {
    $activeGatewayProfile.set('default')
    $selectedStoredSessionId.set(null)
    setSessions([])
    $sessionTiles.set([])
  })

  it('carries the owning profile into a cold tile resume so it cannot fork profiles', async () => {
    // A tile opens a session owned by another profile. Resuming without the
    // profile lets the gateway fall back to the launch-profile DB and clone the
    // conversation into the wrong profile (#67603). The owning profile must ride
    // both the transcript prefetch and the resume RPC.
    setSessions([row({ id: 'stored-x', profile: 'ai-engineer' })])
    $sessionTiles.set([{ storedSessionId: 'stored-x' }])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-1' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    const location = locateSessionTile('stored-x')!
    const resumed = await sessionTileDelegate()!.resumeTile('stored-x')
    patchSessionTileAt(location, { profile: resumed.profile, runtimeId: resumed.runtimeId })

    expect(resumed).toEqual({ profile: 'ai-engineer', runtimeId: 'runtime-1' })
    expect(getSessionMessages).toHaveBeenCalledWith('stored-x', 'ai-engineer')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-x',
      cols: 96,
      profile: 'ai-engineer'
    })
    expect($sessionTiles.get()).toContainEqual(
      expect.objectContaining({ profile: 'ai-engineer', storedSessionId: 'stored-x' })
    )
  })

  it('resolves and carries a default-profile session explicitly', async () => {
    setSessions([row({ id: 'stored-y', profile: 'default' })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-2' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    await sessionTileDelegate()!.resumeTile('stored-y')

    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-y',
      cols: 96,
      profile: 'default'
    })
  })

  it('does not reuse an id-only warm runtime when the tile has an explicit owner', async () => {
    const storedSessionId = 'same-id'
    $sessionTiles.set([{ profile: 'profile-b', storedSessionId }])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-b' } as never) : ({} as never)
    )

    renderHook(() =>
      useSessionTileDelegate({
        archiveSession: vi.fn(async () => undefined),
        branchStoredSession: vi.fn(async () => undefined),
        executeSlashCommand: vi.fn(async () => undefined) as never,
        removeSession: vi.fn(async () => undefined),
        requestGateway: requestGateway as never,
        runtimeIdByStoredSessionIdRef: { current: new Map([[storedSessionId, 'runtime-a']]) },
        sessionStateByRuntimeIdRef: {
          current: new Map([['runtime-a', { messages: [], storedSessionId } as never]])
        },
        updateSessionState: vi.fn()
      })
    )

    expect(await sessionTileDelegate()!.resumeTile(storedSessionId, 'profile-b')).toEqual({
      profile: 'profile-b',
      runtimeId: 'runtime-b'
    })
    expect(getSessionMessages).toHaveBeenCalledWith(storedSessionId, 'profile-b')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: storedSessionId,
      cols: 96,
      profile: 'profile-b'
    })
  })

  it('rejects a stale resume after an equal-id tile changes owners', async () => {
    const storedSessionId = 'retargeted-same-id'
    $sessionTiles.set([{ profile: 'profile-a', storedSessionId }])
    const location = locateSessionTile(storedSessionId)!
    const resume = deferred<SessionResumeResponse>()

    const requestGateway = vi.fn((method: string) =>
      method === 'session.resume' ? resume.promise : Promise.resolve({})
    )

    renderTile(requestGateway)
    const pending = sessionTileDelegate()!.resumeTile(storedSessionId, 'profile-a')

    openSessionTile(storedSessionId, 'right', undefined, undefined, 'profile-b')
    resume.resolve({ session_id: 'runtime-a' } as SessionResumeResponse)
    const stale = await pending

    expect(patchSessionTileAt(location, { profile: stale.profile, runtimeId: stale.runtimeId })).toBe(false)
    expect($sessionTiles.get()).toContainEqual(
      expect.objectContaining({ profile: 'profile-b', runtimeId: undefined, storedSessionId })
    )
  })

  it('forwards the durable owner to destructive tile actions', async () => {
    const archiveSession = vi.fn(async () => undefined)
    const branchStoredSession = vi.fn(async () => undefined)
    const removeSession = vi.fn(async () => undefined)

    renderHook(() =>
      useSessionTileDelegate({
        archiveSession,
        branchStoredSession,
        executeSlashCommand: vi.fn(async () => undefined) as never,
        removeSession,
        requestGateway: vi.fn() as never,
        runtimeIdByStoredSessionIdRef: { current: new Map() },
        sessionStateByRuntimeIdRef: { current: new Map() },
        updateSessionState: vi.fn()
      })
    )

    await sessionTileDelegate()!.archiveSession('same-id', 'profile-b')
    await sessionTileDelegate()!.branchSession('same-id', 'profile-b')
    await sessionTileDelegate()!.deleteSession('same-id', 'profile-b')

    expect(archiveSession).toHaveBeenCalledWith('same-id', 'profile-b')
    expect(branchStoredSession).toHaveBeenCalledWith('same-id', 'profile-b')
    expect(removeSession).toHaveBeenCalledWith('same-id', 'profile-b')
  })

  it('backfills the origin bucket after a profile switch without touching a duplicate id', async () => {
    const storedSessionId = 'duplicate-across-profile-buckets'
    const originBucket = 'resume-race-origin'
    const activeBucket = 'resume-race-active'

    $activeGatewayProfile.set(originBucket)
    openSessionTile(storedSessionId, 'right', undefined, undefined, originBucket)
    // Simulate an older v2 record whose bucket is known but owner was not yet
    // persisted.
    patchSessionTile(storedSessionId, { profile: undefined })

    $activeGatewayProfile.set(activeBucket)
    openSessionTile(storedSessionId, 'right', undefined, undefined, 'other-owner')
    $activeGatewayProfile.set(originBucket)

    const lookup = deferred<SessionInfo>()
    vi.mocked(getSession).mockReturnValueOnce(lookup.promise)

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-origin' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    const location = locateSessionTile(storedSessionId)!
    const pending = sessionTileDelegate()!.resumeTile(storedSessionId)

    // Settle profile discovery only after the visible bucket has changed.
    $activeGatewayProfile.set(activeBucket)
    lookup.resolve(row({ id: storedSessionId, profile: 'resolved-owner' }))
    const resumed = await pending
    patchSessionTileAt(location, { profile: resumed.profile, runtimeId: resumed.runtimeId })

    expect($sessionTiles.get()).toEqual([expect.objectContaining({ profile: 'other-owner', storedSessionId })])

    $activeGatewayProfile.set(originBucket)
    expect($sessionTiles.get()).toEqual([expect.objectContaining({ profile: 'resolved-owner', storedSessionId })])

    discardSessionTile(storedSessionId)
    $activeGatewayProfile.set(activeBucket)
    discardSessionTile(storedSessionId)
  })

  it('does not resurrect a tile closed while profile resolution is pending', async () => {
    const storedSessionId = 'closed-during-profile-resolution'
    const originBucket = 'resume-race-closed'

    $activeGatewayProfile.set(originBucket)
    openSessionTile(storedSessionId, 'right', undefined, undefined, originBucket)
    patchSessionTile(storedSessionId, { profile: undefined })

    const lookup = deferred<SessionInfo>()
    vi.mocked(getSession).mockReturnValueOnce(lookup.promise)

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-closed' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    const location = locateSessionTile(storedSessionId)!
    const pending = sessionTileDelegate()!.resumeTile(storedSessionId)

    closeSessionTile(storedSessionId)
    lookup.resolve(row({ id: storedSessionId, profile: 'resolved-owner' }))
    const resumed = await pending
    patchSessionTileAt(location, { profile: resumed.profile, runtimeId: resumed.runtimeId })

    expect($sessionTiles.get()).toEqual([])

    // Switching away and back hydrates from the persisted bucket map. The
    // absent tile proves the delayed backfill did not recreate it there.
    $activeGatewayProfile.set('default')
    $activeGatewayProfile.set(originBucket)
    expect($sessionTiles.get()).toEqual([])
  })
})
