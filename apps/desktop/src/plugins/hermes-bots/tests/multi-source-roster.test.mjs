import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function runtime() {
  const atom = value => ({ get: () => value, set: () => undefined })
  const jsx = (type, props = {}) => ({ type, props })
  const context = {
    atom,
    jsx,
    jsxs: jsx,
    useQuery: () => ({}),
    useValue: value => (value?.get ? value.get() : value),
    useState: value => [value, () => undefined],
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: {
      state: {
        connectionId: { get: () => 'local', listen: () => undefined },
        profile: { get: () => 'ops', listen: () => undefined }
      },
      request: () => undefined
    },
    sdk: new Proxy({}, { get: () => undefined })
  }
  const code = source
    .replace(/^import \* as sdk from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(
      '\nglobalThis.__mergeMultiSourceRoster = mergeMultiSourceRoster;\nglobalThis.__botHandle = botHandle;\nglobalThis.__botRosterKey = botRosterKey;\nglobalThis.__botRosterMeta = botRosterMeta;\nglobalThis.__displayName = displayName;\nglobalThis.__filterBots = filterBots;'
    )
  vm.runInNewContext(code, context)
  return context
}

test('merge: no union → local list untouched', () => {
  const { __mergeMultiSourceRoster: merge } = runtime()
  const local = { profiles: [{ name: 'default', last_session: { id: 's1' } }] }

  const out = merge(local, { agents: [] })
  assert.equal(out.profiles.length, 1)
  assert.equal(out.profiles[0].name, 'default')
  assert.equal(out.profiles[0].last_session.id, 's1')
})

test('merge: local rows are annotated, remote rows appended with source tags', () => {
  const { __mergeMultiSourceRoster: merge } = runtime()
  const local = { profiles: [{ name: 'research', last_session: { id: 's1' } }] }
  const union = {
    agents: [
      {
        connectionId: 'local',
        connectionKind: 'local',
        connectionLabel: 'This device',
        profile: 'research',
        handle: 'research-this-device'
      },
      {
        connectionId: 'homelab',
        connectionKind: 'remote',
        connectionLabel: 'Homelab',
        profile: 'research',
        handle: 'research-homelab'
      },
      { connectionId: 'homelab', connectionKind: 'remote', connectionLabel: 'Homelab', profile: 'coder', handle: 'coder' }
    ]
  }

  const out = merge(local, union, 'local')
  assert.equal(out.profiles.length, 3)

  const localRow = out.profiles.find(p => p.name === 'research' && !p.remoteSource)
  // Annotated in place — rich fields survive, handle attached.
  assert.equal(localRow.last_session.id, 's1')
  assert.equal(localRow.handle, 'research-this-device')
  assert.equal(localRow.sourceScoped, true)
  assert.equal(localRow.remoteSource, undefined)

  const remoteRow = out.profiles.find(p => p.name === 'research' && p.remoteSource)
  assert.equal(remoteRow.handle, 'research-homelab')
  assert.equal(remoteRow.connectionId, 'homelab')
  assert.equal(remoteRow.connectionLabel, 'Homelab')

  const coder = out.profiles.find(p => p.name === 'coder')
  assert.equal(coder.remoteSource, true)
  assert.equal(coder.handle, 'coder')
})

test('merge: union-only active profiles are NOT invented as thin rows', () => {
  const { __mergeMultiSourceRoster: merge } = runtime()
  const local = { profiles: [{ name: 'default' }] }
  const union = {
    agents: [
      { connectionId: 'local', connectionKind: 'local', connectionLabel: 'This device', profile: 'ghost', handle: 'ghost' }
    ]
  }

  const out = merge(local, union, 'local')
  assert.equal(out.profiles.length, 1)
  assert.equal(out.profiles[0].name, 'default')
})

test('merge: rich rows follow the active remote source, not the local source', () => {
  const { __mergeMultiSourceRoster: merge } = runtime()
  const active = { profiles: [{ name: 'default', last_session: { id: 'remote-session' } }] }
  const union = {
    agents: [
      {
        connectionId: 'local',
        connectionKind: 'local',
        connectionLabel: 'This device',
        profile: 'default',
        handle: 'default-this-device'
      },
      {
        connectionId: 'work',
        connectionKind: 'remote',
        connectionLabel: 'Work',
        profile: 'default',
        handle: 'default-work'
      }
    ]
  }

  const out = merge(active, union, 'work')
  const remote = out.profiles.find(p => p.connectionId === 'work')
  const local = out.profiles.find(p => p.connectionId === 'local')

  assert.equal(remote.last_session.id, 'remote-session')
  assert.equal(remote.sourceScoped, true)
  assert.equal(remote.remoteSource, undefined)
  assert.equal(local.remoteSource, true)
  assert.equal(local.sourceScoped, true)
})

test('merge: repeated refreshes stay idempotent and do not mutate gateway rows', () => {
  const { __mergeMultiSourceRoster: merge } = runtime()
  const rich = { name: 'default', last_session: { id: 'remote-session' } }
  const local = { profiles: [rich, rich] }
  const union = {
    agents: [
      {
        connectionId: 'local',
        connectionKind: 'local',
        connectionLabel: 'This device',
        profile: 'default',
        handle: 'default-this-device'
      },
      {
        connectionId: 'work',
        connectionKind: 'remote',
        connectionLabel: 'Work',
        profile: 'default',
        handle: 'default-work'
      },
      {
        connectionId: 'work',
        connectionKind: 'remote',
        connectionLabel: 'Work',
        profile: 'default',
        handle: 'default-work'
      }
    ]
  }

  const once = merge(local, union, 'work')
  const twice = merge(once, union, 'work')
  const identities = twice.profiles.map(row => `${row.connectionId}:${row.name}`)

  assert.equal(identities.join(','), 'work:default,local:default')
  assert.equal(new Set(identities).size, identities.length)
  assert.equal(rich.connectionId, undefined)
})

test('default rows use source identity without borrowing another source title', () => {
  const { __botRosterKey: key, __botRosterMeta: metaFor, __displayName: name } = runtime()
  const remote = {
    name: 'default',
    connectionId: 'personal',
    connectionLabel: 'Personal',
    remoteSource: true,
    sourceScoped: true
  }
  const active = { ...remote, remoteSource: undefined }
  const metadata = { default: { title: 'Active workspace' } }

  assert.equal(metaFor(remote, metadata), null)
  assert.equal(name(remote, metaFor(remote, metadata)), 'Personal')
  assert.equal(name(active, metadata.default), 'Personal')
  assert.equal(key(remote), 'personal::default')
})

test('botHandle: precomputed multi-source handle wins; default stays hermes', () => {
  const { __botHandle: botHandle } = runtime()

  assert.equal(botHandle('research', { handle: 'research-homelab' }), 'research-homelab')
  assert.equal(botHandle('research', { handle: 'research' }), 'research')
  assert.equal(botHandle('research'), 'research')
  assert.equal(botHandle('default'), 'hermes')
})

test('filterBots: matches the source device name for remote rows', () => {
  const { __filterBots: filterBots } = runtime()
  const roster = [
    { name: 'research' },
    { name: 'research', remoteSource: true, connectionLabel: 'Homelab', handle: 'research-homelab' }
  ]

  const hits = filterBots(roster, {}, 'homelab')
  assert.equal(hits.length, 1)
  assert.equal(hits[0].remoteSource, true)

  // Handle search still narrows to the disambiguated row.
  const byHandle = filterBots(roster, {}, '@research-homelab')
  assert.equal(byHandle.length, 1)
  assert.equal(byHandle[0].handle, 'research-homelab')

  // Bare profile search keeps matching both rows.
  assert.equal(filterBots(roster, {}, 'research').length, 2)
})
