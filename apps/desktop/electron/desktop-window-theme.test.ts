import { describe, expect, it } from 'vitest'

import { createDesktopWindowTheme } from './desktop-window-theme'

function makeTheme() {
  const writes: Array<{ path: string; text: string }> = []
  const nativeTheme = { shouldUseDarkColors: false, themeSource: '' }
  const fs = {
    mkdirSync: () => undefined,
    readFileSync: () => {
      throw new Error('missing')
    },
    writeFileSync: (filePath: string, text: string) => writes.push({ path: filePath, text })
  }
  const theme = createDesktopWindowTheme({
    DARWIN_MAJOR: 0,
    GLASS_SUPPORTED: false,
    IS_MAC: false,
    IS_WINDOWS: false,
    IS_WSL: false,
    TITLEBAR_HEIGHT: 34,
    app: { getPath: () => 'C:/profile' },
    backgroundMaterialFor: () => 'mica',
    defaultTranslucencyState: () => ({ mode: 'clear', intensity: 0, fade: 0 }),
    fs,
    glassActive: () => false,
    nativeTheme,
    normalizeTranslucency: value => value,
    opacityNeedsSetting: () => false,
    path: {
      dirname: (value: string) => value.split('/').slice(0, -1).join('/'),
      join: (...parts: string[]) => parts.join('/')
    },
    rememberLog: () => undefined,
    vibrancyForTranslucency: () => 'sidebar',
    windowBackingOptions: (_state, color: string) => ({ backgroundColor: color }),
    windowOpacityFor: () => 1,
    windowOpacityOptions: () => ({})
  })

  return { nativeTheme, theme, writes }
}

describe('desktop window theme seam', () => {
  it('owns the persisted theme/translucency state and preserves the legacy names', () => {
    const { nativeTheme, theme, writes } = makeTheme()

    expect(nativeTheme.themeSource).toBe('system')
    expect(theme.THEME_SOURCES).toEqual(new Set(['dark', 'light', 'system']))
    expect(theme.readPersistedThemeSource).toBeTypeOf('function')
    expect(theme.writePersistedThemeSource).toBeTypeOf('function')
    expect(theme.readPersistedTranslucency).toBeTypeOf('function')
    expect(theme.writePersistedTranslucency).toBeTypeOf('function')

    theme.writePersistedThemeSource('dark')
    theme.writePersistedTranslucency({ mode: 'clear', intensity: 20, fade: 0 })

    expect(writes).toEqual([
      { path: 'C:/profile/native-theme.json', text: '{\n  "themeSource": "dark"\n}' },
      { path: 'C:/profile/translucency.json', text: '{\n  "mode": "clear",\n  "intensity": 20,\n  "fade": 0\n}' }
    ])
  })

  it('applies the same native window seam through the returned original-namespace bindings', () => {
    const { theme } = makeTheme()
    const calls: string[] = []
    const win = {
      getOpacity: () => 1,
      isDestroyed: () => false,
      setBackgroundColor: (color: string) => calls.push(`background:${color}`),
      setOpacity: () => calls.push('opacity'),
      setTitleBarOverlay: () => calls.push('overlay')
    }

    theme.translucencyBackedWindows.add(win)
    theme.applyWindowTranslucency(win)
    theme.applyTitleBarOverlay(win)

    expect(calls).toEqual(['background:#f7f7f7', 'overlay'])
    expect(theme.getWindowBackgroundColor()).toBe('#f7f7f7')

    theme.setRendererTitleBarTheme({ background: '#123456', foreground: '#abcdef' })
    expect(theme.getWindowBackgroundColor()).toBe('#123456')
    expect(theme.getTitleBarOverlayOptions()).toMatchObject({ symbolColor: '#abcdef' })
  })
})
