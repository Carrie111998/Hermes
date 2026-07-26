import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// HMR-stability tests for the gateway registry. The registry parks its mutable
// state on globalThis (Symbol.for key) so a dev hot-update of the module hands
// back the SAME live sockets. `import.meta.hot` is truthy under vitest, so we
// exercise the real migration path: plant a container on globalThis, reset the
// module registry, re-import, and assert the fresh module adopted + migrated it.

const STATE_KEY = Symbol.for('hermes.desktop.gatewayRegistryState')

type Registry = Record<string, unknown>

function plantContainer(container: Registry): void {
  ;(globalThis as unknown as Record<symbol, Registry>)[STATE_KEY] = container
}

function readContainer(): Registry | undefined {
  return (globalThis as unknown as Record<symbol, Registry | undefined>)[STATE_KEY]
}

beforeEach(() => {
  vi.resetModules()
})

afterEach(() => {
  // Don't leak a planted container to another file's module load.
  delete (globalThis as unknown as Record<symbol, unknown>)[STATE_KEY]
  vi.resetModules()
})

describe('gateway registry HMR migration', () => {
  it('adds all seven lease/reconnect fields and initializes old secondaries lacking retryStopped (test 38)', async () => {
    // An OLD container: the original six fields only, plus a live secondary that
    // predates retryStopped.
    const oldSecondary = {
      gateway: { close: () => undefined },
      offEvent: () => undefined,
      offState: () => undefined,
      profile: 'apollo',
      reconnectAttempt: 0,
      reconnectTimer: null,
      reconnecting: false,
      wantOpen: true
    }

    const old = {
      $gateway: atom(null),
      activeKey: 'default',
      config: null,
      primaryGateway: null,
      primaryProfile: 'default',
      secondaries: new Map([['apollo', oldSecondary]])
    }

    plantContainer(old as unknown as Registry)

    // Re-import: the fresh module's gatewayState() finds the old container and
    // migrates it in place.
    await import('@/store/gateway')

    const migrated = readContainer() as Registry
    expect(migrated).toBe(old) // same object — migrated in place, not replaced

    // All seven new fields now present with the right shapes.
    expect(migrated.leasedProfiles).toBeInstanceOf(Map)
    expect(migrated.profileOpens).toBeInstanceOf(Map)
    expect(migrated.pendingLeases).toBeInstanceOf(Set)
    expect(migrated.leasePruneTimer).toBeNull()
    expect(migrated.lastKnownKeepSet).toBeInstanceOf(Set)
    expect(migrated.reauthErrors).toBeInstanceOf(Map)
    expect(migrated.primaryReconnect).toBeNull()

    // The pre-existing secondary gained retryStopped.
    expect((oldSecondary as { retryStopped?: unknown }).retryStopped).toBeNull()
  })

  it('preserves live lease state across a hot re-eval (test 17)', async () => {
    // A CURRENT container already carrying lease + keep-set state.
    const leasedProfiles = new Map<string, number>([
      ['apollo', 2],
      ['nova', 1]
    ])
    const lastKnownKeepSet = new Set<string>(['nova'])

    const container = {
      $gateway: atom(null),
      activeKey: 'default',
      config: null,
      lastKnownKeepSet,
      leasePruneTimer: null,
      leasedProfiles,
      pendingLeases: new Set<string>(),
      primaryGateway: null,
      primaryReconnect: null,
      primaryProfile: 'default',
      profileOpens: new Map<string, Promise<void>>(),
      reauthErrors: new Map<string, unknown>(),
      secondaries: new Map()
    }

    plantContainer(container as unknown as Registry)

    await import('@/store/gateway')

    // The fresh module reused the container (??= never clobbers), so the lease
    // refcounts and keep-set survive the hot update.
    const migrated = readContainer() as Registry
    expect(migrated).toBe(container)
    expect((migrated.leasedProfiles as Map<string, number>).get('apollo')).toBe(2)
    expect((migrated.leasedProfiles as Map<string, number>).get('nova')).toBe(1)
    expect((migrated.lastKnownKeepSet as Set<string>).has('nova')).toBe(true)
  })

  it('creates a fresh, fully-initialized container when none exists', async () => {
    // No planted container (deleted in afterEach / absent here).
    delete (globalThis as unknown as Record<symbol, unknown>)[STATE_KEY]

    await import('@/store/gateway')

    const created = readContainer() as Registry
    expect(created).toBeDefined()
    expect(created.leasedProfiles).toBeInstanceOf(Map)
    expect(created.reauthErrors).toBeInstanceOf(Map)
    expect(created.primaryReconnect).toBeNull()
  })
})
