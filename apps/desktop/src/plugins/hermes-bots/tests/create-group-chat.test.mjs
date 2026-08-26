import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// Discord-style creation flow: the header "+" is a dropdown (New Bot /
// New Group Chat), and New Group Chat opens a checkbox-picker modal with
// search, a name input, and a Create button. Source-contract style, like
// the other roster affordance tests.

test('source contract: header + is a dropdown offering agent and group chat', () => {
  assert.match(pluginSource, /DropdownMenuTrigger/)
  assert.match(pluginSource, /'New Bot'/)
  assert.match(pluginSource, /'New Group Chat'/)
})

test('source contract: create-group modal has search, checkboxes, name, create', () => {
  assert.match(pluginSource, /function CreateGroupChatDialog\(/)
  // An outage placeholder preserves identity, but cannot receive a message.
  assert.match(pluginSource, /const selectableRoster = roster\.filter\(bot => !bot\?\.ghost\)/)
  // Reuses the roster search filter so name/@handle/title all match.
  assert.match(pluginSource, /const visible = filterBots\(selectableRoster, allMeta, query\)/)
  // Selection is checkbox-driven and capped at the room member limit.
  assert.match(pluginSource, /const atCap = selected\.length >= GROUP_CHAT_MAX_MEMBERS/)
  // Create requires 2+ members. Membership mutation is covered by the
  // behavioral groupMembershipPatch tests rather than another source regex.
  assert.match(pluginSource, /selected\.length >= 2/)
  // Creating drops the user straight into the room (main window when the
  // desktop offers host.openWorkspace, in-panel fallback otherwise).
  assert.match(pluginSource, /onCreated: groupName => openGroupChat\(groupName\)/)
})

test('source contract: group name falls back to member names, Discord-style', () => {
  assert.match(pluginSource, /selected\.map\(bot => displayName\(bot, botRosterMeta\(bot, allMeta\)\)\)\.join\(', '\)/)
})

test('source contract: every selected machine is persisted in the durable room record', () => {
  assert.match(pluginSource, /const roomMembers = durableGroupChatMembers\(selected\)/)
  assert.match(pluginSource, /room\.members = roomMembers/)
  assert.doesNotMatch(pluginSource, /const remoteMembers = selected/)
})

test('source contract: creation negotiates continuity without exposing routing jargon', () => {
  assert.match(pluginSource, /resolveAutonomousRoomPlan\(selected/)
  assert.match(pluginSource, /describeAutonomousRoomPlan\(plan/)
  assert.match(pluginSource, /setKeepRunning\(copy\.defaultEnabled && keepRunningPreference\.current !== 'off'\)/)
  assert.match(pluginSource, /withHostedRoomProbeTimeout\(Promise\.all/)
  assert.match(pluginSource, /disabled: !canCreate \|\| createPending \|\| hostProbePending/)
  assert.match(pluginSource, /jsxs\('details'/)
  assert.match(pluginSource, /'Advanced'/)
  assert.match(pluginSource, /'groups\.peer\.invite'/)
  assert.match(pluginSource, /'groups\.peer\.register'/)
  assert.match(pluginSource, /Checking room continuity/)
  assert.doesNotMatch(pluginSource, /children: 'RoomLink'/)
  assert.doesNotMatch(pluginSource, /children: 'Peer transport'/)
})

test('source contract: an admitted autonomous room must roll back before any Desktop fallback', () => {
  assert.match(pluginSource, /cancel_id: `rollback-\$\{roomId\}`/)
  assert.doesNotMatch(pluginSource, /Room created\. Keep Desktop open until its gateways are ready\./)
})
