import assert from 'node:assert/strict'

import { test } from 'vitest'

import { makeBackendTarget } from './backend-target'
import {
  applyProfileDeleteLifecycle,
  createProfileRevocationGuard,
  decideProfileDeleteAction,
  profileNameFromCreateRequest,
  profileNameFromDeleteRequest,
  removeProfileConnectionOverride,
  resolveRouteTarget
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
