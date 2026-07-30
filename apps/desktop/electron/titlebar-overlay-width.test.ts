import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  MACOS_TAHOE_DARWIN_MAJOR,
  macTitleBarOverlayHeight,
  nativeOverlayWidth,
  OVERLAY_FALLBACK_WIDTH,
  shouldSuppressWcoOnWsl
} from './titlebar-overlay-width'

// This static reservation is only the pre-layout FALLBACK. Once laid out the
// renderer reads the exact width from navigator.windowControlsOverlay
// (use-window-controls-overlay-width.ts) and uses these values only when the WCO
// API is unavailable.

test('Windows reserves the overlay fallback width', () => {
  assert.equal(nativeOverlayWidth({ isWindows: true }), OVERLAY_FALLBACK_WIDTH)
})

test('WSLg paints the same WCO, so it reserves the same fallback width', () => {
  // The original bug: WSL fell through to 0, so the right tools sat under the
  // controls and the title overran into them.
  assert.equal(nativeOverlayWidth({ isWsl: true }), OVERLAY_FALLBACK_WIDTH)
})

test('plain Linux paints the WCO too, so it reserves the fallback width', () => {
  // Regression #53185: re-enabling the overlay on plain Linux (KDE/GNOME)
  // without reserving its width left the native min/max/close buttons painting
  // on top of the app's right-edge titlebar tools.
  assert.equal(nativeOverlayWidth({ isWindows: false, isWsl: false }), OVERLAY_FALLBACK_WIDTH)
  assert.equal(nativeOverlayWidth(), OVERLAY_FALLBACK_WIDTH)
  assert.equal(nativeOverlayWidth({}), OVERLAY_FALLBACK_WIDTH)
})

test('macOS uses traffic lights, not a WCO overlay, so it reserves nothing', () => {
  assert.equal(nativeOverlayWidth({ isMac: true }), 0)
})

test('the fallback width is a sane positive pixel value', () => {
  assert.ok(Number.isInteger(OVERLAY_FALLBACK_WIDTH) && OVERLAY_FALLBACK_WIDTH > 0)
})

test('pre-Tahoe keeps the full titlebar overlay height', () => {
  assert.equal(macTitleBarOverlayHeight({ darwinMajor: MACOS_TAHOE_DARWIN_MAJOR - 1, titlebarHeight: 34 }), 34)
})

test('Tahoe (Darwin 25+) drops the overlay height to 0 to avoid electron#49183', () => {
  assert.equal(macTitleBarOverlayHeight({ darwinMajor: MACOS_TAHOE_DARWIN_MAJOR, titlebarHeight: 34 }), 0)
  assert.equal(macTitleBarOverlayHeight({ darwinMajor: MACOS_TAHOE_DARWIN_MAJOR + 1, titlebarHeight: 34 }), 0)
})

test('macTitleBarOverlayHeight tolerates missing args (unknown platform → 0)', () => {
  assert.equal(macTitleBarOverlayHeight(), 0)
})

test('WSL requests the overlay by default (#57614)', () => {
  // The bug: WSLg was assumed to paint RDP-host min/max/close, so the overlay
  // was suppressed — but common WSLg builds paint nothing, leaving a dead
  // frameless window. The overlay must NOT be suppressed by default.
  assert.equal(shouldSuppressWcoOnWsl({ isWsl: true }), false)
})

test('HERMES_DESKTOP_WSL_NO_WCO=1 restores the old WSL suppression', () => {
  assert.equal(shouldSuppressWcoOnWsl({ isWsl: true, wslNoWcoEnv: '1' }), true)
  // Any other value is not an opt-out
  assert.equal(shouldSuppressWcoOnWsl({ isWsl: true, wslNoWcoEnv: 'true' }), false)
  assert.equal(shouldSuppressWcoOnWsl({ isWsl: true, wslNoWcoEnv: '0' }), false)
})

test('native Windows never suppresses, even with the env set', () => {
  assert.equal(shouldSuppressWcoOnWsl({ isWindows: true, isWsl: true, wslNoWcoEnv: '1' }), false)
})

test('plain Linux never suppresses', () => {
  assert.equal(shouldSuppressWcoOnWsl({}), false)
  assert.equal(shouldSuppressWcoOnWsl({ wslNoWcoEnv: '1' }), false)
})
