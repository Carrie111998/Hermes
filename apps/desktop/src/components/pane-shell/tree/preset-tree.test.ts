import { describe, expect, it } from 'vitest'

import { allPaneIds, group, split } from './model'
import { isPresetExcludedPaneId, stripPresetLivePanes } from './preset-tree'

describe('stripPresetLivePanes', () => {
  it('drops session tiles and keeps structural rails', () => {
    const tree = split(
      'row',
      [
        group(['sessions', 'preview'], { active: 'sessions', id: 'grp-sessions' }),
        group(['workspace', 'session-tile:20260823_193634_728a24', 'session-tile:20260823_193532_a7dbd9'], {
          active: 'session-tile:20260823_193634_728a24',
          id: 'grp-main'
        }),
        group(['files'], { active: 'files', id: 'grp-files' })
      ],
      [1, 3.4, 1.25],
      'spl-root'
    )

    const next = stripPresetLivePanes(tree)

    expect(next).not.toBeNull()
    expect(allPaneIds(next!)).toEqual(['sessions', 'preview', 'workspace', 'files'])
    expect(next).toMatchObject({
      type: 'split',
      id: 'spl-root',
      children: [{ id: 'grp-sessions' }, { id: 'grp-main', active: 'workspace', panes: ['workspace'] }, { id: 'grp-files' }]
    })
  })

  it('drops preview-tile:undefined and route tiles, then collapses the empty zone', () => {
    const tree = split('row', [
      group(['workspace'], { active: 'workspace', id: 'grp-main' }),
      group(['preview-tile:undefined', 'route-tile:skills'], { active: 'preview-tile:undefined', id: 'grp-ghost' })
    ])

    const next = stripPresetLivePanes(tree)

    expect(next).toMatchObject({ type: 'group', id: 'grp-main', panes: ['workspace'] })
    expect(allPaneIds(next!)).not.toEqual(expect.arrayContaining(['preview-tile:undefined', 'route-tile:skills']))
  })

  it('returns the same reference when the tree is already clean', () => {
    const tree = group(['workspace', 'files'], { active: 'workspace', id: 'grp-main' })

    expect(stripPresetLivePanes(tree)).toBe(tree)
  })

  it('returns null when a preset is only live tiles (nothing structural left)', () => {
    const tree = group(['session-tile:abc', 'preview-tile:file:/tmp/x'], { active: 'session-tile:abc', id: 'only' })

    expect(stripPresetLivePanes(tree)).toBeNull()
  })

  it('classifies the prefixes the field report baked into user-jrl', () => {
    expect(isPresetExcludedPaneId('session-tile:20260823_193634_728a24')).toBe(true)
    expect(isPresetExcludedPaneId('preview-tile:undefined')).toBe(true)
    expect(isPresetExcludedPaneId('route-tile:page')).toBe(true)
    expect(isPresetExcludedPaneId('workspace')).toBe(false)
    expect(isPresetExcludedPaneId('hermes-bots:pane')).toBe(false)
    expect(isPresetExcludedPaneId('terminal')).toBe(false)
  })
})
