import type { AppendMessage } from '@assistant-ui/react'
import { describe, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { type ChatMessage, textPart } from '@/lib/chat-messages'

import { applyReloadOptimistic, planEdit, planReload, planRestore, truncateSubmitParams } from './rewind'

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

const user = (id: string, text: string, displayKind?: string): ChatMessage =>
  ({
    id,
    role: 'user',
    parts: [textPart(text)],
    displayKind: displayKind ?? null
  }) as ChatMessage

const assistant = (id: string): ChatMessage => ({
  id,
  role: 'assistant',
  parts: [textPart('reply')]
})

describe('planRestore', () => {
  const messages = [
    user('u0', 'first real turn'),
    assistant('a0'),
    user('skill', 'expanded skill invocation', 'skill_invocation'),
    user('auto', 'automatic continuation', 'auto_continue'),
    user('u1', 'last real turn')
  ]

  it('recomputes the ordinal from the message id instead of trusting a stale target ordinal', () => {
    expect(planRestore(messages, 'u0', { userOrdinal: 2 })).toMatchObject({
      sourceIndex: 0,
      truncateOrdinal: 0
    })
    expect(planRestore(messages, 'u1', { userOrdinal: 3 })).toMatchObject({
      sourceIndex: 4,
      truncateOrdinal: 1
    })
  })

  it('uses the filtered ordinal mapping when the message id is unavailable', () => {
    expect(planRestore(messages, 'missing', { userOrdinal: 1 })).toMatchObject({
      sourceIndex: 4,
      truncateOrdinal: 1
    })
  })
})

describe('reload and edit source selection', () => {
  const messages = [
    user('u0', 'first real turn'),
    assistant('a0'),
    user('skill', 'expanded skill invocation', 'skill_invocation'),
    assistant('a1')
  ]

  const state = (currentMessages: ChatMessage[]): ClientSessionState => ({
    awaitingResponse: false,
    branch: 'main',
    busy: false,
    cwd: '/workspace',
    fast: false,
    interimBoundaryPending: false,
    interrupted: false,
    messages: currentMessages,
    model: 'model',
    needsInput: false,
    pendingBranchGroup: null,
    personality: '',
    provider: 'provider',
    reasoningEffort: '',
    sawAssistantPayload: false,
    serviceTier: '',
    storedSessionId: null,
    streamId: null,
    turnStartedAt: null,
    usage: null,
    yolo: false
  })

  it('does not use a display-kind row as the reload source or optimistic boundary', () => {
    const plan = planReload(messages, 'a1')

    expect(plan).toMatchObject({
      text: 'first real turn',
      truncateOrdinal: 0,
      userIndex: 0
    })

    const next = applyReloadOptimistic(state(messages), plan!)

    expect(next.messages.map(message => message.id)).toEqual(['u0', 'a0', 'skill', 'a1'])
    expect(next.messages.filter(message => message.role === 'assistant').every(message => message.hidden)).toBe(true)
  })

  it('does not treat a display-kind row as an editable gateway turn', () => {
    const edited = {
      content: [{ text: 'edited skill invocation', type: 'text' }],
      role: 'user',
      sourceId: 'skill'
    } as unknown as AppendMessage

    expect(planEdit(messages, edited)).toBeNull()
  })
})
