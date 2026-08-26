/**
 * Renderer-side spell-check for the context menu.
 *
 * Replaces the broken Chromium `context-menu` spell-check facts path: on
 * Linux, Chromium reports `misspelledWord: ""` for `contenteditable` fields
 * (the desktop composer is one), so the native bridge never fires and the
 * right-click menu shows no spell-check section at all. Here we compute the
 * word under the click directly from the DOM and generate suggestions offline.
 */

import { refreshMisspellMarks } from '@/lib/spellcheck/marker'

import { addUserWord, getDictionary, getUserWords, isKnownWord } from './dictionary'
import type { SpellcheckContext } from './store'
import { matchCase, suggest } from './suggestions'

const WORD_CHAR = /[A-Za-z''’]/

/** Where the click word lives, kept so the "replace" action can find it
 *  again after the menu closes (the menu's focus trap steals the caret). */
export interface SpellAnchor {
  editable: HTMLElement
  /** For input/textarea: character offsets into `.value`. */
  start: number
  end: number
  /** For contenteditable: the text node the word lives in. */
  textNode?: Text | null
}

function isWordChar(ch: string | undefined): boolean {
  return ch !== undefined && WORD_CHAR.test(ch)
}

function isDigit(ch: string | undefined): boolean {
  return ch !== undefined && ch >= '0' && ch <= '9'
}

/** Word + indices around a caret index inside a plain string. */
function expandAt(value: string, caret: number): { word: string; start: number; end: number } | null {
  const c = Math.min(Math.max(caret, 0), value.length)
  let start = c

  while (start > 0 && isWordChar(value[start - 1])) {
    start--
  }

  let end = c

  while (end < value.length && isWordChar(value[end])) {
    end++
  }

  if (end <= start) {
    return null
  }

  // Reject words adjacent to digits — parity with the composer marker
  // tokenizer: "v2" or "abc123" should not yield a suggestion word.
  if (isDigit(value[start - 1]) || isDigit(value[end])) {
    return null
  }

  return { word: value.slice(start, end), start, end }
}

function rangeAtPoint(x: number, y: number): Range | null {
  const doc = document

  if (typeof doc.caretRangeFromPoint === 'function') {
    return (doc.caretRangeFromPoint(x, y) as Range | null) ?? null
  }

  if (typeof doc.caretPositionFromPoint === 'function') {
    const pos = doc.caretPositionFromPoint(x, y)

    if (!pos) {
      return null
    }

    const range = doc.createRange()
    range.setStart(pos.offsetNode, pos.offset)

    return range
  }

  return null
}

/** The word under the click, for any of the editables the app surfaces. */
export function wordAtPoint(x: number, y: number, editable: HTMLElement): { word: string; anchor: SpellAnchor } | null {
  // Plain form fields: caret index into the value.
  if (editable instanceof HTMLInputElement || editable instanceof HTMLTextAreaElement) {
    const caret = editable.selectionStart ?? editable.value.length
    const hit = expandAt(editable.value, caret)

    if (!hit) {
      return null
    }

    return { word: hit.word, anchor: { editable, start: hit.start, end: hit.end } }
  }

  // contenteditable: hit-test the clicked point to a text node, then expand.
  const range = rangeAtPoint(x, y)

  if (!range) {
    return null
  }

  const node = range.startContainer

  if (node.nodeType !== Node.TEXT_NODE) {
    return null
  }

  const textNode = node as Text

  if (!editable.contains(textNode)) {
    return null
  }

  const hit = expandAt(textNode.data, range.startOffset)

  if (!hit) {
    return null
  }

  return { word: hit.word, anchor: { editable, start: hit.start, end: hit.end, textNode } }
}

/**
 * Compute the spell-check context for a right-click in an editable:
 * the clicked word, and — when it is not in the dictionary — suggestions.
 * Returns null when there is nothing to show (no word, correct word,
 * digits only, or no suggestions found).
 */
export async function computeSpellcheck(
  x: number,
  y: number,
  editable: HTMLElement
): Promise<SpellcheckContext | null> {
  const found = wordAtPoint(x, y, editable)

  if (!found) {
    return null
  }

  const { word, anchor } = found

  if (/^[0-9]+$/.test(word)) {
    return null
  }

  const lower = word.toLowerCase()
  const [dict, userWords] = await Promise.all([getDictionary(), Promise.resolve(getUserWords())])

  if (!dict) {
    // Dictionary failed to load — nothing to suggest against.
    return null
  }

  if (lower.length < 2 || userWords.has(lower) || isKnownWord(lower, dict)) {
    return null
  }

  const suggestions = await suggest(lower, dict)

  // Return the payload even with zero suggestions: the menu still needs the
  // "Add to dictionary" action for a misspelled word we couldn't suggest a
  // fix for (gibberish gets no edit-distance hits, but must be addable).
  return {
    misspelledWord: word,
    suggestions: suggestions.map(s => matchCase(s, word)),
    anchor
  }
}

/** Replace the clicked misspelled word with a suggestion, in the DOM. */
export function replaceWord(word: string, anchor: SpellcheckContext['anchor'] | undefined): void {
  if (!anchor) {
    // Fallback: let Chromium replace its last-reported misspelling, if any.
    void window.hermesDesktop?.contextMenuSpellcheck?.({ kind: 'replace', word } as never)

    return
  }

  const editable = anchor.editable

  if (editable instanceof HTMLInputElement || editable instanceof HTMLTextAreaElement) {
    editable.focus()
    const start = Math.min(anchor.start, editable.value.length)
    const end = Math.min(anchor.end, editable.value.length)
    const typed = editable.value.slice(start, end)
    editable.setRangeText(matchCase(word, typed || 'x'), start, end, 'select')
    editable.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: word }))

    return
  }

  if (anchor.textNode) {
    try {
      const node = anchor.textNode
      const start = Math.min(anchor.start, node.data.length)
      const end = Math.min(anchor.end, node.data.length)
      const typed = node.data.slice(start, end)
      const range = document.createRange()
      range.setStart(node, start)
      range.setEnd(node, end)

      const selection = window.getSelection()
      selection?.removeAllRanges()
      selection?.addRange(range)

      // execCommand fires the real input event, so the composer's React
      // binding and the app's IME pipeline stay consistent.
      document.execCommand('insertText', false, matchCase(word, typed))

      return
    } catch {
      // node may have been removed by a re-render — fall through
    }
  }

  void window.hermesDesktop?.contextMenuSpellcheck?.({ kind: 'replace', word } as never)
}

/** "Add to dictionary": persist locally AND teach Chromium's spellchecker,
 *  then immediately refresh the visible markers so the red line clears
 *  without waiting for the next keystroke. */
export function addToDictionary(word: string, editable: HTMLElement | null): void {
  addUserWord(word)
  editable?.focus()

  if (editable) {
    void refreshMisspellMarks(editable)
  }

  void window.hermesDesktop?.contextMenuSpellcheck?.({ kind: 'add', word } as never)
}
