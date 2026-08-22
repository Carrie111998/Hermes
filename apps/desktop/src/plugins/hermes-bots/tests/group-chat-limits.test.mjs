// Per-room limit overrides. Three states per axis (a number, off, or absent)
// and a safety brake that is itself switchable, so the resolver is where a
// wrong default would quietly change how long every room runs.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadLimits() {
  const consts = source.slice(
    source.indexOf('const GROUP_CHAT_MAX_ROUNDS'),
    source.indexOf('/** Per-room limit overrides')
  )
  assert.ok(consts.includes('GROUP_CHAT_LIMIT_CEILINGS'), 'the ceilings block must stay extractable')

  const fn = name => {
    const start = source.indexOf(`function ${name}(`)
    assert.ok(start >= 0, `${name} not found`)
    const end = source.indexOf('\n}\n', start) + 3
    return source.slice(start, end)
  }

  const context = {}
  vm.runInNewContext(
    [
      consts,
      fn('resolveGroupChatLimits'),
      fn('groupChatDriveCaps'),
      fn('durableGroupChatLimits'),
      fn('groupChatBudgetLabel'),
      'globalThis.__resolve = resolveGroupChatLimits',
      'globalThis.__caps = groupChatDriveCaps',
      'globalThis.__durable = durableGroupChatLimits',
      'globalThis.__label = groupChatBudgetLabel',
      'globalThis.__defaults = { GROUP_CHAT_MAX_ROUNDS, GROUP_CHAT_MAX_MESSAGES, GROUP_CHAT_HISTORY_LIMIT, GROUP_CHAT_MAX_MEMBERS }',
      'globalThis.__ceilings = GROUP_CHAT_LIMIT_CEILINGS',
      'globalThis.__safety = GROUP_CHAT_SAFETY_DEFAULTS'
    ].join('\n'),
    context
  )

  return context
}

const { __resolve: resolve, __caps: caps, __durable: durable, __label: label, __defaults: defaults, __ceilings: ceilings, __safety: safety } =
  loadLimits()

/** Objects minted inside the vm carry that realm's prototype, which
 *  deepEqual compares. Compare the data, not the realm. */
const plain = value => (value === null || value === undefined ? value : JSON.parse(JSON.stringify(value)))

test('a room with no overrides gets the shipped defaults', () => {
  for (const room of [undefined, null, {}, { limits: null }, { limits: {} }]) {
    const limits = resolve(room)
    assert.equal(limits.rounds, defaults.GROUP_CHAT_MAX_ROUNDS)
    assert.equal(limits.messages, defaults.GROUP_CHAT_MAX_MESSAGES)
    assert.equal(limits.members, defaults.GROUP_CHAT_MAX_MEMBERS)
    assert.equal(limits.history, defaults.GROUP_CHAT_HISTORY_LIMIT)
  }
})

test('an explicit number wins over the default', () => {
  const limits = resolve({ limits: { rounds: 9, messages: 40, members: 12, history: 60 } })
  assert.deepEqual(
    [limits.rounds, limits.messages, limits.members, limits.history],
    [9, 40, 12, 60]
  )
})

test('null means off, and off is not the same as absent', () => {
  assert.equal(resolve({ limits: { rounds: null } }).rounds, null)
  assert.equal(resolve({ limits: {} }).rounds, defaults.GROUP_CHAT_MAX_ROUNDS)
})

test('a nonsense value falls back instead of disabling the limit', () => {
  for (const bad of [0, -3, 'lots', NaN, {}, [], true]) {
    assert.equal(resolve({ limits: { rounds: bad } }).rounds, defaults.GROUP_CHAT_MAX_ROUNDS, `rounds: ${String(bad)}`)
  }
})

test('a value above the ceiling is clamped, never rejected', () => {
  assert.equal(resolve({ limits: { rounds: 10_000 } }).rounds, ceilings.rounds)
  assert.equal(resolve({ limits: { messages: 10_000 } }).messages, ceilings.messages)
})

test('the safety brake only exists while its axis is off', () => {
  assert.equal(resolve({ limits: { rounds: 5 } }).safetyRounds, null, 'a bounded axis needs no brake')
  assert.equal(resolve({ limits: { rounds: null } }).safetyRounds, safety.rounds)
  assert.equal(resolve({ limits: { rounds: null, safetyRounds: 12 } }).safetyRounds, 12)
  assert.equal(resolve({ limits: { rounds: null, safetyRounds: null } }).safetyRounds, null)
})

test('drive caps collapse the three states into what the loop needs', () => {
  const bounded = caps(resolve({ limits: { rounds: 4 } }))
  assert.equal(bounded.rounds, 4)
  assert.equal(bounded.roundsUnbounded, false)

  const braked = caps(resolve({ limits: { rounds: null, safetyRounds: 20 } }))
  assert.equal(braked.rounds, 20, 'the brake becomes the loop bound')
  assert.equal(braked.roundsUnbounded, false)

  const free = caps(resolve({ limits: { rounds: null, safetyRounds: null } }))
  assert.equal(free.rounds, null, 'nothing bounds the loop by count')
  assert.equal(free.roundsUnbounded, true)
})

test('the stored form keeps only what the room actually set', () => {
  assert.equal(durable(null), null)
  assert.equal(durable({}), null, 'a room on every default stores nothing')
  assert.deepEqual(plain(durable({ rounds: 5 })), { rounds: 5 })
  assert.deepEqual(plain(durable({ rounds: null })), { rounds: null }, 'off must survive a round trip')
  assert.deepEqual(plain(durable({ rounds: 5, bogus: 1 })), { rounds: 5 }, 'unknown axes are dropped')
  assert.equal(durable({ rounds: 0 }), null, 'a nonsense value is not stored')
})

test('a stored override survives the round trip through the resolver', () => {
  const stored = durable({ rounds: null, safetyRounds: null, messages: 40 })
  const limits = resolve({ limits: stored })
  assert.equal(limits.rounds, null)
  assert.equal(limits.safetyRounds, null)
  assert.equal(limits.messages, 40)
})

test('the header label names all three states', () => {
  assert.equal(label({}), `${defaults.GROUP_CHAT_MAX_ROUNDS} rounds · ${defaults.GROUP_CHAT_MAX_MESSAGES} msgs`)
  assert.match(label({ limits: { rounds: null } }), /≤\d+ rounds/)
  assert.match(label({ limits: { rounds: null, safetyRounds: null } }), /∞ rounds/)
  assert.equal(label({ limits: { rounds: 1, messages: 1 } }), '1 round · 1 msg', 'singular reads correctly')
})

test('the drive loop is bounded by the caps, not by the constants', () => {
  const loop = source.slice(source.indexOf('async function runGroupChatRounds('))
  assert.match(loop, /for \(let round = 0; caps\.rounds === null \|\| round < caps\.rounds; round\+\+\)/)
  assert.match(loop, /caps\.messages !== null && posted >= caps\.messages/)
  assert.doesNotMatch(
    loop.slice(0, loop.indexOf('\n}\n')),
    /GROUP_CHAT_MAX_ROUNDS|GROUP_CHAT_MAX_MESSAGES/,
    'the loop must read the room, not the module defaults'
  )
})

test('a room that hits its brake says so instead of going quiet', () => {
  const loop = source.slice(source.indexOf('async function runGroupChatRounds('))
  assert.match(loop, /kind: 'safety', member: null, thread, detail: 'rounds'/)
  assert.match(loop, /kind: 'safety', member: null, thread, detail: 'messages'/)
})

test('the editor warns when the message budget, not the round setting, ends the room', () => {
  const editor = source.slice(source.indexOf('function GroupLimitsControls('))
  const body = editor.slice(0, editor.indexOf('\n}\n'))

  // With N members one round costs N messages, so raising rounds alone is a
  // no-op whenever messages/N is the smaller number. That has to be visible.
  assert.match(body, /Math\.ceil\(effectiveMessages \/ memberCount\) < effectiveRounds/)
  assert.match(body, /raise it too, or the round setting will not change anything/)
})

test('the room header label is what the create dialog and settings also write', () => {
  // One component, three mount points: a second editor would drift.
  const mounts = [...source.matchAll(/jsx\(GroupLimitsControls, \{/g)]
  assert.equal(mounts.length, 2, 'create dialog and group settings mount the shared editor')
  assert.match(source, /children: groupChatBudgetLabel\(room\)/, 'the header shows the same resolved budget')
})
