import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

/** Load the plugin in a vm and capture host.onEvent subscriptions so a
 *  desktop_ui ``bots.group.send`` event can drive the existing room engine. */
function load() {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const rpc = []
  const eventListeners = new Map()
  const context = {
    atom,
    setTimeout: fn => {
      fn()
      return 0
    },
    clearTimeout: () => undefined,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: {
      request: async (method, params) => {
        rpc.push({ method, params })
        return {}
      },
      state: { profile: { get: () => 'default', listen: () => undefined }, gateway: { listen: () => undefined } },
      notify: () => undefined,
      notifyError: () => undefined,
      onEvent: (type, fn) => {
        const set = eventListeners.get(type) || new Set()
        set.add(fn)
        eventListeners.set(type, set)
        return () => set.delete(fn)
      }
    }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(`
globalThis.__gs = { sendToGroupChat, $groupChats, $botMeta, $lastRoster };
try { globalThis.__gs.ingestBotGroupSend = ingestBotGroupSend } catch {}
try { globalThis.__gs.resolveBotGroupSendMembers = resolveBotGroupSendMembers } catch {}
`)
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  const storageWrites = new Map()
  context.plugin.register({
    storage: { get: () => null, set: (key, value) => storageWrites.set(key, value) },
    register: () => undefined
  })
  return {
    ...context.__gs,
    eventListeners,
    host: context.host,
    rpc,
    emit(type, event) {
      for (const listener of eventListeners.get(type) || []) {
        listener(event)
      }
    }
  }
}

function seatWorkshop(gs) {
  gs.$botMeta.set({
    research: { title: 'Research', groups: ['Workshop'], group: 'Workshop' },
    builder: { title: 'Builder', groups: ['Workshop'], group: 'Workshop' }
  })
  gs.$lastRoster.set([{ name: 'research' }, { name: 'builder' }])
}

function post(gs, payload) {
  if (typeof gs.ingestBotGroupSend === 'function') {
    return gs.ingestBotGroupSend(payload)
  }

  gs.emit('bots.group.send', { type: 'bots.group.send', payload })
  return Boolean((gs.$groupChats.get()[payload.group] || { log: [] }).log.length)
}

test('a bots.group.send event posts into the existing room as a user send', () => {
  const gs = load()
  seatWorkshop(gs)

  const posted = post(gs, { group: 'Workshop', text: 'kick Gate 0' })

  assert.equal(posted, true)
  const log = (gs.$groupChats.get().Workshop || { log: [] }).log
  assert.equal(log.length >= 1, true)
  assert.equal(log[0].from.kind, 'user')
  assert.equal(log[0].text, 'kick Gate 0')
  assert.equal((gs.$groupChats.get().Workshop || {}).running, true)
})

test('unknown group and empty text fail closed without minting a room', () => {
  const gs = load()
  seatWorkshop(gs)

  assert.equal(post(gs, { group: 'DoesNotExist', text: 'hello' }), false)
  assert.equal(gs.$groupChats.get().DoesNotExist, undefined)

  assert.equal(post(gs, { group: 'Workshop', text: '   ' }), false)
  assert.equal(gs.$groupChats.get().Workshop, undefined)
})

test('register subscribes host.onEvent for bots.group.send', () => {
  const gs = load()
  seatWorkshop(gs)

  assert.equal((gs.eventListeners.get('bots.group.send') || new Set()).size >= 1, true)

  gs.emit('bots.group.send', { type: 'bots.group.send', payload: { group: 'Workshop', text: '@builder take Gate 0' } })

  const log = (gs.$groupChats.get().Workshop || { log: [] }).log
  assert.equal(log[0].text, '@builder take Gate 0')
})

test('non-string group or text fail closed instead of String()-coercing', () => {
  const gs = load()
  seatWorkshop(gs)

  assert.equal(post(gs, { group: ['Workshop'], text: 'hello' }), false)
  assert.equal(gs.$groupChats.get().Workshop, undefined)

  assert.equal(post(gs, { group: 'Workshop', text: ['hello'] }), false)
  assert.equal(gs.$groupChats.get().Workshop, undefined)
})

test('a remote same-named member keeps its own title, not the local meta title', () => {
  const gs = load()
  gs.$botMeta.set({
    research: { title: 'Local Research', groups: ['Workshop'], group: 'Workshop' }
  })
  gs.$lastRoster.set([{ name: 'research' }])
  gs.$groupChats.set({
    Workshop: {
      log: [],
      watermarks: {},
      epoch: 0,
      running: false,
      members: [
        {
          name: 'research',
          title: 'Remote Research',
          handle: 'research-mini',
          remoteSource: true,
          connectionId: 'mini',
          connectionLabel: 'Mac Mini'
        }
      ]
    }
  })

  const members = gs.resolveBotGroupSendMembers('Workshop')
  const remote = members.find(member => member.remoteSource)

  assert.equal(Boolean(remote), true)
  assert.equal(remote.title, 'Remote Research')
})

test('a request_id is answered with posted=false for an unknown group', () => {
  const gs = load()
  seatWorkshop(gs)

  gs.emit('bots.group.send', {
    type: 'bots.group.send',
    payload: { group: 'DoesNotExist', text: 'hello', request_id: 'rid-1' }
  })

  const answer = gs.rpc.find(call => call.method === 'bots.group.send.respond')
  assert.equal(Boolean(answer), true)
  assert.equal(answer.params.request_id, 'rid-1')
  assert.equal(JSON.parse(answer.params.text).error.includes('Unknown'), true)
})
