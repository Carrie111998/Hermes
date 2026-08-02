import assert from 'node:assert/strict'

import { test } from 'vitest'

import { restartLocalBackend } from './backend-restart'

test('local restart waits for teardown before starting a new backend', async () => {
  let releaseTeardown!: () => void

  const teardownDone = new Promise<void>(resolve => {
    releaseTeardown = resolve
  })

  const events: string[] = []

  const restart = restartLocalBackend({
    teardown: async () => {
      events.push('teardown-start')
      await teardownDone
      events.push('teardown-end')
    },
    start: async () => {
      events.push('start')
      assert.deepEqual(events, ['teardown-start', 'teardown-end', 'start'])
    },
    notifyApplied: () => events.push('applied')
  })

  await Promise.resolve()
  assert.deepEqual(events, ['teardown-start'])
  releaseTeardown()

  assert.deepEqual(await restart, { ok: true, mode: 'local' })
  assert.deepEqual(events, ['teardown-start', 'teardown-end', 'start', 'applied'])
})

test('local restart reports startup failure and leaves reconnect notification available', async () => {
  let applied = 0

  const result = await restartLocalBackend({
    teardown: async () => {},
    start: async () => {
      throw new Error('backend did not become ready')
    },
    notifyApplied: () => {
      applied += 1
    }
  })

  assert.deepEqual(result, {
    ok: false,
    reason: 'restart-failed',
    message: 'backend did not become ready'
  })
  assert.equal(applied, 1)
})
