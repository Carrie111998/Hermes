import { expect, it } from 'vitest'

import { applySshConnectionTeardown, replacePublishedSshConnection } from './connection-apply'
import { applyProfileDeleteLifecycle } from './profile-delete-routing'
import { createBootstrapCoordinator } from './ssh-bootstrap-coordinator'

function deferred() {
  let resolve

  const promise = new Promise(ok => {
    resolve = ok
  })

  return { promise, resolve }
}

it('retires the scope while draining bootstrap and closing published state', async () => {
  const coordinator = createBootstrapCoordinator()
  const bootstrapGate = deferred()
  const closeGate = deferred()
  const events: string[] = []

  const bootstrap = coordinator.start('worker', 'old', async lease => {
    events.push('old-start')
    await bootstrapGate.promise
    lease.assertCurrent()
  })

  await Promise.resolve()

  const run = applySshConnectionTeardown({
    closePublishedConnection: async scope => {
      events.push(`closed:${scope}`)
      await closeGate.promise
    },
    retireAndRun: (scope, operation) => coordinator.retireAndRun(scope, operation),
    scope: 'worker'
  })

  await Promise.resolve()
  const duringRetirement = coordinator.start('worker', 'new', async () => events.push('new-start'))

  bootstrapGate.resolve(undefined)
  await expect(bootstrap).rejects.toMatchObject({ kind: 'superseded' })
  await expect(duringRetirement).rejects.toMatchObject({ kind: 'retired' })
  expect(events).toEqual(['old-start', 'closed:worker'])

  closeGate.resolve(undefined)
  await run

  await expect(coordinator.start('worker', 'after', async () => 'after')).resolves.toBe('after')
  expect(events).toEqual(['old-start', 'closed:worker'])
})

it('keeps the scope retired through sibling profile-pool cleanup', async () => {
  const coordinator = createBootstrapCoordinator()
  const poolGate = deferred()
  const events: string[] = []

  const teardown = applySshConnectionTeardown({
    closePublishedConnection: async scope => events.push(`closed:${scope}`),
    retireAndRun: (scope, operation) => coordinator.retireAndRun(scope, operation),
    scope: 'worker',
    teardownRelatedState: async () => {
      events.push('pool-start')
      await poolGate.promise
      events.push('pool-done')
    }
  })

  await Promise.resolve()

  const duringPoolCleanup = coordinator.start('worker', 'new', async () => events.push('new-start'))

  await expect(duringPoolCleanup).rejects.toMatchObject({ kind: 'retired' })
  expect(events).toEqual(['closed:worker', 'pool-start'])

  poolGate.resolve(undefined)
  await teardown

  await expect(coordinator.start('worker', 'after', async () => 'after')).resolves.toBe('after')
  expect(events).toEqual(['closed:worker', 'pool-start', 'pool-done'])
})

it('keeps a deleted profile scope retired through SSH and pool teardown', async () => {
  const coordinator = createBootstrapCoordinator()
  const bootstrapGate = deferred()
  const poolGate = deferred()
  const events: string[] = []

  const bootstrap = coordinator.start('worker', 'old', async lease => {
    events.push('old-start')
    await bootstrapGate.promise
    lease.assertCurrent()
  })

  await Promise.resolve()

  const deletion = applyProfileDeleteLifecycle(
    { action: 'teardown-pool', profile: 'worker' },
    {
      destroyRevokedWindows: () => events.push('windows-revoked'),
      failRevocation: () => events.push('revocation-failed'),
      revokeProfile: () => 'mutation',
      revokeWindowTargets: () => [],
      teardownPrimary: async () => {
        events.push('primary-torn-down')
      },
      teardownProfileBackends: () =>
        applySshConnectionTeardown({
          closePublishedConnection: async scope => events.push(`closed:${scope}`),
          retireAndRun: (scope, operation) => coordinator.retireAndRun(scope, operation),
          scope: 'worker',
          teardownRelatedState: async () => {
            events.push('pool-start')
            await poolGate.promise
            events.push('pool-done')
          }
        }),
      writeActiveProfile: profile => events.push(`active:${profile}`)
    }
  )

  await Promise.resolve()
  const duringDeletion = coordinator.start('worker', 'new', async () => events.push('new-start'))

  bootstrapGate.resolve(undefined)
  await expect(bootstrap).rejects.toMatchObject({ kind: 'superseded' })
  await expect(duringDeletion).rejects.toMatchObject({ kind: 'retired' })
  expect(events).toEqual(['old-start', 'windows-revoked', 'closed:worker', 'pool-start'])

  poolGate.resolve(undefined)
  await expect(deletion).resolves.toEqual({ mutation: 'mutation', profile: 'worker' })
  expect(events).toEqual(['old-start', 'windows-revoked', 'closed:worker', 'pool-start', 'pool-done'])
})

it('replaces stale published state inside the current lease without waiting on itself', async () => {
  const coordinator = createBootstrapCoordinator()
  const events: string[] = []

  await expect(
    coordinator.start('worker', 'changed', async () =>
      replacePublishedSshConnection({
        closePublishedConnection: async scope => events.push(`closed:${scope}`),
        existingFingerprint: 'old',
        fingerprint: 'changed',
        scope: 'worker'
      })
    )
  ).resolves.toBeUndefined()

  expect(events).toEqual(['closed:worker'])
})
