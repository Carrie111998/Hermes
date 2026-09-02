/** Verified, cross-window persistence for gateway-hosted Group Chat commands. */

import type { PluginContext } from '@hermes/plugin-sdk'

import { createHostedRoomOutbox, reduceHostedRoomOutbox } from './hosted-room-client'
import type { HostedRoomOutbox, HostedRoomOutboxAction } from './hosted-room-client'

export const HOSTED_ROOM_OUTBOX_KEY = 'hosted-room-outbox-v1'

const OUTBOX_MUTATION_LOCK = 'hermes-bots-hosted-room-outbox'
const OUTBOX_DISPATCH_LOCK = 'hermes-bots-hosted-room-outbox-dispatch'

interface LockManager {
  request<T>(
    name: string,
    options: { mode: 'exclusive' },
    callback: (lock: null | object) => Promise<T> | T
  ): Promise<T>
}

let mutationTail: Promise<void> = Promise.resolve()
let dispatchTail: Promise<void> = Promise.resolve()

function lockManager() {
  return (globalThis.navigator as (Navigator & { locks?: LockManager }) | undefined)?.locks
}

function processLock<T>(kind: 'dispatch' | 'mutation', callback: () => Promise<T>) {
  const tail = kind === 'mutation' ? mutationTail : dispatchTail
  const result = tail.then(callback, callback)

  const settled = result.then(
    () => undefined,
    () => undefined
  )

  if (kind === 'mutation') {
    mutationTail = settled
  } else {
    dispatchTail = settled
  }

  return result
}

function withLock<T>(name: string, kind: 'dispatch' | 'mutation', callback: () => Promise<T>) {
  const locks = lockManager()

  return locks?.request ? locks.request(name, { mode: 'exclusive' }, callback) : processLock(kind, callback)
}

function sameOutbox(left: HostedRoomOutbox, right: HostedRoomOutbox) {
  return JSON.stringify(left) === JSON.stringify(right)
}

export async function readHostedRoomOutbox(storage: null | PluginContext['storage']) {
  if (typeof storage?.get !== 'function') {
    throw new Error('Desktop storage is unavailable, so Group Chat changes cannot be secured.')
  }

  return createHostedRoomOutbox(await storage.get(HOSTED_ROOM_OUTBOX_KEY, null), false)
}

async function persistOutbox(storage: PluginContext['storage'], next: HostedRoomOutbox) {
  await storage.set(HOSTED_ROOM_OUTBOX_KEY, next)
  const persisted = await readHostedRoomOutbox(storage)

  if (!sameOutbox(persisted, next)) {
    throw new Error('Desktop storage did not persist the Group Chat change.')
  }

  return persisted
}

export async function mutateHostedRoomOutbox(storage: null | PluginContext['storage'], action: HostedRoomOutboxAction) {
  return withLock(OUTBOX_MUTATION_LOCK, 'mutation', async () => {
    if (typeof storage?.set !== 'function') {
      throw new Error('Desktop storage is unavailable, so Group Chat changes cannot be secured.')
    }

    const current = await readHostedRoomOutbox(storage)
    const next = reduceHostedRoomOutbox(current, action)

    if (sameOutbox(current, next)) {
      return current
    }

    return persistOutbox(storage, next)
  })
}

export async function recoverHostedRoomOutbox(storage: null | PluginContext['storage']) {
  return withLock(OUTBOX_MUTATION_LOCK, 'mutation', async () => {
    if (typeof storage?.set !== 'function') {
      throw new Error('Desktop storage is unavailable, so Group Chat changes cannot be secured.')
    }

    const current = await readHostedRoomOutbox(storage)
    const recovered = createHostedRoomOutbox(current)

    return sameOutbox(current, recovered) ? current : persistOutbox(storage, recovered)
  })
}

export function withHostedRoomOutboxDispatch<T>(callback: () => Promise<T>) {
  return withLock(OUTBOX_DISPATCH_LOCK, 'dispatch', callback)
}

export function resetHostedRoomOutboxLocksForTests() {
  mutationTail = Promise.resolve()
  dispatchTail = Promise.resolve()
}
