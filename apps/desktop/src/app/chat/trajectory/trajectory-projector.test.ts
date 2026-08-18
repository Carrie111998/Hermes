import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { projectTrajectory } from './trajectory-projector'

const messages: ChatMessage[] = [
  {
    id: 'user-1',
    role: 'user',
    timestamp: 10,
    completedAt: 11,
    parts: [{ type: 'text', text: 'Inspect the repository', timestamp: 10, completedAt: 11 }]
  },
  {
    id: 'assistant-1',
    role: 'assistant',
    timestamp: 12,
    completedAt: 15,
    durationS: 3,
    parts: [
      { type: 'reasoning', text: 'I should read the file.', timestamp: 12, completedAt: 12.5 },
      {
        type: 'tool-call',
        toolCallId: 'call-1',
        toolName: 'read_file',
        args: { path: 'README.md' },
        argsText: '{"path":"README.md"}',
        result: { preview: 'contents' },
        timestamp: 12.5,
        completedAt: 13,
        isError: false
      },
      { type: 'text', text: 'Done.', timestamp: 14, completedAt: 15 }
    ]
  }
]

describe('projectTrajectory', () => {
  it('projects live messages into turn, assistant, and tool records', () => {
    const projection = projectTrajectory(messages, { model: 'configured-model', provider: 'configured-provider' })

    expect(projection.summary).toMatchObject({ turns: 1, steps: 1, toolCalls: 1, errors: 0 })
    expect(projection.records.map(record => record.kind)).toEqual(['user', 'reasoning', 'tool', 'assistant'])
    expect(projection.records[2]).toMatchObject({
      callId: 'call-1',
      name: 'read_file',
      durationMs: 500,
      status: 'complete'
    })
  })

  it('keeps running tools visible before their result arrives', () => {
    const pending: ChatMessage[] = [
      {
        id: 'assistant-live',
        role: 'assistant',
        pending: true,
        parts: [
          {
            type: 'tool-call',
            toolCallId: 'live-1',
            toolName: 'terminal',
            args: { command: 'npm test' },
            argsText: '{"command":"npm test"}',
            timestamp: 20
          }
        ]
      }
    ]

    const projection = projectTrajectory(pending, { model: 'configured-model', provider: 'configured-provider' })

    expect(projection.records[0]).toMatchObject({ kind: 'tool', status: 'running', durationMs: null })
  })
})
