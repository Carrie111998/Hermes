import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createBackendConnectionState, invalidateAndStopBackendAttempt } from './backend-connection-state'

type FakeProcess = { id: string }

test('a stale backend exit cannot clear a newer connection attempt', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const oldAttempt = state.startAttempt()
  const oldPromise = Promise.resolve('old')

  state.setPromise(oldAttempt, oldPromise)
  const oldOwner = state.attachProcess(oldAttempt, { id: 'old' })
  assert.ok(oldOwner)

  state.invalidate()

  const newAttempt = state.startAttempt()
  const newPromise = Promise.resolve('new')
  const newProcess = { id: 'new' }

  state.setPromise(newAttempt, newPromise)
  assert.ok(state.attachProcess(newAttempt, newProcess))

  assert.equal(state.clearForCurrentProcess(oldOwner), false)
  assert.equal(state.getProcess(), newProcess)
  assert.equal(state.getPromise(), newPromise)
})

test('the current backend exit clears its process and connection promise', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()

  state.setPromise(attempt, Promise.resolve('current'))
  const owner = state.attachProcess(attempt, { id: 'current' })
  assert.ok(owner)

  assert.equal(state.clearForCurrentProcess(owner), true)
  assert.equal(state.clearPromiseForAttempt(attempt), true)
  assert.equal(state.getProcess(), null)
  assert.equal(state.getPromise(), null)
})

test('a stale rejected attempt cannot clear a newer connection promise', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const oldAttempt = state.startAttempt()

  state.setPromise(oldAttempt, Promise.resolve('old'))
  state.invalidate()

  const newAttempt = state.startAttempt()
  const newPromise = Promise.resolve('new')

  state.setPromise(newAttempt, newPromise)

  assert.equal(state.clearPromiseForAttempt(oldAttempt), false)
  assert.equal(state.getPromise(), newPromise)
})

test('an invalidated attempt cannot attach a late-spawned process', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const staleAttempt = state.startAttempt()

  state.invalidate()

  assert.equal(state.attachProcess(staleAttempt, { id: 'late' }), null)
  assert.equal(state.getProcess(), null)
})

test('an invalidated attempt is observable before a delayed spawn begins', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const staleAttempt = state.startAttempt()

  assert.equal(state.isCurrentAttempt(staleAttempt), true)
  state.invalidate()
  assert.equal(state.isCurrentAttempt(staleAttempt), false)
})

test('invalidation during an awaited startup gate prevents the delayed spawn', async () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()
  let releaseGate!: () => void

  const gate = new Promise<void>(resolve => {
    releaseGate = resolve
  })

  let spawns = 0

  const startup = (async () => {
    await gate

    if (!state.isCurrentAttempt(attempt)) {
      throw new Error('cancelled')
    }

    spawns += 1
  })()

  state.invalidate()
  releaseGate()

  await assert.rejects(startup, /cancelled/)
  assert.equal(spawns, 0)
})

test('invalidateSnapshot returns the exact pending promise and process before clearing state', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()
  const promise = Promise.resolve('pending')
  const process = { id: 'pending' }

  state.setPromise(attempt, promise)
  assert.ok(state.attachProcess(attempt, process))

  assert.deepEqual(state.invalidateSnapshot(), { process, promise })
  assert.equal(state.getProcess(), null)
  assert.equal(state.getPromise(), null)
  assert.equal(state.isCurrentAttempt(attempt), false)
})

test('invalidateAttempt returns the child owned by the failing current attempt', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()
  const process = { id: 'failed-start' }

  state.setPromise(attempt, Promise.resolve('pending'))
  assert.ok(state.attachProcess(attempt, process))

  assert.deepEqual(state.invalidateAttempt(attempt), { process, promise: attempt.promise })
  assert.equal(state.getProcess(), null)
  assert.equal(state.getPromise(), null)
})

test('invalidateAttempt leaves a newer attempt and child untouched', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const stale = state.startAttempt()
  state.invalidate()

  const current = state.startAttempt()
  const process = { id: 'current' }
  const promise = Promise.resolve('current')
  state.setPromise(current, promise)
  assert.ok(state.attachProcess(current, process))

  assert.equal(state.invalidateAttempt(stale), null)
  assert.equal(state.getProcess(), process)
  assert.equal(state.getPromise(), promise)
})

test('failed-attempt cleanup invalidates and stops only the child owned by that attempt', () => {
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()
  const process = { id: 'failed-start' }
  const stopped: FakeProcess[] = []

  state.setPromise(attempt, Promise.resolve('pending'))
  assert.ok(state.attachProcess(attempt, process))

  assert.equal(invalidateAndStopBackendAttempt(state, attempt, child => stopped.push(child)), true)
  assert.deepEqual(stopped, [process])
  assert.equal(state.getProcess(), null)
  assert.equal(state.getPromise(), null)

  const current = state.startAttempt()
  const currentProcess = { id: 'current' }
  state.setPromise(current, Promise.resolve('current'))
  assert.ok(state.attachProcess(current, currentProcess))

  assert.equal(invalidateAndStopBackendAttempt(state, attempt, child => stopped.push(child)), false)
  assert.deepEqual(stopped, [process])
  assert.equal(state.getProcess(), currentProcess)
})
