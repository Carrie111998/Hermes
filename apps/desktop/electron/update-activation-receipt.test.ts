import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { writeActivationFailureReceipt, writeActivationReceipt } from './update-activation-receipt'

test('writeActivationReceipt durably records the verified first-boot terminal state', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-activation-receipt-'))
  const receiptPath = path.join(root, 'updates', 'activation-receipt.json')

  const receipt = writeActivationReceipt(receiptPath, {
    httpReady: true,
    websocketReady: true,
    profile: 'worker'
  }, { now: () => new Date('2026-07-27T19:00:00.000Z') })

  assert.deepEqual(receipt, {
    stage: 'activation',
    state: 'completed',
    verifiedAt: '2026-07-27T19:00:00.000Z',
    httpReady: true,
    websocketReady: true,
    profile: 'worker'
  })
  assert.deepEqual(JSON.parse(fs.readFileSync(receiptPath, 'utf8')), receipt)
  assert.equal(fs.existsSync(`${receiptPath}.tmp`), false)
})

test('writeActivationFailureReceipt durably records that first-boot failed without rollback', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-activation-failure-'))
  const receiptPath = path.join(root, 'updates', 'activation-receipt.json')

  const receipt = writeActivationFailureReceipt(
    receiptPath,
    {
      error: 'WebSocket readiness probe failed.',
      httpReady: true,
      websocketReady: false,
      rollbackState: 'not-attempted'
    },
    { now: () => new Date('2026-07-27T19:01:00.000Z') }
  )

  assert.deepEqual(receipt, {
    stage: 'activation',
    state: 'failed',
    verifiedAt: '2026-07-27T19:01:00.000Z',
    error: 'WebSocket readiness probe failed.',
    httpReady: true,
    websocketReady: false,
    rollbackState: 'not-attempted'
  })
  assert.deepEqual(JSON.parse(fs.readFileSync(receiptPath, 'utf8')), receipt)
  assert.equal(fs.existsSync(`${receiptPath}.tmp`), false)
})
