import { AssistantRuntimeProvider, type ThreadMessage, useAuiState } from '@assistant-ui/react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { textPart } from '@/lib/chat-messages'
import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'

import { useRuntimeMessageRepository } from './runtime-repository'

function message(id: string, role: ChatMessage['role'], text: string): ChatMessage {
  return { id, role, parts: [textPart(text)] }
}

function RuntimeMessages() {
  const messages = useAuiState(state => state.thread.messages)

  return (
    <ol>
      {messages.map((item, index) => (
        <li data-message-id={item.id} data-testid="runtime-message" key={index}>
          {item.content.find(part => part.type === 'text')?.text}
        </li>
      ))}
    </ol>
  )
}

function RuntimeHarness({ messages }: { messages: ChatMessage[] }) {
  const messageRepository = useRuntimeMessageRepository(messages)

  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>({
    messageRepository,
    isRunning: false,
    setMessages: () => {},
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RuntimeMessages />
    </AssistantRuntimeProvider>
  )
}

function renderedMessages() {
  return screen.getAllByTestId('runtime-message').map(element => ({
    id: element.getAttribute('data-message-id'),
    text: element.textContent
  }))
}

describe('runtime message repository', () => {
  it('leaves unique message ids unchanged', () => {
    const unique = [message('user-id', 'user', 'question'), message('assistant-id', 'assistant', 'answer')]

    render(<RuntimeHarness messages={unique} />)

    expect(renderedMessages().map(item => item.id)).toEqual(['user-id', 'assistant-id'])
    expect(unique.map(item => item.id)).toEqual(['user-id', 'assistant-id'])
  })

  it('preserves duplicate-id messages with deterministic renderer-only ids across updates', () => {
    const malformed = [
      message('reused-id', 'user', 'first visible message'),
      message('assistant-id', 'assistant', 'reply between duplicates'),
      message('reused-id', 'user', 'second visible message'),
      message('reused-id:renderer-duplicate:2', 'assistant', 'valid suffix-shaped id')
    ]

    const { rerender } = render(<RuntimeHarness messages={malformed} />)
    const initial = renderedMessages()

    expect(initial.map(item => item.text)).toEqual([
      'first visible message',
      'reply between duplicates',
      'second visible message',
      'valid suffix-shaped id'
    ])
    expect(initial[0]?.id).toBe('reused-id')
    expect(initial[3]?.id).toBe('reused-id:renderer-duplicate:2')
    expect(new Set(initial.map(item => item.id)).size).toBe(initial.length)
    expect(malformed.map(item => item.id)).toEqual([
      'reused-id',
      'assistant-id',
      'reused-id',
      'reused-id:renderer-duplicate:2'
    ])

    rerender(<RuntimeHarness messages={malformed.map(item => ({ ...item }))} />)
    expect(renderedMessages()).toEqual(initial)

    rerender(
      <RuntimeHarness
        messages={[...malformed.map(item => ({ ...item })), message('streaming-id', 'assistant', 'streaming update')]}
      />
    )
    expect(renderedMessages().slice(0, initial.length)).toEqual(initial)
  })
})
