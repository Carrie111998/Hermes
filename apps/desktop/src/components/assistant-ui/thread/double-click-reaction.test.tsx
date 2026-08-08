// Double-click an assistant reply to heart it (the iMessage gesture), gated on
// the same opt-in toggle as the rest of message reactions.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as ReactionsStore from '@/store/reactions'
import { $reactionsEnabled } from '@/store/reactions-enabled'
import { $localReactions } from '@/store/reactions-local'
import {
  $threadJumpButtonVisible,
  $threadScrolledUp,
  notifyThreadEditOpen,
  requestScrollToBottom,
  resetThreadScroll,
  setThreadAtBottom
} from '@/store/thread-scroll'

import { TranscriptWindowProvider } from './transcript-window'
import { isTapbackDoubleClick } from './use-message-reactions'

import { Thread } from '.'

const createdAt = new Date('2026-05-01T00:00:00.000Z')

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}

// The gesture persists through the gateway; this suite is about the local
// paint, which is what the user actually sees on the click.
vi.mock('@/store/reactions', async importOriginal => ({
  ...(await importOriginal<typeof ReactionsStore>()),
  toggleMessageReaction: vi.fn(async () => {})
}))

function assistantMessage(): ThreadMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: [{ type: 'text', text: 'done' }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as ThreadMessage
}

function userMessageWithReaction(): ThreadMessage {
  return {
    id: 'user-1',
    role: 'user',
    content: [{ type: 'text', text: 'question' }],
    attachments: [],
    createdAt,
    metadata: { custom: { reactions: [{ emoji: '❤️', author: 'user', at: 1 }] } }
  } as ThreadMessage
}

function Harness({
  message = assistantMessage(),
  messages,
  readOnly = false
}: {
  message?: ThreadMessage
  messages?: ThreadMessage[]
  readOnly?: boolean
}) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: messages ?? [message],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread readOnly={readOnly} />
    </AssistantRuntimeProvider>
  )
}

beforeEach(() => {
  $localReactions.set({})
  $reactionsEnabled.set(false)
})

afterEach(() => {
  cleanup()
  resetThreadScroll()
})

describe('isTapbackDoubleClick', () => {
  it('claims a plain double-click on message body', () => {
    expect(isTapbackDoubleClick({ detail: 2, target: document.createElement('span') })).toBe(true)
  })

  it('ignores a triple-click, so selecting a paragraph does not re-toggle', () => {
    expect(isTapbackDoubleClick({ detail: 3, target: document.createElement('span') })).toBe(false)
  })

  it('leaves double-click alone where it already means something', () => {
    const code = document.createElement('pre')
    const inner = document.createElement('code')

    code.append(inner)

    expect(isTapbackDoubleClick({ detail: 2, target: inner })).toBe(false)
    expect(isTapbackDoubleClick({ detail: 2, target: document.createElement('a') })).toBe(false)
    expect(isTapbackDoubleClick({ detail: 2, target: document.createElement('button') })).toBe(false)
  })
})

describe('double-click to heart an assistant message', () => {
  it('hearts the message, and a second double-click retracts it', async () => {
    $reactionsEnabled.set(true)
    render(<Harness />)

    const message = (await screen.findByText('done')).closest('[data-slot="aui_assistant-message-root"]')

    expect(message).toBeTruthy()

    fireEvent.doubleClick(message!, { detail: 2 })
    await waitFor(() => expect($localReactions.get()['assistant-1']?.[0]?.emoji).toBe('❤️'))

    fireEvent.doubleClick(message!, { detail: 2 })
    await waitFor(() => expect($localReactions.get()['assistant-1']).toEqual([]))
  })

  it('does nothing while reactions are off', async () => {
    render(<Harness />)

    const message = (await screen.findByText('done')).closest('[data-slot="aui_assistant-message-root"]')

    fireEvent.doubleClick(message!, { detail: 2 })

    expect($localReactions.get()['assistant-1']).toBeUndefined()
  })

  it('does nothing in a read-only transcript while reactions are enabled', async () => {
    $reactionsEnabled.set(true)
    render(<Harness readOnly />)

    const message = (await screen.findByText('done')).closest('[data-slot="aui_assistant-message-root"]')

    fireEvent.doubleClick(message!, { detail: 2 })

    expect($localReactions.get()['assistant-1']).toBeUndefined()
    expect(message?.querySelector('[data-slot="aui_msg-reactions"]')).toBeNull()
  })

  it('renders a persisted user reaction as display-only in a read-only transcript', async () => {
    $reactionsEnabled.set(true)
    render(<Harness message={userMessageWithReaction()} readOnly />)

    expect(await screen.findByText('❤️')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Remove ❤️ reaction' })).toBeNull()
    expect($localReactions.get()['user-1']).toBeUndefined()
  })

  it('does not bridge spectator scroll or edit state into the live thread', async () => {
    setThreadAtBottom(false)
    const view = render(<Harness readOnly />)

    await screen.findByText('done')
    const viewport = view.container.querySelector<HTMLElement>('[data-slot="aui_thread-viewport"]')

    expect($threadScrolledUp.get()).toBe(true)
    expect($threadJumpButtonVisible.get()).toBe(true)

    act(() => notifyThreadEditOpen())
    expect(viewport?.hasAttribute('data-editing')).toBe(false)

    Object.defineProperty(viewport!, 'clientHeight', { configurable: true, value: 50 })
    Object.defineProperty(viewport!, 'scrollHeight', { configurable: true, value: 200 })
    viewport!.scrollTop = 25
    act(() => requestScrollToBottom())
    expect(viewport?.scrollTop).toBe(25)

    view.unmount()
    expect($threadScrolledUp.get()).toBe(true)
    expect($threadJumpButtonVisible.get()).toBe(true)
  })

  it('does not mount the interactive timeline in a read-only transcript', async () => {
    const messages: ThreadMessage[] = Array.from(
      { length: 4 },
      (_, index) =>
        ({
          ...userMessageWithReaction(),
          id: `user-${index}`,
          content: [{ type: 'text' as const, text: `historical prompt ${index}` }]
        }) as ThreadMessage
    )

    render(<Harness messages={messages} readOnly />)

    await screen.findByText('historical prompt 0')
    expect(screen.queryByRole('navigation', { name: 'Conversation timeline' })).toBeNull()
  })

  it('does not inherit or mutate the live transcript window in a read-only transcript', async () => {
    const expandWindow = vi.fn()

    render(
      <TranscriptWindowProvider value={{ expandWindow, olderAvailable: true }}>
        <Harness readOnly />
      </TranscriptWindowProvider>
    )

    await screen.findByText('done')
    expect(screen.queryByRole('button', { name: 'Show earlier messages' })).toBeNull()
    expect(expandWindow).not.toHaveBeenCalled()
  })
})
