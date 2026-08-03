import { afterEach, describe, expect, it, vi } from 'vitest'

const LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY = 'hermes.desktop.statusbarVisible'
const STATUSBAR_VISIBLE_STORAGE_KEY = 'hermes.desktop.statusbarVisible.v2'

async function loadStatusbarPrefs() {
  vi.resetModules()

  return import('./statusbar-prefs')
}

afterEach(() => {
  window.localStorage.removeItem(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY)
  window.localStorage.removeItem(STATUSBAR_VISIBLE_STORAGE_KEY)
})

describe('statusbar visibility preference', () => {
  it('shows the statusbar when a user has no stored preference', async () => {
    window.localStorage.removeItem(STATUSBAR_VISIBLE_STORAGE_KEY)

    expect((await loadStatusbarPrefs()).$statusbarVisible.get()).toBe(true)
  })

  it('resets the legacy default that was persisted before user interaction', async () => {
    window.localStorage.setItem(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY, 'false')

    expect((await loadStatusbarPrefs()).$statusbarVisible.get()).toBe(true)
  })

  it('preserves an explicit choice to hide the statusbar', async () => {
    window.localStorage.setItem(STATUSBAR_VISIBLE_STORAGE_KEY, 'false')

    expect((await loadStatusbarPrefs()).$statusbarVisible.get()).toBe(false)
  })

  it('persists a new hide choice across reloads', async () => {
    const prefs = await loadStatusbarPrefs()

    prefs.toggleStatusbarVisible()

    expect(window.localStorage.getItem(STATUSBAR_VISIBLE_STORAGE_KEY)).toBe('false')
    expect((await loadStatusbarPrefs()).$statusbarVisible.get()).toBe(false)
  })
})
