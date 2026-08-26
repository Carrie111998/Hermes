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

// Regression: programmatic / synthetic clicks (cua-driver Quartz, assistive
// tools) often skip the hover dwell Radix's pointerdown capture relies on,
// so its DropdownMenuItem.onSelect silently swallows the click. Wire an
// onClick fallback on both New Bot and New Group Chat menu items so the
// dialog opens regardless of which pointer-event path delivered the click.
test('regression: dropdown menu items also fire onClick for programmatic click paths', () => {
  assert.match(
    pluginSource,
    /onClick:\s*\(\)\s*=>\s*setCreateOpen\(true\)/,
    'New Bot item must wire onClick alongside onSelect so synthetic clicks open the dialog'
  )
  assert.match(
    pluginSource,
    /onClick:\s*\(\)\s*=>\s*\{[^}]*setGroupCreateOpen\(true\)/,
    'New Group Chat item must wire onClick alongside onSelect so synthetic clicks open the dialog'
  )
})

// Regression: the + dropdown has historically been the only entry point to
// the New Group Chat dialog, so a Radix-driven silent failure left the
// feature invisible from the UI. Surface a Cmd+Shift+G keyboard shortcut
// that routes through the same setGroupCreateOpen state setter — the
// shortcut gates on roster size >= 2 to match the menu item's disabled
// rule, so users get the same affordance either way.
test('regression: Cmd+Shift+G keyboard shortcut opens New Group Chat dialog', () => {
  assert.match(
    pluginSource,
    /addEventListener\(['"]keydown['"]/,
    'BotsPane must register a global keydown listener'
  )
  assert.match(
    pluginSource,
    /event\.shiftKey/,
    'shortcut must require Shift as part of the modifier chord'
  )
  assert.match(
    pluginSource,
    /event\.metaKey\s*\|\|\s*event\.ctrlKey/,
    'shortcut must accept either Cmd (macOS) or Ctrl (other platforms) as the primary modifier'
  )
  assert.match(
    pluginSource,
    /event\.key\.toLowerCase\(\)\s*===\s*['"]g['"]/,
    'shortcut must trigger on the literal "g" key (case-insensitive)'
  )
  assert.match(
    pluginSource,
    /setGroupCreateOpen\(true\)/,
    'shortcut must route through the same setGroupCreateOpen setter the menu item does'
  )
})

// Regression: the shortcut effect must be declared AFTER activeSourceRoster.
// The first version referenced activeSourceRoster.length in its deps array
// above the `const activeSourceRoster = ...` initializer — a TDZ
// ReferenceError that crashed BotsPane on mount. The source-contract test
// below pins the ordering so a future move of the effect cannot silently
// reintroduce the crash (a regex test cannot catch a runtime TDZ, but the
// ordering invariant is exactly what keeps it out).
test('regression: keydown effect is declared after activeSourceRoster (no TDZ)', () => {
  const declIdx = pluginSource.indexOf('const activeSourceRoster = roster.filter')
  const effectIdx = pluginSource.indexOf("addEventListener('keydown', handler)")
  assert.ok(declIdx !== -1, 'activeSourceRoster declaration must exist')
  assert.ok(effectIdx !== -1, 'keydown listener registration must exist')
  assert.ok(
    effectIdx > declIdx,
    'keydown effect must be declared after activeSourceRoster — reading the deps array before the const initializer throws a TDZ ReferenceError'
  )
})

// Regression: the global keydown listener must not hijack chords while the
// user is typing (BotsPane is a docked pane, so the listener is global while
// Bot Mode is on screen). Without the editable-target gate, Cmd+Shift+G would
// fire the create dialog from the chat composer, settings inputs, or the
// pane's own search box.
test('regression: keydown shortcut ignores editable targets (composer/inputs)', () => {
  assert.match(
    pluginSource,
    /isContentEditable/,
    'shortcut must bail out when the event target is an editable element (INPUT/TEXTAREA/SELECT/contentEditable)'
  )
  assert.match(
    pluginSource,
    /tag === ['"]INPUT['"] || tag === ['"]TEXTAREA['"]/,
    'shortcut must skip INPUT and TEXTAREA targets so typing in the composer or search box is never hijacked'
  )
})

// Discoverability: surface the shortcut next to the menu label so users can
// find the parallel entry point when the Radix click path misfires.
test('UX: New Group Chat menu item shows ⌘⇧G shortcut hint', () => {
  assert.match(
    pluginSource,
    /⌘⇧G/,
    'menu item must show the shortcut hint so users discover the parallel entry point'
  )
  assert.match(
    pluginSource,
    /jsx\(['"]kbd['"]/,
    'shortcut hint must be rendered as a <kbd> element (jsx("kbd")) for semantic + visual consistency'
  )
})
