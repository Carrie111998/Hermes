import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { __resetBackendSkinSync, ingestBackendSkin } from './backend-sync'
import { ThemeProvider } from './context'

// The live-authoring loop: Hermes writes/edits one skin file and every surface
// repaints. An in-place edit keeps the NAME — only the palette moves.
const bloomberg = (foreground: string) => ({
  name: 'bloomberg',
  colors: { background: '#000000', ui_text: foreground, ui_accent: '#ff8000' }
})

const cssVar = (name: string) => window.document.documentElement.style.getPropertyValue(name)

describe('ThemeProvider ← backend skin sync', () => {
  beforeEach(() => {
    window.localStorage.clear()
    __resetBackendSkinSync()
  })

  afterEach(cleanup)

  it('applies an activated backend skin', () => {
    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    act(() => ingestBackendSkin(bloomberg('#ff9f0a'), { apply: true }))

    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')
    expect(cssVar('--theme-background-seed')).toBe('#000000')
  })

  it('repaints an in-place edit of the ACTIVE skin (same name, new palette)', () => {
    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    act(() => ingestBackendSkin(bloomberg('#ff9f0a'), { apply: true }))
    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')

    // Recolor the same skin file. The same-name apply guard correctly no-ops
    // (protects manual desktop picks), so the repaint must come from the
    // registry update reaching the active theme derivation.
    act(() => ingestBackendSkin(bloomberg('#ff2d95'), { apply: true }))
    expect(cssVar('--theme-foreground')).toBe('#ff2d95')
  })

  it('does not repaint an edit to an INACTIVE skin', () => {
    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    act(() => ingestBackendSkin(bloomberg('#ff9f0a'), { apply: true }))

    // A different skin registered without apply (e.g. seeded on reconnect)
    // must not touch the painted theme.
    act(() =>
      ingestBackendSkin({ name: 'forest', colors: { background: '#001100', ui_text: '#66ff66' } }, { apply: false })
    )
    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')
  })

  it('restores a persisted BACKEND skin after the gateway.ready seed (no apply)', () => {
    // A backend skin (e.g. `catppuccin` from $HERMES_HOME/skins) cannot
    // resolve at boot — the gateway hasn't announced it yet — so the user's
    // persisted choice would previously fall back to the default and stay
    // there. The gateway.ready seed (apply:false) must re-resolve it.
    window.localStorage.setItem('hermes-desktop-theme-v2', 'bloomberg')

    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    // Before the backend announces anything, the persisted skin is unresolvable:
    // paint is on the default.
    expect(cssVar('--theme-foreground')).not.toBe('#ff9f0a')

    // gateway.ready: seed the backend skin into the registry WITHOUT applying.
    act(() => ingestBackendSkin(bloomberg('#ff9f0a'), { apply: false }))

    // The persisted pref now resolves — the user's choice must repaint even
    // though the seed itself never applies.
    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')
  })
})
