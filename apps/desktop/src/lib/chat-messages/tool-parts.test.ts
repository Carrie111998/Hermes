import { describe, expect, it } from 'vitest'

import { bindPendingClarifyIdentity } from './tool-parts'
import type { ChatMessage } from './types'

function clarifyMessage(id: string, requestId?: string, args?: Record<string, unknown>): ChatMessage {
  return {
    id,
    role: 'assistant',
    parts: [
      {
        args: {
          choices: ['staging', 'production'],
          question: 'Which deployment target?',
          ...(requestId ? { request_id: requestId } : {}),
          ...args
        },
        result: undefined,
        toolCallId: `call-${id}`,
        toolName: 'clarify',
        type: 'tool-call'
      }
    ]
  }
}

describe('bindPendingClarifyIdentity', () => {
  it('binds an unresolved batch by its ordered questions', () => {
    const messages = [
      clarifyMessage('batch', undefined, {
        questions: [{ question: 'Color?' }, { question: 'Name?' }]
      })
    ]

    const bound = bindPendingClarifyIdentity(messages, {
      questions: ['Color?', 'Name?'],
      requestId: 'request-batch'
    })

    expect(bound[0]?.parts[0]).toMatchObject({
      args: { request_id: 'request-batch' }
    })
  })

  it('binds only the newest matching unresolved card', () => {
    const messages = [clarifyMessage('older'), clarifyMessage('newer')]

    const bound = bindPendingClarifyIdentity(messages, {
      choices: ['staging', 'production'],
      question: 'Which deployment target?',
      requestId: 'request-newer'
    })

    expect(bound[0].parts[0].type === 'tool-call' && bound[0].parts[0].args).not.toMatchObject({
      request_id: 'request-newer'
    })
    expect(bound[1].parts[0].type === 'tool-call' && bound[1].parts[0].args).toMatchObject({
      request_id: 'request-newer'
    })
  })

  it('never falls back to an older card when the newest match has another request identity', () => {
    const older = clarifyMessage('older')
    const newer = clarifyMessage('newer', 'request-older')
    const messages = [older, newer]

    const bound = bindPendingClarifyIdentity(messages, {
      choices: ['staging', 'production'],
      question: 'Which deployment target?',
      requestId: 'request-newer'
    })

    expect(bound).toBe(messages)
    expect(bound[0].parts[0].type === 'tool-call' && bound[0].parts[0].args).not.toMatchObject({
      request_id: 'request-newer'
    })
    expect(bound[1].parts[0].type === 'tool-call' && bound[1].parts[0].args).toMatchObject({
      request_id: 'request-older'
    })
  })
})