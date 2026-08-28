import { describe, expect, it } from 'vitest'

import { InputEvent } from './input-event.js'
import { INITIAL_STATE, parseMultipleKeypresses, type ParsedKey } from '../parse-keypress.js'

/**
 * Under Ghostty the TUI pushes xterm modifyOtherKeys level 2, so every Shift
 * combo arrives as ESC[27;2;<codepoint>~ (and as ESC[<codepoint>;2u under the
 * Kitty protocol). parse-keypress's keycodeToName() lowercases printable
 * codepoints for stable binding lookups, which used to mean Shift+T reached
 * text input as "t" — no capital letters at all while the mode was active.
 */
function inputFor(sequence: string): { input: string; shift: boolean; ctrl: boolean } {
  const [items] = parseMultipleKeypresses(INITIAL_STATE, sequence)
  expect(items).toHaveLength(1)
  const event = new InputEvent(items[0] as ParsedKey)

  return { input: event.input, shift: event.key.shift, ctrl: event.key.ctrl }
}

describe('shifted letters under modifyOtherKeys / CSI u', () => {
  it('uppercases Shift+letter from modifyOtherKeys', () => {
    expect(inputFor('\x1b[27;2;84~')).toEqual({ input: 'T', shift: true, ctrl: false })
    expect(inputFor('\x1b[27;2;72~')).toEqual({ input: 'H', shift: true, ctrl: false })
  })

  it('uppercases Shift+letter from Kitty CSI u', () => {
    expect(inputFor('\x1b[84;2u')).toEqual({ input: 'T', shift: true, ctrl: false })
  })

  it('passes shifted symbols through unchanged', () => {
    expect(inputFor('\x1b[27;2;126~').input).toBe('~')
    expect(inputFor('\x1b[27;2;33~').input).toBe('!')
  })

  it('leaves Ctrl combos lowercase so bindings still match', () => {
    expect(inputFor('\x1b[27;5;99~')).toEqual({ input: 'c', shift: false, ctrl: true })
  })

  it('does not uppercase Ctrl+Shift combos', () => {
    expect(inputFor('\x1b[27;6;99~').input).toBe('c')
  })

  it('leaves Shift+Enter with empty input', () => {
    expect(inputFor('\x1b[27;2;13~').input).toBe('')
  })

  it('never leaks the raw escape sequence as text', () => {
    for (const seq of ['\x1b[27;2;84~', '\x1b[27;2;126~', '\x1b[84;2u']) {
      expect(inputFor(seq).input).not.toContain('[27;')
      expect(inputFor(seq).input).not.toContain('\x1b')
    }
  })
})
