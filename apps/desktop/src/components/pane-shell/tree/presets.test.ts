import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { allPaneIds, group, split } from './model'

const USER_KEY = 'hermes.desktop.layoutPresets.v2'

function dirtyJrlTree() {
  return split(
    'row',
    [
      group(['sessions', 'preview'], { active: 'sessions', id: 'grp-sessions' }),
      group(['workspace', 'session-tile:20260823_193634_728a24'], {
        active: 'session-tile:20260823_193634_728a24',
        id: 'grp-main'
      }),
      group(['preview-tile:undefined'], { active: 'preview-tile:undefined', id: 'grp-ghost' })
    ],
    [1, 3.4, 1],
    'spl-root'
  )
}

describe('user layout presets strip live tiles (#94260)', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    vi.resetModules()
  })

  it('heals a stored preset that baked in session and preview tiles', async () => {
    window.localStorage.setItem(
      USER_KEY,
      JSON.stringify({ 'user-jrl': { name: 'JRL', tree: dirtyJrlTree() } })
    )

    await import('./presets')

    const stored = JSON.parse(window.localStorage.getItem(USER_KEY) ?? 'null') as {
      'user-jrl': { name: string; tree: ReturnType<typeof dirtyJrlTree> }
    }

    expect(stored['user-jrl'].name).toBe('JRL')
    expect(allPaneIds(stored['user-jrl'].tree)).toEqual(['sessions', 'preview', 'workspace'])
    expect(allPaneIds(stored['user-jrl'].tree).some(id => id.startsWith('session-tile:'))).toBe(false)
    expect(allPaneIds(stored['user-jrl'].tree)).not.toContain('preview-tile:undefined')
  })

  it('saves only geometry, not the open session tiles', async () => {
    const { saveLayoutPresetTree } = await import('./presets')

    const id = saveLayoutPresetTree('JRL', dirtyJrlTree())

    expect(id).toBe('user-jrl')

    const stored = JSON.parse(window.localStorage.getItem(USER_KEY) ?? 'null') as {
      'user-jrl': { tree: ReturnType<typeof dirtyJrlTree> }
    }

    expect(allPaneIds(stored['user-jrl'].tree)).toEqual(['sessions', 'preview', 'workspace'])
  })

  it('applies a dirty snapshot without resurrecting baked-in session tiles', async () => {
    const store = await import('./store')
    const apply = vi.spyOn(store, 'applyTree')
    const { applyLayoutPreset } = await import('./presets')

    applyLayoutPreset('user-jrl', dirtyJrlTree())

    expect(apply).toHaveBeenCalledTimes(1)
    const passed = apply.mock.calls[0]?.[0]
    expect(passed).toBeDefined()
    expect(allPaneIds(passed!)).toEqual(['sessions', 'preview', 'workspace'])
    apply.mockRestore()
  })
})
