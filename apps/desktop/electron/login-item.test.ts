import { beforeEach, describe, expect, it, vi } from 'vitest'

import { registerLoginItemHandlers } from './login-item'

type LoginItemSettings = { openAtLogin?: boolean; openAsHidden?: boolean }

describe('login-item IPC handlers', () => {
  const handlers = new Map<string, (...args: any[]) => unknown>()
  const getLoginItemSettings = vi.fn<() => LoginItemSettings>(() => ({ openAtLogin: false, openAsHidden: false }))
  const setLoginItemSettings = vi.fn()
  const ipcMain = {
    handle: vi.fn((channel: string, handler: (...args: any[]) => unknown) => {
      handlers.set(channel, handler)
    })
  }
  const app = { getLoginItemSettings, setLoginItemSettings } as any

  beforeEach(() => {
    handlers.clear()
    getLoginItemSettings.mockReset()
    getLoginItemSettings.mockReturnValue({ openAtLogin: false, openAsHidden: false })
    setLoginItemSettings.mockReset()
    registerLoginItemHandlers(app, ipcMain as any)
  })

  it('returns the Electron login-item state', () => {
    const state = { openAtLogin: true, openAsHidden: false }
    getLoginItemSettings.mockReturnValue(state)

    expect(handlers.get('hermes:login-item:get')?.()).toBe(state)
    expect(getLoginItemSettings).toHaveBeenCalledOnce()
  })

  it('sets openAtLogin and the current executable path', () => {
    handlers.get('hermes:login-item:set')?.({}, { openAtLogin: true })

    expect(setLoginItemSettings).toHaveBeenCalledWith({
      openAtLogin: true,
      openAsHidden: false,
      path: process.execPath,
      args: process.defaultApp ? [process.argv[1] ?? ''] : []
    })
  })

  it('forwards openAsHidden when provided and tolerates an empty Electron state', () => {
    getLoginItemSettings.mockReturnValue({})

    expect(handlers.get('hermes:login-item:get')?.()).toEqual({})
    expect(() => handlers.get('hermes:login-item:set')?.({}, { openAtLogin: false, openAsHidden: true })).not.toThrow()
    expect(setLoginItemSettings).toHaveBeenCalledWith(expect.objectContaining({ openAsHidden: true }))
  })
})
