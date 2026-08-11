import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  applyProfileRenameLifecycle,
  decideProfileRenameAction,
  profileRenameFromRequest,
  renameProfileConnectionOverride
} from './profile-delete-routing'

test('applyProfileRenameLifecycle revokes and rehomes a renamed primary before retiring the old identity', async () => {
  const events: string[] = []

  await applyProfileRenameLifecycle({ from: 'worker', to: 'coder' }, 'worker', {
    completeRevocation: mutation => events.push(`complete:${mutation}`),
    destroyRevokedWindows: ids => events.push(`destroy:${ids.join(',')}`),
    failRevocation: mutation => events.push(`failed:${mutation}`),
    migrateConnectionOverride: (from, to) => events.push(`config:${from}->${to}`),
    revokeProfile: profile => {
      events.push(`revoke:${profile}`)

      return `mutation:${profile}`
    },
    revokeWindowTargets: profile => {
      events.push(`windows:${profile}`)

      return [7, 9]
    },
    teardownPrimary: async () => {
      events.push('primary')
    },
    teardownProfilePools: async profile => {
      events.push(`pools:${profile}`)
    },
    writeActiveProfile: profile => events.push(`active:${profile}`)
  })

  assert.deepEqual(events, [
    'revoke:worker',
    'windows:worker',
    'destroy:7,9',
    'config:worker->coder',
    'active:coder',
    'primary',
    'pools:worker',
    'complete:mutation:worker'
  ])
})

test('applyProfileRenameLifecycle leaves primary selection and backend untouched for a pooled profile', async () => {
  const events: string[] = []

  await applyProfileRenameLifecycle({ from: 'worker', to: 'coder' }, 'default', {
    completeRevocation: () => events.push('complete'),
    destroyRevokedWindows: () => events.push('destroy'),
    failRevocation: () => events.push('failed'),
    migrateConnectionOverride: () => events.push('config'),
    revokeProfile: () => 'mutation',
    revokeWindowTargets: () => [],
    teardownPrimary: async () => {
      events.push('primary')
    },
    teardownProfilePools: async () => {
      events.push('pools')
    },
    writeActiveProfile: () => events.push('active')
  })

  assert.deepEqual(events, ['destroy', 'config', 'pools', 'complete'])
})

test('applyProfileRenameLifecycle fails the revocation token when teardown rejects', async () => {
  const failed: string[] = []

  await assert.rejects(
    applyProfileRenameLifecycle({ from: 'worker', to: 'coder' }, 'default', {
      completeRevocation: () => {},
      destroyRevokedWindows: () => {},
      failRevocation: mutation => failed.push(mutation),
      migrateConnectionOverride: () => {},
      revokeProfile: () => 'rename-token',
      revokeWindowTargets: () => [],
      teardownPrimary: async () => {},
      teardownProfilePools: async () => {
        throw new Error('teardown failed')
      },
      writeActiveProfile: () => {}
    }),
    /teardown failed/
  )

  assert.deepEqual(failed, ['rename-token'])
})

test('applyProfileRenameLifecycle waits for every started primary teardown before failing revocation', async () => {
  let releasePoolTeardown!: () => void

  const poolTeardown = new Promise<void>(resolve => {
    releasePoolTeardown = resolve
  })

  const failed: string[] = []

  const lifecycle = applyProfileRenameLifecycle({ from: 'worker', to: 'coder' }, 'worker', {
    completeRevocation: () => {},
    destroyRevokedWindows: () => {},
    failRevocation: mutation => failed.push(mutation),
    migrateConnectionOverride: () => {},
    revokeProfile: () => 'rename-token',
    revokeWindowTargets: () => [],
    teardownPrimary: async () => {
      throw new Error('primary teardown failed')
    },
    teardownProfilePools: () => poolTeardown,
    writeActiveProfile: () => {}
  })

  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual(failed, [])

  releasePoolTeardown()
  await assert.rejects(lifecycle, /primary teardown failed/)
  assert.deepEqual(failed, ['rename-token'])
})

test('profileRenameFromRequest parses a valid PATCH profile rename', () => {
  assert.deepEqual(
    profileRenameFromRequest({ method: 'PATCH', path: '/api/profiles/Worker', body: { new_name: 'Coder' } }),
    { from: 'worker', to: 'coder' }
  )
})

test('profileRenameFromRequest rejects non-renames and invalid identities', () => {
  assert.equal(profileRenameFromRequest({ method: 'POST', path: '/api/profiles/worker', body: { new_name: 'coder' } }), null)
  assert.equal(profileRenameFromRequest({ method: 'PATCH', path: '/api/profiles/worker/soul', body: { new_name: 'coder' } }), null)
  assert.equal(profileRenameFromRequest({ method: 'PATCH', path: '/api/profiles/worker', body: {} }), null)
  assert.equal(profileRenameFromRequest({ method: 'PATCH', path: '/api/profiles/default', body: { new_name: 'coder' } }), null)
  assert.equal(profileRenameFromRequest({ method: 'PATCH', path: '/api/profiles/worker', body: { new_name: '../coder' } }), null)
})

test('decideProfileRenameAction rehomes the primary profile and tears down non-primary pools', () => {
  assert.deepEqual(decideProfileRenameAction({ from: 'worker', to: 'coder' }, 'worker'), {
    action: 'teardown-primary',
    from: 'worker',
    to: 'coder'
  })
  assert.deepEqual(decideProfileRenameAction({ from: 'worker', to: 'coder' }, 'default'), {
    action: 'teardown-pool',
    from: 'worker',
    to: 'coder'
  })
})

test('renameProfileConnectionOverride moves only the renamed profile without mutating input', () => {
  const original = {
    mode: 'remote',
    profiles: {
      coder: { mode: 'local' },
      worker: { mode: 'ssh', ssh: { host: 'worker.example' } }
    }
  }

  const next = renameProfileConnectionOverride(original, 'worker', 'renamed')

  assert.deepEqual(next, {
    mode: 'remote',
    profiles: {
      coder: { mode: 'local' },
      renamed: { mode: 'ssh', ssh: { host: 'worker.example' } }
    }
  })
  assert.deepEqual(Object.keys(original.profiles), ['coder', 'worker'])
})

test('renameProfileConnectionOverride leaves unrelated config unchanged when no old override exists', () => {
  const original = { mode: 'local', profiles: { coder: { mode: 'local' } } }

  assert.deepEqual(renameProfileConnectionOverride(original, 'worker', 'renamed'), original)
})
