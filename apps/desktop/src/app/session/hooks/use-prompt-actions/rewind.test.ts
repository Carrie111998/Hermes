import { describe, expect, it } from 'vitest'

import { type ChatMessage, mergePersistedTodoProvenance } from '@/lib/chat-messages'
import { latestSessionPlan } from '@/lib/todos'

import { finalizeInterruptedMessages, truncateSubmitParams } from './rewind'

describe('truncateSubmitParams', () => {
  it('omits truncation fields when no ordinal is set', () => {
    expect(truncateSubmitParams(undefined)).toEqual({})
  })

  it('requires confirm_empty_truncate only for ordinal 0', () => {
    expect(truncateSubmitParams(0)).toEqual({
      truncate_before_user_ordinal: 0,
      confirm_empty_truncate: true
    })
    expect(truncateSubmitParams(1)).toEqual({
      truncate_before_user_ordinal: 1
    })
  })
})

describe('finalizeInterruptedMessages', () => {
  it('preserves a completed tool-only todo result for provenance hydration', () => {
    const messages: ChatMessage[] = [
      {
        id: 'assistant-stream',
        parts: [
          {
            result: { todos: [] },
            toolCallId: 'todo-clear',
            toolName: 'todo',
            type: 'tool-call'
          }
        ],
        pending: true,
        role: 'assistant'
      }
    ]

    expect(finalizeInterruptedMessages(messages, 'assistant-stream')).toEqual([
      expect.objectContaining({ id: 'assistant-stream', pending: false })
    ])
  })

  it('retains the exact persisted result and timestamp through cancellation hydration', () => {
    const items = [{ content: 'Persist me', id: 'persist', status: 'completed' as const }]

    const local: ChatMessage[] = [
      { id: 'user-1', parts: [{ text: 'Make a plan', type: 'text' }], role: 'user' },
      {
        id: 'assistant-stream',
        parts: [{ result: { todos: items }, toolCallId: 'todo-1', toolName: 'todo', type: 'tool-call' }],
        pending: true,
        role: 'assistant'
      }
    ]

    const persisted: ChatMessage[] = [
      {
        id: 'stored-assistant',
        parts: [
          {
            result: { todos: items },
            todoUpdatedAt: 456,
            toolCallId: 'todo-1',
            toolName: 'todo',
            type: 'tool-call'
          } as ChatMessage['parts'][number] & { todoUpdatedAt: number }
        ],
        role: 'assistant'
      }
    ]

    const finalized = finalizeInterruptedMessages(local, 'assistant-stream')
    const hydrated = mergePersistedTodoProvenance(finalized, persisted)

    expect(latestSessionPlan(hydrated, { busy: false, hasRuntime: true })).toMatchObject({
      items,
      updatedAt: 456
    })
  })

  it('retains an explicit empty persisted clear through cancellation hydration', () => {
    const local: ChatMessage[] = [
      {
        id: 'older-plan',
        parts: [
          {
            result: { todos: [{ content: 'Old', id: 'old', status: 'completed' }] },
            todoUpdatedAt: 100,
            toolCallId: 'todo-old',
            toolName: 'todo',
            type: 'tool-call'
          } as ChatMessage['parts'][number] & { todoUpdatedAt: number }
        ],
        role: 'assistant'
      },
      {
        id: 'assistant-stream',
        parts: [{ result: { todos: [] }, toolCallId: 'todo-clear', toolName: 'todo', type: 'tool-call' }],
        pending: true,
        role: 'assistant'
      }
    ]

    const persisted: ChatMessage[] = [
      {
        id: 'stored-clear',
        parts: [
          {
            result: { todos: [] },
            todoUpdatedAt: 789,
            toolCallId: 'todo-clear',
            toolName: 'todo',
            type: 'tool-call'
          } as ChatMessage['parts'][number] & { todoUpdatedAt: number }
        ],
        role: 'assistant'
      }
    ]

    const finalized = finalizeInterruptedMessages(local, 'assistant-stream')
    const hydrated = mergePersistedTodoProvenance(finalized, persisted)

    expect(latestSessionPlan(hydrated, { busy: false, hasRuntime: true })).toBeNull()
  })
})
