import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// Minimal load harness (subset of group-chat.test.mjs's load()) exposing only
// what the reconciliation helpers need.
function load() {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const calls = []
  const context = {
    atom,
    setTimeout: fn => { fn(); return 0 },
    clearTimeout: () => undefined,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: {
      request: async (method, params) => { calls.push({ method, params }); return {} },
      onEvent: () => () => undefined,
      state: { profile: { get: () => 'default', listen: () => undefined }, gateway: { listen: () => undefined }, connectionId: { get: () => 'local', listen: () => undefined } },
      activeConnectionId: () => 'local',
      profileRoutes: async () => [],
      notify: () => undefined,
      notifyError: () => undefined
    },
    localStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined },
    queryClient: undefined,
    console,
    crypto: globalThis.crypto
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(
      '\nglobalThis.__gc = { isRoomFedUserText, reconciledRowText, collectExternalGroupEntries, commitExternalGroupEntries, reconcileExternalGroupWrites, $groupChats, updateGroupChat, appendGroupChatEntry, isGroupPassText, groupMemberKey };'
    )
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  context.plugin.register({
    storage: { get: () => null, set: (key, value) => undefined },
    register: () => undefined
  })
  return context.__gc
}

const MARKER = '[Group chat: "mycon_all"] You are @mycon-worker1'

test('room-fed user rows are detected by the marker prefix', t => {
  const gc = load()
  assert.equal(gc.isRoomFedUserText(MARKER + ' anything'), true)
  assert.equal(gc.isRoomFedUserText('plain external post'), false)
  assert.equal(gc.isRoomFedUserText(''), false)
  assert.equal(gc.isRoomFedUserText(null), false)
})

test('reconciledRowText coerces shapes and skips tool rows', t => {
  const gc = load()
  assert.equal(gc.reconciledRowText({ role: 'user', content: 'hello' }), 'hello')
  assert.equal(gc.reconciledRowText({ role: 'assistant', content: [{ text: 'a' }, { text: 'b' }] }), 'ab')
  assert.equal(gc.reconciledRowText({ role: 'tool', content: 'x' }), '')
  assert.equal(gc.reconciledRowText({ role: 'assistant', content: '   ' }), '')
})

test('collectExternalGroupEntries mirrors external user+assistant pairs', t => {
  const gc = load()
  const messages = [
    // already-seen room traffic
    { role: 'user', content: MARKER + ' delta' },
    { role: 'assistant', content: '(pass)' },
    // external exchange
    { role: 'user', content: '【タスク割当】Phase 4 を開始してください' },
    { role: 'assistant', content: '承知しました。開始します。' },
  ]

  const { entries, totalRows } = gc.collectExternalGroupEntries(messages, 2)

  assert.equal(totalRows, 4)
  assert.equal(JSON.stringify(entries), JSON.stringify([
    { role: 'user', text: '【タスク割当】Phase 4 を開始してください' },
    { role: 'assistant', text: '承知しました。開始します。' },
  ]))
})

test('assistant rows after a room-fed prompt are NOT mirrored', t => {
  const gc = load()
  const messages = [
    { role: 'user', content: MARKER + ' delta' },
    { role: 'assistant', content: 'room reply — plugin commits this itself' },
  ]
  const { entries } = gc.collectExternalGroupEntries(messages, 0)
  assert.equal(entries.length, 0)
})

test('tool rows and empty rows never mirror', t => {
  const gc = load()
  const messages = [
    { role: 'user', content: 'external instruction' },
    { role: 'tool', content: '{"output":"..."}' },
    { role: 'assistant', content: '' },
    { role: 'assistant', content: 'final answer' },
  ]
  const { entries } = gc.collectExternalGroupEntries(messages, 0)
  assert.equal(JSON.stringify(entries.map(e => e.text)), JSON.stringify(['external instruction', 'final answer']))
})

test('seenCount window: only unseen rows are considered', t => {
  const gc = load()
  const messages = [
    { role: 'user', content: 'old external post' },
    { role: 'assistant', content: 'old reply' },
    { role: 'user', content: 'new external post' },
  ]
  const { entries } = gc.collectExternalGroupEntries(messages, 2)
  assert.equal(JSON.stringify(entries), JSON.stringify([{ role: 'user', text: 'new external post' }]))
})

test('reconcileExternalGroupWrites advances the cursor with no entries', async t => {
  const gc = load()
  // Seed a room so $groupChats has state.
  gc.$groupChats.set({ mycon_all: { log: [], watermarks: {}, epoch: 0, running: false } })

  const member = { name: 'mycon-worker1' }
  const resumeState = {
    messages: [
      { role: 'user', content: MARKER + ' old delta' },
      { role: 'assistant', content: '(pass)' },
    ]
  }

  const changed = await gc.reconcileExternalGroupWrites('mycon_all', member, resumeState, 0)
  assert.equal(changed, false)
  const room = gc.$groupChats.get().mycon_all
  assert.equal(room.externalCursors['mycon-worker1'], 2)
  assert.equal(room.log.length, 0)
})

test('external writes are mirrored into the room log as member entries', async t => {
  const gc = load()
  gc.$groupChats.set({ mycon_all: { log: [], watermarks: {}, epoch: 0, running: false } })

  const member = { name: 'mycon-worker1' }
  const resumeState = {
    messages: [
      { role: 'user', content: 'manager post: Phase 4 done, verify reports' },
      { role: 'assistant', content: 'verified — all green' },
    ]
  }

  const changed = await gc.reconcileExternalGroupWrites('mycon_all', member, resumeState, 0)
  assert.equal(changed, true)

  const room = gc.$groupChats.get().mycon_all
  assert.equal(room.log.length, 2)
  assert.equal(room.log[0].from.kind, 'member')
  assert.equal(room.log[0].from.name, 'mycon-worker1')
  assert.equal(room.log[0].text, 'manager post: Phase 4 done, verify reports')
  assert.equal(room.log[1].text, 'verified — all green')
  assert.equal(room.externalCursors['mycon-worker1'], 2)
})

test('cap overflow: entries beyond GROUP_RECONCILE_MAX_ENTRIES survive for the next sweep', async t => {
  const gc = load()
  gc.$groupChats.set({ mycon_all: { log: [], watermarks: {}, epoch: 0, running: false } })

  const member = { name: 'mycon-worker1' }
  // 12 external user+assistant pairs = 24 rows, well beyond the cap of 10.
  const messages = []
  for (let i = 1; i <= 12; i++) {
    messages.push({ role: 'user', content: `external post ${i}` })
    messages.push({ role: 'assistant', content: `reply ${i}` })
  }

  // Sweep 1: mirrors only the first 10 entries; cursor stops at the last
  // mirrored row — NOT at transcript end.
  {
    const { entries } = gc.collectExternalGroupEntries(messages, 0)
    assert.equal(entries.length, 10) // capped at collection

    const changed = await gc.reconcileExternalGroupWrites('mycon_all', member, { messages }, 0)
    assert.equal(changed, true)

    const room = gc.$groupChats.get().mycon_all
    assert.equal(room.log.length, 10)
    assert.equal(room.log[0].text, 'external post 1')
    assert.equal(room.log[9].text, 'reply 5')

    // Cursor must sit at the last MIRRORED row (index 9 -> count 10), so the
    // overflow is still visible on the next sweep. This is the #94340 review
    // fix: previously the cursor jumped to messages.length (24), silently
    // dropping entries 11-24 forever.
    assert.equal(room.externalCursors['mycon-worker1'], 10)
  }

  // Sweep 2: the next 10 rows are picked up (cap applies per sweep).
  {
    const changed = await gc.reconcileExternalGroupWrites('mycon_all', member, { messages }, null)
    assert.equal(changed, true)

    let room = gc.$groupChats.get().mycon_all
    assert.equal(room.log.length, 20)
    assert.equal(room.log[19].text, 'reply 10')
    assert.equal(room.externalCursors['mycon-worker1'], 20)
  }

  // Sweep 3: the final 4 rows are picked up.
  {
    const changed = await gc.reconcileExternalGroupWrites('mycon_all', member, { messages }, null)
    assert.equal(changed, true)

    const room = gc.$groupChats.get().mycon_all
    // All 24 delivered: nothing silently dropped despite the cap.
    assert.equal(room.log.length, 24)
    assert.equal(room.externalCursors['mycon-worker1'], 24)

    // Every pair arrived exactly once, in order.
    for (let i = 1; i <= 12; i++) {
      const upost = room.log[(i - 1) * 2]
      const areply = room.log[(i - 1) * 2 + 1]
      assert.equal(upost.text, `external post ${i}`)
      assert.equal(areply.text, `reply ${i}`)
    }
  }

  // Sweep 4: nothing left — cursor stable, no changes.
  {
    const changed = await gc.reconcileExternalGroupWrites('mycon_all', member, { messages }, null)
    assert.equal(changed, false)
    const room = gc.$groupChats.get().mycon_all
    assert.equal(room.externalCursors['mycon-worker1'], 24)
    assert.equal(room.log.length, 24)
  }
})
