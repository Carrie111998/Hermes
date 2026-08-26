import assert from 'node:assert/strict'

import { describe, test } from 'vitest'

import { LOCAL_CONNECTION_ID, normalizeRegistry, REGISTRY_VERSION } from '../../electron/connection-registry'
import { makeRouteKey } from '../../electron/connection-route-identity'

import { RoutePartitions } from './route-partition'

function fakeRoute(id: string, profile='default', gen=1) {
  const reg = normalizeRegistry({
    version: REGISTRY_VERSION, primary: id, launchMode: 'primary', lastUsed: id,
    connections: [{ id: LOCAL_CONNECTION_ID, kind: 'local', label: 'local' }, { id, kind: 'remote', label:id, url:`https://${id}.example`, generation: gen }],
  })

  return makeRouteKey(reg.connections.find(c=>c.id===id)!, profile)
}

describe('RoutePartitions (§12 §13)', () => {
  test('forRoute partitions by route; foreground switch is pointer swap', () => {
    const parts = new RoutePartitions(()=>({ sessions:[] as string[] }))
    const a = fakeRoute('homelab','default')
    const b = fakeRoute('other','default')
    parts.forRoute(a).data.sessions.push('s-a')
    assert.equal(parts.forRoute(a).data.sessions.length, 1)
    assert.equal(parts.forRoute(b).data.sessions.length, 0)
    // Same route returns same partition (LRU touch)
    assert.equal(parts.forRoute(a).data.sessions[0], 's-a')
  })
  test('same id different profiles are different partitions', () => {
    const parts = new RoutePartitions(()=>({ v:0 }))
    const r1 = fakeRoute('homelab','default')
    const r2 = fakeRoute('homelab','research')
    parts.forRoute(r1).data.v = 1
    assert.equal(parts.forRoute(r2).data.v, 0)
  })
  test('bounded: LRU evicts oldest when over maxPartitions', () => {
    const parts = new RoutePartitions(()=>({ v:0 }), { maxPartitions:2 })
    parts.forRoute(fakeRoute('a'))
    parts.forRoute(fakeRoute('b'))
    parts.forRoute(fakeRoute('c'))
    assert.equal(parts.size(), 2)
    assert.ok(!parts.keys().includes('conn:a::default'))
  })

  // A generation bump is a new gateway authority. Keying partitions on the
  // pool key alone would hand it the previous gateway's data and a stale
  // gen-1 `partition.route`.
  test('a generation bump gets a fresh partition, not the prior gateway data', () => {
    const parts = new RoutePartitions(() => ({ sessions: [] as string[] }))
    const gen1 = fakeRoute('homelab', 'default', 1)
    const gen2 = fakeRoute('homelab', 'default', 2)

    parts.forRoute(gen1).data.sessions.push('from-gen-1')

    const fresh = parts.forRoute(gen2)
    assert.deepEqual(fresh.data.sessions, [])
    assert.equal(fresh.route.generation, 2)
    assert.notEqual(fresh.scopeKey, parts.forRoute(gen1).scopeKey)
  })
})
