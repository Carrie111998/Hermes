/**
 * Windows chat windows must be ordinary opaque DWM windows while glass is off.
 * `transparent: true` is constructor-only, so turning glass on/off requires a
 * recreate when that bit would change. These helpers are the contract main
 * applies at birth and on the Settings toggle.
 */
import { describe, expect, it } from 'vitest'

import {
  chatWindowNeedsSurfaceRecreate,
  chatWindowSurfaceOptions,
  DEFAULT_GLASS_MATERIAL,
  DEFAULT_GLASS_SCOPE,
  type TranslucencyState,
  windowsChatWindowTransparent
} from '../translucency'

const clear = (intensity: number): TranslucencyState => ({
  intensity,
  fade: 0,
  mode: 'clear',
  material: DEFAULT_GLASS_MATERIAL,
  scope: DEFAULT_GLASS_SCOPE
})

const glass = (intensity: number, material = DEFAULT_GLASS_MATERIAL): TranslucencyState => ({
  intensity,
  fade: 0,
  mode: 'glass',
  material,
  scope: DEFAULT_GLASS_SCOPE
})

const themed = '#101014'

describe('windowsChatWindowTransparent', () => {
  it('is off for the default Win11 profile (glass selected, intensity 0)', () => {
    expect(windowsChatWindowTransparent('win32', true, glass(0))).toBe(false)
    expect(windowsChatWindowTransparent('win32', true, clear(0))).toBe(false)
    expect(windowsChatWindowTransparent('win32', true, clear(60))).toBe(false)
  })

  it('is on only when glass is actually active on a glass-capable Windows host', () => {
    expect(windowsChatWindowTransparent('win32', true, glass(60))).toBe(true)
    expect(windowsChatWindowTransparent('win32', false, glass(60))).toBe(false)
    expect(windowsChatWindowTransparent('darwin', true, glass(60))).toBe(false)
    expect(windowsChatWindowTransparent('linux', true, glass(60))).toBe(false)
  })
})

describe('chatWindowSurfaceOptions', () => {
  it('does not birth a Win11 window transparent while glass is off', () => {
    const off = chatWindowSurfaceOptions({
      platform: 'win32',
      glassSupported: true,
      state: glass(0),
      themedColor: themed
    })

    expect(off.transparent).toBeUndefined()
    expect(off.backgroundMaterial).toBe('none')
    expect(off.backgroundColor).toBe(themed)
  })

  it('births a Win11 window transparent only while glass is active', () => {
    const on = chatWindowSurfaceOptions({
      platform: 'win32',
      glassSupported: true,
      state: glass(60),
      themedColor: themed
    })

    expect(on.transparent).toBe(true)
    expect(on.backgroundMaterial).not.toBe('none')
    expect(on.backgroundColor).toBeUndefined()
  })

  it('never sets Windows transparent on macOS or unsupported Windows', () => {
    expect(
      chatWindowSurfaceOptions({
        platform: 'darwin',
        glassSupported: true,
        state: glass(60),
        themedColor: themed
      }).transparent
    ).toBeUndefined()
    expect(
      chatWindowSurfaceOptions({
        platform: 'win32',
        glassSupported: false,
        state: glass(60),
        themedColor: themed
      }).transparent
    ).toBeUndefined()
  })
})

describe('chatWindowNeedsSurfaceRecreate', () => {
  it('is required on glass-capable Windows when glass crosses the active threshold', () => {
    expect(chatWindowNeedsSurfaceRecreate(glass(0), glass(1), 'win32', true)).toBe(true)
    expect(chatWindowNeedsSurfaceRecreate(glass(1), glass(0), 'win32', true)).toBe(true)
    expect(chatWindowNeedsSurfaceRecreate(clear(60), glass(60), 'win32', true)).toBe(true)
    expect(chatWindowNeedsSurfaceRecreate(glass(60), clear(60), 'win32', true)).toBe(true)
    // A 0→1 crossing is the first notch of a drag, not a completed gesture.
    // Main must trailing-debounce recreate so Settings is not torn down mid-drag.
    expect(chatWindowNeedsSurfaceRecreate(glass(0), glass(60), 'win32', true)).toBe(true)
  })

  it('is not required for tint/frost/fade ticks or on platforms that can swap live', () => {
    expect(chatWindowNeedsSurfaceRecreate(glass(40), glass(41), 'win32', true)).toBe(false)
    expect(chatWindowNeedsSurfaceRecreate(clear(40), clear(41), 'win32', true)).toBe(false)
    expect(chatWindowNeedsSurfaceRecreate(glass(0), glass(1), 'darwin', true)).toBe(false)
    expect(chatWindowNeedsSurfaceRecreate(glass(0), glass(1), 'win32', false)).toBe(false)
  })
})
