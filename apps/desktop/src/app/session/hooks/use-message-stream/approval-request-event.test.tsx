import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $approvalRequest, clearAllPrompts } from '@/store/prompts'
import { $activeSessionId } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const ACTIVE_SID = 'session-active'
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(ACTIVE_SID)
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

describe('approval request events', () => {
  beforeEach(async () => {
    handleEvent = null
    $activeSessionId.set(ACTIVE_SID)
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())
  })

  afterEach(() => {
    cleanup()
    clearAllPrompts()
    $activeSessionId.set(null)
    vi.restoreAllMocks()
  })

  it('parks the opaque approval id with the prompt', () => {
    act(() =>
      handleEvent!({
        payload: {
          approval_id: 'approval-a',
          command: 'rm /tmp/a',
          description: 'delete a file'
        },
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    expect($approvalRequest.get()).toMatchObject({ approvalId: 'approval-a', command: 'rm /tmp/a' })
  })

  it('does not surface an approval event without an id', () => {
    act(() =>
      handleEvent!({
        payload: { command: 'rm /tmp/a', description: 'delete a file' },
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    expect($approvalRequest.get()).toBeNull()
  })
})
