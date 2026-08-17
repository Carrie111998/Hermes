import { describe, expect, it } from 'vitest'

import { parseRichMarkup } from '../banner.js'

// Banner art is authored once and parsed by TWO renderers: Rich (classic CLI)
// and this one (TUI). The vocabulary is Rich's, so anything Rich accepts has to
// survive here — a name this parser does not know is not "no color", it is the
// whole line leaking its markup into the art.
describe('parseRichMarkup', () => {
  it('keeps hex colors untouched', () => {
    expect(parseRichMarkup('[#B8860B]art[/]')).toEqual([['#B8860B', 'art']])
  })

  it('maps Rich palette names onto terminal-owned ansi specs', () => {
    expect(parseRichMarkup('[bright_black]art[/]')).toEqual([['ansi:blackBright', 'art']])
    expect(parseRichMarkup('[yellow]art[/]')).toEqual([['ansi:yellow', 'art']])
    expect(parseRichMarkup('[bold yellow]art[/]')).toEqual([['ansi:yellow', 'art']])
  })

  it('never leaves a recognised tag in the rendered text', () => {
    for (const tag of ['#B8860B', 'yellow', 'bold yellow', 'bright_cyan', 'dim white']) {
      const [[, text]] = parseRichMarkup(`[${tag}]▀▀▀[/]`)

      expect(text).toBe('▀▀▀')
      expect(text).not.toContain('[')
    }
  })

  it('distinguishes a base slot from its bright sibling', () => {
    const [[base]] = parseRichMarkup('[white]a[/]')
    const [[bright]] = parseRichMarkup('[bright_white]a[/]')

    expect(base).not.toBe(bright)
  })

  it('falls back to the terminal foreground for an unknown name', () => {
    // A typo must degrade to a readable line, not print the tag.
    const [[color, text]] = parseRichMarkup('[chartreuse]art[/]')

    expect(color).toBe('')
    expect(text).toBe('art')
  })

  it('emits every art line for multi-line markup', () => {
    const art = '[bright_black]one[/]\n[yellow]two[/]'

    expect(parseRichMarkup(art)).toEqual([
      ['ansi:blackBright', 'one'],
      ['ansi:yellow', 'two']
    ])
  })
})
