import { useCallback } from 'react'

import { createDragGhost, type DragGhost } from '@/lib/drag-ghost'

import { hasTextSelection } from './selection'

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
 * The ghost is destroyed on `dragend`. Cleanup is module-level (not a ref
 * inside the hook) on purpose: message bubbles can unmount mid-drag (a
 * streaming turn removes a message, a session switch tears the transcript
 * down), and a ref owned by the unmounted component would orphan the ghost
 * node forever. The module holder + document-level dragend listener survive
 * unmount and always release the node.
 *
 * This is an ADDITION, not a replacement: dragstart is a separate event from
 * click, contextmenu, and pointerdown. No existing behavior is touched.
 */

let activeGhost: DragGhost | null = null

function releaseGhost() {
  activeGhost?.destroy()
  activeGhost = null
}

function ensureDocumentListener() {
  if (typeof document === 'undefined') {
    return
  }

  // Idempotent: a single document-level dragend listener serves every message
  // bubble. It also catches drags that end OUTSIDE the app window, where the
  // bubble's own React dragend handler may never fire.
  if (document.documentElement.dataset.hermesDragGhostArmed === '1') {
    return
  }

  document.documentElement.dataset.hermesDragGhostArmed = '1'
  document.addEventListener('dragend', releaseGhost)
}

ensureDocumentListener()

export function useDragTextToComposer() {
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
    // Truncate to ~40 chars so the chip stays compact. Release any stale ghost
    // first (a previous drag whose dragend was swallowed).
    releaseGhost()
    activeGhost = createDragGhost(text.length > 40 ? text.slice(0, 40) + '…' : text)
    activeGhost.moveTo(event.clientX, event.clientY)
  }, [])

  const onDragEnd = useCallback(() => {
    releaseGhost()
  }, [])

  return { onDragStart, onDragEnd }
}
