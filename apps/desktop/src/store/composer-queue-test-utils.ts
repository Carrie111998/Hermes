import { $queuedPromptsBySession, QUEUE_STORAGE_KEY, QUEUE_TOMBSTONES_STORAGE_KEY } from './composer-queue'
import type { QueuedPromptEntry } from './composer-queue'

/**
 * Shared helpers for the composer-queue suites (store + hook). The fake lock
 * manager encodes real Web Locks semantics — exclusivity, FIFO waiting,
 * `ifAvailable` null grants, abort-signal rejection, release-on-settle — so
 * both suites model cross-window contention against the SAME behavior.
 */

interface FakeLockOptions {
  ifAvailable?: boolean
  signal?: AbortSignal
}

type FakeLockCallback = (lock: null | { name: string }) => Promise<unknown> | unknown

export function installFakeLocks() {
  // name → FIFO of waiters. A key existing (even with an empty array) means
  // the lock is currently held.
  const queues = new Map<string, Array<() => void>>()

  const release = (name: string) => {
    const waiters = queues.get(name)
    const next = waiters?.shift()

    if (next) {
      next() // hand over; the map entry stays = still held
    } else {
      queues.delete(name)
    }
  }

  const request = async (name: string, options: FakeLockOptions = {}, callback: FakeLockCallback) => {
    if (queues.has(name)) {
      if (options.ifAvailable) {
        return callback(null)
      }

      await new Promise<void>((resolve, reject) => {
        const waiter = () => resolve()
        const signal = options.signal

        if (signal) {
          if (signal.aborted) {
            reject(signal.reason instanceof DOMException ? signal.reason : new DOMException('Aborted', 'AbortError'))

            return
          }

          signal.addEventListener(
            'abort',
            () => {
              const waiters = queues.get(name)
              const index = waiters ? waiters.indexOf(waiter) : -1

              if (waiters && index >= 0) {
                waiters.splice(index, 1)
              }

              reject(signal.reason instanceof DOMException ? signal.reason : new DOMException('Aborted', 'AbortError'))
            },
            { once: true }
          )
        }

        queues.get(name)!.push(waiter)
      })
    } else {
      queues.set(name, [])
    }

    try {
      return await callback({ name })
    } finally {
      release(name)
    }
  }

  Object.defineProperty(window.navigator, 'locks', { configurable: true, value: { request } })

  return () => {
    delete (window.navigator as { locks?: unknown }).locks
  }
}

/** Reset every storage surface the queue store uses, plus the atom. */
export function resetQueueStorage() {
  window.localStorage.removeItem(QUEUE_STORAGE_KEY)
  window.localStorage.removeItem(QUEUE_TOMBSTONES_STORAGE_KEY)
  $queuedPromptsBySession.set({})
}

export function remoteEntry(id: string, text: string): QueuedPromptEntry {
  return { id, text, attachments: [], queuedAt: 1 }
}

/**
 * Simulate another window writing the shared queue key. With `fireEvent` the
 * `storage` event is dispatched too (it never fires in the writing window
 * itself, so dispatching manually is exactly the other-window signal); without
 * it, the write models the worst case — persisted but not yet synced.
 */
export function otherWindowWrites(state: Record<string, unknown>, { fireEvent = false } = {}) {
  const value = JSON.stringify(state)
  window.localStorage.setItem(QUEUE_STORAGE_KEY, value)

  if (fireEvent) {
    window.dispatchEvent(new StorageEvent('storage', { key: QUEUE_STORAGE_KEY, newValue: value }))
  }
}

export function persistedQueueTexts(sid: string): string[] {
  const parsed = JSON.parse(window.localStorage.getItem(QUEUE_STORAGE_KEY) ?? '{}') as Record<
    string,
    { text: string }[]
  >

  return (parsed[sid] ?? []).map(e => e.text)
}
