import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuTrigger } from '@/components/ui/context-menu'
import { requestComposerInsert } from '@/app/chat/composer/focus'
import { addComposerTextAttachment } from '@/store/composer'
import type { ReactNode } from 'react'

interface MessageContextMenuProps {
  children: ReactNode
  /** The message element to attach the context menu to. */
  messageId?: string
}

/** Shared right-click context menu for message blocks (user + assistant).
 *  When text is selected, offers "Add as context" (stages a chip) and
 *  "Paste as text" (inserts quoted text into the composer). */
export function MessageContextMenu({ children, messageId }: MessageContextMenuProps) {
  const getSelectedText = (): string => {
    const selection = window.getSelection()
    return selection?.toString().trim() ?? ''
  }

  const handleAddAsContext = () => {
    const text = getSelectedText()
    if (!text) return
    addComposerTextAttachment(text, messageId)
  }

  const handlePasteAsText = () => {
    const text = getSelectedText()
    if (!text) return
    const quoted = text
      .split('\n')
      .map(line => `> ${line}`)
      .join('\n')
    requestComposerInsert(quoted + '\n\n', { mode: 'block' })
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
