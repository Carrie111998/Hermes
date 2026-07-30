import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const TILES_KEY = 'hermes.desktop.sessionTiles.v2'

const values = new Map<string, string>()

const localStorageStub: Storage = {
  clear: () => values.clear(),
  getItem: key => values.get(key) ?? null,
  key: index => [...values.keys()][index] ?? null,
  get length() {
    return values.size
  },
  removeItem: key => void values.delete(key),
  setItem: (key, value) => void values.set(key, String(value))
}

describe('session tile profile migration', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'localStorage', { configurable: true, value: localStorageStub })
    window.localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('does not mistake an old v2 rail bucket for the session owner', async () => {
    window.localStorage.setItem(
      TILES_KEY,
      JSON.stringify({ default: [{ dir: 'center', storedSessionId: 'cross-profile-session' }] })
    )

    const { $sessionTiles, patchSessionTile } = await import('./session-states')

    expect($sessionTiles.get()).toEqual([
      {
        anchor: undefined,
        before: undefined,
        dir: 'center',
        profile: undefined,
        storedSessionId: 'cross-profile-session'
      }
    ])

    patchSessionTile('cross-profile-session', { runtimeId: 'runtime-1' })

    const persisted = JSON.parse(window.localStorage.getItem(TILES_KEY) ?? '{}') as Record<
      string,
      Array<{ profile?: string; storedSessionId: string }>
    >

    expect(persisted.default).toEqual([{ storedSessionId: 'cross-profile-session', dir: 'center' }])
  })
})
