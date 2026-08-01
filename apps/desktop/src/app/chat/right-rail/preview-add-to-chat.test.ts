import { afterEach, describe, expect, it } from 'vitest'

import {
  lineSelectionFromOffsets,
  lineSelectionFromSelectedText,
  previewOwnsAddSelectionShortcut,
  previewSelectionFileLabel,
  readHostTextSelection,
  retainPreviewAddShortcutClaim,
  sourceLineSelectionRef
} from './preview-add-to-chat'

afterEach(() => {
  window.getSelection()?.removeAllRanges()
  document.body.replaceChildren()
})

describe('sourceLineSelectionRef', () => {
  it('builds a single-line @line ref relative to cwd', () => {
    expect(sourceLineSelectionRef('/repo/src/app.ts', { end: 12, start: 12 }, '/repo')).toBe('@line:src/app.ts:12')
  })

  it('builds an inclusive range when end is past start', () => {
    expect(sourceLineSelectionRef('/repo/a.py', { end: 20, start: 10 }, '/repo')).toBe('@line:a.py:10-20')
  })

  it('quotes values that contain spaces', () => {
    expect(sourceLineSelectionRef('/repo/my file.ts', { end: 3, start: 3 }, '/repo')).toBe('@line:`my file.ts:3`')
  })
})

describe('previewSelectionFileLabel', () => {
  it('returns the basename for posix and windows paths', () => {
    expect(previewSelectionFileLabel('/tmp/notes.md')).toBe('notes.md')
    expect(previewSelectionFileLabel('C:\\work\\notes.md')).toBe('notes.md')
  })
})

describe('lineSelectionFromOffsets', () => {
  it('maps character offsets to 1-based inclusive lines', () => {
    const text = 'one\ntwo\nthree\n'

    expect(lineSelectionFromOffsets(text, 0, 3)).toEqual({ end: 1, start: 1 })
    expect(lineSelectionFromOffsets(text, 4, 7)).toEqual({ end: 2, start: 2 })
    expect(lineSelectionFromOffsets(text, 0, 11)).toEqual({ end: 3, start: 1 })
  })
})

describe('lineSelectionFromSelectedText', () => {
  it('finds the line span for a unique selection', () => {
    const text = 'alpha\nbeta\ngamma\n'

    expect(lineSelectionFromSelectedText(text, 'beta')).toEqual({ end: 2, start: 2 })
    expect(lineSelectionFromSelectedText(text, 'alpha\nbeta')).toEqual({ end: 2, start: 1 })
  })

  it('prefers the occurrence nearest preferOffset when duplicated', () => {
    const text = 'x\nrepeat\ny\nrepeat\nz\n'

    expect(lineSelectionFromSelectedText(text, 'repeat', 0)).toEqual({ end: 2, start: 2 })
    expect(lineSelectionFromSelectedText(text, 'repeat', 12)).toEqual({ end: 4, start: 4 })
  })
})

describe('previewOwnsAddSelectionShortcut', () => {
  it('tracks nested retain/release so the terminal can defer Cmd/Ctrl+L', () => {
    expect(previewOwnsAddSelectionShortcut()).toBe(false)

    const releaseA = retainPreviewAddShortcutClaim()
    expect(previewOwnsAddSelectionShortcut()).toBe(true)

    const releaseB = retainPreviewAddShortcutClaim()
    releaseA()
    expect(previewOwnsAddSelectionShortcut()).toBe(true)

    releaseB()
    expect(previewOwnsAddSelectionShortcut()).toBe(false)
  })
})

describe('readHostTextSelection', () => {
  it('returns null when nothing is selected inside the host', () => {
    const host = document.createElement('div')
    document.body.appendChild(host)

    expect(readHostTextSelection(host)).toBeNull()
  })

  it('reads a live selection that belongs to the host', () => {
    const host = document.createElement('div')
    const span = document.createElement('span')
    span.textContent = 'quote me please'
    host.appendChild(span)
    document.body.appendChild(host)

    const range = document.createRange()
    range.selectNodeContents(span)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)

    expect(readHostTextSelection(host)?.text).toBe('quote me please')
  })

  it('ignores selections outside the host', () => {
    const host = document.createElement('div')
    const outsider = document.createElement('span')
    outsider.textContent = 'outside'
    document.body.append(host, outsider)

    const range = document.createRange()
    range.selectNodeContents(outsider)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)

    expect(readHostTextSelection(host)).toBeNull()
  })
})
