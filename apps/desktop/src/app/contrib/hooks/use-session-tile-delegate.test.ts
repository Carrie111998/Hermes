import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { setSessions } from '@/store/session'
import { sessionTileDelegate } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { useSessionTileDelegate } from './use-session-tile-delegate'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getSessionMessages: vi.fn(async () => ({ messages: [], session_id: '' }))
}))

const { getSessionMessages } = await import('@/hermes')

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

function renderTile(
  requestGateway: ReturnType<typeof vi.fn>,
  actions: {
    archiveSession?: (storedSessionId: string, profile?: null | string) => Promise<unknown>
    branchStoredSession?: (storedSessionId: string, profile?: null | string) => Promise<unknown>
    removeSession?: (storedSessionId: string, profile?: null | string) => Promise<unknown>
  } = {},
  requestGatewayForProfile?: ReturnType<typeof vi.fn>
) {
  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: actions.archiveSession ?? vi.fn(async () => undefined),
      branchStoredSession: actions.branchStoredSession ?? vi.fn(async () => undefined),
      executeSlashCommand: vi.fn(async () => undefined) as never,
      removeSession: actions.removeSession ?? vi.fn(async () => undefined),
      requestGateway: requestGateway as never,
      requestGatewayForProfile: requestGatewayForProfile as never,
      runtimeIdByStoredSessionIdRef: { current: new Map() },
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn()
    })
  )
}

describe('useSessionTileDelegate owned actions', () => {
  it('forwards the tab owner instead of substituting the active profile', async () => {
    const archiveSession = vi.fn(async () => undefined)
    const branchStoredSession = vi.fn(async () => undefined)
    const removeSession = vi.fn(async () => undefined)

    renderTile(vi.fn(), { archiveSession, branchStoredSession, removeSession })
    await sessionTileDelegate()!.archiveSession('shared', 'alpha')
    await sessionTileDelegate()!.branchSession('shared', 'alpha')
    await sessionTileDelegate()!.deleteSession('shared', 'alpha')

    expect(archiveSession).toHaveBeenCalledWith('shared', 'alpha')
    expect(branchStoredSession).toHaveBeenCalledWith('shared', 'alpha')
    expect(removeSession).toHaveBeenCalledWith('shared', 'alpha')
  })
})

describe('useSessionTileDelegate resumeTile', () => {
  beforeEach(() => {
    setSessions([])
    vi.mocked(getSessionMessages).mockClear()
  })

  afterEach(() => {
    setSessions([])
  })

  it('carries the owning profile into a cold tile resume so it cannot fork profiles', async () => {
    // A tile opens a session owned by another profile. Resuming without the
    // profile lets the gateway fall back to the launch-profile DB and clone the
    // conversation into the wrong profile (#67603). The owning profile must ride
    // both the transcript prefetch and the resume RPC.
    setSessions([row({ id: 'stored-x', profile: 'ai-engineer' })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-1' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-x', 'ai-engineer')

    expect(runtimeId).toBe('runtime-1')
    expect(getSessionMessages).toHaveBeenCalledWith('stored-x', 'ai-engineer')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-x',
      cols: 96,
      profile: 'ai-engineer'
    })
  })

  it('resolves and carries a default-profile session explicitly', async () => {
    setSessions([row({ id: 'stored-y', profile: 'default' })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-2' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    await sessionTileDelegate()!.resumeTile('stored-y', 'default')

    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-y',
      cols: 96,
      profile: 'default'
    })
  })

  it('uses the tile bucket owner instead of a sole colliding cache row', async () => {
    setSessions([row({ id: 'shared', profile: 'alpha' })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-beta' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    await sessionTileDelegate()!.resumeTile('shared', 'beta')

    expect(getSessionMessages).toHaveBeenCalledWith('shared', 'beta')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'shared',
      cols: 96,
      profile: 'beta'
    })
  })

  it('resumes and submits through the captured owner gateway', async () => {
    const requestGateway = vi.fn(async () => ({} as never))

    const requestGatewayForProfile = vi.fn(async (_profile: string, method: string) =>
      (method === 'session.resume' ? { session_id: 'runtime-alpha' } : {}) as never
    )

    renderTile(requestGateway, {}, requestGatewayForProfile)

    const runtimeId = await sessionTileDelegate()!.resumeTile('shared', 'alpha')
    await sessionTileDelegate()!.submitToSession(runtimeId, 'owned prompt', 'alpha')

    expect(requestGatewayForProfile).toHaveBeenNthCalledWith(1, 'alpha', 'session.resume', {
      session_id: 'shared',
      cols: 96,
      profile: 'alpha'
    })
    expect(requestGatewayForProfile).toHaveBeenNthCalledWith(
      2,
      'alpha',
      'prompt.submit',
      { session_id: 'runtime-alpha', text: 'owned prompt' },
      1_800_000
    )
    expect(requestGateway).not.toHaveBeenCalled()
  })
})
