import { cleanup, fireEvent, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ThreadRuntime } from '@/components/assistant-ui/test-utils'

import type { ChatBarProps } from './types'

import { ChatBar } from './index'

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
})
