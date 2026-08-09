import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $turnSummaries } from '@/store/turn-summaries'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'summary-session'

// Module-scoped so a test can seed session state before the handler reads it.
const sessionStates = new Map<string, ClientSessionState>()
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(sessionStates)
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const next = updater(sessionStates.get(sessionId) ?? createClientSessionState())
      sessionStates.set(sessionId, next)

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

function emit(type: RpcEvent['type'], payload: RpcEvent['payload'] = {}, sessionId = SID) {
  act(() => handleEvent!({ payload, session_id: sessionId, type }))
}

describe('per-turn tool summary wiring', () => {
  beforeEach(() => {
    handleEvent = null
    sessionStates.clear()
    $turnSummaries.set({})
  })

  afterEach(() => {
    cleanup()
    sessionStates.clear()
    $turnSummaries.set({})
    vi.restoreAllMocks()
  })

  it('publishes a summary line for a tool-call turn when it settles', async () => {
    await mountStream()

    emit('message.start')
    emit('message.delta', { text: 'working…' })
    emit('tool.start', { name: 'terminal', tool_id: 'tool-1' })
    emit('tool.complete', { name: 'terminal', tool_id: 'tool-1', error: null })
    emit('tool.start', { name: 'read_file', tool_id: 'tool-2' })
    emit('tool.complete', { name: 'read_file', tool_id: 'tool-2', error: null })
    emit('message.complete', { text: 'done' })

    const entries = Object.entries($turnSummaries.get())
    expect(entries.length).toBe(1)
    const [messageId, summary] = entries[0]
    expect(messageId).toMatch(/^assistant-/)
    expect(summary).toMatch(/^⋯ \d+(\.\d+)?s · read 1 file · ran 1 command$/)
  })

  it('publishes nothing for a fast plain-chat reply', async () => {
    await mountStream()

    emit('message.start')
    emit('message.delta', { text: 'hi' })
    emit('message.complete', { text: 'hello!' })

    // No tool calls → empty tally; the turn settles well under the 2s
    // toolless threshold, so the formatter returns '' and nothing is stored.
    expect($turnSummaries.get()).toEqual({})
  })

  it('excludes failed tool calls from the tally', async () => {
    await mountStream()

    emit('message.start')
    emit('message.delta', { text: 'working…' })
    emit('tool.start', { name: 'write_file', tool_id: 'tool-1' })
    emit('tool.complete', { name: 'write_file', tool_id: 'tool-1', error: 'denied by approval' })
    emit('message.complete', { text: 'done' })

    const summary = Object.values($turnSummaries.get())[0] ?? ''
    // Never over-claim an edit: the denied write must not count.
    expect(summary).not.toContain('edited')
  })

  it('tallies tools across an interim-sealed multi-segment turn', async () => {
    await mountStream()

    // The normal Desktop tool-call turn: message.interim seals the tool
    // bubble (streamId → null) and the final answer lands as a DISTINCT
    // text-only bubble. The tally must come from the per-session collector
    // fed by tool.complete, not from the final bubble's parts (which carry
    // no tool-call entries).
    emit('message.start')
    emit('message.delta', { text: 'running…' })
    emit('tool.start', { name: 'terminal', tool_id: 'tool-1' })
    emit('tool.complete', { name: 'terminal', tool_id: 'tool-1', error: null })
    emit('message.interim', { text: 'command ran…' })
    emit('message.delta', { text: 'final answer' })
    emit('message.complete', { text: 'final answer' })

    const entries = Object.entries($turnSummaries.get())
    expect(entries.length).toBe(1)
    const [messageId, summary] = entries[0]
    expect(messageId).toMatch(/^assistant-/)
    expect(summary).toMatch(/^⋯ \d+(\.\d+)?s · ran 1 command$/)
  })
})
