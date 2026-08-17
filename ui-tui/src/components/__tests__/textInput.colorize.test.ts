import { describe, expect, it } from 'vitest'

import { colorizeEcho, colorizeHint } from '../textInput.js'

const ESC = '\x1b'

// The skin vocabulary this file must understand, with the SGR foreground code
// each slot maps to. Written out here because it IS the contract — the terminal
// resolves these to the user's own theme, which is the whole point.
const SLOTS: Array<[string, number]> = [
  ['black', 30],
  ['red', 31],
  ['green', 32],
  ['yellow', 33],
  ['blue', 34],
  ['magenta', 35],
  ['cyan', 36],
  ['white', 37],
  ['blackBright', 90],
  ['redBright', 91],
  ['greenBright', 92],
  ['yellowBright', 93],
  ['blueBright', 94],
  ['magentaBright', 95],
  ['cyanBright', 96],
  ['whiteBright', 97],
]

describe('colorizeHint', () => {
  it('emits truecolor for a hex skin, byte for byte', () => {
    expect(colorizeHint('hint', '#ff2d95')).toBe(`${ESC}[38;2;255;45;149mhint${ESC}[39m`)
    expect(colorizeHint('hint', '#808080')).toBe(`${ESC}[38;2;128;128;128mhint${ESC}[39m`)
  })

  it('keeps the grey fallback when there is no usable color', () => {
    const grey = `${ESC}[38;2;128;128;128mhint${ESC}[39m`

    expect(colorizeHint('hint')).toBe(grey)
    expect(colorizeHint('hint', 'lolnope')).toBe(grey)
    expect(colorizeHint('hint', '#fff')).toBe(grey)
    expect(colorizeHint('hint', 'ansi:chartreuse')).toBe(grey)
  })

  it.each(SLOTS)('maps ansi:%s to its palette code, not truecolor', (name, code) => {
    const out = colorizeHint('hint', `ansi:${name}`)

    expect(out).toBe(`${ESC}[${code}mhint${ESC}[39m`)
    expect(out).not.toContain('38;2;')
    expect(out).not.toContain('38;5;')
  })

  it('asks for the terminal default fg on an empty color', () => {
    expect(colorizeHint('hint', '')).toBe(`${ESC}[39mhint${ESC}[39m`)
  })

  it('never emits a relative attribute (dim or inverse)', () => {
    const outputs = [
      colorizeHint('hint'),
      colorizeHint('hint', ''),
      colorizeHint('hint', '#808080'),
      ...SLOTS.map(([name]) => colorizeHint('hint', `ansi:${name}`)),
    ]

    for (const out of outputs) {
      expect(out).not.toContain(`${ESC}[2m`)
      expect(out).not.toContain(`${ESC}[7m`)
    }
  })
})

describe('colorizeEcho', () => {
  it('passes through when there is nothing explicit to paint with', () => {
    expect(colorizeEcho('x')).toBe('x')
    expect(colorizeEcho('x', undefined)).toBe('x')
    expect(colorizeEcho('x', 'lolnope')).toBe('x')
    expect(colorizeEcho('x', 'ansi:chartreuse')).toBe('x')
    expect(colorizeEcho('x', '#fff')).toBe('x')
  })

  it('still carries the same explicit truecolor for hex skins', () => {
    expect(colorizeEcho('x', '#ff2d95')).toBe(`${ESC}[38;2;255;45;149mx${ESC}[39m`)
  })

  it.each(SLOTS)('carries ansi:%s as a palette code', (name, code) => {
    expect(colorizeEcho('x', `ansi:${name}`)).toBe(`${ESC}[${code}mx${ESC}[39m`)
  })

  it('agrees with colorizeHint on every color it paints', () => {
    for (const value of ['#ff2d95', ...SLOTS.map(([name]) => `ansi:${name}`)]) {
      expect(colorizeEcho('x', value)).toBe(colorizeHint('x', value))
    }
  })
})
