import { describe, expect, it, vi } from 'vitest'

import { openExternalUrlForPlatform } from './external-url'

describe('openExternalUrlForPlatform', () => {
  it('opens WSL URLs without a shell even when shell metacharacters are present', () => {
    const proc = { on: vi.fn(), unref: vi.fn() } as never
    const spawn = vi.fn(() => proc)
    const shellOpenExternal = vi.fn(async () => undefined)
    const url = 'https://example.test/?x=1&calc.exe|whoami^<x>'

    expect(openExternalUrlForPlatform(url, { isWsl: true, spawn, shellOpenExternal })).toBe(true)
    expect(spawn).toHaveBeenCalledWith('explorer.exe', [url], expect.any(Object))
    expect(spawn).not.toHaveBeenCalledWith('cmd.exe', expect.anything(), expect.anything())
    expect(shellOpenExternal).not.toHaveBeenCalled()
  })

  it('passes the WSL failure callback through the safe opener path', () => {
    const proc = { on: vi.fn(), unref: vi.fn() } as never
    const spawn = vi.fn(() => proc)
    const shellOpenExternal = vi.fn(async () => undefined)
    const onError = vi.fn()

    openExternalUrlForPlatform('https://example.test', { isWsl: true, spawn, shellOpenExternal, onError })
    const callback = (proc as { on: ReturnType<typeof vi.fn> }).on.mock.calls[0]?.[1]
    callback?.(new Error('explorer unavailable'))
    expect(onError).toHaveBeenCalledWith(expect.any(Error))
  })
})
