import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// Extract the REAL function. This extraction is itself the regression guard:
// the #90006 reconciliation merge (0404020f7b) dropped this definition while
// keeping both call sites (useRoster's queryFn and sweepBotProfileSessions),
// so every roster fetch threw ReferenceError and react-query's retry masked
// it as a permanent "Waking up ..." spinner — the Bots tab never populated.
// Nothing else can catch that class of loss here: plugin.js is outside the
// eslint file set (*.{ts,tsx} only), outside tsc, and the vm tests exercise
// slices rather than the whole module.
function loadActiveBotRoute(host) {
  const start = source.indexOf('async function activeBotRoute()')
  assert.notEqual(start, -1, 'activeBotRoute must be DEFINED in plugin.js, not just called')
  const end = source.indexOf('\n}', start) + 2
  const context = { host }
  vm.createContext(context)
  vm.runInContext(`${source.slice(start, end)}\nglobalThis.__fn = activeBotRoute`, context)

  return context.__fn
}

function hostWith({ profile, connectionId, routes, agents }) {
  return {
    profileRoutes: routes ? async () => routes : undefined,
    agents,
    activeConnectionId: () => connectionId,
    state: {
      profile: { get: () => profile },
      connectionId: { get: () => connectionId }
    }
  }
}

test('resolves the active (connection, profile) pair to its route', async () => {
  const route = { connectionId: 'mini', profile: 'researcher' }
  const fn = loadActiveBotRoute(hostWith({ profile: 'researcher', connectionId: 'mini', routes: [route] }))

  assert.equal(await fn(), route)
})

test('legacy host without profileRoutes resolves to null (local path)', async () => {
  const fn = loadActiveBotRoute({ state: {} })

  assert.equal(await fn(), null)
})

test('a multi-source host with no route for the active bot fails loudly', async () => {
  const fn = loadActiveBotRoute(
    hostWith({ profile: 'researcher', connectionId: 'mini', routes: [], agents: async () => [] })
  )

  await assert.rejects(fn(), /No route for active bot mini::researcher/)
})

test('a single-source host with no matching route falls back to null', async () => {
  const fn = loadActiveBotRoute(hostWith({ profile: 'default', connectionId: 'local', routes: [] }))

  assert.equal(await fn(), null)
})
