/**
 * Live misspelling markers for the desktop composer.
 *
 * Chromium's spelling underline is a thin native wavy line painted through
 * the renderer's `::spelling-error` decoration. In the Hermes desktop app
 * that decoration is all but invisible: the chat window can run at partial
 * native opacity (translucency), and via remote displays (RDP/VNC) thin low-
 * saturation marks wash out entirely. Fixing both is out of reach from CSS
 * alone, so we render our OWN high-contrast marker — a normal DOM `<span>`
 * carrying a thick underline plus a tinted background chip. It paints through
 * any compositing path, translucency or remote display included.
 *
 * The marking pass is idempotent and cheap: it re-runs after edits (debounced),
 * skips IME composition, and preserves the caret. It never changes the text,
 * so the app's `composerPlainText` + sanitize pipeline is unaffected.
 */

import { getDictionary, getUserWords, isKnownWord } from '@/app/context-menu/dictionary'

/** Tokenizer: words only (letters + apostrophes), never adjacent to digits,
 *  so "v2", "3d", "abc123" are left alone. */
const WORD_RE = /(?<![0-9])[A-Za-z][A-Za-z''’]*(?![0-9])/g

/** Our marker attribute. The composer's DOM normalizer leaves it alone, and
 *  composerPlainText reads through it transparently. */
export const MISS_ATTR = 'data-hermes-miss'

let dictPromise: Promise<ReadonlySet<string> | null> | null = null

function markerClass(): string {
  // Class matches the .hermes-miss rule in src/styles.css.
  return 'hermes-miss'
}

/** Remove our markers, restoring each wrapped word to a bare text node. */
function removeMarkers(root: HTMLElement): void {
  const markers = root.querySelectorAll(`[${MISS_ATTR}]`)

  for (const el of Array.from(markers)) {
    el.replaceWith(...Array.from(el.childNodes))
  }
}

function isEditableTextNode(node: Node): node is Text {
  if (node.nodeType !== Node.TEXT_NODE) {
    return false
  }

  if (!(node.textContent || '').trim()) {
    return false
  }

  const parent = node.parentElement

  if (!parent) {
    return false
  }

  // Never touch reference chips or non-editable embeds.
  if (parent.closest('[contenteditable="false"], [data-ref-text]')) {
    return false
  }

  return true
}

/** Wrap misspelled words inside a text node in marker spans. Returns the
 *  number of words marked (used only for debugging/tests). `caretLocal` is
 *  the caret's offset within THIS text node; the token under it (the word
 *  being typed) is never wrapped — live underlining mid-word is what made
 *  typing look broken before. */
function markTextNode(node: Text, known: (word: string) => boolean, caretLocal: number | null): number {
  const text = node.data
  const owner = node.parentElement

  if (!owner) {
    return 0
  }

  let count = 0
  const fragments: (string | Node)[] = []
  let last = 0
  WORD_RE.lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = WORD_RE.exec(text)) !== null) {
    const start = match.index
    const end = match.index + match[0].length
    const word = match[0].toLowerCase()
    const underCaret = caretLocal !== null && start <= caretLocal && caretLocal <= end

    if (!known(word) && !underCaret) {
      if (match.index > last) {
        fragments.push(text.slice(last, match.index))
      }

      const span = document.createElement('span')
      span.setAttribute(MISS_ATTR, 'true')
      span.className = markerClass()
      span.textContent = match[0]
      fragments.push(span)
      last = match.index + match[0].length
      count++
    }
  }

  if (count === 0) {
    return 0
  }

  if (last < text.length) {
    fragments.push(text.slice(last))
  }

  // Replace only THIS text node — siblings must never be touched (the editor
  // can hold several adjacent text runs, e.g. after a previous unwrap).
  node.replaceWith(...fragments)

  return count
}

/** All non-known words in `text`, in order — mirrors markTextNode's tokenizer.
 *  `caretChar` (absolute plain-text offset) skips the token the caret sits in,
 *  so the word the user is STILL typing is never marked. */
function collectMisspelled(text: string, known: (word: string) => boolean, caretChar: number | null): string[] {
  const out: string[] = []
  WORD_RE.lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = WORD_RE.exec(text)) !== null) {
    const start = match.index
    const end = match.index + match[0].length
    const underCaret = caretChar !== null && start <= caretChar && caretChar <= end

    if (!known(match[0].toLowerCase()) && !underCaret) {
      out.push(match[0])
    }
  }

  return out
}

/** Absolute character position of a node+offset inside the editor's plain text. */
function charOffsetIn(editor: HTMLElement, node: Node, offset: number): number {
  if (!editor.contains(node)) {
    return -1
  }

  let abs = 0
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT)
  let n: Node | null

  while ((n = walker.nextNode())) {
    const len = (n.textContent ?? '').length

    if (n === node) {
      return abs + Math.min(offset, len)
    }

    abs += len
  }

  return abs
}

/** Collapse the caret to `offset` (plain-text characters) inside the editor. */
function setCaretIn(editor: HTMLElement, offset: number): void {
  const len = editor.textContent?.length ?? 0
  let remaining = Math.max(0, Math.min(offset, len))
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT)
  let n: Node | null

  while ((n = walker.nextNode())) {
    const full = n.textContent ?? ''

    if (remaining <= full.length) {
      const range = document.createRange()
      range.setStart(n, remaining)
      range.collapse(true)
      const sel = window.getSelection()
      sel?.removeAllRanges()
      sel?.addRange(range)

      return
    }

    remaining -= full.length
  }

  // No text to place a caret in — collapse at the editor start.
  const range = document.createRange()
  range.setStart(editor, 0)
  range.collapse(true)
  const sel = window.getSelection()
  sel?.removeAllRanges()
  sel?.addRange(range)
}

export interface MarkOptions {
  /** Debounce window in ms. 0 = run immediately (sync). */
  debounce?: number
}

/**
 * Refresh misspelling markers in a contenteditable.
 *
 * No-op when the existing marks are already correct — that path leaves the
 * DOM and the caret completely untouched, so typing never gets disturbed.
 * When the marks DO change, the caret is captured as a plain-text character
 * offset BEFORE the unwrap/rewrap and restored AFTER it, so the insertion
 * point stays exactly where the user left it. This is what keeps the
 * composer from "typing backwards" (a previous version dropped the caret,
 * which fell to the start and prepended every subsequent keystroke).
 */
export async function refreshMisspellMarks(editor: HTMLElement, options: MarkOptions = {}): Promise<number> {
  if (!editor.isConnected) {
    return 0
  }

  dictPromise ??= getDictionary()
  const dict = await dictPromise

  // Dictionary failed to load — don't underline EVERYTHING, just back off.
  if (!dict) {
    return 0
  }

  const userWords = getUserWords()
  const known = (word: string): boolean => userWords.has(word) || isKnownWord(word, dict)

  const sel = window.getSelection()
  let caret = -1

  if (sel && sel.rangeCount > 0 && sel.anchorNode?.nodeType === Node.TEXT_NODE && editor.contains(sel.anchorNode)) {
    caret = charOffsetIn(editor, sel.anchorNode, sel.anchorOffset)
  }

  const current = [...editor.querySelectorAll(`[${MISS_ATTR}]`)].map(m => m.textContent ?? '')
  const expected = collectMisspelled(editor.textContent ?? '', known, caret >= 0 ? caret : null)
  const equal = current.length === expected.length && current.every((w, i) => w === expected[i])

  if (equal) {
    return 0
  }

  removeMarkers(editor)

  // Collect FIRST (with their plain-text start offsets), mutate after:
  // replacing nodes while a live TreeWalker walks them loses trailing runs.
  const runs: Array<{ node: Text; start: number }> = []
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT)
  let cursor = 0

  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const txt = node as Text
    const len = (txt.textContent ?? '').length

    if (isEditableTextNode(txt)) {
      runs.push({ node: txt, start: cursor })
    }

    cursor += len
  }

  let marked = 0

  for (const { node, start } of runs) {
    let caretLocal: number | null = null

    if (caret >= 0) {
      caretLocal = caret - start

      if (caretLocal < 0 || caretLocal > (node.textContent ?? '').length) {
        caretLocal = null
      }
    }

    marked += markTextNode(node, known, caretLocal)
  }

  if (caret >= 0) {
    setCaretIn(editor, caret)
  }

  return marked
}

/** Debounced scheduler used by the composer. `skipIf` guards against running
 *  a DOM-mutating pass mid-IME-composition (the composer's composingRef). */
export function scheduleMisspellMarks(
  editor: HTMLElement | null,
  options: { ms?: number; skipIf?: () => boolean } = {}
): void {
  if (!editor) {
    return
  }

  const { ms = 180, skipIf } = options
  pendingEditor = editor

  if (pendingTimer !== null) {
    window.clearTimeout(pendingTimer)
  }

  pendingTimer = window.setTimeout(() => {
    pendingTimer = null
    const el = pendingEditor
    pendingEditor = null

    if (el && el.isConnected && !(skipIf && skipIf())) {
      void refreshMisspellMarks(el)
    }
  }, ms)
}

let pendingTimer: number | null = null
let pendingEditor: HTMLElement | null = null

export function cancelMisspellMarks(): void {
  if (pendingTimer !== null) {
    window.clearTimeout(pendingTimer)
    pendingTimer = null
  }

  pendingEditor = null
}
