import assert from 'node:assert/strict'

import { test } from 'vitest'

import { requireUpdateGateCompletion, waitForUpdateGate } from './update-wait'

const liveMarker = { pid: 4242, ageMs: 1_000 }

test('update wait returns an explicit skipped terminal receipt when no update is active', async () => {
  const receipt = await waitForUpdateGate('C:/Hermes', {
    readMarker: () => null,
    now: () => 0,
    sleep: async () => {},
    onProgress: async () => {},
    log: () => {}
  })

  assert.deepEqual(receipt, { stage: 'update-wait', status: 'skipped' })
})

test('update wait returns completed only after the live marker disappears', async () => {
  const markers = [liveMarker, liveMarker, null]

  const receipt = await waitForUpdateGate('C:/Hermes', {
    readMarker: () => markers.shift() ?? null,
    now: () => 0,
    sleep: async () => {},
    onProgress: async () => {},
    log: () => {},
    timeoutMs: 10,
    pollMs: 1
  })

  assert.deepEqual(receipt, {
    stage: 'update-wait',
    status: 'completed',
    updaterPid: 4242
  })
})

test('update wait exposes timeout as a distinct terminal receipt instead of completion', async () => {
  let now = 0
  const logs: string[] = []

  const receipt = await waitForUpdateGate('C:/Hermes', {
    readMarker: () => liveMarker,
    now: () => now,
    sleep: async ms => {
      now += ms
    },
    onProgress: async () => {},
    log: message => logs.push(message),
    timeoutMs: 2,
    pollMs: 1
  })

  assert.deepEqual(receipt, {
    stage: 'update-wait',
    status: 'timed-out',
    updaterPid: 4242
  })
  assert.equal(logs.at(-1), 'update still in progress after wait timeout; refusing backend startup')
})

test('a timed-out update gate cannot be treated as permission to start the backend', () => {
  assert.throws(
    () => requireUpdateGateCompletion({ stage: 'update-wait', status: 'timed-out', updaterPid: 4242 }),
    /updater process 4242 is still active/
  )

  assert.doesNotThrow(() => requireUpdateGateCompletion({ stage: 'update-wait', status: 'skipped' }))
  assert.doesNotThrow(() =>
    requireUpdateGateCompletion({ stage: 'update-wait', status: 'completed', updaterPid: 4242 })
  )
})
