import fs from 'node:fs'

import { describe, expect, it, vi } from 'vitest'

// terminal-ipc imports electron and node-pty at module level; neither exists in
// the vitest node environment. Mock both so the module can load.
vi.mock('electron', () => ({ app: {}, ipcMain: { handle: vi.fn() } }))
vi.mock('node-pty', () => ({ default: { spawn: vi.fn() } }))

import { resolvePosixInteractiveShell } from './terminal-ipc'

describe('resolvePosixInteractiveShell', () => {
  it('prefers a PATH-resolved shell over absolute locations (NixOS case)', () => {
    // NixOS: nothing under /bin, zsh on PATH via the user profile.
    const findOnPath = vi.fn((name: string) => (name === 'zsh' ? '/run/current-system/sw/bin/zsh' : null))
    const realAccessSync = fs.accessSync
    vi.spyOn(fs, 'accessSync').mockImplementation((p: any, mode?: any) => {
      if (typeof p === 'string' && p.startsWith('/bin/')) {
        throw new Error(`ENOENT: ${p}`)
      }

      return realAccessSync(p as any, mode)
    })

    try {
      expect(resolvePosixInteractiveShell(findOnPath)).toBe('/run/current-system/sw/bin/zsh')
    } finally {
      vi.restoreAllMocks()
    }
  })

  it('falls back to an absolute shell when PATH has neither zsh nor bash', () => {
    const findOnPath = vi.fn(() => null)
    const result = resolvePosixInteractiveShell(findOnPath)
    // Whatever exists on this host: /bin/zsh, /bin/bash, /bin/sh, or PATH sh.
    expect(result).toBeTruthy()
  })

  it('returns bare /bin/sh as last resort when no candidate exists anywhere', () => {
    // Simulate an empty host: every absolute candidate lookup fails too.
    const findOnPath = vi.fn(() => null)
    vi.spyOn(fs, 'accessSync').mockImplementation(() => {
      throw new Error('ENOENT')
    })

    try {
      expect(resolvePosixInteractiveShell(findOnPath)).toBe('/bin/sh')
    } finally {
      vi.restoreAllMocks()
    }
  })
})