import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// The expanded group-chat surface must be a SECOND VIEW over the same room
// state — never a forked log. These tests exercise the state layer the dialog
// renders: $groupChats + updateGroupChat (what GroupChatWorkspace reads via
// useValue($groupChats) in both the pane and the dialog).

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadRoomState() {
  const start = source.indexOf('/** Group-chat rooms:')
  const end = source.indexOf('/** Ensure the member', start)
  assert.notEqual(start, -1, 'group-chat state section must exist')
  assert.notEqual(end, -1, 'section end anchor must exist')
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const context = { atom, pluginCtx: null }
  const section = source
    .slice(start, end)
    .concat('\nglobalThis.__t = { $groupChats, $groupChatExpanded, updateGroupChat, appendGroupChatEntry };\n')
  vm.runInNewContext(section, context, { filename: 'room-state.js' })
  return context.__t
}

test('expanded toggle atom exists and defaults to null (dialog closed)', () => {
  const t = loadRoomState()
  assert.equal(typeof t.$groupChatExpanded.get, 'function')
  assert.equal(t.$groupChatExpanded.get(), null, 'no expanded group at boot')
})

test('dual-surface: pane + dialog read the same room log entries', () => {
  const t = loadRoomState()
  // The pane writes (user send) and the dialog writes (member reply) — one log.
  t.appendGroupChatEntry('standup', { kind: 'user', name: 'You' }, 'first from pane')
  t.appendGroupChatEntry('standup', { kind: 'member', name: 'chercheur' }, 'reply seen from dialog')
  const log = t.$groupChats.get().standup.log
  assert.equal(log.length, 2)
  assert.equal(log[0].text, 'first from pane')
  assert.equal(log[1].from.name, 'chercheur')
  // Any number of views reading $groupChats.get() see identical data.
  assert.deepEqual(t.$groupChats.get().standup.log, log)
})

test('close-during-running safe: clearing the expanded view keeps room + running flag', () => {
  const t = loadRoomState()
  t.appendGroupChatEntry('standup', { kind: 'user', name: 'You' }, 'kick off')
  t.updateGroupChat('standup', room => ({ ...room, running: true }))
  t.$groupChatExpanded.set('standup') // dialog open
  // User closes the dialog mid-run: only the expanded atom clears.
  t.$groupChatExpanded.set(null)
  const room = t.$groupChats.get().standup
  assert.equal(room.running, true, 'round-robin keeps running after close')
  assert.equal(room.log.length, 1, 'room log untouched')
  // Member replies landing after the close still append to the same room.
  t.appendGroupChatEntry('standup', { kind: 'member', name: 'codeur' }, 'late reply')
  assert.equal(t.$groupChats.get().standup.log.length, 2)
})

test('reopen restores: re-expanding shows the accumulated room state', () => {
  const t = loadRoomState()
  t.appendGroupChatEntry('standup', { kind: 'user', name: 'You' }, 'msg 1')
  t.$groupChatExpanded.set('standup')
  t.$groupChatExpanded.set(null) // close
  t.appendGroupChatEntry('standup', { kind: 'member', name: 'veilleur' }, 'msg 2')
  t.$groupChatExpanded.set('standup') // reopen
  assert.equal(t.$groupChatExpanded.get(), 'standup')
  const log = t.$groupChats.get().standup.log
  assert.equal(log.length, 2, 'full history present on reopen')
  assert.equal(log[1].text, 'msg 2')
})

test('source contract: expand affordance + dialog wired into the pane workspace', () => {
  assert.match(source, /const \$groupChatExpanded = atom\(null\)/)
  assert.match(source, /function GroupChatExpandedDialog\(/)
  assert.match(source, /children: 'Expand'/)
  assert.match(source, /onExpand: \(\) => \$groupChatExpanded\.set\(groupChatName\)/)
  assert.match(source, /onClose: \(\) => \$groupChatExpanded\.set\(null\)/)
  assert.match(source, /jsx\(GroupChatWorkspace, \{ group, members, expanded: true \}\)/)
})
