import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { createWindowCloseBarrier } from './window-close-barrier'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

test('waits for a pending controller debounce flush before answering a close request', async () => {
  let onRequest: ((requestId: string) => void) | undefined
  const responses: Array<{ allowed: boolean; requestId: string }> = []
  const barrier = createWindowCloseBarrier({
    onRequest: listener => {
      onRequest = listener

      return () => {
        onRequest = undefined
      }
    },
    resolve: (requestId, allowed) => responses.push({ allowed, requestId })
  })
  const save = deferred<boolean>()

  barrier.register(() => save.promise)
  barrier.install()
  onRequest?.('pending-save')

  await Promise.resolve()
  assert.deepEqual(responses, [])

  save.resolve(true)
  await vi.waitFor(() => assert.deepEqual(responses, [{ allowed: true, requestId: 'pending-save' }]))
})

test('vetoes a close request when any registered controller reports a conflict', async () => {
  let onRequest: ((requestId: string) => void) | undefined
  const responses: Array<{ allowed: boolean; requestId: string }> = []
  const barrier = createWindowCloseBarrier({
    onRequest: listener => {
      onRequest = listener

      return () => undefined
    },
    resolve: (requestId, allowed) => responses.push({ allowed, requestId })
  })

  barrier.register(async () => false)
  barrier.install()
  onRequest?.('conflict')

  await vi.waitFor(() => assert.deepEqual(responses, [{ allowed: false, requestId: 'conflict' }]))
})

test('answers immediately when the window has no live controllers', async () => {
  let onRequest: ((requestId: string) => void) | undefined
  const responses: Array<{ allowed: boolean; requestId: string }> = []
  const barrier = createWindowCloseBarrier({
    onRequest: listener => {
      onRequest = listener

      return () => undefined
    },
    resolve: (requestId, allowed) => responses.push({ allowed, requestId })
  })

  barrier.install()
  onRequest?.('empty')

  await vi.waitFor(() => assert.deepEqual(responses, [{ allowed: true, requestId: 'empty' }]))
})

test('keeps an unregistering controller in the in-flight close request until its flush settles', async () => {
  let onRequest: ((requestId: string) => void) | undefined
  const responses: Array<{ allowed: boolean; requestId: string }> = []
  const barrier = createWindowCloseBarrier({
    onRequest: listener => {
      onRequest = listener

      return () => undefined
    },
    resolve: (requestId, allowed) => responses.push({ allowed, requestId })
  })
  const save = deferred<boolean>()
  const unregister = barrier.register(() => save.promise)

  barrier.install()
  onRequest?.('in-flight')
  unregister()
  save.resolve(true)

  await vi.waitFor(() => assert.deepEqual(responses, [{ allowed: true, requestId: 'in-flight' }]))

  onRequest?.('after-unregister')
  await vi.waitFor(() =>
    assert.deepEqual(responses, [
      { allowed: true, requestId: 'in-flight' },
      { allowed: true, requestId: 'after-unregister' }
    ])
  )
})
