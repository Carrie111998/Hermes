import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// #90458 — blank Bot Chat click-spam mints kickoffs into the launch/default
// ~/.hermes/state.db and overwrites profiles/<bot>/profile.yaml
// ui_meta.hermes-bots.chat. profiles.list looks the pin up in the *bot*
// profile store, so a launch-store kickoff comes back as preferred_session=null
// and used to be treated as "pin is dead" → another mint → another overwrite.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadOpenPath({ openSession, request }) {
  const start = source.indexOf('const canonicalCreations = new Map()')
  const end = source.indexOf('function displayName(', start)
  const saved = []
  const requests = []
  const context = {
    host: {
      openSession,
      request: async (method, params) => {
        requests.push({ method, params })
        return request(method, params)
      }
    },
    saveBotMeta: (name, patch) => saved.push({ name, patch: JSON.parse(JSON.stringify(patch)) }),
    $hideBotChats: { get: () => false },
    window: { setTimeout: callback => callback() }
  }
  const exportLine = [
    '',
    'globalThis.__open = { openBotCanonicalChat, createCanonicalChat };',
    ''
  ].join('\n')
  const section = source.slice(start, end).concat(exportLine)

  assert.notEqual(start, -1, 'canonical chat section is missing')
  assert.notEqual(end, -1, 'canonical chat section delimiter is missing')
  vm.runInNewContext(section, context, { filename: 'canonical-click-spam.js' })
  return { ...context.__open, saved, requests }
}

test('click-spam: extra clicks do not mint a new pin in the launch/default store', async () => {
  const opened = []
  let creates = 0
  const runtime = loadOpenPath({
    openSession: async id => {
      opened.push(id)
    },
    request: async method => {
      if (method === 'profiles.list') {
        // Launch-store kickoff is invisible in profiles/<bot>/state.db.
        return { profiles: [{ name: 'ops', preferred_session: null }] }
      }
      if (method === 'session.create') {
        creates += 1
        return { stored_session_id: `launch-kickoff-${creates}`, session_id: `rt-${creates}` }
      }
      return {}
    }
  })

  const first = await runtime.openBotCanonicalChat('ops', 'bot-profile-chat', null)
  const second = await runtime.openBotCanonicalChat('ops', 'bot-profile-chat', null)

  assert.equal(first, 'bot-profile-chat')
  assert.equal(second, 'bot-profile-chat')
  assert.deepEqual(opened, ['bot-profile-chat', 'bot-profile-chat'])
  assert.equal(creates, 0, 'extra clicks must not session.create into the launch store')
  assert.deepEqual(runtime.saved, [], 'the bot-profile Bot Chat pin must stay put')
})

test('click-spam: a just-minted chat is reused when the next click still has no pin snapshot', async () => {
  let creates = 0
  const runtime = loadOpenPath({
    openSession: async () => undefined,
    request: async method => {
      if (method === 'session.create') {
        creates += 1
        return { stored_session_id: `launch-kickoff-${creates}`, session_id: `rt-${creates}` }
      }
      return {}
    }
  })

  const first = await runtime.openBotCanonicalChat('ops', null, null)
  const second = await runtime.openBotCanonicalChat('ops', null, null)

  assert.equal(first, 'launch-kickoff-1')
  assert.equal(second, 'launch-kickoff-1')
  assert.equal(creates, 1, 'the second click must not mint another launch-store kickoff')
  assert.deepEqual(runtime.saved.filter(entry => entry.patch?.chat), [
    { name: 'ops', patch: { chat: 'launch-kickoff-1' } }
  ])
})

test('click-spam: openSession not-found remints once', async () => {
  // preferred_session=null + opener "not found" is the only proven-missing
  // path. Extra clicks still pass the dead pin (React has not re-rendered);
  // lastCanonicalPins must stop a second launch-store kickoff.
  const opened = []
  let creates = 0
  const runtime = loadOpenPath({
    openSession: async id => {
      if (id === 'dead-pin') throw new Error('session not found')
      opened.push(id)
    },
    request: async method => {
      if (method === 'profiles.list') {
        return { profiles: [{ name: 'ops', preferred_session: null }] }
      }
      if (method === 'session.create') {
        creates += 1
        return { stored_session_id: `remint-${creates}`, session_id: `rt-${creates}` }
      }
      return {}
    }
  })

  const first = await runtime.openBotCanonicalChat('ops', 'dead-pin', null)
  const second = await runtime.openBotCanonicalChat('ops', 'dead-pin', null)

  assert.equal(first, 'remint-1')
  assert.equal(second, 'remint-1')
  assert.equal(creates, 1, 'a proven-missing pin remints once, not on every extra click')
  assert.deepEqual(opened, ['remint-1', 'remint-1'])
  assert.deepEqual(runtime.saved, [
    { name: 'ops', patch: { chat: null } },
    { name: 'ops', patch: { chat: 'remint-1' } }
  ])
})

test('click-spam: openSession timeout does not remint or overwrite the pin', async () => {
  // Hydration/timeout is not proof the pin is gone. Pass the same pin on
  // every click so this cannot accidentally pass via lastCanonicalPins.
  const opened = []
  let creates = 0
  const runtime = loadOpenPath({
    openSession: async id => {
      opened.push(id)
      throw new Error("Timed out loading ops's session history.")
    },
    request: async method => {
      if (method === 'profiles.list') {
        return { profiles: [{ name: 'ops', preferred_session: null }] }
      }
      if (method === 'session.create') {
        creates += 1
        return { stored_session_id: `timeout-kickoff-${creates}`, session_id: `rt-${creates}` }
      }
      return {}
    }
  })

  await assert.rejects(
    runtime.openBotCanonicalChat('ops', 'bot-profile-chat', null),
    /Timed out loading/
  )
  await assert.rejects(
    runtime.openBotCanonicalChat('ops', 'bot-profile-chat', null),
    /Timed out loading/
  )

  assert.deepEqual(opened, ['bot-profile-chat', 'bot-profile-chat'])
  assert.equal(creates, 0, 'timeout must not session.create into the launch store')
  assert.deepEqual(runtime.saved, [], 'timeout must not overwrite the bot-profile pin')
})

test('same-named bots on different connections never reuse the other source pin', async () => {
  const opened = []
  const created = []
  const runtime = loadOpenPath({
    openSession: async id => {
      opened.push(id)
    },
    request: async method => {
      if (method === 'profiles.list') {
        return { profiles: [{ name: 'ops', preferred_session: null }] }
      }
      if (method === 'session.create') {
        created.push(method)
        return { stored_session_id: `mint-${created.length}`, session_id: `rt-${created.length}` }
      }
      return {}
    }
  })

  const sourceA = { name: 'ops', connectionId: 'conn-a' }
  const sourceB = { name: 'ops', connectionId: 'conn-b' }

  const firstA = await runtime.openBotCanonicalChat('ops', 'pin-a', null, sourceA)
  const firstB = await runtime.openBotCanonicalChat('ops', null, null, sourceB)
  const againA = await runtime.openBotCanonicalChat('ops', null, null, sourceA)

  assert.equal(firstA, 'pin-a')
  assert.notEqual(firstB, 'pin-a', 'source B must not open source A pin')
  assert.equal(againA, 'pin-a', 'returning to source A must reuse its own pin')
  assert.equal(opened.includes('pin-a'), true)
  assert.equal(created.length, 1, 'only the null-pin source mints')
  assert.deepEqual(
    runtime.saved.filter(entry => entry.patch?.chat === 'pin-a'),
    [],
    'source B must not save source A pin'
  )
})

test('overlapping creates for same-named bots on different sources do not share inflight', async () => {
  let createStarted = 0
  let releaseFirst
  const firstGate = new Promise(resolve => {
    releaseFirst = resolve
  })
  const runtime = loadOpenPath({
    openSession: async () => undefined,
    request: async method => {
      if (method === 'session.create') {
        createStarted += 1
        const n = createStarted
        if (n === 1) await firstGate
        return { stored_session_id: `mint-${n}`, session_id: `rt-${n}` }
      }
      return {}
    }
  })

  const sourceA = { name: 'ops', connectionId: 'conn-a' }
  const sourceB = { name: 'ops', connectionId: 'conn-b' }
  const pendingA = runtime.createCanonicalChat('ops', sourceA)
  for (let i = 0; i < 20 && createStarted < 1; i += 1) {
    await Promise.resolve()
  }
  assert.equal(createStarted, 1, 'source A create should start first')
  const pendingB = runtime.createCanonicalChat('ops', sourceB)
  for (let i = 0; i < 20 && createStarted < 2; i += 1) {
    await Promise.resolve()
  }
  assert.equal(createStarted, 2, 'same name on different sources must not share one inflight create')
  releaseFirst()

  assert.equal(await pendingA, 'mint-1')
  assert.equal(await pendingB, 'mint-2')
})
