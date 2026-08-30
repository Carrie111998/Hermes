import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $clarifyRequests, clearClarifyRequest, hasClarifyRequest, setClarifyRequest } from '@/store/clarify'
import { onScrollToBottomRequest } from '@/store/thread-scroll'

import type { ClientSessionState } from '../../../types'
import { useSessionActions } from '../use-session-actions'
import { ensurePendingClarifyToolRow } from '../use-session-actions/utils'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  getSession: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

// A `clarify.request` must leave an answerable inline row even when the
// `tool.start` that normally mounts it was missed (stream reconnect /
// hydration race). Without it the sidebar says "needs input" but the
// transcript has nowhere to render the choices, so the agent blocks forever.

const SID = 'session-1'

let stream: MessageStreamHarness
let stopScrollListener: (() => void) | null = null

const scrollToBottom = vi.fn()

function mountStream() {
  stream = renderMessageStream(SID)
}

const clarifyRequest = (payload: Record<string, unknown>) =>
  act(() => stream.handleEvent({ payload, session_id: SID, type: 'clarify.request' }))

const toolStart = (payload: Record<string, unknown>) =>
  act(() => stream.handleEvent({ payload, session_id: SID, type: 'tool.start' }))

function clarifyParts() {
  const messages = stream.state().messages ?? []

  return messages.flatMap(m => m.parts).filter(p => p.type === 'tool-call' && p.toolName === 'clarify')
}

describe('clarify.request stream hydration', () => {
  beforeEach(() => {
    clearClarifyRequest()
    scrollToBottom.mockClear()
    stopScrollListener = onScrollToBottomRequest(scrollToBottom)
  })

  afterEach(() => {
    cleanup()
    clearClarifyRequest()
    stopScrollListener?.()
    stopScrollListener = null
    vi.restoreAllMocks()
  })

  it('mounts an answerable clarify row when the tool.start row was missed', () => {
    mountStream()

    clarifyRequest({ choices: ['yes', 'no'], question: 'Ship it?', request_id: 'req-1' })

    const parts = clarifyParts()
    expect(parts).toHaveLength(1)
    expect(parts[0].type === 'tool-call' && parts[0].toolCallId).toBe('req-1')
    expect(parts[0].type === 'tool-call' && parts[0].args).toMatchObject({
      choices: ['yes', 'no'],
      question: 'Ship it?'
    })
  })

  it('reveals a clarify prompt raised by the active session', () => {
    mountStream()

    clarifyRequest({ choices: ['yes', 'no'], question: 'Ship it?', request_id: 'req-reveal' })

    expect(scrollToBottom).toHaveBeenCalledOnce()
  })

  it('does not move the active thread for a background session clarify', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { choices: ['yes', 'no'], question: 'Ship it?', request_id: 'req-background' },
        session_id: 'session-background',
        type: 'clarify.request'
      })
    )

    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('preserves multi-select through the store and hydrated tool row', () => {
    mountStream()

    clarifyRequest({
      choices: ['read', 'write'],
      multi_select: true,
      question: 'Which permissions?',
      request_id: 'req-multi'
    })

    expect($clarifyRequests.get()[SID]?.multiSelect).toBe(true)

    const part = clarifyParts()[0]
    expect(part?.type).toBe('tool-call')

    if (part?.type !== 'tool-call') {
      throw new Error('Expected a hydrated clarify tool call')
    }

    expect(part.args).toMatchObject({
      choices: ['read', 'write'],
      multi_select: true,
      question: 'Which permissions?'
    })
  })

  it('merges with the real tool.start row even though its id differs from the request id', () => {
    mountStream()

    // Reality: tool.start carries the model's tool_call_id, clarify.request a
    // separately-generated request_id. They must still collapse to ONE card
    // (correlated by question), not two.
    toolStart({ args: { choices: ['a'], question: 'Pick' }, name: 'clarify', tool_id: 'call-abc' })
    clarifyRequest({ choices: ['a'], question: 'Pick', request_id: 'req-2' })

    expect(clarifyParts()).toHaveLength(1)
  })

  it('does not duplicate when clarify.request arrives before the tool.start row', () => {
    mountStream()

    clarifyRequest({ choices: ['a'], question: 'Pick', request_id: 'req-3' })
    toolStart({ args: { choices: ['a'], question: 'Pick' }, name: 'clarify', tool_id: 'call-xyz' })

    expect(clarifyParts()).toHaveLength(1)
  })

  it('merges a BATCH tool.start row with its clarify.request (no top-level question)', () => {
    mountStream()

    // The batch shape: tool args carry `questions`, no top-level `question`.
    // The correlation key must come from the question list, or the two ids
    // mount two cards (the duplicate seen in the field).
    toolStart({
      args: { questions: [{ question: 'Drink?' }, { question: 'Productive when?' }] },
      name: 'clarify',
      tool_id: 'call-batch'
    })
    clarifyRequest({
      questions: [
        { qid: 'q0', question: 'Drink?' },
        { qid: 'q1', question: 'Productive when?' }
      ],
      request_id: 'req-batch'
    })

    expect(clarifyParts()).toHaveLength(1)
    expect($clarifyRequests.get()[SID]?.questions).toHaveLength(2)
  })

  it('does not duplicate when the batch clarify.request arrives before tool.start', () => {
    mountStream()

    clarifyRequest({
      questions: [
        { qid: 'q0', question: 'Drink?' },
        { qid: 'q1', question: 'Productive when?' }
      ],
      request_id: 'req-batch-2'
    })
    toolStart({
      args: { questions: [{ question: 'Drink?' }, { question: 'Productive when?' }] },
      name: 'clarify',
      tool_id: 'call-batch-2'
    })

    expect(clarifyParts()).toHaveLength(1)
    expect($clarifyRequests.get()[SID]?.questions).toHaveLength(2)
  })
})

// ─── Durable long-session clarify projection ─────────────────────────────────
//
// Production-shaped reproduction of the field failure: a long compaction-era
// session parks a blocking clarify, an UNRELATED tool finishes while the
// backend is still blocked on `clarify.respond`, and the sidebar's
// "Needs your input" attention must survive it. Clarify attention is derived
// from the one unresolved request, so only a matching clarify settlement may
// clear it — otherwise the session goes quiet while Python still waits.

const LONG_SID = 'runtime-long-1'
const FIELD_QUESTION = 'Authorize the exact sandbox credential-read boundary?'

/** 741 persisted rows with compaction-era lineage and an interim boundary. */
function longPersistedHistory() {
  const messages = []

  for (let index = 0; index < 740; index += 1) {
    messages.push({
      id: `row-${index}`,
      parts: [{ type: 'text' as const, text: `persisted turn ${index}` }],
      role: index % 2 === 0 ? ('user' as const) : ('assistant' as const),
      rowId: index
    })
  }

  messages.push({
    id: 'inflight-assistant-segment-0-runtime-long-1',
    interim: true,
    parts: [{ type: 'text' as const, text: 'sealed interim boundary' }],
    pending: false,
    role: 'assistant' as const
  })

  return messages
}

describe('durable clarify projection across a long session', () => {
  beforeEach(() => {
    clearClarifyRequest()
  })

  afterEach(() => {
    cleanup()
    clearClarifyRequest()
  })

  it('keeps clarify attention when an unrelated tool completes while the request is unresolved', () => {
    const states = new Map()
    stream = renderMessageStream(LONG_SID, { states })

    act(() =>
      stream.handleEvent({
        payload: { messages: longPersistedHistory() },
        session_id: LONG_SID,
        type: 'session.info'
      })
    )

    act(() =>
      stream.handleEvent({
        payload: { args: { path: 'notes.md' }, name: 'read_file', tool_id: 'call-read-1' },
        session_id: LONG_SID,
        type: 'tool.start'
      })
    )

    act(() =>
      stream.handleEvent({
        payload: {
          choices: ['Allow', 'Deny'],
          question: FIELD_QUESTION,
          request_id: 'req-field-1'
        },
        session_id: LONG_SID,
        type: 'clarify.request'
      })
    )

    // Pre-condition: one canonical unresolved request, one unresolved row, and
    // clarify-derived attention on the CURRENT runtime identity.
    expect($clarifyRequests.get()[LONG_SID]?.requestId).toBe('req-field-1')
    expect(unresolvedClarifyRows()).toHaveLength(1)
    expect(states.get(LONG_SID)?.needsInput).toBe(true)

    // The unrelated read_file finishes. The clarify tool is still blocked
    // server-side, so nothing about this completion settles it.
    act(() =>
      stream.handleEvent({
        payload: { name: 'read_file', result: 'ok', tool_id: 'call-read-1' },
        session_id: LONG_SID,
        type: 'tool.complete'
      })
    )

    expect($clarifyRequests.get()[LONG_SID]?.requestId).toBe('req-field-1')
    expect(unresolvedClarifyRows()).toHaveLength(1)
    expect(states.get(LONG_SID)?.needsInput).toBe(true)
  })

  it('clears clarify attention exactly once on the matching clarify completion', () => {
    const states = new Map()
    stream = renderMessageStream(LONG_SID, { states })

    act(() =>
      stream.handleEvent({
        payload: { choices: ['Allow', 'Deny'], question: FIELD_QUESTION, request_id: 'req-field-2' },
        session_id: LONG_SID,
        type: 'clarify.request'
      })
    )

    expect(states.get(LONG_SID)?.needsInput).toBe(true)

    act(() =>
      stream.handleEvent({
        payload: {
          args: { question: FIELD_QUESTION },
          name: 'clarify',
          result: JSON.stringify({ question: FIELD_QUESTION, user_response: 'Allow' }),
          tool_id: 'req-field-2'
        },
        session_id: LONG_SID,
        type: 'tool.complete'
      })
    )

    expect(states.get(LONG_SID)?.needsInput).toBe(false)
    expect($clarifyRequests.get()[LONG_SID]).toBeUndefined()
  })
})

function unresolvedClarifyRows() {
  return (stream.state(LONG_SID).messages ?? [])
    .flatMap(m => m.parts)
    .filter(p => p.type === 'tool-call' && p.toolName === 'clarify' && p.result === undefined)
}

// ── F-003: a clarify answered by ANOTHER renderer must settle here too ───────
//
// The live split is real: `tool.start` carries the model's tool_call_id and is
// the id `tool.complete` comes back with, while `clarify.request` carries the
// gateway's separately minted request_id, which is what the store keys on. When
// this renderer answers the card itself, ClarifyTool clears the store locally
// and the id split never matters. When ANOTHER renderer/client answers, the
// completion is the ONLY settlement signal this renderer receives — and it
// arrives under the model id.

const REMOTE_SID = 'runtime-remote-1'
const REMOTE_QUESTION = 'Authorize the exact sandbox credential-read boundary?'

const remoteEvent = (type: string, payload: Record<string, unknown>) =>
  act(() => stream.handleEvent({ payload, session_id: REMOTE_SID, type }))

const startClarify = (toolId: string) =>
  remoteEvent('tool.start', {
    args: { choices: ['Allow', 'Deny'], question: REMOTE_QUESTION },
    name: 'clarify',
    tool_id: toolId
  })

const requestClarify = (requestId: string) =>
  remoteEvent('clarify.request', { choices: ['Allow', 'Deny'], question: REMOTE_QUESTION, request_id: requestId })

const completeClarify = (toolId: string) =>
  remoteEvent('tool.complete', {
    args: { question: REMOTE_QUESTION },
    name: 'clarify',
    result: JSON.stringify({ question: REMOTE_QUESTION, user_response: 'Allow' }),
    tool_id: toolId
  })

describe('clarify answered by another renderer', () => {
  beforeEach(() => {
    clearClarifyRequest()
  })

  afterEach(() => {
    cleanup()
    clearClarifyRequest()
  })

  it('settles the gateway request when the completion carries only the model tool-call id', () => {
    const states = new Map()
    stream = renderMessageStream(REMOTE_SID, { states })

    startClarify('call-remote')
    requestClarify('req-remote')

    // Pre-condition: the canonical request is keyed by the GATEWAY id, and the
    // one row still carries the MODEL id it was started with.
    expect($clarifyRequests.get()[REMOTE_SID]?.requestId).toBe('req-remote')
    expect(states.get(REMOTE_SID)?.needsInput).toBe(true)

    // No local card-success clear happens here — the other renderer answered.
    completeClarify('call-remote')

    expect($clarifyRequests.get()[REMOTE_SID]).toBeUndefined()
    expect(hasClarifyRequest(REMOTE_SID)).toBe(false)
    expect(states.get(REMOTE_SID)?.needsInput).toBe(false)

    // The activation/reconciliation path derives its row from the store, so a
    // stale entry is exactly what resurrects the card. There is none left.
    const reprojected = ensurePendingClarifyToolRow(stream.state(REMOTE_SID).messages ?? [], undefined, REMOTE_SID)

    expect(
      reprojected
        .flatMap(message => message.parts)
        .filter(part => part.type === 'tool-call' && part.toolName === 'clarify' && part.result === undefined)
    ).toHaveLength(0)
  })

  it('leaves the live request alone when an OLDER epoch’s clarify completes late', () => {
    const states = new Map()
    stream = renderMessageStream(REMOTE_SID, { states })

    startClarify('call-epoch-1')
    requestClarify('req-epoch-1')

    // The agent asks the same thing again under fresh ids (the first one timed
    // out server-side, or another client answered it).
    startClarify('call-epoch-2')
    requestClarify('req-epoch-2')

    expect($clarifyRequests.get()[REMOTE_SID]?.requestId).toBe('req-epoch-2')

    // The FIRST clarify finally returns. Identical wording is not identity.
    completeClarify('call-epoch-1')

    expect($clarifyRequests.get()[REMOTE_SID]?.requestId).toBe('req-epoch-2')
    expect(states.get(REMOTE_SID)?.needsInput).toBe(true)
  })

  it('still settles when an unrelated tool completes in between', () => {
    const states = new Map()
    stream = renderMessageStream(REMOTE_SID, { states })

    startClarify('call-mixed')
    requestClarify('req-mixed')

    remoteEvent('tool.start', { args: { path: 'notes.md' }, name: 'read_file', tool_id: 'call-read-9' })
    remoteEvent('tool.complete', { name: 'read_file', result: 'ok', tool_id: 'call-read-9' })

    expect($clarifyRequests.get()[REMOTE_SID]?.requestId).toBe('req-mixed')
    expect(states.get(REMOTE_SID)?.needsInput).toBe(true)

    completeClarify('call-mixed')

    expect($clarifyRequests.get()[REMOTE_SID]).toBeUndefined()
    expect(states.get(REMOTE_SID)?.needsInput).toBe(false)
  })
})

// ── F-001: the pending request must MOVE off a dead runtime on cold resume ────
//
// A stale warm-cache entry is purged (`takeWarmCache` cross-wired branch, the
// empty-transcript drop, and the session-gone catch all delete the
// stored→runtime mapping) BEFORE the cold `session.resume` mints the new
// runtime id. The rebind then has to translate the OLD runtime identity that
// actually raised the request — not whatever the already-purged map still
// answers with — or the authority stays stranded under a key nothing reads.

const REBIND_STORED_ID = 'stored-clarify-move'
const REBIND_OLD_RUNTIME = 'rt-clarify-old'
const REBIND_NEW_RUNTIME = 'rt-clarify-new'
const REBIND_QUESTION = 'Resume the interrupted migration?'

function ClarifyResumeHarness({
  onReady,
  requestGateway,
  runtimeIdByStoredSessionIdRef,
  sessionStateByRuntimeIdRef
}: {
  onReady: (resume: (storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>>
  sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'clarify-rebind',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway,
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef,
    syncSessionStateToView: vi.fn(),
    updateSessionState: (sessionId, updater, storedSessionId) => {
      const current =
        sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState(storedSessionId ?? null)

      const next = updater(current)

      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    onReady(actions.resumeSession)
  }, [actions.resumeSession, onReady])

  return null
}

describe('clarify authority survives a stale-runtime cold resume', () => {
  beforeEach(() => {
    clearClarifyRequest()
  })

  afterEach(() => {
    cleanup()
    clearClarifyRequest()
    vi.restoreAllMocks()
  })

  it('moves the old runtime’s pending request onto the runtime the resume minted', async () => {
    // The request was raised by the runtime that has since died, and the warm
    // cache entry for it is cross-wired to a different conversation — the
    // exact shape that purges the stored→runtime mapping before the rebind.
    setClarifyRequest({
      choices: ['Resume', 'Discard'],
      multiSelect: false,
      question: REBIND_QUESTION,
      requestId: 'req-durable-move',
      sessionId: REBIND_OLD_RUNTIME
    })

    const runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>> = {
      current: new Map([[REBIND_STORED_ID, REBIND_OLD_RUNTIME]])
    }

    const sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>> = {
      current: new Map([[REBIND_OLD_RUNTIME, createClientSessionState('stored-somebody-else')]])
    }

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        return {
          info: {},
          messages: [{ content: 'Restored context.', role: 'assistant', timestamp: 1 }],
          session_id: REBIND_NEW_RUNTIME
        } as never
      }

      return {} as never
    })

    vi.mocked(getLatestSessionMessages).mockResolvedValue({ messages: [] } as never)

    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null

    render(
      <ClarifyResumeHarness
        onReady={r => (resume = r)}
        requestGateway={requestGateway}
        runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
        sessionStateByRuntimeIdRef={sessionStateByRuntimeIdRef}
      />
    )

    await waitFor(() => expect(resume).not.toBeNull())
    await resume!(REBIND_STORED_ID, true)

    // The stale mapping really was purged before the rebind ran — otherwise
    // this test would not exercise the defect at all.
    expect(runtimeIdByStoredSessionIdRef.current.get(REBIND_STORED_ID)).not.toBe(REBIND_OLD_RUNTIME)

    // MOVED, not copied: exactly one actionable copy, under the new identity,
    // carrying the original request id and payload.
    const moved = $clarifyRequests.get()[REBIND_NEW_RUNTIME]

    expect(moved?.requestId).toBe('req-durable-move')
    expect(moved?.question).toBe(REBIND_QUESTION)
    expect(moved?.choices).toEqual(['Resume', 'Discard'])
    expect(moved?.sessionId).toBe(REBIND_NEW_RUNTIME)
    expect($clarifyRequests.get()[REBIND_OLD_RUNTIME]).toBeUndefined()

    // Attention derives from the resolved identity, and the card projects once.
    expect(hasClarifyRequest(REBIND_NEW_RUNTIME)).toBe(true)
    expect(hasClarifyRequest(REBIND_OLD_RUNTIME)).toBe(false)

    const projected = ensurePendingClarifyToolRow(
      [{ id: 'assistant-restored', parts: [{ text: 'Restored context.', type: 'text' }], role: 'assistant' }],
      undefined,
      REBIND_NEW_RUNTIME
    )

    const rows = projected
      .flatMap(message => message.parts)
      .filter(part => part.type === 'tool-call' && part.toolName === 'clarify' && part.result === undefined)

    expect(rows).toHaveLength(1)
    expect(rows[0]?.type === 'tool-call' && rows[0].toolCallId).toBe('req-durable-move')
  })
})
