import { describe, expect, it } from 'vitest'

import { shouldCollapsePaste } from '../lib/pasteCollapse.js'

const LINES = 5
const CHARS = 2000
const DATA = 500

// A whitespace-free single-line data dump — the exact shape that flooded the
// user's composer (~1-2KB of synthesis unit/timing stream, below CHARS).
const DATA_BLOB = Array.from({ length: 80 }, (_, i) => `1${i}M35;${i + 8};`).join('')

describe('shouldCollapsePaste', () => {
  it('collapses multi-line pastes at the line threshold', () => {
    expect(shouldCollapsePaste({ text: 'a\nb\nc\nd\ne', lineCount: 5, linesThreshold: LINES, charsThreshold: CHARS })).toBe(true)
    expect(shouldCollapsePaste({ text: 'a\nb\nc\nd', lineCount: 4, linesThreshold: LINES, charsThreshold: CHARS })).toBe(false)
  })

  it('collapses long single-line pastes at the char threshold', () => {
    const long = 'x'.repeat(CHARS)
    expect(shouldCollapsePaste({ text: long, lineCount: 1, linesThreshold: LINES, charsThreshold: CHARS })).toBe(true)
  })

  it('collapses whitespace-free data dumps below the char threshold', () => {
    expect(DATA_BLOB.length).toBeGreaterThan(DATA)
    expect(DATA_BLOB.length).toBeLessThan(CHARS)
    expect(/\s/.test(DATA_BLOB)).toBe(false)

    expect(
      shouldCollapsePaste({
        text: DATA_BLOB,
        lineCount: 1,
        linesThreshold: LINES,
        charsThreshold: CHARS
      })
    ).toBe(true)
  })

  it('keeps normal prose below the data threshold raw', () => {
    const prose = 'the quick brown fox jumps over the lazy dog '.repeat(12)
    expect(prose.length).toBeGreaterThan(DATA)
    expect(
      shouldCollapsePaste({
        text: prose,
        lineCount: 1,
        linesThreshold: LINES,
        charsThreshold: CHARS
      })
    ).toBe(false)
  })

  it('keeps short whitespace-free single lines raw', () => {
    expect(
      shouldCollapsePaste({
        text: '1M35;9;2M35;11',
        lineCount: 1,
        linesThreshold: LINES,
        charsThreshold: CHARS
      })
    ).toBe(false)
  })

  it('honours dataCharsThreshold = 0 (guard disabled)', () => {
    expect(
      shouldCollapsePaste({
        text: DATA_BLOB,
        lineCount: 1,
        linesThreshold: LINES,
        charsThreshold: CHARS,
        dataCharsThreshold: 0
      })
    ).toBe(false)
  })

  it('honours a custom data threshold', () => {
    const shortBlob = '1M35;9;2M35;11;3M35;12'
    expect(
      shouldCollapsePaste({
        text: shortBlob,
        lineCount: 1,
        linesThreshold: LINES,
        charsThreshold: CHARS,
        dataCharsThreshold: 10
      })
    ).toBe(true)
  })
})
