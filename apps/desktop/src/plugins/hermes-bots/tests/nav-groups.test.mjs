import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Bot Mode roster navigation groups — pure-function contract. The helpers are
// inlined in plugin.js (the plugin's single-file convention); they are loaded
// through the same vm harness as every other plugin test and exercised directly.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load() {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const sdkStub = new Proxy({}, { get: () => undefined })
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: { state: { profile: { listen: () => undefined }, gateway: { listen: () => undefined } } },
    sdk: sdkStub
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(
      '\nglobalThis.__nav = { botNavKey, groupNavKey, normalizeGroupName, hasAnyNavGroups, bucketByGroup, moveItemToGroup, setItemAlias, reorderGroup, addGroup, renameGroup, deleteGroup };\n'
    )
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  return context.__nav
}

const nav = load()

test('botNavKey and groupNavKey are stable and disjoint', () => {
  assert.equal(nav.botNavKey({ connectionId: 'local', name: 'research' }), 'local::research')
  assert.equal(nav.botNavKey({ name: 'default' }), 'legacy::default')
  assert.equal(nav.groupNavKey('Room'), 'group:Room')
  // a bot named "Room" must never collide with a group chat named "Room"
  assert.notEqual(nav.groupNavKey('Room'), nav.botNavKey({ connectionId: 'legacy', name: 'Room' }))
})

test('normalizeGroupName trims and collapses blanks to null', () => {
  assert.equal(nav.normalizeGroupName('  Team A  '), 'Team A')
  assert.equal(nav.normalizeGroupName('   '), null)
  assert.equal(nav.normalizeGroupName(null), null)
  assert.equal(nav.normalizeGroupName(undefined), null)
})

test('hasAnyNavGroups gates on a real group name', () => {
  assert.equal(nav.hasAnyNavGroups({}), false)
  assert.equal(nav.hasAnyNavGroups({ a: 'Team A' }), true)
  assert.equal(nav.hasAnyNavGroups({ a: '  ' }), false)
})

test('bucketByGroup groups, aliases, and leaves the home ungrouped', () => {
  const items = [
    { key: 'a', name: 'alpha' },
    { key: 'b', name: 'beta' },
    { key: 'c', name: 'gamma' }
  ]
  const groups = { a: 'Team A', b: 'Team A' }
  const order = ['Team B', 'Team A'] // Team B has no items — must not render
  const aliases = { 'Team A': ['c'] }

  const { sections, ungrouped } = nav.bucketByGroup(items, groups, order, aliases)

  assert.equal(sections.length, 1, 'Team B without items must be skipped')
  assert.equal(sections[0].name, 'Team A')
  assert.equal(sections[0].items.length, 3, 'two homes + one alias')
  const aliasRow = sections[0].items.find(i => i.key === 'c')
  assert.equal(aliasRow.alias, true, 'aliased item is flagged as a shortcut')
  assert.equal(aliasRow.name, 'gamma', 'aliased item keeps its payload')

  assert.equal(ungrouped.length, 1, 'c stays ungrouped — alias does not move its home')
  assert.equal(ungrouped[0].key, 'c')
})

test('moveItemToGroup moves and ungroups without mutating', () => {
  const groups = { a: 'Team A' }
  const moved = nav.moveItemToGroup(groups, 'b', 'Team B')
  assert.equal(moved.b, 'Team B')
  assert.equal(groups.b, undefined, 'original object untouched')

  const ungrouped = nav.moveItemToGroup(moved, 'a', null)
  assert.equal(ungrouped.a, undefined)
  const blank = nav.moveItemToGroup(moved, 'a', '   ')
  assert.equal(blank.a, undefined)
})

test('setItemAlias toggles shortcuts without mutating', () => {
  const aliases = {}
  const added = nav.setItemAlias(aliases, 'Team A', 'x', true)
  assert.deepEqual([...added['Team A']], ['x'])
  assert.deepEqual(aliases, {}, 'original object untouched')

  const deduped = nav.setItemAlias(added, 'Team A', 'x', true)
  assert.deepEqual([...deduped['Team A']], ['x'], 'no duplicate alias')

  const removed = nav.setItemAlias(added, 'Team A', 'x', false)
  assert.equal(removed['Team A'], undefined, 'empty alias list is dropped')
})

test('renameGroup merges collisions and carries color', () => {
  const groups = { a: 'Old', b: 'New' }
  const order = ['Old', 'New']
  const aliases = { Old: ['x'] }
  const colors = { Old: '#f00' }

  const r = nav.renameGroup(groups, order, aliases, colors, 'Old', 'New')

  assert.equal(r.groups.a, 'New', 'renamed item follows')
  assert.equal(r.groups.b, 'New', 'collision target preserved')
  assert.deepEqual([...r.order], ['New'], 'order slot dedupes to one entry')
  assert.deepEqual([...r.aliases.New], ['x'])
  assert.equal(r.colors.New, '#f00', 'color survives the rename')
})

test('deleteGroup returns items to ungrouped and drops color/order/alias', () => {
  const groups = { a: 'Team A', b: 'Team B' }
  const order = ['Team A', 'Team B']
  const aliases = { 'Team A': ['x'], 'Team B': ['y'] }
  const colors = { 'Team A': '#f00', 'Team B': '#0f0' }

  const r = nav.deleteGroup(groups, order, aliases, colors, 'Team A')

  assert.equal(r.groups.a, undefined, 'item returns to ungrouped')
  assert.equal(r.groups.b, 'Team B', 'other group untouched')
  assert.deepEqual([...r.order], ['Team B'])
  assert.equal(r.aliases['Team A'], undefined)
  assert.deepEqual([...r.aliases['Team B']], ['y'])
  assert.equal(r.colors['Team A'], undefined)
})

test('reorderGroup and addGroup keep names unique', () => {
  assert.deepEqual([...nav.addGroup([], 'Team A')], ['Team A'])
  assert.deepEqual([...nav.addGroup(['Team A'], 'Team A')], ['Team A'], 'no duplicate')
  assert.deepEqual([...nav.reorderGroup(['A', 'B', 'C'], 'C', 0)], ['C', 'A', 'B'])
})
