/**
 * Registration contract for the bundled hermes-achievements plugin — the
 * wiring receipt: the plugin registers its page, sidebar nav, statusbar chip,
 * and palette command through the SDK's contribution registry, scoped to its
 * own namespace, and ships opt-in (`defaultEnabled: false`).
 *
 * Lives in `contrib/` (not the plugin dir) because the plugin fence lets
 * plugin code import only the SDK — this test exercises the contract through
 * the same host machinery the loader uses.
 */

import {
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS
} from '@hermes/plugin-sdk'
import { describe, expect, it } from 'vitest'

import plugin from '../plugins/hermes-achievements/plugin'

import { createPluginContext } from './plugin'
import { registry } from './registry'

function register() {
  const disposers: Array<() => void> = []
  const ctx = createPluginContext(plugin.id, dispose => disposers.push(dispose))
  plugin.register(ctx)

  return disposers
}

describe('hermes-achievements plugin registration', () => {
  it('ships opt-in (off until the user enables it in Settings ▸ Plugins)', () => {
    expect(plugin.id).toBe('hermes-achievements')
    expect(plugin.defaultEnabled).toBe(false)
  })

  it('registers the page, nav row, statusbar chip, and palette command', () => {
    const disposers = register()

    const areas = {
      page: registry.getArea(ROUTES_AREA),
      nav: registry.getArea(SIDEBAR_NAV_AREA),
      statusbar: registry.getArea(STATUSBAR_AREAS.right),
      palette: registry.getArea(PALETTE_AREA)
    }

    expect(areas.page.map(c => c.id)).toContain('hermes-achievements:page')
    expect(areas.page.find(c => c.id === 'hermes-achievements:page')?.data).toMatchObject({ path: '/achievements' })

    expect(areas.nav.map(c => c.id)).toContain('hermes-achievements:nav')
    expect(areas.nav.find(c => c.id === 'hermes-achievements:nav')?.data).toMatchObject({ path: '/achievements' })

    expect(areas.statusbar.map(c => c.id)).toContain('hermes-achievements:score')
    expect(areas.palette.map(c => c.id)).toContain('hermes-achievements:open')

    // The contribution batch, the i18n bundle, and the bindApi cleanup each
    // tracked a disposer — deactivate must tear all of them down.
    expect(disposers.length).toBe(3)
  })

  it('unregisters everything when the plugin deactivates', () => {
    const disposers = register()
    disposers.forEach(dispose => dispose())

    expect(registry.getArea(ROUTES_AREA).map(c => c.id)).not.toContain('hermes-achievements:page')
    expect(registry.getArea(SIDEBAR_NAV_AREA).map(c => c.id)).not.toContain('hermes-achievements:nav')
    expect(registry.getArea(STATUSBAR_AREAS.right).map(c => c.id)).not.toContain('hermes-achievements:score')
    expect(registry.getArea(PALETTE_AREA).map(c => c.id)).not.toContain('hermes-achievements:open')
  })
})
