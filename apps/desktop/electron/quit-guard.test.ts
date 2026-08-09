import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createQuitTeardownBarrier, mergeActiveWork, normalizeActiveWork, quitPromptFor } from './quit-guard'

test('normalizeActiveWork drops junk and keeps the count at least the title count', () => {
  assert.deepEqual(normalizeActiveWork(null), { count: 0, titles: [] })
  assert.deepEqual(normalizeActiveWork({ count: 'many', titles: 'nope' }), { count: 0, titles: [] })
  assert.deepEqual(normalizeActiveWork({ count: -3, titles: ['  Fix login  ', '', 7] }), {
    count: 1,
    titles: ['Fix login']
  })
})

test('normalizeActiveWork keeps untitled sessions in the count', () => {
  assert.deepEqual(normalizeActiveWork({ count: 3, titles: ['Fix login'] }), { count: 3, titles: ['Fix login'] })
})

test('mergeActiveWork de-dupes a session two windows both report', () => {
  const merged = mergeActiveWork([
    { count: 2, titles: ['Fix login', 'Ship docs'] },
    { count: 1, titles: ['Fix login'] }
  ])

  assert.deepEqual(merged, { count: 2, titles: ['Fix login', 'Ship docs'] })
})

test('quitPromptFor stays out of the way when nothing is running', () => {
  assert.equal(quitPromptFor({ count: 0, titles: [] }, false), null)
})

test('quitPromptFor stays out of the way during an update handoff', () => {
  assert.equal(quitPromptFor({ count: 2, titles: ['Fix login'] }, true), null)
})

test('quitPromptFor names the running chats', () => {
  const prompt = quitPromptFor({ count: 2, titles: ['Fix login', 'Ship docs'] }, false)

  assert.ok(prompt)
  assert.equal(prompt.message, 'Hermes is still working on 2 chats.')
  assert.ok(prompt.detail.includes('• Fix login'))
  assert.ok(prompt.detail.includes('• Ship docs'))
})

test('quitPromptFor summarizes past the list cap and counts untitled work', () => {
  const prompt = quitPromptFor({ count: 9, titles: ['a', 'b', 'c', 'd', 'e', 'f'] }, false)

  assert.ok(prompt)
  assert.equal(prompt.message, 'Hermes is still working on 9 chats.')
  assert.ok(prompt.detail.includes('• d'))
  assert.ok(!prompt.detail.includes('• e'))
  assert.ok(prompt.detail.includes('• 5 more'))
})

test('quitPromptFor speaks singular for one chat', () => {
  const prompt = quitPromptFor({ count: 1, titles: [] }, false)

  assert.ok(prompt)
  assert.equal(prompt.message, 'Hermes is still working on 1 chat.')
  assert.ok(prompt.detail.includes('mid-turn'))
})

test('one quit barrier waits for backend and SSH teardown before one retry', async () => {
  let releaseBackend!: () => void
  let releaseSsh!: () => void
  let retries = 0
  let duplicateRuns = 0

  const backend = new Promise<void>(resolve => {
    releaseBackend = resolve
  })

  const ssh = new Promise<void>(resolve => {
    releaseSsh = resolve
  })

  const barrier = createQuitTeardownBarrier()

  const first = barrier.start(async () => {
    await Promise.all([backend, ssh])
  }, () => {
    retries += 1
  })

  const second = barrier.start(async () => {
    duplicateRuns += 1
  }, () => {
    retries += 1
  })

  assert.equal(first, second)
  assert.equal(barrier.started, true)
  assert.equal(barrier.pending, true)

  releaseBackend()
  await Promise.resolve()
  assert.equal(barrier.pending, true)
  assert.equal(retries, 0)

  releaseSsh()
  await first
  assert.equal(barrier.done, true)
  assert.equal(barrier.pending, false)
  assert.equal(duplicateRuns, 0)
  assert.equal(retries, 1)
})

test('one quit barrier retains a removed pool start until it settles', async () => {
  let releaseStart!: () => void
  let retries = 0

  const start = new Promise<void>(resolve => {
    releaseStart = resolve
  })

  const barrier = createQuitTeardownBarrier()

  barrier.track(start)

  const teardown = barrier.start(async () => {
    // The start was removed from its pool registry before this quit began.
  }, () => {
    retries += 1
  })

  await Promise.resolve()
  assert.equal(barrier.pending, true)
  assert.equal(retries, 0)

  releaseStart()
  await teardown
  assert.equal(retries, 1)
})
