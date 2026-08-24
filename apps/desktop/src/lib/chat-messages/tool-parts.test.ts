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
      requestId: 'request-batch',
      toolCallId: 'call-batch'
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
      requestId: 'request-newer',
      toolCallId: 'call-newer'
    })

    expect(bound[0].parts[0].type === 'tool-call' && bound[0].parts[0].args).not.toMatchObject({
      request_id: 'request-newer'
    })
    expect(bound[1].parts[0].type === 'tool-call' && bound[1].parts[0].args).toMatchObject({
      request_id: 'request-newer'
    })
  })

  it('never falls back to an older card when the exact tool identity is absent', () => {
    const older = clarifyMessage('older')
    const newer = clarifyMessage('newer', 'request-older')
    const messages = [older, newer]

    const bound = bindPendingClarifyIdentity(messages, {
      choices: ['staging', 'production'],
      question: 'Which deployment target?',
      requestId: 'request-newer',
      toolCallId: 'call-missing'
    })

    expect(bound).toBe(messages)
    expect(bound[0].parts[0].type === 'tool-call' && bound[0].parts[0].args).not.toMatchObject({
      request_id: 'request-newer'
    })
    expect(bound[1].parts[0].type === 'tool-call' && bound[1].parts[0].args).toMatchObject({
      request_id: 'request-older'
    })
  })

  it('binds cold resume by the exact tool call id instead of identical question text', () => {
    const messages = [clarifyMessage('old'), clarifyMessage('current')]

    const bound = bindPendingClarifyIdentity(messages, {
      choices: ['staging', 'production'],
      question: 'Which deployment target?',
      requestId: 'request-current',
      toolCallId: 'call-current'
    })

    expect(bound[0]?.parts[0]).not.toMatchObject({ args: { request_id: 'request-current' } })
    expect(bound[1]?.parts[0]).toMatchObject({ args: { request_id: 'request-current' } })
  })

  it('does not revive an identical stale card when the exact resumed tool call is absent', () => {
    const messages = [clarifyMessage('stale')]

    const bound = bindPendingClarifyIdentity(messages, {
      choices: ['staging', 'production'],
      question: 'Which deployment target?',
      requestId: 'request-current',
      toolCallId: 'call-current'
    })

    expect(bound).toBe(messages)
  })
})