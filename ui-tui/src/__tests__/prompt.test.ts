import { describe, expect, it } from 'vitest'

import { composerPromptText } from '../lib/prompt.js'

describe('composerPromptText', () => {
  it('returns shell prompt for ! commands', () => {
    expect(composerPromptText('❯', 'coder', true)).toBe('$')
  })

  it('keeps the edit gutter stable when a profile is active', () => {
    expect(composerPromptText('❯', 'coder')).toBe('❯')
    expect(composerPromptText('❯', 'a-very-long-specialist-profile')).toBe('❯')
  })

  it('does not prefix default or custom profiles', () => {
    expect(composerPromptText('❯', 'default')).toBe('❯')
    expect(composerPromptText('❯', 'custom')).toBe('❯')
    expect(composerPromptText('❯')).toBe('❯')
  })

  it('uses a Termux-safe ASCII prompt marker in normal mode', () => {
    expect(composerPromptText('❯', 'coder', false, true, 50)).toBe('>')
  })

  it('keeps profile prefix suppressed on narrow Termux widths', () => {
    expect(composerPromptText('❯', 'upstr', false, true, 72)).toBe('>')
  })

  it('keeps the edit gutter stable on very wide Termux panes', () => {
    expect(composerPromptText('❯', 'upstr', false, true, 120)).toBe('>')
  })
})
