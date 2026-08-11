import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createAppCloseBarrier,
  createRendererCloseCoordinator,
  installWindowCloseBarrier,
  RENDERER_CLOSE_REQUEST_CHANNEL
} from './window-close-barrier'

type CloseListener = (event: { preventDefault: () => void }) => void

function makeWindow(id = 1) {
  const listeners = new Map<string, Set<CloseListener>>()
  const sent: Array<{ channel: string; payload: unknown }> = []
  let destroyed = false
  let closeCalls = 0

  const window = {
    close() {
      closeCalls += 1
      emitClose()
    },
    isDestroyed: () => destroyed,
    on(event: string, listener: CloseListener) {
      const eventListeners = listeners.get(event) ?? new Set<CloseListener>()
      eventListeners.add(listener)
      listeners.set(event, eventListeners)
    },
    removeListener(event: string, listener: CloseListener) {
      listeners.get(event)?.delete(listener)
    },
    webContents: {
      id,
      send(channel: string, payload: unknown) {
        sent.push({ channel, payload })
      }
    }
  }

  function emitClose() {
    let prevented = false
    for (const listener of listeners.get('close') ?? []) {
      listener({
        preventDefault: () => {
          prevented = true
        }
      })
    }

    if (!prevented) {
      destroyed = true
    }

    return prevented
  }

  return {
    emitClose,
    get closeCalls() {
      return closeCalls
    },
    get destroyed() {
      return destroyed
    },
    sent,
    window
  }
}

async function nextTurn(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
}

test('rearms app-close flushing after a downstream quit guard delays the retry', async () => {
  let flushes = 0
  let retries = 0
  let prevented = 0
  const barrier = createAppCloseBarrier({
    requestFlush: async () => {
      flushes += 1

      return true
    },
    retryClose: () => {
      retries += 1
    }
  })
  const event = {
    preventDefault: () => {
      prevented += 1
    }
  }

  assert.equal(barrier.hold(event), true)
  await nextTurn()
  assert.equal(barrier.isPermitted(), true)
  assert.deepEqual({ flushes, prevented, retries }, { flushes: 1, prevented: 1, retries: 1 })

  barrier.rearm()
  assert.equal(barrier.hold(event), true)
  await nextTurn()
  assert.deepEqual({ flushes, prevented, retries }, { flushes: 2, prevented: 2, retries: 2 })
})

test('vetoes a window close when the renderer reports a conflicted flush', async () => {
  const fake = makeWindow()
  const coordinator = createRendererCloseCoordinator()

  installWindowCloseBarrier(fake.window, { requestFlush: () => coordinator.request(fake.window) })

  assert.equal(fake.emitClose(), true)
  assert.deepEqual(fake.sent, [{ channel: RENDERER_CLOSE_REQUEST_CHANNEL, payload: { requestId: '1' } }])
  assert.equal(coordinator.resolve(fake.window.webContents.id, { allowed: false, requestId: '1' }), true)

  await nextTurn()

  assert.equal(fake.closeCalls, 0)
  assert.equal(fake.destroyed, false)
})

test('retries a successful close exactly once after its renderer flush resolves', async () => {
  const fake = makeWindow()
  const coordinator = createRendererCloseCoordinator()

  installWindowCloseBarrier(fake.window, { requestFlush: () => coordinator.request(fake.window) })

  assert.equal(fake.emitClose(), true)
  assert.equal(coordinator.resolve(fake.window.webContents.id, { allowed: true, requestId: '1' }), true)

  await nextTurn()

  assert.equal(fake.closeCalls, 1)
  assert.equal(fake.destroyed, true)
})

test('coalesces repeated close attempts while a renderer flush is pending', async () => {
  const fake = makeWindow()
  const coordinator = createRendererCloseCoordinator()

  installWindowCloseBarrier(fake.window, { requestFlush: () => coordinator.request(fake.window) })

  assert.equal(fake.emitClose(), true)
  assert.equal(fake.emitClose(), true)
  assert.deepEqual(fake.sent, [{ channel: RENDERER_CLOSE_REQUEST_CHANNEL, payload: { requestId: '1' } }])

  assert.equal(coordinator.resolve(fake.window.webContents.id, { allowed: true, requestId: '1' }), true)
  await nextTurn()

  assert.equal(fake.closeCalls, 1)
  assert.equal(fake.destroyed, true)
})
test('passes the final app-close event through after the shared flush succeeds', () => {
  const fake = makeWindow()
  const coordinator = createRendererCloseCoordinator()

  installWindowCloseBarrier(fake.window, {
    isTeardownPermitted: () => true,
    requestFlush: () => coordinator.request(fake.window)
  })

  assert.equal(fake.emitClose(), false)
  assert.equal(fake.destroyed, true)
  assert.deepEqual(fake.sent, [])
})

test('vetoes a close after the bounded renderer-response timeout', async () => {
  const fake = makeWindow()
  let timeout: (() => void) | undefined
  const failures: string[] = []
  const coordinator = createRendererCloseCoordinator({
    clearScheduled: () => undefined,
    onFailure: failure => failures.push(failure.reason),
    scheduleTimeout: callback => {
      timeout = callback

      return 1
    }
  })

  installWindowCloseBarrier(fake.window, { requestFlush: () => coordinator.request(fake.window) })

  assert.equal(fake.emitClose(), true)
  timeout?.()
  await nextTurn()

  assert.equal(fake.closeCalls, 0)
  assert.equal(fake.destroyed, false)
  assert.deepEqual(failures, ['unresponsive'])
})
