import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
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
  options: {
    runtimeIdByStoredSessionId?: Map<string, string>
    sessionStateByRuntimeId?: Map<string, ReturnType<typeof createClientSessionState>>
  } = {}
) {
  const runtimeIdByStoredSessionId = options.runtimeIdByStoredSessionId ?? new Map()
  const sessionStateByRuntimeId = options.sessionStateByRuntimeId ?? new Map()

  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchStoredSession: vi.fn(async () => undefined),
      executeSlashCommand: vi.fn(async () => undefined) as never,
      removeSession: vi.fn(async () => undefined),
      requestGateway: requestGateway as never,
      runtimeIdByStoredSessionIdRef: { current: runtimeIdByStoredSessionId },
      sessionStateByRuntimeIdRef: { current: sessionStateByRuntimeId },
      updateSessionState: vi.fn((runtimeId, updater, storedSessionId) => {
        const current = sessionStateByRuntimeId.get(runtimeId) ?? createClientSessionState(storedSessionId ?? null)
        const next = updater(current)
        sessionStateByRuntimeId.set(runtimeId, next)

        if (storedSessionId) {
          runtimeIdByStoredSessionId.set(storedSessionId, runtimeId)
        }

        return next
      })
    })
  )

  return { runtimeIdByStoredSessionId, sessionStateByRuntimeId }
}

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
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-x')

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
    await sessionTileDelegate()!.resumeTile('stored-y')

    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-y',
      cols: 96,
      profile: 'default'
    })
  })

  it('honors an explicit caller profile without relying on the global session cache', async () => {
    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-3' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    await sessionTileDelegate()!.resumeTile('stored-z', 'wake-profile')

    expect(getSessionMessages).toHaveBeenCalledWith('stored-z', 'wake-profile')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-z',
      cols: 96,
      profile: 'wake-profile'
    })
  })

  it('does not replace another profile runtime in the global stored-id cache', async () => {
    const runtimeIdByStoredSessionId = new Map([['shared-id', 'runtime-a']])
    const sessionStateByRuntimeId = new Map([['runtime-a', createClientSessionState('shared-id')]])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-b' } as never) : ({} as never)
    )

    renderTile(requestGateway, { runtimeIdByStoredSessionId, sessionStateByRuntimeId })
    await sessionTileDelegate()!.resumeTile('shared-id', 'profile-b')

    expect(runtimeIdByStoredSessionId.get('shared-id')).toBe('runtime-a')
    expect(sessionStateByRuntimeId.get('runtime-b')?.storedSessionId).toBe('shared-id')
  })

  it('leaves no global stored-id mapping after an explicit-profile cold resume', async () => {
    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-c' } as never) : ({} as never)
    )

    const { runtimeIdByStoredSessionId } = renderTile(requestGateway)

    await sessionTileDelegate()!.resumeTile('shared-id', 'profile-c')

    expect(runtimeIdByStoredSessionId.has('shared-id')).toBe(false)
  })
})
