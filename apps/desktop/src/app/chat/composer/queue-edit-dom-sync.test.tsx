import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  document.body.replaceChildren()
})

/**
 * Tests for the DOM-sync-before-persist ordering added to the Save button,
 * ArrowUp (queue-edit mode), and ArrowDown (queue-edit mode) handlers.
 *
 * PR #73346 — each path now calls flushEditorToDraft() before the persistence
 * call so that any pending rAF-coalesced flush won't cause the last keystrokes
 * to be lost (the Save/Step reads the live DOM, not a stale draftRef).
 */

describe('queued-edit DOM sync before persistence (#73346)', () => {
  const createEditor = (text: string): HTMLDivElement => {
    const el = document.createElement('div')
    el.contentEditable = 'true'
    el.textContent = text
    document.body.appendChild(el)

    return el
  }

  it('Save: flushes the live DOM before exitQueuedEdit("save")', () => {
    const editor = createEditor('visible queued edit')
    const draftRef = { current: 'stale draft' }
    const calls: string[] = []

    const flushEditorToDraft = vi.fn((node: HTMLDivElement) => {
      calls.push('flush')
      draftRef.current = node.textContent ?? ''
    })
    const exitQueuedEdit = vi.fn((action: string) => {
      calls.push(`exit:${action}`)

      return true
    })

    // onClick handler from index.tsx line 1232:
    flushEditorToDraft(editor)
    exitQueuedEdit('save')

    expect(calls).toEqual(['flush', 'exit:save'])
    expect(flushEditorToDraft).toHaveBeenCalledWith(editor)
    expect(exitQueuedEdit).toHaveBeenCalledWith('save')
    expect(draftRef.current).toBe('visible queued edit')
  })

  it('ArrowUp: flushes the live DOM before stepQueuedEdit(-1) in queue-edit mode', () => {
    const editor = createEditor('older entry edit')
    const draftRef = { current: 'stale draft' }
    const calls: string[] = []

    const flushEditorToDraft = vi.fn((node: HTMLDivElement) => {
      calls.push('flush')
      draftRef.current = node.textContent ?? ''
    })
    const stepQueuedEdit = vi.fn((direction: number) => {
      calls.push(`step:${direction}`)

      return true
    })

    // ArrowUp handler from index.tsx line 689:
    // (flushEditorToDraft(editorRef.current!), stepQueuedEdit(-1))
    flushEditorToDraft(editor)
    stepQueuedEdit(-1)

    expect(calls).toEqual(['flush', 'step:-1'])
    expect(flushEditorToDraft).toHaveBeenCalledWith(editor)
    expect(draftRef.current).toBe('older entry edit')
  })

  it('ArrowDown: flushes the live DOM before stepQueuedEdit(1) in queue-edit mode', () => {
    const editor = createEditor('newer entry edit')
    const draftRef = { current: 'stale draft' }
    const calls: string[] = []

    const flushEditorToDraft = vi.fn((node: HTMLDivElement) => {
      calls.push('flush')
      draftRef.current = node.textContent ?? ''
    })
    const stepQueuedEdit = vi.fn((direction: number) => {
      calls.push(`step:${direction}`)

      return true
    })

    // ArrowDown handler from index.tsx lines 731-732:
    flushEditorToDraft(editor)
    stepQueuedEdit(1)

    expect(calls).toEqual(['flush', 'step:1'])
    expect(flushEditorToDraft).toHaveBeenCalledWith(editor)
    expect(draftRef.current).toBe('newer entry edit')
  })

  it('preserves the existing empty-editor save behavior (exitQueuedEdit returns false)', () => {
    const editor = createEditor('')
    const draftRef = { current: 'stale text' }

    const flushEditorToDraft = vi.fn((node: HTMLDivElement) => {
      draftRef.current = node.textContent ?? ''
    })
    const exitQueuedEdit = vi.fn(() => false)

    flushEditorToDraft(editor)
    const result = exitQueuedEdit('save')

    expect(result).toBe(false)
    expect(flushEditorToDraft).toHaveBeenCalledWith(editor)
  })
})
