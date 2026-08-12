/**
 * Knowledge — interactive force-directed knowledge graph over the
 * ~/.hermes/knowledge markdown wiki, plus page browser, search, and reader.
 * Backend is the bundled plugins/knowledge/dashboard/plugin_api.py router.
 *
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

import { bindApi } from './api'
import { KNOWLEDGE_LOCALES } from './i18n'
import { KnowledgeScreen } from './knowledge-screen'

const plugin: HermesPlugin = {
  id: 'knowledge',
  name: 'Knowledge',
  description:
    'Interactive knowledge graph over the ~/.hermes/knowledge markdown wiki — force-directed canvas, page browser, search, links and backlinks.',
  defaultEnabled: false,
  register(ctx) {
    ctx.i18n.register(KNOWLEDGE_LOCALES)
    ctx.onDispose(bindApi(ctx.rest))

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/knowledge' } satisfies RouteContribution,
        render: () => <KnowledgeScreen />
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 56,
        data: { codicon: 'graph', label: 'Knowledge', path: '/knowledge' } satisfies SidebarNavContribution
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'knowledge.open',
          label: 'Knowledge: Open knowledge graph',
          keywords: ['knowledge', 'graph', 'wiki', 'memory', 'notes'],
          run: () => host.navigate('/knowledge')
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
