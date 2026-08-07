import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { assistantTextPart, chatMessageText, textPart } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'
let handleEvent: ((event: RpcEvent) => void) | null = null
let latestState: ClientSessionState

function postHydrationState(): ClientSessionState {
  return {
    ...createClientSessionState(),
    awaitingResponse: true,
    busy: true,
    messages: [
      { id: 'user-live', role: 'user', parts: [textPart('follow-up')] },
      {
        id: 'assistant-hydrated-pending',
        role: 'assistant',
        parts: [assistantTextPart('partial')],
        pending: true
      }
    ],
    // A lagging hydrate replaced the messages array but left the live stream id.
    streamId: 'assistant-stream-orphaned'
  }
}

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>([[SID, postHydrationState()]]))
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
      latestState = next

      return next
    }
  })

  useEffect(() => {
    latestState = sessionStateByRuntimeIdRef.current.get(SID)!
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent, sessionStateByRuntimeIdRef])

  return null
}

describe('useMessageStream hydration race recovery', () => {
  beforeEach(() => {
    handleEvent = null
    latestState = postHydrationState()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('reuses the pending assistant when hydration orphaned streamId', async () => {
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())

    act(() => {
      handleEvent!({ payload: { text: ' continued' }, session_id: SID, type: 'message.delta' })
    })

    await waitFor(() => {
      const assistants = latestState.messages.filter(message => message.role === 'assistant' && !message.hidden)
      expect(assistants).toHaveLength(1)
      expect(chatMessageText(assistants[0])).toBe('partial continued')
      expect(assistants[0].id).toBe('assistant-stream-orphaned')
    })
  })
})
