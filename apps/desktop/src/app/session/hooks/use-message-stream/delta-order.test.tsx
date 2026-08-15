import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'

let handleEvent: ((event: RpcEvent) => void) | null = null
let sessionStates: Map<string, ClientSessionState>

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
      sessionStates.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

function mountStream() {
  sessionStates = new Map()
  render(<Harness />)
  expect(handleEvent).not.toBeNull()
}

const start = () => act(() => handleEvent!({ payload: {}, session_id: SID, type: 'message.start' }))

const reasoningDelta = (text: string) =>
  act(() => handleEvent!({ payload: { text }, session_id: SID, type: 'reasoning.delta' }))

const delta = (text: string) => act(() => handleEvent!({ payload: { text }, session_id: SID, type: 'message.delta' }))

const complete = (text: string) =>
  act(() => handleEvent!({ payload: { text }, session_id: SID, type: 'message.complete' }))

const MAX_FLUSH_GAP_MS = 250

// Deltas are coalesced into one flush per window, so nothing lands in the
// store until the flush timer runs.
const flush = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(MAX_FLUSH_GAP_MS)
  })
}

function partKinds(): string[] {
  const message = [...(sessionStates.get(SID)?.messages ?? [])].reverse().find(m => m.role === 'assistant')

  return (message?.parts ?? []).map(part => part.type)
}

describe('useMessageStream reasoning/text ordering within one flush window', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    handleEvent = null
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('renders reasoning above the answer when both arrive in the same flush window', async () => {
    mountStream()
    start()

    // The model stops thinking and starts answering inside a single flush
    // window — both deltas are queued together and land in one flush.
    reasoningDelta('weighing the options')
    delta('The answer is 42.')
    await flush()

    expect(partKinds()).toEqual(['reasoning', 'text'])
  })

  it('keeps the streamed order stable across message.complete', async () => {
    mountStream()
    start()

    reasoningDelta('weighing the options')
    delta('The answer is 42.')
    await flush()

    const streamed = partKinds()

    complete('The answer is 42.')

    // message.complete rebuilds the bubble with the final text appended after
    // the non-text parts. If the live order disagreed, settling the turn would
    // visibly reshuffle reasoning and answer.
    expect(partKinds()).toEqual(streamed)
    expect(partKinds()).toEqual(['reasoning', 'text'])
  })

  it('appends into the existing blocks once both parts exist', async () => {
    mountStream()
    start()

    reasoningDelta('weighing')
    await flush()
    delta('The answer')
    await flush()

    // A later window carrying both kinds must extend the existing blocks
    // rather than opening a second reasoning or text part.
    reasoningDelta(' some more')
    delta(' is 42.')
    await flush()

    expect(partKinds()).toEqual(['reasoning', 'text'])
  })
})
