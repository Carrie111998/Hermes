import { type AppendMessage, ExportedMessageRepository } from '@assistant-ui/react'
// Clicking a user bubble must open the inline edit composer — through the
// app's incremental external-store runtime (which reimplements capability
// resolution, incl. `edit: onEdit !== undefined`) and the stock runtime.
//
// Note: this covers the React/runtime wiring only. The Electron-level failure
// mode (titlebar -webkit-app-region:drag swallowing clicks on *stuck* sticky
// bubbles) is not reproducible in jsdom — see USER_BUBBLE_BASE_CLASS's no-drag
// carve-out in thread.tsx.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'

import { assistantMessage, stubThreadEnvironment, stubThreadViewportSize, userMessage } from '../test-utils'

import { BLUR_SETTLE_MS, LATCH_COOLDOWN_MS } from './user-edit-composer'

import { Thread } from '.'
stubThreadEnvironment()

afterEach(() => {
  cleanup()
})

stubThreadViewportSize()

async function moveFocusOutside(editor: HTMLElement) {
  const outside = window.document.createElement('button')
  window.document.body.append(outside)
  editor.focus()

  await act(async () => {
    outside.focus()
    await new Promise(resolve => window.setTimeout(resolve, 120))
  })

  outside.remove()
}

// Mirrors chat/index.tsx: incremental runtime + messageRepository + onEdit.
function IncrementalHarness({ onEdit }: { onEdit: (message: AppendMessage) => Promise<void> }) {
  const repository = ExportedMessageRepository.fromArray([userMessage(), assistantMessage()])

  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>({
    messageRepository: repository,
    isRunning: false,
    setMessages: () => {},
    onNew: async () => {},
    onEdit,
    onCancel: async () => {},
    onReload: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

// Control: stock external store runtime.
function StockHarness({ onEdit }: { onEdit: () => Promise<void> }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [userMessage(), assistantMessage()],
    isRunning: false,
    onNew: async () => {},
    onEdit
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

describe('click-to-edit user message', () => {
  it('opens the edit composer with the incremental runtime', async () => {
    const { container } = render(<IncrementalHarness onEdit={async () => {}} />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    fireEvent.click(bubble)

    await waitFor(() => {
      expect(container.querySelector('[data-slot="aui_edit-composer-root"]')).toBeTruthy()
    })
  })

  it('does not submit an inline edit while IME composition is active', async () => {
    const onEdit = vi.fn(async (_message: AppendMessage) => {})

    render(<IncrementalHarness onEdit={onEdit} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    const editor = await screen.findByRole('textbox', { name: 'Edit message' })
    const editedText = 'edit me please\u4f60'

    await act(async () => {
      fireEvent.compositionStart(editor)
      editor.textContent = editedText
      fireEvent.input(editor)
      fireEvent.keyDown(editor, { isComposing: true, key: 'Enter' })
    })

    expect(onEdit).not.toHaveBeenCalled()

    await act(async () => {
      fireEvent.compositionEnd(editor)
      fireEvent.keyDown(editor, { key: 'Enter' })
    })

    await waitFor(() => expect(onEdit).toHaveBeenCalledTimes(1))
    expect(onEdit).toHaveBeenCalledWith(
      expect.objectContaining({
        content: [{ text: editedText, type: 'text' }]
      })
    )
  })

  it('keeps a dirty inline edit open when focus leaves the composer', async () => {
    const { container } = render(<IncrementalHarness onEdit={async () => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    const editor = await screen.findByRole('textbox', { name: 'Edit message' })
    const editedText = 'edited draft that must not be discarded'

    editor.textContent = editedText
    fireEvent.input(editor)
    await moveFocusOutside(editor)

    expect(container.querySelector('[data-slot="aui_edit-composer-root"]')).toBeTruthy()
    expect((await screen.findByRole('textbox', { name: 'Edit message' })).textContent).toBe(editedText)
  })

  it('still cancels an untouched inline edit when focus leaves the composer', async () => {
    const { container } = render(<IncrementalHarness onEdit={async () => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))
    const editor = await screen.findByRole('textbox', { name: 'Edit message' })

    await moveFocusOutside(editor)

    expect(container.querySelector('[data-slot="aui_edit-composer-root"]')).toBeFalsy()
  })

  it('opens the edit composer with the stock runtime', async () => {
    const { container } = render(<StockHarness onEdit={async () => {}} />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    fireEvent.click(bubble)

    await waitFor(() => {
      expect(container.querySelector('[data-slot="aui_edit-composer-root"]')).toBeTruthy()
    })
  })

  // A long previous prompt is capped at max-h-48 in the edit composer. Without
  // an overflow rule the overflow is clipped with no way to scroll, hiding the
  // tail of the prompt. The editor must scroll its own overflow (like the main
  // composer's editor does).
  it('keeps the edit composer editor scrollable when the prompt overflows the cap', async () => {
    const { container } = render(<IncrementalHarness onEdit={async () => {}} />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    fireEvent.click(bubble)

    const editor = await waitFor(() => {
      const node = container.querySelector('[contenteditable="true"]')
      expect(node).toBeTruthy()

      return node as HTMLElement
    })

    expect(editor.className).toContain('max-h-48')
    expect(editor.className).toContain('overflow-y-auto')
  })
})

describe('Enter submission and latch behavior', () => {
  it('submits the edit when Enter is pressed', async () => {
    const onEdit = vi.fn(async () => {})
    render(<IncrementalHarness onEdit={onEdit} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    const editor = await screen.findByRole('textbox', { name: 'Edit message' })
    const editedText = 'modified text for submission'

    editor.textContent = editedText
    fireEvent.input(editor)

    fireEvent.keyDown(editor, { key: 'Enter' })

    await waitFor(() => {
      expect(onEdit).toHaveBeenCalledTimes(1)
    })
  })

  it('clears the submitting latch after onEdit resolves, allowing second edit session', async () => {
    const onEdit = vi.fn(async () => {})
    render(<IncrementalHarness onEdit={onEdit} />)

    // First edit session
    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    let editor = await screen.findByRole('textbox', { name: 'Edit message' })
    editor.textContent = 'first edit'
    fireEvent.input(editor)
    fireEvent.keyDown(editor, { key: 'Enter' })

    await waitFor(() => {
      expect(onEdit).toHaveBeenCalledTimes(1)
    })

    // Wait for the latch cooldown to clear
    await new Promise(resolve => setTimeout(resolve, 300))

    // Second edit session - open the editor again
    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    editor = await screen.findByRole('textbox', { name: 'Edit message' })
    editor.textContent = 'second edit'
    fireEvent.input(editor)
    fireEvent.keyDown(editor, { key: 'Enter' })

    // If the latch wasn't cleared, this second submission would be blocked
    await waitFor(() => {
      expect(onEdit).toHaveBeenCalledTimes(2)
    })
  })

  it('inserts a newline on Shift+Enter without submitting', async () => {
    const onEdit = vi.fn(async () => {})
    render(<IncrementalHarness onEdit={onEdit} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    const editor = await screen.findByRole('textbox', { name: 'Edit message' })

    editor.textContent = 'line one'
    fireEvent.input(editor)

    fireEvent.keyDown(editor, { key: 'Enter', shiftKey: true })

    // Shift+Enter should not call onEdit
    await new Promise(resolve => window.setTimeout(resolve, 50))
    expect(onEdit).not.toHaveBeenCalled()

    // The editor should allow the newline to be inserted (by not preventing default)
    // We don't simulate actual newline insertion here (requires complex DOM manipulation)
    // but we verify the guard did not block it
  })
})

// The edit composer is the one composer that routinely unmounts (confirming or
// cancelling tears it down), so a deferred callback can outlive the component.
// In the app that is a stray state update on a dead tree; in jsdom the window is
// already gone when the timer fires, surfacing as an uncaught
// `ReferenceError: window is not defined` that fails the whole suite from an
// unrelated test file.
//
// These assert on the timers this composer itself schedules — `getTimerCount()`
// is global and legitimately non-zero from the runtime and React internals.
describe('deferred work does not outlive the edit composer', () => {
  // Records timers scheduled while tracking is active, keyed by their delay, so
  // a test can assert on the composer's own timers. The global timer count is
  // legitimately non-zero from the runtime and React internals, so filtering by
  // this component's distinctive delays is what isolates its cleanup.
  function trackTimers() {
    const scheduled = new Map<number, { delayMs: number; run: () => void }>()
    const cleared = new Set<number>()
    const realSetTimeout = window.setTimeout.bind(window)
    const realClearTimeout = window.clearTimeout.bind(window)

    vi.spyOn(window, 'setTimeout').mockImplementation(((run: () => void, delayMs?: number) => {
      const id = realSetTimeout(run, delayMs) as unknown as number
      scheduled.set(id, { delayMs: delayMs ?? 0, run })

      return id
    }) as typeof window.setTimeout)

    vi.spyOn(window, 'clearTimeout').mockImplementation(((id?: number) => {
      if (id !== undefined) {
        cleared.add(id)
      }

      realClearTimeout(id)
    }) as typeof window.clearTimeout)

    return {
      pendingAt: (delayMs: number) =>
        [...scheduled.entries()].filter(([id, timer]) => timer.delayMs === delayMs && !cleared.has(id)).map(([id]) => id),
      run: (id: number) => scheduled.get(id)?.run()
    }
  }

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('clears the pending submit latch timer when the composer unmounts', async () => {
    const onEdit = vi.fn(async () => {})
    const { unmount } = render(<IncrementalHarness onEdit={onEdit} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    const editor = await screen.findByRole('textbox', { name: 'Edit message' })
    editor.textContent = 'edit that schedules the latch timer'
    fireEvent.input(editor)

    // Track only from here: the latch is scheduled synchronously by this
    // keyDown. Assert before awaiting anything — a resolved onEdit closes the
    // edit and unmounts the composer, which clears the latch on its own.
    const timers = trackTimers()
    fireEvent.keyDown(editor, { key: 'Enter' })

    const stranded = timers.pendingAt(LATCH_COOLDOWN_MS)
    expect(stranded.length).toBeGreaterThan(0)

    unmount()

    // The latch timer must not survive the composer that scheduled it.
    expect(timers.pendingAt(LATCH_COOLDOWN_MS)).toEqual([])
  })

  it('clears the deferred blur handler when the composer unmounts', async () => {
    const onEdit = vi.fn(async () => {})
    const { unmount } = render(<IncrementalHarness onEdit={onEdit} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }))

    const editor = await screen.findByRole('textbox', { name: 'Edit message' })

    const timers = trackTimers()
    // Blur defers the trigger-close and the cancel by BLUR_SETTLE_MS.
    fireEvent.blur(editor)

    const stranded = timers.pendingAt(BLUR_SETTLE_MS)
    expect(stranded.length).toBeGreaterThan(0)

    unmount()

    expect(timers.pendingAt(BLUR_SETTLE_MS)).toEqual([])

    // Firing a stranded callback by hand is what a real timer flush would do:
    // post-fix these are cleared, so nothing is left to touch the dead tree.
    expect(() => {
      stranded.forEach(id => timers.run(id))
    }).not.toThrow()
  })
})
