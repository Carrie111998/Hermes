import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useComposerDrop } from '@/app/chat/composer/hooks/use-composer-drop'

// Composer drop-handler tests for the text-drop path added alongside the
// existing file-drop engine. A drag of selected message text carries only
// `text/plain` — no HERMES_PATHS_MIME, no Files — and must reach the composer
// as a quoted block via requestComposerInsert, without disturbing the
// file-drop paths that already existed.

const insertComposer = vi.hoisted(() => vi.fn())

vi.mock('@/app/chat/composer/focus', () => ({
  requestComposerInsert: insertComposer
}))

// Minimal DataTransfer stand-in for jsdom. `types` is the only field the
// guards read; `getData` serves the drop handlers.
function textTransfer(text: string) {
  return {
    dropEffect: 'none',
    effectAllowed: 'none',
    files: { length: 0, item: () => null },
    getData: (format: string) => (format === 'text/plain' ? text : ''),
    items: [] as unknown[],
    types: text.length > 0 ? ['text/plain'] : []
  }
}

function renderDrop() {
  const insertInlineRefs = vi.fn(() => false)
  const onAttachDroppedItems = vi.fn()
  const requestMainFocus = vi.fn()

  const { result } = renderHook(() =>
    useComposerDrop({
      cwd: '/repo',
      insertInlineRefs,
      onAttachDroppedItems,
      requestMainFocus
    })
  )

  return { insertInlineRefs, onAttachDroppedItems, requestMainFocus, result }
}

describe('useComposerDrop — text drops', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    insertComposer.mockReset()
  })

  it('inserts dragged text into the composer as-is (pre-quoted by the drag source)', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: textTransfer('> hello world'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).toHaveBeenCalledWith('> hello world')
    expect(event.preventDefault).toHaveBeenCalled()
  })

  it('trims surrounding whitespace from the dropped text', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: textTransfer('  > hello world\n\n  '),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).toHaveBeenCalledWith('> hello world')
  })

  it('ignores an empty text drop', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: textTransfer(''),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).not.toHaveBeenCalled()
  })

  it('inserts text dropped onto the input area', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: textTransfer('> from input drop'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleInputDrop(event as unknown as React.DragEvent<HTMLDivElement>)

    expect(insertComposer).toHaveBeenCalledWith('> from input drop')
    expect(event.stopPropagation).toHaveBeenCalled()
  })

  it('text drag-over accepts the drop with copy effect on the form', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: textTransfer('> hello'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDragOver(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(event.preventDefault).toHaveBeenCalled()
    expect(event.dataTransfer.dropEffect).toBe('copy')
  })

  it('text drag-over accepts the drop on the input area', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: textTransfer('> hello'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleInputDragOver(event as unknown as React.DragEvent<HTMLDivElement>)

    expect(event.preventDefault).toHaveBeenCalled()
    expect(event.stopPropagation).toHaveBeenCalled()
    expect(event.dataTransfer.dropEffect).toBe('copy')
  })

  it('text drops work even when no attach handler is wired', () => {
    const insertInlineRefs = vi.fn(() => false)
    const requestMainFocus = vi.fn()

    const { result } = renderHook(() =>
      useComposerDrop({
        cwd: '/repo',
        insertInlineRefs,
        onAttachDroppedItems: undefined,
        requestMainFocus
      })
    )

    const event = {
      dataTransfer: textTransfer('> still works'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).toHaveBeenCalledWith('> still works')
  })
})
