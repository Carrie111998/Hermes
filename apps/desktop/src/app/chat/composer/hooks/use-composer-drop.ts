import { type DragEvent as ReactDragEvent, useRef, useState } from 'react'

import { requestComposerInsert } from '@/app/chat/composer/focus'
import { triggerHaptic } from '@/lib/haptics'

import { extractDroppedFiles, HERMES_PATHS_MIME, partitionDroppedFiles } from '../../hooks/use-composer-actions'
import { dragHasAttachments, droppedFileInlineRefs, type InlineRefInput } from '../inline-refs'
import type { ChatBarProps } from '../types'

interface UseComposerDropArgs {
  cwd: ChatBarProps['cwd']
  insertInlineRefs: (refs: InlineRefInput[]) => boolean
  onAttachDroppedItems: ChatBarProps['onAttachDroppedItems']
  requestMainFocus: () => void
}

/**
 * Drag-and-drop attachment engine. Splits drops by origin: in-app drags
 * (project tree / gutter) stay inline `@file:`/`@line:` refs the gateway
 * resolves directly; OS/Finder drops (absolute local paths a remote gateway
 * can't read, image bytes vision needs) route through the upload pipeline.
 * Off the keystroke path; consumes `insertInlineRefs` + the attach handler.
 */
export function useComposerDrop({
  cwd,
  insertInlineRefs,
  onAttachDroppedItems,
  requestMainFocus
}: UseComposerDropArgs) {
  const [dragActive, setDragActive] = useState(false)
  const dragDepthRef = useRef(0)

  const resetDragState = () => {
    dragDepthRef.current = 0
    setDragActive(false)
  }

  const handleDragEnter = (event: ReactDragEvent<HTMLFormElement>) => {
    if (!onAttachDroppedItems && !event.dataTransfer.types.includes('text/plain')) {
      return
    }

    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !event.dataTransfer.types.includes('text/plain')) {
      return
    }

    event.preventDefault()
    dragDepthRef.current += 1

    if (!dragActive) {
      setDragActive(true)
    }
  }

  const handleDragOver = (event: ReactDragEvent<HTMLFormElement>) => {
    if (!onAttachDroppedItems && !event.dataTransfer.types.includes('text/plain')) {
      return
    }

    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !event.dataTransfer.types.includes('text/plain')) {
      return
    }

    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }

  const handleDragLeave = (event: ReactDragEvent<HTMLFormElement>) => {
    if (!onAttachDroppedItems) {
      return
    }

    event.preventDefault()
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)

    if (dragDepthRef.current === 0) {
      setDragActive(false)
    }
  }

  const handleDrop = (event: ReactDragEvent<HTMLFormElement>) => {
    if (!onAttachDroppedItems && !event.dataTransfer.types.includes('text/plain')) {
      return
    }

    event.preventDefault()
    resetDragState()

    const candidates = extractDroppedFiles(event.dataTransfer)

    if (candidates.length > 0) {
      // In-app drags (project tree / gutter) are workspace-relative paths the
      // gateway resolves directly, so they stay inline @file:/@line: refs. OS
      // drops are absolute local paths a remote gateway can't read (and images
      // need byte upload for vision), so route them through the upload pipeline.
      const { inAppRefs, osDrops } = partitionDroppedFiles(candidates)
      const refs = droppedFileInlineRefs(inAppRefs, cwd)

      if (refs.length && insertInlineRefs(refs)) {
        triggerHaptic('selection')
      }

      if (osDrops.length) {
        void Promise.resolve(onAttachDroppedItems!(osDrops)).then(attached => {
          if (attached) {
            triggerHaptic('selection')
            requestMainFocus()
          }
        })
      }

      return
    }

    // Text drop: selected message text dragged into the composer.
    // Insert as a quoted block (same format as "Paste as text").
    const text = event.dataTransfer.getData('text/plain').trim()

    if (text) {
      requestComposerInsert(text)
      triggerHaptic('selection')
    }
  }

  const handleInputDragOver = (event: ReactDragEvent<HTMLDivElement>) => {
    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !event.dataTransfer.types.includes('text/plain')) {
      return
    }

    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = 'copy'
  }

  const handleInputDrop = (event: ReactDragEvent<HTMLDivElement>) => {
    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !event.dataTransfer.types.includes('text/plain')) {
      return
    }

    const candidates = extractDroppedFiles(event.dataTransfer)

    if (candidates.length > 0) {
      event.preventDefault()
      event.stopPropagation()
      resetDragState()

      // Dropping straight onto the text box used to inline-ref *every* file —
      // including OS/Finder drops, whose absolute local path a remote gateway
      // can't read and whose image bytes never reached vision. Split by origin:
      // in-app drags stay inline refs; OS drops go through the upload pipeline.
      // (When no upload handler is wired, fall back to inline refs for all.)
      const attach = onAttachDroppedItems
      const { inAppRefs, osDrops } = partitionDroppedFiles(candidates)
      const refs = droppedFileInlineRefs(attach ? inAppRefs : candidates, cwd)

      if (refs.length && insertInlineRefs(refs)) {
        triggerHaptic('selection')
      }

      if (attach && osDrops.length) {
        void Promise.resolve(attach(osDrops)).then(attached => {
          if (attached) {
            triggerHaptic('selection')
            requestMainFocus()
          }
        })
      }

      return
    }

    // Text drop onto the input area.
    const text = event.dataTransfer.getData('text/plain').trim()

    if (text) {
      event.preventDefault()
      event.stopPropagation()
      resetDragState()
      requestComposerInsert(text)
      triggerHaptic('selection')
    }
  }

  return {
    dragActive,
    handleDragEnter,
    handleDragLeave,
    handleDragOver,
    handleDrop,
    handleInputDragOver,
    handleInputDrop
  }
}
