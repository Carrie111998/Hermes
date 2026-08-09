/**
 * Pure helpers for native BrowserWindow translucency.
 *
 * Omitting the `opacity` option at zero is behaviorally important on Windows:
 * Electron's SetOpacity path marks the HWND as layered even when the value is
 * exactly 1, and that layered composition mode cannot be removed from the live
 * window by calling setOpacity(1). A fully opaque cold start should therefore
 * never enter that path.
 */

function opacityForTranslucency(intensity: number): number {
  // Floor at 0.3 so the most translucent setting remains usable.
  return 1 - (intensity / 100) * 0.7
}

function initialWindowTranslucencyOptions(intensity: number, isWindows: boolean): { opacity?: number } {
  return isWindows && intensity === 0 ? {} : { opacity: opacityForTranslucency(intensity) }
}

export { initialWindowTranslucencyOptions, opacityForTranslucency }
