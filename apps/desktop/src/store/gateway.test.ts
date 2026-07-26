import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { HermesGateway } from '@/hermes'
import {
  __resetGatewayRegistryForTests,
  configureGatewayRegistry,
  ensureGatewayForProfile,
  isActivePrimary,
  leaseProfileGateway,
  primaryProfileKey,
  pruneSecondaryGateways,
  reconnectPrimaryGateway,
  releaseProfileGateway,
  requestGatewayForProfile,
  setPrimaryGateway,
  updateGatewayKeepSet,
  withProfileGatewayLease
} from '@/store/gateway'
import { $connection } from '@/store/session'

// Drive the real HermesGateway through a fake WebSocket we fully control. The
// socket auto-opens AND echoes a JSON-RPC result for every request, so lease /
// open / prune / profile-RPC behavior runs against the genuine connect()+request
// path without a real port.

type Listener = (ev: unknown) => void

class FakeWebSocket {
  static OPEN = 1
  static CLOSED = 3
  static mode: 'fail' | 'open' = 'open'
  static instances: FakeWebSocket[] = []

  readyState = 0
  private listeners: Record<string, Set<Listener>> = {}

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
    const willOpen = FakeWebSocket.mode === 'open'
    setTimeout(() => {
      if (willOpen) {
        this.readyState = FakeWebSocket.OPEN
        this.emit('open', {})
      } else {
        this.readyState = FakeWebSocket.CLOSED
        this.emit('error', {})
      }
    }, 0)
  }

  addEventListener(type: string, fn: Listener) {
    ;(this.listeners[type] ??= new Set()).add(fn)
  }

  removeEventListener(type: string, fn: Listener) {
    this.listeners[type]?.delete(fn)
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', {})
  }

  // Echo a successful result so pending requests resolve.
  send(raw: string) {
    let id: unknown
    try {
      id = (JSON.parse(raw) as { id?: unknown }).id
    } catch {
      return
    }

    if (id === undefined) {
      return
    }

    setTimeout(() => {
      this.emit('message', { data: JSON.stringify({ id, result: { ok: true } }) })
    }, 0)
  }

  private emit(type: string, ev: unknown) {
    for (const fn of this.listeners[type] ?? []) {
      fn(ev)
    }
  }
}

const originalWebSocket = globalThis.WebSocket
const getConnection = vi.fn(async (profile?: string) => ({
  authMode: 'token' as const,
  baseUrl: 'http://localhost',
  profile: profile ?? 'default',
  token: 't',
  wsUrl: `ws://localhost/ws?profile=${profile ?? 'default'}`
}))

const opensFor = (profile: string) => getConnection.mock.calls.filter(([p]) => (p ?? 'default') === profile).length

async function flush(cycles = 2) {
  for (let i = 0; i < cycles; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await vi.advanceTimersByTimeAsync(0)
  }
}

// Drive fake timers until an in-flight RPC (which awaits a fake-socket response
// timer) settles. Advancing timers must interleave with the request promise —
// awaiting the promise first would deadlock against the timers that resolve it.
async function settle<T>(promise: Promise<T>, cycles = 20): Promise<T> {
  for (let i = 0; i < cycles; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await vi.advanceTimersByTimeAsync(0)
  }

  return promise
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeWebSocket.mode = 'open'
  FakeWebSocket.instances = []
  getConnection.mockClear()
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
    getConnection,
    getGatewayWsUrl: vi.fn(async () => 'ws://localhost/ws'),
    touchBackend: vi.fn(async () => undefined)
  }
  __resetGatewayRegistryForTests()
  configureGatewayRegistry({ onEvent: () => undefined })
})

afterEach(() => {
  __resetGatewayRegistryForTests()
  vi.useRealTimers()
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

describe('requestGatewayForProfile — primary reconnect extraction (blocker #2)', () => {
  it('two concurrent primary reconnects share ONE mint and never publish the connection while a secondary is active (test 54)', async () => {
    // Primary backend booted as "default" but idle (never connected).
    const primary = new HermesGateway()
    setPrimaryGateway(primary, 'default')

    // Make a secondary ("apollo") the ACTIVE gateway. ensureGatewayForProfile
    // awaits the secondary's open timer, so drive timers while awaiting it.
    await settle(ensureGatewayForProfile('apollo'))
    expect(isActivePrimary()).toBe(false)

    const sentinel = { mode: 'local', wsUrl: 'ws://sentinel' }
    $connection.set(sentinel as never)

    // Two concurrent primary reconnects while apollo is active. Both must share
    // the single in-flight g.primaryReconnect (dedup) and resolve to the primary.
    const [g1, g2] = await settle(
      Promise.all([reconnectPrimaryGateway('default'), reconnectPrimaryGateway('default')])
    )

    expect(g1).toBe(primary)
    expect(g2).toBe(primary)
    expect(primary.connectionState).toBe('open')

    // Deduped: exactly one reconnect minted a connection for the primary.
    expect(opensFor('default')).toBe(1)
    // The foreground connection was NOT clobbered (activeKey is apollo, so the
    // background primary reconnect must not call setConnection).
    expect($connection.get()).toBe(sentinel)

    primary.close()
  })
})

describe('primary event tagging source (blocker #1)', () => {
  it('primaryProfileKey tracks the registry primary, not the active secondary (test 53)', async () => {
    const primary = new HermesGateway()
    setPrimaryGateway(primary, 'default')
    expect(primaryProfileKey()).toBe('default')

    // Activate a secondary: the active key moves, but the primary key the boot
    // hook reads at EMIT time must stay the primary's profile — otherwise primary
    // events get tagged with the secondary's profile after a rebuild.
    await settle(ensureGatewayForProfile('apollo'))
    expect(isActivePrimary()).toBe(false)
    expect(primaryProfileKey()).toBe('default')

    // A rebuild re-sets the primary; the emit-time read follows the registry's
    // primary key, never the active secondary.
    setPrimaryGateway(primary, 'default')
    expect(primaryProfileKey()).toBe('default')

    primary.close()
  })
})

describe('requestGatewayForProfile — secondary routing (test 16)', () => {
  it('routes an RPC to a leased secondary without changing the active gateway', async () => {
    const primary = new HermesGateway()
    setPrimaryGateway(primary, 'default')
    expect(isActivePrimary()).toBe(true)

    leaseProfileGateway('apollo')
    await flush()

    const result = await settle(requestGatewayForProfile('apollo', 'pet.info', { profile: 'apollo' }))

    expect(result).toEqual({ ok: true })
    expect(opensFor('apollo')).toBe(1)
    // Active gateway unchanged — still the primary.
    expect(isActivePrimary()).toBe(true)

    primary.close()
  })
})

describe('lease lifecycle', () => {
  it('a leased idle profile survives an empty-keep prune; releasing prunes within 50ms (tests 6, 36)', async () => {
    leaseProfileGateway('apollo')
    await flush()
    expect(opensFor('apollo')).toBe(1)

    // Empty keep-set prune must spare the leased profile.
    pruneSecondaryGateways(new Set())
    expect(opensFor('apollo')).toBe(1)

    // Release → debounced prune. Before 50ms a re-lease still reuses the socket.
    releaseProfileGateway('apollo')
    await vi.advanceTimersByTimeAsync(30)
    leaseProfileGateway('apollo')
    await flush()
    expect(opensFor('apollo')).toBe(1) // survived: re-leased before the prune landed

    // Release for good; the 50ms prune now drops it and a re-lease opens fresh.
    releaseProfileGateway('apollo')
    await vi.advanceTimersByTimeAsync(60)
    leaseProfileGateway('apollo')
    await flush()
    expect(opensFor('apollo')).toBe(2)
  })

  it('a lease+release burst settles to one debounced prune and never a negative refcount (test 37)', async () => {
    leaseProfileGateway('apollo')
    leaseProfileGateway('nova')
    await flush()
    expect(opensFor('apollo')).toBe(1)
    expect(opensFor('nova')).toBe(1)

    // Burst: release both, plus a stray extra release (must not go negative).
    releaseProfileGateway('apollo')
    releaseProfileGateway('nova')
    releaseProfileGateway('apollo')

    // Before the 50ms debounce lands, nothing is pruned yet; after, both dropped.
    await vi.advanceTimersByTimeAsync(30)
    await vi.advanceTimersByTimeAsync(60)
    leaseProfileGateway('apollo')
    leaseProfileGateway('nova')
    await flush()
    expect(opensFor('apollo')).toBe(2)
    expect(opensFor('nova')).toBe(2)
  })

  it('withProfileGatewayLease releases in finally even when the work throws (test 7)', async () => {
    await expect(
      withProfileGatewayLease('apollo', async () => {
        throw new Error('boom')
      })
    ).rejects.toThrow('boom')

    // Lease released in finally → prune drops it; a fresh lease re-opens.
    await vi.advanceTimersByTimeAsync(60)
    leaseProfileGateway('apollo')
    await flush()
    expect(opensFor('apollo')).toBe(2)
  })

  it('concurrent leases for the same profile open a single socket and need every ref released (test 39)', async () => {
    leaseProfileGateway('apollo')
    leaseProfileGateway('apollo')
    await flush()

    // Two refcounts, ONE deduped open.
    expect(opensFor('apollo')).toBe(1)

    // First release leaves refcount 1 → no prune; socket survives an empty prune.
    releaseProfileGateway('apollo')
    await vi.advanceTimersByTimeAsync(60)
    pruneSecondaryGateways(new Set())
    expect(opensFor('apollo')).toBe(1)

    // Second release hits zero → prune drops it; re-lease opens fresh.
    releaseProfileGateway('apollo')
    await vi.advanceTimersByTimeAsync(60)
    leaseProfileGateway('apollo')
    await flush()
    expect(opensFor('apollo')).toBe(2)
  })

  it('a lease acquired before the registry is configured is queued and drained on boot (test 8)', async () => {
    // Drop the registry config to simulate pre-boot, lease, then re-configure.
    __resetGatewayRegistryForTests()
    leaseProfileGateway('preboot')
    await flush()
    // No registry yet → queued, not opened.
    expect(opensFor('preboot')).toBe(0)

    configureGatewayRegistry({ onEvent: () => undefined })
    await flush()
    expect(opensFor('preboot')).toBe(1)
  })

  it('a failed leased open keeps the lease (not pruned) and schedules a bounded retry (test 9)', async () => {
    FakeWebSocket.mode = 'fail'
    leaseProfileGateway('apollo')
    await flush()

    // The open failed, but the lease is retained: an empty-keep prune must NOT
    // drop it. A wake/backoff tick nudges another open attempt.
    pruneSecondaryGateways(new Set())
    const attemptsBefore = opensFor('apollo')
    expect(attemptsBefore).toBeGreaterThanOrEqual(1)

    await vi.advanceTimersByTimeAsync(15_000)
    expect(opensFor('apollo')).toBeGreaterThan(attemptsBefore)
  })

  it('updateGatewayKeepSet spares live-work profiles from a lease-triggered prune', async () => {
    leaseProfileGateway('apollo')
    await flush()
    releaseProfileGateway('apollo')

    // Publish live work naming apollo BEFORE the debounce lands.
    updateGatewayKeepSet(new Set(['apollo']))
    await vi.advanceTimersByTimeAsync(60)

    // The lease-triggered prune used the keep-set → apollo survived; re-lease reuses.
    leaseProfileGateway('apollo')
    await flush()
    expect(opensFor('apollo')).toBe(1)
  })
})
