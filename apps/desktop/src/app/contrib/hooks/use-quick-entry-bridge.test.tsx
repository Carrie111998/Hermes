import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'
import { $notifications, clearNotifications } from '@/store/notifications'
import type { QuickEntryStatePush, QuickEntrySubmitPayload } from '@/store/quick-entry'
import { setSessions } from '@/store/session'
import { setSessionTileDelegate } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { useQuickEntryBridge } from './use-quick-entry-bridge'

const row = (profile: string, title: string, id = 'shared'): SessionInfo =>
  ({
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile,
    source: null,
    started_at: 0,
    title
  }) as SessionInfo

describe('useQuickEntryBridge session ownership', () => {
  let submitFromQuickWindow: ((payload: QuickEntrySubmitPayload) => void) | null
  const pushState = vi.fn<(payload: QuickEntryStatePush) => void>()

  beforeEach(() => {
    submitFromQuickWindow = null
    pushState.mockClear()
    setSessions([row('alpha', 'Alpha'), row('beta', 'Beta')])
    clearNotifications()

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        quickEntry: {
          onSubmit: vi.fn((callback: (payload: QuickEntrySubmitPayload) => void) => {
            submitFromQuickWindow = callback

            return () => {
              submitFromQuickWindow = null
            }
          }),
          pushState
        }
      }
    })
  })

  afterEach(() => {
    setSessions([])
    clearNotifications()
  })

  it('publishes collision-safe targets and resumes the selected exact owner', async () => {
    const resumeTile = vi.fn(async () => 'runtime-alpha')
    const submitToSession = vi.fn(async () => undefined)

    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash: vi.fn(async () => undefined),
      interruptSession: vi.fn(async () => undefined),
      resumeTile,
      submitToSession,
      updateSession: vi.fn()
    })

    const { unmount } = renderHook(() =>
      useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText: vi.fn() })
    )

    expect(pushState).toHaveBeenCalledWith({
      connected: false,
      sessions: [
        {
          id: sessionIdentityKey('shared', 'alpha'),
          target: { kind: 'session', profile: 'alpha', storedSessionId: 'shared' },
          title: 'Alpha'
        },
        {
          id: sessionIdentityKey('shared', 'beta'),
          target: { kind: 'session', profile: 'beta', storedSessionId: 'shared' },
          title: 'Beta'
        }
      ]
    })

    submitFromQuickWindow?.({
      target: { kind: 'session', profile: 'alpha', storedSessionId: 'shared' },
      text: 'owned prompt'
    })

    await waitFor(() => expect(resumeTile).toHaveBeenCalledWith('shared', 'alpha'))
    expect(submitToSession).toHaveBeenCalledWith('runtime-alpha', 'owned prompt', 'alpha')

    unmount()
  })

  it('preserves an exact opaque target containing the identity separator', async () => {
    const opaqueId = 'left\0right'
    const resumeTile = vi.fn(async () => 'runtime-alpha')

    setSessions([row('alpha', 'Alpha', opaqueId)])
    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash: vi.fn(async () => undefined),
      interruptSession: vi.fn(async () => undefined),
      resumeTile,
      submitToSession: vi.fn(async () => undefined),
      updateSession: vi.fn()
    })

    const { unmount } = renderHook(() =>
      useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText: vi.fn() })
    )

    submitFromQuickWindow?.({
      target: { kind: 'session', profile: 'alpha', storedSessionId: opaqueId },
      text: 'opaque prompt'
    })

    await waitFor(() => expect(resumeTile).toHaveBeenCalledWith(opaqueId, 'alpha'))
    unmount()
  })

  it('does not confuse opaque IDs with control names or another compound picker id', async () => {
    const compoundCollision = sessionIdentityKey('shared', 'alpha')
    const resumeTile = vi.fn(async () => 'runtime-exact')
    const submitToSession = vi.fn(async () => undefined)

    setSessions([
      row('alpha', 'Alpha', 'shared'),
      row('beta', 'Beta', compoundCollision),
      row('gamma', 'Gamma', 'current')
    ])
    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash: vi.fn(async () => undefined),
      interruptSession: vi.fn(async () => undefined),
      resumeTile,
      submitToSession,
      updateSession: vi.fn()
    })

    const { unmount } = renderHook(() =>
      useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText: vi.fn() })
    )

    submitFromQuickWindow?.({
      target: { kind: 'session', profile: 'beta', storedSessionId: compoundCollision },
      text: 'compound collision'
    })
    submitFromQuickWindow?.({
      target: { kind: 'session', profile: 'gamma', storedSessionId: 'current' },
      text: 'control collision'
    })

    await waitFor(() => expect(resumeTile).toHaveBeenCalledTimes(2))
    expect(resumeTile).toHaveBeenNthCalledWith(1, compoundCollision, 'beta')
    expect(resumeTile).toHaveBeenNthCalledWith(2, 'current', 'gamma')
    expect(submitToSession).toHaveBeenNthCalledWith(1, 'runtime-exact', 'compound collision', 'beta')
    expect(submitToSession).toHaveBeenNthCalledWith(2, 'runtime-exact', 'control collision', 'gamma')

    unmount()
  })

  it('fails closed when a selected session target is stale', async () => {
    const resumeTile = vi.fn(async () => 'runtime-stale')
    const submitText = vi.fn()

    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash: vi.fn(async () => undefined),
      interruptSession: vi.fn(async () => undefined),
      resumeTile,
      submitToSession: vi.fn(async () => undefined),
      updateSession: vi.fn()
    })

    const { unmount } = renderHook(() =>
      useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText })
    )

    setSessions([])
    submitFromQuickWindow?.({
      target: { kind: 'session', profile: 'alpha', storedSessionId: 'shared' },
      text: 'do not redirect'
    })
    await Promise.resolve()

    expect(resumeTile).not.toHaveBeenCalled()
    expect(submitText).not.toHaveBeenCalled()
    expect($notifications.get()[0]?.kind).toBe('error')
    unmount()
  })

  it('does not redirect into the visible chat when an owned-session resume rejects', async () => {
    const resumeTile = vi.fn(async () => Promise.reject(new Error('resume failed')))
    const submitToSession = vi.fn(async () => undefined)
    const submitText = vi.fn()

    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash: vi.fn(async () => undefined),
      interruptSession: vi.fn(async () => undefined),
      resumeTile,
      submitToSession,
      updateSession: vi.fn()
    })

    const { unmount } = renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText }))

    submitFromQuickWindow?.({
      target: { kind: 'session', profile: 'alpha', storedSessionId: 'shared' },
      text: 'do not redirect'
    })

    await waitFor(() => expect(resumeTile).toHaveBeenCalledWith('shared', 'alpha'))
    await Promise.resolve()

    expect(submitToSession).not.toHaveBeenCalled()
    expect(submitText).not.toHaveBeenCalled()
    expect($notifications.get()[0]?.kind).toBe('error')
    unmount()
  })
})