import assert from 'node:assert/strict'
import { test, describe } from 'vitest'
import { RoutePartitions } from './route-partition'
import { makeRouteKey } from '../../electron/connection-route-identity'
import { LOCAL_CONNECTION_ID, REGISTRY_VERSION, normalizeRegistry } from '../../electron/connection-registry'

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
})
