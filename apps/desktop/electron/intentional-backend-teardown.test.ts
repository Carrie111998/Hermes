import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createBackendConnectionState } from './backend-connection-state'
import {
  createIntentionalBackendTeardownGuard,
  shouldSuppressBackendExitNotice
} from './intentional-backend-teardown'

test('intentional update/uninstall teardown suppresses the backend-exit notice', () => {
  assert.equal(
    shouldSuppressBackendExitNotice({ softRehomeInProgress: false, intentionalTeardownDepth: 1 }),
    true
  )
})

test('an unexpected current-backend exit is not suppressed', () => {
  // Current-owner exit still reaches sendBackendExit; suppression must stay off
  // so a real crash/stop surfaces to the user.
  assert.equal(
    shouldSuppressBackendExitNotice({ softRehomeInProgress: false, intentionalTeardownDepth: 0 }),
    false
  )
})

test('soft re-home still suppresses independently of the update guard', () => {
  assert.equal(
    shouldSuppressBackendExitNotice({ softRehomeInProgress: true, intentionalTeardownDepth: 0 }),
    true
  )
})

test('guard depth covers the kill window and releases afterward', async () => {
  const guard = createIntentionalBackendTeardownGuard()
  let suppressedDuringKill = false

  await guard.run(async () => {
    suppressedDuringKill = shouldSuppressBackendExitNotice({
      softRehomeInProgress: false,
      intentionalTeardownDepth: guard.depth
    })
  })

  assert.equal(suppressedDuringKill, true)
  assert.equal(guard.depth, 0)
  assert.equal(
    shouldSuppressBackendExitNotice({
      softRehomeInProgress: false,
      intentionalTeardownDepth: guard.depth
    }),
    false
  )
})

test('nested intentional teardowns stay suppressed until the outermost ends', async () => {
  const guard = createIntentionalBackendTeardownGuard()

  await guard.run(async () => {
    assert.equal(guard.depth, 1)
    await guard.run(async () => {
      assert.equal(guard.depth, 2)
      assert.equal(
        shouldSuppressBackendExitNotice({
          softRehomeInProgress: false,
          intentionalTeardownDepth: guard.depth
        }),
        true
      )
    })
    assert.equal(guard.depth, 1)
  })

  assert.equal(guard.depth, 0)
})

test('update lock-release covers a late exit via invalidate even after the guard ends', () => {
  // Models releaseBackendLock: capture owner → invalidate → kill → guard ends →
  // delayed 'exit'. clearForCurrentProcess must fail so sendBackendExit is never
  // reached, even with intentionalTeardownDepth already back at 0.
  type FakeProcess = { id: string }
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()

  state.setPromise(attempt, Promise.resolve('primary'))
  const owner = state.attachProcess(attempt, { id: 'primary' })
  assert.ok(owner)

  state.invalidate()

  assert.equal(state.clearForCurrentProcess(owner), false)
  assert.equal(
    shouldSuppressBackendExitNotice({ softRehomeInProgress: false, intentionalTeardownDepth: 0 }),
    false
  )
})

test('current-owner exit during the intentional guard is suppressed at sendBackendExit', async () => {
  // Models a straggler/respawn killed while releaseBackendLock's guard is held:
  // clearForCurrentProcess would succeed, so suppression must come from depth.
  type FakeProcess = { id: string }
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()

  state.setPromise(attempt, Promise.resolve('straggler'))
  const owner = state.attachProcess(attempt, { id: 'straggler' })
  assert.ok(owner)

  const guard = createIntentionalBackendTeardownGuard()
  let wouldToast = true

  await guard.run(async () => {
    assert.equal(state.clearForCurrentProcess(owner), true)
    wouldToast = !shouldSuppressBackendExitNotice({
      softRehomeInProgress: false,
      intentionalTeardownDepth: guard.depth
    })
  })

  assert.equal(wouldToast, false)
})

test('unexpected current-owner exit still reaches a toastable sendBackendExit path', () => {
  type FakeProcess = { id: string }
  const state = createBackendConnectionState<FakeProcess, string>()
  const attempt = state.startAttempt()

  state.setPromise(attempt, Promise.resolve('primary'))
  const owner = state.attachProcess(attempt, { id: 'primary' })
  assert.ok(owner)

  assert.equal(state.clearForCurrentProcess(owner), true)
  assert.equal(
    shouldSuppressBackendExitNotice({ softRehomeInProgress: false, intentionalTeardownDepth: 0 }),
    false
  )
})
