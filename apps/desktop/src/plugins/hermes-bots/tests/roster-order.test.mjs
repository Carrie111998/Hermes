import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')
const modelStart = pluginSource.indexOf('// ── roster ordering model (pure)')
const modelEnd = pluginSource.indexOf('// ── end roster ordering model')

assert.ok(modelStart >= 0 && modelEnd > modelStart, 'roster ordering model is present')

const modelSource = pluginSource.slice(modelStart, modelEnd)
const {
  emptyRosterState,
  moveRosterPeerToBandEnd,
  moveRosterRow,
  normalizeRosterState,
  reconcileRosterState,
  removeRosterPeer,
  reorderRosterRows,
  replaceRosterPeer,
  rosterStateGatewayJsonSize,
  setGroupPinned,
  sortRosterRows
} = Function(`${modelSource}\nreturn { emptyRosterState, moveRosterPeerToBandEnd, moveRosterRow, normalizeRosterState, reconcileRosterState, removeRosterPeer, reorderRosterRows, replaceRosterPeer, rosterStateGatewayJsonSize, setGroupPinned, sortRosterRows }`)()

const row = (peerId, { pinned = false, activity = 0 } = {}) => ({ peerId, pinned, activity })
const ids = rows => rows.map(item => item.peerId)

test('existing users retain pinned-first recency order until they reorder', () => {
  const rows = [
    row('bot:local::recent', { activity: 40 }),
    row('group:id:standup', { pinned: true, activity: 10 }),
    row('bot:local::pinned', { pinned: true, activity: 30 }),
    row('group:id:review', { activity: 20 })
  ]

  assert.deepEqual(ids(sortRosterRows(rows, emptyRosterState())), [
    'bot:local::pinned',
    'group:id:standup',
    'bot:local::recent',
    'group:id:review'
  ])
})

test('group pins enter and leave the existing pinned band', () => {
  const group = 'group:id:standup'
  const rows = [row('bot:local::hermes', { pinned: true }), row(group), row('bot:local::builder')]
  const pinned = setGroupPinned(emptyRosterState(), rows, group, true)

  assert.deepEqual(pinned.pinnedGroups, [group])
  assert.deepEqual(ids(sortRosterRows(rows.map(item => item.peerId === group ? { ...item, pinned: true } : item), pinned)), [
    'bot:local::hermes',
    group,
    'bot:local::builder'
  ])

  assert.deepEqual(setGroupPinned(pinned, rows, group, false).pinnedGroups, [])
})

test('manual order freely intermixes groups and bots inside the pinned band', () => {
  const rows = [
    row('bot:a::one', { pinned: true, activity: 400 }),
    row('group:id:first', { pinned: true, activity: 300 }),
    row('bot:b::one', { pinned: true, activity: 200 }),
    row('group:id:second', { pinned: true, activity: 100 })
  ]
  const state = {
    version: 1,
    manual: true,
    order: ['group:id:first', 'bot:a::one', 'group:id:second', 'bot:b::one'],
    pinnedGroups: ['group:id:first', 'group:id:second']
  }

  assert.deepEqual(ids(sortRosterRows(rows, state)), state.order)
})

test('drag reorder changes pinned rows without moving unpinned rows', () => {
  const rows = [
    row('group:id:a', { pinned: true }),
    row('bot:local::a', { pinned: true }),
    row('group:id:b', { pinned: true }),
    row('bot:local::b')
  ]
  const state = reorderRosterRows(emptyRosterState(), rows, 'group:id:b', 'group:id:a')

  assert.equal(state.manual, true)
  assert.deepEqual(ids(sortRosterRows(rows, state)), [
    'group:id:b',
    'group:id:a',
    'bot:local::a',
    'bot:local::b'
  ])
})

test('drag and keyboard reorder stay inside the unpinned band', () => {
  const rows = [
    row('bot:local::pinned', { pinned: true }),
    row('bot:local::one'),
    row('group:id:room'),
    row('bot:local::two')
  ]
  const dragged = reorderRosterRows(emptyRosterState(), rows, 'bot:local::two', 'bot:local::one')
  const keyed = moveRosterRow(dragged, rows, 'group:id:room', -1)

  assert.deepEqual(ids(sortRosterRows(rows, keyed)), [
    'bot:local::pinned',
    'bot:local::two',
    'group:id:room',
    'bot:local::one'
  ])
  assert.equal(reorderRosterRows(keyed, rows, 'group:id:room', 'bot:local::pinned'), keyed)
})

test('activity updates do not reshuffle an opted-in manual roster', () => {
  const state = {
    version: 1,
    manual: true,
    order: ['bot:local::quiet', 'bot:local::busy'],
    pinnedGroups: []
  }
  const rows = [row('bot:local::busy', { activity: 999 }), row('bot:local::quiet', { activity: 1 })]

  assert.deepEqual(ids(sortRosterRows(rows, state)), state.order)
})

test('new bots and groups append without disturbing saved positions', () => {
  const state = {
    version: 1,
    manual: true,
    order: ['bot:local::one', 'group:id:existing'],
    pinnedGroups: []
  }
  const rows = [
    row('group:id:new', { activity: 500 }),
    row('bot:local::one'),
    row('bot:remote::new', { activity: 600 }),
    row('group:id:existing')
  ]
  const reconciled = reconcileRosterState(state, rows)

  assert.deepEqual(reconciled.order, [
    'bot:local::one',
    'group:id:existing',
    'group:id:new',
    'bot:remote::new'
  ])
  assert.deepEqual(ids(sortRosterRows(rows, reconciled)), reconciled.order)
})

test('pin transitions place a row at the end of its new manual band', () => {
  const state = {
    version: 1,
    manual: true,
    order: ['bot:local::a', 'group:id:a', 'bot:local::b', 'group:id:b'],
    pinnedGroups: ['group:id:a']
  }
  const rows = [
    row('bot:local::a', { pinned: true }),
    row('group:id:a', { pinned: true }),
    row('bot:local::b'),
    row('group:id:b')
  ]

  assert.deepEqual(
    ids(sortRosterRows(rows.map(item => item.peerId === 'group:id:b' ? { ...item, pinned: true } : item), moveRosterPeerToBandEnd(state, rows, 'group:id:b', true))),
    ['bot:local::a', 'group:id:a', 'group:id:b', 'bot:local::b']
  )
})

test('filtered views cannot corrupt hidden order slots', () => {
  const state = {
    version: 1,
    manual: true,
    order: ['bot:local::hidden', 'bot:local::visible', 'group:id:room'],
    pinnedGroups: []
  }
  const fullRows = [row('bot:local::hidden'), row('bot:local::visible'), row('group:id:room')]
  const filteredRows = fullRows.filter(item => item.peerId !== 'bot:local::hidden')

  assert.equal(reconcileRosterState(state, fullRows), state)
  assert.deepEqual(ids(sortRosterRows(filteredRows, state)), ['bot:local::visible', 'group:id:room'])
  assert.deepEqual(state.order, ['bot:local::hidden', 'bot:local::visible', 'group:id:room'])

  const reordered = reorderRosterRows(
    state,
    fullRows,
    'group:id:room',
    'bot:local::visible',
    new Set(filteredRows.map(item => item.peerId))
  )
  assert.deepEqual(reordered.order, ['bot:local::hidden', 'group:id:room', 'bot:local::visible'])
})

test('deletes prune stale order and pin state; durable group rename preserves it', () => {
  const state = {
    version: 1,
    manual: true,
    order: ['bot:a::same', 'bot:b::same', 'group:name:legacy'],
    pinnedGroups: ['group:name:legacy']
  }
  const distinct = removeRosterPeer(state, 'bot:a::same')

  assert.deepEqual(distinct.order, ['bot:b::same', 'group:name:legacy'])
  const renamed = replaceRosterPeer(distinct, 'group:name:legacy', 'group:name:renamed')
  assert.deepEqual(renamed.order, ['bot:b::same', 'group:name:renamed'])
  assert.deepEqual(renamed.pinnedGroups, ['group:name:renamed'])
})

test('stored state hydration is bounded and backwards compatible', () => {
  assert.deepEqual(normalizeRosterState(null), emptyRosterState())
  assert.deepEqual(normalizeRosterState({
    manual: 1,
    order: ['bot:a::one', 'bot:a::one', null, ''],
    pinnedGroups: ['group:id:a', 'group:id:a']
  }), {
    version: 1,
    manual: true,
    order: ['bot:a::one'],
    pinnedGroups: ['group:id:a']
  })

  assert.equal(normalizeRosterState({
    order: Array.from({ length: 300 }, (_, index) => `bot:local::${index}`),
    pinnedGroups: Array.from({ length: 100 }, (_, index) => `group:id:${index}`)
  }).order.length, 192)
  assert.equal(normalizeRosterState({
    pinnedGroups: Array.from({ length: 100 }, (_, index) => `group:id:${index}`)
  }).pinnedGroups.length, 64)

  const escaped = normalizeRosterState({
    manual: true,
    order: Array.from({ length: 192 }, (_, index) => `group:name:${'😀'.repeat(60)}-${index}`),
    pinnedGroups: Array.from({ length: 64 }, (_, index) => `group:name:${'界'.repeat(60)}-${index}`)
  })
  assert.ok(rosterStateGatewayJsonSize(escaped) <= 48000)
  assert.ok(escaped.pinnedGroups.length > 0, 'pins survive before the optional order tail')
})
