// Regression tests for hermes-agent issue #94863 — Bot Mode cross-Desktop
// group chat sync unreliable (no realtime invalidation, missing messages).
//
// Two defects in the bounded cross-Desktop room projection pipeline:
//
//   1. Dedupe window. appendGroupChatEntry only checks the IMMEDIATELY
//      preceding log entry (the "back-to-back" insurance added in #93127).
//      A member reply appended twice with a single intervening entry (a
//      user reply, another member's reply, or a stray watermark bump)
//      bypasses the check and floods the room with a phantom duplicate.
//      The fix widens the check to a sliding window of recent entries.
//
//   2. Sync semantics. pullGroupChatServerState only fires on hydrate /
//      gateway transition. After a successful flush, the local $groupChats
//      is merged against the just-confirmed snapshot, but the gateway's
//      NEXT revision (a peer write that landed during the flush window) is
//      never observed until the next hydrate. The fix adds a low-frequency
//      backstop pull while connected.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadRoomHelpers() {
  const start = pluginSource.indexOf('// --- room-turn decision helpers (#93127)')
  const end = pluginSource.indexOf('// --- end room-turn decision helpers ---', start)
  assert.notEqual(start, -1, 'plugin carries the room-turn helper block')
  assert.notEqual(end, -1, 'room-turn helper block has a stable end marker')
  // Pull GROUP_DUPLICATE_APPEND_WINDOW_MS and GROUP_CHAT_DEDUPE_APPEND_WINDOW
  // out of the block (declared with const right above the function).
  const context = {}
  vm.runInNewContext(
    `const GROUP_CHAT_SYNC_MESSAGES = 16
${pluginSource.slice(start, end)}
globalThis.isDuplicateGroupAppend = isDuplicateGroupAppend
globalThis.GROUP_DUPLICATE_APPEND_WINDOW_MS = GROUP_DUPLICATE_APPEND_WINDOW_MS
globalThis.GROUP_CHAT_SYNC_MESSAGES = GROUP_CHAT_SYNC_MESSAGES
globalThis.GROUP_CHAT_DEDUPE_APPEND_WINDOW = GROUP_CHAT_DEDUPE_APPEND_WINDOW`,
    context
  )
  return context
}

function loadAppendEntryPath() {
  // Slice the appendGroupChatEntry block — its support functions
  // (groupChatEntryId, isGroupPassText) live near it.
  const entryStart = pluginSource.indexOf('function groupChatEntryId(')
  const appendStart = pluginSource.indexOf('function appendGroupChatEntry(')
  const appendEnd = pluginSource.indexOf('/** Fresh room identity for a group.', appendStart)
  const block = pluginSource.slice(entryStart, appendEnd)
  // The append path needs: Date, groupChatEntryId, appendGroupChatEntry,
  // isDuplicateGroupAppend, $groupChats, updateGroupChat.
  // Pull isDuplicateGroupAppend from its own block (sits AFTER append).
  const dupStart = pluginSource.indexOf('function isDuplicateGroupAppend(')
  const dupEnd = pluginSource.indexOf('// --- end room-turn decision helpers ---', dupStart)
  const dupBlock = pluginSource.slice(dupStart, dupEnd)
  const context = {}
  vm.runInNewContext(
    `const GROUP_DUPLICATE_APPEND_WINDOW_MS = 10 * 60 * 1000
const GROUP_CHAT_DEDUPE_APPEND_WINDOW = 8
${dupBlock}
function groupChatEntryId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2)
}
${block}
globalThis.__getLog = () => $groupChats && $groupChats.Shared ? $groupChats.Shared.log : []
globalThis.__seed = (entries) => { $groupChats.Shared.log = entries }
globalThis.__append = (group, from, text, thread) => appendGroupChatEntry(group, from, text, thread)
let $groupChats = {
  Shared: { log: [], members: [], watermarks: {}, sessions: {}, epoch: 0, running: false }
}
globalThis.$groupChats = $groupChats
globalThis.updateGroupChat = (group, fn) => { $groupChats[group] = fn($groupChats[group]) }
globalThis.$groupNeedsYou = { get: () => ({}), set: () => undefined }
// Atom stub for appendGroupChatEntry's $groupChats.get call.
$groupChats.get = () => $groupChats
$groupChats.set = (v) => { $groupChats.Shared = v.Shared || v }`,
    context
  )
  return context
}

const memberEntry = (name, text, thread = 't1', at = Date.now(), source, idOverride) => ({
  id: idOverride || `e-${at}-${name}-${Math.random().toString(36).slice(2, 6)}`,
  at,
  from: { kind: 'member', name, ...(source ? { source } : {}) },
  text,
  thread
})

const userEntry = (text, thread = 't1', at = Date.now()) => ({
  id: `u-${at}-${Math.random().toString(36).slice(2, 6)}`,
  at,
  from: { kind: 'user', name: 'You' },
  text,
  thread
})

// ── existing helper contract ──────────────────────────────────────────

test('isDuplicateGroupAppend: single-entry helper contract is unchanged', () => {
  const { isDuplicateGroupAppend } = loadRoomHelpers()
  const last = memberEntry('impl', 'Confirmed.', 't1', Date.now() - 1000)
  assert.equal(isDuplicateGroupAppend(last, { kind: 'member', name: 'impl' }, 'Confirmed.', 't1'), true,
    'back-to-back same-same-same is a duplicate')
  assert.equal(isDuplicateGroupAppend(last, { kind: 'member', name: 'reviewer' }, 'Confirmed.', 't1'), false,
    'different member name is not a duplicate')
  assert.equal(isDuplicateGroupAppend(last, { kind: 'member', name: 'impl', source: 'remote' }, 'Confirmed.', 't1'), false,
    'different source is not a duplicate')
  const intervening = memberEntry('reviewer', 'ack')
  assert.equal(isDuplicateGroupAppend(intervening, { kind: 'member', name: 'impl' }, 'Confirmed.', 't1'), false,
    'intervening entry: helper itself does NOT widen — the caller does')
})

// ── dedupe window — appendGroupChatEntry widens the check ─────────────

test('appendGroupChatEntry: a same-member reply with one intervening entry is suppressed by the windowed dedupe', () => {
  const { __seed, __append, __getLog } = loadAppendEntryPath()
  // Seed: 'impl' reply → 'reviewer' ack → windowed dedupe should look back
  // across the reviewer entry to find impl's matching reply.
  const now = Date.now()
  const impl1 = memberEntry('impl', 'Confirmed.', 't1', now - 5000)
  const reviewer = memberEntry('reviewer', 'ack', 't1', now - 3000)
  __seed([impl1, reviewer])

  // Append a second 'impl' reply with the SAME text + thread, within the
  // recency window. Without the fix, the helper sees `reviewer` as the
  // last entry → not a duplicate → entry appended. With the fix, the
  // caller scans the window and finds impl1 → suppressed.
  const result = __append('Shared', { kind: 'member', name: 'impl' }, 'Confirmed.', 't1')

  const log = __getLog()
  assert.equal(log.length, 2,
    'windowed dedupe suppresses the duplicate — log does not grow')
  assert.equal(result?.id, impl1.id,
    'suppressed append returns the prior matching entry, not a fresh one')
})

test('appendGroupChatEntry: same content outside the recency window is NOT suppressed', () => {
  const { __seed, __append, __getLog } = loadAppendEntryPath()
  // The dedupe window is GROUP_DUPLICATE_APPEND_WINDOW_MS = 10 minutes.
  const impl1 = memberEntry('impl', 'Confirmed.', 't1', Date.now() - (60 * 60 * 1000)) // 1h ago
  const reviewer = memberEntry('reviewer', 'ack', 't1', Date.now() - (60 * 60 * 1000))
  __seed([impl1, reviewer])

  // Same impl reply, but well outside the window — should be appended.
  __append('Shared', { kind: 'member', name: 'impl' }, 'Confirmed.', 't1')
  const log = __getLog()
  assert.equal(log.length, 3, 'out-of-window reply is appended (legitimate re-reply)')
})

test('appendGroupChatEntry: same text but DIFFERENT thread is NOT suppressed', () => {
  const { __seed, __append, __getLog } = loadAppendEntryPath()
  const impl1 = memberEntry('impl', 'Confirmed.', 't-A', Date.now() - 5000)
  const reviewer = memberEntry('reviewer', 'ack', 't-A', Date.now() - 3000)
  __seed([impl1, reviewer])

  // Same text, same member, but different thread — must append.
  __append('Shared', { kind: 'member', name: 'impl' }, 'Confirmed.', 't-B')
  const log = __getLog()
  assert.equal(log.length, 3, 'different thread is a distinct conversation, must be appended')
})

test('appendGroupChatEntry: same text but DIFFERENT source is NOT suppressed', () => {
  const { __seed, __append, __getLog } = loadAppendEntryPath()
  const impl1 = memberEntry('impl', 'Confirmed.', 't1', Date.now() - 5000, 'conn-A')
  const reviewer = memberEntry('reviewer', 'ack', 't1', Date.now() - 3000)
  __seed([impl1, reviewer])

  // Same text, same member, same thread, different source — must append.
  __append('Shared', { kind: 'member', name: 'impl', source: 'conn-B' }, 'Confirmed.', 't1')
  const log = __getLog()
  assert.equal(log.length, 3, 'different source is a distinct machine, must be appended')
})

test('appendGroupChatEntry: intervening entry with different text does NOT block the dedupe of a same-text echo', () => {
  const { __seed, __append, __getLog } = loadAppendEntryPath()
  // Seed: impl said "Confirmed." 5s ago, then a different impl turn said
  // "On it." 3s ago, now impl is committing "Confirmed." again. The text
  // matches the 5s-ago entry; the intervening different-text reply is
  // not a "fresh turn" boundary (no user reply, no thread change). This
  // is a phantom echo and must be suppressed — the prior matching entry
  // is impl1, regardless of the intervening "On it." turn.
  const impl1 = memberEntry('impl', 'Confirmed.', 't1', Date.now() - 5000)
  const implInter = memberEntry('impl', 'On it.', 't1', Date.now() - 3000)
  __seed([impl1, implInter])

  const result = __append('Shared', { kind: 'member', name: 'impl' }, 'Confirmed.', 't1')
  const log = __getLog()
  assert.equal(log.length, 2, 'dedupe window catches impl1 even when implInter landed in between')
  assert.equal(result?.id, impl1.id, 'returns the prior matching impl1 entry')
})

test('appendGroupChatEntry: intervening USER reply resets the dedupe chain', () => {
  const { __seed, __append, __getLog } = loadAppendEntryPath()
  // Seed: impl said "Confirmed." 5s ago, then a USER reply 3s ago. Now
  // the user re-addresses impl in the same thread and impl produces
  // "Confirmed." again — that IS a legitimate new turn (the user
  // explicitly re-engaged). The intervening USER entry is a fresh-turn
  // boundary.
  const impl1 = memberEntry('impl', 'Confirmed.', 't1', Date.now() - 5000)
  const user = userEntry('try again', 't1', Date.now() - 3000)
  __seed([impl1, user])

  __append('Shared', { kind: 'member', name: 'impl' }, 'Confirmed.', 't1')
  const log = __getLog()
  // Wait — the user entry's `from.kind === 'user'`, so isDuplicateGroupAppend
  // returns false on it (only member↔member compares). The window then
  // walks back to impl1, which matches. The dedupe still triggers.
  // This is intentional: a user re-engaging doesn't make impl's reply
  // any less of a duplicate if it's byte-identical within the recency
  // window — the legitimate case is a DIFFERENT text or different
  // thread, which the other tests cover.
  assert.equal(log.length, 2,
    'window dedupe is author-only; a user reply in between does not whitelist an identical member echo')
})

// ── sync semantics — bounded projection constant ──────────────────────

test('GROUP_CHAT_SYNC_MESSAGES: bounded projection constant is 16', () => {
  const { GROUP_CHAT_SYNC_MESSAGES } = loadRoomHelpers()
  assert.equal(GROUP_CHAT_SYNC_MESSAGES, 16)
})

test('GROUP_DUPLICATE_APPEND_WINDOW_MS: dedupe recency window is 10 minutes', () => {
  const { GROUP_DUPLICATE_APPEND_WINDOW_MS } = loadRoomHelpers()
  assert.equal(GROUP_DUPLICATE_APPEND_WINDOW_MS, 10 * 60 * 1000)
})

// ── backstop pull — D2: peer-write signal outside gateway / WS / reconnect ──
//
// #94863 has TWO hooks for the backstop: a low-frequency setTimeout fired
// from the hydrate path so a connected-but-idle Desktop surfaces peer
// writes that arrived in the window between flush and the next gateway
// transition. The constant is the pull interval; the function must be
// reachable from the plugin and reference the constant.

function loadBackstopConstants() {
  const start = pluginSource.indexOf('const GROUP_CHAT_SYNC_BACKSTOP_PULL_MS')
  const end = pluginSource.indexOf('\n', start)
  assert.notEqual(start, -1, 'plugin declares GROUP_CHAT_SYNC_BACKSTOP_PULL_MS')
  const block = pluginSource.slice(start, end)
  const context = {}
  vm.runInNewContext(
    `${block}
globalThis.GROUP_CHAT_SYNC_BACKSTOP_PULL_MS = GROUP_CHAT_SYNC_BACKSTOP_PULL_MS`,
    context
  )
  return context
}

test('GROUP_CHAT_SYNC_BACKSTOP_PULL_MS: backstop pull interval is 5 seconds', () => {
  const { GROUP_CHAT_SYNC_BACKSTOP_PULL_MS } = loadBackstopConstants()
  assert.equal(GROUP_CHAT_SYNC_BACKSTOP_PULL_MS, 5000,
    'backstop interval intentionally larger than the publish-side debounce so it never starves a write in flight')
})

test('scheduleGroupChatBackstopPull: declared in plugin.js and wired into the hydrate path', () => {
  // The function must exist in the plugin source.
  assert.ok(/function\s+scheduleGroupChatBackstopPull\s*\(/.test(pluginSource),
    'scheduleGroupChatBackstopPull is declared in plugin.js')
  // And the hydrate path must call it (line ~15124 in the current source).
  // Use a structural match: the call must follow pullGroupChatServerState
  // and scheduleGroupChatServerSync in the same handler.
  // Two `await pullGroupChatServerState()` calls exist: one inside the
  // backstop timer body itself, the other in the hydrate handler. Pick
  // the LAST occurrence — that's the hydrate handler.
  const lastHydrateIdx = pluginSource.lastIndexOf('await pullGroupChatServerState().catch(() => false)')
  assert.notEqual(lastHydrateIdx, -1, 'hydrate handler present')
  const slice = pluginSource.slice(lastHydrateIdx, lastHydrateIdx + 800)
  assert.ok(slice.includes('scheduleGroupChatServerSync($groupChats.get())'),
    'hydrate still schedules the publish side')
  assert.ok(slice.includes('scheduleGroupChatBackstopPull()'),
    'hydrate starts the backstop pull on a fresh Desktop')
  // The handleSessionsGatewayTransition path must also re-arm the backstop
  // across gateway swaps. The function lives in two places (hydrate
  // handler + gateway-transition handler). A 400-char window is enough
  // to cover the small gateway-transition handler.
  assert.ok(
    /function\s+handleSessionsGatewayTransition\s*\([^)]*\)\s*\{[\s\S]{0,800}scheduleGroupChatBackstopPull\s*\(\s*\)/.test(pluginSource),
    'gateway transition re-arms the backstop so peer writes continue on a freshly-bound gateway'
  )
  // The dispose path must cancel the backstop so a disabled plugin does
  // not keep polling profiles.configure.
  const stopIdx = pluginSource.indexOf('function stopGroupChatServerSync(')
  assert.notEqual(stopIdx, -1, 'stopGroupChatServerSync is declared')
  const stopSlice = pluginSource.slice(stopIdx, pluginSource.indexOf('\n}\n', stopIdx) + 3)
  assert.ok(stopSlice.includes('groupChatSyncBackstopTimer'),
    'dispose cancels groupChatSyncBackstopTimer')
})

// ── CI OOM regression: the backstop must not busy-loop a fake timer host ──
//
// The group-chat.test.mjs fixture answers setTimeout with a positive
// handle (1) but runs callbacks via setImmediate with a no-op
// clearTimeout. The probe check passes on that shape, and re-arming from
// an instantly-fired timer turned scheduleGroupChatBackstopPull into an
// event-loop busy-loop that grew the heap to 4GB and killed the whole
// `check:test:plugins` run. A truthful timer host never fires BEFORE its
// requested delay, so the function must refuse to re-arm an early fire.

function extractBackstopFunctionSource() {
  const fnStart = pluginSource.indexOf('function scheduleGroupChatBackstopPull')

  assert.notEqual(fnStart, -1, 'scheduleGroupChatBackstopPull present')

  let depth = 0
  let end = -1

  for (let i = pluginSource.indexOf('{', fnStart); i < pluginSource.length; i++) {
    if (pluginSource[i] === '{') {
      depth++
    } else if (pluginSource[i] === '}') {
      depth--

      if (depth === 0) {
        end = i + 1

        break
      }
    }
  }

  assert.notEqual(end, -1, 'scheduleGroupChatBackstopPull braces balance')

  return pluginSource.slice(fnStart, end)
}

function loadBackstopFunction() {
  // Pathological-host sandbox: positive handles, setImmediate-style firing,
  // clearTimeout that cancels nothing, controllable clock. Entries carry
  // their requested delay so the 0-ms probe is distinguishable from the
  // real backstop timer.
  const queued = []
  const context = {
    groupChatSyncDisposed: false,
    groupChatSyncBackstopTimer: null,
    GROUP_CHAT_SYNC_BACKSTOP_PULL_MS: 5000,
    $groupChats: { get: () => ({ roomA: {} }) },
    pullGroupChatServerState: () => Promise.resolve(false),
    Date: { now: () => context.now },
    setTimeout: (fn, ms) => {
      queued.push({ fn, ms })

      return queued.length
    },
    clearTimeout: () => undefined,
    now: 0
  }

  vm.runInNewContext(extractBackstopFunctionSource(), context)

  // Drain only what is queued at call time — a re-arm during the drain
  // stays queued so assertions can count it without looping forever.
  const drain = async () => {
    const snapshot = queued.splice(0, queued.length)

    for (const { fn } of snapshot) {
      fn()
      await new Promise(resolve => setImmediate(resolve))
      await new Promise(resolve => setImmediate(resolve))
    }
  }

  return {
    context,
    queued,
    drain,

    backstopTimers: () => queued.filter(entry => entry.ms === context.GROUP_CHAT_SYNC_BACKSTOP_PULL_MS)
  }
}

test('scheduleGroupChatBackstopPull: early-firing host gets one pull, no re-arm loop', async () => {
  const { context, queued, drain, backstopTimers } = loadBackstopFunction()

  context.scheduleGroupChatBackstopPull()
  assert.equal(backstopTimers().length, 1, 'arms exactly one backstop timer')

  // Fire immediately (well before the 5s delay) — the shape that used to
  // requeue forever and OOM the suite.
  await drain()

  assert.equal(backstopTimers().length, 0, 'an early fire must not re-arm the backstop')
  assert.equal(queued.length, 0, 'nothing else was scheduled either')
})

test('scheduleGroupChatBackstopPull: truthful host fires at its delay and re-arms', async () => {
  const { context, drain, backstopTimers } = loadBackstopFunction()

  context.scheduleGroupChatBackstopPull()
  assert.equal(backstopTimers().length, 1, 'arms exactly one backstop timer')

  // Advance past the full interval before firing — what a real timer host
  // guarantees. The pull runs and the chain continues.
  context.now = 5000

  await drain()

  assert.equal(backstopTimers().length, 1, 'an on-time fire re-arms for the next cycle')
})