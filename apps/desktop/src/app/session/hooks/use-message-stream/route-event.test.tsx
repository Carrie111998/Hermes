import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $turnRoutes, clearTurnRoutes } from '@/store/turn-routing'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'
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

function emit(sessionId: string, type: RpcEvent['type'], payload: RpcEvent['payload'] = {}) {
  act(() => handleEvent!({ payload, session_id: sessionId, type }))
}

beforeEach(() => {
  handleEvent = null
  clearTurnRoutes()
})

afterEach(() => {
  cleanup()
  clearTurnRoutes()
  vi.restoreAllMocks()
})

describe('useMessageStream route event ingestion', () => {
  it('attributes backend route provenance to the event session', async () => {
    await mountStream()

    emit('background-session', 'route.decided', {
      confidence: 0.9,
      mode: 'observe',
      reason_code: 'architecture_complexity',
      route: 'deep',
      should_apply: false,
      source: 'rule',
      target: { kind: 'moa', preset: 'deep' },
      turn_id: 'turn-1'
    })

    expect($turnRoutes.get()['background-session']).toMatchObject({
      mode: 'observe',
      reasonCode: 'architecture_complexity',
      route: 'deep',
      shouldApply: false,
      source: 'rule',
      turnId: 'turn-1'
    })
    expect($turnRoutes.get()[SID]).toBeUndefined()
  })

  it('does not let a delayed terminal event replace a newer turn', async () => {
    await mountStream()

    emit(SID, 'route.decided', { mode: 'observe', route: 'deep', turn_id: 'turn-old' })
    emit(SID, 'route.decided', { mode: 'observe', route: 'current', turn_id: 'turn-new' })
    emit(SID, 'route.degraded', {
      mode: 'observe',
      reason_code: 'route_restore_failed',
      route: 'deep',
      turn_id: 'turn-old'
    })

    expect($turnRoutes.get()[SID]).toMatchObject({ event: 'route.decided', route: 'current', turnId: 'turn-new' })
  })

  it('drops a delayed decided event and preserves the backend selection reason', async () => {
    await mountStream()

    emit(SID, 'route.decided', {
      mode: 'observe',
      reason_code: 'architecture_complexity',
      route: 'deep',
      turn_id: 'turn-new',
      turn_sequence: 2
    })
    emit(SID, 'route.decided', {
      mode: 'observe',
      reason_code: 'plain_default',
      route: 'stale',
      turn_id: 'turn-old',
      turn_sequence: 1
    })
    emit(SID, 'route.completed', {
      mode: 'observe',
      reason_code: 'route_completed',
      route: 'deep',
      selection_reason_code: 'architecture_complexity',
      turn_id: 'turn-new',
      turn_sequence: 2
    })

    expect($turnRoutes.get()[SID]).toMatchObject({
      event: 'route.completed',
      reasonCode: 'architecture_complexity',
      route: 'deep',
      turnId: 'turn-new',
      turnSequence: 2
    })
  })
})
