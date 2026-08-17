import { describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import {
  clearRuntimeStores,
  publishRuntimeState,
  runtimeStatusStore,
  runtimeTranscriptStore
} from './runtime-session-stores'

describe('runtime session stores', () => {
  it('publishes a token delta only to the target transcript subscriber', () => {
    clearRuntimeStores()
    const aStatus = vi.fn()
    const aTranscript = vi.fn()
    const bTranscript = vi.fn()

    const unlisten = [
      runtimeStatusStore('a').listen(aStatus),
      runtimeTranscriptStore('a').listen(aTranscript),
      runtimeTranscriptStore('b').listen(bTranscript)
    ]

    aStatus.mockClear()
    aTranscript.mockClear()
    bTranscript.mockClear()

    const first = createClientSessionState()
    first.messages = [{ id: 'tail', role: 'assistant', parts: [{ type: 'text', text: 'a' }], pending: true }]
    publishRuntimeState('a', null, first)
    aStatus.mockClear()
    aTranscript.mockClear()

    const next = {
      ...first,
      messages: [{ ...first.messages[0], parts: [{ type: 'text' as const, text: 'ab' }] }]
    }

    publishRuntimeState('a', first, next)

    expect(aTranscript).toHaveBeenCalledTimes(1)
    expect(aStatus).not.toHaveBeenCalled()
    expect(bTranscript).not.toHaveBeenCalled()
    unlisten.forEach(stop => stop())
  })

  it('keeps settled message identity across a tail replacement', () => {
    clearRuntimeStores()
    const settled = { id: 'settled', role: 'user' as const, parts: [{ type: 'text' as const, text: 'prompt' }] }
    const first = createClientSessionState()
    first.messages = [settled, { id: 'tail', role: 'assistant', parts: [{ type: 'text', text: 'a' }] }]
    publishRuntimeState('a', null, first)

    const next = {
      ...first,
      messages: [settled, { ...first.messages[1], parts: [{ type: 'text' as const, text: 'ab' }] }]
    }

    publishRuntimeState('a', first, next)

    expect(runtimeTranscriptStore('a').get().messages[0]).toBe(settled)
    expect(runtimeTranscriptStore('a').get().operation).toMatchObject({ kind: 'replace-tail', message: next.messages[1] })
  })

  it('classifies append, finalize, and structural reset operations', () => {
    clearRuntimeStores()
    const user = { id: 'user', role: 'user' as const, parts: [{ type: 'text' as const, text: 'prompt' }] }

    const pending = {
      id: 'tail',
      role: 'assistant' as const,
      parts: [{ type: 'text' as const, text: 'answer' }],
      pending: true
    }

    const first = { ...createClientSessionState(), messages: [user] }
    publishRuntimeState('a', null, first)

    const appended = { ...first, messages: [user, pending] }
    publishRuntimeState('a', first, appended)
    expect(runtimeTranscriptStore('a').get().operation).toEqual({ kind: 'append', message: pending })

    const final = { ...appended, messages: [user, { ...pending, pending: false }] }
    publishRuntimeState('a', appended, final)
    expect(runtimeTranscriptStore('a').get().operation.kind).toBe('finalize-tail')

    const branch = { ...final, messages: [{ ...user, id: 'branch-user' }] }
    publishRuntimeState('a', final, branch)
    expect(runtimeTranscriptStore('a').get().operation).toEqual({ kind: 'reset', messages: branch.messages })
  })
})
