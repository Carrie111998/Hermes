import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import { dispatchPluginNativeNotification } from '@/store/native-notifications'
import { $connection, setConnection } from '@/store/session'

import { createPluginContext } from './plugin'

vi.mock('@/store/native-notifications', () => ({ dispatchPluginNativeNotification: vi.fn() }))

describe('createPluginContext.onDispose', () => {
  it('collects arbitrary cleanups so the host runs them on deactivate', () => {
    const disposers: Array<() => void> = []
    const ctx = createPluginContext('demo', dispose => disposers.push(dispose))

    let cleaned = false
    ctx.onDispose(() => {
      cleaned = true
    })

    // The cleanup is tracked alongside contribution/socket disposers, so the
    // loader's deactivate (which runs every collected disposer) tears it down.
    expect(disposers).toHaveLength(1)
    disposers.forEach(dispose => dispose())
    expect(cleaned).toBe(true)
  })
})

describe('createPluginContext.os', () => {
  it('dispatches a native notification attributed to the plugin', () => {
    const ctx = createPluginContext('demo')
    ctx.os.notify({ body: 'b', title: 't' })
    expect(dispatchPluginNativeNotification).toHaveBeenCalledWith('demo', { body: 'b', title: 't' })
  })

  it('resolves false (never throws) when the desktop bridge is missing', async () => {
    const ctx = createPluginContext('demo')

    // jsdom has no window.hermesDesktop — the exact older-shell/browser case.
    await expect(ctx.os.openExternal('https://example.com')).resolves.toBe(false)
    await expect(ctx.os.revealPath('/tmp')).resolves.toBe(false)
    await expect(ctx.os.writeClipboard('hi')).resolves.toBe(false)
  })

  it('routes through the bridge and turns a bridge throw into false', async () => {
    const bridge = {
      openExternal: vi.fn().mockResolvedValue(undefined),
      revealPath: vi.fn().mockResolvedValue(true),
      writeClipboard: vi.fn().mockRejectedValue(new Error('nope'))
    }

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = bridge

    try {
      const ctx = createPluginContext('demo')
      await expect(ctx.os.openExternal('https://example.com')).resolves.toBe(true)
      expect(bridge.openExternal).toHaveBeenCalledWith('https://example.com')
      await expect(ctx.os.revealPath('/tmp')).resolves.toBe(true)
      await expect(ctx.os.writeClipboard('hi')).resolves.toBe(false)
    } finally {
      delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    }
  })
})

describe('createPluginContext.connection', () => {
  afterEach(() => {
    setConnection(null)
  })

  const conn = (mode?: 'local' | 'remote') =>
    ({ baseUrl: 'http://127.0.0.1:8787', mode, token: 'secret' }) as unknown as HermesConnection

  it('reports the live mode without exposing the descriptor', () => {
    setConnection(conn('remote'))
    const ctx = createPluginContext('demo')

    expect(ctx.connection.mode()).toBe('remote')
    // The whole door is two functions — there is no descriptor to reach past.
    expect(Object.keys(ctx.connection).sort()).toEqual(['mode', 'onModeChange'])
  })

  it('reports null before a connection resolves', () => {
    expect(createPluginContext('demo').connection.mode()).toBeNull()
  })

  it('fires immediately and on every real transition', () => {
    setConnection(conn('local'))
    const seen: Array<'local' | 'remote' | null> = []
    createPluginContext('demo').connection.onModeChange(mode => seen.push(mode))

    setConnection(conn('remote'))
    setConnection(null)

    expect(seen).toEqual(['local', 'remote', null])
  })

  it('stays quiet when a descriptor refresh does not move the mode', () => {
    setConnection(conn('remote'))
    const listener = vi.fn()
    createPluginContext('demo').connection.onModeChange(listener)

    // A reconnect re-mints the descriptor (new token/wsUrl) on the same mode.
    setConnection({ ...conn('remote'), token: 'rotated' } as unknown as HermesConnection)

    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('stops listening when the plugin unloads, even if the disposer is ignored', () => {
    setConnection(conn('local'))
    const disposers: Array<() => void> = []
    const listener = vi.fn()
    createPluginContext('demo', dispose => disposers.push(dispose)).connection.onModeChange(listener)

    disposers.forEach(dispose => dispose())
    setConnection(conn('remote'))

    expect(listener).toHaveBeenCalledTimes(1)
    expect($connection.get()?.mode).toBe('remote')
  })
})
