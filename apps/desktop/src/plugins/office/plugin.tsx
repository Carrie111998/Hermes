/**
 * Hermes Office (Claw3d) — a page that manages the hermes-office 3D
 * interface (dev server + gateway adapter) through the Electron bridge.
 * Ships OFF by default — inventories in Settings ▸ Plugins.
 */
import {
  type HermesPlugin,
  host,
  PALETTE_AREA,
  type PaletteContribution,
  type RouteContribution,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  type SidebarNavContribution
} from '@hermes/plugin-sdk'

import { bindOfficeApi } from './api'
import { OFFICE_LOCALES } from './i18n'
import { OfficeScreen } from './office-screen'

const plugin: HermesPlugin = {
  id: 'office',
  name: 'Office',
  description:
    'Hermes Office (Claw3d) — 3D visual interface: install, start/stop the hermes-office dev server and gateway adapter, view logs.',
  defaultEnabled: false,
  register(ctx) {
    ctx.i18n.register(OFFICE_LOCALES)
    ctx.onDispose(bindOfficeApi())

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/office' } satisfies RouteContribution,
        render: () => <OfficeScreen />
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 57,
        data: { codicon: 'browser', label: 'Office', path: '/office' } satisfies SidebarNavContribution
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'office.open',
          label: 'Office: Open Hermes Office',
          keywords: ['office', 'claw3d', '3d', 'hermes office'],
          run: () => host.navigate('/office')
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
