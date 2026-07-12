import { beforeEach, describe, expect, it, vi } from 'vitest'

const getHermesConfigRecord = vi.fn()
const getApiRequestProfile = vi.fn<() => null | string>(() => null)
const saveHermesConfig = vi.fn()

vi.mock('@/hermes', () => ({
  getApiRequestProfile: () => getApiRequestProfile(),
  getHermesConfigRecord: (profile?: null | string) => getHermesConfigRecord(profile),
  saveHermesConfig: (config: unknown, profile?: null | string) => saveHermesConfig(config, profile)
}))

import { readKey, writeKey } from '@/lib/storage'

import {
  $desktopStatusbarMode,
  applyDesktopStatusbarFromConfig,
  migrateLegacyDesktopStatusbarPreference,
  normalizeDesktopStatusbarMode,
  persistDesktopStatusbarMode,
  toggleDesktopStatusbarVisible
} from './desktop-statusbar'

const LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY = 'hermes.desktop.statusbarVisible'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

describe('desktop status bar preference', () => {
  beforeEach(() => {
    writeKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY, null)
    vi.clearAllMocks()
    getApiRequestProfile.mockReturnValue(null)
    applyDesktopStatusbarFromConfig({ display: { desktop_statusbar: 'off' } })
  })

  it('normalizes unknown and missing values to the quiet default', () => {
    expect(normalizeDesktopStatusbarMode(undefined)).toBe('off')
    expect(normalizeDesktopStatusbarMode('sometimes')).toBe('off')
    expect(normalizeDesktopStatusbarMode('auto-hide')).toBe('auto-hide')
  })

  it('applies a saved profile preference', () => {
    applyDesktopStatusbarFromConfig({ display: { desktop_statusbar: 'off' } })

    expect($desktopStatusbarMode.get()).toBe('off')
  })

  it('preserves sibling config fields while persisting', async () => {
    getHermesConfigRecord.mockResolvedValue({ display: { language: 'zh', skin: 'slate' }, terminal: { cwd: '/work' } })
    saveHermesConfig.mockResolvedValue({ ok: true })

    const saved = await persistDesktopStatusbarMode('auto-hide')

    expect($desktopStatusbarMode.get()).toBe('auto-hide')
    expect(saved).toEqual({
      display: { desktop_statusbar: 'auto-hide', language: 'zh', skin: 'slate' },
      terminal: { cwd: '/work' }
    })
    expect(saveHermesConfig).toHaveBeenCalledWith(saved, null)
  })

  it('rolls back the optimistic preference when persistence fails', async () => {
    getHermesConfigRecord.mockRejectedValue(new Error('offline'))

    await expect(persistDesktopStatusbarMode('on')).rejects.toThrow('offline')
    expect($desktopStatusbarMode.get()).toBe('off')
  })

  it('toggles a visible mode off and restores the always-visible mode', async () => {
    getHermesConfigRecord.mockResolvedValue({ display: { language: 'zh' } })
    saveHermesConfig.mockResolvedValue({ ok: true })
    applyDesktopStatusbarFromConfig({ display: { desktop_statusbar: 'auto-hide' } })

    await toggleDesktopStatusbarVisible()

    expect($desktopStatusbarMode.get()).toBe('off')
    expect(saveHermesConfig).toHaveBeenLastCalledWith({ display: { desktop_statusbar: 'off', language: 'zh' } }, null)

    await toggleDesktopStatusbarVisible()

    expect($desktopStatusbarMode.get()).toBe('on')
    expect(saveHermesConfig).toHaveBeenLastCalledWith({ display: { desktop_statusbar: 'on', language: 'zh' } }, null)
  })

  it('migrates the released whole-bar preference into the active profile', async () => {
    writeKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY, 'true')
    const config = { display: { desktop_statusbar: 'off', language: 'zh' } }
    getHermesConfigRecord.mockResolvedValue(config)
    saveHermesConfig.mockResolvedValue({ ok: true })

    applyDesktopStatusbarFromConfig(config)

    expect($desktopStatusbarMode.get()).toBe('on')

    await migrateLegacyDesktopStatusbarPreference(config)

    expect(saveHermesConfig).toHaveBeenCalledWith({ display: { desktop_statusbar: 'on', language: 'zh' } }, null)
    expect(readKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY)).toBeNull()
  })

  it('prefers an explicit non-default config over the legacy whole-bar key', async () => {
    writeKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY, 'false')
    const config = { display: { desktop_statusbar: 'auto-hide' } }

    applyDesktopStatusbarFromConfig(config)
    await migrateLegacyDesktopStatusbarPreference(config)

    expect($desktopStatusbarMode.get()).toBe('auto-hide')
    expect(getHermesConfigRecord).not.toHaveBeenCalled()
    expect(readKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY)).toBeNull()
  })

  it('serializes rapid changes so the latest intent wins', async () => {
    const firstSave = deferred<{ ok: boolean }>()
    getHermesConfigRecord.mockResolvedValue({ display: { language: 'zh' } })
    saveHermesConfig.mockReturnValueOnce(firstSave.promise).mockResolvedValueOnce({ ok: true })

    const show = persistDesktopStatusbarMode('on')
    const hide = persistDesktopStatusbarMode('off')

    expect($desktopStatusbarMode.get()).toBe('off')
    await vi.waitFor(() => expect(saveHermesConfig).toHaveBeenCalledTimes(1))
    expect(getHermesConfigRecord).toHaveBeenCalledTimes(1)

    firstSave.resolve({ ok: true })
    await Promise.all([show, hide])

    expect(getHermesConfigRecord).toHaveBeenCalledTimes(2)
    expect(saveHermesConfig).toHaveBeenLastCalledWith({ display: { desktop_statusbar: 'off', language: 'zh' } }, null)
    expect($desktopStatusbarMode.get()).toBe('off')
  })

  it('pins both halves of a config write to the profile that started it', async () => {
    const profileAConfig = deferred<Record<string, unknown>>()
    getApiRequestProfile.mockReturnValue('profile-a')
    getHermesConfigRecord.mockReturnValue(profileAConfig.promise)
    saveHermesConfig.mockResolvedValue({ ok: true })

    const saving = persistDesktopStatusbarMode('on')
    await vi.waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalledWith('profile-a'))

    getApiRequestProfile.mockReturnValue('profile-b')
    applyDesktopStatusbarFromConfig({ display: { desktop_statusbar: 'off' } })
    profileAConfig.resolve({ display: { language: 'ja' } })
    await saving

    expect(saveHermesConfig).toHaveBeenCalledWith({ display: { desktop_statusbar: 'on', language: 'ja' } }, 'profile-a')
    expect($desktopStatusbarMode.get()).toBe('off')
  })
})
