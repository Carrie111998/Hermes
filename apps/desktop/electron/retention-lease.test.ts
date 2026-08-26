/**
 * Phase 2 — Retention leases adversarial tests (#94724 §8 §21)
 */
import assert from 'node:assert/strict'

import { describe, test } from 'vitest'

import { LOCAL_CONNECTION_ID, normalizeRegistry, REGISTRY_VERSION } from './connection-registry'
import { makeRouteKey } from './connection-route-identity'
import type { RouteKey } from './connection-route-identity'
import { RetentionRegistry } from './retention-lease'

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

describe('RetentionRegistry (§8)', () => {
  test('a tile-lease keeps the route alive even when foreground moves', () => {
    const r = new RetentionRegistry()
    const route = fakeRoute('homelab', 'research')
    const lease = r.acquire(route, { kind: 'bot-tile', ownerId: 'tile:42' })
    assert.equal(r.countFor(route), 1)
    assert.equal(r.mayPrune(route, { isForeground: false }), false)
    lease.release()
    assert.equal(r.countFor(route), 0)
    assert.equal(r.mayPrune(route, { isForeground: false }), true)
  })

  test('multiple owners are counted independently; release one keeps the route', () => {
    const r = new RetentionRegistry()
    const route = fakeRoute('homelab')
    const a = r.acquire(route, { kind: 'terminal-pane', ownerId: 'term:abc' })
    const b = r.acquire(route, { kind: 'active-turn', ownerId: 'turn:91' })
    assert.equal(r.countFor(route), 2)
    a.release()
    assert.equal(r.countFor(route), 1)
    assert.equal(r.mayPrune(route), false)
    b.release()
    assert.equal(r.mayPrune(route), true)
  })

  test('mayPrune is false when activeRequests or activeTurns are nonzero (§8 criteria)', () => {
    const r = new RetentionRegistry()
    const route = fakeRoute('homelab')
    assert.equal(r.mayPrune(route, { activeRequests: 1 }), false)
    assert.equal(r.mayPrune(route, { activeTurns: 1 }), false)
    assert.equal(r.mayPrune(route, { isForeground: true }), false)
    assert.equal(r.mayPrune(route, {}), true)
  })

  test('ownersFor is observable: why is this gateway still alive?', () => {
    const r = new RetentionRegistry()
    const route = fakeRoute('homelab')
    r.acquire(route, { kind: 'bot-tile', ownerId: 'tile:1' })
    r.acquire(route, { kind: 'background-job', ownerId: 'job:7' })
    const owners = r.ownersFor(route)
    assert.equal(owners.length, 2)
    assert.ok(owners.some(o => o.ownerId === 'tile:1'))
  })

  test('release is idempotent', () => {
    const r = new RetentionRegistry()
    const route = fakeRoute('homelab')
    const lease = r.acquire(route, { kind: 'bot-tile', ownerId: 'tile:1' })
    assert.equal(lease.release(), true)
    assert.equal(lease.release(), false)
    assert.equal(r.countFor(route), 0)
  })
})
