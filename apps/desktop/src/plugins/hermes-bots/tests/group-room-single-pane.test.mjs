import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// #89788: opening a group chat painted the room TWICE — once as a main-window
// workspace tab (host.openWorkspace) and once as the in-panel fallback, because
// the panel render only checked $groupChatWorkspace + members. Two live panes
// with independent drafts drove one shared engine. The panel room must render
// ONLY when no main tab owns the group (groupChatMainTabs), while the atom
// stays set so the roster row still highlights.

test('source contract: in-panel room is gated on no live main-window tab (#89788)', () => {
  assert.match(
    pluginSource,
    /groupChatName && groupChatMembers\.length && !groupChatMainTabs\.has\(groupChatName\)/
  )
})

test('source contract: openGroupChat still sets the atom before trying the main door', () => {
  // The atom doubles as the roster-row highlight + the fallback trigger for
  // desktops without host.openWorkspace — the gate above must not remove it.
  const fn = pluginSource.slice(pluginSource.indexOf('function openGroupChat('))
  const body = fn.slice(0, fn.indexOf('\n}'))
  assert.match(body, /\$groupChatWorkspace\.set\(group\)/)
  assert.match(body, /host\.openWorkspace/)
})
