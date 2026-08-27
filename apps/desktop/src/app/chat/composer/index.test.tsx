import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ThreadRuntime } from '@/components/assistant-ui/test-utils'
import { registry } from '@/contrib/registry'
import { clearSessionDraft, stashSessionDraft } from '@/store/composer'

import { COMPOSER_AREAS, type ComposerDraft, type ComposerMiddleware } from './contrib'
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
  const disposers: Array<() => void> = []

  afterEach(() => {
    for (const dispose of disposers.splice(0)) {
      dispose()
    }

    cleanup()
    window.localStorage.clear()
    vi.restoreAllMocks()

    clearSessionDraft('runtime-a')
    clearSessionDraft(null)
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

  it('switches from A to the New Chat draft scope while runtime A still lags', () => {
    stashSessionDraft('runtime-a', 'session A draft', [])
    stashSessionDraft(null, 'new chat draft', [])

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ queueSessionKey: 'runtime-a' })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    expect(editor.textContent).toBe('session A draft')

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ actionsDisabled: true, queueSessionKey: null })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const newChatEditor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    expect(newChatEditor).toBe(editor)
    expect(newChatEditor.textContent).toBe('new chat draft')
  })

  it('drops an A submit when asynchronous middleware resolves after B converges', async () => {
    let resolveMiddleware: ((draft: ComposerDraft) => void) | undefined

    const middlewarePending = new Promise<ComposerDraft>(resolve => {
      resolveMiddleware = resolve
    })

    const handler = vi.fn(() => middlewarePending)

    disposers.push(
      registry.register({
        area: COMPOSER_AREAS.middleware,
        data: { handler } satisfies ComposerMiddleware,
        id: 'test-delayed-middleware'
      })
    )

    const onSubmit = vi.fn(async () => true)

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ onSubmit })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    editor.textContent = '/compress'
    fireEvent.input(editor)
    fireEvent.keyDown(editor, { key: 'Enter' })
    expect(handler).toHaveBeenCalledOnce()

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ onSubmit, queueSessionKey: 'session-b', sessionId: 'runtime-b' })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    await act(async () => resolveMiddleware?.({ text: '/compress' }))

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('keeps an old A middleware callback stale across A to New Chat to A', async () => {
    let resolveMiddleware: ((draft: ComposerDraft) => void) | undefined

    const middlewarePending = new Promise<ComposerDraft>(resolve => {
      resolveMiddleware = resolve
    })

    disposers.push(
      registry.register({
        area: COMPOSER_AREAS.middleware,
        data: { handler: () => middlewarePending } satisfies ComposerMiddleware,
        id: 'test-new-chat-epoch-middleware'
      })
    )

    const onSubmit = vi.fn(async () => true)

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ onSubmit, queueSessionKey: 'runtime-a' })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    editor.textContent = '/compress'
    fireEvent.input(editor)
    fireEvent.keyDown(editor, { key: 'Enter' })

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ actionsDisabled: true, onSubmit, queueSessionKey: null })} />
        </ThreadRuntime>
      </MemoryRouter>
    )
    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ onSubmit, queueSessionKey: 'runtime-a' })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    await act(async () => resolveMiddleware?.({ text: '/compress' }))

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('keeps profile A middleware stale when profile B has the same stored id', async () => {
    let resolveMiddleware: ((draft: ComposerDraft) => void) | undefined

    const middlewarePending = new Promise<ComposerDraft>(resolve => {
      resolveMiddleware = resolve
    })

    disposers.push(
      registry.register({
        area: COMPOSER_AREAS.middleware,
        data: { handler: () => middlewarePending } satisfies ComposerMiddleware,
        id: 'test-profile-owner-middleware'
      })
    )

    const onSubmit = vi.fn(async () => true)

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ identityScopeKey: 'profile-a\0stored-1', onSubmit, queueSessionKey: 'stored-1' })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    editor.textContent = '/compress'
    fireEvent.input(editor)
    fireEvent.keyDown(editor, { key: 'Enter' })

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              actionsDisabled: true,
              identityScopeKey: 'profile-b\0stored-1',
              onSubmit,
              queueSessionKey: 'stored-1'
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )
    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ identityScopeKey: 'profile-b\0stored-1', onSubmit, queueSessionKey: 'stored-1' })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    await act(async () => resolveMiddleware?.({ text: '/compress' }))

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('blocks image, clipboard, and PR attachment paste while actions are fenced', () => {
    const onAttachImageBlob = vi.fn(async () => true)
    const onAttachPrCommentUrl = vi.fn(() => true)
    const onPasteClipboardImage = vi.fn(async () => true)

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({ actionsDisabled: true, onAttachImageBlob, onAttachPrCommentUrl, onPasteClipboardImage })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!
    const image = new Blob(['image'], { type: 'image/png' })

    fireEvent.paste(editor, {
      clipboardData: {
        files: [],
        getData: () => '',
        items: [{ getAsFile: () => image, kind: 'file', type: 'image/png' }]
      }
    })
    fireEvent.paste(editor, {
      clipboardData: { files: [], getData: () => '', items: [] }
    })
    fireEvent.paste(editor, {
      clipboardData: {
        files: [],
        getData: () => 'https://github.com/NousResearch/hermes-agent/pull/1#discussion_r1',
        items: []
      }
    })

    expect(onAttachImageBlob).not.toHaveBeenCalled()
    expect(onPasteClipboardImage).not.toHaveBeenCalled()
    expect(onAttachPrCommentUrl).not.toHaveBeenCalled()
  })

  it('hides the underside plugin surface while session identities are fenced', () => {
    act(() => {
      disposers.push(
        registry.register({
          area: COMPOSER_AREAS.underside,
          id: 'test-unsafe-underside',
          render: () => <button type="button">Unsafe underside action</button>
        })
      )
    })

    render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ actionsDisabled: true })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    expect(screen.queryByRole('button', { name: 'Unsafe underside action' })).toBeNull()
  })
})
