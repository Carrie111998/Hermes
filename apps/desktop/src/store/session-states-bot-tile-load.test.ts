import { beforeEach, describe, expect, it, vi } from 'vitest'

const TILES_KEY = 'hermes.desktop.sessionTiles.v2'

async function loadSessionStates() {
  return import('./session-states')
}

/**
 * loadTilesByProfile's bot-bucket dedup collapses persisted tiles by their
 * storedSessionId on every module load (app start / restart). Two DIFFERENT
 * bot workspaces can legitimately persist a tile with the SAME
 * storedSessionId — a restored backup, a copied state.db — the same
 * collision class #95895 fixed for the sidebar's session rows. Keying the
 * dedup Map by bare storedSessionId silently drops one twin's tile on every
 * load; keying it by (workspaceOwnerKey, storedSessionId) keeps both.
 */
describe('loadTilesByProfile bot-bucket dedup (#92454-class)', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('keeps both twins when two bot workspaces persisted the same storedSessionId', async () => {
    window.localStorage.setItem(
      TILES_KEY,
      JSON.stringify({
        __bots_workspace__: [
          {
            ownerRoute: { connectionId: 'conn-a', mode: 'remote', profile: 'oxcoder', targetProfile: 'oxcoder' },
            storedSessionId: 'twin',
            workspaceMode: 'bots',
            workspaceOwnerKey: 'bot:a'
          },
          {
            ownerRoute: { connectionId: 'conn-b', mode: 'remote', profile: 't2oracle', targetProfile: 't2oracle' },
            storedSessionId: 'twin',
            workspaceMode: 'bots',
            workspaceOwnerKey: 'bot:b'
          }
        ]
      })
    )

    const { $sessionTiles } = await loadSessionStates()

    const owners = $sessionTiles
      .get()
      .filter(t => t.storedSessionId === 'twin')
      .map(t => t.workspaceOwnerKey)
      .sort()

    expect(owners).toEqual(['bot:a', 'bot:b'])
  })

  it('still collapses duplicates that share the same workspaceOwnerKey (genuine re-persist, not a twin)', async () => {
    window.localStorage.setItem(
      TILES_KEY,
      JSON.stringify({
        __bots_workspace__: [
          { storedSessionId: 'same', workspaceMode: 'bots', workspaceOwnerKey: 'bot:a' },
          { storedSessionId: 'same', workspaceMode: 'bots', workspaceOwnerKey: 'bot:a' }
        ]
      })
    )

    const { $sessionTiles } = await loadSessionStates()

    expect($sessionTiles.get().filter(t => t.storedSessionId === 'same')).toHaveLength(1)
  })
})
