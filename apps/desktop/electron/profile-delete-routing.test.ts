import assert from 'node:assert/strict'

import { test } from 'vitest'

import { makeBackendTarget } from './backend-target'
import {
  applyProfileDeleteLifecycle,
  assertProfileNotRevoked,
  createProfileRevocationGuard,
  decideProfileDeleteAction,
  profileNameFromCreateRequest,
  profileNameFromDeleteRequest,
  removeProfileConnectionOverride,
  resolveRouteTarget,
  runProfileMutationPreflight
} from './profile-delete-routing'

// ---------------------------------------------------------------------------
// profileNameFromDeleteRequest
// ---------------------------------------------------------------------------

test('profileNameFromDeleteRequest parses a DELETE /api/profiles/<name> path', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/worker' }), 'worker')
})

test('profileNameFromDeleteRequest lowercases the profile name', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/Worker' }), 'worker')
})

test('profileNameFromDeleteRequest returns null for non-DELETE methods', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'GET', path: '/api/profiles/worker' }), null)
})

test('profileNameFromDeleteRequest returns null when the path does not match', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/sessions' }), null)
})

test('profileNameFromDeleteRequest returns null for an empty/whitespace name', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/%20' }), null)
})

test('profileNameFromDeleteRequest returns null for an undecodable path segment', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/%E0%A4%A' }), null)
})

test('profileNameFromCreateRequest parses a valid POST /api/profiles body', () => {
  assert.equal(
    profileNameFromCreateRequest({ method: 'POST', path: '/api/profiles', body: { name: 'Worker' } }),
    'worker'
  )
})

test('profileNameFromCreateRequest parses a query-bearing profile collection path', () => {
  assert.equal(
    profileNameFromCreateRequest({ method: 'POST', path: '/api/profiles?source=desktop', body: { name: 'Worker' } }),
    'worker'
  )
})

test('profileNameFromCreateRequest rejects other methods, paths, and invalid names', () => {
  assert.equal(profileNameFromCreateRequest({ method: 'GET', path: '/api/profiles', body: { name: 'worker' } }), null)
  assert.equal(profileNameFromCreateRequest({ method: 'POST', path: '/api/profiles/worker', body: { name: 'worker' } }), null)
  assert.equal(profileNameFromCreateRequest({ method: 'POST', path: '/api/profiles', body: { name: '../worker' } }), null)
})

test('runProfileMutationPreflight completes a tracked mutation when preflight fails', async () => {
  const completions: string[] = []

  await assert.rejects(
    runProfileMutationPreflight(
      'create-token',
      async () => {
        throw new Error('backend unavailable')
      },
      (mutation, succeeded) => completions.push(`${succeeded ? 'succeeded' : 'failed'}:${mutation}`)
    ),
    /backend unavailable/
  )

  assert.deepEqual(completions, ['failed:create-token'])
})

test('runProfileMutationPreflight does not double-complete after handoff', async () => {
  const completions: string[] = []

  await assert.rejects(
    runProfileMutationPreflight(
      'create-token',
      async handoff => {
        handoff()
        throw new Error('request failed after handoff')
      },
      (mutation, succeeded) => completions.push(`${succeeded ? 'succeeded' : 'failed'}:${mutation}`)
    ),
    /request failed after handoff/
  )

  assert.deepEqual(completions, [])
})

test('runProfileMutationPreflight completes an early success before handoff', async () => {
  const completions: string[] = []

  const result = await runProfileMutationPreflight(
    'create-token',
    async () => 'intercepted',
    (mutation, succeeded) => completions.push(`${succeeded ? 'succeeded' : 'failed'}:${mutation}`)
  )

  assert.equal(result, 'intercepted')
  assert.deepEqual(completions, ['succeeded:create-token'])
})

test('runProfileMutationPreflight adopts a deletion token created during preflight', async () => {
  const completions: string[] = []

  await assert.rejects(
    runProfileMutationPreflight(
      null,
      async (_handoff, track) => {
        track('delete-token')
        throw new Error('backend unavailable after teardown')
      },
      (mutation, succeeded) => completions.push(`${succeeded ? 'succeeded' : 'failed'}:${mutation}`)
    ),
    /backend unavailable after teardown/
  )

  assert.deepEqual(completions, ['failed:delete-token'])
})

test('an adopted failed deletion drains so a later successful recreation restores authority', async () => {
  const guard = createProfileRevocationGuard()
  const deletion = guard.revoke('worker')

  await assert.rejects(
    runProfileMutationPreflight(
      null,
      async (_handoff, track) => {
        track(deletion)
        throw new Error('backend unavailable after teardown')
      },
      (mutation, succeeded) => guard.completeMutation({ mutation, succeeded })
    ),
    /backend unavailable after teardown/
  )

  const recreation = guard.startCreation('worker')
  guard.completeMutation({ mutation: recreation, succeeded: true })

  assert.equal(guard.isRevoked('worker'), false)
})

test('runProfileMutationPreflight does not settle twice when settlement itself throws', async () => {
  const completions: boolean[] = []

  await assert.rejects(
    runProfileMutationPreflight(
      'create-token',
      async () => 'created',
      (_mutation, succeeded) => {
        completions.push(succeeded)
        throw new Error('settlement failed')
      }
    ),
    /settlement failed/
  )

  assert.deepEqual(completions, [true])
})

test('assertProfileNotRevoked rejects profile connection resolution while deletion owns authority', () => {
  assert.throws(() => assertProfileNotRevoked('worker', profile => profile === 'worker'), /being deleted/)
  assert.doesNotThrow(() => assertProfileNotRevoked('worker', () => false))
  assert.doesNotThrow(() => assertProfileNotRevoked(null, () => true))
})

// ---------------------------------------------------------------------------
// decideProfileDeleteAction
// ---------------------------------------------------------------------------

const deps = {
  isDefaultProfile: p => p === 'default',
  isValidProfileName: p => /^[a-z0-9][a-z0-9_-]{0,63}$/.test(p),
  primaryProfileKey: () => 'primary-profile'
}

test('decideProfileDeleteAction is a noop for the default profile', () => {
  assert.deepEqual(decideProfileDeleteAction('default', deps), { action: 'noop', profile: null })
})

test('decideProfileDeleteAction is a noop for null (no profile parsed)', () => {
  assert.deepEqual(decideProfileDeleteAction(null, deps), { action: 'noop', profile: null })
})

test('decideProfileDeleteAction is a noop for an invalid profile name', () => {
  assert.deepEqual(decideProfileDeleteAction('Not Valid!', deps), { action: 'noop', profile: null })
})

test('decideProfileDeleteAction tears down the primary backend for the primary profile', () => {
  assert.deepEqual(decideProfileDeleteAction('primary-profile', deps), {
    action: 'teardown-primary',
    profile: 'primary-profile'
  })
})

test('decideProfileDeleteAction tears down the pool backend for any other valid profile', () => {
  assert.deepEqual(decideProfileDeleteAction('worker', deps), { action: 'teardown-pool', profile: 'worker' })
})

test('applyProfileDeleteLifecycle resets and tears down every primary-profile backend', async () => {
  const events: string[] = []

  const result = await applyProfileDeleteLifecycle(
    { action: 'teardown-primary', profile: 'primary-profile' },
    {
      destroyRevokedWindows: ids => events.push(`windows:${ids.join(',')}`),
      failRevocation: mutation => events.push(`failed:${mutation}`),
      revokeProfile: profile => {
        events.push(`revoked:${profile}`)

        return 'mutation'
      },
      revokeWindowTargets: profile => {
        events.push(`targets:${profile}`)

        return [7, 9]
      },
      teardownPrimary: async () => {
        events.push('primary-torn-down')
      },
      teardownProfileBackends: async profile => {
        events.push(`profile-torn-down:${profile}`)
      },
      writeActiveProfile: profile => events.push(`active:${profile}`)
    }
  )

  assert.deepEqual(result, { mutation: 'mutation', profile: 'primary-profile' })
  assert.deepEqual(events, [
    'revoked:primary-profile',
    'targets:primary-profile',
    'windows:7,9',
    'active:default',
    'primary-torn-down',
    'profile-torn-down:primary-profile'
  ])
})

test('applyProfileDeleteLifecycle fails the revocation token when teardown rejects', async () => {
  const failed: string[] = []

  await assert.rejects(
    applyProfileDeleteLifecycle(
      { action: 'teardown-pool', profile: 'worker' },
      {
        destroyRevokedWindows: () => {},
        failRevocation: mutation => failed.push(mutation),
        revokeProfile: () => 'delete-token',
        revokeWindowTargets: () => [],
        teardownPrimary: async () => {},
        teardownProfileBackends: async () => {
          throw new Error('teardown failed')
        },
        writeActiveProfile: () => {}
      }
    ),
    /teardown failed/
  )

  assert.deepEqual(failed, ['delete-token'])
})

test('applyProfileDeleteLifecycle waits for every started primary teardown before failing revocation', async () => {
  let releaseProfileTeardown!: () => void

  const profileTeardown = new Promise<void>(resolve => {
    releaseProfileTeardown = resolve
  })

  const failed: string[] = []

  const lifecycle = applyProfileDeleteLifecycle(
    { action: 'teardown-primary', profile: 'worker' },
    {
      destroyRevokedWindows: () => {},
      failRevocation: mutation => failed.push(mutation),
      revokeProfile: () => 'delete-token',
      revokeWindowTargets: () => [],
      teardownPrimary: () => {
        throw new Error('primary teardown failed')
      },
      teardownProfileBackends: () => profileTeardown,
      writeActiveProfile: () => {}
    }
  )

  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual(failed, [])

  releaseProfileTeardown()
  await assert.rejects(lifecycle, /primary teardown failed/)
  assert.deepEqual(failed, ['delete-token'])
})

// ---------------------------------------------------------------------------
// resolveRouteTarget
// ---------------------------------------------------------------------------

test('resolveRouteTarget routes deletion through primary instead of recreating the deleted target', () => {
  assert.deepEqual(
    resolveRouteTarget('worker', makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })),
    makeBackendTarget({ kind: 'primary' })
  )
})

test('resolveRouteTarget preserves the already-authorized target when nothing was torn down', () => {
  const target = makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })

  assert.deepEqual(resolveRouteTarget(null, target), target)
})

test('profile revocation guard remains revoked until explicitly restored', () => {
  const guard = createProfileRevocationGuard()

  assert.equal(guard.isRevoked('worker'), false)
  guard.revoke('worker')
  assert.equal(guard.isRevoked('worker'), true)
  guard.restore('worker')
  assert.equal(guard.isRevoked('worker'), false)
})

test('a current successful deletion retires its tombstone and reports the connection profile to remove', () => {
  const guard = createProfileRevocationGuard()
  const creation = guard.startCreation('worker')
  const deletion = guard.revoke('worker')

  guard.completeMutation({ mutation: creation, succeeded: true })
  assert.equal(guard.isRevoked('worker'), true)
  const completion = guard.completeMutation({ mutation: deletion, succeeded: true })

  assert.deepEqual(completion, { retiredProfile: 'worker' })
  assert.equal(guard.isRevoked('worker'), false)
})

test('a creation completing after deletion restores the recreated profile', () => {
  const guard = createProfileRevocationGuard()
  const deletion = guard.revoke('worker')

  assert.deepEqual(guard.completeMutation({ mutation: deletion, succeeded: true }), {
    retiredProfile: 'worker'
  })
  const creation = guard.startCreation('worker')
  guard.completeMutation({ mutation: creation, succeeded: true })

  assert.equal(guard.isRevoked('worker'), false)
})

test('an ambiguous deletion failure remains revoked fail closed', () => {
  const guard = createProfileRevocationGuard()
  const deletion = guard.revoke('worker')

  assert.deepEqual(guard.completeMutation({ mutation: deletion, succeeded: false }), {
    retiredProfile: null
  })

  assert.equal(guard.isRevoked('worker'), true)
})

test('a create that started before a later delete cannot clear the newer tombstone', () => {
  const guard = createProfileRevocationGuard()
  const staleCreate = guard.startCreation('worker')
  const deletion = guard.revoke('worker')

  assert.deepEqual(guard.completeMutation({ mutation: deletion, succeeded: true }), {
    retiredProfile: null
  })
  assert.deepEqual(guard.completeMutation({ mutation: staleCreate, succeeded: true }), {
    retiredProfile: null
  })

  assert.equal(guard.isRevoked('worker'), true)
})

test('a failed older create lets the newer successful delete retire after it drains', () => {
  const guard = createProfileRevocationGuard()
  const staleCreate = guard.startCreation('worker')
  const deletion = guard.revoke('worker')

  assert.deepEqual(guard.completeMutation({ mutation: deletion, succeeded: true }), {
    retiredProfile: null
  })
  assert.deepEqual(guard.completeMutation({ mutation: staleCreate, succeeded: false }), {
    retiredProfile: 'worker'
  })

  assert.equal(guard.isRevoked('worker'), false)
})

test('a newer successful create restores after an older create overtakes deletion', () => {
  const guard = createProfileRevocationGuard()
  const staleCreate = guard.startCreation('worker')
  const deletion = guard.revoke('worker')

  guard.completeMutation({ mutation: deletion, succeeded: true })
  guard.completeMutation({ mutation: staleCreate, succeeded: true })
  assert.equal(guard.isRevoked('worker'), true)

  const recreation = guard.startCreation('worker')
  guard.completeMutation({ mutation: recreation, succeeded: true })

  assert.equal(guard.isRevoked('worker'), false)
})

test('a successful create that started after a delete restores once that older delete drains', () => {
  const guard = createProfileRevocationGuard()
  const deletion = guard.revoke('worker')
  const recreation = guard.startCreation('worker')

  guard.completeMutation({ mutation: recreation, succeeded: true })
  assert.equal(guard.isRevoked('worker'), true)

  assert.deepEqual(guard.completeMutation({ mutation: deletion, succeeded: true }), {
    retiredProfile: null
  })
  assert.equal(guard.isRevoked('worker'), false)
})

test('successful unique deletions do not retain old tombstones', () => {
  const guard = createProfileRevocationGuard()

  for (let index = 0; index < 5_000; index += 1) {
    const profile = `worker-${index}`
    const deletion = guard.revoke(profile)

    assert.deepEqual(guard.completeMutation({ mutation: deletion, succeeded: true }), {
      retiredProfile: profile
    })
  }

  assert.equal(guard.isRevoked('worker-0'), false)
  assert.equal(guard.isRevoked('worker-4999'), false)
})

test('removeProfileConnectionOverride removes only the deleted profile without mutating input', () => {
  const original = {
    mode: 'remote',
    remote: { url: 'https://primary.example' },
    profiles: {
      coder: { mode: 'remote', url: 'https://coder.example' },
      worker: { mode: 'remote', url: 'https://worker.example' }
    }
  }

  const next = removeProfileConnectionOverride(original, 'worker')

  assert.deepEqual(next, {
    mode: 'remote',
    remote: { url: 'https://primary.example' },
    profiles: {
      coder: { mode: 'remote', url: 'https://coder.example' }
    }
  })
  assert.equal('worker' in original.profiles, true)
})
