import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { TodoItem } from '@/lib/todos'
import { $todosBySession, clearSessionTodos, setSessionTodos } from '@/store/todos'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'
const todo = (id: string, status: TodoItem['status']): TodoItem => ({ content: `task ${id}`, id, status })

let handleEvent: ((event: RpcEvent) => void) | null = null
let appendUserMessage: ((text: string) => void) | null = null
const hydrateFromStoredSession = vi.fn(async () => undefined)

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession,
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState('stored-1')
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  appendUserMessage = text => {
    const current = sessionStateByRuntimeIdRef.current.get(SID) ?? createClientSessionState('stored-1')
    sessionStateByRuntimeIdRef.current.set(SID, {
      ...current,
      messages: [
        ...current.messages,
        { id: `user-${current.messages.length}`, parts: [{ text, type: 'text' }], role: 'user' }
      ]
    })
  }

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

const complete = () => act(() => handleEvent!({ payload: { text: 'done' }, session_id: SID, type: 'message.complete' }))

describe('useMessageStream turn-end todo cleanup', () => {
  beforeEach(() => {
    handleEvent = null
    appendUserMessage = null
    hydrateFromStoredSession.mockClear()
    clearSessionTodos(SID)
  })

  afterEach(() => {
    cleanup()
    clearSessionTodos(SID)
    vi.restoreAllMocks()
  })

  it('drops a still-active task list when the turn completes', async () => {
    await mountStream()
    setSessionTodos(SID, [todo('a', 'completed'), todo('b', 'in_progress')])

    complete()

    expect($todosBySession.get()[SID]).toBeUndefined()
  })

  it('keeps a finished list on completion so its linger shows the final checkmarks', async () => {
    await mountStream()
    setSessionTodos(SID, [todo('a', 'completed')])

    complete()

    // Not cleared immediately — the finished-list linger still owns it.
    expect($todosBySession.get()[SID]).toHaveLength(1)
  })

  it('rehydrates after a completed todo result so persisted provenance replaces live state', async () => {
    await mountStream()

    act(() =>
      handleEvent!({
        payload: { name: 'todo', result: { todos: [todo('a', 'completed')] }, tool_id: 'todo-1' },
        session_id: SID,
        type: 'tool.complete'
      })
    )
    complete()

    await waitFor(() => expect(hydrateFromStoredSession).toHaveBeenCalledWith(3, 'stored-1', SID))
  })

  it('rehydrates when a completed todo precedes later bubbles in the same turn', async () => {
    await mountStream()

    act(() => appendUserMessage!('Run the multi-step turn'))
    act(() => handleEvent!({ payload: {}, session_id: SID, type: 'message.start' }))
    act(() =>
      handleEvent!({
        payload: { name: 'todo', result: { todos: [todo('early', 'completed')] }, tool_id: 'todo-early' },
        session_id: SID,
        type: 'tool.complete'
      })
    )
    act(() =>
      handleEvent!({ payload: { text: 'Todo finished; checking one more thing.' }, session_id: SID, type: 'message.interim' })
    )
    act(() =>
      handleEvent!({
        payload: { name: 'terminal', result: 'ok', tool_id: 'terminal-later' },
        session_id: SID,
        type: 'tool.complete'
      })
    )
    complete()

    await waitFor(() => expect(hydrateFromStoredSession).toHaveBeenCalledWith(3, 'stored-1', SID))
  })

  it('rehydrates a successful todo turn when an older visible assistant error exists', async () => {
    await mountStream()

    act(() =>
      handleEvent!({ payload: { message: 'Earlier failure' }, session_id: SID, type: 'error' })
    )
    act(() => appendUserMessage!('Try again'))
    act(() => handleEvent!({ payload: {}, session_id: SID, type: 'message.start' }))
    act(() =>
      handleEvent!({
        payload: { name: 'todo', result: { todos: [todo('later', 'completed')] }, tool_id: 'todo-later' },
        session_id: SID,
        type: 'tool.complete'
      })
    )
    complete()

    await waitFor(() => expect(hydrateFromStoredSession).toHaveBeenCalledWith(3, 'stored-1', SID))
  })

  it('requests provenance-only hydration after a compacted todo turn', async () => {
    await mountStream()

    act(() => appendUserMessage!('Compact this turn'))
    act(() => handleEvent!({ payload: {}, session_id: SID, type: 'message.start' }))
    act(() => handleEvent!({ payload: { kind: 'compacting' }, session_id: SID, type: 'status.update' }))
    act(() =>
      handleEvent!({
        payload: { name: 'todo', result: { todos: [todo('compacted', 'completed')] }, tool_id: 'todo-compacted' },
        session_id: SID,
        type: 'tool.complete'
      })
    )
    complete()

    await waitFor(() =>
      expect(hydrateFromStoredSession).toHaveBeenCalledWith(3, 'stored-1', SID, { preserveLocalScrollback: true })
    )
  })

  it('drops a still-active task list when the turn errors out', async () => {
    await mountStream()
    setSessionTodos(SID, [todo('a', 'in_progress')])

    act(() => handleEvent!({ payload: { message: 'boom' }, session_id: SID, type: 'error' }))

    expect($todosBySession.get()[SID]).toBeUndefined()
  })
})
