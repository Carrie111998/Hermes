/**
 * Crews — multi-agent crew orchestration: crew list + detail (members,
 * dispatch, live activity feed) and the visual DAG workflow builder.
 * Backend is the bundled `plugins/crews/dashboard/plugin_api.py` router,
 * reached through ctx.rest (/api/plugins/crews/*).
 *
 * Ships OFF by default — it inventories in Settings ▸ Plugins and registers
 * nothing until the user flips the switch (same contract as kanban).
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

import { $createOpen, bindApi } from './api'
import { CrewsScreen } from './crews-screen'
import { CREWS_LOCALES } from './i18n'

const plugin: HermesPlugin = {
  id: 'crews',
  name: 'Crews',
  description:
    'Multi-agent crews — named groups of specialised profile agents, one-shot task dispatch, DAG workflow builder, and a live activity feed.',
  defaultEnabled: false,
  register(ctx) {
    ctx.i18n.register(CREWS_LOCALES)
    ctx.onDispose(bindApi(ctx.rest, ctx.storage, ctx.socket))

    const newCrew = () => {
      $createOpen.set(true)
      host.navigate('/crews')
    }

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/crews' } satisfies RouteContribution,
        render: () => <CrewsScreen />
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 55,
        data: { codicon: 'organization', label: 'Crews', path: '/crews' } satisfies SidebarNavContribution
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'crews.open',
          label: 'Crews: Open crews',
          keywords: ['crews', 'agents', 'multi-agent', 'team'],
          run: () => host.navigate('/crews')
        } satisfies PaletteContribution
      },
      {
        id: 'new-crew',
        area: PALETTE_AREA,
        data: {
          id: 'crews.newCrew',
          label: 'Crews: New crew',
          keywords: ['crews', 'new', 'create', 'team'],
          run: newCrew
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
