import { describe, expect, it } from 'vitest'

import { isPasteHotkey } from '../components/textInput.js'

const noMod = { ctrl: false, meta: false }
const none = ''

describe('isPasteHotkey (#88192: Ghostty/Wayland Ctrl+V)', () => {
  it('matches the raw legacy Ctrl+V byte', () => {
    expect(isPasteHotkey('\x16', noMod, '')).toBe(true)
  })

  it('matches the raw Alt+V escape forms', () => {
    expect(isPasteHotkey('\x1bv', noMod, '')).toBe(true)
    expect(isPasteHotkey('\x1bV', noMod, '')).toBe(true)
  })

  it('matches semantic Ctrl+V (kitty CSI-u / xterm modifyOtherKeys shape)', () => {
    // Ghostty sends Ctrl+V as CSI 27;5;118~ or CSI 118;5u; Ink reports the
    // parsed key with ctrl=true and inp='v' — raw \x16 never arrives.
    expect(isPasteHotkey(none, { ctrl: true, meta: false }, 'v', false)).toBe(true)
    expect(isPasteHotkey(none, { ctrl: true, meta: false }, 'V', false)).toBe(true)
  })

  it('matches Ctrl+Shift+V (the 6-modifier CSI-u variant keeps the ctrl bit)', () => {
    expect(isPasteHotkey(none, { ctrl: true, meta: false }, 'v', false)).toBe(true)
  })

  it('matches Cmd+V on macOS (meta/super action modifier)', () => {
    expect(isPasteHotkey(none, { ctrl: false, meta: true }, 'v', true)).toBe(true)
    expect(isPasteHotkey(none, { ctrl: false, meta: false, super: true }, 'v', true)).toBe(true)
  })

  it('does not fire for a bare v, other letters, or unrelated modifiers', () => {
    expect(isPasteHotkey(none, noMod, 'v', false)).toBe(false)
    expect(isPasteHotkey(none, { ctrl: true, meta: false }, 'c', false)).toBe(false)
    expect(isPasteHotkey(none, { ctrl: false, meta: true }, 'x', true)).toBe(false)
    // macOS rejects plain Ctrl+V as paste (that is the isMacActionFallback
    // domain) — the semantic branch requires the Mac action modifier.
    expect(isPasteHotkey(none, { ctrl: true, meta: false }, 'v', true)).toBe(false)
  })
})
