import { beforeEach, describe, expect, it } from 'vitest'

import {
  $terminalNerdFontEnabled,
  setTerminalNerdFontEnabled,
  TERMINAL_NERD_FONT_STORAGE_KEY,
  terminalFontFamily
} from './terminal-font'

describe('terminal font preference', () => {
  beforeEach(() => {
    window.localStorage.clear()
    setTerminalNerdFontEnabled(false)
  })

  it('keeps the bundled terminal font as the default', () => {
    const family = terminalFontFamily(false)

    expect(family.startsWith("'JetBrains Mono'")).toBe(true)
    expect(family).not.toContain('Nerd Font')
  })

  it('adds common installed Nerd Font families without removing the fallback', () => {
    setTerminalNerdFontEnabled(true)

    const family = terminalFontFamily($terminalNerdFontEnabled.get())

    expect(family).toContain("'FiraCode Nerd Font Mono'")
    expect(family).toContain("'Symbols Nerd Font Mono'")
    expect(family).toContain("'JetBrains Mono'")
    expect(window.localStorage.getItem(TERMINAL_NERD_FONT_STORAGE_KEY)).toBe('true')
  })
})
