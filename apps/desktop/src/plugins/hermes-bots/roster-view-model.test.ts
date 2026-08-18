// @ts-nocheck -- migrated legacy JS behavior matrix; production is exercised through real imports.
import assert from 'node:assert/strict'

import { test } from 'vitest'

// Exercise the real ESM module; production never exposes this seam.
globalThis.__HERMES_BOTS_TEST__ = true
await import('./plugin.js')
const viewModel = globalThis.__HERMES_BOTS_TEST_API__

function load() {
  return viewModel
}

// ── Constants ───────────────────────────────────────────────────────────────

const VIEW_GROUPING_NONE = 'none'
const VIEW_GROUPING_SECTIONS = 'sections'
const VIEW_GROUPING_GROUPS = 'groups'
const VIEW_SORT_ALPHA = 'alpha'
const VIEW_SORT_PINNED_RECENCY = 'pinned-recency'
const VIEW_SORT_MANUAL = 'manual'

// ── Helpers ─────────────────────────────────────────────────────────────────

const bot = (name, opts = {}) => ({
  name,
  remoteSource: opts.remoteSource || false,
  sourceScoped: opts.sourceScoped || opts.remoteSource || false,
  connectionId: opts.connectionId || undefined,
  connectionLabel: opts.connectionLabel || undefined,
  last_session: opts.last_session || null,
  ...opts.extra
})

const meta = (opts = {}) => ({
  title: opts.title,
  groups: opts.groups || [],
  pinned: opts.pinned || false,
  hidden: opts.hidden || false,
  ...opts.extra
})

// ── Peer identity tests ─────────────────────────────────────────────────────

test('botPeerIdentity: source-qualified key as botRosterKey', () => {
  const { botPeerIdentity, botRosterKey: keyFor } = load()
  // Must match botRosterKey for backwards compat
  const local = { name: 'hermes', sourceScoped: true }
  const remote = { name: 'spark', remoteSource: true, sourceScoped: true, connectionId: 'mini' }

  assert.equal(botPeerIdentity(local), keyFor(local))
  assert.equal(botPeerIdentity(remote), keyFor(remote))
  assert.equal(botPeerIdentity({ name: 'default' }), 'legacy::default')
})

test('groupPeerIdentity: kind-qualified, never collides with bot identity', () => {
  const { groupPeerIdentity, botPeerIdentity } = load()

  const gId = groupPeerIdentity('Engineering')
  assert.ok(gId.startsWith('group:'))
  assert.equal(gId, 'group:Engineering')

  // No collision with a similarly-named bot
  const bId = botPeerIdentity({ name: 'Engineering', sourceScoped: true })
  assert.notEqual(gId, bId)
})

test('groupPeerIdentity: collision-safe even for adversarial names', () => {
  const { groupPeerIdentity, botPeerIdentity } = load()

  // A bot named "group:x" must not collide with group "x"
  const gId = groupPeerIdentity('flat')
  const bId = botPeerIdentity({ name: 'flat', sourceScoped: true })
  assert.notEqual(gId, bId)

  // Unicode group names
  const uId = groupPeerIdentity('ÜberGroup')
  assert.ok(uId.startsWith('group:'))
  assert.ok(uId.includes('ÜberGroup'))
})

// ── buildPeerList tests ─────────────────────────────────────────────────────

test('buildPeerList: every visible bot + every known group, no duplicates', () => {
  const { buildPeerList } = load()
  const roster = [bot('hermes'), bot('atlas'), bot('echo')]

  const metaByName = {
    atlas: meta({ groups: ['Engineering'] }),
    echo: meta({ groups: ['Engineering', 'Research'] })
  }

  const rooms = {}

  const peers = buildPeerList(roster, metaByName, rooms, false)

  // 3 bots + 2 groups = 5 peers
  assert.equal(peers.length, 5)
  assert.equal(peers.filter(p => p.kind === 'bot').length, 3)
  assert.equal(peers.filter(p => p.kind === 'group').length, 2)

  const groupNames = peers.filter(p => p.kind === 'group').map(p => p.title)
  assert.equal(JSON.stringify(groupNames.sort()), JSON.stringify(['Engineering', 'Research']))
  assert.equal(peers.find(p => p.kind === 'group' && p.title === 'Engineering').members.length, 2)
  assert.equal(peers.find(p => p.kind === 'group' && p.title === 'Research').members.length, 1)
})

test('buildPeerList: group rows stay visible even with no members in roster', () => {
  const { buildPeerList } = load()
  const roster = [bot('hermes')]

  const metaByName = {
    ghost: meta({ groups: ['Empty'] }) // bot not in roster
  }

  const rooms = {}

  const peers = buildPeerList(roster, metaByName, rooms, false)
  assert.ok(peers.some(p => p.kind === 'group' && p.title === 'Empty'))
})

test('buildPeerList: hidden bots excluded when showHidden=false', () => {
  const { buildPeerList } = load()
  const roster = [bot('hermes'), bot('atlas')]
  const metaByName = { atlas: meta({ hidden: true }) }

  const peers = buildPeerList(roster, metaByName, {}, false)
  assert.equal(peers.filter(p => p.kind === 'bot').length, 1)
  assert.equal(peers.filter(p => p.kind === 'bot')[0].name, 'hermes')
})

test('buildPeerList: hidden bots included when showHidden=true', () => {
  const { buildPeerList } = load()
  const roster = [bot('hermes'), bot('atlas')]
  const metaByName = { atlas: meta({ hidden: true }) }

  const peers = buildPeerList(roster, metaByName, {}, true)
  assert.equal(peers.filter(p => p.kind === 'bot').length, 2)
})

// ── Sorting tests ───────────────────────────────────────────────────────────

test('sortAlpha: bots by display name, groups by group name, case-insensitive', () => {
  const { sortAlpha } = load()

  const items = [
    { kind: 'bot', name: 'Zeta', meta: meta(), botRow: { name: 'Zeta' } },
    { kind: 'bot', name: 'alpha', meta: meta(), botRow: { name: 'alpha' } },
    { kind: 'group', title: 'Research', meta: null },
    { kind: 'group', title: 'engineering', meta: null },
    { kind: 'bot', name: 'Beta', meta: meta(), botRow: { name: 'Beta' } }
  ]

  const sorted = sortAlpha(items)
  const names = sorted.map(i => (i.kind === 'bot' ? i.name : i.title))
  // All sorted together alphabetically, case-insensitive: alpha, Beta, engineering, Research, Zeta
  assert.equal(JSON.stringify(names), JSON.stringify(['alpha', 'Beta', 'engineering', 'Research', 'Zeta']))
})

test('sortPinnedRecency: pinned bots first (by recency), then unpinned by recency, groups by room activity', () => {
  const { sortPinnedRecency } = load()

  const items = [
    { kind: 'bot', name: 'echo', meta: meta(), activity: 100, peerId: 'legacy::echo' },
    { kind: 'bot', name: 'atlas', meta: meta({ pinned: true }), activity: 50, peerId: 'legacy::atlas' },
    { kind: 'bot', name: 'hermes', meta: meta({ pinned: true }), activity: 200, peerId: 'legacy::hermes' },
    { kind: 'group', title: 'Eng', meta: null, groupActivity: 30, peerId: 'group:Eng' },
    { kind: 'group', title: 'Research', meta: null, groupActivity: 80, peerId: 'group:Research' },
    { kind: 'bot', name: 'scout', meta: meta(), activity: 150, peerId: 'legacy::scout' }
  ]

  const sorted = sortPinnedRecency(items)
  const names = sorted.map(i => (i.kind === 'bot' ? i.name : i.title))
  // Pinned: hermes(200), atlas(50). Unpinned: scout(150), echo(100). Groups: Research(80), Eng(30)
  assert.equal(JSON.stringify(names), JSON.stringify(['hermes', 'atlas', 'scout', 'echo', 'Research', 'Eng']))
})

test('sortManual: preserves persisted order, new items appended', () => {
  const { sortManual } = load()

  const items = [
    { peerId: 'legacy::echo', kind: 'bot', name: 'echo' },
    { peerId: 'legacy::atlas', kind: 'bot', name: 'atlas' },
    { peerId: 'legacy::hermes', kind: 'bot', name: 'hermes' }
  ]

  const persisted = ['legacy::atlas', 'legacy::echo'] // hermes is new

  const sorted = sortManual(items, persisted)
  assert.equal(
    JSON.stringify(sorted.map(i => i.peerId)),
    JSON.stringify(['legacy::atlas', 'legacy::echo', 'legacy::hermes'])
  )
})

test('sortManual: stale IDs in persistence are ignored', () => {
  const { sortManual } = load()

  const items = [
    { peerId: 'legacy::echo', kind: 'bot', name: 'echo' },
    { peerId: 'legacy::atlas', kind: 'bot', name: 'atlas' }
  ]

  const persisted = ['legacy::atlas', 'legacy::ghost', 'legacy::echo']

  const sorted = sortManual(items, persisted)
  assert.equal(JSON.stringify(sorted.map(i => i.peerId)), JSON.stringify(['legacy::atlas', 'legacy::echo']))
})

// ── Grouping mode tests ─────────────────────────────────────────────────────

test('applyGroupingMode None: every peer as flat list, no section dividers', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta() },
    { kind: 'bot', peerId: 'legacy::atlas', name: 'atlas', groups: ['Engineering'], meta: meta() },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null }
  ]

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, false)

  // All peers rendered flat, no section dividers
  assert.ok(Array.isArray(result))
  assert.equal(result.length, 3)
  assert.equal(result[0].kind, 'bot')
  assert.equal(result[1].kind, 'bot')
  assert.equal(result[2].kind, 'group')
})

test('applyGroupingMode None + Nest ON: group members are duplicated under shortcuts and remain standalone', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    {
      kind: 'bot',
      peerId: 'legacy::kiln',
      name: 'kiln',
      groups: ['Engineering', 'Research'],
      meta: meta(),
      botRow: { name: 'kiln' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null },
    { kind: 'group', peerId: 'group:Research', title: 'Research', meta: null }
  ]

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, true)

  // Every Bot remains a standalone peer; groups add shortcut copies.
  const topLevel = result.filter(r => r.level === 0)
  assert.equal(topLevel.length, 5)
  assert.equal(topLevel.filter(r => r.kind === 'bot').length, 3)

  const engGroup = result.find(r => r.peerId === 'group:Engineering')
  assert.ok(engGroup)
  assert.ok(Array.isArray(engGroup.children))
  assert.equal(engGroup.children.length, 2) // atlas, kiln
  assert.equal(
    JSON.stringify(engGroup.children.map(c => c.peerId).sort()),
    JSON.stringify(['legacy::atlas', 'legacy::kiln'])
  )

  // kiln appears under BOTH groups
  const researchGroup = result.find(r => r.peerId === 'group:Research')
  assert.equal(researchGroup.children.length, 1)
  assert.equal(researchGroup.children[0].peerId, 'legacy::kiln')
})

test('applyGroupingMode None + Nest OFF: every bot as top-level peer, groups also visible', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null }
  ]

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, false)

  // All 3 as top-level peers
  assert.equal(result.length, 3)
  assert.equal(result.filter(r => r.kind === 'bot').length, 2)
  assert.equal(result.filter(r => r.kind === 'group').length, 1)
  // No children on the group
  const eng = result.find(r => r.peerId === 'group:Engineering')
  assert.ok(!eng.children || eng.children.length === 0)
})

test('applyGroupingMode Groups: retains current behavior from 21fb9f779', () => {
  const { applyGroupingMode, VIEW_GROUPING_GROUPS } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    {
      kind: 'bot',
      peerId: 'legacy::kiln',
      name: 'kiln',
      groups: ['Engineering', 'Research'],
      meta: meta(),
      botRow: { name: 'kiln' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null },
    { kind: 'group', peerId: 'group:Research', title: 'Research', meta: null }
  ]

  const result = applyGroupingMode(VIEW_GROUPING_GROUPS, peers, {}, false)

  // All Bots remain standalone before the group shortcuts.
  const standaloneBots = result.filter(r => r.kind === 'bot' && r.level === 0)
  assert.equal(
    JSON.stringify(standaloneBots.map(r => r.peerId)),
    JSON.stringify(['legacy::hermes', 'legacy::atlas', 'legacy::kiln'])
  )

  // Then group sections alphabetically with nested shortcut copies.
  const eng = result.find(r => r.peerId === 'group:Engineering')
  assert.ok(eng)
  assert.ok(eng.children)
  assert.equal(eng.children.length, 2)
})

test('applyGroupingMode Sections: peers partitioned by visual sections, unassigned first', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    {
      kind: 'bot',
      peerId: 'legacy::scout',
      name: 'scout',
      groups: ['Research'],
      meta: meta(),
      botRow: { name: 'scout' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null }
  ]

  const viewState = {
    visualSections: [
      { key: 'section:A', title: 'Core Team', order: 0 },
      { key: 'section:B', title: 'External', order: 1 }
    ],
    sectionAssignments: {
      'legacy::atlas': 'section:A',
      'legacy::scout': 'section:A',
      'group:Engineering': 'section:B'
    },
    sectionCollapsed: {},
    sectionOrder: ['section:A', 'section:B']
  }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, false)

  // Should produce: unassigned section + section:A + section:B
  assert.equal(result.length, 3)

  // Unassigned first
  assert.equal(result[0].sectionKey, undefined)
  assert.ok(Array.isArray(result[0].peers))
  assert.equal(result[0].peers.length, 1)
  assert.equal(result[0].peers[0].peerId, 'legacy::hermes')

  // Section A
  assert.equal(result[1].sectionKey, 'section:A')
  assert.equal(result[1].title, 'Core Team')
  assert.equal(result[1].peers.length, 2)

  // Section B
  assert.equal(result[2].sectionKey, 'section:B')
  assert.equal(result[2].title, 'External')
  assert.equal(result[2].peers.length, 1)
  assert.equal(result[2].peers[0].peerId, 'group:Engineering')
})

test('applyGroupingMode Sections: empty sections render their header', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } }
  ]

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Empty Team', order: 0 }],
    sectionAssignments: {},
    sectionCollapsed: {},
    sectionOrder: ['section:A']
  }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, false)
  assert.equal(result.length, 2)
  assert.equal(result[1].sectionKey, 'section:A')
  assert.equal(result[1].peers.length, 0) // empty but still rendered
})

test('applyGroupingMode Sections: sections respect persisted order', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    }
  ]

  const viewState = {
    visualSections: [
      { key: 'section:B', title: 'Second', order: 1 },
      { key: 'section:A', title: 'First', order: 0 }
    ],
    sectionAssignments: {
      'legacy::atlas': 'section:A',
      'legacy::hermes': 'section:B'
    },
    sectionCollapsed: {},
    sectionOrder: ['section:B', 'section:A'] // B first, then A
  }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, false)
  // Unassigned is empty (both peers assigned), so first section is B, then A
  assert.equal(result[0].sectionKey, 'section:B') // B before A per viewState.sectionOrder
  assert.equal(result[1].sectionKey, 'section:A')
})

test('applyGroupingMode Sections: group rows can be assigned to sections', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null },
    { kind: 'group', peerId: 'group:Research', title: 'Research', meta: null }
  ]

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Active', order: 0 }],
    sectionAssignments: {
      'group:Engineering': 'section:A'
    },
    sectionCollapsed: {},
    sectionOrder: ['section:A']
  }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, false)
  assert.equal(result[0].peers[0].peerId, 'legacy::hermes')
  assert.equal(result[0].peers[1].peerId, 'group:Research')
  assert.equal(result[1].peers[0].peerId, 'group:Engineering')
})

// ── Visual Section CRUD tests ───────────────────────────────────────────────

test('createVisualSection: generates collision-safe key', () => {
  const { createVisualSection } = load()
  const sections = []

  const s = createVisualSection(sections, 'My Section')
  assert.equal(s.title, 'My Section')
  assert.ok(s.key.startsWith('section:'))
  assert.equal(typeof s.order, 'number')
})

test('createVisualSection: deduplicates title suffixes for keys', () => {
  const { createVisualSection } = load()
  const sections = []
  const a = createVisualSection(sections, 'Same')
  const b = createVisualSection([a], 'Same')

  assert.notEqual(a.key, b.key)
  assert.equal(a.title, 'Same')
  assert.equal(b.title, 'Same')
})

test('renameVisualSection: changes title, returns updated', () => {
  const { renameVisualSection } = load()
  const sections = [{ key: 'section:A', title: 'Old', order: 0 }]
  const updated = renameVisualSection(sections, 'section:A', 'New Name')

  assert.equal(updated[0].title, 'New Name')
  assert.equal(updated[0].key, 'section:A')
})

test('deleteVisualSection: unassigns all peers, removes section', () => {
  const { deleteVisualSection } = load()
  const sections = [{ key: 'section:A', title: 'To Delete', order: 0 }]
  const assignments = { 'legacy::atlas': 'section:A', 'group:Eng': 'section:A' }
  const order = ['section:A']

  const result = deleteVisualSection(sections, assignments, order, 'section:A')
  assert.equal(result.sections.length, 0)
  assert.equal(Object.keys(result.assignments).length, 0)
  // original assignments unchanged (pure)
  assert.equal(assignments['legacy::atlas'], 'section:A')
})

test('deleteVisualSection: never deletes a group or bot', () => {
  const { deleteVisualSection } = load()
  // Just verifies the function signature — it doesn't receive the roster
  const sections = [{ key: 'section:A', title: 'Section', order: 0 }]
  const assignments = {}
  const order = ['section:A']

  const result = deleteVisualSection(sections, assignments, order, 'section:A')
  assert.equal(result.sections.length, 0)
  // The caller handles what happens to unassigned peers
})

test('assignPeerToSection: sets assignment, returns new map', () => {
  const { assignPeerToSection } = load()
  const assignments = {}
  const updated = assignPeerToSection(assignments, 'legacy::atlas', 'section:A')

  assert.equal(updated['legacy::atlas'], 'section:A')
  // Original not mutated
  assert.equal(Object.keys(assignments).length, 0)
})

test('unassignPeerFromSection: removes assignment', () => {
  const { unassignPeerFromSection } = load()
  const assignments = { 'legacy::atlas': 'section:A', 'legacy::hermes': 'section:B' }
  const updated = unassignPeerFromSection(assignments, 'legacy::atlas')

  assert.equal(updated['legacy::atlas'], undefined)
  assert.equal(updated['legacy::hermes'], 'section:B')
  assert.equal(Object.keys(updated).length, 1)
})

test('collapseSection: toggles collapsed state', () => {
  const { collapseSection, isSectionCollapsed } = load()
  const collapsed = {}

  const c1 = collapseSection(collapsed, 'section:A', true)
  assert.equal(isSectionCollapsed(c1, 'section:A'), true)

  const c2 = collapseSection(c1, 'section:A', false)
  assert.equal(isSectionCollapsed(c2, 'section:A'), false)
})

// ── resolveSortOrder tests ──────────────────────────────────────────────────

test('resolveSortOrder: uses mode-appropriate sort function', () => {
  const { resolveSortOrder, VIEW_SORT_ALPHA, VIEW_SORT_PINNED_RECENCY, VIEW_SORT_MANUAL } = load()

  const items = [
    { kind: 'bot', peerId: 'legacy::z', name: 'z', meta: meta(), activity: 10, botRow: { name: 'z' } },
    { kind: 'bot', peerId: 'legacy::a', name: 'a', meta: meta(), activity: 100, botRow: { name: 'a' } }
  ]

  const alphaSorted = resolveSortOrder(VIEW_SORT_ALPHA, items, null, null)
  assert.equal(alphaSorted[0].name, 'a')

  const recencySorted = resolveSortOrder(VIEW_SORT_PINNED_RECENCY, items, null, null)
  assert.equal(recencySorted[0].name, 'a') // most recent

  const manualSorted = resolveSortOrder(VIEW_SORT_MANUAL, items, ['legacy::z', 'legacy::a'], null)
  assert.equal(manualSorted[0].name, 'z')
})

// ── Malformed persistence normalization tests ───────────────────────────────

test('normalizeViewState: fills in defaults for empty/missing', () => {
  const { normalizeViewState } = load()

  const result = normalizeViewState(null)
  assert.equal(result.grouping, VIEW_GROUPING_NONE)
  assert.equal(result.sort, VIEW_SORT_PINNED_RECENCY)
  assert.equal(result.nestMembers, false)
  assert.equal(result.showHidden, false)
  assert.ok(Array.isArray(result.visualSections))
  assert.equal(typeof result.sectionAssignments, 'object')
  assert.ok(Array.isArray(result.sectionOrder))
  assert.ok(Array.isArray(result.manualOrderNone))
  assert.equal(typeof result.manualOrderUnassigned, 'object')
})

test('normalizeViewState: strips stale section assignments', () => {
  const { normalizeViewState } = load()

  const input = {
    visualSections: [{ key: 'section:A', title: 'Real', order: 0 }],
    sectionAssignments: {
      'legacy::atlas': 'section:A',
      'legacy::hermes': 'section:GONE' // stale
    },
    sectionOrder: ['section:A'],
    sectionCollapsed: { 'section:GONE': true }
  }

  const result = normalizeViewState(input)
  assert.equal(result.sectionAssignments['legacy::atlas'], 'section:A')
  assert.equal(result.sectionAssignments['legacy::hermes'], undefined)
  assert.equal(result.sectionCollapsed['section:GONE'], undefined)
})

test('normalizeViewState: deduplicates section order', () => {
  const { normalizeViewState } = load()

  const input = {
    visualSections: [{ key: 'section:A', title: 'A', order: 0 }],
    sectionOrder: ['section:A', 'section:A', 'section:A'],
    sectionAssignments: {},
    sectionCollapsed: {}
  }

  const result = normalizeViewState(input)
  assert.equal(result.sectionOrder.length, 1)
  assert.equal(result.sectionOrder[0], 'section:A')
})

test('normalizeViewState: unknown section keys in order are removed', () => {
  const { normalizeViewState } = load()

  const input = {
    visualSections: [{ key: 'section:A', title: 'A', order: 0 }],
    sectionOrder: ['section:GHOST', 'section:A'],
    sectionAssignments: {},
    sectionCollapsed: {}
  }

  const result = normalizeViewState(input)
  assert.equal(result.sectionOrder.length, 1)
  assert.equal(result.sectionOrder[0], 'section:A')
})

// ── Race-safe hydration tests ───────────────────────────────────────────────

test('hydrateViewState: first load sets token and applies normalized state', () => {
  const { hydrateViewState, normalizeViewState } = load()
  const stored = { grouping: 'groups', sort: 'alpha', nestMembers: true, showHidden: true }
  const current = { state: normalizeViewState(null), token: 0, localGeneration: 0 }

  const result = hydrateViewState(current, stored, 1, 0)
  assert.equal(result.state.grouping, 'groups')
  assert.equal(result.state.sort, 'alpha')
  assert.equal(result.state.nestMembers, true)
  assert.equal(result.token, 1)
})

test('hydrateViewState: stale request token is rejected', () => {
  const { hydrateViewState } = load()

  const current = {
    state: { grouping: 'none', sort: 'pinned-recency' },
    token: 5,
    localGeneration: 0
  }

  const stored = { grouping: 'groups', sort: 'alpha' }

  // Request token 3 < current token 5 → rejected
  const result = hydrateViewState(current, stored, 3)
  assert.equal(result, current)
})

test('hydrateViewState: later request token with no local mutations overwrites', () => {
  const { hydrateViewState } = load()

  const current = {
    state: { grouping: 'none', sort: 'pinned-recency', nestMembers: false, showHidden: false },
    token: 3,
    localGeneration: 0
  }

  const stored = { grouping: 'groups', sort: 'alpha', nestMembers: true, showHidden: true }

  const result = hydrateViewState(current, stored, 5)
  assert.equal(result.state.grouping, 'groups')
  assert.equal(result.state.sort, 'alpha')
  assert.equal(result.token, 5)
})

test('hydrateViewState: later request with active local mutations is rejected', () => {
  const { hydrateViewState } = load()

  const current = {
    state: { grouping: 'none', sort: 'pinned-recency', nestMembers: false, showHidden: false },
    token: 3,
    localGeneration: 2 // local mutation pending
  }

  const stored = { grouping: 'groups', sort: 'alpha' }

  const result = hydrateViewState(current, stored, 5, 0)
  // A mutation after the read began wins; stored state is NOT applied
  assert.equal(result, current)
})

test('hydrateViewState: reset keeps the lifecycle token monotonic', () => {
  const { hydrateViewState } = load()

  const current = {
    state: { grouping: 'groups', sort: 'manual' },
    token: 5,
    localGeneration: 1
  }

  // token=0 means reset (dispose)
  const result = hydrateViewState(current, null, 0)
  assert.equal(result.state.grouping, VIEW_GROUPING_NONE)
  assert.equal(result.token, 5)
  assert.equal(result.localGeneration, 0)
})

// ── Nesting + Section interaction tests ─────────────────────────────────────

test('applyGroupingMode Sections + Nest ON: bot members nest under group rows within sections', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null }
  ]

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: {
      'legacy::hermes': 'section:A',
      'legacy::atlas': 'section:A',
      'group:Engineering': 'section:A'
    },
    sectionCollapsed: {},
    sectionOrder: ['section:A']
  }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, true)
  // Section A keeps both standalone Bots and the Engineering shortcut.
  const sectionA = result.find(r => r.sectionKey === 'section:A')
  assert.ok(sectionA)
  assert.equal(sectionA.peers.length, 3) // hermes + atlas + group:Engineering
  const engGroup = sectionA.peers.find(p => p.peerId === 'group:Engineering')
  assert.ok(engGroup.children)
  assert.equal(engGroup.children.length, 1)
  assert.equal(engGroup.children[0].peerId, 'legacy::atlas')
})

test('Sections + Nest ON: a multi-group Bot appears under every group across Section assignments', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering', 'Research'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null },
    { kind: 'group', peerId: 'group:Research', title: 'Research', meta: null }
  ]

  const viewState = {
    visualSections: [
      { key: 'section:A', title: 'Core', order: 0 },
      { key: 'section:B', title: 'Labs', order: 1 }
    ],
    sectionAssignments: {
      'legacy::atlas': 'section:A',
      'group:Engineering': 'section:A',
      'group:Research': 'section:B'
    },
    sectionCollapsed: {},
    sectionOrder: ['section:A', 'section:B']
  }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, true)
  const core = result.find(r => r.sectionKey === 'section:A')
  const labs = result.find(r => r.sectionKey === 'section:B')
  const engineering = core.peers.find(p => p.peerId === 'group:Engineering')
  const research = labs.peers.find(p => p.peerId === 'group:Research')

  assert.deepEqual(
    engineering.children.map(c => c.peerId),
    ['legacy::atlas']
  )
  assert.deepEqual(
    research.children.map(c => c.peerId),
    ['legacy::atlas']
  )
  assert.ok(
    core.peers.some(p => p.kind === 'bot' && p.peerId === 'legacy::atlas' && p.level === 0),
    'member Bot remains standalone and draggable'
  )
})

// ── Search composition tests ────────────────────────────────────────────────

test('applyGroupingMode None + search: bot query retains matching bot rows; group query retains group row', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null }
  ]

  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, false, searchOpts)
  assert.equal(result.length, 1)
  assert.equal(result[0].peerId, 'legacy::atlas')
})

test('applyGroupingMode None + search: group name match retains group row', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null }
  ]

  const searchOpts = { query: 'engin', matchesBotId: new Set() }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, false, searchOpts)
  assert.equal(result.length, 1)
  assert.equal(result[0].peerId, 'group:Engineering')
})

// ── Member-name search retains group rows ───────────────────────────────────

// buildPeerList seats group peers with `members` descriptors (not `children`).
// A member-name search must keep the group row even before nesting computes
// children, in both None and Sections and with Nest OFF and ON.

test('search None + Nest OFF: member-name match keeps the group row alongside the bot', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] }
  ]

  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, false, searchOpts)

  const ids = result.map(p => p.peerId)
  assert.ok(ids.includes('legacy::atlas'), 'matching bot retained')
  assert.ok(ids.includes('group:Engineering'), 'group row retained by member match')
  assert.equal(result.length, 2)
})

test('search None + Nest ON: group row keeps its matching child nested', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] }
  ]

  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, true, searchOpts)

  const eng = result.find(r => r.peerId === 'group:Engineering')
  assert.ok(eng, 'Engineering group row retained')
  assert.equal(eng.children.length, 1)
  assert.equal(eng.children[0].peerId, 'legacy::atlas')
  // atlas remains standalone while also appearing in the group shortcut
  assert.ok(result.some(r => r.kind === 'bot' && r.peerId === 'legacy::atlas' && r.level === 0))
})

test('search Sections + Nest OFF: member-name match keeps the group row', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] }
  ]

  const viewState = { visualSections: [], sectionAssignments: {}, sectionCollapsed: {}, sectionOrder: [] }
  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, false, searchOpts)

  const unassigned = result.find(r => !r.sectionKey)
  assert.ok(unassigned, 'unassigned range present')
  const ids = unassigned.peers.map(p => p.peerId)
  assert.ok(ids.includes('legacy::atlas'))
  assert.ok(ids.includes('group:Engineering'))
})

test('search Sections + Nest ON: group row keeps its matching child nested', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] }
  ]

  const viewState = { visualSections: [], sectionAssignments: {}, sectionCollapsed: {}, sectionOrder: [] }
  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, true, searchOpts)

  const unassigned = result.find(r => !r.sectionKey)
  assert.ok(unassigned)
  const eng = unassigned.peers.find(p => p.peerId === 'group:Engineering')
  assert.ok(eng, 'Engineering group row retained')
  assert.equal(eng.children.length, 1)
  assert.equal(eng.children[0].peerId, 'legacy::atlas')
  assert.ok(unassigned.peers.some(p => p.kind === 'bot' && p.peerId === 'legacy::atlas' && p.level === 0))
})

test('search Sections + Nest ON: separated matching member still retains its assigned group row', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] }
  ]

  const viewState = {
    visualSections: [{ key: 'section:Core', title: 'Core', order: 0 }],
    sectionAssignments: { 'group:Engineering': 'section:Core' },
    sectionCollapsed: {},
    sectionOrder: ['section:Core']
  }

  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, true, searchOpts)
  const unassigned = result.find(r => !r.sectionKey)
  const core = result.find(r => r.sectionKey === 'section:Core')

  const engineering = core.peers.find(p => p.peerId === 'group:Engineering')
  assert.ok(engineering, 'assigned group row retained across section boundary')
  assert.deepEqual(
    engineering.children.map(c => c.peerId),
    ['legacy::atlas'],
    'matching member nests under its group across the Section boundary'
  )
  assert.ok(
    unassigned.peers.some(p => p.peerId === 'legacy::atlas'),
    'matching member remains standalone across the Section boundary'
  )
})

test('search None + Nest OFF: a multi-group match retains every matching group row', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering', 'Research'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] },
    { kind: 'group', peerId: 'group:Research', title: 'Research', meta: null, members: [{ name: 'atlas' }] }
  ]

  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, false, searchOpts)

  const ids = result.map(r => r.peerId)
  assert.ok(ids.includes('legacy::atlas'), 'matching bot retained')
  assert.ok(ids.includes('group:Engineering'), 'first group retained')
  assert.ok(ids.includes('group:Research'), 'second group retained')
  assert.equal(result.length, 3)
})

test('search: source-qualified remote member descriptor keeps its group even when absent from the roster', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()
  const descriptor = { name: 'spark', handle: 'spark', remoteSource: true, sourceScoped: true, connectionId: 'mini' }

  const peers = [{ kind: 'group', peerId: 'group:Remote', title: 'Remote', meta: null, members: [descriptor] }]

  // spark is NOT a live roster row, so matchesBotId does not carry `mini::spark`.
  const searchOpts = { query: 'spark', matchesBotId: new Set() }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, {}, false, searchOpts)
  assert.equal(result.length, 1)
  assert.equal(result[0].peerId, 'group:Remote')
})

// ── Nested group collapse ───────────────────────────────────────────────────

test('normalizeViewState: preserves nestedExpansion for persistence', () => {
  const { normalizeViewState } = load()
  const result = normalizeViewState({ nestedExpansion: { 'group:Engineering': true } })
  assert.equal(result.nestedExpansion['group:Engineering'], true)
})

test('applyGroupingMode None + Nest ON: nested group rows honor nestedExpansion collapse', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] }
  ]

  const viewState = { nestedExpansion: { 'group:Engineering': true } }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, viewState, true)

  const eng = result.find(r => r.peerId === 'group:Engineering')
  assert.equal(eng.collapsed, true)
})

test('applyGroupingMode: search bypasses nested group collapse to reveal matches', () => {
  const { applyGroupingMode, VIEW_GROUPING_NONE } = load()

  const peers = [
    {
      kind: 'bot',
      peerId: 'legacy::atlas',
      name: 'atlas',
      groups: ['Engineering'],
      meta: meta(),
      botRow: { name: 'atlas' }
    },
    { kind: 'group', peerId: 'group:Engineering', title: 'Engineering', meta: null, members: [{ name: 'atlas' }] }
  ]

  const viewState = { nestedExpansion: { 'group:Engineering': true } }
  const searchOpts = { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }

  const result = applyGroupingMode(VIEW_GROUPING_NONE, peers, viewState, true, searchOpts)

  const eng = result.find(r => r.peerId === 'group:Engineering')
  assert.equal(eng.collapsed, false)
  assert.equal(eng.children.length, 1)
})

// ── reorderVisualSections test ──────────────────────────────────────────────

test('reorderVisualSections: updates order keys and sorts correctly', () => {
  const { reorderVisualSections } = load()

  const sections = [
    { key: 'section:A', title: 'A', order: 0 },
    { key: 'section:B', title: 'B', order: 1 },
    { key: 'section:C', title: 'C', order: 2 }
  ]

  const order = ['section:A', 'section:B', 'section:C']

  // Move C to first
  const result = reorderVisualSections(sections, order, 'section:C', 0)
  assert.equal(JSON.stringify(result.order), JSON.stringify(['section:C', 'section:A', 'section:B']))
  assert.equal(result.sections[0].key, 'section:C')
  assert.equal(result.sections[0].order, 0)
  assert.equal(result.sections[1].key, 'section:A')
  assert.equal(result.sections[1].order, 1)
})

// ── Section collapse + search interaction test ──────────────────────────────

test('collapsed section does not bury search match', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS, collapseSection } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::scout', name: 'scout', groups: [], meta: meta(), botRow: { name: 'scout' } },
    { kind: 'bot', peerId: 'legacy::hermes', name: 'hermes', groups: [], meta: meta(), botRow: { name: 'hermes' } }
  ]

  let vsCollapsed = collapseSection({}, 'section:A', true)

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Hidden Section', order: 0 }],
    sectionAssignments: {
      'legacy::scout': 'section:A',
      'legacy::hermes': 'section:A'
    },
    sectionCollapsed: vsCollapsed,
    sectionOrder: ['section:A']
  }

  const searchOpts = { query: 'scout', matchesBotId: new Set(['legacy::scout']) }
  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, false, searchOpts)

  // Scout must be visible despite collapsed section
  const sectionA = result.find(r => r.sectionKey === 'section:A')
  assert.ok(sectionA)
  assert.equal(sectionA.peers.length, 1) // only scout visible
  assert.equal(sectionA.peerCount, 2) // header count remains the full assigned range
  assert.equal(sectionA.peers[0].peerId, 'legacy::scout')
})

// ── Drag-and-drop helpers ───────────────────────────────────────────────────

test('manualOrderBucketKey: collision-safe across modes and adversarial names', () => {
  const { manualOrderBucketKey, MANUAL_ORDER_UNASSIGNED_KEY } = load()

  assert.equal(manualOrderBucketKey(VIEW_GROUPING_SECTIONS, null), MANUAL_ORDER_UNASSIGNED_KEY)
  assert.equal(manualOrderBucketKey(VIEW_GROUPING_SECTIONS, 'section:A'), 'section:A')
  // A section literally named "unassigned" still lives under its `section:` key,
  // never the reserved unassigned bucket.
  assert.equal(manualOrderBucketKey(VIEW_GROUPING_SECTIONS, 'section:unassigned'), 'section:unassigned')
  assert.notEqual(manualOrderBucketKey(VIEW_GROUPING_SECTIONS, 'section:unassigned'), MANUAL_ORDER_UNASSIGNED_KEY)

  assert.equal(manualOrderBucketKey(VIEW_GROUPING_GROUPS, null), MANUAL_ORDER_UNASSIGNED_KEY)
  assert.equal(manualOrderBucketKey(VIEW_GROUPING_GROUPS, 'Engineering'), 'group:Engineering')
  // A group literally named "unassigned" is namespaced under `group:`.
  assert.equal(manualOrderBucketKey(VIEW_GROUPING_GROUPS, 'unassigned'), 'group:unassigned')
  assert.notEqual(manualOrderBucketKey(VIEW_GROUPING_GROUPS, 'unassigned'), MANUAL_ORDER_UNASSIGNED_KEY)
})

test('reorderPeerInto: moves a peer onto a target, returns a new array, never mutates input', () => {
  const { reorderPeerInto } = load()
  const order = ['a', 'b', 'c', 'd']
  // Move 'a' before 'c' → ['b','a','c','d'] (index adjust: to > from, shifts left)
  const next = reorderPeerInto(order, 'a', 'c')
  assert.equal(JSON.stringify(next), JSON.stringify(['b', 'a', 'c', 'd']))
  // Input untouched
  assert.equal(JSON.stringify(order), JSON.stringify(['a', 'b', 'c', 'd']))
})

test('reorderPeerInto: null target appends to the end', () => {
  const { reorderPeerInto } = load()
  assert.equal(JSON.stringify(reorderPeerInto(['a', 'b', 'c'], 'a', null)), JSON.stringify(['b', 'c', 'a']))
})

test('reorderPeerInto: no-op returns null when target absent or already in place', () => {
  const { reorderPeerInto } = load()
  assert.equal(reorderPeerInto(['a', 'b'], 'a', 'ghost'), null)
  assert.equal(reorderPeerInto(['a', 'b'], 'a', 'a'), null)
  assert.equal(reorderPeerInto(['a', 'b'], 'b', null), null) // already last
})

test('reorderPeerInto: a peer absent from the range is a cross-range insert at the target slot', () => {
  const { reorderPeerInto } = load()
  // Dropping a peer that is not yet in the range onto a target inserts it at
  // the target's slot (before it); dropping onto empty space appends.
  assert.equal(JSON.stringify(reorderPeerInto(['hermes'], 'atlas', 'hermes')), JSON.stringify(['atlas', 'hermes']))
  assert.equal(JSON.stringify(reorderPeerInto(['hermes'], 'atlas', null)), JSON.stringify(['hermes', 'atlas']))
})

test('reorderPeerInto: dropping on itself is always a no-op', () => {
  const { reorderPeerInto } = load()
  const ids = ['a', 'b', 'c']

  assert.equal(reorderPeerInto(ids, 'a', 'a', 'before'), null)
  assert.equal(reorderPeerInto(ids, 'a', 'a', 'after'), null)
  assert.equal(reorderPeerInto(ids, 'b', 'b', 'after'), null)
  assert.equal(JSON.stringify(ids), JSON.stringify(['a', 'b', 'c']))
})

test('reorderPeerInto position=before: inserts before the target (default)', () => {
  const { reorderPeerInto } = load()
  // Explicit 'before' is the default — insert at target's index, adjusted when
  // the peer was ahead of the target (to > from shifts left).
  assert.equal(JSON.stringify(reorderPeerInto(['a', 'b', 'c'], 'a', 'c', 'before')), JSON.stringify(['b', 'a', 'c']))
  // Move 'c' before 'a' — 'c' is after 'a', so no index shift needed.
  assert.equal(JSON.stringify(reorderPeerInto(['a', 'b', 'c'], 'c', 'a', 'before')), JSON.stringify(['c', 'a', 'b']))
})

test('reorderPeerInto position=after: inserts after the target', () => {
  const { reorderPeerInto } = load()
  // In-range move: 'a' after 'c' → [b, c, a]
  assert.equal(JSON.stringify(reorderPeerInto(['a', 'b', 'c'], 'a', 'c', 'after')), JSON.stringify(['b', 'c', 'a']))
  // Cross-range insert after target: 'x' after 'c' → [a, b, c, x]
  assert.equal(
    JSON.stringify(reorderPeerInto(['a', 'b', 'c'], 'x', 'c', 'after')),
    JSON.stringify(['a', 'b', 'c', 'x'])
  )
  // Cross-range insert after FIRST item: 'x' after 'a' → [a, x, b, c]
  assert.equal(
    JSON.stringify(reorderPeerInto(['a', 'b', 'c'], 'x', 'a', 'after')),
    JSON.stringify(['a', 'x', 'b', 'c'])
  )
})

test('reorderPeerInto position=after with null target appends (same as before)', () => {
  const { reorderPeerInto } = load()
  assert.equal(JSON.stringify(reorderPeerInto(['a', 'b'], 'a', null, 'after')), JSON.stringify(['b', 'a']))
})

test('reorderPeerInto position=after: no-op when already after target', () => {
  const { reorderPeerInto } = load()
  // 'b' is already directly after 'a' — no change
  assert.equal(reorderPeerInto(['a', 'b'], 'b', 'a', 'after'), null)
  // 'a' after last is no-op (same as null append for last)
  assert.equal(reorderPeerInto(['x', 'a'], 'a', null, 'after'), null)
})

test('reorderPeerInto position=before: no-op boundaries', () => {
  const { reorderPeerInto } = load()
  // Already before target
  assert.equal(reorderPeerInto(['a', 'b', 'c'], 'a', 'b', 'before'), null)
  // Unknown target
  assert.equal(reorderPeerInto(['a', 'b'], 'a', 'ghost', 'before'), null)
  // Same peer
  assert.equal(reorderPeerInto(['a', 'b'], 'a', 'a', 'before'), null)
})

// ── applyPeerDrop ───────────────────────────────────────────────────────────

test('applyPeerDrop Sections + alpha: assigns a peer to a section, no reorder', () => {
  const { applyPeerDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: {},
    sectionOrder: ['section:A']
  }

  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::atlas',
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_ALPHA,
    fromRangeKey: null,
    toRangeKey: 'section:A',
    targetPeerId: null,
    rangePeerIds: ['legacy::atlas']
  })

  assert.equal(patch.sectionAssignments['legacy::atlas'], 'section:A')
  assert.equal(patch.manualOrderUnassigned, undefined, 'alpha sort never reorders')
})

test('applyPeerDrop Sections: dropping on the Unassigned range unassigns', () => {
  const { applyPeerDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: { 'legacy::atlas': 'section:A' },
    sectionOrder: ['section:A']
  }

  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::atlas',
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_PINNED_RECENCY,
    fromRangeKey: 'section:A',
    toRangeKey: null,
    targetPeerId: null,
    rangePeerIds: ['legacy::atlas']
  })

  assert.equal(patch.sectionAssignments['legacy::atlas'], undefined)
  assert.equal(Object.keys(patch.sectionAssignments).length, 0)
})

test('applyPeerDrop Sections + manual: assigns AND inserts into the target range bucket', () => {
  const { applyPeerDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: {},
    sectionOrder: ['section:A'],
    manualOrderUnassigned: {}
  }

  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::atlas',
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: null,
    toRangeKey: 'section:A',
    targetPeerId: null,
    rangePeerIds: ['legacy::hermes']
  })

  assert.equal(patch.sectionAssignments['legacy::atlas'], 'section:A')
  // atlas is appended into section:A's bucket (rangePeerIds [hermes] + atlas at end)
  assert.equal(
    JSON.stringify(patch.manualOrderUnassigned['section:A']),
    JSON.stringify(['legacy::hermes', 'legacy::atlas'])
  )
})

test('applyPeerDrop Sections + manual: collapsed Section header appends without discarding hidden order', () => {
  const { applyPeerDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: { 'legacy::a': 'section:A', 'legacy::b': 'section:A' },
    sectionOrder: ['section:A'],
    manualOrderUnassigned: { 'section:A': ['legacy::a', 'legacy::b'] }
  }

  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::c',
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: null,
    toRangeKey: 'section:A',
    targetPeerId: null,
    rangePeerIds: []
  })

  assert.deepEqual(patch.manualOrderUnassigned['section:A'], ['legacy::a', 'legacy::b', 'legacy::c'])
})

test('applyPeerDrop Sections + manual: reorder within the same section', () => {
  const { applyPeerDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: { 'legacy::atlas': 'section:A', 'legacy::hermes': 'section:A' },
    sectionOrder: ['section:A'],
    manualOrderUnassigned: { 'section:A': ['legacy::atlas', 'legacy::hermes'] }
  }

  // Move hermes before atlas (was [atlas, hermes] → [hermes, atlas])
  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::hermes',
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: 'section:A',
    toRangeKey: 'section:A',
    targetPeerId: 'legacy::atlas',
    rangePeerIds: ['legacy::atlas', 'legacy::hermes']
  })

  assert.equal(
    JSON.stringify(patch.manualOrderUnassigned['section:A']),
    JSON.stringify(['legacy::hermes', 'legacy::atlas'])
  )
})

test('applyPeerDrop None + manual: reorders the flat bucket', () => {
  const { applyPeerDrop } = load()
  const viewState = { manualOrderNone: ['legacy::atlas', 'legacy::hermes', 'legacy::scout'] }

  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::scout',
    kind: 'bot',
    grouping: VIEW_GROUPING_NONE,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: null,
    toRangeKey: null,
    targetPeerId: 'legacy::atlas',
    rangePeerIds: ['legacy::atlas', 'legacy::hermes', 'legacy::scout']
  })

  assert.equal(
    JSON.stringify(patch.manualOrderNone),
    JSON.stringify(['legacy::scout', 'legacy::atlas', 'legacy::hermes'])
  )
})

test('applyPeerDrop None + alpha/recency: sort is authoritative, no reorder', () => {
  const { applyPeerDrop } = load()
  const viewState = { manualOrderNone: ['legacy::atlas'] }

  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::atlas',
    kind: 'bot',
    grouping: VIEW_GROUPING_NONE,
    sort: VIEW_SORT_ALPHA,
    fromRangeKey: null,
    toRangeKey: null,
    targetPeerId: null,
    rangePeerIds: ['legacy::atlas']
  })

  assert.equal(patch, null)
})

test('applyPeerDrop Groups + manual: reorders within the same automatic group', () => {
  const { applyPeerDrop } = load()
  const viewState = { manualOrderGroups: { 'group:Engineering': ['legacy::atlas', 'legacy::kiln'] } }

  // Move kiln before atlas (was [atlas, kiln] → [kiln, atlas])
  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::kiln',
    kind: 'bot',
    grouping: VIEW_GROUPING_GROUPS,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: 'Engineering',
    toRangeKey: 'Engineering',
    targetPeerId: 'legacy::atlas',
    rangePeerIds: ['legacy::atlas', 'legacy::kiln']
  })

  assert.equal(
    JSON.stringify(patch.manualOrderGroups['group:Engineering']),
    JSON.stringify(['legacy::kiln', 'legacy::atlas'])
  )
})

test('applyPeerDrop Groups + manual: cross-group drop is ignored and never mutates groups[]', () => {
  const { applyPeerDrop } = load()
  const viewState = { manualOrderGroups: { 'group:Engineering': ['legacy::atlas'] } }

  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::atlas',
    kind: 'bot',
    grouping: VIEW_GROUPING_GROUPS,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: 'Engineering',
    toRangeKey: 'Research',
    targetPeerId: 'legacy::scout',
    rangePeerIds: ['legacy::scout']
  })

  assert.equal(patch, null, 'cross-group drop must be ignored')
  // viewState.manualOrderGroups untouched (no mutation of groups[]/rooms)
  assert.equal(JSON.stringify(viewState.manualOrderGroups), JSON.stringify({ 'group:Engineering': ['legacy::atlas'] }))
})

test('applyPeerDrop Groups + alpha/recency: authoritative, no reorder', () => {
  const { applyPeerDrop } = load()

  const patch = applyPeerDrop(
    { manualOrderGroups: {} },
    {
      peerId: 'legacy::atlas',
      kind: 'bot',
      grouping: VIEW_GROUPING_GROUPS,
      sort: VIEW_SORT_ALPHA,
      fromRangeKey: 'Engineering',
      toRangeKey: 'Engineering',
      targetPeerId: null,
      rangePeerIds: ['legacy::atlas']
    }
  )

  assert.equal(patch, null)
})

test('applyPeerDrop Sections + manual + position=after: cross-range insert after target', () => {
  const { applyPeerDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: {},
    sectionOrder: ['section:A'],
    manualOrderUnassigned: { 'section:A': ['legacy::hermes'] }
  }

  // Insert atlas AFTER hermes into section:A's manual bucket
  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::atlas',
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: null,
    toRangeKey: 'section:A',
    targetPeerId: 'legacy::hermes',
    position: 'after',
    rangePeerIds: ['legacy::hermes']
  })

  assert.equal(patch.sectionAssignments['legacy::atlas'], 'section:A')
  assert.equal(
    JSON.stringify(patch.manualOrderUnassigned['section:A']),
    JSON.stringify(['legacy::hermes', 'legacy::atlas'])
  )
})

test('applyPeerDrop Sections + manual + position=after: reorder after target within same section', () => {
  const { applyPeerDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: { 'legacy::atlas': 'section:A', 'legacy::hermes': 'section:A' },
    sectionOrder: ['section:A'],
    manualOrderUnassigned: { 'section:A': ['legacy::atlas', 'legacy::hermes'] }
  }

  // Move atlas after hermes (was [atlas, hermes] → [hermes, atlas])
  const patch = applyPeerDrop(viewState, {
    peerId: 'legacy::atlas',
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_MANUAL,
    fromRangeKey: 'section:A',
    toRangeKey: 'section:A',
    targetPeerId: 'legacy::hermes',
    position: 'after',
    rangePeerIds: ['legacy::atlas', 'legacy::hermes']
  })

  assert.equal(
    JSON.stringify(patch.manualOrderUnassigned['section:A']),
    JSON.stringify(['legacy::hermes', 'legacy::atlas'])
  )
})

test('applyPeerDrop adversarial: a Bot named "group:x" and the group "x" key independently', () => {
  const { applyPeerDrop, groupPeerIdentity } = load()

  // The group's peerId is `group:Engineering`; a bot's peerId is `legacy::group:Engineering`
  // (botRosterKey namespaces the bot, so they cannot collide). Both are opaque to
  // the drop resolver — assignment keys by exact identity only.
  const viewState = {
    visualSections: [{ key: 'section:A', title: 'Core', order: 0 }],
    sectionAssignments: { [groupPeerIdentity('Engineering')]: 'section:A' },
    sectionOrder: ['section:A']
  }

  const botPeerId = 'legacy::group:Engineering'

  const patch = applyPeerDrop(viewState, {
    peerId: botPeerId,
    kind: 'bot',
    grouping: VIEW_GROUPING_SECTIONS,
    sort: VIEW_SORT_ALPHA,
    fromRangeKey: null,
    toRangeKey: 'section:A',
    targetPeerId: null,
    rangePeerIds: [botPeerId]
  })

  // The BOT gets its own assignment; the group's assignment is untouched.
  assert.equal(patch.sectionAssignments[botPeerId], 'section:A')
  assert.equal(patch.sectionAssignments[groupPeerIdentity('Engineering')], 'section:A')
})

test('applyPeerDrop adversarial: a group named "unassigned" cannot collide with the reserved bucket', () => {
  const { applyPeerDrop, manualOrderBucketKey } = load()
  assert.notEqual(manualOrderBucketKey(VIEW_GROUPING_GROUPS, 'unassigned'), 'unassigned')

  // Dragging a bot within a group named "unassigned" lands in `group:unassigned`,
  // never the reserved `unassigned` key.
  const patch = applyPeerDrop(
    { manualOrderGroups: {} },
    {
      peerId: 'legacy::atlas',
      kind: 'bot',
      grouping: VIEW_GROUPING_GROUPS,
      sort: VIEW_SORT_MANUAL,
      fromRangeKey: 'unassigned',
      toRangeKey: 'unassigned',
      targetPeerId: 'legacy::hermes',
      rangePeerIds: ['legacy::hermes', 'legacy::atlas']
    }
  )

  assert.equal(
    JSON.stringify(patch.manualOrderGroups['group:unassigned']),
    JSON.stringify(['legacy::atlas', 'legacy::hermes'])
  )
  assert.equal(patch.manualOrderGroups['unassigned'], undefined)
})

// ── applySectionDrop ────────────────────────────────────────────────────────

test('applySectionDrop: reorders two visual sections', () => {
  const { applySectionDrop } = load()

  const viewState = {
    visualSections: [
      { key: 'section:A', title: 'A', order: 0 },
      { key: 'section:B', title: 'B', order: 1 }
    ],
    sectionOrder: ['section:A', 'section:B']
  }

  const patch = applySectionDrop(viewState, { sectionKey: 'section:B', toSectionKey: 'section:A' })
  assert.equal(JSON.stringify(patch.sectionOrder), JSON.stringify(['section:B', 'section:A']))
  assert.equal(patch.sectionAssignments, undefined, 'section reorder never touches assignments')
})

test('applySectionDrop: honors before and after insertion positions', () => {
  const { applySectionDrop } = load()

  const viewState = {
    visualSections: [
      { key: 'section:A', title: 'A', order: 0 },
      { key: 'section:B', title: 'B', order: 1 },
      { key: 'section:C', title: 'C', order: 2 }
    ],
    sectionOrder: ['section:A', 'section:B', 'section:C']
  }

  const after = applySectionDrop(viewState, {
    sectionKey: 'section:A',
    toSectionKey: 'section:B',
    position: 'after'
  })

  assert.equal(JSON.stringify(after.sectionOrder), JSON.stringify(['section:B', 'section:A', 'section:C']))

  const before = applySectionDrop(viewState, {
    sectionKey: 'section:C',
    toSectionKey: 'section:B',
    position: 'before'
  })

  assert.equal(JSON.stringify(before.sectionOrder), JSON.stringify(['section:A', 'section:C', 'section:B']))
})

test('applySectionDrop: same section or unknown target is a no-op', () => {
  const { applySectionDrop } = load()

  const viewState = {
    visualSections: [{ key: 'section:A', title: 'A', order: 0 }],
    sectionOrder: ['section:A']
  }

  assert.equal(applySectionDrop(viewState, { sectionKey: 'section:A', toSectionKey: 'section:A' }), null)
  assert.equal(applySectionDrop(viewState, { sectionKey: 'section:A', toSectionKey: 'section:GHOST' }), null)
  assert.equal(applySectionDrop(viewState, {}), null)
})

// ── Per-range manual order in applyGroupingMode ─────────────────────────────

test('applyGroupingMode Sections + manual: each range obeys its own order bucket', () => {
  const { applyGroupingMode, VIEW_GROUPING_SECTIONS, VIEW_SORT_MANUAL } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::a', name: 'a', groups: [], meta: meta(), botRow: { name: 'a' } },
    { kind: 'bot', peerId: 'legacy::b', name: 'b', groups: [], meta: meta(), botRow: { name: 'b' } },
    { kind: 'bot', peerId: 'legacy::c', name: 'c', groups: [], meta: meta(), botRow: { name: 'c' } },
    { kind: 'bot', peerId: 'legacy::d', name: 'd', groups: [], meta: meta(), botRow: { name: 'd' } }
  ]

  const viewState = {
    sort: VIEW_SORT_MANUAL,
    visualSections: [
      { key: 'section:A', title: 'A', order: 0 },
      { key: 'section:B', title: 'B', order: 1 }
    ],
    sectionAssignments: {
      'legacy::a': 'section:A',
      'legacy::b': 'section:A',
      'legacy::c': 'section:B',
      'legacy::d': 'section:B'
    },
    sectionCollapsed: {},
    sectionOrder: ['section:A', 'section:B'],
    manualOrderUnassigned: {
      'section:A': ['legacy::b', 'legacy::a'], // b before a (reverse of input)
      'section:B': ['legacy::d', 'legacy::c']
    }
  }

  const result = applyGroupingMode(VIEW_GROUPING_SECTIONS, peers, viewState, false)
  const sectionA = result.find(r => r.sectionKey === 'section:A')
  const sectionB = result.find(r => r.sectionKey === 'section:B')
  assert.equal(JSON.stringify(sectionA.peers.map(p => p.peerId)), JSON.stringify(['legacy::b', 'legacy::a']))
  assert.equal(JSON.stringify(sectionB.peers.map(p => p.peerId)), JSON.stringify(['legacy::d', 'legacy::c']))
})

test('applyGroupingMode Groups + manual: ungrouped and per-group buckets are authoritative', () => {
  const { applyGroupingMode, VIEW_GROUPING_GROUPS, VIEW_SORT_MANUAL } = load()

  const peers = [
    { kind: 'bot', peerId: 'legacy::u1', name: 'u1', groups: [], meta: meta(), botRow: { name: 'u1' } },
    { kind: 'bot', peerId: 'legacy::u2', name: 'u2', groups: [], meta: meta(), botRow: { name: 'u2' } },
    { kind: 'bot', peerId: 'legacy::a', name: 'a', groups: ['Eng'], meta: meta(), botRow: { name: 'a' } },
    { kind: 'bot', peerId: 'legacy::b', name: 'b', groups: ['Eng'], meta: meta(), botRow: { name: 'b' } },
    { kind: 'group', peerId: 'group:Eng', title: 'Eng', meta: null }
  ]

  const viewState = {
    sort: VIEW_SORT_MANUAL,
    manualOrderGroups: {
      unassigned: ['legacy::u2', 'legacy::u1'],
      'group:Eng': ['legacy::b', 'legacy::a']
    }
  }

  const result = applyGroupingMode(VIEW_GROUPING_GROUPS, peers, viewState, false)
  const standalone = result.filter(r => r.level === 0 && r.kind === 'bot')
  assert.equal(
    JSON.stringify(standalone.map(p => p.peerId)),
    JSON.stringify(['legacy::u2', 'legacy::u1', 'legacy::a', 'legacy::b'])
  )

  const eng = result.find(r => r.peerId === 'group:Eng')
  assert.equal(JSON.stringify(eng.children.map(c => c.peerId)), JSON.stringify(['legacy::b', 'legacy::a']))
})
