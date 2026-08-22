import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveRemoteSshDashboardProfile } from './connection-config'
import { backendScopeKey, normalizeRegistry, REGISTRY_VERSION } from './connection-registry'
import {
  effectiveDialDigest,
  registryTargetForRoute,
  resolveDesktopRemoteRoute,
  sshPayloadFieldsMatch
} from './desktop-remote-route'

const tokenA = { encoding: 'plain', value: 'token-a' }
const tokenB = { encoding: 'plain', value: 'token-b' }

function registry(primary: string, connections: Record<string, unknown>[]) {
  return normalizeRegistry({
    version: REGISTRY_VERSION,
    primary,
    connections: [{ id: 'local', kind: 'local', label: 'This device' }, ...connections]
  })
}

test('profile remote wins precedence and carries one exact registry id', () => {
  const route = resolveDesktopRemoteRoute({
    config: {
      mode: 'remote',
      remote: { url: 'https://global.test', authMode: 'token', token: tokenB },
      profiles: {
        worker: { mode: 'remote', url: 'https://worker.test/', authMode: 'token', token: tokenA }
      }
    },
    env: { url: 'https://env.test', token: 'env-token' },
    profile: 'worker',
    registry: registry('global', [
      { id: 'global', kind: 'remote', label: 'Global', url: 'https://global.test', token: tokenB },
      { id: 'worker', kind: 'remote', label: 'Worker', url: 'https://worker.test', token: tokenA }
    ])
  })

  assert.equal(route?.kind, 'remote')
  assert.equal(route?.source, 'profile')
  assert.equal(route?.connectionId, 'worker')
})

test('environment route wins over global but never claims a registry id', () => {
  const route = resolveDesktopRemoteRoute({
    config: { mode: 'remote', remote: { url: 'https://global.test', token: tokenB } },
    env: { url: 'https://env.test', token: 'token-a' },
    registry: registry('env', [{ id: 'env', kind: 'remote', label: 'Env', url: 'https://env.test', token: tokenA }])
  })

  assert.equal(route?.source, 'env')
  assert.equal(route?.connectionId, undefined)
  assert.equal(route?.kind === 'remote' ? route.url : null, 'https://env.test')
})

test('environment URL without its token keeps the existing error', () => {
  assert.throws(
    () =>
      resolveDesktopRemoteRoute({
        config: { mode: 'local' },
        env: { url: 'https://env.test' },
        registry: registry('local', [])
      }),
    /HERMES_DESKTOP_REMOTE_TOKEN is not/
  )
})

test('global remote uses exact primary provenance when another row is identical', () => {
  const route = resolveDesktopRemoteRoute({
    config: { mode: 'remote', remote: { url: 'https://gateway.test/', authMode: 'token', token: tokenA } },
    registry: registry('gateway-primary', [
      { id: 'gateway-primary', kind: 'remote', label: 'Primary', url: 'https://gateway.test', token: tokenA },
      { id: 'gateway-copy', kind: 'remote', label: 'Copy', url: 'https://gateway.test', token: tokenA }
    ])
  })

  assert.equal(route?.source, 'settings')
  assert.equal(route?.connectionId, 'gateway-primary')
})

test('global route fails closed when primary differs, even if another row matches', () => {
  const route = resolveDesktopRemoteRoute({
    config: { mode: 'remote', remote: { url: 'https://gateway.test', authMode: 'token', token: tokenA } },
    registry: registry('other', [
      { id: 'other', kind: 'remote', label: 'Other', url: 'https://other.test', token: tokenB },
      { id: 'matching', kind: 'remote', label: 'Matching', url: 'https://gateway.test', token: tokenA }
    ])
  })

  assert.equal(route?.connectionId, undefined)
})

test('profile SSH identity includes port, key, paths, and remote profile', () => {
  const ssh = {
    mode: 'ssh',
    host: 'box.test',
    user: 'hermes',
    port: 2222,
    keyPath: '/keys/a',
    remoteHermesPath: '/srv/hermes',
    remoteProfile: 'worker'
  }

  const route = resolveDesktopRemoteRoute({
    config: { mode: 'local', profiles: { worker: ssh } },
    profile: 'worker',
    registry: registry('local', [
      { id: 'wrong-port', kind: 'ssh', label: 'Wrong port', ...ssh, port: 22 },
      { id: 'worker-ssh', kind: 'ssh', label: 'Worker SSH', ...ssh }
    ])
  })

  assert.equal(route?.kind, 'ssh')
  assert.equal(route?.connectionId, 'worker-ssh')
})

test('profile SSH route fails closed when any dial field differs', () => {
  const ssh = {
    mode: 'ssh',
    host: 'box.test',
    user: 'hermes',
    port: 2222,
    keyPath: '/keys/a',
    remoteHermesPath: '/srv/hermes',
    remoteProfile: 'worker'
  }

  const variants = [
    { ...ssh, port: 2200 },
    { ...ssh, keyPath: '/keys/b' },
    { ...ssh, remoteHermesPath: '/opt/hermes' },
    { ...ssh, remoteProfile: 'default' },
    { ...ssh, user: 'other' }
  ]

  for (const [index, variant] of variants.entries()) {
    const route = resolveDesktopRemoteRoute({
      config: { mode: 'local', profiles: { worker: ssh } },
      profile: 'worker',
      registry: registry('local', [{ id: `ssh-${index}`, kind: 'ssh', label: `SSH ${index}`, ...variant }])
    })

    assert.equal(route?.connectionId, undefined)
  }
})

test('global SSH treats an omitted port as 22 and checks the primary route', () => {
  const route = resolveDesktopRemoteRoute({
    config: { mode: 'ssh', remote: { mode: 'ssh', host: 'box.test', user: 'hermes' } },
    registry: registry('ssh-primary', [
      { id: 'ssh-primary', kind: 'ssh', label: 'SSH primary', host: 'box.test', user: 'hermes', port: 22 }
    ])
  })

  assert.equal(route?.kind, 'ssh')
  assert.equal(route?.connectionId, 'ssh-primary')
})

test('an exact registry-backed primary SSH route delegates to the registry backend', () => {
  const route = resolveDesktopRemoteRoute({
    config: { mode: 'ssh', remote: { mode: 'ssh', host: 'box.test', user: 'hermes' } },
    profile: 'default',
    registry: registry('ssh-primary', [
      { id: 'ssh-primary', kind: 'ssh', label: 'SSH primary', host: 'box.test', user: 'hermes', port: 22 }
    ])
  })

  assert.deepEqual(registryTargetForRoute(route, 'default'), {
    connectionId: 'ssh-primary',
    profile: 'default'
  })
  assert.deepEqual(registryTargetForRoute(route, null), { connectionId: 'ssh-primary', profile: 'default' })
  assert.deepEqual(registryTargetForRoute(route, '  '), { connectionId: 'ssh-primary', profile: 'default' })
})

test('a route without a registry identity keeps the historical v1 path', () => {
  assert.equal(registryTargetForRoute(null, 'default'), null)
  assert.equal(
    registryTargetForRoute(
      { kind: 'ssh', source: 'settings', ssh: { host: 'box.test', mode: 'ssh', user: 'hermes' } },
      'default'
    ),
    null
  )

  // Two identical registry entries are ambiguous, so resolution deliberately
  // drops the identity: delegating to an arbitrary one of them would move the
  // backend under a scope the user never picked.
  const ssh = { mode: 'ssh', host: 'box.test', user: 'hermes' }

  const ambiguous = resolveDesktopRemoteRoute({
    config: { mode: 'ssh', remote: ssh },
    profile: 'default',
    registry: registry('ssh-a', [
      { id: 'ssh-a', kind: 'ssh', label: 'A', ...ssh },
      { id: 'ssh-b', kind: 'ssh', label: 'B', ...ssh }
    ])
  })

  assert.equal(ambiguous?.connectionId, 'ssh-a')

  const unrelatedPrimary = resolveDesktopRemoteRoute({
    config: { mode: 'ssh', remote: ssh },
    profile: 'default',
    registry: registry('other', [
      { id: 'other', kind: 'ssh', label: 'Other', host: 'other.test', user: 'hermes' },
      { id: 'ssh-a', kind: 'ssh', label: 'A', ...ssh }
    ])
  })

  assert.equal(registryTargetForRoute(unrelatedPrimary, 'default'), null)
})

test('a global SSH route never merges into a registry entry that dials differently', () => {
  const ssh = {
    mode: 'ssh',
    host: 'box.test',
    user: 'hermes',
    port: 2222,
    keyPath: '/keys/a',
    remoteHermesPath: '/srv/hermes',
    remoteProfile: 'worker'
  }

  const variants = [
    { ...ssh, port: 2200 },
    { ...ssh, keyPath: '/keys/b' },
    { ...ssh, remoteHermesPath: '/opt/hermes' },
    { ...ssh, remoteProfile: 'other' },
    { ...ssh, user: 'other' },
    { ...ssh, host: 'other.test' }
  ]

  for (const [index, variant] of variants.entries()) {
    const route = resolveDesktopRemoteRoute({
      config: { mode: 'ssh', remote: ssh },
      profile: 'default',
      registry: registry(`ssh-${index}`, [{ id: `ssh-${index}`, kind: 'ssh', label: `SSH ${index}`, ...variant }])
    })

    assert.equal(registryTargetForRoute(route, 'default'), null)
  }
})

test('a v1 route and a direct registry call join one bootstrap for the same pair', async () => {
  const route = resolveDesktopRemoteRoute({
    config: { mode: 'ssh', remote: { mode: 'ssh', host: 'box.test', user: 'hermes' } },
    profile: 'default',
    registry: registry('ssh-primary', [
      { id: 'ssh-primary', kind: 'ssh', label: 'SSH primary', host: 'box.test', user: 'hermes', port: 22 }
    ])
  })

  // Mirrors ensureRegistryBackend()'s pool: one composite key, one in-flight
  // connection promise. The point of the delegation is that the legacy route
  // now lands on that key instead of minting a second SSH scope.
  const pool = new Map<string, Promise<string>>()
  let bootstraps = 0

  const ensureRegistryBackend = (connectionId: string, profile: string) => {
    const key = backendScopeKey(connectionId, profile)
    const existing = pool.get(key)

    if (existing) {
      return existing
    }

    bootstraps += 1
    const promise = Promise.resolve(key)
    pool.set(key, promise)

    return promise
  }

  const target = registryTargetForRoute(route, 'default')

  assert.ok(target)

  const [viaV1, viaRegistry] = await Promise.all([
    ensureRegistryBackend(target.connectionId, target.profile),
    ensureRegistryBackend('ssh-primary', 'default')
  ])

  assert.equal(bootstraps, 1)
  assert.equal(viaV1, viaRegistry)
  assert.equal(viaV1, 'conn:ssh-primary::default')
})

test('the delegated default profile stays the remote root profile', () => {
  const route = resolveDesktopRemoteRoute({
    config: { mode: 'ssh', remote: { mode: 'ssh', host: 'box.test', user: 'hermes' } },
    profile: 'default',
    registry: registry('ssh-primary', [
      { id: 'ssh-primary', kind: 'ssh', label: 'SSH primary', host: 'box.test', user: 'hermes', port: 22 }
    ])
  })

  const target = registryTargetForRoute(route, 'default')

  assert.ok(target)
  const poolKey = backendScopeKey(target.connectionId, target.profile)

  assert.equal(poolKey, 'conn:ssh-primary::default')
  // The composite pool key is a DESKTOP routing label. What reaches the remote
  // `hermes serve` must stay the root profile — never `conn:<id>::default`.
  assert.equal(resolveRemoteSshDashboardProfile('', poolKey), '')
  assert.equal(resolveRemoteSshDashboardProfile('', backendScopeKey(target.connectionId, 'writer')), 'writer')
})

test('profile route omits identity when two registry entries match exactly', () => {
  const block = { mode: 'remote', url: 'https://worker.test', authMode: 'token', token: tokenA }

  const route = resolveDesktopRemoteRoute({
    config: { mode: 'local', profiles: { worker: block } },
    profile: 'worker',
    registry: registry('local', [
      { id: 'worker-a', kind: 'remote', label: 'Worker A', ...block },
      { id: 'worker-b', kind: 'remote', label: 'Worker B', ...block }
    ])
  })

  assert.equal(route?.connectionId, undefined)
})

test('kind, auth material, headers, and Cloud org stay part of route identity', () => {
  const cloud = {
    mode: 'cloud',
    url: 'https://cloud.test',
    authMode: 'oauth',
    headers: { 'CF-Access': { encoding: 'plain', value: 'a' } },
    org: 'org-a'
  }

  const route = resolveDesktopRemoteRoute({
    config: { mode: 'cloud', remote: cloud },
    registry: registry('cloud', [
      { id: 'cloud', kind: 'cloud', label: 'Cloud', ...cloud },
      { id: 'remote', kind: 'remote', label: 'Remote', ...cloud },
      { id: 'other-org', kind: 'cloud', label: 'Other org', ...cloud, org: 'org-b' }
    ])
  })

  assert.equal(route?.kind, 'cloud')
  assert.equal(route?.connectionId, 'cloud')
})

test('URL route fails closed for different token, headers, kind, or Cloud org', () => {
  const cases = [
    {
      config: { mode: 'remote', remote: { url: 'https://gateway.test', token: tokenA } },
      primary: { kind: 'remote', url: 'https://gateway.test', token: tokenB }
    },
    {
      config: {
        mode: 'remote',
        remote: {
          url: 'https://gateway.test',
          token: tokenA,
          headers: { 'CF-Access': { encoding: 'plain', value: 'a' } }
        }
      },
      primary: {
        kind: 'remote',
        url: 'https://gateway.test',
        token: tokenA,
        headers: { 'CF-Access': { encoding: 'plain', value: 'b' } }
      }
    },
    {
      config: { mode: 'remote', remote: { url: 'https://gateway.test', token: tokenA } },
      primary: { kind: 'cloud', url: 'https://gateway.test', token: tokenA }
    },
    {
      config: { mode: 'cloud', remote: { url: 'https://gateway.test', authMode: 'oauth', org: 'a' } },
      primary: { kind: 'cloud', url: 'https://gateway.test', authMode: 'oauth', org: 'b' }
    }
  ]

  for (const [index, item] of cases.entries()) {
    const route = resolveDesktopRemoteRoute({
      config: item.config,
      registry: registry('primary', [{ id: 'primary', label: `Primary ${index}`, ...item.primary }])
    })

    assert.equal(route?.connectionId, undefined)
  }
})

// A real `ssh -G` dump, trimmed to the keywords that decide the destination.
function dump(overrides: Record<string, string> = {}) {
  const fields: Record<string, string> = {
    host: 'galaxybook2',
    hostname: '100.124.139.54',
    user: 'thibaut-roux',
    port: '22',
    addressfamily: 'any',
    identityfile: '~/.ssh/id_ed25519',
    proxyjump: 'none',
    ...overrides
  }

  return Object.entries(fields)
    .map(([key, value]) => `${key} ${value}`)
    .join('\n')
}

test('an ssh.config alias and its resolved host have the same effective dial', () => {
  // This is the shape that shipped two backends: connection.json kept the
  // ~/.ssh/config alias while the registry entry stored the resolved host+user.
  assert.equal(
    effectiveDialDigest(dump({ host: 'galaxybook2' })),
    effectiveDialDigest(dump({ host: '100.124.139.54' }))
  )
  assert.equal(effectiveDialDigest(dump()), effectiveDialDigest(`${dump()}\r\n`))
})

test('anything that changes where or as whom ssh connects breaks the dial match', () => {
  const base = effectiveDialDigest(dump())

  for (const override of [
    { hostname: '10.0.0.9' },
    { user: 'someone-else' },
    { port: '2222' },
    { identityfile: '~/.ssh/other' },
    { proxyjump: 'bastion.test' }
  ]) {
    assert.notEqual(base, effectiveDialDigest(dump(override)), JSON.stringify(override))
  }

  // `hostname` must survive the `host ` filter, or every target on the same
  // config file would look identical.
  assert.notEqual(effectiveDialDigest('hostname a'), effectiveDialDigest('hostname b'))
  assert.equal(effectiveDialDigest('host a\nhostname z'), effectiveDialDigest('host b\nhostname z'))
})

test('remote Hermes path and remote profile are never resolved by ssh -G', () => {
  assert.equal(sshPayloadFieldsMatch({}, { remoteHermesPath: '', remoteProfile: '' }), true)
  assert.equal(sshPayloadFieldsMatch({ remoteHermesPath: '/srv/hermes' }, { remoteHermesPath: '/srv/hermes' }), true)
  assert.equal(sshPayloadFieldsMatch({ remoteHermesPath: '/srv/hermes' }, { remoteHermesPath: '/opt/hermes' }), false)
  assert.equal(sshPayloadFieldsMatch({ remoteProfile: 'writer' }, { remoteProfile: '' }), false)
})

test('local config without overrides returns null', () => {
  assert.equal(resolveDesktopRemoteRoute({ config: { mode: 'local' }, registry: registry('local', []) }), null)
})
