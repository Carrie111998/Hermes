import { describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'

import { handleStatusEvent } from './status'
import type { GatewayEventContext } from './types'

function btwCompleteEvent({
  sessionId,
  question,
  text
}: {
  sessionId: string
  question: string
  text: string
}): { ctx: GatewayEventContext; state: { current: ClientSessionState } } {
  const state = { current: createClientSessionState('stored-1') }

  const ctx = {
    deps: {
      activeGatewayProfile: 'default',
      activeSessionIdRef: { current: sessionId },
      compactedTurnRef: { current: new Set<string>() },
      failAssistantMessage: vi.fn(),
      flushQueuedDeltas: vi.fn(),
      hydrateFromStoredSession: vi.fn(),
      lastCwdInfoSessionRef: { current: null },
      queryClient: { invalidateQueries: vi.fn() },
      refreshHermesConfig: vi.fn(),
      scheduleSessionsRefresh: vi.fn(),
      sessionInterrupted: () => false,
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn((_id: string, updater: (s: ClientSessionState) => ClientSessionState) => {
        state.current = updater(state.current)

        return state.current
      }),
      upsertToolCall: vi.fn()
    },
    event: { profile: 'default', session_id: sessionId, type: 'btw.complete' },
    explicitSid: sessionId,
    fromActiveSource: () => true,
    isActiveEvent: true,
    occurredAt: 1_700_000_000,
    payload: { question, text },
    scheduleConfigRefresh: vi.fn(),
    sessionId
  } as unknown as GatewayEventContext

  return { ctx, state }
}

describe('handleStatusEvent btw.complete', () => {
  it('appends the side-question answer onto the originating session', () => {
    const { ctx, state } = btwCompleteEvent({
      question: 'hello',
      sessionId: 'runtime-1',
      text: 'It was in foo.py'
    })

    expect(handleStatusEvent(ctx)).toBe(true)
    expect(ctx.deps.flushQueuedDeltas).toHaveBeenCalledWith('runtime-1')

    const last = state.current.messages.at(-1)
    const body = last?.parts?.map(part => ('text' in part ? part.text : '')).join('') ?? ''

    expect(last?.role).toBe('system')
    expect(body).toContain('slash:/btw')
    expect(body).toContain('/btw: "hello"')
    expect(body).toContain('It was in foo.py')
  })

  it('surfaces a worker failure instead of looking like /btw hung', () => {
    const { ctx, state } = btwCompleteEvent({
      question: 'hello',
      sessionId: 'runtime-1',
      text: 'error: no provider credentials'
    })

    expect(handleStatusEvent(ctx)).toBe(true)

    const body =
      state.current.messages
        .at(-1)
        ?.parts?.map(part => ('text' in part ? part.text : ''))
        .join('') ?? ''

    expect(body).toContain('/btw failed: "hello"')
    expect(body).toContain('no provider credentials')
  })
})
