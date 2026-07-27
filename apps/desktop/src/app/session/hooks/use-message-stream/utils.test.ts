import { describe, expect, it } from 'vitest'

import type { ChatMessage, GatewayEventPayload } from '@/lib/chat-messages'

import {
  completionErrorText,
  delegateTaskPayloads,
  hasSessionInfoStatePatch,
  sessionInfoStatePatch,
  settleOrphanedToolMessages,
  settleOrphanedToolParts,
  toTodoPayload
} from './utils'

const payload = (over: Record<string, unknown>): GatewayEventPayload => over as GatewayEventPayload

describe('settleOrphanedToolParts', () => {
  it('settles only tool calls that never received a completion event', () => {
    const pending = { type: 'tool-call', toolCallId: 'pending', toolName: 'patch', args: {} } as const
    const completed = {
      type: 'tool-call',
      toolCallId: 'completed',
      toolName: 'patch',
      args: {},
      result: { success: true }
    } as const

    const [settledPending, settledCompleted] = settleOrphanedToolParts([pending, completed])

    expect(settledPending).toMatchObject({ isError: true, result: { error: expect.stringContaining('completion') } })
    expect(settledCompleted).toBe(completed)
  })

  it('preserves the parts array when every tool already has a terminal result', () => {
    const parts = [
      { type: 'tool-call', toolCallId: 'null-result', toolName: 'patch', args: {}, result: null }
    ] as ChatMessage['parts']

    expect(settleOrphanedToolParts(parts)).toBe(parts)
  })
})

describe('settleOrphanedToolMessages', () => {
  it('settles a tool in a sealed interim bubble even when the final answer uses another bubble', () => {
    const interim = {
      id: 'interim',
      role: 'assistant',
      interim: true,
      parts: [{ type: 'tool-call', toolCallId: 'pending', toolName: 'patch', args: {} }]
    } as ChatMessage
    const final = { id: 'final', role: 'assistant', parts: [{ type: 'text', text: 'Done' }] } as ChatMessage

    const settled = settleOrphanedToolMessages([interim, final])

    expect(settled[0]).not.toBe(interim)
    expect(settled[0].parts[0]).toMatchObject({ isError: true })
    expect(settled[1]).toBe(final)
  })

  it('preserves transcript identity when there are no orphaned tools', () => {
    const messages = [{ id: 'final', role: 'assistant', parts: [{ type: 'text', text: 'Done' }] }] as ChatMessage[]

    expect(settleOrphanedToolMessages(messages)).toBe(messages)
  })
})

describe('completionErrorText', () => {
  it('flags provider/HTTP/retry failures, ignores normal text', () => {
    expect(completionErrorText('API call failed after 3 retries: boom')).toMatch(/^API call failed/)
    expect(completionErrorText('HTTP 500 upstream')).toMatch(/^HTTP 500/)
    expect(completionErrorText('Gateway error: nope')).toMatch(/^Gateway error/)
    expect(completionErrorText('here is your answer')).toBeNull()
    expect(completionErrorText('   ')).toBeNull()
  })
})

describe('toTodoPayload', () => {
  it('routes named todo and anonymous todos-bearing events to the todo stream', () => {
    expect(toTodoPayload(payload({ name: 'todo' }))?.tool_id).toBe('todo-live')
    expect(toTodoPayload(payload({ todos: [] }))?.name).toBe('todo')
    expect(toTodoPayload(payload({ name: 'web_search' }))).toBeUndefined()
    expect(toTodoPayload(undefined)).toBeUndefined()
  })
})

describe('sessionInfoStatePatch / hasSessionInfoStatePatch', () => {
  it('extracts only present runtime fields', () => {
    const patch = sessionInfoStatePatch(payload({ model: 'gpt', fast: true, branch: 'main' }))
    expect(patch).toMatchObject({ model: 'gpt', fast: true, branch: 'main' })
    expect(hasSessionInfoStatePatch(patch)).toBe(true)
    expect(hasSessionInfoStatePatch(sessionInfoStatePatch(payload({})))).toBe(false)
  })
})

describe('delegateTaskPayloads', () => {
  it('returns [] for non-delegate events', () => {
    expect(delegateTaskPayloads(payload({ name: 'web_search' }), 'running')).toEqual([])
  })

  it('maps a running tool.start to a subagent.start spec', () => {
    const [spec] = delegateTaskPayloads(
      payload({ name: 'delegate_task', tool_id: 't1', args: { goal: 'do it' } }),
      'running',
      'tool.start'
    )

    expect(spec).toMatchObject({ event_type: 'subagent.start', goal: 'do it', status: 'running' })
  })

  it('maps completion (with error) to a failed subagent.complete', () => {
    const [spec] = delegateTaskPayloads(
      payload({ name: 'delegate_task', error: 'boom', result: { summary: 'failed run' } }),
      'complete'
    )

    expect(spec).toMatchObject({ event_type: 'subagent.complete', status: 'failed' })
  })
})
