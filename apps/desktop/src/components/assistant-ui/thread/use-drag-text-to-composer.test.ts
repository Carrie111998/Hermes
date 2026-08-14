import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useDragTextToComposer } from '@/components/assistant-ui/thread/use-drag-text-to-composer'

// The drag-text-to-composer hook is a thin wrapper around the HTML5 Drag and
// Drop API. Tests exercise the handler's behavior through a simulated
// DragEvent, verifying that:
// - Selected text is formatted as quoted lines and set on the DataTransfer
// - effectAllowed is 'copy'
// - No selection → event is prevented (no-op)
// - dragend cleans up the ghost

// Minimal DataTransfer stand-in for jsdom (no native DataTransfer constructor).
interface StubDataTransfer {
  effectAllowed: string
  getData: (format: string) => string
  setData: (format: string, data: string) => void
  types: string[]
}

function stubDataTransfer(): StubDataTransfer {
  const store: Record<string, string> = {}

  return {
    effectAllowed: 'none',
    getData: (format: string) => store[format] ?? '',
    setData: (format: string, data: string) => {
      store[format] = data
    },
    types: []
  }
}

describe('useDragTextToComposer', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  function createDragEvent(selectedText: string) {
    const dataTransfer = stubDataTransfer()

    // Simulate a live selection
    const selection = {
      isCollapsed: selectedText.length === 0,
      toString: () => selectedText
    }

    vi.spyOn(window, 'getSelection').mockReturnValue(selection as Selection)

    return {
      dataTransfer,
      clientX: 100,
      clientY: 200,
      preventDefault: vi.fn()
    }
  }

  it('sets text/plain with quoted text on dragstart', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('hello world')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.dataTransfer.effectAllowed).toBe('copy')
    expect(event.dataTransfer.getData('text/plain')).toBe('> hello world')
  })

  it('quotes each line separately', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('line one\nline two\nline three')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.dataTransfer.getData('text/plain')).toBe('> line one\n> line two\n> line three')
  })

  it('prevents the drag when no text is selected', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.preventDefault).toHaveBeenCalled()
  })

  it('prevents the drag when selection is collapsed', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('')

    // Collapsed selection: isCollapsed = true, toString returns ''
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true,
      toString: () => ''
    } as Selection)

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.preventDefault).toHaveBeenCalled()
  })

  it('cleans up the drag ghost on dragend', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('test')

    const beforeCount = document.body.children.length

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    // The ghost should have been appended to the body
    expect(document.body.children.length).toBe(beforeCount + 1)

    // dragend should remove it
    result.current.onDragEnd()

    expect(document.body.children.length).toBe(beforeCount)
  })

  it('is a no-op on dragend when no ghost was created', () => {
    const { result } = renderHook(() => useDragTextToComposer())

    // Should not throw
    expect(() => result.current.onDragEnd()).not.toThrow()
  })
})
