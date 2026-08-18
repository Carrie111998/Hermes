import { ContextMenu, ContextMenuTrigger, DropdownMenu, DropdownMenuTrigger } from '@hermes/plugin-sdk'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

// Exercise the real ESM module; production never exposes this seam.
;(globalThis as any).__HERMES_BOTS_TEST__ = true
// @ts-expect-error Bot Mode is an intentionally plain-JavaScript bundled plugin.
await import('./plugin.js')
const plugin = (globalThis as any).__HERMES_BOTS_TEST_API__

const {
  $groupChats,
  $lastRoster,
  $botMeta,
  $viewStateWrapper,
  applyGroupingMode,
  applyKeyboardPeerMove,
  BOT_LOCALES,
  BotRow,
  GroupRowEnhanced,
  hydrateViewState,
  insertionMatches,
  ManageSectionsDialog,
  MoveToSectionMenu,
  normalizeViewState,
  remapGroupViewState,
  renderProjectedRoster,
  SectionHeader,
  setPluginCtxForTest,
  ViewMenuContent
} = plugin

const translate = (key: string, ...args: unknown[]) => {
  let value: any = BOT_LOCALES.en

  for (const part of key.split('.')) {
    value = value?.[part]
  }

  return typeof value === 'function' ? value(...args) : String(value ?? key)
}

setPluginCtxForTest({ i18n: { t: translate } })

afterEach(() => cleanup())

describe('Bot roster localized copy', () => {
  it('ships the same message contract for every supported locale', () => {
    const flatten = (value: any, prefix = ''): string[] =>
      Object.entries(value)
        .flatMap(([key, child]) => {
          const path = prefix ? `${prefix}.${key}` : key

          return child && typeof child === 'object' ? flatten(child, path) : [path]
        })
        .sort()

    const enKeys = flatten(BOT_LOCALES.en)

    expect(Object.keys(BOT_LOCALES).sort()).toEqual(['en', 'ja', 'zh', 'zh-hant'])

    for (const bundle of Object.values(BOT_LOCALES)) {
      expect(flatten(bundle)).toEqual(enKeys)
    }

    expect(BOT_LOCALES.ja.view.grouping).not.toBe(BOT_LOCALES.en.view.grouping)
  })
})

describe('Bot roster view state lifecycle', () => {
  it('starts with a renderable normalized state before async storage hydration', () => {
    expect($viewStateWrapper.get().state).toEqual(normalizeViewState(null))
  })

  it('rejects hydration when a local mutation lands after the read starts', () => {
    const initial = {
      state: normalizeViewState(null),
      token: 0,
      localGeneration: 0
    }

    const locallyMutated = {
      state: normalizeViewState({ grouping: 'sections' }),
      token: 0,
      localGeneration: 1
    }

    expect(hydrateViewState(locallyMutated, { grouping: 'groups' }, 1, initial.localGeneration)).toBe(locallyMutated)
  })

  it('accepts a newer hydration when the generation is unchanged since read start', () => {
    const current = {
      state: normalizeViewState({ grouping: 'none' }),
      token: 1,
      localGeneration: 3
    }

    const hydrated = hydrateViewState(current, { grouping: 'sections' }, 2, current.localGeneration)

    expect(hydrated.state.grouping).toBe('sections')
    expect(hydrated.token).toBe(2)
    expect(hydrated.localGeneration).toBe(3)
  })
})

describe('Bot roster projection contracts', () => {
  const botPeer = (peerId: string, name: string, groups: string[] = []) => ({
    kind: 'bot',
    peerId,
    name,
    groups,
    botRow: { name },
    meta: { groups }
  })

  const groupPeer = (
    title: string,
    members: Array<{ name: string; connectionId?: string; remoteSource?: boolean }> = []
  ) => ({
    kind: 'group',
    peerId: `group:${title}`,
    title,
    members
  })

  it('preserves the selected top-level order when shortcut nesting is enabled', () => {
    const alpha = botPeer('legacy::alpha', 'Alpha', ['Bravo'])
    const bravo = groupPeer('Bravo', [{ name: 'Alpha' }])
    const charlie = botPeer('legacy::charlie', 'Charlie')

    const projected = applyGroupingMode('none', [alpha, bravo, charlie], { nestedExpansion: {} }, true)

    expect(projected.map((item: { peerId: string }) => item.peerId)).toEqual([
      'legacy::alpha',
      'group:Bravo',
      'legacy::charlie'
    ])
  })

  it('uses source-qualified seated group members for shortcut children', () => {
    const remote = {
      ...botPeer('mini::spark', 'spark'),
      botRow: { name: 'spark', remoteSource: true, connectionId: 'mini', sourceScoped: true }
    }

    const group = groupPeer('Remote', [{ name: 'spark', remoteSource: true, connectionId: 'mini' }])

    const projected = applyGroupingMode('groups', [remote, group], {}, true)
    const shortcut = projected.find((item: { peerId: string }) => item.peerId === 'group:Remote')

    expect(shortcut.children.map((item: { peerId: string }) => item.peerId)).toEqual(['mini::spark'])
  })

  it('returns no projection for a Sections search with no matches', () => {
    const projected = applyGroupingMode(
      'sections',
      [botPeer('legacy::atlas', 'Atlas')],
      {
        visualSections: [{ key: 'section:core', title: 'Core' }],
        sectionAssignments: { 'legacy::atlas': 'section:core' },
        sectionOrder: ['section:core'],
        sectionCollapsed: {},
        nestedExpansion: {}
      },
      false,
      { query: 'missing', matchesBotId: new Set() }
    )

    expect(projected).toEqual([])
  })

  it('temporarily expands a collapsed Section to reveal a search match', () => {
    const projected = applyGroupingMode(
      'sections',
      [botPeer('legacy::atlas', 'Atlas')],
      {
        visualSections: [{ key: 'section:core', title: 'Core' }],
        sectionAssignments: { 'legacy::atlas': 'section:core' },
        sectionOrder: ['section:core'],
        sectionCollapsed: { 'section:core': true },
        nestedExpansion: {}
      },
      false,
      { query: 'atlas', matchesBotId: new Set(['legacy::atlas']) }
    )

    expect(projected).toHaveLength(1)
    expect(projected[0].collapsed).toBe(false)
    expect(projected[0].peers.map((item: { peerId: string }) => item.peerId)).toEqual(['legacy::atlas'])
  })

  it('migrates or clears group-keyed view state when a room identity changes', () => {
    const state = normalizeViewState({
      visualSections: [{ key: 'section:core', title: 'Core' }],
      sectionOrder: ['section:core'],
      sectionAssignments: { 'group:Old': 'section:core' },
      manualOrderNone: ['legacy::atlas', 'group:Old'],
      manualOrderUnassigned: { 'section:core': ['group:Old'] },
      manualOrderGroups: { 'group:Old': ['legacy::atlas'] },
      nestedExpansion: { 'group:Old': true }
    })

    const renamed = normalizeViewState({ ...state, ...remapGroupViewState(state, 'Old', 'New') })
    expect(renamed.sectionAssignments['group:New']).toBe('section:core')
    expect(renamed.manualOrderNone).toContain('group:New')
    expect(renamed.manualOrderGroups['group:New']).toEqual(['legacy::atlas'])
    expect(renamed.nestedExpansion['group:New']).toBe(true)

    const removed = normalizeViewState({ ...renamed, ...remapGroupViewState(renamed, 'New', null) })
    expect(removed.sectionAssignments['group:New']).toBeUndefined()
    expect(removed.manualOrderNone).not.toContain('group:New')
    expect(removed.manualOrderGroups['group:New']).toBeUndefined()
    expect(removed.nestedExpansion['group:New']).toBeUndefined()
  })

  it('reorders a manual peer range from the keyboard without drag state', () => {
    const patch = applyKeyboardPeerMove(
      normalizeViewState({ sort: 'manual', manualOrderNone: ['legacy::atlas', 'legacy::forge', 'legacy::scout'] }),
      {
        peerId: 'legacy::forge',
        kind: 'bot',
        grouping: 'none',
        rangeKey: null,
        rangePeerIds: ['legacy::atlas', 'legacy::forge', 'legacy::scout'],
        delta: -1
      }
    )

    expect(patch.manualOrderNone).toEqual(['legacy::forge', 'legacy::atlas', 'legacy::scout'])
  })

  it('scopes insertion feedback to one shortcut range', () => {
    const insertion = {
      type: 'peer',
      targetId: 'legacy::kiln',
      position: 'before',
      rangeKey: 'Engineering'
    }

    expect(insertionMatches(insertion, 'legacy::kiln', 'Engineering', 'before')).toBe(true)
    expect(insertionMatches(insertion, 'legacy::kiln', 'Research', 'before')).toBe(false)
    expect(insertionMatches(insertion, 'legacy::kiln', null, 'before')).toBe(false)
  })
})

describe('Bot roster current-main integrations', () => {
  it('exposes grouping/sorting and toggles as semantic menu controls', () => {
    render(
      createElement(DropdownMenu, {
        open: true,
        children: [
          createElement(DropdownMenuTrigger, { key: 'trigger' }, 'Open'),
          createElement(ViewMenuContent, {
            key: 'content',
            viewState: { grouping: 'sections', sort: 'manual', nestMembers: true, showHidden: true },
            onSetGrouping: () => undefined,
            onSetSort: () => undefined,
            onToggleNest: () => undefined,
            onToggleShowHidden: () => undefined,
            onManageSections: () => undefined
          })
        ]
      })
    )

    expect(screen.getAllByRole('menuitemradio')).toHaveLength(6)
    expect(screen.getAllByRole('menuitemcheckbox')).toHaveLength(2)
    expect(
      screen.getAllByRole('menuitemradio').filter(item => item.getAttribute('data-state') === 'checked')
    ).toHaveLength(2)
  })

  it('gives collapse and Section-management icon buttons accessible names without native titles', () => {
    const { rerender } = render(
      createElement(SectionHeader, {
        title: 'Core',
        collapsed: false,
        peerCount: 2,
        sectionKey: 'section:core'
      })
    )

    const collapseSection = screen.getByRole('button', { name: 'Collapse Core' })
    expect(collapseSection.getAttribute('title')).toBeNull()

    rerender(
      createElement(GroupRowEnhanced, {
        group: 'Engineering',
        members: [{ name: 'atlas' }],
        needsYou: false,
        onOpen: () => undefined,
        collapsed: false,
        peerId: 'group:Engineering',
        viewState: { grouping: 'sections' },
        collapsible: true,
        onToggleCollapse: () => undefined
      })
    )
    const collapseGroup = screen.getByRole('button', { name: 'Collapse Engineering' })
    expect(collapseGroup.getAttribute('title')).toBeNull()

    cleanup()
    $lastRoster.set([{ name: 'atlas' }])
    $botMeta.set({ atlas: { groups: [] } })
    $groupChats.set({})
    $viewStateWrapper.set({
      state: normalizeViewState({
        grouping: 'sections',
        visualSections: [{ key: 'section:core', title: 'Core' }],
        sectionOrder: ['section:core']
      }),
      token: 1,
      localGeneration: 0
    })
    render(createElement(ManageSectionsDialog, { open: true, onClose: () => undefined }))

    expect(screen.getByRole('button', { name: 'Move Core up' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Move Core down' })).toBeTruthy()
    const renameButton = screen.getByRole('button', { name: 'Rename Core' })
    expect(renameButton).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Delete Core' })).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'New section name…' })).toBeTruthy()

    const unassigned = screen.getByRole('button', { name: 'Move Atlas to Unassigned' })
    const core = screen.getByRole('button', { name: 'Move Atlas to Core' })
    expect(unassigned.getAttribute('aria-pressed')).toBe('true')
    expect(core.getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(renameButton)
    expect(screen.getByRole('textbox', { name: 'Rename Core' })).toBeTruthy()
  })

  it('does not advertise manual keyboard movement on automatic group headers', () => {
    const tree = renderProjectedRoster(
      [
        {
          kind: 'group',
          peerId: 'group:Engineering',
          title: 'Engineering',
          members: [],
          level: 0,
          children: [],
          isGroupHeader: true
        }
      ],
      'groups',
      {
        setDeleting: () => undefined,
        setEditing: () => undefined,
        setGrouping: () => undefined,
        groupNeedsYou: {},
        openGroupChat: () => undefined,
        viewState: { grouping: 'groups', sort: 'manual' },
        toggleNestedGroup: () => undefined,
        drag: {
          canDragPeer: true,
          canKeyboardMovePeer: true,
          insertion: null,
          endPeerDrag: () => undefined,
          keyboardMovePeer: () => undefined
        }
      }
    )

    const { container } = render(createElement('div', null, tree))
    const group = [...container.querySelectorAll('button')].find(button => button.textContent?.includes('Engineering'))

    expect(group).toBeTruthy()
    expect(group?.getAttribute('aria-keyshortcuts')).toBeNull()
  })

  it('renders a configured room picture in the active group-row component', () => {
    $groupChats.set({
      Engineering: { image: 'data:image/png;base64,room-picture', log: [] }
    })

    const { container } = render(
      createElement(GroupRowEnhanced, {
        group: 'Engineering',
        members: [],
        needsYou: false,
        onOpen: () => undefined,
        peerId: 'group:Engineering',
        viewState: { grouping: 'none' }
      })
    )

    expect(container.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,room-picture')
  })

  it('exposes the current visual Section as a semantic context-menu radio choice', () => {
    render(
      createElement(ContextMenu, {
        open: true,
        children: [
          createElement(ContextMenuTrigger, { key: 'trigger', children: 'Open' }),
          createElement(MoveToSectionMenu, {
            key: 'content',
            peerId: 'mini::spark',
            viewState: { grouping: 'sections' },
            sectionAssignments: {},
            sections: [{ key: 'section:core', title: 'Core' }]
          })
        ]
      })
    )

    const choices = screen.getAllByRole('menuitemradio')
    const unassigned = screen.getByRole('menuitemradio', { name: 'Unassigned' })
    expect(choices).toHaveLength(2)
    expect(unassigned.getAttribute('aria-checked')).toBe('true')
  })

  it('offers Move to section for a source-qualified remote Bot without unsafe profile actions', async () => {
    const { container } = render(
      createElement(BotRow, {
        bot: {
          name: 'spark',
          remoteSource: true,
          sourceScoped: true,
          connectionId: 'mini',
          connectionLabel: 'Mini'
        },
        peerId: 'mini::spark',
        viewState: {
          grouping: 'sections',
          sectionAssignments: {},
          visualSections: [{ key: 'section:core', title: 'Core' }]
        },
        onDelete: () => undefined,
        onEdit: () => undefined,
        onGroup: () => undefined
      })
    )

    const row = container.querySelector('button')
    expect(row).not.toBeNull()
    fireEvent.contextMenu(row!)

    const moveToSection = await screen.findByText('Move to section')
    expect(moveToSection).toBeTruthy()
    expect(screen.queryByText('Edit Profile')).toBeNull()
  })
})
