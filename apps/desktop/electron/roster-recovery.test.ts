import { expect, it } from 'vitest'

import { createRosterRecoverySignals } from './roster-recovery'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, reject, resolve }
}

it('signals once when repeated consumers observe the same recovering connection', async () => {
  const notifications: string[] = []
  const recovery = createRosterRecoverySignals(signal => notifications.push(signal.connectionId))
  const dial = deferred<void>()

  recovery.afterPendingDial('homelab', dial.promise)
  recovery.afterPendingDial('homelab', dial.promise)
  dial.resolve()
  await dial.promise
  await Promise.resolve()

  expect(notifications).toEqual(['homelab'])
})

it('does not signal when the background dial fails', async () => {
  const notifications: string[] = []
  const recovery = createRosterRecoverySignals(signal => notifications.push(signal.connectionId))
  const dial = deferred<void>()

  recovery.afterPendingDial('homelab', dial.promise)
  dial.reject(new Error('offline'))
  await expect(dial.promise).rejects.toThrow(/offline/)
  await Promise.resolve()

  expect(notifications).toEqual([])
})

it('allows a later recovery attempt after an earlier dial settles', async () => {
  const notifications: string[] = []
  const recovery = createRosterRecoverySignals(signal => notifications.push(signal.connectionId))
  const first = deferred<void>()
  const second = deferred<void>()

  recovery.afterPendingDial('homelab', first.promise)
  first.reject(new Error('offline'))
  await expect(first.promise).rejects.toThrow(/offline/)
  await Promise.resolve()

  recovery.afterPendingDial('homelab', second.promise)
  second.resolve()
  await second.promise
  await Promise.resolve()

  expect(notifications).toEqual(['homelab'])
})
