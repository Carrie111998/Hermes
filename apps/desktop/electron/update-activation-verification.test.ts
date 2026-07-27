import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { pathsReferToSameLocation, verifyFirstBootReadiness } from './update-activation-verification'

test('profile-home comparison is case-insensitive and separator-stable on Windows', () => {
  assert.equal(
    pathsReferToSameLocation(
      'C:\\Users\\Auron\\AppData\\Local\\hermes\\profiles\\AGY',
      'c:/users/auron/AppData/Local/Hermes/profiles/agy/',
      true
    ),
    true
  )
  assert.equal(pathsReferToSameLocation('/srv/Hermes', '/srv/hermes', false), false)
})

test('first-boot verification requires both HTTP and WebSocket readiness before success', async () => {
  const events: string[] = []

  const receipt = await verifyFirstBootReadiness({
    waitForHttp: async () => {
      events.push('http')
    },
    probeWebSocket: async () => {
      events.push('websocket')

      return { ok: true }
    },
    verifyProfile: async () => {
      events.push('profile')

      return { ok: true, profile: 'worker' }
    }
  })

  assert.deepEqual(events, ['http', 'websocket', 'profile'])
  assert.deepEqual(receipt, {
    ok: true,
    stage: 'first-boot',
    httpReady: true,
    websocketReady: true,
    profileReady: true,
    profile: 'worker'
  })
})

test('first-boot verification rejects a backend serving the wrong profile', async () => {
  const receipt = await verifyFirstBootReadiness({
    waitForHttp: vi.fn(async () => {}),
    probeWebSocket: vi.fn(async () => ({ ok: true })),
    verifyProfile: vi.fn(async () => ({ ok: false, reason: 'expected research, received default' }))
  })

  assert.deepEqual(receipt, {
    ok: false,
    stage: 'first-boot',
    httpReady: true,
    websocketReady: true,
    profileReady: false,
    error: 'expected research, received default'
  })
})

test('first-boot verification returns a terminal receipt when WebSocket readiness fails', async () => {
  const receipt = await verifyFirstBootReadiness({
    waitForHttp: vi.fn(async () => {}),
    probeWebSocket: vi.fn(async () => ({ ok: false, reason: 'upgrade rejected' }))
  })

  assert.deepEqual(receipt, {
    ok: false,
    stage: 'first-boot',
    httpReady: true,
    websocketReady: false,
    error: 'upgrade rejected'
  })
})

test('first-boot verification does not probe WebSocket after HTTP readiness fails', async () => {
  const probeWebSocket = vi.fn(async () => ({ ok: true }))

  const receipt = await verifyFirstBootReadiness({
    waitForHttp: async () => {
      throw new Error('backend unavailable')
    },
    probeWebSocket
  })

  assert.deepEqual(receipt, {
    ok: false,
    stage: 'first-boot',
    httpReady: false,
    websocketReady: false,
    error: 'backend unavailable'
  })
  assert.equal(probeWebSocket.mock.calls.length, 0)
})
