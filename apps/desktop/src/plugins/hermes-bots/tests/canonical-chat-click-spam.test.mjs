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
    'globalThis.__open = { openBotCanonicalChat };',
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
