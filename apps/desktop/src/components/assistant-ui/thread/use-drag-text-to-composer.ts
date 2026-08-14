import { useCallback, useRef } from 'react'

import { createDragGhost, type DragGhost } from '@/lib/drag-ghost'

import { hasTextSelection } from './user-message'

/**
 * Shared drag-text-to-composer behavior for message bubbles (user + assistant).
 *
 * When the user selects text in a message and starts dragging, the handler:
 * 1. Reads the live selection text
 * 2. Formats it as a quoted block ("> line" per line, same as "Paste as text")
 * 3. Sets `text/plain` on the DataTransfer so the composer's drop handler
 *    (use-composer-drop) can pick it up
 * 4. Creates a drag ghost via the existing `createDragGhost` utility
 *
 * The ghost is destroyed on `dragend` — even when the drop target is outside
 * the app window (the browser fires dragend regardless).
 *
 * This is an ADDITION, not a replacement: dragstart is a separate event from
 * click, contextmenu, and pointerdown. No existing behavior is touched.
 */
export function useDragTextToComposer() {
  const ghostRef = useRef<DragGhost | null>(null)

  const onDragStart = useCallback((event: React.DragEvent<HTMLElement>) => {
    if (!hasTextSelection()) {
      event.preventDefault()

      return
    }

    const selection = window.getSelection()
    const text = selection?.toString().trim() ?? ''

    if (!text) {
      event.preventDefault()

      return
    }

    const quoted = text
      .split('\n')
      .map(line => `> ${line}`)
      .join('\n')

    event.dataTransfer.effectAllowed = 'copy'
    event.dataTransfer.setData('text/plain', quoted)

    // Drag ghost: a flat, pointer-following chip showing what's being dragged.
    // Truncate to ~40 chars so the chip stays compact.
    const label = text.length > 40 ? text.slice(0, 40) + '…' : text
    const ghost = createDragGhost(label)

    ghostRef.current = ghost
    ghost.moveTo(event.clientX, event.clientY)
  }, [])

  const onDragEnd = useCallback(() => {
    ghostRef.current?.destroy()
    ghostRef.current = null
  }, [])

  return { onDragStart, onDragEnd }
}
