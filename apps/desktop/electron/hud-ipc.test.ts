/**
 * Contract guard for the HUD frost path.
 *
 * The HUD window is created `transparent: true`, and on Windows Electron's
 * `setBackgroundMaterial` is a one-way door: issuing 'none' flips the backing
 * to an opaque state with no way back, so any material hand-off leaves the HUD
 * as a persistent opaque slab over the whole window (issue #91459). The HUD
 * therefore NEVER calls setBackgroundMaterial, on any platform and in any
 * translucency/band state — it is CSS-only, exactly as it was before glass
 * landed for the chat windows.
 *
 * This is a behavior contract, not a snapshot: it drives the real
 * `registerHudIpc` and asserts the material method is never invoked, in any
 * state. A future change that reintroduces the call fails here immediately.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { GlassMaterial, TranslucencyState } from '../../shared/src/translucency'

import { registerHudIpc } from './hud-ipc'

const electronMock = vi.hoisted(() => {
  const handlers = new Map<string, (...args: any[]) => unknown>()
  const listeners = new Map<string, (...args: any[]) => void>()

  return {
    BrowserWindow: class {},
    ipcMain: {
      handle: (channel: string, fn: (...args: any[]) => unknown) => {
        handlers.set(channel, fn)
      },
      on: (channel: string, fn: (...args: any[]) => void) => {
        listeners.set(channel, fn)
      },
      handlers,
      listeners
    }
  }
})

vi.mock('electron', () => ({
  BrowserWindow: electronMock.BrowserWindow,
  ipcMain: electronMock.ipcMain
}))

// The native material methods the frost path could reach. setBackgroundMaterial
// is deliberately present so the contract assertion is meaningful — a
// reintroduced call would land on a real spy and fail the "never called" check.
function makeFakeHudWindow() {
  return {
    isDestroyed: () => false,
    setVibrancy: vi.fn(),
    setBackgroundMaterial: vi.fn(),
    setIgnoreMouseEvents: vi.fn(),
    getPosition: () => [0, 0],
    setBounds: vi.fn(),
    getSize: () => [400, 200],
    isResizable: () => false,
    setResizable: vi.fn(),
    webContents: { on: vi.fn(), send: vi.fn() }
  }
}

function glassState(intensity: number, material: GlassMaterial): TranslucencyState {
  return { intensity, fade: 0, mode: 'glass', material, scope: 'window' }
}

function clearState(intensity: number): TranslucencyState {
  return { intensity, fade: 0, mode: 'clear', material: 'under-window', scope: 'window' }
}

beforeEach(() => {
  electronMock.ipcMain.handlers.clear()
  electronMock.ipcMain.listeners.clear()
})

describe('HUD frost (issue #91459)', () => {
  it('never calls setBackgroundMaterial on the HUD window in any state', () => {
    const hudWindow = makeFakeHudWindow()

    const states: TranslucencyState[] = [
      clearState(0),
      clearState(60),
      clearState(100),
      glassState(0, 'header'),
      glassState(60, 'header'),
      glassState(60, 'under-window'),
      glassState(100, 'titlebar')
    ]

    let stateIndex = 0

    const { applyHudFrost } = registerHudIpc({
      isMac: true,
      getTranslucencyState: () => states[stateIndex],
      getHudWindow: () => hudWindow as never,
      openHudWindow: vi.fn(),
      closeHudWindow: vi.fn(),
      setHudSessionId: vi.fn()
    })

    // Drive both halves of the frost decision through the real IPC surface:
    // the band report (latches `bandShowing`) and the frost re-apply.
    const frostHandler = electronMock.ipcMain.handlers.get('hermes:hud:frost')!

    for (const bandShowing of [false, true]) {
      frostHandler({} as never, bandShowing)

      for (let i = 0; i < states.length; i += 1) {
        stateIndex = i
        applyHudFrost()
      }
    }

    // The Windows material door must never open for the transparent HUD.
    expect(hudWindow.setBackgroundMaterial).not.toHaveBeenCalled()
  })

  it('still frosts via vibrancy on macOS while the band is showing', () => {
    const hudWindow = makeFakeHudWindow()

    const { applyHudFrost } = registerHudIpc({
      isMac: true,
      getTranslucencyState: () => glassState(60, 'header'),
      getHudWindow: () => hudWindow as never,
      openHudWindow: vi.fn(),
      closeHudWindow: vi.fn(),
      setHudSessionId: vi.fn()
    })

    // Latch the band as showing (the renderer's report), then re-apply frost.
    electronMock.ipcMain.handlers.get('hermes:hud:frost')!({} as never, true)
    applyHudFrost()

    expect(hudWindow.setVibrancy).toHaveBeenCalledWith('header')
    expect(hudWindow.setBackgroundMaterial).not.toHaveBeenCalled()
  })
})
