import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $notifications, clearNotifications } from '@/store/notifications'
import type { QuickEntryStatePush, QuickEntrySubmitPayload } from '@/store/quick-entry'
import { $gatewayState, $sessions } from '@/store/session'
import { $sessionTiles, setSessionTileDelegate } from '@/store/session-states'

import { useQuickEntryBridge } from './use-quick-entry-bridge'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialDesktop = desktopWindow.hermesDesktop
const pushed: QuickEntryStatePush[] = []
let emitSubmit: ((payload: QuickEntrySubmitPayload | string) => void) | null = null

beforeEach(() => {
  pushed.length = 0
  emitSubmit = null
  $gatewayState.set('open')
  $sessions.set([])
  $sessionTiles.set([])
  desktopWindow.hermesDesktop = {
    quickEntry: {
      onSubmit: (callback: (payload: QuickEntrySubmitPayload | string) => void) => {
        emitSubmit = callback

        return () => {
          emitSubmit = null
        }
      },
      pushState: (payload: QuickEntryStatePush) => pushed.push(payload)
    }
  } as unknown as Window['hermesDesktop']
})

afterEach(() => {
  cleanup()
  clearNotifications()
  $sessions.set([])
  $sessionTiles.set([])

  if (initialDesktop) {
    desktopWindow.hermesDesktop = initialDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('Quick Entry duplicate-owner target contract', () => {
  it('surfaces an exact picked target when the background-session bridge is unavailable', async () => {
    const ownerB = { connectionId: 'source-b', mode: 'remote' as const, profile: 'worker' }
    const submitText = vi.fn()

    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText }))

    act(() => {
      emitSubmit?.({
        session: { id: 'shared', ownerGeneration: 9, ownerRoute: ownerB },
        target: 'source-b/worker/shared',
        text: 'owner B only'
      })
    })

    await vi.waitFor(() => expect($notifications.get()).toHaveLength(1))
    expect(submitText).not.toHaveBeenCalled()
  })

  it('keeps the selected duplicate owner exact through post-resume submit', async () => {
    const ownerA = { connectionId: 'source-a', mode: 'remote' as const, profile: 'worker' }
    const ownerB = { connectionId: 'source-b', mode: 'remote' as const, profile: 'worker' }
    const resumeTile = vi.fn(async () => 'runtime-owner-b')
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
    } as never)
    $sessionTiles.set([
      { ownerGeneration: 4, ownerRoute: ownerA, storedSessionId: 'shared' },
      { ownerGeneration: 9, ownerRoute: ownerB, storedSessionId: 'shared' }
    ])
    $sessions.set([
      { connection_id: 'source-a', id: 'shared', profile: 'worker', title: 'Owner A' },
      { connection_id: 'source-b', id: 'shared', profile: 'worker', title: 'Owner B' }
    ] as never)

    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText: vi.fn() }))

    const latest = pushed.at(-1)!
    expect(new Set(latest.sessions.map(session => session.target)).size).toBe(2)
    const ownerBOption = latest.sessions.find(session => session.ownerRoute?.connectionId === 'source-b')!

    act(() => {
      emitSubmit?.({
        session: {
          id: ownerBOption.id,
          ownerGeneration: ownerBOption.ownerGeneration,
          ownerRoute: ownerBOption.ownerRoute
        },
        target: ownerBOption.target!,
        text: 'owner B only'
      })
    })

    await vi.waitFor(() => expect(resumeTile).toHaveBeenCalledWith('shared', 9, ownerB))
    expect(submitToSession).toHaveBeenCalledWith('runtime-owner-b', 'owner B only', ownerB)
  })

  it('never redirects an exact picked target into the ambient composer when resume fails', async () => {
    const ownerB = { connectionId: 'source-b', mode: 'remote' as const, profile: 'worker' }
    const resumeError = new Error('exact owner resume failed')
    const resumeTile = vi.fn(async () => Promise.reject(resumeError))
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
    } as never)

    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText }))

    act(() => {
      emitSubmit?.({
        session: { id: 'shared', ownerGeneration: 9, ownerRoute: ownerB },
        target: 'source-b/worker/shared',
        text: 'owner B only'
      })
    })

    await vi.waitFor(() => expect(resumeTile).toHaveBeenCalledWith('shared', 9, ownerB))
    await vi.waitFor(() => expect($notifications.get()).toHaveLength(1))

    expect(submitToSession).not.toHaveBeenCalled()
    expect(submitText).not.toHaveBeenCalled()
    expect($notifications.get()[0]).toMatchObject({
      kind: 'error',
      title: 'Quick Entry could not reach the selected session'
    })
  })

  it('keeps generation-qualified ownerless metadata exact instead of treating it as a legacy raw id', async () => {
    const resumeTile = vi.fn(async () => Promise.reject(new Error('generation resume failed')))
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
    } as never)

    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText }))

    act(() => {
      emitSubmit?.({
        session: { id: 'ownerless-tile', ownerGeneration: 4 },
        target: 'ownerless-tile',
        text: 'generation four only'
      })
    })

    await vi.waitFor(() => expect(resumeTile).toHaveBeenCalledWith('ownerless-tile', 4, undefined))
    await vi.waitFor(() => expect($notifications.get()).toHaveLength(1))
    expect(submitText).not.toHaveBeenCalled()
  })

  it('preserves the legacy ownerless raw-id fallback when resume fails', async () => {
    const resumeTile = vi.fn(async () => Promise.reject(new Error('legacy resume failed')))
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
    } as never)

    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText }))

    act(() => {
      emitSubmit?.({ target: 'legacy-stored-id', text: 'legacy prompt' })
    })

    await vi.waitFor(() => expect(submitText).toHaveBeenCalledWith('legacy prompt'))
    expect(resumeTile).toHaveBeenCalledWith('legacy-stored-id')
    expect($notifications.get()).toHaveLength(0)
  })
})
