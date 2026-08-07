import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { textPart } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $messages } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'runtime-session-1'
let handleGatewayEvent: ((event: RpcEvent) => void) | null = null
let states: Map<string, ClientSessionState>

type UpdateSessionState = (
  sessionId: string,
  updater: (state: ClientSessionState) => ClientSessionState,
  storedSessionId?: string | null
) => ClientSessionState

let updateSessionState: ReturnType<typeof vi.fn<UpdateSessionState>>

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(states)
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState
  })

  useEffect(() => {
    handleGatewayEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

describe('live agent reaction events', () => {
  beforeEach(() => {
    handleGatewayEvent = null

    const state = createClientSessionState('stored-session-1', [
      {
        id: 'optimistic-user-message',
        role: 'user',
        parts: [textPart('hello')],
        timestamp: 1
      }
    ])

    states = new Map([[SID, state]])
    updateSessionState = vi.fn((sessionId, updater) => {
      const next = updater(states.get(sessionId) ?? createClientSessionState())
      states.set(sessionId, next)

      return next
    })
    $messages.set([])
  })

  afterEach(() => {
    cleanup()
    $messages.set([])
    vi.restoreAllMocks()
  })

  it('paints an optimistic message in the owning runtime session', () => {
    render(<Harness />)
    expect(handleGatewayEvent).not.toBeNull()

    act(() =>
      handleGatewayEvent!({
        type: 'message.reaction',
        session_id: SID,
        payload: {
          row_id: 42,
          reactions: [{ author: 'agent', emoji: '❤️' }],
          role: 'user'
        }
      })
    )

    expect(states.get(SID)?.messages).toEqual([
      {
        id: 'optimistic-user-message',
        role: 'user',
        parts: [textPart('hello')],
        timestamp: 1,
        rowId: 42,
        reactions: [{ author: 'agent', emoji: '❤️' }]
      }
    ])
    expect($messages.get()).toEqual([])
  })
})
