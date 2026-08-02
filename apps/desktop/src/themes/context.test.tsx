import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import { __resetBackendSkinSync, activateBackendSkinProfile, ingestBackendSkin } from './backend-sync'
import { ThemeProvider } from './context'
import { DEFAULT_SKIN_NAME } from './presets'
import { PROFILE_SKINS_STORAGE_KEY } from './skin-preference'

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
    $activeGatewayProfile.set('default')
  })

  afterEach(() => {
    cleanup()
    $activeGatewayProfile.set('default')
  })

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

  it('drains rapid applies to each source profile after the foreground switches', () => {
    activateBackendSkinProfile('work')
    ingestBackendSkin(bloomberg('#ff9f0a'), { apply: true, profile: 'work' })
    ingestBackendSkin({ name: 'mono', colors: {} }, { apply: true, profile: 'personal' })
    $activeGatewayProfile.set('personal')

    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    const stored = JSON.parse(window.localStorage.getItem(PROFILE_SKINS_STORAGE_KEY) ?? '{}') as Record<string, string>

    expect(stored.work).toBe('bloomberg')
    expect(stored.personal).toBe('mono')
    expect(cssVar('--theme-foreground')).not.toBe('#ff9f0a')
    expect(window.document.documentElement.dataset.hermesTheme).toBe('mono')

    act(() => $activeGatewayProfile.set('work'))

    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')
  })

  it('routes a queued reset to default without repainting the new foreground profile', () => {
    window.localStorage.setItem(PROFILE_SKINS_STORAGE_KEY, JSON.stringify({ work: 'mono' }))
    ingestBackendSkin({ name: 'default', colors: {} }, { apply: true, profile: 'work' })
    $activeGatewayProfile.set('personal')

    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    const stored = JSON.parse(window.localStorage.getItem(PROFILE_SKINS_STORAGE_KEY) ?? '{}') as Record<string, string>

    expect(stored.work).toBe(DEFAULT_SKIN_NAME)
    expect(stored.personal).toBeUndefined()
    expect(window.document.documentElement.dataset.hermesTheme).toBe(DEFAULT_SKIN_NAME)

    act(() => $activeGatewayProfile.set('work'))

    expect(window.document.documentElement.dataset.hermesTheme).toBe(DEFAULT_SKIN_NAME)
  })
})
