import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ThreadRuntime } from '@/components/assistant-ui/test-utils'

import type { ChatBarProps } from './types'

import { ChatBar } from './index'

const mocks = vi.hoisted(() => ({
  runComposerMiddleware: vi.fn(async draft => draft)
}))

vi.mock('./contrib', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  runComposerMiddleware: mocks.runComposerMiddleware
}))

function props(overrides: Partial<ChatBarProps> = {}): ChatBarProps {
  return {
    actionsDisabled: false,
    busy: false,
    cwd: null,
    disabled: false,
    onCancel: vi.fn(),
    onSubmit: vi.fn(async () => true),
    queueSessionKey: 'session-a',
    sessionId: 'runtime-a',
    state: {
      model: { canSwitch: false, model: 'test-model', provider: 'test-provider' },
      tools: { enabled: false, label: 'Tools' },
      voice: { active: false, enabled: false }
    },
    ...overrides
  }
}

describe('ChatBar transition focus', () => {
  afterEach(() => {
    cleanup()
    window.localStorage.clear()
    vi.restoreAllMocks()
    mocks.runComposerMiddleware.mockReset()
    mocks.runComposerMiddleware.mockImplementation(async draft => draft)
  })

  it('keeps the real contenteditable, draft, focus, and caret while actions become fenced', () => {
    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props()} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    editor.focus()
    editor.textContent = 'draft in progress'
    fireEvent.input(editor)

    const range = globalThis.document.createRange()
    const selection = window.getSelection()!

    range.setStart(editor.firstChild!, 5)
    range.collapse(true)
    selection.removeAllRanges()
    selection.addRange(range)

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ actionsDisabled: true })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editorAfterTransition = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    expect(editorAfterTransition).toBe(editor)
    expect(globalThis.document.activeElement).toBe(editor)
    expect(editorAfterTransition.textContent).toBe('draft in progress')
    expect(selection.anchorNode).toBe(editor.firstChild)
    expect(selection.anchorOffset).toBe(5)
  })

  it('rejects middleware output after the committed submit scope changes', async () => {
    let resolveMiddleware: ((draft: { attachments: never[]; text: string }) => void) | undefined
    const middleware = new Promise<{ attachments: never[]; text: string }>(resolve => {
      resolveMiddleware = resolve
    })
    const onSubmit = vi.fn(async () => true)
    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ onSubmit })} />
        </ThreadRuntime>
      </MemoryRouter>
    )
    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    mocks.runComposerMiddleware.mockReturnValueOnce(middleware)
    editor.textContent = 'message from session A'
    fireEvent.input(editor)
    fireEvent.keyDown(editor, { key: 'Enter' })

    await waitFor(() => expect(mocks.runComposerMiddleware).toHaveBeenCalledOnce())

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              actionsDisabled: true,
              onSubmit,
              queueSessionKey: 'session-b',
              sessionId: 'runtime-b'
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    await act(async () => {
      resolveMiddleware?.({ attachments: [], text: 'message from session A' })
      await middleware
    })

    expect(onSubmit).not.toHaveBeenCalled()
  })
})
