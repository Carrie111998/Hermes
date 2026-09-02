import { describe, expect, it } from 'vitest'

import { isKnownWord } from './dictionary'
import { suggest } from './suggestions'

const DICT = new Set([
  'a',
  'and',
  'be',
  'better',
  'big',
  'book',
  'box',
  'boxed',
  'brown',
  'city',
  'dog',
  'eat',
  'fast',
  'fox',
  'go',
  'happy',
  'jump',
  'jumps',
  'nice',
  'over',
  'quick',
  'receive',
  'run',
  'separate',
  'the',
  'try',
  'urn',
  'very'
])

describe('renderer spell-check engine', () => {
  it('does not re-suggest a correctly spelled word', async () => {
    for (const word of ['quick', 'brown', 'fox', 'box', 'city', 'receive']) {
      const got = await suggest(word, DICT)
      expect(got).not.toContain(word)
    }
  })

  it('suggests the classic transposition typo first', async () => {
    const got = await suggest('teh', DICT)
    expect(got[0]).toBe('the')
  })

  it('suggests quick for quikc', async () => {
    const got = await suggest('quikc', DICT)
    expect(got[0]).toBe('quick')
  })

  it('suggests jumps for juumps (deletion)', async () => {
    const got = await suggest('juumps', DICT)
    expect(got).toContain('jumps')
  })

  it('suggests separate for seperate', async () => {
    const got = await suggest('seperate', DICT)
    expect(got).toContain('separate')
  })

  it('caps suggestions at five', async () => {
    const got = await suggest('teh', DICT)
    expect(got.length).toBeLessThanOrEqual(5)
  })

  it('recognizes inflected forms of a stem', () => {
    // boxes -> box, running -> run, happier -> happy, faster -> fast,
    // cities -> city (pins the -ies/-s inflection path)
    expect(isKnownWord('boxes', DICT)).toBe(true)
    expect(isKnownWord('cities', DICT)).toBe(true)
    expect(isKnownWord('running', DICT)).toBe(true)
    expect(isKnownWord('happier', DICT)).toBe(true)
    expect(isKnownWord('faster', DICT)).toBe(true)
    expect(isKnownWord('dogg', DICT)).toBe(false)
    expect(isKnownWord('thee', DICT)).toBe(false)
    expect(isKnownWord('gohh', DICT)).toBe(false)
  })
})
