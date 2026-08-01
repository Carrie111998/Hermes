import { droppedFileInlineRef } from '@/app/chat/composer/inline-refs'

export interface LineSelection {
  end: number
  start: number
}

/** `@line:path:start-end` chip for a gutter or resolved text selection in the file preview. */
export function sourceLineSelectionRef(
  filePath: string,
  selection: LineSelection,
  cwd: string | null | undefined
): null | string {
  const lineEnd = selection.end > selection.start ? selection.end : undefined

  return droppedFileInlineRef({ line: selection.start, lineEnd, path: filePath }, cwd)
}

/** Basename label for preview quotes when a line range cannot be resolved. */
export function previewSelectionFileLabel(filePath: string): string {
  return filePath.split(/[\\/]/).filter(Boolean).pop() || filePath.trim()
}

function normalizeNewlines(text: string) {
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}

/** 1-based inclusive line span covering [start, end) character offsets. */
export function lineSelectionFromOffsets(fullText: string, start: number, end: number): LineSelection | null {
  const text = normalizeNewlines(fullText)

  if (!text || start < 0 || end < start) {
    return null
  }

  const clampedStart = Math.min(start, text.length)
  const clampedEnd = Math.min(Math.max(end, clampedStart + 1), text.length + 1)
  const startLine = text.slice(0, clampedStart).split('\n').length
  const endOffset = Math.max(clampedStart, clampedEnd - 1)
  const endLine = text.slice(0, endOffset).split('\n').length

  return { end: endLine, start: startLine }
}

/**
 * Resolve a free-text selection to source line numbers by locating the selected
 * string in the file. Prefers an occurrence near `preferOffset` when the needle
 * appears more than once (virtualized source views only mount a window).
 */
export function lineSelectionFromSelectedText(
  fullText: string,
  selected: string,
  preferOffset?: number
): LineSelection | null {
  const haystack = normalizeNewlines(fullText)
  const needle = normalizeNewlines(selected)

  if (!haystack || !needle.trim()) {
    return null
  }

  const candidates: number[] = []
  let from = 0

  while (from <= haystack.length) {
    const idx = haystack.indexOf(needle, from)

    if (idx < 0) {
      break
    }

    candidates.push(idx)
    from = idx + Math.max(1, needle.length)
  }

  if (candidates.length === 0) {
    const trimmed = needle.trim()

    if (trimmed === needle) {
      return null
    }

    return lineSelectionFromSelectedText(haystack, trimmed, preferOffset)
  }

  let idx = candidates[0]!

  if (preferOffset != null && candidates.length > 1) {
    idx = candidates.reduce((best, next) =>
      Math.abs(next - preferOffset) < Math.abs(best - preferOffset) ? next : best
    )
  }

  return lineSelectionFromOffsets(haystack, idx, idx + needle.length)
}

/** Live window selection that belongs to `host` (collapsed / outside → null). */
export function readHostTextSelection(host: HTMLElement): { range: Range; text: string } | null {
  const selection = window.getSelection()

  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    return null
  }

  const range = selection.getRangeAt(0)
  const anchor = range.commonAncestorContainer
  const node = anchor.nodeType === Node.TEXT_NODE ? anchor.parentNode : anchor

  if (!(node instanceof Node) || !host.contains(node)) {
    return null
  }

  const text = selection.toString()

  if (!text.trim()) {
    return null
  }

  return { range, text }
}

/**
 * When the preview frame has a claimable line/text selection, it owns ⌘/Ctrl+L.
 * The terminal's long-lived capture listener registers earlier and would otherwise
 * also insert an `@terminal:` quote from the same `window.getSelection()`.
 */
let previewAddShortcutClaims = 0

export function retainPreviewAddShortcutClaim(): () => void {
  previewAddShortcutClaims += 1

  return () => {
    previewAddShortcutClaims = Math.max(0, previewAddShortcutClaims - 1)
  }
}

export function previewOwnsAddSelectionShortcut(): boolean {
  return previewAddShortcutClaims > 0
}
