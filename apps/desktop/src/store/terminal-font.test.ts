import { beforeEach, describe, expect, it, vi } from 'vitest'

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

  it('restores the persisted preference and writes later changes', async () => {
    window.localStorage.setItem(TERMINAL_NERD_FONT_STORAGE_KEY, 'true')
    vi.resetModules()

    const reloaded = await import('./terminal-font')

    expect(reloaded.$terminalNerdFontEnabled.get()).toBe(true)

    reloaded.setTerminalNerdFontEnabled(false)

    expect(window.localStorage.getItem(TERMINAL_NERD_FONT_STORAGE_KEY)).toBe('false')
  })
})
