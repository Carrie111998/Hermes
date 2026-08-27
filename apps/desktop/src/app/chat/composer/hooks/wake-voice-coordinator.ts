import { $gateway } from '@/store/gateway'
import { resumeWakeAfterVoice } from '@/store/wake-word'

export type WakeVoiceOwner = symbol

const owners = new Set<WakeVoiceOwner>()
let operationQueue: Promise<void> = Promise.resolve()

const enqueue = (operation: () => Promise<void>): Promise<void> => {
  const result = operationQueue.then(operation)

  operationQueue = result.catch(() => undefined)

  return result
}

/**
 * Share the gateway wake-listener lease across every mounted composer. The first
 * owner pauses wake and later owners await the same serialized barrier.
 */
export function acquireWakeForVoice(owner: WakeVoiceOwner): Promise<void> {
  if (owners.has(owner)) {
    return operationQueue
  }

  owners.add(owner)

  if (owners.size > 1) {
    return operationQueue
  }

  return enqueue(async () => {
    if (owners.size === 0) {
      return
    }

    try {
      await $gateway.get()?.request('wake.pause', {})
    } catch {
      // No wake listener / older backend — nothing held the mic.
    }
  })
}

/** Resume wake only after the final composer releases the shared lease. */
export function releaseWakeForVoice(owner: WakeVoiceOwner): void {
  if (!owners.delete(owner) || owners.size > 0) {
    return
  }

  void enqueue(async () => {
    if (owners.size === 0) {
      await resumeWakeAfterVoice()
    }
  })
}
