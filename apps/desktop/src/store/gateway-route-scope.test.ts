import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Dev-HMR migration for the exact-route snapshot atom: a container parked on
// globalThis by an OLDER module generation predates $route, so a re-eval of
// store/gateway must reconstruct it from the container's own state instead of
// exporting an undefined atom (which would crash every $activeGatewayScope
// subscriber on the first hot update after this feature landed).

vi.mock('@/hermes', () => ({
  HermesGateway: class {},
  setApiRequestConnection: vi.fn()
}))
vi.mock('@/store/session', () => ({
  setConnection: vi.fn(),
  setGatewayState: vi.fn()
}))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))

const STATE_KEY = Symbol.for('hermes.desktop.gatewayRegistryState')

// A pre-$route container shape: everything the module touches at import time
// EXCEPT the snapshot atom under test.
function plantLegacyState(overrides: Record<string, unknown> = {}): void {
  ;(globalThis as unknown as Record<symbol, unknown>)[STATE_KEY] = {
    activationEpoch: 2,
    activeKey: 'default',
    config: null,
    openedSecondaryScopes: new Set<string>(),
    primaryConnectionId: null,
    primaryGateway: null,
    primaryProfile: 'default',
    secondaries: new Map(),
    turnLeaseReleaseTimers: new Map(),
    turnLeases: new Map(),
    $activeProfile: atom('default'),
    $gateway: atom(null),
    ...overrides
  }
}

async function importGateway(): Promise<typeof import('./gateway')> {
  vi.resetModules()

  return await import('./gateway')
}

afterEach(() => {
  delete (globalThis as unknown as Record<symbol, unknown>)[STATE_KEY]
  vi.resetModules()
})

describe('$activeGatewayScope HMR migration', () => {
  it('reconstructs the primary route for a container that predates the snapshot atom', async () => {
    plantLegacyState({ primaryConnectionId: 'homelab' })

    const mod = await importGateway()

    expect(mod.$activeGatewayScope.get()).toEqual({ connectionId: 'homelab', profile: 'default' })
    // One atom instance, handed back on every read.
    expect(mod.$activeGatewayScope.get()).toBe(mod.$activeGatewayScope.get())
  })

  it('falls back to the boot-published descriptor when the primary has no registry id yet', async () => {
    plantLegacyState({
      config: { activeConnectionId: () => 'homelab-ssh' },
      primaryConnectionId: null
    })

    const mod = await importGateway()

    expect(mod.$activeGatewayScope.get()).toEqual({ connectionId: 'homelab-ssh', profile: 'default' })
  })

  it('reconstructs a secondary (registry agent) route from the active scope entry', async () => {
    plantLegacyState({
      activeKey: 'conn:homelab::loki',
      secondaries: new Map([['conn:homelab::loki', { connectionId: 'homelab', profile: 'loki' }]])
    })

    const mod = await importGateway()

    expect(mod.$activeGatewayScope.get()).toEqual({ connectionId: 'homelab', profile: 'loki' })
  })

  it('yields a local-pool route when nothing better is known, and the atom stays live', async () => {
    plantLegacyState()

    const mod = await importGateway()

    expect(mod.$activeGatewayScope.get()).toEqual({ connectionId: null, profile: 'default' })

    // The reconstructed atom is the same instance future publications write to.
    mod.setPrimaryGatewayConnectionId('work-vps')

    expect(mod.$activeGatewayScope.get()).toEqual({ connectionId: 'work-vps', profile: 'default' })
  })
})
