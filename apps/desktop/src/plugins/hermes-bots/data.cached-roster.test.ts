/**
 * `cachedUnionRoster` is the imperative roster read — the @mention popover
 * must answer per keystroke and the composer middleware runs on submit, so
 * neither can wait on the hook.
 *
 * `useRoster` keys its query on `[...ROSTER_KEY, connectionId]`, one entry per
 * connection the window has been on. Reading it back with the BARE key is an
 * exact-key match in TanStack Query and therefore matches NOTHING — the
 * regression where completions offered no handles and remote `@name-device`
 * mentions passed through unresolved. The read has to fall back to a prefix
 * match over the key family, newest snapshot wins.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const { cache, connection } = vi.hoisted(() => ({
  cache: new Map<string, { key: unknown[]; value: unknown }>(),
  connection: { id: 'local' }
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')
  const keyOf = (key: unknown[]) => JSON.stringify(key)

  return {
    atom,
    host: { state: { connectionId: { get: () => connection.id }, profile: { get: () => 'default' } } },
    queryClient: {
      getQueriesData: ({ queryKey }: { queryKey: unknown[] }) =>
        [...cache.values()]
          .filter(entry => queryKey.every((part, index) => entry.key[index] === part))
          .map(entry => [entry.key, entry.value]),
      getQueryData: (key: unknown[]) => cache.get(keyOf(key))?.value,
      invalidateQueries: vi.fn(),
      setQueryData: (key: unknown[], value: unknown) => cache.set(keyOf(key), { key, value })
    },
    useQuery: vi.fn(),
    useValue: vi.fn()
  }
})

vi.mock('./shared', () => ({ getPluginCtx: () => null, ID: 'hermes-bots' }))

const seed = (key: unknown[], value: unknown) => cache.set(JSON.stringify(key), { key, value })

beforeEach(() => {
  cache.clear()
  connection.id = 'local'
})

describe('cachedUnionRoster', () => {
  it('reads the entry useRoster wrote under the connection-suffixed key', async () => {
    const { cachedUnionRoster } = await import('./data')

    seed(['hermes-bots', 'roster', 'local'], { profiles: [{ name: 'default' }] })

    expect(cachedUnionRoster()?.profiles).toHaveLength(1)
    // The bare key is what the broken read used — it must still miss, or this
    // test would pass for the wrong reason.
    expect(cache.has(JSON.stringify(['hermes-bots', 'roster']))).toBe(false)
  })

  it('falls back to another connection’s entry when the window has moved', async () => {
    const { cachedUnionRoster } = await import('./data')

    seed(['hermes-bots', 'roster', 'vera'], { profiles: [{ connectionId: 'vera', name: 'default' }] })
    connection.id = 'local'

    expect(cachedUnionRoster()?.profiles?.[0]).toMatchObject({ connectionId: 'vera' })
  })

  it('prefers the freshest snapshot among several cached connections', async () => {
    const { cachedUnionRoster } = await import('./data')

    seed(['hermes-bots', 'roster', 'old'], { fetchedAt: 1_000, profiles: [{ name: 'stale' }] })
    seed(['hermes-bots', 'roster', 'new'], { fetchedAt: 9_000, profiles: [{ name: 'fresh' }] })
    connection.id = 'neither'

    expect(cachedUnionRoster()?.profiles?.[0]).toMatchObject({ name: 'fresh' })
  })

  it('reports nothing rather than throwing on a cold cache', async () => {
    const { cachedUnionRoster } = await import('./data')

    expect(cachedUnionRoster()).toBeNull()
  })
})

describe('progressive roster cache', () => {
  it('hydrates bounded connection-scoped rows without a gateway request', async () => {
    const { hydrateRosterSnapshot } = await import('./data')

    const stored = {
      entries: {
        local: {
          fetchedAt: 1234,
          profiles: [
            {
              canonical_session: { id: 'must-not-survive' },
              display_name: 'Writer',
              last_session: { preview: 'private text' },
              name: 'writer'
            }
          ],
          sources: [{ connectionId: 'local', kind: 'local', label: 'This device', reachable: true }]
        }
      },
      version: 1
    }

    const set = vi.fn()

    const storage = {
      get<T>(_key: string, _fallback: T): T {
        return stored as T
      },
      remove: vi.fn(),
      set
    }

    await expect(hydrateRosterSnapshot(storage)).resolves.toBe(true)
    expect(cache.get(JSON.stringify(['hermes-bots', 'roster', 'local']))?.value).toEqual({
      fetchedAt: 1234,
      partial: true,
      profiles: [{ display_name: 'Writer', name: 'writer' }],
      sources: [{ connectionId: 'local', kind: 'local', label: 'This device', reachable: true }]
    })
    expect(set).not.toHaveBeenCalled()
  })

  it('never lets a late lightweight response overwrite rich roster data', async () => {
    const { publishColdRosterSnapshot } = await import('./data')
    const key = ['hermes-bots', 'roster', 'local']
    const rich = { fetchedAt: 2000, profiles: [{ canonical_session: { id: 'live' }, name: 'writer' }] }

    seed(key, rich)

    expect(publishColdRosterSnapshot(key, { profiles: [{ name: 'stale' }] }, 3000)).toBe(false)
    expect(cache.get(JSON.stringify(key))?.value).toBe(rich)
  })

  it('persists sanitized rows after hydration has settled', async () => {
    const { persistRosterSnapshot } = await import('./data')
    const writes: Array<{ key: string; value: unknown }> = []

    const storage = {
      get<T>(_key: string, fallback: T): T {
        return fallback
      },
      remove: vi.fn(),
      set: vi.fn((key: string, value: unknown) => {
        writes.push({ key, value })
      })
    }

    await expect(
      persistRosterSnapshot(
        'remote-a',
        [
          {
            canonical_session: { id: 'must-not-persist', preview: 'private text' },
            connectionId: 'remote-a',
            name: 'writer'
          }
        ],
        [],
        4321,
        storage
      )
    ).resolves.toBe(true)

    expect(writes.at(-1)?.key).toBe('roster-snapshot-v1')
    expect(writes.at(-1)?.value).toMatchObject({
      entries: {
        'remote-a': {
          fetchedAt: 4321,
          profiles: [{ connectionId: 'remote-a', name: 'writer' }]
        }
      },
      version: 1
    })
    expect(JSON.stringify(writes.at(-1)?.value)).not.toContain('must-not-persist')
    expect(JSON.stringify(writes.at(-1)?.value)).not.toContain('private text')
  })

  it('uses a slower rich-roster interval while the pane is hidden', async () => {
    const { rosterRefreshInterval } = await import('./data')

    expect(rosterRefreshInterval(true)).toBe(5000)
    expect(rosterRefreshInterval(false)).toBe(30000)
  })
})
