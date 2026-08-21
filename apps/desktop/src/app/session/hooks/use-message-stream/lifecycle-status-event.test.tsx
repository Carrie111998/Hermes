import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import type { RpcEvent } from '@/types/hermes'

import { renderMessageStream } from './test-harness'

const SID = 'session-lifecycle'

describe('lifecycle status visibility', () => {
  afterEach(cleanup)

  it('shows a truthful lifecycle phase and clears it when the turn ends', () => {
    const stream = renderMessageStream(SID)

    const emit = (type: RpcEvent['type'], payload: RpcEvent['payload'] = {}) =>
      act(() => stream.handleEvent({ payload, session_id: SID, type }))

    emit('status.update', {
      kind: 'lifecycle',
      text: 'Queued behind active Hermes — executing tool: terminal · waiting 30s.'
    })

    expect(stream.state()).toMatchObject({
      lastActivityDescription: 'Queued behind active Hermes — executing tool: terminal · waiting 30s.'
    })

    emit('message.complete', { text: '' })

    expect(stream.state()).toMatchObject({
      lastActivityAt: null,
      lastActivityDescription: ''
    })
  })

  it('clears Queued only when message.start names that exact optimistic bubble', () => {
    const initial = createClientSessionState()
    initial.messages = [
      {
        id: 'user-older-sending',
        role: 'user',
        parts: [{ type: 'text', text: 'older' }],
        deliveryState: 'sending'
      },
      {
        id: 'user-question-mark',
        role: 'user',
        parts: [{ type: 'text', text: '؟' }],
        deliveryState: 'queued'
      }
    ]
    const stream = renderMessageStream(SID, { states: new Map([[SID, initial]]) })

    act(() =>
      stream.handleEvent({
        payload: { client_message_id: 'user-question-mark' },
        session_id: SID,
        type: 'message.start'
      })
    )

    expect(stream.state().messages).toMatchObject([
      { id: 'user-older-sending', deliveryState: 'sending' },
      { id: 'user-question-mark', deliveryState: undefined }
    ])
  })

  it('clears only tool-boundary redirects on the next model-originated progress event', () => {
    const initial = createClientSessionState()
    initial.messages = [
      {
        id: 'user-tool-boundary',
        role: 'user',
        parts: [{ type: 'text', text: 'adjust this run' }],
        deliveryState: 'queued',
        deliveryClearsOnProgress: true
      },
      {
        id: 'user-next-turn',
        role: 'user',
        parts: [{ type: 'text', text: 'run this later' }],
        deliveryState: 'queued'
      }
    ]
    const stream = renderMessageStream(SID, { states: new Map([[SID, initial]]) })

    act(() =>
      stream.handleEvent({
        payload: { args: {}, name: 'terminal', tool_id: 'same-batch-tool-2' },
        session_id: SID,
        type: 'tool.start'
      })
    )

    expect(stream.state().messages.find(message => message.id === 'user-tool-boundary')).toMatchObject({
      deliveryState: 'queued',
      deliveryClearsOnProgress: true
    })

    act(() =>
      stream.handleEvent({
        payload: { text: 'Thinking' },
        session_id: SID,
        type: 'thinking.delta'
      })
    )

    expect(stream.state().messages.find(message => message.id === 'user-tool-boundary')).toMatchObject({
      deliveryState: undefined,
      deliveryClearsOnProgress: undefined
    })
    expect(stream.state().messages.find(message => message.id === 'user-next-turn')).toMatchObject({
      deliveryState: 'queued'
    })
  })
})
