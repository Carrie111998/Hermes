import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'

import { requestComposerInsert } from '@/app/chat/composer/focus'
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuSeparator, ContextMenuTrigger } from '@/components/ui/context-menu'
import { writeClipboardText } from '@/components/ui/copy-button'
import { useI18n } from '@/i18n'
import { addComposerTextAttachment } from '@/store/composer'

interface MessageContextMenuProps {
  children: ReactNode
  /** The message element to attach the context menu to. */
  messageId?: string
}

/** Shared right-click context menu for message blocks (user + assistant).
 *  When text is selected, the standard Copy / Select All actions stay,
 *  with "Add as context" (stages a chip) and "Paste as text" (inserts
 *  quoted text into the composer) added below — an addition, not a
 *  replacement. When no text is selected, the native context menu is
 *  preserved (and the user-message reaction picker keeps its own
 *  right-click behavior). */
export function MessageContextMenu({ children, messageId }: MessageContextMenuProps) {
  const { t } = useI18n()
  const hasSelectionRef = useRef(false)
  const selectionAnchorRef = useRef<Element | null>(null)
  const [, forceUpdate] = useState(0)

  // Keep the ref in sync with the live selection so the render decision
  // (ContextMenu wrapper vs plain children) is always correct by the time
  // the user right-clicks. selectionchange fires on drag-end, click-away,
  // and programmatic clears — every path that changes the selection.
  // Both ref writes are DOM-selection mirrors, not atom mirrors: the
  // listener is the ONLY source of truth, so the rule is waived here.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    const sync = () => {
      const selection = window.getSelection()
      const hasSelection = Boolean(selection && !selection.isCollapsed && selection.toString().trim().length > 0)

      if (hasSelection !== hasSelectionRef.current) {
        hasSelectionRef.current = hasSelection
        forceUpdate(n => n + 1)
      }

      // Capture the anchor while the selection is alive — "Select All"
      // scopes itself to the message body the user highlighted, and the
      // DOM anchor can outlive the live selection once the menu takes focus.
      if (selection?.anchorNode) {
        const anchor = selection.anchorNode
        selectionAnchorRef.current = anchor.nodeType === 1 ? (anchor as Element) : anchor.parentElement
      }
    }

    document.addEventListener('selectionchange', sync)

    return () => document.removeEventListener('selectionchange', sync)
  }, [])

  const getSelectedText = useCallback((): string => {
    const selection = window.getSelection()

    return selection?.toString().trim() ?? ''
  }, [])

  const handleCopy = useCallback(() => {
    const text = getSelectedText()

    if (!text) {
      return
    }

    void writeClipboardText(text)
  }, [getSelectedText])

  const handleSelectAll = useCallback(() => {
    const selection = window.getSelection()
    const anchor = selectionAnchorRef.current

    if (!selection || !anchor) {
      return
    }

    // User bubbles carry the message text in `.composer-human-message`;
    // assistant content sits under the aui content slot.
    const container =
      anchor.closest<HTMLElement>('.composer-human-message') ??
      anchor.closest<HTMLElement>('[data-slot="aui_assistant-message-content"]')

    if (!container) {
      return
    }

    const range = document.createRange()
    range.selectNodeContents(container)
    selection.removeAllRanges()
    selection.addRange(range)
  }, [])

  const handleAddAsContext = useCallback(() => {
    const text = getSelectedText()

    if (!text) {
      return
    }

    addComposerTextAttachment(text, messageId)
  }, [getSelectedText, messageId])

  const handlePasteAsText = useCallback(() => {
    const text = getSelectedText()

    if (!text) {
      return
    }

    const quoted = text
      .split('\n')
      .map(line => `> ${line}`)
      .join('\n')

    requestComposerInsert(quoted + '\n\n', { mode: 'block' })
  }, [getSelectedText])

  // No selection → render children directly so the browser's native
  // context menu (Copy, Select All, etc.) works as expected — and the
  // user-message reaction picker keeps its own right-click handler.
  if (!hasSelectionRef.current) {
    return <>{children}</>
  }

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={handleCopy}>{t.common.copy}</ContextMenuItem>
        <ContextMenuItem onSelect={handleSelectAll}>{t.common.selectAll}</ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={handleAddAsContext}>Add as context</ContextMenuItem>
        <ContextMenuItem onSelect={handlePasteAsText}>Paste as text</ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}
