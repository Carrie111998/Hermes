import { beforeEach, describe, expect, it, vi } from 'vitest'

// Mock the dictionary so the 77k-word module never loads in the test graph.
vi.mock('@/app/context-menu/dictionary', () => ({
  getDictionary: () =>
    Promise.resolve(new Set(['the', 'quick', 'brown', 'fox', 'jumps', 'over', 'lazy', 'dog', 'running'])),
  getUserWords: () => new Set(['hermes']),
  isKnownWord: (w: string, d: Set<string>) => d.has(w)
}))
vi.mock('@/app/context-menu/wordlist', () => ({ wordList: [] }))

import { MISS_ATTR, refreshMisspellMarks } from './marker'

function editorWith(text: string): HTMLElement {
  const el = document.createElement('div')
  el.setAttribute('contenteditable', 'true')
  el.textContent = text
  document.body.appendChild(el)

  return el
}

function marked(editor: HTMLElement): string[] {
  return [...editor.querySelectorAll(`[${MISS_ATTR}]`)].map(s => s.textContent ?? '')
}

describe('renderer misspelling markers', () => {
  beforeEach(() => {
    document.body.replaceChildren()
  })

  it('wraps misspelled words, leaves correct ones alone', async () => {
    const editor = editorWith('teh quick browwn foxx')
    const n = await refreshMisspellMarks(editor)
    expect(n).toBeGreaterThanOrEqual(3)
    expect(marked(editor)).toEqual(['teh', 'browwn', 'foxx'])
    expect(editor.textContent).toBe('teh quick browwn foxx')
  })

  it('is idempotent', async () => {
    const editor = editorWith('teh quikc')
    await refreshMisspellMarks(editor)
    const first = editor.innerHTML
    await refreshMisspellMarks(editor)
    expect(editor.innerHTML).toBe(first)
    expect(marked(editor).length).toBe(2)
  })

  it('skips user-dictionary words', async () => {
    const editor = editorWith('hermes rocks')
    await refreshMisspellMarks(editor)
    expect(marked(editor)).toEqual(['rocks'])
  })

  it('does not mark numbers-only or mixed tokens', async () => {
    const editor = editorWith('v2 test 1234 ä')
    await refreshMisspellMarks(editor)
    // v2 has a digit -> skipped by tokenizer; test is fine; 1234 digits-only
    expect(marked(editor)).toEqual(['test'])
  })

  it('preserves the caret position across a marking pass', async () => {
    const editor = editorWith('teh quick browwn')
    // Put the caret right after "teh" (plain-text offset 3).
    const sel = window.getSelection()!
    const range = document.createRange()
    range.setStart(editor.firstChild as Text, 3)
    range.collapse(true)
    sel.removeAllRanges()
    sel.addRange(range)

    await refreshMisspellMarks(editor)

    const anchor = sel.anchorNode as Text

    const abs = (() => {
      let acc = 0
      const w = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT)

      for (let n = w.nextNode(); n; n = w.nextNode()) {
        if (n === anchor) {
          return acc + sel.anchorOffset
        }

        acc += n.textContent?.length ?? 0
      }

      return -1
    })()

    expect(abs).toBe(3)
  })

  it('does not underline the word the caret is inside while typing', async () => {
    const editor = editorWith('the quikc brown') // mock dict knows "the" and "brown"

    // Caret inside "quikc" (offsets 4..8; place at 6).
    const sel = window.getSelection()!
    const range = document.createRange()
    range.setStart(editor.firstChild as Text, 6)
    range.collapse(true)
    sel.removeAllRanges()
    sel.addRange(range)

    await refreshMisspellMarks(editor)
    expect(marked(editor).length).toBe(0) // quikc skipped — caret is inside it

    // Caret out of the word -> it gets marked.
    const last = editor.firstChild as Text
    const r2 = document.createRange()
    r2.setStart(last, last.textContent?.length ?? 0)
    r2.collapse(true)
    sel.removeAllRanges()
    sel.addRange(r2)

    await refreshMisspellMarks(editor)
    expect(marked(editor)).toEqual(['quikc'])
  })

  it('is a no-op when marks are already correct (caret untouched)', async () => {
    const editor = editorWith('teh quick browwn')
    await refreshMisspellMarks(editor)

    // Caret placed in a NEUTRAL spot — inside the space after "quick" (one
    // char before the start of "browwn") — so no word is under the caret and
    // the marks are already exactly right. This is the true no-op case.
    const quickSpace = (() => {
      const w = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT)

      for (let n = w.nextNode(); n; n = w.nextNode()) {
        if ((n.textContent ?? '').includes('quick')) {
          return n as Text
        }
      }

      return null
    })()!

    const neutralOffset = quickSpace.textContent!.length - 1 // inside the space
    const sel = window.getSelection()!
    const range = document.createRange()
    range.setStart(quickSpace, neutralOffset)
    range.collapse(true)
    sel.removeAllRanges()
    sel.addRange(range)

    const beforeHTML = editor.innerHTML
    await refreshMisspellMarks(editor)

    expect(editor.innerHTML).toBe(beforeHTML)
    expect(sel.anchorNode).toBe(quickSpace)
    expect(sel.anchorOffset).toBe(neutralOffset)
  })
})
