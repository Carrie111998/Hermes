/**
 * Phase 2 — GatewaySupervisor adversarial tests (#94724 §4 §5 §6 §7 §21)
 */
import assert from 'node:assert/strict'

import { describe, test } from 'vitest'

import { LOCAL_CONNECTION_ID, normalizeRegistry, REGISTRY_VERSION } from './connection-registry'
import { makeRouteKey, type RouteKey } from './connection-route-identity'
import { canTransition, GatewaySupervisor, isActivationGateOpen, type TransportHandle } from './gateway-supervisor'

/** A transport whose gate conditions all hold, so tests opt IN to failure. */
function handle(over: Partial<TransportHandle<string>> = {}): TransportHandle<string> {
  return {
    gatewayEpoch: 'e1',
    socketInstanceId: 's1',
    descriptor: 'descriptor',
    gatewayReady: true,
    targetProfileMatches: true,
    ...over,
  }
}

function fakeRoute(id: string, profile = 'default', generation = 1): RouteKey {
  const registry = normalizeRegistry({
    version: REGISTRY_VERSION,
    primary: id,
    launchMode: 'primary',
    lastUsed: id,
    connections: [
      { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device', generation: 1 },
      { id, kind: 'remote', label: id, url: `https://${id}.example`, generation },
    ],
  })

  const conn = registry.connections.find(c => c.id === id)!

  return makeRouteKey(conn, profile)
}

describe('GatewaySupervisor state machine (§6)', () => {
  test('Dormant → Resolving → Provisioning → Dialing → Handshaking → Live is the happy path', () => {
    const s = new GatewaySupervisor({ activateTransport: async () => handle({ gatewayEpoch: 'e1', socketInstanceId: 's1' }), isRouteCurrent: () => true })
    const route = fakeRoute('homelab')
    assert.equal(s.stateFor(route), 'Dormant')
    assert.equal(s.transition(route, 'Resolving'), true)
    assert.equal(s.transition(route, 'Provisioning'), true)
    assert.equal(s.transition(route, 'Dialing'), true)
    assert.equal(s.transition(route, 'Handshaking'), true)
    assert.equal(s.transition(route, 'Live'), true)
  })

  test('illegal transition is rejected (no boolean explosion)', () => {
    const s = new GatewaySupervisor({ activateTransport: async () => handle({ gatewayEpoch: 'e1', socketInstanceId: 's1' }), isRouteCurrent: () => true })
    const route = fakeRoute('homelab')
    assert.equal(s.transition(route, 'Live'), false)
    assert.equal(canTransition('Dormant', 'Live'), false)
    assert.equal(canTransition('Live', 'Resolving'), false)
  })

  test('WebSocket.OPEN alone never means route ready (§7) — gate requires all fields', () => {
    assert.equal(isActivationGateOpen({ transportOpen: true, gatewayReady: false, generationCurrent: true, targetProfileMatches: true, gatewayEpochKnown: true }), false)
    assert.equal(isActivationGateOpen({ transportOpen: true, gatewayReady: true, generationCurrent: true, targetProfileMatches: true, gatewayEpochKnown: false }), false)
    assert.equal(isActivationGateOpen({ transportOpen: true, gatewayReady: true, generationCurrent: true, targetProfileMatches: true, gatewayEpochKnown: true }), true)
  })
})

describe('GatewaySupervisor single-flight (§5) — multi-renderer race', () => {
  test('two concurrent activates for the same (connectionId,generation,profile) coalesce to ONE dial', async () => {
    let dials = 0

    const s = new GatewaySupervisor({
      activateTransport: async () => {
        dials += 1
        await new Promise<void>(resolve => setTimeout(resolve, 15))

        return handle({ gatewayEpoch: 'epoch-1', socketInstanceId: 'sock-1' })
      },
      isRouteCurrent: () => true,
    })

    const route = fakeRoute('homelab', 'default', 1)
    const [a, b] = await Promise.all([s.activate(route), s.activate(route)])
    assert.equal(dials, 1)
    assert.equal(a.status, 'activated')
    assert.equal(b.status, 'activated')

    // Second caller receives the SAME lease (coalesced)
    if (a.status === 'activated' && b.status === 'activated') {
      assert.equal(a.lease.socketInstanceId, b.lease.socketInstanceId)
    }
  })

  test('different generations do NOT coalesce (generation is part of the key)', async () => {
    let dials = 0

    const s = new GatewaySupervisor({
      activateTransport: async () => {
        dials += 1

        return handle({ gatewayEpoch: 'e', socketInstanceId: `sock-${dials}` })
      },
      isRouteCurrent: () => true,
    })

    const r1 = fakeRoute('homelab', 'default', 1)
    const r2 = fakeRoute('homelab', 'default', 2)
    const [a, b] = await Promise.all([s.activate(r1), s.activate(r2)])
    assert.equal(dials, 2)
    assert.equal(a.status, 'activated')
    assert.equal(b.status, 'activated')
  })

  test('generation bump during dial → stale caller resolves as superseded, never publishes (§3.1)', async () => {
    let currentGen = 1

    const s = new GatewaySupervisor({
      activateTransport: async () => {
        await new Promise<void>(r => setTimeout(r, 10))

        return handle({ gatewayEpoch: 'e', socketInstanceId: 'sock' })
      },
      isRouteCurrent: route => route.generation === currentGen,
    })

    const oldRoute = fakeRoute('homelab', 'default', 1)
    const pending = s.activate(oldRoute)
    // Edit bumps the slot from 1 → 2 before the dial resolves
    currentGen = 2
    const receipt = await pending
    assert.equal(receipt.status, 'superseded')
  })

  test('failed dial releases the flight so a later retry can run (bounded, not latched)', async () => {
    let attempt = 0

    const s = new GatewaySupervisor({
      activateTransport: async () => {
        attempt += 1

        if (attempt === 1) {throw new Error('transient offline')}

        return handle({ gatewayEpoch: 'e', socketInstanceId: 'sock' })
      },
      isRouteCurrent: () => true,
    })

    const route = fakeRoute('homelab')
    const first = await s.activate(route)
    assert.equal(first.status, 'offline')
    const second = await s.activate(route)
    assert.equal(second.status, 'activated')
    assert.equal(attempt, 2)
  })

  test('reconnect is an alias for activate under the same single-flight', async () => {
    let dials = 0

    const s = new GatewaySupervisor({
      activateTransport: async () => {
        dials += 1

        return handle({ gatewayEpoch: 'e', socketInstanceId: 'sock' })
      },
      isRouteCurrent: () => true,
    })

    const route = fakeRoute('homelab')
    const [a, b] = await Promise.all([s.activate(route), s.reconnect(route)])
    assert.equal(dials, 1)
    assert.equal(a.status, 'activated')
    assert.equal(b.status, 'activated')
  })
})

describe('GatewaySupervisor lease (§3.3)', () => {
  test('successful activation mints a RouteLease bound to the route and epoch', async () => {
    const s = new GatewaySupervisor({
      activateTransport: async () => handle({ gatewayEpoch: 'gw-epoch-99', socketInstanceId: 'sock-abc' }),
      isRouteCurrent: () => true,
    })

    const route = fakeRoute('homelab', 'research', 1)
    const receipt = await s.activate(route)
    assert.equal(receipt.status, 'activated')

    if (receipt.status === 'activated') {
      assert.equal(receipt.lease.route, route)
      assert.equal(receipt.lease.gatewayEpoch, 'gw-epoch-99')
      assert.equal(receipt.lease.socketInstanceId, 'sock-abc')
      assert.equal(s.leaseFor(route)?.activationEpoch, receipt.lease.activationEpoch)
    }
  })
})

describe('GatewaySupervisor activation gate (§7) — open transport is not enough', () => {
  test('a dial that resolves without a ready gateway does not publish', async () => {
    const s = new GatewaySupervisor<string>({
      activateTransport: async () => handle({ gatewayReady: false }),
      isRouteCurrent: () => true,
    })

    const route = fakeRoute('homelab')
    const receipt = await s.activate(route)
    assert.equal(receipt.status, 'offline')
    assert.equal(s.leaseFor(route), null)
  })

  test('a descriptor for the wrong target profile does not publish', async () => {
    const s = new GatewaySupervisor<string>({
      activateTransport: async () => handle({ targetProfileMatches: false }),
      isRouteCurrent: () => true,
    })

    const receipt = await s.activate(fakeRoute('homelab', 'research'))
    assert.equal(receipt.status, 'offline')

    if (receipt.status === 'offline') {
      assert.match(String(receipt.reason), /targetProfileMatches/)
    }
  })

  test('the dialed descriptor rides the lease, so it cannot be read cross-route', async () => {
    const s = new GatewaySupervisor<string>({
      activateTransport: async route => handle({ descriptor: `backend-for-${route.connectionId}` }),
      isRouteCurrent: () => true,
    })

    const [a, b] = await Promise.all([s.activate(fakeRoute('homelab')), s.activate(fakeRoute('cloudbox'))])
    assert.equal(a.status, 'activated')
    assert.equal(b.status, 'activated')

    if (a.status === 'activated' && b.status === 'activated') {
      assert.equal(a.lease.descriptor, 'backend-for-homelab')
      assert.equal(b.lease.descriptor, 'backend-for-cloudbox')
    }
  })

  test('a per-call dial overrides the default transport for that call site', async () => {
    const s = new GatewaySupervisor<string>({
      activateTransport: async () => handle({ descriptor: 'default-transport' }),
      isRouteCurrent: () => true,
    })

    const receipt = await s.activate(fakeRoute('homelab'), async () => handle({ descriptor: 'caller-supplied' }))
    assert.equal(receipt.status, 'activated')

    if (receipt.status === 'activated') {
      assert.equal(receipt.lease.descriptor, 'caller-supplied')
    }
  })
})
