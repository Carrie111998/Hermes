import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { assistantMessage, stubThreadEnvironment, stubThreadViewportSize, userMessage } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()
stubThreadViewportSize()

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function Harness({ messages }: { messages: ThreadMessage[] }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages,
    isRunning: false,
    onNew: async () => {},
    onEdit: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

function rect(top: number, bottom: number): DOMRect {
  return {
    bottom,
    height: bottom - top,
    left: 0,
    right: 800,
    top,
    width: 800,
    x: 0,
    y: top,
    toJSON: () => ({})
  }
}

describe('jump to answer start', () => {
  it('offers the action on a user prompt that has an assistant response', () => {
    render(<Harness messages={[userMessage(), assistantMessage()]} />)

    expect(screen.getByRole('button', { name: 'Jump to answer start' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Restore checkpoint' })).toBeNull()
  })

  it('omits the action until the prompt has an assistant response', () => {
    render(<Harness messages={[userMessage()]} />)

    expect(screen.queryByRole('button', { name: 'Jump to answer start' })).toBeNull()
  })

  it('scrolls only the owning transcript so the answer begins below its sticky prompt', () => {
    const { container } = render(
      <>
        <div data-testid="first-pane">
          <Harness messages={[userMessage(), assistantMessage()]} />
        </div>
        <div data-testid="second-pane">
          <Harness messages={[userMessage(), assistantMessage()]} />
        </div>
      </>
    )

    const firstPane = screen.getByTestId('first-pane')
    const secondPane = screen.getByTestId('second-pane')
    const viewport = firstPane.querySelector<HTMLElement>('[data-slot="aui_thread-viewport"]')
    const otherViewport = secondPane.querySelector<HTMLElement>('[data-slot="aui_thread-viewport"]')
    const prompt = firstPane.querySelector<HTMLElement>('[data-slot="aui_user-message-root"]')
    const answer = firstPane.querySelector<HTMLElement>('[data-slot="aui_assistant-message-root"]')

    expect(viewport).toBeTruthy()
    expect(otherViewport).toBeTruthy()
    expect(prompt).toBeTruthy()
    expect(answer).toBeTruthy()

    Object.defineProperties(viewport!, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 2_000 },
      scrollTop: { configurable: true, value: 800, writable: true }
    })
    Object.defineProperty(otherViewport!, 'scrollTop', { configurable: true, value: 0, writable: true })
    vi.spyOn(viewport!, 'getBoundingClientRect').mockReturnValue(rect(0, 600))
    vi.spyOn(prompt!, 'getBoundingClientRect').mockReturnValue(rect(20, 100))
    vi.spyOn(answer!, 'getBoundingClientRect').mockReturnValue(rect(-400, -200))
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
      callback(performance.now() + 200)

      return 1
    })

    fireEvent.click(within(firstPane).getByRole('button', { name: 'Jump to answer start' }))

    expect(viewport!.scrollTop).toBe(300)
    expect(otherViewport!.scrollTop).toBe(0)
    expect(container.querySelector('[data-slot="aui_edit-composer-root"]')).toBeNull()
  })
})
