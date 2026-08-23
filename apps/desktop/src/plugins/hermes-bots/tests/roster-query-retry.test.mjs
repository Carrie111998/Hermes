import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// The Bots pane renders a spinner while useRoster() is isLoading and has no
// snapshot. React Query treats `retry: true` as infinite retries, which keeps
// isLoading true forever — the sidebar stays empty with no error/retry card.
// Bounded retries plus the existing 5s refetch recover SSH/sleep drops.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load(hostExtras = {}) {
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
    host: {
      state: {
        profile: { get: () => 'default', listen: () => undefined },
        gateway: { get: () => 'open', listen: () => undefined },
        connectionId: { get: () => 'local', listen: () => undefined }
      },
      request: () => Promise.resolve({ profiles: [] }),
      ...hostExtras
    },
    sdk: new Proxy({}, { get: () => undefined })
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(`
globalThis.__roster = { ROSTER_QUERY_RETRY, activeBotRoute };
`)
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  return context.__roster
}

test('roster query retries are bounded so a stalled profiles.list cannot pin the spinner', () => {
  const { ROSTER_QUERY_RETRY } = load()
  assert.equal(typeof ROSTER_QUERY_RETRY, 'number')
  assert.equal(ROSTER_QUERY_RETRY, 2)
  assert.ok(Number.isInteger(ROSTER_QUERY_RETRY))
  assert.ok(ROSTER_QUERY_RETRY >= 0)
  assert.ok(ROSTER_QUERY_RETRY <= 3)
  assert.notEqual(ROSTER_QUERY_RETRY, true)
})

test('activeBotRoute is null when host.profileRoutes is missing', async () => {
  const { activeBotRoute } = load()
  assert.equal(typeof activeBotRoute, 'function')
  assert.equal(await activeBotRoute(), null)
})

test('activeBotRoute returns the matching connection+profile route', async () => {
  const match = { connectionId: 'local', mode: 'local', profile: 'default', targetProfile: 'default' }
  const { activeBotRoute } = load({
    profileRoutes: async () => [
      { connectionId: 'other', profile: 'ops' },
      match
    ]
  })
  assert.deepEqual(await activeBotRoute(), match)
})

test('activeBotRoute is null when profileRoutes throws or has no match', async () => {
  const thrown = load({
    profileRoutes: async () => {
      throw new Error('inventory down')
    }
  })
  assert.equal(await thrown.activeBotRoute(), null)

  const missed = load({
    profileRoutes: async () => [{ connectionId: 'spark', profile: 'ops' }]
  })
  assert.equal(await missed.activeBotRoute(), null)

  const junk = load({
    profileRoutes: async () => ({ not: 'an array' })
  })
  assert.equal(await junk.activeBotRoute(), null)
})
