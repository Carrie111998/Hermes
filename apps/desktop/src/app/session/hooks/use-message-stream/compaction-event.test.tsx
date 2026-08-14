import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $compactingSessions, setSessionCompacting } from '@/store/compaction'
import { $notifications, clearNotifications } from '@/store/notifications'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'
const OTHER_SID = 'session-2'
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

function emit(type: RpcEvent['type'], payload: RpcEvent['payload'] = {}) {
  act(() => handleEvent!({ payload, session_id: SID, type }))
}

describe('useMessageStream compaction lifecycle', () => {
  beforeEach(() => {
    handleEvent = null
    $compactingSessions.set({})
    clearNotifications()
  })

  afterEach(() => {
    cleanup()
    $compactingSessions.set({})
    clearNotifications()
    vi.restoreAllMocks()
  })

  it.each([
    ['message.delta', { text: 'resumed' }],
    ['thinking.delta', { text: 'still working' }],
    ['reasoning.delta', { text: 'thinking again' }],
    ['tool.start', { name: 'terminal', tool_id: 'tool-1' }]
  ] as const)('clears the stale compaction phase when %s resumes the turn', async (type, payload) => {
    await mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compacting' })
    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })

    emit(type, payload)

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
  })

  it('clears the compaction phase on the structured completion edge', async () => {
    await mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compacting' })
    emit('status.update', { kind: 'compacted' })

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
    expect($notifications.get()).toEqual([
      expect.objectContaining({
        kind: 'success',
        message: 'Earlier history was summarized successfully. This session is ready to continue.',
        title: 'Context compressed'
      })
    ])
  })

  it('keeps manual compression locked until its authoritative session.info edge', async () => {
    await mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compressing' })
    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })

    // Unlike automatic mid-turn compaction, arbitrary model output is not a
    // manual-compression completion edge.
    emit('message.delta', { text: 'unrelated stream output' })
    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })

    emit('session.info', { running: false })

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
    expect($notifications.get()).toEqual([])
  })

  it('releases a manual compression lock on terminal ready without claiming success', async () => {
    await mountStream()

    emit('status.update', { kind: 'compressing' })
    emit('status.update', { kind: 'ready' })

    expect($compactingSessions.get()).toEqual({})
    expect($notifications.get()).toEqual([])
  })

  it('does not treat ready as completion for automatic mid-turn compaction', async () => {
    await mountStream()

    emit('status.update', { kind: 'compacting' })
    emit('status.update', { kind: 'ready' })

    expect($compactingSessions.get()).toEqual({ [SID]: true })
  })

  it('unlocks with an explicit failure notice when compaction errors', async () => {
    await mountStream()

    emit('status.update', { kind: 'compacting' })
    emit('error', { message: 'Compression provider failed' })

    expect($compactingSessions.get()).toEqual({})
    expect($notifications.get()).toEqual([
      expect.objectContaining({
        kind: 'error',
        message: 'Compression provider failed',
        title: 'Context compression failed'
      })
    ])
  })
})
