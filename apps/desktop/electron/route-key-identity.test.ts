/**
 * Phase 1 — RouteKey generation-bound identity (Magnum #94724 §3.1 §11 §21).
 * Adversarial tests that would have been impossible before generation existed.
 */
import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  connectionDialFieldsChanged,
  type ConnectionRegistry,
  LOCAL_CONNECTION_ID,
  normalizeConnectionInput,
  normalizeRegistry,
  REGISTRY_VERSION,
} from './connection-registry'
import {
  asConnectionId,
  asProfileKey,
  descriptorScopeMatchesRoute,
  isRouteKeyCurrent,
  makeRouteKey,
  routeKeyPartitionKey,
  routeKeyScopeKey,
} from './connection-route-identity'

function registryWith(overrides: Partial<ConnectionRegistry> = {}): ConnectionRegistry {
  const base: ConnectionRegistry = {
    version: REGISTRY_VERSION,
    primary: LOCAL_CONNECTION_ID,
    launchMode: 'primary',
    lastUsed: LOCAL_CONNECTION_ID,
    connections: [{ id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device', generation: 1 }],
    ...overrides,
  }

  return normalizeRegistry(base)
}

test('RouteKey is generation-bound: stale generation never equals current', () => {
  const registry = normalizeRegistry({
    version: REGISTRY_VERSION,
    primary: 'homelab',
    launchMode: 'primary',
    lastUsed: 'homelab',
    connections: [
      { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device' },
      { id: 'homelab', kind: 'remote', label: 'Homelab', url: 'https://homelab.example', authMode: 'token', token: { encoding: 'plain', value: 't' } },
    ],
  })

  // New registry defaults generation to 1
  const conn = registry.connections.find(c => c.id === 'homelab')!
  assert.equal(conn.generation, 1)
  const key = makeRouteKey(conn, 'default')
  assert.equal(key.generation, 1)
  assert.equal(isRouteKeyCurrent(registry, key), true)

  // Simulate edit that bumps generation to 2
  const bumped: ConnectionRegistry = {
    ...registry,
    connections: registry.connections.map(c => (c.id === 'homelab' ? { ...c, generation: 2 } : c)),
  }

  assert.equal(isRouteKeyCurrent(bumped, key), false)
  // A key minted at gen 2 is current again
  const fresh = bumped.connections.find(c => c.id === 'homelab')!
  const key2 = makeRouteKey(fresh, 'default')
  assert.equal(key2.generation, 2)
  assert.equal(isRouteKeyCurrent(bumped, key2), true)
})

test('makeRouteKey separates desktopProfile vs targetProfile on SSH', () => {
  const ssh = {
    id: 'lab-ssh',
    kind: 'ssh' as const,
    label: 'Lab',
    host: 'lab.example',
    user: 'alice',
    remoteProfile: 'research',
    generation: 3,
  }

  const key = makeRouteKey(ssh as never, 'assistant')
  assert.equal((key.connectionId as string), 'lab-ssh')
  assert.equal((key.desktopProfile as string), 'assistant')
  assert.equal((key.targetProfile as string), 'research')
  assert.equal(key.generation, 3)
  assert.equal(routeKeyScopeKey(key), 'conn:lab-ssh::assistant')
})

test('makeRouteKey for local reuses bare profile key', () => {
  const local = { id: LOCAL_CONNECTION_ID, kind: 'local' as const, label: 'This device', generation: 1 }
  const key = makeRouteKey(local as never, 'default')
  assert.equal(routeKeyScopeKey(key), 'default')
})

test('normalizeRegistry preserves and defaults generation', () => {
  const raw = {
    version: REGISTRY_VERSION,
    primary: 'remote-a',
    launchMode: 'primary',
    lastUsed: 'remote-a',
    connections: [
      { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device', generation: 5 },
      { id: 'remote-a', kind: 'remote', label: 'A', url: 'https://a.example', generation: 2 },
      { id: 'remote-b', kind: 'remote', label: 'B', url: 'https://b.example' }, // missing → default 1
    ],
  }

  const reg = normalizeRegistry(raw as never)
  assert.equal(reg.connections.find(c => c.id === LOCAL_CONNECTION_ID)?.generation, 5)
  assert.equal(reg.connections.find(c => c.id === 'remote-a')?.generation, 2)
  assert.equal(reg.connections.find(c => c.id === 'remote-b')?.generation, 1)
})

test('generation bump invalidates prior isRouteKeyCurrent (simulates main.ts save)', () => {
  const reg = normalizeRegistry({
    version: REGISTRY_VERSION,
    primary: 'homelab',
    launchMode: 'primary',
    lastUsed: 'homelab',
    connections: [
      { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device' },
      { id: 'homelab', kind: 'remote', label: 'Homelab', url: 'https://homelab.example', authMode: 'token', token: { encoding: 'plain', value: 'old' } },
    ],
  })

  const before = reg.connections.find(c => c.id === 'homelab')!
  const oldKey = makeRouteKey(before, 'default')

  // dial material change → generation should bump
  const after = normalizeConnectionInput(
    { id: 'homelab', kind: 'remote', label: 'Homelab', url: 'https://homelab.example', authMode: 'token', token: { encoding: 'plain', value: 'new-token' } },
    reg,
  )

  // normalizeConnectionInput preserves generation (does not auto-bump); the bump happens in saveRegistryConnection
  assert.equal(after.generation, before.generation)
  assert.equal(connectionDialFieldsChanged(before, after), true)
  // Simulate the saveRegistryConnection bump
  const bumped = { ...after, generation: (before.generation ?? 1) + 1 }
  const bumpedReg: ConnectionRegistry = { ...reg, connections: reg.connections.map(c => (c.id === 'homelab' ? bumped : c)) }
  assert.equal(isRouteKeyCurrent(bumpedReg, oldKey), false)
  assert.equal(isRouteKeyCurrent(bumpedReg, makeRouteKey(bumped, 'default')), true)
})

test('branded ids do not alias across connections even with same profile string', () => {
  const a = asConnectionId('homelab')
  const b = asConnectionId('homelab-2')
  assert.notEqual(a as string, b as string)
  assert.equal(asProfileKey('default') as string, 'default')
  assert.equal(asProfileKey('') as string, 'default')
})

// The activation gate rejects a descriptor scoped to a THIRD profile, but must
// not reject the two legitimate re-scopings — otherwise SSH remotes and the
// shared primary stop activating entirely.
test('descriptorScopeMatchesRoute accepts the legitimate re-scopings', () => {
  const registry = normalizeRegistry({
    version: REGISTRY_VERSION,
    primary: LOCAL_CONNECTION_ID,
    launchMode: 'primary',
    lastUsed: LOCAL_CONNECTION_ID,
    connections: [
      { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device', generation: 1 },
      {
        id: 'homelab',
        kind: 'ssh',
        label: 'Homelab',
        host: 'homelab.example',
        remoteProfile: 'work',
        generation: 1,
      },
    ],
  })

  const ssh = makeRouteKey(registry.connections.find(c => c.id === 'homelab')!, 'default')

  // SSH maps desktopProfile -> remoteProfile; the descriptor may advertise either.
  assert.equal(ssh.targetProfile as string, 'work')
  assert.equal(descriptorScopeMatchesRoute({ profile: 'work' }, ssh), true)
  assert.equal(descriptorScopeMatchesRoute({ profile: 'default' }, ssh), true)

  // Shared primary intentionally advertises the primary's descriptorProfile.
  assert.equal(descriptorScopeMatchesRoute({ profile: 'anything', sharedPrimary: true }, ssh), true)

  // No profile of its own -> inherits the requested scope.
  assert.equal(descriptorScopeMatchesRoute({}, ssh), true)
  assert.equal(descriptorScopeMatchesRoute(null, ssh), true)

  // A third profile means the dial handed back another route's backend.
  assert.equal(descriptorScopeMatchesRoute({ profile: 'research' }, ssh), false)
})

test('partition key is generation-bound while the pool key is not', () => {
  const registry = normalizeRegistry({
    version: REGISTRY_VERSION,
    primary: LOCAL_CONNECTION_ID,
    launchMode: 'primary',
    lastUsed: LOCAL_CONNECTION_ID,
    connections: [
      { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device', generation: 1 },
      { id: 'homelab', kind: 'remote', label: 'Homelab', url: 'https://homelab.example', generation: 1 },
    ],
  })
  const gen1 = makeRouteKey(registry.connections.find(c => c.id === 'homelab')!, 'default')
  const gen2 = { ...gen1, generation: 2 }

  // Pool slots are per (connection, profile): a bump re-dials the SAME slot.
  assert.equal(routeKeyScopeKey(gen1), routeKeyScopeKey(gen2))
  // Partitions are per authority: a bump must NOT inherit the old gateway data.
  assert.notEqual(routeKeyPartitionKey(gen1), routeKeyPartitionKey(gen2))
})
