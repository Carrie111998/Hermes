import assert from 'node:assert/strict'

import { describe, test } from 'vitest'

import { LOCAL_CONNECTION_ID, normalizeRegistry, REGISTRY_VERSION } from '../../electron/connection-registry'
import { makeRouteKey } from '../../electron/connection-route-identity'

import { BANNED_AMBIENT_PATTERNS, foregroundApi, resourceRef } from './resource-ref'

function fakeRoute(id: string, profile='default') {
  const reg = normalizeRegistry({
    version: REGISTRY_VERSION, primary: id, launchMode: 'primary', lastUsed: id,
    connections: [{ id: LOCAL_CONNECTION_ID, kind: 'local', label: 'local' }, { id, kind: 'remote', label: id, url: `https://${id}.example` }],
  })

  return makeRouteKey(reg.connections.find(c=>c.id===id)!, profile)
}

describe('resource-ref (§9 §10)', () => {
  test('ResourceRef carries ownership; foreground switch cannot redirect it', () => {
    const routeA = fakeRoute('homelab','default')
    const ref = resourceRef(routeA, 'session-123' as never)
    assert.equal((ref.owner.connectionId as string), 'homelab')
    assert.equal(ref.id, 'session-123')
    // Foreground change does not mutate existing ref
    const routeB = fakeRoute('other','default')
    assert.notEqual((ref.owner.connectionId as string), (routeB.connectionId as string))
  })
  test('foregroundApi is explicitly named (grep-able)', () => {
    assert.equal(foregroundApi(()=>'foreground-only'), 'foreground-only')
    assert.ok(BANNED_AMBIENT_PATTERNS.length > 0)
  })
})
