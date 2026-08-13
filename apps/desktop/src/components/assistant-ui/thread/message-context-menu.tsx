import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuTrigger } from '@/components/ui/context-menu'
import { requestComposerInsert } from '@/app/chat/composer/focus'
import { addComposerTextAttachment } from '@/store/composer'
import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'

interface MessageContextMenuProps {
  children: ReactNode
  /** The message element to attach the context menu to. */
  messageId?: string
}

/** Shared right-click context menu for message blocks (user + assistant).
 *  When text is selected, offers "Add as context" (stages a chip) and
 *  "Paste as text" (inserts quoted text into the composer).
 *  When no text is selected, the native browser context menu is preserved. */
export function MessageContextMenu({ children, messageId }: MessageContextMenuProps) {
  const hasSelectionRef = useRef(false)
  const [, forceUpdate] = useState(0)

  // Keep the ref in sync with the live selection so the render decision
  // (ContextMenu wrapper vs plain children) is always correct by the time
  // the user right-clicks. selectionchange fires on drag-end, click-away,
  // and programmatic clears — every path that changes the selection.
  useEffect(() => {
    const sync = () => {
      const selection = window.getSelection()
      const hasSelection = Boolean(selection && !selection.isCollapsed && selection.toString().trim().length > 0)
      if (hasSelectionRef.current !== hasSelection) {
        hasSelectionRef.current = hasSelection
        forceUpdate(n => n + 1)
      }
    }
    document.addEventListener('selectionchange', sync)
    return () => document.removeEventListener('selectionchange', sync)
  }, [])

  const getSelectedText = useCallback((): string => {
    const selection = window.getSelection()
    return selection?.toString().trim() ?? ''
  }, [])

  const handleAddAsContext = useCallback(() => {
    const text = getSelectedText()
    if (!text) return
    addComposerTextAttachment(text, messageId)
  }, [getSelectedText, messageId])

  const handlePasteAsText = useCallback(() => {
    const text = getSelectedText()
    if (!text) return
    const quoted = text
      .split('\n')
      .map(line => `> ${line}`)
      .join('\n')
    requestComposerInsert(quoted + '\n\n', { mode: 'block' })
  }, [getSelectedText])

  // No selection → render children directly so the browser's native
  // context menu (Copy, Select All, etc.) works as expected.
  if (!hasSelectionRef.current) {
    return <>{children}</>
  }

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={handleAddAsContext}>Add as context</ContextMenuItem>
        <ContextMenuItem onSelect={handlePasteAsText}>Paste as text</ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}
