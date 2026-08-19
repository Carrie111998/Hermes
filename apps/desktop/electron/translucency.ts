/**
 * Main-process side of window translucency.
 *
 * The mapping itself (modes, clamping, the clear-mode opacity ramp) lives in
 * apps/shared so the renderer and the main process cannot drift; this module
 * adds only what needs a BrowserWindow to mean anything.
 *
 * The import is relative rather than `@hermes/shared/translucency`: the
 * electron bundle is built by esbuild with no tsconfig path resolution (see
 * scripts/bundle-electron-main.mjs), so a bare specifier would typecheck and
 * then fail to bundle.
 */

import {
  backgroundMaterialFor,
  glassActive,
  type TranslucencyState,
  vibrancyFor,
  windowOpacityFor,
  type WindowsBackgroundMaterial
} from '../../shared/src/translucency'

export {
  backgroundMaterialFor,
  clampIntensity,
  DEFAULT_GLASS_MATERIAL,
  DEFAULT_GLASS_SCOPE,
  GLASS_MATERIALS,
  GLASS_SCOPES,
  glassActive,
  type GlassMaterial,
  glassMaterialForPicker,
  glassMaterialsFor,
  glassSupportedOn,
  glassSurfaceKeep,
  hudFrostFor,
  hydrateTranslucencyState,
  normalizeMaterial,
  normalizeMode,
  normalizeScope,
  normalizeState,
  TRANSLUCENCY_CURVE,
  TRANSLUCENCY_MAX,
  TRANSLUCENCY_MIN,
  TRANSLUCENCY_OPACITY_FLOOR,
  type TranslucencyState,
  translucencySupportedOn,
  vibrancyFor,
  windowOpacityFor,
  WINDOWS_BACKGROUND_MATERIALS,
  WINDOWS_GLASS_MIN_BUILD,
  type WindowsBackgroundMaterial
} from '../../shared/src/translucency'

/**
 * BrowserWindow constructor options for a chat window's backing, given the
 * translucency state at creation time.
 *
 * Glass active → OMIT `backgroundColor` entirely: on a `vibrancy` window the
 * NSVisualEffectView then shows through a transparent page from the first
 * frame. Passing an alpha color instead does NOT work — Electron only supports
 * constructor alpha with `transparent: true`, and `#00000000` on a normal
 * window is quietly treated as opaque.
 *
 * Glass inactive → the opaque themed backing (anti-flash paint before the
 * renderer's first paint, and what clear mode fades against).
 *
 * A runtime `setBackgroundColor` swap (see applyWindowTranslucency in main)
 * only settles reliably on a window that has been compositing for a while —
 * measured on macOS 26 / Electron 40, swaps issued during roughly the first
 * seconds of a fresh process were lost, including from 'ready-to-show' and
 * 'did-finish-load' — so creation must not rely on a post-creation fixup.
 */
export function windowBackingOptions(state: TranslucencyState, themedColor: string): { backgroundColor?: string } {
  return glassActive(state) ? {} : { backgroundColor: themedColor }
}

/**
 * Electron only honours `transparent` at BrowserWindow construction. DWM Snap
 * and FancyZones treat a transparent window as outside normal frame hit-testing,
 * so a glass-capable Windows chat window must stay opaque until glass is
 * actually on. Turning glass on later therefore needs a recreate — the live
 * `setBackgroundMaterial` path cannot add transparency after the fact
 * (electron#49443).
 */
export function windowsChatWindowTransparent(
  platform: string,
  glassSupported: boolean,
  state: TranslucencyState
): boolean {
  return platform === 'win32' && glassSupported && glassActive(state)
}

export function chatWindowNeedsSurfaceRecreate(
  previous: TranslucencyState,
  next: TranslucencyState,
  platform: string,
  glassSupported: boolean
): boolean {
  return (
    windowsChatWindowTransparent(platform, glassSupported, previous) !==
    windowsChatWindowTransparent(platform, glassSupported, next)
  )
}

export type ChatWindowSurfaceOptions = {
  vibrancy?: ReturnType<typeof vibrancyFor>
  visualEffectState?: 'active'
  transparent?: true
  backgroundMaterial?: WindowsBackgroundMaterial
  opacity: number
  backgroundColor?: string
}

export function chatWindowSurfaceOptions(input: {
  platform: string
  glassSupported: boolean
  state: TranslucencyState
  themedColor: string
}): ChatWindowSurfaceOptions {
  const { platform, glassSupported, state, themedColor } = input
  const isMac = platform === 'darwin'
  const isWindows = platform === 'win32'
  const transparent = windowsChatWindowTransparent(platform, glassSupported, state)

  return {
    vibrancy: isMac ? vibrancyFor(state) : undefined,
    visualEffectState: isMac ? 'active' : undefined,
    ...(transparent ? { transparent: true as const } : {}),
    backgroundMaterial: isWindows && glassSupported ? backgroundMaterialFor(state) : undefined,
    opacity: windowOpacityFor(state),
    ...windowBackingOptions(state, themedColor)
  }
}
