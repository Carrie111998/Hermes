import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $zoomScrollEnabled, setZoomScrollEnabled } from './zoom-scroll'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop
const setScrollEnabledBridge = vi.fn()
const getScrollEnabledBridge = vi.fn().mockResolvedValue(true)

beforeEach(() => {
  desktopWindow.hermesDesktop = {
    zoom: {
      getScrollEnabled: getScrollEnabledBridge,
      setScrollEnabled: setScrollEnabledBridge
    }
  } as unknown as Window['hermesDesktop']
  setZoomScrollEnabled(true)
  setScrollEnabledBridge.mockClear()
})

afterEach(() => {
  desktopWindow.hermesDesktop = initialHermesDesktop
})

describe('zoom-scroll store', () => {
  it('updates the preference and mirrors it to Electron', () => {
    setZoomScrollEnabled(false)
    expect($zoomScrollEnabled.get()).toBe(false)
    expect(setScrollEnabledBridge).toHaveBeenLastCalledWith(false)

    setZoomScrollEnabled(true)
    expect($zoomScrollEnabled.get()).toBe(true)
    expect(setScrollEnabledBridge).toHaveBeenLastCalledWith(true)
  })
})
