/**
 * Native theme and window translucency orchestration extracted from
 * electron/main.ts.
 *
 * The factory keeps Electron and the existing main-process helpers explicit.
 * Its returned bindings are composed back into main.ts under their historical
 * names, while the mutable translucency state remains owned here.
 */
export function createDesktopWindowTheme(deps: Record<string, any>) {
  const {
    DARWIN_MAJOR,
    GLASS_SUPPORTED,
    IS_MAC,
    IS_WINDOWS,
    IS_WSL,
    TITLEBAR_HEIGHT,
    app,
    backgroundMaterialFor,
    defaultTranslucencyState,
    fs,
    glassActive,
    nativeTheme,
    normalizeTranslucency,
    opacityNeedsSetting,
    path,
    rememberLog,
    vibrancyForTranslucency,
    windowBackingOptions,
    windowOpacityFor,
    windowOpacityOptions,
    macTitleBarOverlayHeight
  } = deps

  const NATIVE_THEME_CONFIG_PATH = path.join(app.getPath('userData'), 'native-theme.json')
  const THEME_SOURCES = new Set(['dark', 'light', 'system'])

  function readPersistedThemeSource() {
    try {
      const parsed = JSON.parse(fs.readFileSync(NATIVE_THEME_CONFIG_PATH, 'utf8'))

      if (parsed && THEME_SOURCES.has(parsed.themeSource)) {
        return parsed.themeSource
      }
    } catch {
      // Missing / malformed → follow the OS like a fresh install.
    }

    return 'system'
  }

  function writePersistedThemeSource(mode) {
    try {
      fs.mkdirSync(path.dirname(NATIVE_THEME_CONFIG_PATH), { recursive: true })
      fs.writeFileSync(NATIVE_THEME_CONFIG_PATH, JSON.stringify({ themeSource: mode }, null, 2), 'utf8')
    } catch (error) {
      rememberLog(`[theme] write native theme failed: ${error.message}`)
    }
  }

  nativeTheme.themeSource = readPersistedThemeSource()

  const TRANSLUCENCY_CONFIG_PATH = path.join(app.getPath('userData'), 'translucency.json')

  function readPersistedTranslucency() {
    try {
      return normalizeTranslucency(JSON.parse(fs.readFileSync(TRANSLUCENCY_CONFIG_PATH, 'utf8')), GLASS_SUPPORTED)
    } catch {
      // Nothing persisted yet — a first launch. Glass ships on, so the FIRST
      // window has to be created with the glass backing already: a window born
      // opaque cannot reliably be swapped to glass afterwards (see
      // windowBackingOptions). nativeTheme is the only appearance signal main
      // has this early; the renderer's first resolved send corrects it.
      return defaultTranslucencyState(nativeTheme.shouldUseDarkColors ? 'dark' : 'light', GLASS_SUPPORTED, IS_WINDOWS)
    }
  }

  function writePersistedTranslucency(state) {
    try {
      fs.mkdirSync(path.dirname(TRANSLUCENCY_CONFIG_PATH), { recursive: true })
      fs.writeFileSync(TRANSLUCENCY_CONFIG_PATH, JSON.stringify(state, null, 2), 'utf8')
    } catch (error) {
      rememberLog(`[translucency] write failed: ${error.message}`)
    }
  }

  let translucencyState = readPersistedTranslucency()

  const translucencyBackedWindows = new WeakSet()

  function applyWindowOpacity(win) {
    const opacity = windowOpacityFor(translucencyState)

    if (typeof win.setOpacity === 'function' && opacityNeedsSetting(opacity, win.getOpacity?.() ?? 1)) {
      win.setOpacity(opacity)
    }
  }

  function applyWindowTranslucency(win, changed = { backing: true, material: true, opacity: true }) {
    if (!win || win.isDestroyed()) {
      return
    }

    try {
      // Backing swap + material are scoped to registered chat windows (see
      // translucencyBackedWindows above).
      if (translucencyBackedWindows.has(win)) {
        if (changed.backing && typeof win.setBackgroundColor === 'function') {
          win.setBackgroundColor(glassActive(translucencyState) ? '#00000000' : getWindowBackgroundColor())
        }

        if (changed.material) {
          // Glass frost level = the platform material. Animate the macOS hop so
          // a deliberate frost switch feels continuous — which only works if we
          // don't re-issue it on unrelated updates. Windows has no equivalent
          // animation option; setBackgroundMaterial is instantaneous.
          if (IS_MAC && typeof win.setVibrancy === 'function') {
            win.setVibrancy(vibrancyForTranslucency(translucencyState), { animationDuration: 150 })
          }

          if (IS_WINDOWS && GLASS_SUPPORTED && typeof win.setBackgroundMaterial === 'function') {
            win.setBackgroundMaterial(backgroundMaterialFor(translucencyState))
          }
        }
      }

      if (changed.opacity) {
        applyWindowOpacity(win)
      }
    } catch (error) {
      rememberLog(`[translucency] apply failed: ${error.message}`)
    }
  }

  function chatWindowSurfaceOptions() {
    return {
      vibrancy: IS_MAC ? vibrancyForTranslucency(translucencyState) : undefined,
      // Pin the material to its ACTIVE appearance: several NSVisualEffectView
      // materials collapse to a shared inactive look when the window blurs
      // (measured on macOS 26: sidebar, popover and under-window composited
      // pixel-identically once unfocused), which would quietly erase the
      // user's frost choice whenever they click elsewhere. Only observable
      // under glass — everywhere else the page buries the material.
      visualEffectState: IS_MAC ? ('active' as const) : undefined,
      // NOT `transparent: true` on Windows. The backdrop material already makes
      // the window translucent on its own: `IsTranslucent` answers yes off
      // `background_material_` alone, which is what gives the page its transparent
      // default backing, and `SetBackgroundMaterial` flips widget translucency
      // live, so a Clear→Glass toggle needs no recreate either way. Its one gate
      // is a frameless window, and `titleBarStyle: 'hidden'` already makes
      // `has_frame()` false here.
      //
      // What `transparent` adds on top is permanent and unwanted: it pins the
      // widget to kTranslucent for the window's whole life, so even glass-OFF
      // windows pay a DirectComposition redraw per frame (electron#39895), and it
      // opts into the documented transparent-window limits — including that a
      // RESIZABLE transparent window is unsupported and breaks (electron#48421).
      // Every chat window is resizable.
      backgroundMaterial: IS_WINDOWS && GLASS_SUPPORTED ? backgroundMaterialFor(translucencyState) : undefined,
      ...windowOpacityOptions(translucencyState),
      ...windowBackingOptions(translucencyState, getWindowBackgroundColor())
    }
  }

  function isHexColor(value) {
    return typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value)
  }

  // Background color to paint a window with BEFORE its renderer loads, so a new
  // (or reopened) window doesn't flash white/light in dark mode. Prefer the theme
  // the renderer last reported; fall back to the OS preference on first launch.
  function getWindowBackgroundColor() {
    if (rendererTitleBarTheme && isHexColor(rendererTitleBarTheme.background)) {
      return rendererTitleBarTheme.background
    }

    return nativeTheme.shouldUseDarkColors ? '#111111' : '#f7f7f7'
  }

  // Transparent WCO — renderer chrome shows through. rgba(0,0,0,0) can fall back
  // to GetFrameColor() on some Electron builds; rgba(1,0,0,0) is the escape hatch.
  const TITLEBAR_OVERLAY_COLOR = 'rgba(1, 0, 0, 0)'

  function getTitleBarOverlayOptions() {
    if (IS_MAC) {
      // Tahoe (Darwin 25+) misplaces the traffic lights when the overlay has a
      // nonzero height (electron#49183); 0 there keeps them at the configured
      // inset. See macTitleBarOverlayHeight.
      return { height: macTitleBarOverlayHeight({ darwinMajor: DARWIN_MAJOR, titlebarHeight: TITLEBAR_HEIGHT }) }
    }

    // WSLg paints WCO via the RDP host's own min/max/close, so requesting
    // an Electron overlay there just leaves a dead gap. Plain Linux (KDE,
    // GNOME) can use the native overlay — let it through.
    if (!IS_WINDOWS && IS_WSL) {
      return false
    }

    return {
      color: TITLEBAR_OVERLAY_COLOR,
      height: TITLEBAR_HEIGHT,
      symbolColor:
        rendererTitleBarTheme && isHexColor(rendererTitleBarTheme.foreground)
          ? rendererTitleBarTheme.foreground
          : nativeTheme.shouldUseDarkColors
            ? '#f7f7f7'
            : '#242424'
    }
  }

  // Push refreshed overlay options to a live window after a theme/appearance
  // change. No-op only on plain (non-WSL) Linux, where getTitleBarOverlayOptions()
  // returns false; the try/catch additionally guards builds where
  // setTitleBarOverlay isn't supported.
  function applyTitleBarOverlay(win) {
    const options = getTitleBarOverlayOptions()

    if (!options || typeof options !== 'object') {
      return
    }

    try {
      win?.setTitleBarOverlay?.(options)
    } catch {
      // Overlay not supported on this platform/build — leave the frameless
      // titlebar as-is.
    }
  }

  let rendererTitleBarTheme = null

  return {
    THEME_SOURCES,
    applyTitleBarOverlay,
    applyWindowOpacity,
    applyWindowTranslucency,
    chatWindowSurfaceOptions,
    getTitleBarOverlayOptions,
    getWindowBackgroundColor,
    getTranslucencyState: () => translucencyState,
    isHexColor,
    readPersistedThemeSource,
    readPersistedTranslucency,
    setRendererTitleBarTheme: next => {
      rendererTitleBarTheme = next
    },
    setTranslucencyState: next => {
      translucencyState = next
    },
    translucencyBackedWindows,
    writePersistedThemeSource,
    writePersistedTranslucency
  }
}
