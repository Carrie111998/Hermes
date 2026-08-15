// Bug #2: the Branch-in-new-chat button used to render unconditionally even
// when its handler was a no-op (session-tile.tsx passed `() => undefined`
// for branched/tiled chats, where nested branching isn't supported). That
// left a visibly clickable button that silently did nothing. The fix makes
// AssistantMessage's action bar hide the button entirely when no handler is
// supplied, matching how onDismissError/onRestoreToMessage already behave.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { onComposerInsertRequest } from '@/app/chat/composer/focus'
import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '@/app/chat/composer/scope'
import { expandComposerQuotes } from '@/lib/composer-quote'

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

afterEach(() => {
  cleanup()
})

function userMessage(): ThreadMessage {
  return {
    id: 'user-1',
    role: 'user',
    content: [{ type: 'text', text: 'question one' }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  } as ThreadMessage
}

function assistantMessage(): ThreadMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: [{ type: 'text', text: 'done' }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {}
    }
  } as ThreadMessage
}

function Harness({
  onBranchInNewChat,
  target = 'main'
}: {
  onBranchInNewChat?: (messageId: string) => void
  target?: string
}) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [userMessage(), assistantMessage()],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target }}>
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread onBranchInNewChat={onBranchInNewChat} />
      </AssistantRuntimeProvider>
    </ComposerScopeProvider>
  )
}

describe('AssistantMessage branch button visibility (bug #2 fix)', () => {
  it('shows the Branch in new chat button when a handler is provided (open chat)', async () => {
    render(<Harness onBranchInNewChat={() => undefined} />)

    expect(await screen.findByRole('button', { name: 'Branch in new chat' })).toBeTruthy()
  })

  it('hides the Branch in new chat button when no handler is provided (session-tile / branched chat)', async () => {
    render(<Harness />)

    // Wait for the assistant message to actually mount before asserting
    // absence, so a missing button isn't just a false negative from an
    // unrendered message.
    await screen.findByText('done')

    expect(screen.queryByRole('button', { name: 'Branch in new chat' })).toBeNull()
  })
})

describe('message Reply composer routing', () => {
  it('routes both message Reply actions to the composer that owns the thread', async () => {
    const inserts: Array<{ mode: string; target: string; text: string }> = []
    const unsubscribe = onComposerInsertRequest(detail => inserts.push(detail))

    render(<Harness target="tile:stored-42" />)

    const replyButtons = await screen.findAllByRole('button', { name: 'Reply' })

    expect(replyButtons).toHaveLength(2)
    for (const button of replyButtons) {
      expect(button.getAttribute('data-size')).toBe('icon-xs')
      expect(button.getAttribute('title')).toBeNull()
      expect(button.querySelector('svg')).not.toBeNull()
    }
    fireEvent.click(replyButtons[0]!)
    fireEvent.click(replyButtons[1]!)

    await waitFor(() => expect(inserts).toHaveLength(2))
    expect(inserts.map(({ mode, target, text }) => ({ mode, target, text: expandComposerQuotes(text) }))).toEqual([
      { mode: 'block', target: 'tile:stored-42', text: '> question one' },
      { mode: 'block', target: 'tile:stored-42', text: '> done' }
    ])

    unsubscribe()
  })
})
