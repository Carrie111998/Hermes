/**
 * Workflows — a node canvas for authoring and running agent scenarios: steps,
 * gates, waits and human approvals wired into a graph you can edit by hand or
 * by asking. A `/workflows` page, a sidebar row, and a palette entry.
 *
 * The canvas is schema-driven: `scenario.ts` names every field a step can
 * carry, `graph.ts` is the only thing that mutates the document, and
 * `graph-tools.ts` publishes those same mutations as tool descriptors — so an
 * agent edit and a hand edit are the same operation.
 *
 * That op vocabulary is what makes the page drivable. `bridge.ts` serves the
 * `workflow` tool over it, so Hermes can read and edit the user's workflows
 * from any chat — and the canvas composer is Hermes too, a full agent turn
 * with every tool it normally has. Play starts a gateway run against the
 * HERMES_HOME copy; the canvas tails the same event log. You build by hand,
 * or by asking, or both in the same minute, against one document.
 *
 * Ships OFF by default (`defaultEnabled: false`): it inventories in
 * Settings ▸ Plugins and registers nothing until the user flips the switch.
 */

import './workflows.css'

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

import { bindBridge } from './bridge'
import { bindDocuments } from './documents'
import WorkflowsPage from './page'
import { bindCanvasSession } from './session'

const PATH = '/workflows'

const plugin: HermesPlugin = {
  id: 'workflows',
  name: 'Workflows',
  description: 'Node canvas for agent scenarios — author a graph of steps, gates and approvals, then run it.',
  defaultEnabled: false,
  register(ctx) {
    ctx.onDispose(bindDocuments(ctx.storage))
    // The canvas conversation outlives any one visit to the page, so its id is
    // remembered here rather than minted on mount.
    bindCanvasSession(ctx.storage)
    // Bound at register, not at mount: the `workflow` tool addresses the
    // user's workflows, not their current page, so Hermes can read and edit
    // them from any chat without the canvas being on screen.
    ctx.onDispose(bindBridge())

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PATH } satisfies RouteContribution,
        render: () => <WorkflowsPage />
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 55,
        data: { codicon: 'type-hierarchy-sub', label: 'Workflows', path: PATH } satisfies SidebarNavContribution
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'workflows.open',
          label: 'Workflows: Open canvas',
          keywords: ['workflow', 'scenario', 'graph', 'canvas', 'nodes'],
          run: () => host.navigate(PATH)
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
