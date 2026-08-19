import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// #89625: clicking a bot in the roster updates $selectedBot immediately, but
// the chat connection takes a moment to actually swap to that bot's gateway,
// so host.state.profile still reports the previous bot for a beat. The
// Routines pane must scope to the bot the user clicked during that gap, not
// the stale gateway profile.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load() {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: { state: { profile: { listen: () => undefined } } }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat('\nglobalThis.__api = { resolveRoutinesBot, bindProfileSync, $selectedBot };\n')
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  return context
}

test('regression: a fresh roster click wins over a not-yet-switched gateway profile', () => {
  const { resolveRoutinesBot } = load().__api
  assert.equal(resolveRoutinesBot('blog-writer', 'default'), 'blog-writer')
})

test('unit: the gateway profile is used before any bot has been selected', () => {
  const { resolveRoutinesBot } = load().__api
  assert.equal(resolveRoutinesBot('', 'blog-writer'), 'blog-writer')
})

test('unit: falls back to default when nothing is known yet', () => {
  const { resolveRoutinesBot } = load().__api
  assert.equal(resolveRoutinesBot('', ''), 'default')
  assert.equal(resolveRoutinesBot('   ', '   '), 'default')
})

// #89625 follow-up: nanostores' `.listen()` never replays the current value
// the way `.subscribe()` does. Before bindProfileSync reseeded on every call,
// a disable -> profile switch -> re-enable sequence left $selectedBot pointed
// at whichever bot was active before the plugin was disabled, so the
// click-wins fix above would have just relocated the original staleness bug
// rather than closing it. Mimic real nanostores atom semantics here (not a
// mock that assumes the fix) so this fails honestly if bindProfileSync
// regresses to a plain, non-reseeding listen().
function fakeProfileStore(initial) {
  let value = initial
  const listeners = new Set()
  return {
    get: () => value,
    set: next => {
      value = next
      for (const listener of listeners) listener(value)
    },
    listen: listener => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    }
  }
}

test('regression: re-enabling after a profile switch while disabled resyncs, not stays stale', () => {
  const { bindProfileSync, resolveRoutinesBot, $selectedBot } = load().__api
  const profile = fakeProfileStore('blog-writer')

  const unbind1 = bindProfileSync(profile)
  assert.equal($selectedBot.get(), 'blog-writer', 'reseeded to the live profile on first bind')

  unbind1() // plugin disabled: listener torn down
  profile.set('researcher') // profile changes while nothing is listening

  bindProfileSync(profile) // plugin re-enabled: register() runs again
  assert.equal($selectedBot.get(), 'researcher', 'reseeded to the live profile on re-bind, not left stale')
  assert.equal(
    resolveRoutinesBot($selectedBot.get(), profile.get()),
    'researcher',
    'RoutinesPane scopes to the live profile after re-enable'
  )
})

test('unit: bindProfileSync keeps forwarding live profile changes after (re)binding', () => {
  const { bindProfileSync, $selectedBot } = load().__api
  const profile = fakeProfileStore('default')

  bindProfileSync(profile)
  profile.set('researcher')
  assert.equal($selectedBot.get(), 'researcher', 'live changes still flow through the listener')
})
