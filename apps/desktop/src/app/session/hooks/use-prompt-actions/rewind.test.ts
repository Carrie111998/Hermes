import { describe, expect, it } from 'vitest'

import { type ChatMessage, textPart } from '@/lib/chat-messages'

import { planRestore, truncateSubmitParams } from './rewind'

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
