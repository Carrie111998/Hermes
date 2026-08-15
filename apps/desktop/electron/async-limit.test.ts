import assert from 'node:assert/strict'
import { performance } from 'node:perf_hooks'

import { test } from 'vitest'

import { createAsyncLimiter } from './async-limit'

test('createAsyncLimiter caps peak work across every caller sharing it', async () => {
  let active = 0
  let peak = 0
  let release!: () => void

  const gate = new Promise<void>(resolve => {
    release = resolve
  })

  const limit = createAsyncLimiter(3)

  const tasks = Array.from({ length: 10 }, (_, index) =>
    limit(async () => {
      active += 1
      peak = Math.max(peak, active)
      await gate
      active -= 1

      return index
    })
  )

  await Promise.resolve()
  await Promise.resolve()
  assert.equal(active, 3)

  release()
  assert.deepEqual(await Promise.all(tasks), Array.from({ length: 10 }, (_, index) => index))
  assert.equal(peak, 3)
})

test('a rejected task releases its limiter slot', async () => {
  const limit = createAsyncLimiter(1)

  const failed = limit(async () => {
    throw new Error('expected')
  })

  const succeeded = limit(async () => 'ok')

  await assert.rejects(failed, /expected/)
  assert.equal(await succeeded, 'ok')
})

test('large queued fan-out drains without quadratic front-array compaction', async () => {
  const limit = createAsyncLimiter(4)
  const startedAt = performance.now()
  const tasks = Array.from({ length: 100_000 }, (_, index) => limit(() => index))

  assert.deepEqual(await Promise.all(tasks), Array.from({ length: 100_000 }, (_, index) => index))
  assert.ok(performance.now() - startedAt < 5_000)
}, 20_000)
