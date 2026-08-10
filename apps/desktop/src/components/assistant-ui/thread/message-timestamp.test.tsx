import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Thread } from '.'

// Message timestamps should be visible at a glance on every message — the
// hover-only relative age in the assistant action bar never showed the actual
// time. These tests pin the always-visible label (and its absence when no
// timestamp exists, e.g. pending/streaming rows).

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

// The running-state enter animation calls `el.animate`, which jsdom lacks.
Element.prototype.animate = function animate(): Animation {
  return {
    cancel: () => {},
    currentTime: null,
    effect: null,
    finished: Promise.resolve(),
    id: '',
    oncancel: null,
    onfinish: null,
    onremove: null,
    pause: () => {},
    pending: false,
    play: () => {},
    playState: 'finished',
    playbackRate: 1,
    ready: Promise.resolve(),
    remove: () => {},
    replaceState: 'active',
    startTime: null,
    timeline: null,
    updatePlaybackRate: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => true,
    commitStyles: () => {},
    persist: () => {},
    reverse: () => {},
    finish: () => {}
  } as unknown as Animation
}

afterEach(() => {
  cleanup()
})

function userMessage(over: Partial<ThreadMessage> = {}): ThreadMessage {
  return {
    id: 'user-1',
    role: 'user',
    content: [{ type: 'text', text: 'question one' }],
    attachments: [],
    createdAt: new Date(),
    metadata: { custom: {} },
    ...over
  } as ThreadMessage
}

function assistantMessage(over: Partial<ThreadMessage> = {}): ThreadMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: [{ type: 'text', text: 'done' }],
    status: { type: 'complete', reason: 'stop' },
    createdAt: new Date(),
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {}
    },
    ...over
  } as ThreadMessage
}

function timestampIn(root: string): string | null {
  const rootEl = document.querySelector(root)

  return rootEl?.querySelector('[data-slot="aui_msg-timestamp"]')?.textContent ?? null
}

function Harness({ messages }: { messages: ThreadMessage[] }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages,
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

describe('MessageTimestamp', () => {
  it('shows an always-visible time label on the user message', async () => {
    const createdAt = new Date(Date.now() - 2 * 60 * 60 * 1000)
    render(<Harness messages={[userMessage({ createdAt }), assistantMessage()]} />)

    await screen.findByText('done')

    // "Today, 3:42 PM" / "Yesterday, 3:42 PM" — a visible label on the message
    // itself, not a hover tooltip.
    expect(timestampIn('[data-role="user"]')).toMatch(/^(Today|Yesterday), /)
  })

  it('shows an always-visible time label on the assistant message', async () => {
    const createdAt = new Date(Date.now() - 2 * 60 * 60 * 1000)
    render(<Harness messages={[userMessage(), assistantMessage({ createdAt })]} />)

    await screen.findByText('done')

    expect(timestampIn('[data-role="assistant"]')).toMatch(/^(Today|Yesterday), /)
  })

  it('renders no timestamp when the message has none (pending / streaming rows)', async () => {
    render(
      <Harness
        messages={[
          userMessage({ createdAt: undefined }),
          assistantMessage({ createdAt: undefined, status: { type: 'running' } })
        ]}
      />
    )

    await screen.findByText('done')

    expect(timestampIn('[data-role="user"]')).toBeNull()
    expect(timestampIn('[data-role="assistant"]')).toBeNull()
  })
})
