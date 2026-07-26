export const OVERLAY_FALLBACK_WIDTH = 144

/**
 * Whether the WSL special case should SUPPRESS the Window Controls Overlay.
 *
 * WSLg was assumed to paint min/max/close via the RDP host's own frame, so the
 * overlay was suppressed on WSL — but in practice common WSLg builds (e.g.
 * 1.0.65) paint NO controls at all, leaving a dead frameless window with no
 * way to minimize, maximize, or close it (#57614). The overlay is therefore
 * requested on WSL by default; HERMES_DESKTOP_WSL_NO_WCO=1 restores the old
 * suppression if some WSLg build ever double-paints controls.
 *
 * Native Windows never suppresses — only Linux-on-WSL is the special case.
 *
 * @param {{ isWindows?: boolean, isWsl?: boolean, wslNoWcoEnv?: string }} opts
 */
export function shouldSuppressWcoOnWsl({ isWindows = false, isWsl = false, wslNoWcoEnv = '' } = {}) {
  return !isWindows && isWsl && wslNoWcoEnv === '1'
}

/**
 * Static pre-layout reservation (px) for the right-side native window-controls
 * overlay (min/max/close). Only a FALLBACK — once laid out the renderer reads
 * the exact width from navigator.windowControlsOverlay
 * (use-window-controls-overlay-width.ts) and uses this value only when the WCO
 * API is unavailable.
 *
 * macOS uses traffic lights positioned via trafficLightPosition, not a WCO
 * overlay, so it reserves nothing here. Every other desktop platform now paints
 * the Electron overlay (Windows, WSLg, and plain Linux KDE/GNOME), so they all
 * reserve the fallback width — the split is simply mac vs. not.
 *
 * @param {{ isMac?: boolean }} opts
 */
export function nativeOverlayWidth({ isWindows = false, isWsl = false, isMac = false } = {}) {
  if (isMac) {
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
