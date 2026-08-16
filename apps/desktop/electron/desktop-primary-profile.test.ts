import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  applyDesktopPrimaryProfile,
  readDesktopPrimaryProfile,
  writeDesktopPrimaryProfile
} from './desktop-primary-profile'

describe('Desktop primary profile persistence', () => {
  let configPath: string
  let temporaryDirectory: string

  beforeEach(() => {
    temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-primary-profile-'))
    configPath = path.join(temporaryDirectory, 'active-profile.json')
  })

  afterEach(() => {
    fs.rmSync(temporaryDirectory, { force: true, recursive: true })
  })

  it('persists named to default before tearing down and reloading the primary backend', async () => {
    writeDesktopPrimaryProfile(configPath, 'dev')
    const events: string[] = []

    await expect(
      applyDesktopPrimaryProfile(configPath, 'default', {
        teardownPrimary: async () => {
          events.push(`teardown:${readDesktopPrimaryProfile(configPath)}`)
        },
        reload: () => events.push('reload')
      })
    ).resolves.toBe('default')

    expect(fs.readFileSync(configPath, 'utf8')).toBe(JSON.stringify({ profile: 'default' }, null, 2))
    expect(events).toEqual(['teardown:default', 'reload'])
  })

  it('persists default to a named primary profile and runs the same lifecycle', async () => {
    writeDesktopPrimaryProfile(configPath, 'default')
    const teardownPrimary = vi.fn(async () => undefined)
    const reload = vi.fn()

    await expect(applyDesktopPrimaryProfile(configPath, 'dev', { reload, teardownPrimary })).resolves.toBe('dev')

    expect(readDesktopPrimaryProfile(configPath)).toBe('dev')
    expect(teardownPrimary).toHaveBeenCalledOnce()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('retries the re-home when a prior teardown failed after persistence', async () => {
    writeDesktopPrimaryProfile(configPath, 'dev')

    const teardownPrimary = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend did not stop'))
      .mockResolvedValueOnce(undefined)

    const reload = vi.fn()

    await expect(applyDesktopPrimaryProfile(configPath, 'default', { reload, teardownPrimary })).rejects.toThrow(
      'backend did not stop'
    )
    expect(readDesktopPrimaryProfile(configPath)).toBe('default')
    expect(reload).not.toHaveBeenCalled()

    await expect(applyDesktopPrimaryProfile(configPath, 'default', { reload, teardownPrimary })).resolves.toBe(
      'default'
    )
    expect(teardownPrimary).toHaveBeenCalledTimes(2)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('rejects an invalid profile without changing persisted state or lifecycle', async () => {
    writeDesktopPrimaryProfile(configPath, 'dev')
    const teardownPrimary = vi.fn(async () => undefined)
    const reload = vi.fn()

    await expect(applyDesktopPrimaryProfile(configPath, 'Not Valid!', { reload, teardownPrimary })).rejects.toThrow(
      'Invalid profile name'
    )

    expect(readDesktopPrimaryProfile(configPath)).toBe('dev')
    expect(teardownPrimary).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })
})
