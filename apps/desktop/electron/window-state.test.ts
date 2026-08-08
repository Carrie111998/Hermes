/**
 * Unit tests for the pure window-state helpers. These cover the logic that
 * protects the user: garbage rejection, off-screen fallback, oversized
 * clamping, the debounce that collapses mid-drag write storms, and the
 * destroyed-window guards that keep a window torn down mid-boot from throwing
 * out of the ready payload.
 */

import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import {
  computeWindowOptions,
  debounce,
  DEFAULT_HEIGHT,
  DEFAULT_WIDTH,
  isWindowLive,
  MIN_HEIGHT,
  MIN_WIDTH,
  onScreen,
  sanitizeWindowState,
  windowButtonPosition,
  windowChromeState
} from './window-state'

// A single 1920×1080 monitor (work area trimmed for the taskbar).
const PRIMARY = [{ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }]
// A laptop panel left behind after a bigger external monitor is unplugged.
const LAPTOP = [{ workArea: { x: 0, y: 0, width: 1366, height: 728 } }]

// ─── sanitizeWindowState ───────────────────────────────────────────────────

test('sanitizeWindowState rejects missing/garbage input', () => {
  for (const bad of [
    null,
    undefined,
    'nope',
    42,
    {},
    { width: 'x', height: 800 },
    { width: NaN, height: 800 },
    { width: 1000 }
  ]) {
    assert.equal(sanitizeWindowState(bad), null)
  }
})

test('sanitizeWindowState keeps a valid full state and rounds HiDPI fractions', () => {
  assert.deepEqual(sanitizeWindowState({ x: 100.6, y: 50.2, width: 1400.4, height: 900.7, isMaximized: true }), {
    x: 101,
    y: 50,
    width: 1400,
    height: 901,
    isMaximized: true
  })
})

test('sanitizeWindowState floors size to the minimums', () => {
  const state = sanitizeWindowState({ width: 10, height: 10 })
  assert.equal(state.width, MIN_WIDTH)
  assert.equal(state.height, MIN_HEIGHT)
})

test('sanitizeWindowState drops a partial position but keeps the size', () => {
  assert.deepEqual(sanitizeWindowState({ x: 100, width: 1400, height: 900 }), {
    width: 1400,
    height: 900,
    isMaximized: false
  })
})

test('sanitizeWindowState treats isMaximized strictly', () => {
  assert.equal(sanitizeWindowState({ width: 1400, height: 900, isMaximized: 'yes' }).isMaximized, false)
})

// ─── onScreen ──────────────────────────────────────────────────────────────

test('onScreen accepts a window on the primary or a secondary display', () => {
  const dual = [...PRIMARY, { workArea: { x: 1920, y: 0, width: 2560, height: 1400 } }]
  assert.equal(onScreen({ x: 100, y: 100, width: 1220, height: 800 }, PRIMARY), true)
  assert.equal(onScreen({ x: 2200, y: 200, width: 1220, height: 800 }, dual), true)
})

test('onScreen rejects off-screen, slivers, and bad input', () => {
  assert.equal(onScreen({ x: 3000, y: 100, width: 1220, height: 800 }, PRIMARY), false) // past right edge
  assert.equal(onScreen({ x: 100, y: -900, width: 1220, height: 800 }, PRIMARY), false) // above top
  assert.equal(onScreen({ x: 1910, y: 100, width: 1220, height: 800 }, PRIMARY), false) // ~10px sliver
  assert.equal(onScreen({ x: 0, y: 0, width: 1220, height: 800 }, []), false)
  assert.equal(onScreen({ x: 0, y: 0, width: 1220, height: 800 }, null), false)
})

// ─── computeWindowOptions ──────────────────────────────────────────────────

test('computeWindowOptions falls back to defaults with no saved state', () => {
  assert.deepEqual(computeWindowOptions(null, PRIMARY), { width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT })
})

test('computeWindowOptions restores an on-screen position', () => {
  const saved = sanitizeWindowState({ x: 200, y: 150, width: 1400, height: 900 })
  assert.deepEqual(computeWindowOptions(saved, PRIMARY), { width: 1400, height: 900, x: 200, y: 150 })
})

test('computeWindowOptions keeps the size but drops an off-screen position', () => {
  const saved = sanitizeWindowState({ x: 5000, y: 150, width: 1400, height: 900 })
  assert.deepEqual(computeWindowOptions(saved, PRIMARY), { width: 1400, height: 900 })
})

test('computeWindowOptions clamps a size larger than the only display', () => {
  const saved = sanitizeWindowState({ width: 2560, height: 1440 })
  assert.deepEqual(computeWindowOptions(saved, LAPTOP), { width: 1366, height: 728 })
})

test('computeWindowOptions keeps the MIN floor on a sub-minimum display', () => {
  const tiny = [{ workArea: { x: 0, y: 0, width: 360, height: 480 } }]
  const saved = sanitizeWindowState({ width: 2000, height: 1500 })
  assert.deepEqual(computeWindowOptions(saved, tiny), { width: MIN_WIDTH, height: MIN_HEIGHT })
})

test('computeWindowOptions does not clamp when displays are unknown', () => {
  const saved = sanitizeWindowState({ width: 2560, height: 1440 })
  assert.deepEqual(computeWindowOptions(saved, []), { width: 2560, height: 1440 })
})

// ─── debounce ──────────────────────────────────────────────────────────────

test('debounce coalesces a burst into one trailing run', () => {
  vi.useFakeTimers()
  let calls = 0

  const d = debounce(() => {
    calls += 1
  }, 250)

  d()
  d()
  d()
  assert.equal(calls, 0)
  vi.advanceTimersByTime(249)
  assert.equal(calls, 0)
  vi.advanceTimersByTime(1)
  assert.equal(calls, 1)

  vi.useRealTimers()
})

test('debounce.flush runs now and cancels the pending timer', () => {
  vi.useFakeTimers()
  let calls = 0

  const d = debounce(() => {
    calls += 1
  }, 250)

  d()
  d.flush()
  assert.equal(calls, 1)
  vi.advanceTimersByTime(1000)
  assert.equal(calls, 1)

  vi.useRealTimers()
})

// ─── destroyed-window guards ───────────────────────────────────────────────

const FALLBACK_BUTTON_POSITION = { x: 24, y: 10 }

// A window that is up: every accessor answers normally.
const liveWindow = () => ({
  isDestroyed: () => false,
  isFullScreen: () => true,
  isMinimized: () => false,
  isVisible: () => true,
  getWindowButtonPosition: () => ({ x: 12, y: 6 })
})

// What Electron actually hands back after destroy(): the JS wrapper survives and
// still exposes every method, but the native binding behind each one throws.
// This is the shape that produced #38468 — `win?.isFullScreen?.()` finds the
// method, invokes it, and the TypeError escapes getWindowState().
const destroyedWindow = () => {
  const destroyed = () => {
    throw new TypeError('Object has been destroyed')
  }

  return {
    isDestroyed: () => true,
    isFullScreen: destroyed,
    isMinimized: destroyed,
    isVisible: destroyed,
    getWindowButtonPosition: destroyed
  }
}

test('isWindowLive is true only for a window that answers isDestroyed() false', () => {
  assert.equal(isWindowLive(liveWindow()), true)
  assert.equal(isWindowLive(destroyedWindow()), false)
  assert.equal(isWindowLive(undefined), false)
  assert.equal(isWindowLive(null), false)
  // No isDestroyed() to probe (a plain stand-in) — treat as not live rather than
  // assuming the rest of the surface is safe to call.
  assert.equal(isWindowLive({}), false)
})

test('windowChromeState passes a live window through unchanged', () => {
  assert.deepEqual(windowChromeState(liveWindow()), {
    isFullscreen: true,
    isMinimized: false,
    isVisible: true
  })
})

test('windowChromeState reports all-false for an absent or partial window', () => {
  const allFalse = { isFullscreen: false, isMinimized: false, isVisible: false }

  assert.deepEqual(windowChromeState(undefined), allFalse)
  assert.deepEqual(windowChromeState(null), allFalse)
  assert.deepEqual(windowChromeState({}), allFalse)
})

test('windowChromeState reports all-false for a destroyed window instead of throwing', () => {
  // Regression for #38468: "Desktop boot failed: Object has been destroyed at
  // getWindowState()". A destroyed window must degrade, not crash the boot
  // payload it is spread into.
  assert.deepEqual(windowChromeState(destroyedWindow()), {
    isFullscreen: false,
    isMinimized: false,
    isVisible: false
  })
})

test('windowButtonPosition passes a live window through unchanged', () => {
  assert.deepEqual(windowButtonPosition(liveWindow(), FALLBACK_BUTTON_POSITION), { x: 12, y: 6 })
})

test('windowButtonPosition falls back for absent, partial, and destroyed windows', () => {
  assert.equal(windowButtonPosition(undefined, FALLBACK_BUTTON_POSITION), FALLBACK_BUTTON_POSITION)
  assert.equal(windowButtonPosition(null, FALLBACK_BUTTON_POSITION), FALLBACK_BUTTON_POSITION)
  assert.equal(windowButtonPosition({}, FALLBACK_BUTTON_POSITION), FALLBACK_BUTTON_POSITION)
  assert.equal(windowButtonPosition(destroyedWindow(), FALLBACK_BUTTON_POSITION), FALLBACK_BUTTON_POSITION)
})

test('windowButtonPosition keeps the pre-guard fallback for a live window with no position', () => {
  // Preserves the old `|| WINDOW_BUTTON_POSITION` behaviour on platforms where
  // the window has no traffic lights to report.
  const noPosition = { ...liveWindow(), getWindowButtonPosition: () => undefined }
  assert.equal(windowButtonPosition(noPosition, FALLBACK_BUTTON_POSITION), FALLBACK_BUTTON_POSITION)
})
