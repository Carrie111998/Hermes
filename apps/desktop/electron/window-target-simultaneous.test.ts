import assert from 'node:assert/strict'

import { test } from 'vitest'

import { makeBackendTarget } from './backend-target'
import { poolRouteForTarget } from './pool-keys'
import { resolveSenderRequestTarget } from './sender-target-routing'
import { createWindowTargetRegistry } from './window-target-registry'

test('simultaneous windows preserve independent configured and forced-local backend identities', async () => {
  const registry = createWindowTargetRegistry({
    resolvePrimaryTarget: () => makeBackendTarget({ kind: 'primary' }),
  })

  registry.bind(101, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  registry.bind(202, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))

  const requests = [
    { senderId: 101, request: { path: '/api/sessions?profile=worker' } },
    { senderId: 202, request: { path: '/api/sessions?profile=coder' } },
  ]

  const routes = await Promise.all(
    requests.map(async ({ senderId, request }) => {
      const senderTarget = registry.lookup(senderId)
      const resolved = resolveSenderRequestTarget(senderTarget, request)

      return poolRouteForTarget(resolved.target)
    })
  )

  assert.deepEqual(routes, [
    { route: 'configured', profile: 'worker', key: 'configured-profile:worker' },
    { route: 'forced-local', profile: 'coder', key: 'forced-local-profile:coder' },
  ])
  assert.notEqual(routes[0].key, routes[1].key)
})
