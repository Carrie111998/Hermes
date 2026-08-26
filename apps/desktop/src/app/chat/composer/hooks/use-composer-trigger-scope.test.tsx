import type { Unstable_TriggerItem } from '@assistant-ui/core'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useComposerTrigger } from './use-composer-trigger'

const slashItem: Unstable_TriggerItem = {
  id: '/status',
  label: 'status',
  metadata: { action: '', command: 'status', display: '/status', group: '', meta: '', rawText: '/status' },
  type: 'slash'
}

function placeCaretAtEnd(editor: HTMLDivElement) {
  const range = globalThis.document.createRange()
  const selection = window.getSelection()!

  range.selectNodeContents(editor)
  range.collapse(false)
  selection.removeAllRanges()
  selection.addRange(range)
}

describe('useComposerTrigger session scope', () => {
  afterEach(() => {
    cleanup()
    globalThis.document.body.replaceChildren()
    vi.restoreAllMocks()
  })

  it('closes A completion state and rejects its stale picker after switching to B', () => {
    const editor = globalThis.document.createElement('div')

    editor.contentEditable = 'true'
    editor.textContent = '/'
    globalThis.document.body.append(editor)
    placeCaretAtEnd(editor)

    const draftRef = { current: '/' }
    const editorRef = { current: editor }
    const at = { adapter: null, loading: false }
    const slash = { adapter: { search: () => [slashItem] } as never, loading: false }

    const hook = renderHook(
      ({ scopeKey }: { scopeKey: string }) =>
        useComposerTrigger({
          actionsDisabled: false,
          at,
          draftRef,
          editorRef,
          requestMainFocus: vi.fn(),
          scopeKey,
          setComposerText: vi.fn(),
          slash
        }),
      { initialProps: { scopeKey: 'session-a' } }
    )

    act(() => hook.result.current.refreshTrigger())
    expect(hook.result.current.trigger?.kind).toBe('/')

    const stalePickFromA = hook.result.current.replaceTriggerWithChip

    hook.rerender({ scopeKey: 'session-b' })

    expect(hook.result.current.trigger).toBeNull()
    expect(hook.result.current.triggerItems).toEqual([])

    act(() => stalePickFromA(slashItem))

    expect(editor.textContent).toBe('/')
  })
})
