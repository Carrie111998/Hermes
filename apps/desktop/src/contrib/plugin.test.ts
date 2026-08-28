import { describe, expect, it, vi } from 'vitest'

import { dispatchPluginNativeNotification } from '@/store/native-notifications'

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
    await expect(ctx.os.selectPaths({ directories: true, multiple: false })).resolves.toEqual([])
  })

  it('routes path selection through the bridge and surfaces a bridge failure', async () => {
    const bridge = {
      openExternal: vi.fn().mockResolvedValue(undefined),
      revealPath: vi.fn().mockResolvedValue(true),
      selectPaths: vi.fn()
        .mockResolvedValueOnce(['C:\\Projects\\demo\\source.docx'])
        .mockRejectedValueOnce(new Error('native dialog failed')),
      writeClipboard: vi.fn().mockRejectedValue(new Error('nope'))
    }

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = bridge

    try {
      const ctx = createPluginContext('demo')
      await expect(ctx.os.openExternal('https://example.com')).resolves.toBe(true)
      expect(bridge.openExternal).toHaveBeenCalledWith('https://example.com')
      await expect(ctx.os.revealPath('/tmp')).resolves.toBe(true)
      const pickerOptions = { defaultPath: 'C:\\Projects\\demo', directories: false, multiple: true }
      await expect(ctx.os.selectPaths(pickerOptions)).resolves.toEqual(['C:\\Projects\\demo\\source.docx'])
      expect(bridge.selectPaths).toHaveBeenCalledWith(pickerOptions)
      await expect(ctx.os.selectPaths(pickerOptions)).rejects.toThrow('native dialog failed')
      await expect(ctx.os.writeClipboard('hi')).resolves.toBe(false)
    } finally {
      delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    }
  })
})
