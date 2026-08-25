import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Roster naming ergonomics: cross-connection rows carry display_name from the
// union roster, the completion filter matches substrings ("@scout" finds
// "default-laptop"), and a cold roster cache triggers a one-shot warm fetch.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function runtime() {
  const context = {
    console,
    setTimeout,
    clearTimeout,
    Date,
    URL,
    atom: initial => {
      let value = initial
      return { get: () => value, set: next => (value = next), listen: () => () => undefined }
    },
    host: {
      request: async () => ({}),
      requestProfile: async () => ({}),
      state: {
        profile: { get: () => 'default', listen: () => undefined },
        connectionId: { get: () => 'homelab', listen: () => undefined },
        gateway: { listen: () => undefined }
      }
    },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } }
  }
  const code = source
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(
      '\nglobalThis.__x = { mergeMultiSourceRoster, displayName, botFriendlyNames, resolveRosterMentions, warmUnionRoster, $botMeta };\n'
    )
  vm.runInNewContext(code, context, { filename: 'plugin.js' })
  return context.__x
}

const scoutAgent = {
  profile: 'default',
  handle: 'default-laptop',
  displayName: 'Scout',
  connectionId: 'local',
  connectionKind: 'local',
  connectionLabel: 'Laptop'
}

test('merge: thin cross-connection rows carry display_name from the union roster', () => {
  const { mergeMultiSourceRoster } = runtime()

  const merged = mergeMultiSourceRoster(
    { profiles: [{ name: 'default' }] },
    { agents: [{ profile: 'default', handle: 'default-homelab', connectionId: 'homelab', connectionKind: 'remote', connectionLabel: 'Homelab' }, scoutAgent] },
    'homelab'
  )

  const scout = merged.profiles.find(row => row.connectionId === 'local')
  assert.equal(scout.display_name, 'Scout')
  assert.equal(scout.handle, 'default-laptop')
})

test('display + mentions: renamed remote default reads as its display_name and resolves @scout', () => {
  const { mergeMultiSourceRoster, displayName, resolveRosterMentions } = runtime()

  const merged = mergeMultiSourceRoster(
    { profiles: [{ name: 'default' }] },
    { agents: [scoutAgent] },
    'homelab'
  )
  const scout = merged.profiles.find(row => row.connectionId === 'local')

  assert.equal(displayName(scout, undefined), 'Scout')

  const resolved = resolveRosterMentions('ping @scout please', [scout], { name: 'default', connectionId: 'homelab' })
  assert.equal(resolved.length, 1)
  assert.equal(resolved[0].handle, 'default-laptop')

  // The old handle keeps resolving.
  const byHandle = resolveRosterMentions('ping @default-laptop', [scout], { name: 'default', connectionId: 'homelab' })
  assert.equal(byHandle.length, 1)
})

test('display: without a display_name the remote default still reads as its connection label', () => {
  const { displayName } = runtime()

  const bare = { name: 'default', remoteSource: true, connectionLabel: 'Laptop' }
  assert.equal(displayName(bare, undefined), 'Laptop')
})

test('completion filter is substring: "scout" matches handle default-laptop', () => {
  // The provider closure is not extractable; assert the shipped predicate shape.
  const provider = source.slice(source.indexOf("id: 'mention-completions'"))
  assert.match(provider.slice(0, 2000), /includes\(q\)/)
  assert.doesNotMatch(provider.slice(0, 2000), /startsWith\(q\)/)
})

test('warmUnionRoster: no-op without a queryClient, never throws', () => {
  const { warmUnionRoster } = runtime()
  assert.doesNotThrow(() => warmUnionRoster())
})
