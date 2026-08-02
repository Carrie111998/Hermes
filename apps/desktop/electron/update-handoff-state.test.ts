import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isUpdateOperationBusy, updateHandoffExitMode } from './update-handoff-state'

test('the backend gate stays closed after detached handoff starts', () => {
  assert.equal(isUpdateOperationBusy({ updateInFlight: false, handoffInFlight: true }), true)
  assert.equal(isUpdateOperationBusy({ updateInFlight: true, handoffInFlight: false }), true)
  assert.equal(isUpdateOperationBusy({ updateInFlight: false, handoffInFlight: false }), false)
})

test('a handoff latch prevents a second updater after the first spawn', () => {
  const state = { updateInFlight: false, handoffInFlight: true }

  assert.equal(isUpdateOperationBusy(state), true)
  assert.equal(isUpdateOperationBusy(state), true)
})

test('Windows handoff uses a hard exit while POSIX keeps graceful quit', () => {
  assert.equal(updateHandoffExitMode(true), 'hard')
  assert.equal(updateHandoffExitMode(false), 'graceful')
})
