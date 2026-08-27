import { beforeEach, describe, expect, it, vi } from 'vitest'

import { registerHudIpc } from './hud-ipc'
import { defaultTranslucencyState } from './translucency'

const electronMock = vi.hoisted(() => {
  const handlers = new Map<string, (...args: any[]) => unknown>()
  const listeners = new Map<string, (...args: any[]) => unknown>()

  return {
    handlers,
    ipcMain: {
      handle: vi.fn((channel: string, handler: (...args: any[]) => unknown) => handlers.set(channel, handler)),
      on: vi.fn((channel: string, listener: (...args: any[]) => unknown) => listeners.set(channel, listener))
    },
    listeners
  }
})

vi.mock('electron', () => ({ ipcMain: electronMock.ipcMain }))

function register() {
  const sender = {}
  const hudWindow = { isDestroyed: () => false, webContents: sender }
  const setHudSessionState = vi.fn()
  const openHudWindow = vi.fn()

  registerHudIpc({
    closeHudWindow: vi.fn(),
    getHudWindow: () => hudWindow as never,
    getTranslucencyState: () => defaultTranslucencyState('dark', false, false),
    isMac: false,
    openHudWindow,
    resetHudLayout: () => true,
    setHudSessionState
  })

  return { openHudWindow, sender, setHudSessionState }
}

beforeEach(() => {
  electronMock.handlers.clear()
  electronMock.listeners.clear()
  electronMock.ipcMain.handle.mockClear()
  electronMock.ipcMain.on.mockClear()
})

describe('HUD session IPC', () => {
  it('carries the exact connection owner through open IPC', () => {
    const { openHudWindow } = register()
    const ownerRoute = { connectionId: 'source-b', mode: 'remote', profile: 'worker' }

    electronMock.handlers.get('hermes:hud:open')?.({}, { ownerRoute, sessionId: 'shared' })

    expect(openHudWindow).toHaveBeenCalledWith('shared', ownerRoute, null, null)
  })

  it('carries the HUD stored-session exact owner into main’s authoritative latch', () => {
    const { sender, setHudSessionState } = register()
    const ownerRoute = { connectionId: 'source-b', mode: 'remote', profile: 'worker' }

    electronMock.listeners.get('hermes:hud:session')?.(
      { sender },
      { newChatGeneration: null, ownerRoute, sessionId: 'shared' }
    )

    expect(setHudSessionState).toHaveBeenCalledWith({ newChatGeneration: null, ownerRoute, sessionId: 'shared' })
  })

  it('carries the HUD New Chat generation into main’s authoritative latch', () => {
    const { sender, setHudSessionState } = register()

    electronMock.listeners.get('hermes:hud:session')?.(
      { sender },
      { newChatGeneration: '88888888-8888-4888-8888-888888888888', sessionId: null }
    )

    expect(setHudSessionState).toHaveBeenCalledWith({
      newChatGeneration: '88888888-8888-4888-8888-888888888888',
      ownerRoute: null,
      sessionId: null
    })
  })

  it('preserves a legacy numeric New Chat generation without coercion', () => {
    const { sender, setHudSessionState } = register()

    electronMock.listeners.get('hermes:hud:session')?.({ sender }, { newChatGeneration: 7, sessionId: null })

    expect(setHudSessionState).toHaveBeenCalledWith({ newChatGeneration: 7, ownerRoute: null, sessionId: null })
  })

  it('discards a reported generation for a stored HUD session', () => {
    const { sender, setHudSessionState } = register()

    electronMock.listeners.get('hermes:hud:session')?.(
      { sender },
      { newChatGeneration: '88888888-8888-4888-8888-888888888888', sessionId: 'stored-hud' }
    )

    expect(setHudSessionState).toHaveBeenCalledWith({
      newChatGeneration: null,
      ownerRoute: null,
      sessionId: 'stored-hud'
    })
  })
})
