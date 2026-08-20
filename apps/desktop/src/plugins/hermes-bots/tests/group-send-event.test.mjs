import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

/** Load the plugin in a vm and capture host.onEvent subscriptions so a
 *  desktop_ui ``bots.group.send`` event can drive the existing room engine. */
function load(turnScript = () => '(pass)') {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
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
      request: async () => ({}),
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
    .concat(
      '\nglobalThis.__gs = { sendToGroupChat, $groupChats, $botMeta, $lastRoster };\n' +
        'try { globalThis.__gs.ingestBotGroupSend = ingestBotGroupSend } catch {}\n'
    )
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
