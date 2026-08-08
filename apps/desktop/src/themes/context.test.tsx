import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'

import { __resetBackendSkinSync, ingestBackendSkin } from './backend-sync'
import { ThemeProvider } from './context'
import { THEMES_AREA } from './user-themes'

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
})

// A contributed theme (plugin / registry-backed) only enters the registry AFTER
// boot. The boot paint and the provider's initial state resolve the persisted
// pick through `normalizeSkin`, which falls back to the default when the name
// doesn't resolve yet — so a chosen contributed theme snaps back to the default
// on every restart unless the provider re-reads the persisted pick once the
// theme registers.
describe('ThemeProvider ← late-registered contributed theme', () => {
  beforeEach(() => {
    window.localStorage.clear()
    __resetBackendSkinSync()
  })

  afterEach(cleanup)

  it('adopts a persisted contributed theme once it registers (survives restart)', () => {
    // The user previously picked the contributed theme — that choice is durable.
    window.localStorage.setItem('hermes-desktop-theme-v2', 'liquid')

    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    // Theme isn't registered yet, so boot fell back to the default (nous:
    // foreground #17171A). The persisted pick must not be lost, only deferred.
    expect(cssVar('--theme-foreground')).toBe('#17171A')

    // The plugin loads and registers its theme. The registry version bumps and
    // the provider must re-read the persisted 'liquid' and repaint it.
    let dispose!: () => void
    act(() => {
      dispose = registry.register({
        id: 'liquid',
        area: THEMES_AREA,
        data: {
          name: 'liquid',
          label: 'Liquid Glass',
          colors: { background: '#0a1120', foreground: '#f2f5fa', primary: '#0a84ff', ring: '#0a84ff', midground: '#0a84ff' },
          // darkColors present → getBaseColors returns `colors` verbatim in
          // light mode (no synthesis), so the assertion reads the literal seed.
          darkColors: { background: '#05080f', foreground: '#e8edf7', primary: '#0a84ff', ring: '#0a84ff', midground: '#0a84ff' }
        }
      })
    })

    try {
      expect(cssVar('--theme-foreground')).toBe('#f2f5fa')
      expect(cssVar('--theme-background-seed')).toBe('#0a1120')
    } finally {
      dispose()
    }
  })
})
