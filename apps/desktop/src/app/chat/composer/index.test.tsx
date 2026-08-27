import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ThreadRuntime } from '@/components/assistant-ui/test-utils'
import { registry } from '@/contrib/registry'
import { clearSessionDraft, stashSessionDraft, takeSessionDraft } from '@/store/composer'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  isQueueParked,
  parkQueuedPrompts
} from '@/store/composer-queue'
import { _resetComposerQueueDrainsForTests } from '@/store/composer-queue-drain'
import { _resetComposerStorageMigrationsForTests } from '@/store/composer-storage-migration'
import { encodeComposerStorageScopeKey, legacyComposerStorageScopeKey } from '@/store/composer-storage-scope'

import { COMPOSER_AREAS, type ComposerDraft, type ComposerMiddleware } from './contrib'
import type { ChatBarProps } from './types'

import { ChatBar } from './index'

const STORAGE_OWNER = { connectionId: 'connection-a', profile: 'profile-a' }
const storageKey = (storedSessionId: string | null) => encodeComposerStorageScopeKey(STORAGE_OWNER, storedSessionId)

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
    clearSessionDraft('profile-a\0stored-1')
    clearSessionDraft('profile-a\0tip-a')
    clearSessionDraft('profile-a\0root-a')
    clearSessionDraft('profile-a\0__new__')
    clearSessionDraft('profile-b\0stored-1')
    clearSessionDraft(storageKey(null))
    clearSessionDraft(storageKey('stored-1'))
    clearSessionDraft(storageKey('tip-a'))
    clearSessionDraft(storageKey('root-a'))
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    _resetComposerQueueDrainsForTests()
    _resetComposerStorageMigrationsForTests()
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

  it('loads a legacy unqualified draft into its first profile-qualified scope', () => {
    stashSessionDraft('stored-1', 'legacy draft', [])

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              legacyStorageScopeKeys: ['stored-1'],
              queueSessionKey: 'stored-1',
              storageScopeKey: 'profile-a\0stored-1'
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    expect(view.container.querySelector('[data-slot="composer-rich-input"]')?.textContent).toBe('legacy draft')
  })

  it('quarantines an ambiguous unqualified draft unless the caller proves the alias', () => {
    const canonicalKey = storageKey('stored-ambiguous')

    stashSessionDraft('stored-ambiguous', 'must not cross owners', [])

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              legacyStorageScopeKeys: [legacyComposerStorageScopeKey(STORAGE_OWNER, 'stored-ambiguous')],
              queueSessionKey: 'stored-ambiguous',
              storageScopeKey: canonicalKey
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    expect(view.container.querySelector('[data-slot="composer-rich-input"]')?.textContent).toBe('')
    expect(takeSessionDraft('stored-ambiguous').text).toBe('must not cross owners')
    expect(takeSessionDraft(canonicalKey).text).toBe('')
  })

  it('claims a pre-codec qualified draft and queue before the canonical scope paints', () => {
    const legacyKey = legacyComposerStorageScopeKey(STORAGE_OWNER, 'stored-1')
    const canonicalKey = storageKey('stored-1')

    stashSessionDraft(legacyKey, 'pre-codec qualified draft', [])
    enqueueQueuedPrompt(legacyKey, { attachments: [], text: 'pre-codec queued turn' })

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              busy: true,
              legacyStorageScopeKeys: [legacyKey],
              queueSessionKey: 'stored-1',
              storageScopeKey: canonicalKey
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    expect(view.container.querySelector('[data-slot="composer-rich-input"]')?.textContent).toBe(
      'pre-codec qualified draft'
    )
    expect(takeSessionDraft(legacyKey).text).toBe('')
    expect(takeSessionDraft(canonicalKey).text).toBe('pre-codec qualified draft')
    expect(getQueuedPrompts(legacyKey)).toHaveLength(0)
    expect(getQueuedPrompts(canonicalKey).map(entry => entry.text)).toEqual(['pre-codec queued turn'])
  })

  it('moves the qualified New Chat composer state onto its first stored session before paint', () => {
    const newChatKey = storageKey(null)
    const storedKey = storageKey('stored-1')

    stashSessionDraft(newChatKey, 'first-session draft', [])
    enqueueQueuedPrompt(newChatKey, { attachments: [], text: 'queued before create' })
    parkQueuedPrompts(newChatKey)

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar {...props({ busy: true, queueSessionKey: null, storageScopeKey: newChatKey })} />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!
    expect(editor.textContent).toBe('first-session draft')

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              busy: true,
              queueSessionKey: 'stored-1',
              storageMigration: { fromKey: newChatKey, kind: 'new-session', toKey: storedKey },
              storageScopeKey: storedKey
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    expect(view.container.querySelector('[data-slot="composer-rich-input"]')).toBe(editor)
    expect(editor.textContent).toBe('first-session draft')
    expect(takeSessionDraft(newChatKey).text).toBe('first-session draft')
    expect(takeSessionDraft(storedKey).text).toBe('first-session draft')
    expect(getQueuedPrompts(newChatKey).map(entry => entry.text)).toEqual(['queued before create'])
    expect(getQueuedPrompts(storedKey).map(entry => entry.text)).toEqual(['queued before create'])
    expect(isQueueParked(newChatKey)).toBe(true)
    expect(isQueueParked(storedKey)).toBe(true)
  })

  it('moves a qualified lineage-tip draft before the root scope first takes and paints', () => {
    const tipKey = storageKey('tip-a')
    const rootKey = storageKey('root-a')

    stashSessionDraft(tipKey, 'draft typed on compression tip', [])

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              busy: true,
              queueSessionKey: 'root-a',
              storageMigration: { fromKey: tipKey, kind: 'lineage', toKey: rootKey },
              storageScopeKey: rootKey
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    expect(view.container.querySelector('[data-slot="composer-rich-input"]')?.textContent).toBe(
      'draft typed on compression tip'
    )
    expect(takeSessionDraft(tipKey).text).toBe('draft typed on compression tip')
    expect(takeSessionDraft(rootKey).text).toBe('draft typed on compression tip')
  })

  it('swaps profile-qualified drafts when both profiles share the same stored id', () => {
    stashSessionDraft('profile-a\0stored-1', 'profile A draft', [])
    stashSessionDraft('profile-b\0stored-1', 'profile B draft', [])

    const view = render(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              identityScopeKey: 'profile-a\0stored-1',
              queueSessionKey: 'stored-1',
              storageScopeKey: 'profile-a\0stored-1'
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    const editor = view.container.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

    expect(editor.textContent).toBe('profile A draft')

    view.rerender(
      <MemoryRouter>
        <ThreadRuntime messages={[]}>
          <ChatBar
            {...props({
              actionsDisabled: true,
              identityScopeKey: 'profile-b\0stored-1',
              queueSessionKey: 'stored-1',
              storageScopeKey: 'profile-b\0stored-1'
            })}
          />
        </ThreadRuntime>
      </MemoryRouter>
    )

    expect(view.container.querySelector('[data-slot="composer-rich-input"]')).toBe(editor)
    expect(editor.textContent).toBe('profile B draft')
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
