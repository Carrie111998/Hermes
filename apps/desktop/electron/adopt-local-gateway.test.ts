import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  DEFAULT_LOCAL_GATEWAY_BASE_URLS,
  gatewayPidLooksLive,
  parseGatewayPidRecord,
  shouldAdoptExistingLocalGateway,
  tryAdoptExistingLocalGateway
} from './adopt-local-gateway'

test('parseGatewayPidRecord reads JSON pid records from gateway.pid', () => {
  assert.deepEqual(parseGatewayPidRecord('{"pid":14504,"kind":"gateway"}'), { pid: 14504 })
  assert.deepEqual(parseGatewayPidRecord('  42  '), { pid: 42 })
  assert.equal(parseGatewayPidRecord(''), null)
  assert.equal(parseGatewayPidRecord('{'), null)
  assert.equal(parseGatewayPidRecord('{"pid":"nope"}'), null)
})

test('gatewayPidLooksLive rejects non-positive pids even if the probe lies', () => {
  assert.equal(gatewayPidLooksLive(0, () => true), false)
  assert.equal(gatewayPidLooksLive(-1, () => true), false)
  assert.equal(gatewayPidLooksLive(22, () => true), true)
  assert.equal(gatewayPidLooksLive(22, () => false), false)
})

test('shouldAdoptExistingLocalGateway requires a live pid AND HTTP', () => {
  assert.equal(shouldAdoptExistingLocalGateway({ pidLive: true, httpReachable: true }), true)
  assert.equal(shouldAdoptExistingLocalGateway({ pidLive: true, httpReachable: false }), false)
  assert.equal(shouldAdoptExistingLocalGateway({ pidLive: false, httpReachable: true }), false)
})

test('tryAdoptExistingLocalGateway skips spawn when the scheduled gateway is live and HTTP-ready', async () => {
  const adopted = await tryAdoptExistingLocalGateway({
    readPidFile: () => '{"pid":14504,"kind":"gateway"}',
    pidExists: pid => pid === 14504,
    probeHttp: async url => url === 'http://127.0.0.1:8642'
  })

  assert.equal(adopted, 'http://127.0.0.1:8642')
})

test('tryAdoptExistingLocalGateway falls through to spawn when the pid file is stale', async () => {
  const adopted = await tryAdoptExistingLocalGateway({
    readPidFile: () => '{"pid":14504}',
    pidExists: () => false,
    probeHttp: async () => true
  })

  assert.equal(adopted, null)
})

test('tryAdoptExistingLocalGateway falls through when HTTP is down even if pid is live', async () => {
  const adopted = await tryAdoptExistingLocalGateway({
    readPidFile: () => '{"pid":7}',
    pidExists: () => true,
    probeHttp: async () => false,
    baseUrls: [...DEFAULT_LOCAL_GATEWAY_BASE_URLS]
  })

  assert.equal(adopted, null)
})
