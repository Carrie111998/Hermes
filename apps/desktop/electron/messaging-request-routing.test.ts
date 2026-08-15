import assert from 'node:assert/strict'

import { test } from 'vitest'

import { requireMessagingRequestProfile } from './messaging-request-routing'

test('rejects a messaging settings request without a profile', () => {
  assert.throws(
    () => requireMessagingRequestProfile('/api/messaging/platforms/telegram', undefined),
    /explicit profile/i
  )
})

test('normalizes an explicit messaging settings profile', () => {
  assert.equal(requireMessagingRequestProfile('/api/messaging/platforms/telegram', ' specialist '), 'specialist')
})

test('keeps unrelated primary requests backward compatible', () => {
  assert.equal(requireMessagingRequestProfile('/api/status', undefined), undefined)
})
