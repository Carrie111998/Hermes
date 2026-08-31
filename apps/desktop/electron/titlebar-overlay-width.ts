export const OVERLAY_FALLBACK_WIDTH = 144

/**
 * Static pre-layout reservation (px) for the right-side native window-controls
 * overlay (min/max/close). Only a FALLBACK — once laid out the renderer reads
 * the exact width from navigator.windowControlsOverlay
 * (use-window-controls-overlay-width.ts) and uses this value only when the WCO
 * API is unavailable.
 *
 * macOS uses traffic lights positioned via trafficLightPosition, not a WCO
 * overlay, so it reserves nothing here. Every other desktop platform paints the
 * Electron overlay when native controls are enabled. Hyprland's system default
 * disables them because its frameless Wayland windows have no server-side
 * decoration to replace them with.
 *
 * @param {{ isWindows?: boolean, isWsl?: boolean, isMac?: boolean, nativeWindowControls?: boolean }} opts
 */
export function nativeOverlayWidth({
  isWindows = false,
  isWsl = false,
  isMac = false,
  nativeWindowControls = true
} = {}) {
  if (isMac || !nativeWindowControls) {
    return 0
  }

  return OVERLAY_FALLBACK_WIDTH
}

// macOS Tahoe ships as Darwin 25 (Sequoia is 24); the Darwin number is truthful,
// unlike the product version which macOS reports as 16 or 26 depending on the
// build SDK.
export const MACOS_TAHOE_DARWIN_MAJOR = 25

/**
 * Height (px) to pass to `titleBarOverlay` on macOS. Tahoe (Darwin 25+)
 * miscalculates the native traffic-light position when the overlay carries a
 * nonzero height (electron#49183), shoving the lights into the left titlebar
 * tools. Return 0 there so `setWindowButtonPosition` lands them at the configured
 * inset; the renderer paints its own drag strips, so nothing is lost. Pre-Tahoe
 * keeps the full titlebar height, byte-identical.
 *
 * @param {{ darwinMajor?: number, titlebarHeight?: number }} opts
 */
export function macTitleBarOverlayHeight({ darwinMajor = 0, titlebarHeight = 0 } = {}) {
  return darwinMajor >= MACOS_TAHOE_DARWIN_MAJOR ? 0 : titlebarHeight
}
