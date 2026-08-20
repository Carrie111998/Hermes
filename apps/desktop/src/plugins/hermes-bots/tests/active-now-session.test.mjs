import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load(open = async storedId => storedId) {
  const opened = []
  const start = source.indexOf('async function openActiveBotSession(')
  const end = source.indexOf("\n\n/** Create the bot's ONE forever chat", start)
  const context = {
    host: { openSession: async () => undefined },
    openStoredBotChat: async (name, storedId, summary) => {
      opened.push({ name, storedId, summary })
      return open(storedId)
    }
  }

  assert.notEqual(start, -1, 'active-session opener is missing')
  assert.notEqual(end, -1, 'active-session opener boundary is missing')
  vm.runInNewContext(source.slice(start, end).concat('\nglobalThis.__active = { openActiveBotSession };\n'), context, {
    filename: 'active-now-session.js'
  })

  return { ...context.__active, opened }
}

test('Active now opens the session that supplied the activity signal', async () => {
  const runtime = load()
  const session = {
    id: 'active-session',
    title: 'Running task',
    preview: 'still working',
    last_active: 1_900_000_000,
    message_count: 3
  }

  const result = await runtime.openActiveBotSession('ops', session)

  assert.equal(result, 'active-session')
  assert.deepEqual(runtime.opened, [{ name: 'ops', storedId: 'active-session', summary: session }])
})

test('Active now uses a resolved lineage tip when one is supplied', async () => {
  const runtime = load()
  const session = {
    id: 'pinned-root',
    resolved_id: 'live-tip',
    title: 'Bot Chat',
    message_count: 4
  }

  const result = await runtime.openActiveBotSession('ops', session)

  assert.equal(result, 'live-tip')
  assert.deepEqual(runtime.opened, [{ name: 'ops', storedId: 'live-tip', summary: session }])
})

test('Active now falls back when no human-facing session is available', async () => {
  const runtime = load()

  assert.equal(await runtime.openActiveBotSession('ops', null), null)
  assert.equal(await runtime.openActiveBotSession('ops', { source: 'kanban' }), null)
  assert.deepEqual(runtime.opened, [])
})

test('Active now falls back when the activity session is definitively gone', async () => {
  const runtime = load(async () => {
    throw new Error('Session not found')
  })

  assert.equal(await runtime.openActiveBotSession('ops', { id: 'stale-session' }), null)
  assert.deepEqual(
    runtime.opened.map(call => call.storedId),
    ['stale-session']
  )
})

test('Active now surfaces transient hydration failures instead of opening Home', async () => {
  const runtime = load(async () => {
    throw new Error('Timed out waiting for session history hydration')
  })

  await assert.rejects(runtime.openActiveBotSession('ops', { id: 'live-session' }), /history hydration/)
  assert.deepEqual(
    runtime.opened.map(call => call.storedId),
    ['live-session']
  )
})
