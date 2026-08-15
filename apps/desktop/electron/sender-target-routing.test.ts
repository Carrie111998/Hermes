import assert from 'node:assert/strict'

import { test } from 'vitest'

import { makeBackendTarget } from './backend-target'
import {
  decideProfileSessionsRoute,
  resolveSenderRequestTarget,
  resolveSenderTarget,
  resolveSessionOwnerTarget,
  scopedSidebarPathForTarget,
  scopeSidebarResponseForTarget,
  sessionProfileForTarget
} from './sender-target-routing'

// ---------------------------------------------------------------------------
// resolveSenderTarget — decide which BackendTarget an IPC call resolves to,
// given the window's bound target and the renderer-supplied profile argument.
//
// Rules (frozen contract):
//   1. A bound target overrides getConnection when the renderer omits profile
//      OR asks for that target's profile.
//   2. An explicit request for a DIFFERENT profile conflicts with the bound
//      target. Only an unbound primary preserves legacy profile routing.
//   3. No renderer-supplied scope: the argument is a profile name (or empty),
//      never a target/scope/url. The decision never trusts a renderer
//      argument as a target identity.
// ---------------------------------------------------------------------------

// A primary-bound window behaves as the default: no binding.

test('session owner preserves the opener route when it already owns that profile', () => {
  const forced = makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' })

  assert.deepEqual(resolveSessionOwnerTarget(forced, 'worker', 'default'), forced)
})

test('session owner uses primary only for the frozen primary profile', () => {
  const primary = makeBackendTarget({ kind: 'primary' })

  assert.deepEqual(resolveSessionOwnerTarget(primary, 'default', 'default'), primary)
  assert.deepEqual(
    resolveSessionOwnerTarget(primary, 'worker', 'default'),
    makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })
  )
})

test('session owner overrides a different opener profile with a configured target', () => {
  const opener = makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })

  assert.deepEqual(
    resolveSessionOwnerTarget(opener, 'coder', 'default'),
    makeBackendTarget({ kind: 'configured-profile', profile: 'coder' })
  )
})

test('session owner falls back to the opener target when no owner hint is supplied', () => {
  const opener = makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })

  assert.deepEqual(resolveSessionOwnerTarget(opener, null, 'default'), opener)
})

test('primary-bound window with no profile arg resolves to primary', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'primary' }), null)

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'primary' }))
  assert.equal(result.overridden, false)
})

test('primary-bound window asking for its own profile (default) resolves to primary', () => {
  // primary has no profile; an empty arg is the "own profile" case.
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'primary' }), '')

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'primary' }))
  assert.equal(result.overridden, false)
})

test('primary-bound window asking for a different profile falls back to ensureBackend(profile)', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'primary' }), 'worker')

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.equal(result.overridden, false)
})

// configured-profile-bound window.

test('configured-profile window with no profile arg uses its bound target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }), null)

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.equal(result.overridden, true)
})

test('configured-profile window asking for its own profile uses its bound target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }), 'worker')

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.equal(result.overridden, true)
})

test('configured-profile window rejects a different profile without changing target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }), 'coder')

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.equal(result.conflict, true)
})

// forced-local-bound window.

test('forced-local window with no profile arg uses its bound target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }), null)

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.overridden, true)
})

test('forced-local window asking for its own profile uses its bound target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }), 'coder')

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.overridden, true)
})

test('forced-local window rejects a different profile without changing target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }), 'worker')

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, true)
})

// Touch must target the same backend: a touch for the window's own profile
// resolves to the bound target; a touch for a different profile is rejected.

test('touch for the bound target profile resolves to the bound target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }), 'coder')

  assert.equal(result.overridden, true)
})

test('touch for a different profile is rejected without changing target', () => {
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }), 'worker')

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, true)
})

// The decision never trusts a renderer argument as a target identity — the
// argument is always treated as a profile name (or empty), never as a kind
// prefix or url.

test('a renderer argument that looks like a target id is treated as a profile name, not a target', () => {
  // "primary" as an arg is NOT the primary target — it is a profile named
  // "primary", which the validator would reject. The decision does not parse
  // it as a target; it falls through to ensureBackend(profile) semantics.
  const result = resolveSenderTarget(makeBackendTarget({ kind: 'primary' }), 'primary')

  // "primary" is not a valid profile name, but resolveSenderTarget does not
  // validate — it just routes. The caller (ensureBackend) validates.
  assert.equal(result.overridden, false)
  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'primary' }))
})

test('resolveSenderRequestTarget keeps an own-profile query on a forced-local window', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }),
    { path: '/api/sessions?profile=coder' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
})

test('resolveSenderRequestTarget rejects a different query profile on a bound sender', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }),
    { path: '/api/sessions?profile=worker' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, true)
})

test('resolveSenderRequestTarget rejects conflicting explicit and query profiles', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }),
    { path: '/api/sessions?profile=ignored', profile: 'worker' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, true)
})

test('resolveSenderRequestTarget rejects a different body profile on a bound sender', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }),
    { path: '/api/pairing/approve', body: { profile: 'worker' } }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, true)
})

test('resolveSenderRequestTarget rejects a different path profile on a bound sender', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }),
    { method: 'DELETE', path: '/api/profiles/worker' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, true)
})

test('resolveSenderRequestTarget rejects conflicting path and query profile authority', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }),
    { method: 'DELETE', path: '/api/profiles/worker?profile=coder' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, true)
})

test('resolveSenderRequestTarget keeps an own-profile path on a bound sender', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }),
    { method: 'PUT', path: '/api/profiles/coder/soul' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  assert.equal(result.conflict, false)
})

test('sessionProfileForTarget derives list routing only from the resolved target', () => {
  assert.equal(sessionProfileForTarget(makeBackendTarget({ kind: 'primary' })), 'all')
  assert.equal(
    sessionProfileForTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })),
    'worker'
  )
})

test('profile session routing never takes the primary aggregate fast path for a bound target', () => {
  const primary = makeBackendTarget({ kind: 'primary' })
  const worker = makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })

  assert.deepEqual(decideProfileSessionsRoute(primary, primary, [], () => false), { kind: 'local-fast-path' })
  assert.deepEqual(decideProfileSessionsRoute(worker, worker, [], () => false), {
    kind: 'target',
    profile: 'worker'
  })
  assert.deepEqual(decideProfileSessionsRoute(primary, primary, ['remote'], () => false), { kind: 'merge' })
})

test('session list concrete authority stays aligned with the resolved target', () => {
  const requests = [
    { profile: 'worker', path: '/api/profiles/sessions' },
    { body: { profile: 'worker' }, path: '/api/profiles/sessions' },
    { path: '/api/profiles/sessions?profile=worker' },
    { path: '/api/profiles/sessions?recents_profile=worker' }
  ]

  for (const request of requests) {
    const resolution = resolveSenderRequestTarget(makeBackendTarget({ kind: 'primary' }), request)

    assert.equal(resolution.conflict, false)
    assert.equal(sessionProfileForTarget(resolution.target), 'worker')
  }
})

test('resolveSenderRequestTarget rejects a different sidebar recents profile on a bound sender', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }),
    { path: '/api/profiles/sessions/sidebar?recents_profile=coder' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.equal(result.conflict, true)
})

test('resolveSenderRequestTarget keeps the all-sessions sentinel on a bound sender target', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }),
    { path: '/api/profiles/sessions/sidebar?recents_profile=all' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.equal(result.conflict, false)
})

test('resolveSenderRequestTarget keeps the aggregate profile sentinel on a bound sender target', () => {
  const result = resolveSenderRequestTarget(
    makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }),
    { path: '/api/profiles/sessions?profile=all' }
  )

  assert.deepEqual(result.target, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.equal(result.conflict, false)
})

test('resolveSenderRequestTarget rejects conflicting duplicate query profiles', () => {
  const target = makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })

  assert.equal(
    resolveSenderRequestTarget(target, { path: '/api/pairing?profile=worker&profile=coder' }).conflict,
    true
  )
  assert.equal(
    resolveSenderRequestTarget(target, {
      path: '/api/profiles/sessions/sidebar?recents_profile=worker&recents_profile=coder'
    }).conflict,
    true
  )
  assert.equal(
    resolveSenderRequestTarget(target, { path: '/api/profiles/sessions?profile=all&profile=coder' }).conflict,
    true
  )
  assert.equal(
    resolveSenderRequestTarget(target, {
      path: '/api/profiles/sessions/sidebar?recents_profile=all&recents_profile=coder'
    }).conflict,
    true
  )
})

test('resolveSenderRequestTarget rejects aggregate and concrete query authority on a primary sender', () => {
  const target = makeBackendTarget({ kind: 'primary' })

  assert.equal(
    resolveSenderRequestTarget(target, { path: '/api/profiles/sessions?profile=all&profile=worker' }).conflict,
    true
  )
  assert.equal(
    resolveSenderRequestTarget(target, {
      path: '/api/profiles/sessions/sidebar?recents_profile=all&recents_profile=worker'
    }).conflict,
    true
  )
  assert.equal(
    resolveSenderRequestTarget(target, { profile: 'worker', path: '/api/profiles/sessions?profile=all' }).conflict,
    true
  )
  assert.equal(
    resolveSenderRequestTarget(target, {
      body: { profile: 'worker' },
      path: '/api/profiles/sessions/sidebar?recents_profile=all'
    }).conflict,
    true
  )
})

test('resolveSenderRequestTarget preserves sole aggregate intent from explicit or body authority', () => {
  const primary = makeBackendTarget({ kind: 'primary' })

  for (const request of [
    { profile: 'all', path: '/api/profiles/sessions' },
    { body: { profile: 'all' }, path: '/api/profiles/sessions/sidebar' }
  ]) {
    const result = resolveSenderRequestTarget(primary, request)

    assert.deepEqual(result.target, primary)
    assert.equal(result.conflict, false)
  }
})

test('scopedSidebarPathForTarget makes one main-authoritative bound request path', () => {
  const path = scopedSidebarPathForTarget(
    makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }),
    '/api/profiles/sessions/sidebar?profile=all&profile=coder&recents_profile=all&recents_limit=7'
  )

  assert.ok(path)
  const url = new URL(path, 'http://hermes.local')
  assert.equal(url.pathname, '/api/profiles/sessions/sidebar')
  assert.deepEqual(url.searchParams.getAll('profile'), [])
  assert.deepEqual(url.searchParams.getAll('recents_profile'), [])
  assert.deepEqual(url.searchParams.getAll('current_only'), ['true'])
  assert.equal(url.searchParams.get('recents_limit'), '7')
  assert.equal(scopedSidebarPathForTarget(makeBackendTarget({ kind: 'primary' }), path), null)
})

test('scopeSidebarResponseForTarget retags a remote backend to the local target alias', () => {
  const response = {
    recents: {
      sessions: [{ id: 'one', profile: 'remote-name', is_default_profile: true }],
      total: 1,
      profile_totals: { 'remote-name': 1 }
    },
    cron: { sessions: [{ id: 'two', profile: 'remote-name' }] },
    messaging: { sessions: [{ id: 'three', profile: 'remote-name' }], total: 1 },
    errors: []
  }

  const scoped = scopeSidebarResponseForTarget(
    makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }),
    response
  )

  assert.deepEqual(
    [...scoped.recents.sessions, ...scoped.cron.sessions, ...scoped.messaging.sessions].map(row => row.profile),
    ['worker', 'worker', 'worker']
  )
  assert.deepEqual(scoped.recents.profile_totals, { worker: 1 })
  assert.equal(scoped.recents.sessions[0].is_default_profile, false)
  assert.equal(response.recents.sessions[0].profile, 'remote-name')
})

test('connection-bound window stays on that connection for any profile arg', () => {
  const bound = makeBackendTarget({ kind: 'configured-connection', connection: 'atrium-agents' })

  const omitted = resolveSenderTarget(bound, null)
  const named = resolveSenderTarget(bound, 'life')

  assert.deepEqual(omitted.target, bound)
  assert.equal(omitted.overridden, true)
  assert.equal(omitted.conflict, false)
  assert.deepEqual(named.target, bound)
  assert.equal(named.conflict, false)
  assert.equal(sessionProfileForTarget(bound), 'all')
})

test('scoped sidebar path is skipped for the local registry connection', () => {
  const local = makeBackendTarget({ kind: 'configured-connection', connection: 'local' })
  const ssh = makeBackendTarget({ kind: 'configured-connection', connection: 'atrium-agents' })

  assert.equal(scopedSidebarPathForTarget(local, '/api/profiles/sessions/sidebar?recents_profile=all'), null)
  assert.match(
    scopedSidebarPathForTarget(ssh, '/api/profiles/sessions/sidebar?recents_profile=all') || '',
    /current_only=true/
  )
})