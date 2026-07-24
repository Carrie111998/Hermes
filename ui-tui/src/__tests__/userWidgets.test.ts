import { mkdtemp, rm, writeFile } from 'fs/promises'
import { tmpdir } from 'os'
import { join } from 'path'

import { beforeEach, describe, expect, it } from 'vitest'

import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { launchWidget } from '../sdk/host.js'
import { defineWidgetApp, getWidgetApp, removeWidgetApp } from '../sdk/registry.js'
import { loadUserWidgets } from '../sdk/userWidgets.js'

const WIDGET = `
export default function register(sdk) {
  sdk.defineWidgetApp({
    id: 'test-user-widget',
    help: 'from disk',
    mode: 'ambient',
    init: arg => ({ arg }),
    reduce: state => state,
    render: ({ state, t }) => sdk.h(sdk.Text, { color: t.color.label }, state.arg)
  })
}
`

beforeEach(() => resetOverlayState())

describe('user widget loading', () => {
  it('missing directory is a clean no-op', async () => {
    const result = await loadUserWidgets(join(tmpdir(), 'definitely-missing-widgets-dir'))

    expect(result).toEqual({ added: [], errors: [], loaded: [], removed: [] })
  })

  it('loads .mjs from disk, registers, dispatches, and reports broken files', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'tui-widgets-'))

    await writeFile(join(dir, 'good.mjs'), WIDGET)
    await writeFile(join(dir, 'broken.mjs'), 'export default 42')
    await writeFile(join(dir, 'ignored.txt'), 'not a widget')

    const result = await loadUserWidgets(dir)

    expect(result.loaded).toEqual(['good.mjs'])
    expect(result.added).toEqual(['test-user-widget'])
    expect(result.errors).toMatchObject([{ file: 'broken.mjs' }])

    // Registered like any built-in: catalog metadata + launchable.
    expect(getWidgetApp('test-user-widget')).toMatchObject({ help: 'from disk', mode: 'ambient' })
    expect(launchWidget('test-user-widget', 'hi')).toBeNull()
    expect(getOverlayState().ambient).toMatchObject([{ appId: 'test-user-widget', state: { arg: 'hi' } }])
  })

  it('a deleted file unregisters its apps on the next scan', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'tui-widgets-'))
    const file = join(dir, 'gone.mjs')

    await writeFile(file, WIDGET.replace('test-user-widget', 'soon-gone'))
    await loadUserWidgets(dir)
    expect(getWidgetApp('soon-gone')).toBeDefined()

    await rm(file)
    const result = await loadUserWidgets(dir)

    expect(result.removed).toEqual(['soon-gone'])
    expect(getWidgetApp('soon-gone')).toBeUndefined()
  })

  it('a changed file unregisters ids that it no longer defines', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'tui-widgets-'))
    const file = join(dir, 'renamed.mjs')

    await writeFile(file, WIDGET.replace('test-user-widget', 'old-widget-id'))
    await loadUserWidgets(dir)
    expect(getWidgetApp('old-widget-id')).toBeDefined()

    await writeFile(file, WIDGET.replace('test-user-widget', 'new-widget-id'))
    const result = await loadUserWidgets(dir)

    expect(result.added).toEqual(['new-widget-id'])
    expect(result.removed).toEqual(['old-widget-id'])
    expect(getWidgetApp('old-widget-id')).toBeUndefined()
    expect(getWidgetApp('new-widget-id')).toBeDefined()
  })

  it('restores a built-in app after its user-widget shadow is deleted', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'tui-widgets-'))
    const file = join(dir, 'shadow.mjs')

    const builtIn = defineWidgetApp({
      id: 'shadowed-built-in',
      help: 'built in',
      init: () => null,
      reduce: state => state,
      render: () => null
    })

    try {
      await writeFile(
        file,
        WIDGET.replace('test-user-widget', 'shadowed-built-in').replace('from disk', 'user override')
      )
      await loadUserWidgets(dir)
      expect(getWidgetApp('shadowed-built-in')).toMatchObject({ help: 'user override' })

      await rm(file)
      const result = await loadUserWidgets(dir)

      expect(result.removed).toEqual([])
      expect(getWidgetApp('shadowed-built-in')).toBe(builtIn)
    } finally {
      removeWidgetApp('shadowed-built-in')
    }
  })
})
