import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
  $sessions.set([])
  $sessionTiles.set([])

  if (initialDesktop) {
    desktopWindow.hermesDesktop = initialDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('Quick Entry duplicate-owner target contract', () => {
  it('pushes distinct exact recent targets and resumes the selected owner generation', async () => {
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
    expect(submitToSession).toHaveBeenCalledWith('runtime-owner-b', 'owner B only')
  })
})
