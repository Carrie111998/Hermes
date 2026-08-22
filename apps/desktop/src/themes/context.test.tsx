import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $pluginDiscoveryReady, markPluginDiscoveryReady } from '@/contrib/plugins-store'
import { registry } from '@/contrib/registry'

import { __resetBackendSkinSync, ingestBackendSkin } from './backend-sync'
import { skinPref, ThemeProvider, useTheme } from './context'
import { DEFAULT_SKIN_NAME, everforestTheme } from './presets'
import { THEMES_AREA } from './user-themes'

// The live-authoring loop: Hermes writes/edits one skin file and every surface
// repaints. An in-place edit keeps the NAME — only the palette moves.
const bloomberg = (foreground: string) => ({
  name: 'bloomberg',
  colors: { background: '#000000', ui_text: foreground, ui_accent: '#ff8000' }
})

const cssVar = (name: string) => window.document.documentElement.style.getPropertyValue(name)

let observedThemeName = ''

function ThemeNameProbe() {
  observedThemeName = useTheme().themeName

  return null
}

const renderThemeNameProbe = () =>
  render(
    <ThemeProvider>
      <ThemeNameProbe />
    </ThemeProvider>
  )

describe('ThemeProvider ← backend skin sync', () => {
  beforeEach(() => {
    window.localStorage.clear()
    __resetBackendSkinSync()
    $pluginDiscoveryReady.set(false)
    observedThemeName = ''
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

  it('repairs an unresolved stored skin after the backend registry is ready', () => {
    skinPref.assign('default', 'typo-skin')
    renderThemeNameProbe()

    // Boot preserves the unresolved name while backend themes are still absent.
    expect(observedThemeName).toBe('typo-skin')

    act(() => ingestBackendSkin({ name: 'default', colors: {} }, { apply: false }))

    // The backend alone is not enough: a persisted theme may still arrive from
    // the initial disk-plugin scan.
    expect(observedThemeName).toBe('typo-skin')

    act(() => markPluginDiscoveryReady())

    expect(observedThemeName).toBe(DEFAULT_SKIN_NAME)
    expect(skinPref.resolve('default')).toBe(DEFAULT_SKIN_NAME)
  })

  it('keeps a stored custom skin when the backend registers it', () => {
    skinPref.assign('default', 'bloomberg')
    renderThemeNameProbe()

    act(() => {
      ingestBackendSkin(bloomberg('#ff9f0a'), { apply: false })
      markPluginDiscoveryReady()
    })

    expect(observedThemeName).toBe('bloomberg')
    expect(skinPref.resolve('default')).toBe('bloomberg')
  })

  it('waits for the initial plugin scan before repairing an unresolved skin', () => {
    skinPref.assign('default', 'plugin-theme')
    const view = renderThemeNameProbe()

    act(() => ingestBackendSkin({ name: 'default', colors: {} }, { apply: false }))
    expect(observedThemeName).toBe('plugin-theme')

    let dispose = () => {}

    act(() => {
      dispose = registry.register({
        area: THEMES_AREA,
        data: { ...everforestTheme, label: 'Plugin theme', name: 'plugin-theme' },
        id: 'plugin-theme'
      })
      markPluginDiscoveryReady()
    })

    expect(observedThemeName).toBe('plugin-theme')
    expect(skinPref.resolve('default')).toBe('plugin-theme')

    view.unmount()
    dispose()
  })
})

describe('ThemeProvider highlight preview', () => {
  beforeEach(() => {
    window.localStorage.clear()
    __resetBackendSkinSync()
    $pluginDiscoveryReady.set(false)
    observedThemeName = ''
  })

  afterEach(cleanup)

  // Read the live context so the tests drive the real provider, not a mock.
  let ctx: ReturnType<typeof useTheme>

  function Probe() {
    ctx = useTheme()

    return null
  }

  const renderProbe = () =>
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>
    )

  it('paints the previewed theme without persisting it', () => {
    renderProbe()

    const committed = ctx.themeName

    act(() => ctx.previewTheme('everforest', 'dark'))

    expect(cssVar('--theme-foreground')).toBe(everforestTheme.darkColors!.foreground)
    // The commit surface does not change. The context name and the stored
    // preference keep their values.
    expect(ctx.themeName).toBe(committed)
    expect(skinPref.resolve('default')).toBe(committed)
  })

  it('clearThemePreview repaints the committed appearance', () => {
    renderProbe()

    act(() => ctx.previewTheme('everforest', 'dark'))
    expect(cssVar('--theme-foreground')).toBe(everforestTheme.darkColors!.foreground)

    act(() => ctx.clearThemePreview())
    expect(cssVar('--theme-foreground')).not.toBe(everforestTheme.darkColors!.foreground)
  })

  it('a commit replaces the preview and persists', () => {
    renderProbe()

    act(() => ctx.previewTheme('everforest', 'dark'))
    act(() => ctx.setTheme('mono'))

    expect(ctx.themeName).toBe('mono')
    expect(skinPref.resolve('default')).toBe('mono')
    expect(cssVar('--theme-foreground')).not.toBe(everforestTheme.darkColors!.foreground)
  })

  it('ignores a preview of an unknown theme', () => {
    renderProbe()

    const painted = cssVar('--theme-foreground')

    act(() => ctx.previewTheme('does-not-exist', 'dark'))
    expect(cssVar('--theme-foreground')).toBe(painted)
  })
})
